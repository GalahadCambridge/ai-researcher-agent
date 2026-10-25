"""最小可用的 LocalExecutor（详细设计 3 第 6 节）。

主干先写了一个能跑冒烟任务的版本，因为启动核对（5.6）依赖它。【实验】同学接手时按文档补齐：
数据校验（6.2 第 3 步）、完整环境快照（5.5，pip freeze、硬件信息）、GPU 串行、OOM 判定等。

LocalExecutor 不在内存里保存任何运行状态，一切都从运行目录的文件读出，所以随时新建实例都能得到一致的结果。
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import psutil

from airesearcher.core.errors import InvalidState, NotFound
from airesearcher.core.fsutil import atomic_write_json, now, read_json, sha256_bytes
from airesearcher.core.models.plan import TaskConfig
from airesearcher.core.models.run import (
    TERMINAL_RUN_STATES,
    MetricRecord,
    Resources,
    RunRecord,
    RunState,
    RunStatus,
)
from airesearcher.core.project import Project

from .envsnap import capture
from .executor import JobHandle, LogChunk, RunOutputs, RunSpec
from .procs import detached_kwargs, kill_tree, pid_matches

HEARTBEAT_STALE_S = 60
ENV_WHITELIST = (
    "PYTHONHASHSEED",
    "OMP_NUM_THREADS",
    "AIR_DATA_DIR",
    "AIR_MODEL_DIR",
    "HF_HOME",
    "HF_HUB_OFFLINE",
    "TRANSFORMERS_OFFLINE",
    "PYTHONDONTWRITEBYTECODE",
)


class UncertainSubmission(InvalidState):
    """A launch marker exists but cannot establish whether submission succeeded."""

    def __init__(self, run_id):
        super().__init__(f"Submission of {run_id} is uncertain; inspect processes before explicitly retrying",
                         {"run_id": run_id})
        self.run_id = run_id


def config_text(config: dict) -> str:
    """config.yaml 用 JSON 写（JSON 是合法的 YAML），这样只用标准库的实验脚本也能读。"""
    return json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def config_sha256(config: dict) -> str:
    return sha256_bytes(config_text(config).encode())


def expand_entrypoint(task: TaskConfig, run_dir: Path, python: str, trial: bool = False) -> list[str]:
    """把 task.yaml 的 entrypoint 占位符展开成实际命令（详细设计 3 第 3.2 节）。

    {config}、{out_dir} 写成相对 code/（实验进程的工作目录）的路径，这样项目目录整体移动、
    或在另一台机器上复现时命令仍然有效。run_dir 参数保留，供以后需要绝对路径的占位符使用。
    """
    values = {"python": python, "config": "../config.yaml", "out_dir": "../outputs", "data_dir": ""}
    cmd = [part.format(**values) for part in task.entrypoint]
    return cmd + (list(task.trial_args) if trial else [])


class LocalExecutor:
    simulation = False

    def __init__(self, project: Project, python: str | None = None):
        self.project = project
        self.root = project.root
        self.python = python or project.config.experiment.get("python") or sys.executable
        self._children: list[subprocess.Popen] = []
        self._captured_environment: dict | None = None

    # ---------- 路径与文件 ----------
    def run_dir(self, run_id: str) -> Path:
        return self.root / "runs" / run_id

    def _write_status(self, st: RunStatus) -> None:
        atomic_write_json(self.run_dir(st.run_id) / "status.json", st)

    def status(self, run_id: str) -> RunStatus:
        p = self.run_dir(run_id) / "status.json"
        if not p.exists():
            if not (self.run_dir(run_id) / "run.json").exists():
                raise NotFound(f"找不到运行 {run_id}")
            return RunStatus(run_id=run_id, state=RunState.unknown, host=platform.node())
        try:
            return RunStatus.model_validate(read_json(p))
        except ValueError:
            return RunStatus(run_id=run_id, state=RunState.unknown, host=platform.node())

    # ---------- 生命周期 ----------
    def probe(self) -> dict[str, Any]:
        return {
            "os": f"{platform.system()} {platform.release()} {platform.machine()}",
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "gpu": None,
        }

    def prepare(self, spec: RunSpec) -> None:
        rec = spec.record
        from .data import CHECKSUMS, input_refs

        if (self.root / CHECKSUMS).exists():
            accepted = input_refs(self.root)
            actual = {r.name: (r.path, r.sha256) for r in rec.inputs}
            if any(actual.get(r.name) != (r.path, r.sha256) for r in accepted):
                raise InvalidState("Run inputs do not match accepted project data_checksums.json")
        d = self.run_dir(rec.run_id)
        # 用相对路径：权限事件日志里不出现本机目录
        self.project.permissions.guard("write", f"runs/{rec.run_id}", actor="executor")
        d.mkdir(parents=True, exist_ok=True)
        (d / "outputs").mkdir(exist_ok=True)
        if not (d / "run.json").exists():  # run.json 创建后不再修改
            atomic_write_json(d / "run.json", rec)
            (d / "config.yaml").write_text(config_text(spec.config), encoding="utf-8")
        self._write_status(RunStatus(run_id=rec.run_id, state=RunState.preparing, host=platform.node()))
        code = d / "code"
        if not (code.exists() and any(code.iterdir())):  # 代码快照：从工作区 git 的 commit 导出 src/
            self.project.workspace.export_src(rec.code_commit, code)
        environment = self._env_snapshot(rec)
        environment["gpu_requested"] = spec.gpu
        atomic_write_json(d / "environment.json", environment)
        self._write_status(RunStatus(run_id=rec.run_id, state=RunState.queued, host=platform.node()))

    def submit(self, spec: RunSpec) -> JobHandle:
        run_id = spec.record.run_id
        self.project.permissions.guard("exec", "local", actor="executor")
        st = self.status(run_id)
        if st.state != RunState.queued:
            raise InvalidState(f"运行 {run_id} 的状态是 {st.state.value}，不能提交")
        d = self.run_dir(run_id)
        # 包装器启动前状态仍为 queued。原子占位，避免下一次 tick/重启重复启动同一训练。
        launch = d / "launch.json"
        try:
            fd = os.open(launch, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            try:
                info = read_json(launch)
            except (OSError, ValueError):
                raise UncertainSubmission(run_id) from None
            if not isinstance(info, dict):
                raise UncertainSubmission(run_id) from None
            if pid_matches(info.get("wrapper_pid"), run_id):
                return JobHandle(run_id=run_id, wrapper_pid=info["wrapper_pid"])
            raise UncertainSubmission(run_id) from None
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"wrapper_pid": None, "submitted_at": now().isoformat()}, f)
        env = os.environ.copy()
        pkg_root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = pkg_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            with open(d / "wrapper.log", "ab") as log:
                # 包装器脱离后台进程（新会话 / Windows 上的独立进程组），后台重启不影响它
                p = subprocess.Popen(
                    [sys.executable, "-m", "airesearcher.runtime.wrapper", str(d)],
                    cwd=d,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=env,
                    **detached_kwargs(),
                )
        except OSError:
            launch.unlink()  # Popen 未成功创建进程，可以安全地重新提交
            raise
        atomic_write_json(launch, {"wrapper_pid": p.pid, "submitted_at": now().isoformat()})
        self._children = [c for c in self._children if c.poll() is None] + [p]
        return JobHandle(run_id=run_id, wrapper_pid=p.pid)

    def mark_submission_lost(self, run_id: str) -> RunStatus:
        """Only after a human decision: retain the old attempt and permit a new ID."""
        status = self.status(run_id)
        if status.state in TERMINAL_RUN_STATES:
            return status
        if status.state not in (RunState.created, RunState.preparing, RunState.queued):
            raise InvalidState("Run is no longer awaiting submission; inspect its current status")
        try:
            launch = read_json(self.run_dir(run_id) / "launch.json")
        except (OSError, ValueError):
            launch = {}
        if not isinstance(launch, dict):
            launch = {}
        if any(pid_matches(pid, run_id) for pid in (status.pid, status.wrapper_pid, launch.get("wrapper_pid"))):
            raise InvalidState("A matching process is still alive; refusing to create a duplicate")
        # Also cover a crash between Popen and recording the wrapper PID.
        directory = str(self.run_dir(run_id).resolve()).casefold()
        for process in psutil.process_iter(["cmdline", "cwd"]):
            try:
                identity = " ".join(process.info.get("cmdline") or []) + " " + (process.info.get("cwd") or "")
                if directory in identity.casefold() and process.status() != psutil.STATUS_ZOMBIE:
                    raise InvalidState("A process still belongs to this run; inspect/cancel it before retrying")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        self.project.permissions.guard("write", f"runs/{run_id}", actor="executor")
        status = status.model_copy(update={"state": RunState.failed, "failure_reason": "lost", "ended_at": now()})
        self._write_status(status)
        self.collect(run_id)
        return status

    def logs(self, run_id: str, stream: str = "stdout", offset: int = 0) -> LogChunk:
        if stream not in ("stdout", "stderr"):
            raise ValueError("stream 只能是 stdout 或 stderr")
        p = self.run_dir(run_id) / f"{stream}.log"
        data = b""
        if p.exists():
            with open(p, "rb") as f:
                f.seek(offset)
                data = f.read(1 << 20)
        terminal = self.status(run_id).state in TERMINAL_RUN_STATES
        next_offset = offset + len(data)
        size = p.stat().st_size if p.exists() else 0
        return LogChunk(
            text=data.decode("utf-8", errors="replace"), next_offset=next_offset, eof=terminal and next_offset >= size
        )

    def cancel(self, run_id: str) -> None:
        d = self.run_dir(run_id)
        st = self.status(run_id)
        if st.state in TERMINAL_RUN_STATES:
            return
        (d / "cancel_requested").write_text(now().isoformat(), encoding="utf-8")
        # 包装器已经不在时，由执行器直接终止（只结束命令行含 run_id 的进程及其子进程）
        if not pid_matches(st.wrapper_pid, run_id) and pid_matches(st.pid, run_id) and st.pid:
            kill_tree(st.pid)

    def collect(self, run_id: str) -> RunOutputs:
        """已结束的运行：登记档案（只记清单）、按 resources.json 记 CPU/GPU 时间。可重复调用。"""
        st = self.status(run_id)
        d = self.run_dir(run_id)
        res = Resources.model_validate(read_json(d / "resources.json")) if (d / "resources.json").exists() else None
        mpath = d / "metrics.jsonl"
        lines = mpath.read_text(encoding="utf-8").splitlines() if mpath.exists() else []
        metrics = [MetricRecord.model_validate(json.loads(line)) for line in lines if line.strip()]
        if st.state in TERMINAL_RUN_STATES and self.project.archive.latest(f"runs/{run_id}") is None:
            self.project.archive.put(
                f"runs/{run_id}", "run", d, producer="agent:experiment/executor", note=f"运行结束：{st.state.value}"
            )
            if res is not None:
                self.project.budget.charge("cpu_hours", res.cpu_seconds / 3600, f"run:{run_id}")
                if res.gpu_seconds:
                    self.project.budget.charge("gpu_hours", res.gpu_seconds / 3600, f"run:{run_id}")
        files = (
            sorted(p.relative_to(d).as_posix() for p in (d / "outputs").rglob("*") if p.is_file())
            if (d / "outputs").exists()
            else []
        )
        return RunOutputs(run_id=run_id, status=st, metrics=metrics, resources=res, files=files)

    def reconcile(self, run_id: str) -> RunStatus:
        """后台重启后核对（详细设计 3 第 6.5 节）。"""
        try:
            st = self.status(run_id)
        except NotFound:
            return RunStatus(run_id=run_id, state=RunState.unknown, host=platform.node())
        if st.state in TERMINAL_RUN_STATES:
            self.collect(run_id)
            return st
        if st.state in (RunState.queued, RunState.preparing, RunState.created):
            if st.state != RunState.queued:
                st = st.model_copy(update={"state": RunState.queued})
                self._write_status(st)
            return st
        if st.state == RunState.running:
            alive = pid_matches(st.pid, run_id) or pid_matches(st.wrapper_pid, run_id)
            if not alive:
                st = st.model_copy(update={"state": RunState.failed, "failure_reason": "lost", "ended_at": now()})
                self._write_status(st)
                self.collect(run_id)
                return st
            hb = st.heartbeat_at or st.started_at
            if hb is not None and now() - hb > timedelta(seconds=HEARTBEAT_STALE_S):
                return st.model_copy(update={"state": RunState.unknown})
            return st
        return st

    def snapshot_env(self, run_id: str) -> dict:
        p = self.run_dir(run_id) / "environment.json"
        return read_json(p) if p.exists() else {}

    def _env_snapshot(self, rec: RunRecord) -> dict:
        info = self.probe()
        env_vars = {k: os.environ[k] for k in ENV_WHITELIST if k in os.environ}
        env_vars.setdefault("PYTHONHASHSEED", "0")
        snap = {
            "os": info["os"],
            "python": info["python"],
            "packages_lock": None,
            "hardware": {
                "cpu": platform.processor() or platform.machine(),
                "cores": info["cpu_count"],
                "memory_gb": psutil.virtual_memory().total / (1024**3),
                "gpu": None,
                "cuda": None,
            },
            "frameworks": {},
            "env_vars": env_vars,
            "workdir": f"runs/{rec.run_id}/code",
            "command": rec.command,
            "inputs": [i.model_dump() for i in rec.inputs],
            "unknown": ["Package lock and GPU details are not captured for simulated runs."],
        }
        if rec.executor == "local":
            if self._captured_environment is None:
                self._captured_environment = capture(self.root, self.python, guard=self.project.permissions.guard)
            captured = self._captured_environment
            snap.update(
                python=captured["python"] or snap["python"],
                python_executable=captured["executable"],
                packages_lock=captured["lock_path"],
                packages_lock_sha256=captured["lock_sha256"],
                frameworks=captured["frameworks"],
            )
            snap["hardware"]["gpu"] = captured["gpu"]
            snap["unknown"] = [*captured["unknown"], "Exact CUDA kernel utilization is not measured.",
                               "CUDA toolkit runtime version"]
        snap["env_hash"] = sha256_bytes(
            json.dumps(
                {k: snap.get(k) for k in ("os", "python", "frameworks", "packages_lock_sha256")}, sort_keys=True
            ).encode()
        )[:12]
        return snap
