"""SSE 推送（详细设计 1 第 9.5 节）：每秒检查一次事件日志和项目状态，有变化就推一条消息。

| event      | data                                              | 什么时候推 |
|------------|---------------------------------------------------|-----------|
| `state`    | `{state, stage_label, blocking_reason}`           | 连接建立时推一次；之后有变化时 |
| `event`    | 新的 `Event`（`id:` = 事件 seq）                   | 事件日志有新行时 |
| `approval` | `{approval_id, status}`                           | 审批被提交、决定、取代时 |
| `question` | `{question_id, status}`                           | 提问、回答、撤回时 |
| `run`      | `{run_id, status}`（加标签时另有 `label`）         | 运行创建、状态变化（含开始运行）、加标签时 |
| `budget`   | `BudgetStatus`                                    | 连接建立时推一次；之后有变化时 |

SSE 只做“通知”，REST 才是数据来源。断线重连时浏览器自动带 Last-Event-ID（= 事件 seq），服务端补发之后的 event。

运行日志的逐行推送见 RunLogStream（路由在 routes/runs.py 的 `/runs/{run_id}/logs/stream`）：
每行一条 `line` 消息 `{text}`，`id:` 是这一行结束处的字节位置（断线重连时从这里继续）；
运行结束且日志读完后推一条 `end` 消息 `{run_id, status}` 并关闭连接——浏览器收到 `end` 后要调用 `close()`，
否则 EventSource 会自动重连（重连后会立刻再收到 `end`，不会重复推日志）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from airesearcher.core.errors import NotFound
from airesearcher.core.models.event import Event
from airesearcher.services import runs as run_service

from .deps import manager
from .manager import ProjectManager

router = APIRouter(prefix="/api/projects/{pid}", tags=["stream"])
POLL_SECONDS = 1.0
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
LOG_CHUNK_BYTES = 1 << 20  # LocalExecutor.logs() 一次最多读这么多

APPROVAL_STATUS = {"approval.requested": "pending", "approval.superseded": "superseded"}
QUESTION_STATUS = {"question.asked": "pending", "question.answered": "answered", "question.withdrawn": "withdrawn"}


def sse_message(event: str, data: dict, id: int | str | None = None) -> str:
    head = f"id: {id}\n" if id is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def derived(ev: Event) -> tuple[str, dict] | None:
    """从一条事件推出 approval / question / run 通知。"""
    d = ev.data or {}
    if ev.type.startswith("approval.") and "approval_id" in d:
        status = d.get("decision") if ev.type == "approval.decided" else APPROVAL_STATUS.get(ev.type, ev.type)
        return "approval", {"approval_id": d["approval_id"], "status": status}
    if ev.type in QUESTION_STATUS and "question_id" in d:
        return "question", {"question_id": d["question_id"], "status": QUESTION_STATUS[ev.type]}
    if ev.type.startswith("run.") and ev.run_id:
        out = {"run_id": ev.run_id, "status": d.get("state") or ev.type.removeprefix("run.")}
        if "label" in d:
            out["label"] = d["label"]
        return "run", out
    return None


class ProjectStream:
    """一个 SSE 连接的状态。poll() 返回这一秒要推送的消息（已格式化），不 sleep，方便测试。"""

    def __init__(self, m: ProjectManager, pid: str, last_seq: int = 0):
        self.m, self.pid, self.last_seq = m, pid, last_seq
        self.project = m.get(pid)
        self.last_state: dict | None = None
        self.last_budget: dict | None = None
        self.runs: dict[str, str] = {}  # 上一次看到的活动运行 → 状态

    def poll(self) -> list[str]:
        out: list[str] = []
        st = self.m.status(self.pid)
        brief = {"state": st.state.value, "stage_label": st.stage_label, "blocking_reason": st.blocking_reason}
        if brief != self.last_state:
            self.last_state = brief
            out.append(sse_message("state", brief))
        budget = st.budget.model_dump(mode="json")
        if budget != self.last_budget:
            self.last_budget = budget
            out.append(sse_message("budget", budget))

        notified: set[str] = set()
        for ev in self.project.events.query(after_seq=self.last_seq, limit=500):
            self.last_seq = ev.seq
            out.append(sse_message("event", ev.model_dump(mode="json"), id=ev.seq))
            d = derived(ev)
            if d is not None:
                out.append(sse_message(*d))
                if d[0] == "run":
                    notified.add(d[1]["run_id"])

        # 包装器直接写 status.json（如 queued → running），不一定有事件，所以再比对一次活动运行的状态
        now_runs = {r.run_id: r.state.value for r in st.active_runs}
        for rid in set(self.runs) | set(now_runs):
            state = now_runs.get(rid)
            if state is None:  # 已不在活动列表：结束了
                try:
                    state = run_service.load_run(self.project.root, rid, with_metrics=False).status.state.value
                except NotFound:
                    continue
            if self.runs.get(rid) != state and rid not in notified:
                out.append(sse_message("run", {"run_id": rid, "status": state}))
        self.runs = now_runs
        return out


class RunLogStream:
    """一个运行日志订阅。poll() 返回 (消息列表, 是否结束)。只推完整的行；运行结束后把最后半行也推出去。"""

    def __init__(self, executor, run_id: str, stream: str = "stdout", offset: int = 0):
        self.executor, self.run_id, self.stream, self.offset = executor, run_id, stream, offset
        self.done = False

    def poll(self) -> tuple[list[str], bool]:
        if self.done:
            return [], True
        out: list[str] = []
        chunk = self.executor.logs(self.run_id, self.stream, self.offset)  # 一次最多读 1 MB
        text = chunk.text
        lines = text.split("\n")
        rest = lines.pop()  # 最后一段没有换行：还没写完，下次再读（除非运行已结束，或一行超过 1 MB）
        for line in lines:
            self.offset += len(line.encode("utf-8")) + 1
            out.append(sse_message("line", {"text": line}, id=self.offset))
        if rest and (chunk.eof or not lines and chunk.next_offset - self.offset >= LOG_CHUNK_BYTES):
            self.offset = chunk.next_offset
            out.append(sse_message("line", {"text": rest}, id=self.offset))
        if chunk.eof and self.offset >= chunk.next_offset:
            status = self.executor.status(self.run_id).state.value
            out.append(sse_message("end", {"run_id": self.run_id, "status": status}, id=self.offset))
            self.done = True
        return out, self.done


@router.get("/stream")
async def stream(pid: str, request: Request, from_now: bool = False,
                 m: ProjectManager = Depends(manager)) -> StreamingResponse:
    """from_now=true：不补推连接之前的历史事件（页面刚打开时用；断线重连时浏览器带的 Last-Event-ID 优先）。"""
    header = request.headers.get("last-event-id")
    try:
        last_seq = int(header) if header else (m.get(pid).events.last_seq if from_now else 0)
    except ValueError:
        last_seq = 0
    ps = ProjectStream(m, pid, last_seq)

    async def gen() -> AsyncIterator[str]:
        while not m.closing.is_set() and not await request.is_disconnected():
            for msg in ps.poll():
                yield msg
            await asyncio.sleep(POLL_SECONDS)

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)
