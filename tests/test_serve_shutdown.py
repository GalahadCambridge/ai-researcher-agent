"""air serve 开着 SSE 连接时按 Ctrl+C 应在几秒内退出（真实子进程）。"""

from __future__ import annotations

import signal
import sys
import threading
import time

import httpx
import pytest

from .test_kill_recovery import DEV, Server

pytestmark = [
    pytest.mark.timeout(120),
    pytest.mark.skipif(sys.platform == "win32", reason="Windows 上给子进程发 Ctrl+C 不可靠"),
]


def test_sigint_with_open_stream_exits_quickly(air_home):
    path = "/stream"
    srv = Server(air_home).start()
    try:
        body = {"idea_text": "比较基线、主方法和消融在冒烟任务上的得分", "task": "tasks/smoke", "dev": DEV,
                "request_id": "s1"}
        pid = srv.api("POST", "/api/projects", json=body)["project_id"]
        got = threading.Event()

        def hold_stream() -> None:
            try:
                with httpx.stream("GET", f"{srv.url}/api/projects/{pid}{path}", timeout=None) as r:
                    for _ in r.iter_lines():
                        got.set()
            except httpx.HTTPError:
                pass  # 服务端关闭连接

        threading.Thread(target=hold_stream, daemon=True).start()
        assert got.wait(15), "没有收到 SSE 消息"
        srv.proc.send_signal(signal.SIGINT)
        t0 = time.time()
        srv.proc.wait(10)
        assert time.time() - t0 < 6
        log = max(air_home.glob("serve-*.log")).read_text(encoding="utf-8", errors="replace")
        assert "CancelledError" not in log
    finally:
        if srv.proc and srv.proc.poll() is None:
            srv.kill()
