"""真实的崩溃恢复（详细设计 1 第 12.2 节）：启动 air serve 子进程，用 kill -9 强制结束，再启动。

与 test_recovery.py 的区别：那里是正常关闭后台；这里后台被强制杀死（Windows 上是 TerminateProcess），
项目锁、检查点、正在写的文件都来不及收尾。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import psutil
import pytest

from airesearcher.core.workspace import load_registry

pytestmark = pytest.mark.timeout(240)
DEV = {"smoke_seconds": 3, "poll_seconds": 1, "compile": False}


class Server:
    """一个真实的 air serve 进程。"""

    def __init__(self, home: Path):
        self.home = home
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen | None = None

    def start(self) -> Server:
        self.home.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "AIR_HOME": str(self.home), "PYTHONIOENCODING": "utf-8"}
        log = open(self.home / f"serve-{time.time_ns()}.log", "wb")  # noqa: SIM115
        self.proc = subprocess.Popen(
            [sys.executable, "-c", "from airesearcher.cli.main import app; app()", "serve", "--port", str(self.port)],
            env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        t0 = time.time()
        while time.time() - t0 < 30:
            try:
                if httpx.get(f"{self.url}/api/health", timeout=1).status_code == 200:
                    return self
            except httpx.HTTPError:
                time.sleep(0.2)
        raise AssertionError("air serve 没有启动")

    def kill(self) -> None:
        assert self.proc is not None
        self.proc.kill()  # SIGKILL / TerminateProcess：不给任何收尾机会
        self.proc.wait(10)

    def api(self, method: str, path: str, **kw):
        r = httpx.request(method, f"{self.url}{path}", timeout=30, **kw)
        assert r.status_code < 400, r.text
        return r.json()

    def wait_state(self, pid: str, state: str, timeout: float = 120) -> dict:
        t0 = time.time()
        while time.time() - t0 < timeout:
            st = self.api("GET", f"/api/projects/{pid}")
            if st["state"] == state:
                return st
            time.sleep(0.3)
        raise AssertionError(f"等不到 {state}，现在是 {st['state']}：{st['blocking_reason']}")

    def approve(self, pid: str) -> None:
        [a] = self.api("GET", f"/api/projects/{pid}/approvals", params={"status": "pending"})
        self.api("POST", f"/api/projects/{pid}/approvals/{a['approval_id']}/decision",
                 json={"decision": "approved", "expected_sha256": a["target"]["sha256"],
                       "request_id": str(uuid.uuid4())})


def statuses(root: Path) -> dict[str, dict]:
    out = {}
    for p in (root / "runs").glob("*/status.json"):
        try:
            out[p.parent.name] = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return out


def start_project_until_running(srv: Server) -> tuple[str, Path, str]:
    pid = srv.api("POST", "/api/projects", json={"idea_text": "比较基线、主方法和消融在冒烟任务上的得分",
                                                 "task": "tasks/smoke", "dev": DEV, "request_id": "c1"})["project_id"]
    srv.wait_state(pid, "IdeaPending")
    srv.approve(pid)
    srv.wait_state(pid, "PlanPending")
    srv.approve(pid)
    root = Path(load_registry(srv.home)[pid])
    t0 = time.time()
    while time.time() - t0 < 60:
        running = [rid for rid, s in statuses(root).items() if s["state"] == "running" and s.get("pid")]
        if running:
            return pid, root, running[0]
        time.sleep(0.1)
    raise AssertionError("没有运行开始")


def test_kill_backend_while_run_is_running(air_home):
    srv = Server(air_home).start()
    try:
        pid, root, rid = start_project_until_running(srv)
        srv.kill()
        before = set(statuses(root))
        # 后台停机期间运行继续（包装器是独立进程），并且跑完
        t0 = time.time()
        while statuses(root)[rid]["state"] not in ("succeeded", "failed"):
            assert time.time() - t0 < 60
            time.sleep(0.2)
        assert statuses(root)[rid]["state"] == "succeeded"
        assert set(statuses(root)) == before  # 停机期间没有新运行

        srv.start()  # 项目锁由操作系统随进程释放，新后台能接管
        srv.wait_state(pid, "LogPending", timeout=180)
        runs = srv.api("GET", f"/api/projects/{pid}/runs")
        assert len(runs) == 9 and len({r["task_key"] for r in runs}) == 9  # 没有重复运行
        assert {r["state"] for r in runs} == {"succeeded"}
        assert before <= {r["run_id"] for r in runs}
        log = srv.api("GET", f"/api/projects/{pid}/runs/{rid}/logs")
        assert "@@AIR_METRIC" in log["text"]  # 日志完整
        events = srv.api("GET", f"/api/projects/{pid}/events", params={"type": "run.reconciled"})
        assert rid in {e["run_id"] for e in events}
    finally:
        if srv.proc and srv.proc.poll() is None:
            srv.kill()


def test_kill_experiment_while_backend_down(air_home):
    srv = Server(air_home).start()
    try:
        pid, root, rid = start_project_until_running(srv)
        srv.kill()
        st = statuses(root)[rid]
        for p in (st.get("wrapper_pid"), st.get("pid")):  # 实验进程和包装器都被杀：没人写结束状态
            if p and psutil.pid_exists(p):
                psutil.Process(p).kill()
        srv.start()
        t0 = time.time()
        while statuses(root)[rid]["state"] != "failed":
            assert time.time() - t0 < 30
            time.sleep(0.2)
        assert statuses(root)[rid]["failure_reason"] == "lost"
        st = srv.wait_state(pid, "Paused", timeout=120)  # 实验阶段对失败的运行提问：跳过 / 重试 / 结束
        assert st["pending_question"] and rid in st["pending_question"]["text"]
        runs = srv.api("GET", f"/api/projects/{pid}/runs")
        assert sum(1 for r in runs if r["run_id"] == rid) == 1
    finally:
        if srv.proc and srv.proc.poll() is None:
            srv.kill()
