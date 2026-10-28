"""运行（数据由【实验】的文件提供，接口由主干实现；详细设计 1 第 9.2 节“运行”）。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from airesearcher.core.errors import ValidationFailed
from airesearcher.core.models.run import RunLabel, RunStatus, RunSummary, RunView
from airesearcher.runtime.executor import LogChunk
from airesearcher.services import runs as run_service

from ..deps import manager
from ..manager import ProjectManager
from ..schemas import LabelRequest, RequestIdOnly
from ..sse import POLL_SECONDS, SSE_HEADERS, RunLogStream

router = APIRouter(prefix="/api/projects/{pid}/runs", tags=["runs"])


@router.get("", response_model=list[RunSummary])
def list_runs(pid: str, experiment_id: str | None = None, status: str | None = None,
              m: ProjectManager = Depends(manager)) -> list[RunSummary]:
    return [r.summary() for r in run_service.list_runs(m.get(pid).root, experiment_id=experiment_id, state=status)]


@router.get("/{run_id}", response_model=RunView)
def get_run(pid: str, run_id: str, m: ProjectManager = Depends(manager)) -> RunView:
    return run_service.load_run(m.get(pid).root, run_id)


@router.get("/{run_id}/logs", response_model=LogChunk)
def get_logs(pid: str, run_id: str, stream: str = "stdout", offset: int = 0,
             m: ProjectManager = Depends(manager)) -> LogChunk:
    return m.engine(pid).executor.logs(run_id, stream, offset)


@router.get("/{run_id}/logs/stream")
async def stream_logs(pid: str, run_id: str, request: Request, stream: str = "stdout", offset: int = 0,
                      m: ProjectManager = Depends(manager)) -> StreamingResponse:
    """SSE：逐行推送运行日志，运行结束、日志读完后推 end 并关闭（详细设计 1 第 9.2 节）。"""
    ex = m.engine(pid).executor
    ex.status(run_id)  # 运行不存在时先返回 404
    if stream not in ("stdout", "stderr"):
        raise ValidationFailed("stream 只能是 stdout 或 stderr")
    try:
        offset = int(request.headers.get("last-event-id", offset))
    except ValueError:
        pass
    ls = RunLogStream(ex, run_id, stream, offset)

    async def gen() -> AsyncIterator[str]:
        while not m.closing.is_set() and not await request.is_disconnected():
            messages, done = ls.poll()
            for msg in messages:
                yield msg
            if done:
                return
            await asyncio.sleep(POLL_SECONDS)

    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post("/{run_id}/cancel", response_model=RunStatus)
def cancel_run(pid: str, run_id: str, body: RequestIdOnly, m: ProjectManager = Depends(manager)) -> RunStatus:
    ex = m.engine(pid).executor
    ex.cancel(run_id)
    m.get(pid).events.append("run.cancel_requested", f"用户请求取消运行 {run_id}", actor="user", run_id=run_id)
    return ex.status(run_id)


@router.post("/{run_id}/label", response_model=RunLabel)
def label_run(pid: str, run_id: str, body: LabelRequest, m: ProjectManager = Depends(manager)) -> RunLabel:
    """人工标注运行：trusted / suspicious / invalid。invalid 的运行在汇总中排除，suspicious 的保留但标出（FR-37）。"""
    return m.label_run(pid, run_id, body.label, body.reason, body.request_id)
