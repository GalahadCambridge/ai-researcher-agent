"""引擎转换表与附加规则（详细设计 1 第 5.2 节）。用脚本化的假阶段逐条验证。"""

from __future__ import annotations

from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.models.question import Question, QuestionOption
from airesearcher.engine.engine import ProjectEngine
from airesearcher.engine.stage import Advance, Continue, Error, NeedsApproval, Stop, Wait
from airesearcher.engine.states import ALLOWED_ADVANCES, DECISION_NEXT

from .helpers import approve_pending, drive


class Scripted:
    """按顺序返回预设结果；元素可以是 StepResult，也可以是 ctx -> StepResult 的函数。"""

    def __init__(self, name, results):
        self.name = name
        self.results = list(results)
        self.contexts = []
        self.background_calls = 0

    def step(self, ctx):
        self.contexts.append(ctx)
        r = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        return r(ctx) if callable(r) else r

    def background(self, ctx):
        self.background_calls += 1


def submit(aid_path, kind, text="x"):
    def f(ctx):
        ref = ctx.archive.put(aid_path, "other", text + str(len(ctx.archive.versions(aid_path))))
        return NeedsApproval(ref, kind, "s", "i")
    return f


def engine(project, **stages):
    return ProjectEngine(project, stages=stages, retry_delay=0)


def test_transition_table_shape():
    assert (S.Executing, S.Writing) not in ALLOWED_ADVANCES
    assert DECISION_NEXT[("idea", "approved")] == S.PlanDrafting
    assert DECISION_NEXT[("log", "rejected")] == S.Analyzing
    assert DECISION_NEXT[("manuscript", "rejected")] is None


def test_illegal_advance_pauses(project):
    eng = engine(project, idea=Scripted("idea", [Advance(S.Writing, "skip everything")]))
    eng.tick()
    assert eng.state == S.Paused and eng.ck.resume_to == S.IdeaDrafting
    assert any(e.type == "engine.error" for e in project.events.all())


def test_wrong_approval_kind_pauses(project):
    eng = engine(project, idea=Scripted("idea", [submit("plan/x.md", "plan")]))
    eng.tick()
    assert eng.state == S.Paused and "非法转换" in eng.ck.reason


def test_retryable_error_three_times_fails(project):
    eng = engine(project, idea=Scripted("idea", [Error("boom", True)]))
    for _ in range(3):
        eng.tick()
    assert eng.state == S.Failed
    eng.request_action("resume")
    assert eng.state == S.IdeaDrafting and eng.ck.consecutive_errors == 0


def test_exception_in_step_is_retryable_error(project):
    def boom(ctx):
        raise RuntimeError("bad")
    eng = engine(project, idea=Scripted("idea", [boom, Continue()]))
    eng.tick()
    assert eng.state == S.IdeaDrafting and eng.ck.consecutive_errors == 1
    eng.tick()
    assert eng.ck.consecutive_errors == 0


def test_pause_resume_actions(project):
    eng = engine(project, idea=Scripted("idea", [Wait("x", 0)]))
    eng.tick()
    assert eng.request_action("pause", "r1") == S.Paused
    assert eng.request_action("pause", "r1") == S.Paused  # 去重：不会报错
    assert eng.request_action("resume", "r2") == S.IdeaDrafting


def test_stop_question_answer_and_resume_blocked(project):
    q = Question(text="选哪个？", options=[QuestionOption(id="a", label="A"), QuestionOption(id="b", label="B")])
    stage = Scripted("idea", [Stop("need input", q), Continue()])
    eng = engine(project, idea=stage)
    eng.tick()
    assert eng.state == S.Paused and eng.ck.pending_question
    import pytest

    from airesearcher.core.errors import InvalidState
    with pytest.raises(InvalidState):
        eng.request_action("resume")
    qid = eng.ck.scratch["idea"]["asked"]
    project.questions.answer(qid, "b", request_id="x")
    eng.tick()
    ctx = stage.contexts[-1]
    assert eng.state == S.IdeaDrafting and ctx.answer.choice == "b"
    eng.tick()
    assert stage.contexts[-1].answer is None  # 只出现一次


def test_reject_then_end(project):
    eng = engine(project, idea=Scripted("idea", [submit("idea/selected.md", "idea")]))
    drive(eng, S.IdeaPending)
    approve_pending(project, "rejected", "不行")
    eng.tick()
    assert eng.state == S.Paused
    q = project.questions.pending()
    project.questions.answer(q.question_id, "end", request_id="e")
    eng.tick()
    assert eng.state == S.Failed and eng.ck.reason == "用户选择结束"


def test_idea_change_marks_plan_stale(project):
    idea = Scripted("idea", [submit("idea/selected.md", "idea")])
    plan = Scripted("plan", [submit("plan/experiment_plan.md", "plan")])
    exp = Scripted("experiment", [Advance(S.Analyzing, "done"), Advance(S.IdeaDrafting, "需要改 idea")])
    eng = engine(project, idea=idea, plan=plan, experiment=exp)
    drive(eng, S.IdeaPending)
    approve_pending(project)
    drive(eng, S.PlanPending)
    approve_pending(project)
    drive(eng, S.IdeaDrafting)
    assert eng.ck.entry["reason"] == "需要改 idea"
    drive(eng, S.IdeaPending)
    assert idea.contexts[-1].entry.reason == "需要改 idea"  # 阶段从 ctx.entry 读到退回原因
    approve_pending(project)
    eng.tick()  # idea 获批 → PlanDrafting（plan 标为 stale）→ 计划阶段重新提交
    assert eng.state == S.PlanPending
    assert "plan/experiment_plan.md" in plan.contexts[-1].stale
    approve_pending(project)
    eng.tick()
    assert "plan/experiment_plan.md" not in eng.ck.stale


def test_human_edit_of_pending_target_returns_to_drafting(project):
    idea = Scripted("idea", [submit("idea/selected.md", "idea"),
                             lambda ctx: NeedsApproval(ctx.archive.latest("idea/selected.md"), "idea", "s", "i")])
    eng = engine(project, idea=idea)
    drive(eng, S.IdeaPending)
    [old] = project.approvals.pending()
    (project.root / "idea/selected.md").write_text("用户手工改过", encoding="utf-8")
    eng.tick()  # 检测到人工编辑 → 旧审批 superseded → 回到起草状态
    assert project.approvals.get(old.approval_id).status == "superseded"
    drive(eng, S.IdeaPending)
    [new] = project.approvals.pending()
    assert new.target.version == 2
    assert project.archive.latest_version("idea/selected.md").producer == "human-edited"


def test_manuscript_blocked_by_pending_log(project):
    w = Scripted("writing", [submit("paper/main.tex", "manuscript")])
    eng = engine(project, writing=w)
    eng.ck.state = S.Writing
    ref = project.archive.put("logs/round_1.md", "process_log", "log")
    project.approvals.request(ref, "log", "s", "i")
    eng.tick()
    assert eng.state == S.Paused and "过程日志" in eng.ck.reason


def test_background_called_in_waiting_state_with_active_runs(project, monkeypatch):
    exp = Scripted("experiment", [Continue()])
    eng = ProjectEngine(project, stages={"experiment": exp}, background_interval=0)
    eng.ck.state = S.Paused
    monkeypatch.setattr("airesearcher.services.runs.active_runs", lambda root: ["r-1"])
    eng.tick()
    assert exp.background_calls == 1
    eng.ck.state = S.Completed
    eng.tick()
    assert exp.background_calls == 1  # Completed 下不调用


def test_budget_exhausted_transition(project):
    from airesearcher.core.models.project import Limit

    project.config.budget["llm_usd"] = Limit(hard=1.0)
    project.budget.charge("llm_usd", 2.0, "test")
    eng = engine(project, idea=Scripted("idea", [Continue()]))
    eng.tick()
    assert eng.state == S.BudgetExhausted and "llm_usd" in eng.ck.reason


def test_checkpoint_history_and_atomic(project):
    eng = engine(project, idea=Scripted("idea", [Continue()]))
    for _ in range(25):
        eng.tick()
    from airesearcher.engine import checkpoint as ckpt

    hist = ckpt.history(project.root)
    recent = [b for b in ckpt.briefs(project.root) if not b.milestone]
    assert len(recent) == 20  # 普通检查点只保留最近 20 个
    assert hist[0] == "ck-000001" and ckpt.briefs(project.root)[-1].milestone  # 第一个（状态切换）作为里程碑保留
    assert ckpt.load(project.root).checkpoint_id == hist[-1]
    assert not list((project.root / ".state").glob("*.tmp"))


def test_human_edits_committed_separately(project):
    import shutil
    import subprocess

    import pytest

    if shutil.which("git") is None:
        pytest.skip("没有 git")

    def write_src(ctx):
        (ctx.root / "src" / "agent.py").write_text("print('agent')\n", encoding="utf-8")
        return Continue()

    eng = engine(project, idea=Scripted("idea", [write_src]))
    (project.root / "src" / "mine.py").write_text("print('mine')\n", encoding="utf-8")  # 用户手工改
    eng.tick()
    log = subprocess.run(["git", "log", "--format=@@%an", "--name-only"], cwd=project.root, capture_output=True,
                         text=True, encoding="utf-8").stdout
    by_author = {}
    for chunk in log.split("@@")[1:]:
        author, *files = [x for x in chunk.splitlines() if x.strip()]
        by_author[author] = files
    assert "src/mine.py" in by_author["human-edited"] and "src/agent.py" not in by_author["human-edited"]
    assert "src/agent.py" in by_author["agent:idea"]
    ev = [e for e in project.events.all() if e.type == "workspace.human_edited"]
    assert ev and ev[0].data["files"] == ["src/mine.py"]
    assert not project.workspace.dirty()


def test_storage_accounting(project):
    from airesearcher.core.models.project import Limit

    eng = ProjectEngine(project, stages={"idea": Scripted("idea", [Continue()])}, retry_delay=0, storage_interval=0)
    eng.tick()
    base = project.budget.used().get("storage_gb", 0.0)
    (project.root / "artifacts" / "big.bin").write_bytes(b"\0" * (3 * 1024 * 1024))  # 3 MB
    eng.tick()
    used = project.budget.used()["storage_gb"]
    assert abs((used - base) * 1024 - 3) < 0.1  # 记的是差值，总数等于当前大小
    eng.tick()
    assert project.budget.used()["storage_gb"] == used  # 没变化不再记

    project.save_config(project.config.model_copy(update={"budget": {"storage_gb": Limit(hard=used / 2)}}))
    eng.tick()
    assert eng.state == S.BudgetExhausted


def test_transition_clears_previous_stage_progress(project):
    """进入新阶段时清掉上一阶段上报的进度，总览不再显示旧阶段的“等待审批（2/2）”。"""
    from airesearcher.server.manager import ProjectManager

    def report(ctx):
        ctx.progress("idea（假实现）：等待审批", 2, 2, "步")
        return Continue()

    eng = engine(project, idea=Scripted("idea", [report, submit("idea/selected.md", "idea")]),
                 plan=Scripted("plan", [Wait(60)]))
    m = ProjectManager(home=project.root.parent, start_engines=False)
    m.projects[project.project_id], m.engines[project.project_id] = project, eng

    eng.tick()
    st = m.status(project.project_id)
    assert st.stage_label == "idea（假实现）：等待审批" and st.progress == {"done": 2, "total": 2, "unit": "步"}
    eng.tick()
    assert eng.state == S.IdeaPending
    approve_pending(project)
    drive(eng, S.PlanDrafting)
    assert "_label" not in eng.ck.scratch and "_progress" not in eng.ck.scratch
    st = m.status(project.project_id)
    assert st.stage_label == "制定实验计划" and st.progress == {}
