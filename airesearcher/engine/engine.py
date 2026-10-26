"""单项目调度循环（详细设计 1 第 5.4 节）。

    while 没有关闭:
        处理收件箱（pause/resume/cancel/reopen）
        以文件为准核对审批决定和问题回答（崩溃恢复也走这条路）
        等待状态 → 可能调用实验阶段的 background()，然后等待
        活动状态 → 检查预算 → 人工编辑检测 → stage.step(ctx) → apply(result) → 保存检查点

审批决定、问题回答先写进文件，再唤醒引擎；引擎只相信文件，所以“写文件后、检查点前崩溃”
在重启后会被补做，且只做一次（因为补做后检查点里的 pending_approval / pending_question 已清空）。
"""

from __future__ import annotations

import os
import queue
import stat
import threading
import time
import traceback
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from airesearcher.core.errors import InvalidState, NotFound, ValidationFailed
from airesearcher.core.fsutil import now
from airesearcher.core.i18n import DEFAULT_LANG, Msg, render
from airesearcher.core.locks import ProjectLock
from airesearcher.core.models.approval import Approval
from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.models.question import END_OPTION_ID, Answer, Question, QuestionOption, QuestionRecord
from airesearcher.core.project import Project
from airesearcher.stages import BACKGROUND_STAGE, BACKGROUND_STATES, STAGE_FOR_STATE, load_stage, stage_impl

from . import checkpoint as ckpt
from .stage import (
    Advance,
    Continue,
    Error,
    NeedsApproval,
    StageContext,
    StepResult,
    Stop,
    Wait,
    call_background,
)
from .states import (
    ACTIVE_STATES,
    APPROVAL_KIND_FOR_STATE,
    DECISION_NEXT,
    DRAFTING_FOR_KIND,
    KIND_FOR_PENDING,
    KIND_LABELS,
    PAUSABLE,
    PENDING_FOR_KIND,
    PENDING_STATES,
    REOPENABLE,
    RESUMABLE,
    STATE_LABELS,
    advance_allowed,
)

# 人工可编辑、需要做人工编辑检测的产物 → 生成它的状态（该状态下由阶段自己改写，不做检测）
MANAGED_ARTIFACTS = {
    "idea/selected.md": S.IdeaDrafting,
    "plan/experiment_plan.md": S.PlanDrafting,
    "paper": S.Writing,
}
MAX_CONSECUTIVE_ERRORS = 3
WALL_FLUSH_SECONDS = 60.0


def dir_bytes(root: Path) -> int:
    """目录下所有普通文件的大小之和（不跟随符号链接）。"""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if not stat.S_ISLNK(st.st_mode):
                total += st.st_size
    return total


@dataclass
class Tick:
    kind: str  # "step" | "idle" | "transition"
    seconds: float = 0.0  # 建议等待多久再 tick
    result: StepResult | None = None


class ProjectEngine:
    def __init__(
        self,
        project: Project,
        *,
        llm: Any = None,
        executor: Any = None,
        skills: Any = None,
        stages: dict[str, Any] | None = None,
        retry_delay: float = 30.0,
        background_interval: float = 10.0,
        storage_interval: float = 600.0,
    ):
        self.project = project
        self.root = project.root
        self.retry_delay = retry_delay
        self.background_interval = background_interval
        self.storage_interval = storage_interval  # 多久统计一次工作区大小（storage_gb，4.3）
        self._last_storage: float | None = None
        self._llm = llm
        self._executor = executor
        self._skills = skills
        self._stages: dict[tuple[str, str], Any] = {}
        self._stage_objects = dict(stages or {})  # 测试用：直接注入阶段对象 {name: stage}
        self.stage_override: str | None = None  # air dev run-stage 用
        self._wake = threading.Event()
        # 状态与审批 / 问题文件一起变化时持有这把锁；API 读状态时也拿它，保证读到一致的快照
        # （否则可能读到“已有新审批、状态却还是起草中”）
        self.state_lock = threading.RLock()
        self._actions: queue.Queue[tuple[str, str, Future]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._lock = ProjectLock(self.root)
        self._wall_accum = 0.0
        self._last_active: float | None = None
        self._last_bg = 0.0
        loaded = ckpt.load(self.root)
        self.ck = loaded or ckpt.Checkpoint(last_event_seq=project.events.last_seq)
        if loaded is None:
            self._save()
        project.approvals.on_change.append(lambda _a: self.wake())
        project.questions.on_change.append(lambda _r: self.wake())

    # ------------------------------------------------------------------ 依赖（懒加载）
    @property
    def llm(self) -> Any:
        if self._llm is None:
            from airesearcher.llm.gateway import LLMGateway

            self._llm = LLMGateway(self.project)
        return self._llm

    @property
    def executor(self) -> Any:
        if self._executor is None:
            from airesearcher.runtime.local import LocalExecutor

            self._executor = LocalExecutor(self.project)
        return self._executor

    @property
    def skills(self) -> Any:
        if self._skills is None:
            from airesearcher.skills.loader import SkillLoader

            self._skills = SkillLoader()
        return self._skills

    @property
    def state(self) -> S:
        return self.ck.state

    # ------------------------------------------------------------------ 线程
    def start(self) -> None:
        self._lock.acquire()
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name=f"engine-{self.project.project_id}", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 15.0) -> None:
        self._stopping.set()
        self.wake()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        self._flush_wall(force=True)
        self._lock.release()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def wake(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        while not self._stopping.is_set():
            self._wake.clear()
            try:
                t = self.tick()
            except Exception:  # 引擎自身的错误：记日志，过一会再试，不让线程死掉
                self._stage_log("engine crash:\n" + traceback.format_exc())
                t = Tick("idle", 5.0)
            if t.seconds > 0 and not self._stopping.is_set():
                self._wake.wait(t.seconds)

    # ------------------------------------------------------------------ 用户操作
    def request_action(self, action: str, request_id: str = "", timeout: float = 30.0, **kw: Any) -> S:
        """pause / resume / cancel / reopen / rollback(checkpoint_id=...)。后台运行时交给引擎线程在两步之间执行。"""
        done = self.project.requests.get(request_id)
        if done is not None:  # 同一 request_id 已处理过（后台重启后也有效）：返回第一次的结果
            return S(done["state"])
        if self.running and threading.current_thread() is not self._thread:
            fut: Future = Future()
            self._actions.put((action, request_id, kw, fut))
            self.wake()
            return fut.result(timeout=timeout)
        return self._do_action(action, request_id, **kw)

    def _process_actions(self) -> None:
        while True:
            try:
                action, rid, kw, fut = self._actions.get_nowait()
            except queue.Empty:
                return
            try:
                fut.set_result(self._do_action(action, rid, **kw))
            except Exception as e:
                fut.set_exception(e)

    def _rollback(self, checkpoint_id: str) -> None:
        """回到历史检查点（详细设计 1 第 5.7 节）：恢复 approved、scratch 等，不删除任何产物或运行。

        只能在引擎没有在工作时执行（已暂停、等待审批、预算用尽、出错、已完成）；正在工作的先暂停。
        恢复后项目停在 Paused（目标检查点是 Completed 时停在 Completed），用户选择“继续”后从那里重新开始：
        当时在等待审批的，回到对应的起草状态重新提交；当时的待回答问题不再恢复。
        """
        ck = self.ck
        if ck.state in ACTIVE_STATES:
            raise InvalidState(f"项目正在工作（{STATE_LABELS[ck.state]}），请先暂停再回滚：air pause")
        old = ckpt.load_history(self.root, checkpoint_id)
        if ck.pending_question:
            self.project.questions.withdraw(ck.pending_question, f"项目回滚到 {checkpoint_id}")
        if ck.pending_approval:
            self.project.approvals.supersede(ck.pending_approval, f"项目回滚到 {checkpoint_id}")
        if old.state in PENDING_STATES:
            resume_to = DRAFTING_FOR_KIND[KIND_FOR_PENDING[old.state]]
        elif old.state in ACTIVE_STATES:
            resume_to = old.state
        else:
            resume_to = old.resume_to
        new = old.model_copy(deep=True, update={
            "checkpoint_id": ck.checkpoint_id, "pending_approval": None, "pending_question": None, "answer": None,
            "rejected_approval": None, "consecutive_errors": 0, "last_event_seq": ck.last_event_seq,
        })
        if old.state == S.Completed:
            new.state, new.resume_to, new.reason = S.Completed, None, ""
        else:
            new.state, new.resume_to = S.Paused, resume_to
            new.reason = (f"已回滚到 {checkpoint_id}（当时：{STATE_LABELS[old.state]}），"
                          "选择“继续”后从那里重新开始")
        self.ck = new
        self.project.events.append(
            "project.rolled_back", f"回滚到检查点 {checkpoint_id}（当时状态：{STATE_LABELS[old.state]}）",
            actor="user", data={"to": checkpoint_id, "state": old.state.value,
                                "resume_to": resume_to.value if resume_to else None},
        )

    def _do_action(self, action: str, request_id: str, **kw: Any) -> S:
        with self.state_lock:
            return self._do_action_locked(action, request_id, **kw)

    def _do_action_locked(self, action: str, request_id: str, **kw: Any) -> S:
        done = self.project.requests.get(request_id)
        if done is not None:  # 两个相同的请求同时排队：第二个直接返回第一个的结果
            return S(done["state"])
        ck = self.ck
        st = ck.state
        if action == "pause":
            if st not in PAUSABLE:
                raise InvalidState(f"当前状态 {st.value} 不能暂停（只有正在工作的状态可以暂停）")
            self._to_waiting(S.Paused, "用户暂停", actor="user")
        elif action == "resume":
            if st not in RESUMABLE:
                raise InvalidState(f"当前状态 {st.value} 不需要继续")
            if st == S.Paused and ck.pending_question:
                raise InvalidState("有待回答的问题，请先回答（air question / air answer）")
            target = ck.resume_to or S.IdeaDrafting
            ck.resume_to = None
            ck.reason = ""
            ck.consecutive_errors = 0
            self._transition(target, "用户选择继续", actor="user")
        elif action == "cancel":
            if st in (S.Completed, S.Failed):
                raise InvalidState(f"当前状态 {st.value} 不能取消")
            if ck.pending_question:
                self.project.questions.withdraw(ck.pending_question, "项目已取消")
                ck.pending_question = None
            if ck.pending_approval:
                self.project.approvals.supersede(ck.pending_approval, "项目已取消")
                ck.pending_approval = None
            self._to_waiting(S.Failed, "用户取消项目", actor="user")
        elif action == "reopen":
            if st not in REOPENABLE:
                raise InvalidState(f"只有已完成的项目可以重新打开（当前 {st.value}）")
            self._transition(S.Writing, "用户重新打开项目修改论文", actor="user")
        elif action == "rollback":
            self._rollback(kw["checkpoint_id"])
        else:
            raise ValidationFailed(f"未知操作 {action}（可用：pause、resume、cancel、reopen、rollback）")
        self._save()
        self.project.requests.record(request_id, f"action:{action}", {"state": self.ck.state.value})
        self.wake()
        return self.ck.state

    # ------------------------------------------------------------------ 一次调度
    def tick(self) -> Tick:
        self.project.maybe_reload_config()
        self._process_actions()
        self._account_storage()
        with self.state_lock:
            changed = self._reconcile_files()
        st = self.ck.state
        if st not in ACTIVE_STATES:
            self._flush_wall(force=True)
            self._last_active = None
            self._maybe_background()
            if changed:
                self._save()
                return Tick("transition", 0.0)
            return Tick("idle", self.background_interval)

        self._account_wall()
        if self.project.budget.check().exhausted:
            cats = ", ".join(self.project.budget.status().exhausted_categories())
            self._to_waiting(S.BudgetExhausted, f"预算已用尽：{cats}")
            self._save()
            return Tick("transition", 0.0)

        own = [a for a, owner in MANAGED_ARTIFACTS.items() if owner != st]
        self.project.archive.sync(own)

        try:
            stage = self._stage(self.stage_override or STAGE_FOR_STATE[st])
        except Exception as e:  # 阶段实现找不到 / 导入出错：转 Failed，修好后可 resume
            self._apply(Error(str(e), retryable=False), STAGE_FOR_STATE[st])
            self._save()
            return Tick("transition", 0.0)
        self._commit_workspace("human-edited", "用户手工修改（引擎在下一步之前自动提交）")
        ctx = self.build_context()
        delivered_answer = ctx.answer is not None
        try:
            result = stage.step(ctx)
        except Exception as e:
            self._stage_log(f"[{stage.name}] step 抛出异常:\n" + traceback.format_exc())
            result = Error(f"{type(e).__name__}: {e}", retryable=True)
        self._commit_workspace(f"agent:{stage.name}", f"{stage.name} 阶段的改动（阶段没有自己提交）")
        if not isinstance(result, Continue | Wait | NeedsApproval | Advance | Stop | Error):
            result = Error(f"step() 返回了无法识别的结果：{result!r}", retryable=False)
        self.ck.scratch = ctx.scratch
        if delivered_answer:
            self.ck.answer = None
        with self.state_lock:
            seconds = self._apply(result, stage.name)
        self._account_wall()
        self._save()
        return Tick("step", seconds, result)

    # ------------------------------------------------------------------ 上下文
    def build_context(self) -> StageContext:
        ck = self.ck
        feedback = []
        for aid in ck.feedback_approval_ids:
            try:
                feedback.append(self.project.approvals.get(aid))
            except NotFound:
                pass
        return StageContext(
            project=self.project.config, root=self.root, state=ck.state,
            archive=self.project.archive, events=self.project.events, approvals=self.project.approvals,
            questions=self.project.questions, budget=self.project.budget,
            permissions=self.project.permissions, workspace=self.project.workspace,
            llm=self.llm, skills=self.skills, executor=self.executor,
            scratch=ck.scratch, approved=dict(ck.approved), feedback=feedback,
            entry=Advance.from_json(ck.entry) if ck.entry else None, stale=list(ck.stale),
            answer=Answer.model_validate(ck.answer) if ck.answer else None, log=self._stage_log,
        )

    def _stage(self, name: str) -> Any:
        if name in self._stage_objects:
            return self._stage_objects[name]
        key = (name, stage_impl(name, self.project.config))
        if key not in self._stages:
            self._stages[key] = load_stage(name, self.project.config)
        return self._stages[key]

    # ------------------------------------------------------------------ apply
    def _apply(self, result: StepResult, stage_name: str) -> float:
        ck = self.ck
        st = ck.state
        if not isinstance(result, Error):
            ck.consecutive_errors = 0
        match result:
            case Continue():
                return 0.0
            case Wait(seconds=sec):
                return max(0.0, float(sec))
            case NeedsApproval(target=target, kind=kind, summary=summary, impact=impact, extra_refs=extra):
                expected = APPROVAL_KIND_FOR_STATE.get(st)
                if kind != expected:
                    return self._illegal(f"状态 {st.value} 只能提交 {expected} 审批，阶段提交了 {kind}")
                if kind == "manuscript" and self.project.approvals.list("log", "pending"):
                    return self._illegal("还有未处理的过程日志审批，不能提交手稿审批")
                aid = self.project.approvals.request(target, kind, summary, impact, extra)
                ck.pending_approval = aid
                ck.feedback_approval_ids = []
                ck.entry = None
                self._transition(PENDING_FOR_KIND[kind], f"提交{KIND_LABELS[kind]}审批 {aid}")
                return 0.0
            case Advance(to=to, reason=reason):
                to = S(to)
                if not advance_allowed(st, to):
                    return self._illegal(f"不允许从 {st.value} 转到 {to.value}")
                ck.entry = result.to_json()
                ck.feedback_approval_ids = []
                self._transition(to, reason, actor=f"agent:{stage_name}")
                return 0.0
            case Stop(reason=reason, question=question):
                if question is not None:
                    qid = self.project.questions.ask(question, stage_name)
                    ck.scratch.setdefault(stage_name, {})["asked"] = qid
                    ck.pending_question = qid
                self._to_waiting(S.Paused, reason, actor=f"agent:{stage_name}")
                return 0.0
            case Error(message=msg, retryable=retryable):
                ck.consecutive_errors += 1
                self.project.events.append(
                    "engine.error", f"[{stage_name}] {msg}", actor="engine",
                    data={"retryable": retryable, "consecutive": ck.consecutive_errors},
                )
                if retryable and ck.consecutive_errors < MAX_CONSECUTIVE_ERRORS:
                    return self.retry_delay
                self._to_waiting(S.Failed, f"出错：{msg}")
                return 0.0
        return 0.0

    def _illegal(self, msg: str) -> float:
        self.project.events.append("engine.error", f"非法转换：{msg}", actor="engine")
        self._to_waiting(S.Paused, f"非法转换：{msg}")
        return 0.0

    def _to_waiting(self, to: S, reason: str, actor: str = "engine") -> None:
        if self.ck.state not in RESUMABLE:  # 已在等待状态（如 Paused 时取消）时保留原来的 resume_to
            self.ck.resume_to = self.ck.state
        self.ck.reason = reason
        self._transition(to, reason, actor=actor)

    def _transition(self, to: S, reason: str, actor: str = "engine") -> None:
        frm = self.ck.state
        self.ck.state = to
        if to != frm:  # 上一状态上报的进度不再适用（否则新阶段第一次上报前总览还显示旧阶段的进度）
            self.ck.scratch.pop("_label", None)
            self.ck.scratch.pop("_progress", None)
        if to in ACTIVE_STATES or to in PENDING_STATES or to == S.Completed:
            self.ck.reason = ""
        self.project.events.append(
            "state.changed", f"{frm.value} → {to.value}：{reason}", actor=actor,
            data={"from": frm.value, "to": to.value, "reason": reason},
        )

    # ------------------------------------------------------------------ 以文件为准核对
    def _reconcile_files(self) -> bool:
        ck = self.ck
        changed = False
        if ck.pending_approval:
            try:
                a: Approval | None = self.project.approvals.get(ck.pending_approval)
            except NotFound:
                a = None
            if a is None:
                ck.pending_approval = None
                changed = True
            else:
                if a.status == "pending" and self.project.archive.sync([a.target.artifact_id]):
                    a = self.project.approvals.supersede(a.approval_id, "待审产物被人工修改，需要重新提交")
                if a.status in ("approved", "changes_requested", "rejected"):
                    self._apply_decision(a)
                    changed = True
                elif a.status == "superseded":
                    ck.pending_approval = None
                    if ck.state in PENDING_STATES:
                        self._transition(DRAFTING_FOR_KIND[a.kind], "审批已被取代，回到起草状态重新提交")
                    changed = True
        elif ck.state in PENDING_STATES:
            self._transition(DRAFTING_FOR_KIND[KIND_FOR_PENDING[ck.state]], "没有待处理的审批，回到起草状态")
            changed = True

        if ck.pending_question:
            try:
                rec: QuestionRecord | None = self.project.questions.record(ck.pending_question)
            except NotFound:
                rec = None
            if rec is None or rec.question.status == "withdrawn":
                ck.pending_question = None
                changed = True
            elif rec.question.status == "answered" and rec.answer is not None:
                self._apply_answer(rec)
                changed = True
        return changed

    def _apply_decision(self, a: Approval) -> None:
        # 注意：pending_approval 在状态转换之后才清空——API 以它为“引擎已处理”的信号
        ck = self.ck
        kind, decision = a.kind, a.status
        if decision == "approved":
            ck.approved[kind] = a.target
            if kind == "idea":
                self._unstale("idea/selected.md")
                if "plan" in ck.approved:
                    self._mark_stale("plan/experiment_plan.md")  # idea 变更传播
            elif kind == "plan":
                self._unstale("plan/experiment_plan.md")
                if "manuscript" in ck.approved:
                    self._mark_stale("paper")
            elif kind == "log":
                if "manuscript" in ck.approved:
                    self._mark_stale("paper")
            elif kind == "manuscript":
                self._unstale("paper")
        ck.entry = None
        nxt = DECISION_NEXT[(kind, decision)]
        label = KIND_LABELS[kind]
        if nxt is None:  # idea/plan/manuscript 被拒绝：转 Paused 并提问 redo / end
            ck.rejected_approval = a.approval_id
            text = Msg(code="question.rejected", params={"kind": f"kind.{kind}", "artifact_id": a.target.artifact_id,
                                                         "version": a.target.version})
            redo, end = Msg(code="question.rejected.redo"), Msg(code="question.rejected.end")
            q = Question(
                text=render(text, DEFAULT_LANG), msg=text,
                options=[
                    QuestionOption(id="redo", label=render(redo, DEFAULT_LANG), msg=redo),
                    QuestionOption(id=END_OPTION_ID, label=render(end, DEFAULT_LANG), msg=end),
                ],
                refs=[a.target],
            )
            ck.pending_question = self.project.questions.ask(q, "engine")
            ck.resume_to = DRAFTING_FOR_KIND[kind]
            ck.reason = f"{label}被拒绝，等待你选择重写还是结束"
            self._transition(S.Paused, ck.reason, actor="user")
            ck.pending_approval = None
            return
        ck.feedback_approval_ids = [a.approval_id] if (decision != "approved" or kind == "log") else []
        self._transition(nxt, f"{label}审批结果：{decision}", actor="user")
        ck.pending_approval = None

    def _apply_answer(self, rec: QuestionRecord) -> None:
        ck = self.ck
        ans, q = rec.answer, rec.question
        assert ans is not None
        if ck.state != S.Paused:
            ck.pending_question = None
            return
        if ans.choice == END_OPTION_ID:
            ck.reason = "用户选择结束"
            ck.rejected_approval = None
            self._transition(S.Failed, "用户选择结束", actor="user")
            ck.pending_question = None
            return
        if q.stage == "engine" and ck.rejected_approval:
            aid = ck.rejected_approval
            ck.rejected_approval = None
            kind = self.project.approvals.get(aid).kind
            ck.feedback_approval_ids = [aid]
            target = DRAFTING_FOR_KIND[kind]
        else:
            ck.answer = ans.model_dump(mode="json")
            target = ck.resume_to or S.IdeaDrafting
        ck.resume_to = None
        self._transition(target, f"收到回答：{ans.choice or ans.text[:30]}", actor="user")
        ck.pending_question = None

    def _mark_stale(self, artifact_id: str) -> None:
        if artifact_id not in self.ck.stale:
            self.ck.stale.append(artifact_id)

    def _unstale(self, artifact_id: str) -> None:
        self.ck.stale = [x for x in self.ck.stale if x != artifact_id]

    # ------------------------------------------------------------------ 等待状态下的后台收集
    def _maybe_background(self) -> None:
        if self.ck.state not in BACKGROUND_STATES:
            return
        t = time.monotonic()
        if t - self._last_bg < self.background_interval * 0.9:
            return
        self._last_bg = t
        from airesearcher.services.runs import active_runs

        if not active_runs(self.root):
            return
        try:
            stage = self._stage(BACKGROUND_STAGE)
            call_background(stage, self.build_context())
        except Exception:
            self._stage_log("background 出错:\n" + traceback.format_exc())

    # ------------------------------------------------------------------ 存储记账（4.3：每 10 分钟统计一次，记差值）
    def _account_storage(self) -> None:
        t = time.monotonic()
        if self._last_storage is not None and t - self._last_storage < self.storage_interval:
            return
        self._last_storage = t
        try:
            gb = dir_bytes(self.root) / 1024**3
            delta = gb - self.project.budget.used().get("storage_gb", 0.0)
            if abs(delta) >= 1 / 1024:  # 变化不到 1 MB 不记
                self.project.budget.charge("storage_gb", round(delta, 6), "engine:storage",
                                           data={"total_gb": round(gb, 6)})
        except OSError:
            self._stage_log("统计工作区大小失败:\n" + traceback.format_exc())

    # ------------------------------------------------------------------ 墙钟时间记账（等待状态不计）
    def _account_wall(self) -> None:
        t = time.monotonic()
        if self._last_active is not None:
            self._wall_accum += t - self._last_active
        self._last_active = t
        self._flush_wall()

    def flush_accounting(self) -> None:
        """把还没记账的墙钟时间写进预算（不经过 start/stop 直接 tick 引擎的调用方，结束时调用）。"""
        self._flush_wall(force=True)

    def _flush_wall(self, force: bool = False) -> None:
        if self._wall_accum > 0 and (force or self._wall_accum >= WALL_FLUSH_SECONDS):
            self.project.budget.charge("wall_hours", self._wall_accum / 3600.0, "engine")
            self._wall_accum = 0.0

    # ------------------------------------------------------------------ 杂项
    def _save(self) -> None:
        self.ck.last_event_seq = self.project.events.last_seq
        ckpt.save(self.root, self.ck)

    def _commit_workspace(self, author: str, message: str) -> None:
        """工作区 git（详细设计 1 第 3.8 节）：每一步之前把用户手工改动单独以 human-edited 提交，
        每一步之后把阶段没提交的改动以阶段的名义提交。这样每次运行都对应一个确定的 commit，且作者分得清。"""
        ws = self.project.workspace
        try:
            files = ws.changed_files()
            if not files:
                return
            sha = ws.commit(message, author=author)
        except Exception:  # git 出问题不影响流程，记日志即可
            self._stage_log("workspace commit failed:\n" + traceback.format_exc())
            return
        if author == "human-edited":
            self.project.events.append(
                "workspace.human_edited", f"用户修改了 {len(files)} 个文件，已自动提交（{sha[:8]}）",
                actor="user", data={"commit": sha, "files": files[:50]},
            )

    def _stage_log(self, msg: str) -> None:
        p = self.root / ".state" / "stage.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{now().isoformat(timespec='seconds')} {msg}\n")

    def auto_approve_pending(self, by: str = "auto-approve(dev)") -> list[str]:
        """开发 / 评价用：批准所有待审批（详细设计 1 第 12.1 节的 auto_approve 策略）。"""
        done = []
        for a in self.project.approvals.pending():
            self.project.approvals.decide(
                a.approval_id, "approved", a.target.sha256, comment=by, request_id=f"{by}-{a.approval_id}", by=by
            )
            done.append(a.approval_id)
        return done
