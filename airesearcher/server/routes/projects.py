"""项目、问题、预算、事件（详细设计 1 第 9.2 节“项目”）。"""

from __future__ import annotations

import yaml
from fastapi import APIRouter, Depends, Query

from airesearcher.core.errors import ValidationFailed
from airesearcher.core.i18n import Msg
from airesearcher.core.models.budget import BudgetStatus
from airesearcher.core.models.event import Event
from airesearcher.core.models.project import Limit
from airesearcher.core.models.question import Question
from airesearcher.core.workspace import repo_root
from airesearcher.engine import checkpoint as ckpt
from airesearcher.engine.checkpoint import CheckpointBrief

from ..deps import manager
from ..manager import ProjectManager, localize_question, project_defaults
from ..schemas import (
    ActionRequest,
    AnswerRequest,
    BudgetPatch,
    CreateProject,
    ProjectDefaults,
    ProjectStatus,
    ProjectSummary,
    RollbackRequest,
    TaskInfo,
)

router = APIRouter(prefix="/api", tags=["projects"])


@router.get("/tasks", response_model=list[TaskInfo])
def list_tasks() -> list[TaskInfo]:
    out = []
    for p in sorted((repo_root() / "tasks").glob("*/task.yaml")):
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        out.append(TaskInfo(name=data.get("name", p.parent.name), path=f"tasks/{p.parent.name}",
                            domain=data.get("domain", ""), description=data.get("description", "")))
    return out


@router.get("/defaults", response_model=ProjectDefaults)
def get_defaults() -> ProjectDefaults:
    """新建项目页用的默认值：任务、运行模式、预算（建项目时 budget 只需传要改的类别）。"""
    return project_defaults()


@router.get("/projects", response_model=list[ProjectSummary])
def list_projects(m: ProjectManager = Depends(manager)) -> list[ProjectSummary]:
    rows = [m.summary(pid) for pid in list(m.projects)]
    return sorted(rows, key=lambda r: r.created_at, reverse=True)


@router.post("/projects", response_model=ProjectSummary)
def create_project(body: CreateProject, m: ProjectManager = Depends(manager)) -> ProjectSummary:
    if not body.idea_text.strip():
        raise ValidationFailed(msg=Msg(code="error.need_idea"))
    try:
        p = m.create(body)
    except FileNotFoundError as e:
        raise ValidationFailed(str(e)) from e
    return m.summary(p.project_id)


@router.get("/projects/{pid}", response_model=ProjectStatus)
def get_status(pid: str, m: ProjectManager = Depends(manager)) -> ProjectStatus:
    return m.status(pid)


@router.post("/projects/{pid}/actions", response_model=ProjectStatus)
def project_action(pid: str, body: ActionRequest, m: ProjectManager = Depends(manager)) -> ProjectStatus:
    m.engine(pid).request_action(body.action, body.request_id)
    return m.status(pid)


@router.get("/projects/{pid}/checkpoints", response_model=list[CheckpointBrief])
def list_checkpoints(pid: str, m: ProjectManager = Depends(manager)) -> list[CheckpointBrief]:
    """可回滚的检查点，从新到旧。milestone=true 的是状态切换时保存的，长期保留；其余只保留最近 20 个。"""
    return ckpt.briefs(m.get(pid).root)


@router.post("/projects/{pid}/rollback", response_model=ProjectStatus)
def rollback(pid: str, body: RollbackRequest, m: ProjectManager = Depends(manager)) -> ProjectStatus:
    """回到历史检查点（项目正在工作时须先暂停）。不删除任何产物或运行；之后选择“继续”从那里重新开始。"""
    m.engine(pid).request_action("rollback", body.request_id, checkpoint_id=body.checkpoint_id)
    return m.status(pid)


@router.get("/projects/{pid}/question", response_model=Question | None)
def get_question(pid: str, m: ProjectManager = Depends(manager)) -> Question | None:
    return localize_question(m.get(pid).questions.pending())


@router.post("/projects/{pid}/question/answer", response_model=ProjectStatus)
def answer_question(pid: str, body: AnswerRequest, m: ProjectManager = Depends(manager)) -> ProjectStatus:
    m.get(pid).questions.answer(body.question_id, body.choice, body.text, request_id=body.request_id)
    m.wait_settled(pid, lambda e: e.ck.pending_question != body.question_id)
    return m.status(pid)


@router.patch("/projects/{pid}/budget", response_model=BudgetStatus)
def patch_budget(pid: str, body: BudgetPatch, m: ProjectManager = Depends(manager)) -> BudgetStatus:
    p = m.get(pid)
    cfg = p.config.model_copy(deep=True)
    changes = {k: v for k, v in (body.model_extra or {}).items()}
    for cat, lim in changes.items():
        if not isinstance(lim, dict):
            raise ValidationFailed(f"{cat} 应为 {{soft, hard}}")
        cur = cfg.budget.get(cat) or Limit()
        cfg.budget[cat] = Limit.model_validate({**cur.model_dump(), **lim})
    if changes:
        p.save_config(cfg, summary=f"调整预算上限：{changes}", actor="user")
    return p.budget.check()


@router.get("/projects/{pid}/events", response_model=list[Event])
def list_events(pid: str, after_seq: int = 0, type: str | None = None, run_id: str | None = None,
                q: str | None = None, artifact_id: str | None = None, limit: int = 200,
                tail: int | None = Query(None, ge=1, le=1000,
                                         description="只取符合条件的最新 N 条（仍按时间正序返回）；给了它就忽略 limit"),
                m: ProjectManager = Depends(manager)) -> list[Event]:
    types = [t for t in (type or "").split(",") if t] or None
    return m.get(pid).events.query(types=types, run_id=run_id, artifact_id=artifact_id, text=q,
                                   after_seq=after_seq, limit=limit, tail=tail)
