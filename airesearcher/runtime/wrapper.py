"""运行包装器（详细设计 3 第 6.3 节）：python -m airesearcher.runtime.wrapper <run_dir>

由 LocalExecutor.submit() 以新会话启动，脱离后台进程。它是 status.json 和 metrics.jsonl 的唯一写入者：
1. 以 code/ 为工作目录、用 run.json 中的命令启动实验进程（新进程组），状态写为 running；
2. 心跳线程每 AIR_HEARTBEAT_S 秒（默认 10）更新 heartbeat_at；监视线程约每秒采样一次 CPU 时间和内存（psutil）；
3. 读实验进程 stdout：原样写 stdout.log；@@AIR_METRIC 行另外追加到 metrics.jsonl；stderr 写 stderr.log；
4. 检查超时和 cancel_requested 文件；
5. 进程结束：写 resources.json；按 5.4 判定结果；写 status.json 终态；
6. 包装器自身出错：尽量写 state=failed、failure_reason=wrapper_error 后退出。
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path

from airesearcher.core.fsutil import atomic_write_json, now, read_json
from airesearcher.core.models.run import Resources, RunRecord, RunState, RunStatus

from .procs import kill_tree, new_group_kwargs, tree_usage

try:  # 只有 Mac / Linux 有；Windows 上用采样值
    import resource
except ImportError:  # pragma: no cover - Windows
    resource = None  # type: ignore[assignment]

METRIC_PREFIX = b"@@AIR_METRIC "


class Wrapper:
    def __init__(self, run_dir: Path):
        self.d = Path(run_dir).resolve()
        self.rec = RunRecord.model_validate(read_json(self.d / "run.json"))
        self.lock = threading.Lock()
        self.status = RunStatus(run_id=self.rec.run_id, state=RunState.running, host=platform.node(),
                                wrapper_pid=os.getpid())
        self.final = False
        self.cancelled = False
        self.timed_out = False
        self.proc: subprocess.Popen | None = None
        self.metric_seq = 0
        self.metric_names: set[str] = set()
        self.heartbeat_s = float(os.environ.get("AIR_HEARTBEAT_S", "10"))
        self.cpu_seen = 0.0  # 采样到的进程树 CPU 秒数（取最大值）
        self.mem_peak = 0.0  # 采样到的内存峰值（MB）

    def write_status(self, **updates: object) -> None:
        with self.lock:
            if self.final:
                return
            self.status = self.status.model_copy(update=updates)
            if self.status.state not in (RunState.running,):
                self.final = True
            atomic_write_json(self.d / "status.json", self.status)

    def _heartbeat(self) -> None:
        while self.proc is not None and self.proc.poll() is None:
            self.write_status(heartbeat_at=now())
            time.sleep(self.heartbeat_s)

    def _kill(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        kill_tree(self.proc.pid, timeout=10)  # 先 terminate，10 秒后仍在则 kill（5.4）

    def _watchdog(self, t0: float) -> None:
        last_sample = 0.0
        while self.proc is not None and self.proc.poll() is None:
            if time.monotonic() - last_sample >= 1.0:
                last_sample = time.monotonic()
                cpu, mem = tree_usage(self.proc.pid)
                self.cpu_seen = max(self.cpu_seen, cpu)
                self.mem_peak = max(self.mem_peak, mem)
            if (self.d / "cancel_requested").exists():
                self.cancelled = True
                self._kill()
                return
            if time.monotonic() - t0 > self.rec.timeout_s:
                self.timed_out = True
                self._kill()
                return
            time.sleep(0.3)

    def run(self) -> int:
        env = os.environ.copy()
        env.update({"PYTHONHASHSEED": "0", "AIR_RUN_ID": self.rec.run_id, "AIR_RUN_DIR": str(self.d),
                    "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})  # Windows 默认不是 UTF-8
        t0 = time.monotonic()
        with open(self.d / "stdout.log", "ab") as out, open(self.d / "stderr.log", "ab") as err, \
                open(self.d / "metrics.jsonl", "ab") as metrics:
            self.proc = subprocess.Popen(self.rec.command, cwd=self.d / "code", stdout=subprocess.PIPE, stderr=err,
                                         stdin=subprocess.DEVNULL, env=env, **new_group_kwargs())
            started = now()
            self.write_status(state=RunState.running, pid=self.proc.pid, pgid=self.proc.pid, started_at=started,
                              heartbeat_at=started)
            threading.Thread(target=self._heartbeat, daemon=True).start()
            threading.Thread(target=self._watchdog, args=(t0,), daemon=True).start()
            assert self.proc.stdout is not None
            for line in iter(self.proc.stdout.readline, b""):
                out.write(line)
                out.flush()
                if line.startswith(METRIC_PREFIX):
                    self._metric(line[len(METRIC_PREFIX):], metrics)
            rc = self.proc.wait()
        cpu, rss = self.cpu_seen, self.mem_peak
        if resource is not None:  # Mac / Linux：用操作系统统计的精确值
            usage = resource.getrusage(resource.RUSAGE_CHILDREN)
            cpu = usage.ru_utime + usage.ru_stime
            rss = max(rss, usage.ru_maxrss / (1024 * 1024 if sys.platform == "darwin" else 1024))
        wall = round(time.monotonic() - t0, 3)
        config = read_json(self.d / "config.yaml")  # LocalExecutor 固定写 JSON 格式
        environment_path = self.d / "environment.json"
        environment = read_json(environment_path) if environment_path.is_file() else {}
        gpu = environment.get("gpu_requested", config.get("device") == "cuda")
        # 按 GPU 串行任务占用的墙钟时间记预算，不代表 CUDA kernel 活跃时间。
        atomic_write_json(self.d / "resources.json", Resources(
            wall_seconds=wall, cpu_seconds=round(cpu, 3),
            gpu_seconds=wall if gpu else 0.0, max_rss_mb=round(rss, 1)))
        self.write_status(**self._verdict(rc), ended_at=now(), exit_code=rc)
        return 0

    def _metric(self, payload: bytes, metrics) -> None:
        try:
            m = json.loads(payload)
            self.metric_seq += 1
            row = {"seq": self.metric_seq, "ts": now().isoformat(), "name": str(m["name"]),
                   "value": float(m["value"]), "split": m.get("split"), "step": m.get("step")}
        except (ValueError, KeyError, TypeError):
            return
        self.metric_names.add(row["name"])
        metrics.write((json.dumps(row, ensure_ascii=False) + "\n").encode())
        metrics.flush()

    def _verdict(self, rc: int) -> dict:
        """按详细设计 3 第 5.4 节判定结果。"""
        if self.cancelled:
            return {"state": RunState.cancelled, "failure_reason": "cancelled"}
        if self.timed_out:
            return {"state": RunState.failed, "failure_reason": "timeout"}
        if rc != 0:
            stderr = (self.d / "stderr.log").read_text(encoding="utf-8", errors="replace")[-4000:].lower()
            oom = rc in (137, -9) or "out of memory" in stderr
            return {"state": RunState.failed, "failure_reason": "oom" if oom else "nonzero_exit"}
        missing = [m for m in self.rec.required_metrics if m not in self.metric_names]
        if missing:
            return {"state": RunState.failed, "failure_reason": "missing_metrics"}
        return {"state": RunState.succeeded, "failure_reason": None}


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    run_dir = Path(argv[0])
    w = None
    try:
        w = Wrapper(run_dir)
        return w.run()
    except Exception as e:  # 包装器自身出错
        try:
            st = (w.status if w else RunStatus(run_id=run_dir.name, state=RunState.running, host=platform.node()))
            st = st.model_copy(update={"state": RunState.failed, "failure_reason": "wrapper_error",
                                       "ended_at": now(), "error_class": type(e).__name__})
            atomic_write_json(run_dir / "status.json", st)
        finally:
            print(f"wrapper error: {e!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
