"""后台进程内的项目管理：打开注册表中的项目、启动核对（5.6）、每个项目一个引擎线程。"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from airesearcher.core import templates
from airesearcher.core.errors import NotFound, ValidationFailed
from airesearcher.core.fsutil import append_jsonl, now
from airesearcher.core.i18n import Msg, render, t
from airesearcher.core.models.approval import ApprovalBrief
from airesearcher.core.models.project import Limit, ProjectConfig
from airesearcher.core.models.question import Question
from airesearcher.core.models.run import RunLabel
from airesearcher.core.models.template import TemplateInfo
from airesearcher.core.project import Project
from airesearcher.core.requests import RequestLog
from airesearcher.core.workspace import air_home, load_registry
from airesearcher.engine.engine import ProjectEngine
from airesearcher.engine.recovery import reconcile_runs
from airesearcher.engine.states import (
    ACTIVE_STATES,
    DRAFTING_FOR_KIND,
    KIND_FOR_PENDING,
    RESUMABLE,
)
from airesearcher.runtime.local import LocalExecutor
from airesearcher.services.runs import ACTIVE as ACTIVE_RUN_STATES
from airesearcher.services.runs import RUN_LABELS, list_runs, load_labels, load_run
from airesearcher.stages import STAGE_FOR_STATE, real_stage_names

from .schemas import CreateProject, ProjectDefaults, ProjectStatus, ProjectSummary


def config_default(name: str) -> Any:
    """project.yaml 某个字段的默认值（core/models/project.py）。"""
    return ProjectConfig.model_fields[name].get_default(call_default_factory=True)


def creation_fields(body: CreateProject) -> dict[str, Any]:
    """建项目请求 → project.yaml 的 budget / experiment / dev（只写和默认值不同的部分）。"""
    out: dict[str, Any] = {}
    if body.budget:  # 只覆盖传入的类别，其余用默认值
        budget = config_default("budget")
        for cat, lim in body.budget.items():
            try:
                budget[cat] = Limit.model_validate(lim)
            except ValueError as e:
                raise ValidationFailed(msg=Msg(code="error.bad_budget",
                                               params={"category": cat, "detail": str(e)})) from e
        out["budget"] = budget
    dev = dict(body.dev or {})
    if body.mode == "real":
        dev["stage_impl"] = {**{name: "real" for name in real_stage_names()}, **dev.get("stage_impl", {})}
        experiment = config_default("experiment")
        experiment["llm_backend"] = "api"
        if body.allow_local_execution:
            experiment["allow_local_execution"] = True
        out["experiment"] = experiment
    elif body.allow_local_execution:
        raise ValidationFailed(msg=Msg(code="error.local_exec_needs_real"))
    if dev:
        out["dev"] = dev
    return out


def project_defaults() -> ProjectDefaults:
    return ProjectDefaults(task=CreateProject.model_fields["task"].default, mode="fake",
                           real_stages=real_stage_names(), allow_local_execution=False,
                           budget=config_default("budget"))


def localize_question(q: Question | None) -> Question | None:
    """带消息代码的问题（目前只有引擎自己提的）按请求语言换成对应文字；阶段提的问题原样返回。"""
    if q is None or (q.msg is None and not any(o.msg for o in q.options)):
        return q
    q = q.model_copy(deep=True)
    if q.msg is not None:
        q.text = render(q.msg) or q.text
    for o in q.options:
        if o.msg is not None:
            o.label = render(o.msg) or o.label
    return q


class ProjectManager:
    def __init__(self, home: Path | None = None, start_engines: bool = True, engine_kwargs: dict | None = None):
        self.home = home or air_home()
        self.start_engines = start_engines
        self.engine_kwargs = engine_kwargs or {}
        self.projects: dict[str, Project] = {}
        self.engines: dict[str, ProjectEngine] = {}
        self.requests = RequestLog(self.home / "requests.jsonl")
        self._lock = threading.RLock()
        self.startup_report: dict[str, list] = {}

    # ---------------------------------------------------------------- 生命周期
    def startup(self) -> None:
        for pid, path in load_registry(self.home).items():
            try:
                self._open(pid, Path(path))
            except Exception as e:  # 一个项目坏了不影响其他项目
                self.startup_report[pid] = [f"无法打开：{e}"]

    def _open(self, pid: str, root: Path) -> ProjectEngine:
        project = Project.open(root)
        executor = LocalExecutor(project)
        self.startup_report[pid] = reconcile_runs(project, executor)
        engine = ProjectEngine(project, executor=executor, **self.engine_kwargs)
        with self._lock:
            self.projects[pid] = project
            self.engines[pid] = engine
        if self.start_engines:
            engine.start()
        return engine

    def shutdown(self) -> None:
        for eng in list(self.engines.values()):
            try:
                eng.stop()
            except Exception:
                pass

    # ---------------------------------------------------------------- 访问
    def get(self, pid: str) -> Project:
        try:
            return self.projects[pid]
        except KeyError:
            raise NotFound(msg=Msg(code="error.project_not_found", params={"pid": pid})) from None

    def engine(self, pid: str) -> ProjectEngine:
        self.get(pid)
        return self.engines[pid]

    def create(self, body: CreateProject) -> Project:
        with self._lock:
            done = self.requests.get(body.request_id)
            if done is not None:  # 同一 request_id 已创建过（后台重启后也有效）
                return self.get(done["project_id"])
            fields: dict[str, Any] = {"constraints": body.constraints, "evidence_check": body.evidence_check,
                                      **creation_fields(body)}
            project = Project.create(goal=body.idea_text, task=body.task, title=body.title, home=self.home,
                                     **fields)
            self._open(project.project_id, project.root)
            self.requests.record(body.request_id, "create_project", {"project_id": project.project_id})
            return self.projects[project.project_id]

    def wait_settled(self, pid: str, predicate: Any, timeout: float = 3.0) -> None:
        """写操作后稍等引擎处理（让 CLI 立刻看到新状态）；引擎忙时不等太久。"""
        eng = self.engine(pid)
        if not eng.running:
            return
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if predicate(eng):
                return
            time.sleep(0.05)

    # ---------------------------------------------------------------- 状态页
    def summary(self, pid: str) -> ProjectSummary:
        p = self.get(pid)
        st = self.status(pid)
        return ProjectSummary(
            project_id=pid, title=p.config.title, state=st.state, stage_label=st.stage_label,
            pending_approvals=len(st.pending_approvals), has_question=st.pending_question is not None,
            created_at=p.config.created_at, updated_at=st.updated_at, root=str(p.root),
        )

    def status(self, pid: str) -> ProjectStatus:
        p, eng = self.get(pid), self.engine(pid)
        with eng.state_lock:  # 状态、待审批、问题一起读，避免读到引擎改到一半的样子
            ck = eng.ck.model_copy(deep=True)
            state = ck.state
            pending = p.approvals.pending()
            question = p.questions.pending()
        if state in ACTIVE_STATES:
            stage = STAGE_FOR_STATE[state]
        elif state in KIND_FOR_PENDING:
            stage = STAGE_FOR_STATE.get(DRAFTING_FOR_KIND[KIND_FOR_PENDING[state]])
        else:
            stage = STAGE_FOR_STATE.get(ck.resume_to) if ck.resume_to else None
        label = ck.scratch.get("_label") if state in ACTIVE_STATES else None  # 阶段上报的文字原样显示
        stage_label = label or t(f"state.{state.value}")
        question = localize_question(question)

        blocking = None
        if pending:
            a = pending[0]
            blocking = Msg(code="blocking.approval", params={"kind": f"kind.{a.kind}", "version": a.target.version,
                                                             "artifact_id": a.target.artifact_id})
        elif question is not None:
            blocking = Msg(code="blocking.question", params={"text": question.text.strip().splitlines()[0][:80]})
        elif state in RESUMABLE:
            blocking = Msg(code="blocking.state_reason" if ck.reason else "blocking.state",
                           params={"state": f"state.{state.value}", "reason": ck.reason})

        actions = [f"approve:{a.approval_id}" for a in pending]
        if question is not None:
            actions.append(f"answer:{question.question_id}")
        elif state in RESUMABLE:
            actions.append("resume")
        if state in ACTIVE_STATES:
            actions.append("pause")
        if state.value == "Completed":
            actions.append("reopen")

        runs = [r.summary() for r in list_runs(p.root) if r.status.state in ACTIVE_RUN_STATES]
        return ProjectStatus(
            project_id=pid, title=p.config.title, state=state, stage=stage, stage_label=stage_label,
            progress=ck.scratch.get("_progress", {}) if state in ACTIVE_STATES else {},
            blocking_reason=render(blocking), blocking=blocking,
            pending_approvals=[ApprovalBrief.of(a) for a in pending],
            pending_question=question, active_runs=runs, budget=p.budget.status(), next_actions=actions,
            updated_at=ck.saved_at, root=str(p.root),
        )


    # ---------------------------------------------------------------- 运行标注（详细设计 3 第 8.4 节）
    def label_run(self, pid: str, run_id: str, label: str, reason: str = "", request_id: str = "",
                  by: str = "user") -> RunLabel:
        p = self.get(pid)
        load_run(p.root, run_id, with_metrics=False)  # 运行不存在 → 404
        with self._lock:
            if request_id:
                for row in reversed(list(load_labels(p.root).values())):
                    if row.request_id == request_id:
                        return row
            row = RunLabel(run_id=run_id, label=label, reason=reason, by=by, ts=now(), request_id=request_id)
            p.permissions.guard("write", RUN_LABELS, actor=by)
            append_jsonl(p.root / RUN_LABELS, row.model_dump(mode="json"))
            p.archive.put(RUN_LABELS, "other", p.root / RUN_LABELS, producer=by,
                          note=f"{run_id} 标注为 {label}")
            p.events.append("run.labeled", f"运行 {run_id} 被标注为 {label}" + (f"：{reason}" if reason else ""),
                            actor=by, run_id=run_id, data={"label": label, "reason": reason})
        return row

    # ---------------------------------------------------------------- 模板
    def upload_template(self, pid: str, data: bytes, filename: str, template_id: str | None = None,
                        request_id: str = "") -> TemplateInfo:
        p = self.get(pid)
        with self._lock:
            done = p.requests.get(request_id)
            if done is not None:
                return templates.template_info(p, done["template_id"], done["version"])
            info = templates.import_zip(p, data, filename, template_id, actor="user")
            p.requests.record(request_id, "upload_template", {"template_id": info.template_id,
                                                              "version": info.version})
        self.engine(pid).wake()
        return info
