"""Document-prescribed execution/repair/analysis contracts, using fakes only."""

from __future__ import annotations

import json
import socket
from collections import Counter

import pytest

from airesearcher.core.fsutil import sha256_file
from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.models.run import RunState
from airesearcher.engine.engine import ProjectEngine
from airesearcher.engine.stage import Error, Stop
from airesearcher.llm.gateway import LiteLLMBackend, LLMGateway, TransientLLMError, litellm_kwargs
from airesearcher.runtime.local import LocalExecutor
from airesearcher.services.runs import list_runs
from airesearcher.stages.experiment.coder import run_coder
from airesearcher.stages.experiment.stage import ExperimentStage, load_approved
from airesearcher.stages.plan.stage import PlanStage
from airesearcher.testing.contracts import check_project
from airesearcher.testing.fake_executor import FakeExecutor, FakeOutcome
from airesearcher.testing.fake_llm import FakeLLM, FakeToolCalls, fake_gateway
from scripts.experiment.demo import analysis_reply, coder_reply, simulated_outcome, summary
from scripts.plan.fixtures import preset_draft

from .helpers import approve_pending, drive
from .test_plan_stage import seeded, start_plan


@pytest.mark.parametrize("coder_fails", [False, True])
def test_coder_budget_adapter_does_not_replace_shared_backend(air_home, monkeypatch, coder_fails):
    project, engine, _ = prepared(air_home)
    engine.tick()
    ctx = engine.build_context()
    approved = load_approved(ctx)
    backend = ctx.llm.backend
    if coder_fails:
        def fail(request):
            raise RuntimeError("Synthetic coder failure")
        monkeypatch.setattr(backend, "call", fail)
    entries = engine.ck.scratch["experiment"]["entries"]
    if coder_fails:
        with pytest.raises(RuntimeError, match="Synthetic coder failure"):
            run_coder(ctx, approved, approved.plan.experiments[0], entries, lambda key: {})
    else:
        result = run_coder(ctx, approved, approved.plan.experiments[0], entries, lambda key: {})
        assert result["ok"]
    assert ctx.llm.backend is backend is engine.llm.backend


def test_paused_expired_coder_trial_is_never_submitted_on_resume(air_home, monkeypatch):
    from airesearcher.stages.experiment import stage
    from scripts.plan.fixtures import fixture_idea

    draft = preset_draft(fixture_idea("tasks/smoke"), "tasks/smoke")
    draft["experiments"][0]["needs_code"] = True
    _, engine, executor = prepared(air_home, draft=draft)
    monkeypatch.setattr(stage, "CODER_DISPATCH_TIMEOUT_S", 0.05)

    def waiting_coder(ctx, approved, spec, entries, trial, *args):
        trial("trial-E1")

    monkeypatch.setattr(stage, "run_coder", waiting_coder)
    engine.tick()  # Set up entries.
    engine.tick()  # Dispatch the worker; keep its queued operation on hold.
    identity = str(engine.project.root)
    worker = stage._JOBS[identity]
    engine.request_action("pause")
    with pytest.raises(TimeoutError):
        worker.result(timeout=3)
    assert not executor.submitted and not list_runs(engine.project.root)
    engine.request_action("resume")
    drive(engine, S.Paused)
    assert engine.project.questions.pending()
    assert not executor.submitted and not list_runs(engine.project.root)
    assert identity not in stage._COMMITS and identity not in stage._JOBS


@pytest.mark.parametrize("retry_cleanup", [False, True])
def test_paused_coder_edits_are_rolled_back_before_user_retry_and_later_trials(
    air_home, tmp_path, monkeypatch, retry_cleanup
):
    import threading
    import time

    from airesearcher.stages.experiment import stage
    from airesearcher.stages.experiment.coder import snapshot
    from scripts.plan.fixtures import fixture_idea

    draft = preset_draft(fixture_idea("tasks/smoke"), "tasks/smoke")
    draft["experiments"][0]["needs_code"] = True
    project, engine, executor = prepared(air_home, draft=draft)
    engine.tick()
    if retry_cleanup:
        # Exercise Windows-style working bytes even on Linux; Git archives use
        # the committed blob, which may have different line endings.
        project.workspace._git("config", "core.autocrlf", "true")
        entrypoint = project.root / "src/run.py"
        entrypoint.write_bytes(entrypoint.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    before = snapshot(project.root)
    accepted_code = tmp_path / "accepted-code"
    project.workspace.export_src(project.workspace.head(), accepted_code)
    accepted_entrypoint = (accepted_code / "run.py").read_bytes()
    edits = coder_reply()
    edits.calls[-1] = ("write_file", {
        "path": "src/run.py", "content": before["src/run.py"].decode("utf-8") + "\n# Unvalidated coder edit\n",
    })
    finish = coder_reply().calls[-1]
    backend = engine.llm.backend
    backend.script["experiment/coder"] = [edits, FakeToolCalls([("run_trial", {"task_key": "trial-E1"}), finish])]
    edits_applied, allow_trial = threading.Event(), threading.Event()
    original_call = backend.call

    def gated_call(request):
        if request.prompt_id == "experiment/coder" and backend.calls:
            edits_applied.set()
            assert allow_trial.wait(timeout=10), "Test must release the offline trial response"
        return original_call(request)

    monkeypatch.setattr(backend, "call", gated_call)
    monkeypatch.setattr(stage, "CODER_DISPATCH_TIMEOUT_S", 1)
    engine_thread = threading.get_ident()
    original_commit = project.workspace.commit

    def engine_commit(*args, **kwargs):
        assert threading.get_ident() == engine_thread
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(project.workspace, "commit", engine_commit)
    original_restore = stage.restore
    restore_attempts = []

    def engine_restore(*args, **kwargs):
        assert threading.get_ident() == engine_thread
        restore_attempts.append(True)
        if retry_cleanup and len(restore_attempts) == 1:
            raise OSError("Synthetic rollback write failure")
        return original_restore(*args, **kwargs)

    monkeypatch.setattr(stage, "restore", engine_restore)
    deadline = time.monotonic() + 10
    try:
        while not edits_applied.is_set():
            assert time.monotonic() < deadline, "Coder did not apply its edits"
            engine.tick()
            time.sleep(0.01)
        assert snapshot(project.root) != before and (project.root / "src/session_check.py").exists()
        rejected_commit = project.workspace.head()
        worker = stage._JOBS[str(project.root)]
        engine.request_action("pause")
    finally:
        allow_trial.set()
    with pytest.raises(RuntimeError, match="abandoned"):
        worker.result(timeout=10)
    assert not executor.submitted
    engine.request_action("resume")
    drive(engine, S.Paused)
    if retry_cleanup:
        assert "Synthetic rollback write failure" in engine.ck.reason
        assert project.questions.pending() is None and not executor.submitted
        assert stage._JOBS[str(project.root)] is worker
        engine.request_action("resume")
        drive(engine, S.Paused)
    assert snapshot(project.root) == before
    question = project.questions.pending()
    assert "restored" in question.text and "src/session_check.py" in question.text
    assert not executor.submitted
    assert not project.workspace.dirty()
    project.questions.answer(question.question_id, "retry", request_id="retry-after-coder-rollback")
    drive(engine, S.Analyzing)
    assert len(executor.submitted) == 12
    for view in list_runs(project.root):
        assert view.record.code_commit != rejected_commit
        code = project.root / "runs" / view.record.run_id / "code"
        assert not (code / "session_check.py").exists()
        assert (code / "run.py").read_bytes() == accepted_entrypoint
    assert project.budget.used().get("llm_usd", 0) == 0


@pytest.mark.parametrize("choice", ["mark_lost_retry", "end"])
def test_uncertain_launch_requires_human_decision_and_preserves_old_attempt(air_home, monkeypatch, choice):
    from airesearcher.core.fsutil import atomic_write_json
    from airesearcher.runtime.local import UncertainSubmission

    project, engine, executor = prepared(air_home)
    original = executor.submit
    blocked = []

    def interrupted_submit(spec):
        if not blocked:
            blocked.append(spec.record.run_id)
            atomic_write_json(spec.run_dir / "launch.json", {"wrapper_pid": None})
            raise UncertainSubmission(spec.record.run_id)
        return original(spec)

    monkeypatch.setattr(executor, "submit", interrupted_submit)
    drive(engine, S.Paused)
    question = project.questions.pending()
    assert {o.id for o in question.options} == {"mark_lost_retry", "end"}
    assert not any(o.default for o in question.options)
    assert not executor.submitted
    engine.tick()
    assert not executor.submitted  # Waiting never means consent.
    old_id = blocked[0]
    project.questions.answer(question.question_id, choice, request_id="launch-recovery")
    if choice == "end":
        engine.tick()
        assert not executor.submitted and executor.status(old_id).state == RunState.queued
        return
    drive(engine, S.Analyzing)
    assert executor.status(old_id).failure_reason == "lost"
    assert (executor.run_dir(old_id) / "launch.json").exists()
    assert project.archive.latest(f"runs/{old_id}")
    assert old_id not in executor.submitted
    assert any(v.record.retry_of == old_id for v in list_runs(project.root))
    assert len(executor.submitted) == 12 and summary(project)["complete"]


def test_live_wrapper_prevents_human_lost_recovery(air_home, monkeypatch):
    from airesearcher.core.errors import InvalidState
    from airesearcher.core.fsutil import atomic_write_json
    from airesearcher.runtime import local

    project, engine, executor = prepared(air_home)

    def uncertain(spec):
        atomic_write_json(spec.run_dir / "launch.json", {"wrapper_pid": 123})
        raise local.UncertainSubmission(spec.record.run_id)

    monkeypatch.setattr(executor, "submit", uncertain)
    drive(engine, S.Paused)
    [view] = list_runs(project.root)
    monkeypatch.setattr(local, "pid_matches", lambda *args: True)
    with pytest.raises(InvalidState, match="still alive"):
        executor.mark_submission_lost(view.record.run_id)
    assert executor.status(view.record.run_id).state == RunState.queued
    assert not executor.submitted


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No network, real LLM or model training in Fake acceptance")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(LiteLLMBackend, "call", forbidden)
    monkeypatch.setattr(LocalExecutor, "submit", forbidden)


def prepared(air_home, *, outcomes=simulated_outcome, draft=None, coder=None, analysis=None):
    project, engine, _, _ = seeded(air_home, [draft] if draft else None)
    start_plan(engine, project)
    dev = project.config.dev.model_copy(update={"stage_impl": {"plan": "real", "experiment": "real"}})
    project.save_config(project.config.model_copy(update={"dev": dev}))
    executor = FakeExecutor(project, outcomes=outcomes)
    gateway = fake_gateway(
        project, {"experiment/coder": [coder or coder_reply()], "experiment/analyze": analysis or [analysis_reply()]}
    )
    engine = ProjectEngine(project, executor=executor, llm=gateway, retry_delay=0, background_interval=0)
    approve_pending(project)
    return project, engine, executor


def test_same_commit_trials_gate_formal_runs_and_contracts(air_home):
    project, engine, executor = prepared(air_home)
    drive(engine, S.Analyzing)
    views = list_runs(project.root)
    assert Counter(v.record.kind == "trial" for v in views) == {True: 3, False: 9}
    for v in views:
        if v.record.kind != "trial":
            assert any(
                t.record.experiment_id == v.record.experiment_id
                and t.record.kind == "trial"
                and t.record.code_commit == v.record.code_commit
                and t.record.created_at < v.record.created_at
                for t in views
            )
    assert len(executor.submitted) == 12 and summary(project)["complete"]
    assert check_project(project.root, owners=["实验"]).ok
    assert not engine.llm.backend.calls


@pytest.mark.parametrize("backend", ["llm", "executor"])
def test_reject_real_backends_before_any_submission(air_home, backend):
    _, engine, executor = prepared(air_home)
    ctx = engine.build_context()
    ctx.state = S.Executing
    if backend == "llm":
        ctx.llm = LLMGateway(engine.project)
    else:
        ctx.executor = LocalExecutor(engine.project)
    assert isinstance(ExperimentStage().step(ctx), Error)
    assert not executor.submitted


def test_unapproved_or_modified_attachments_cannot_run(air_home):
    project, engine, executor = prepared(air_home)
    ctx = engine.build_context()
    ctx.state = S.Executing
    ctx.approved.pop("plan", None)
    assert isinstance(ExperimentStage().step(ctx), Stop)
    (project.root / "plan/tasks.json").write_text("[]", encoding="utf-8")
    drive(engine, S.Paused)
    assert not executor.submitted


def test_runtime_failure_calls_coder_and_preserves_failed_attempt(air_home):
    def outcome(rec, cfg):
        if rec.task_key == "E1-seed=0" and rec.retry_of is None:
            return FakeOutcome.fail(stderr="RuntimeError: synthetic failure")
        return simulated_outcome(rec, cfg)

    project, engine, _ = prepared(air_home, outcomes=outcome)
    drive(engine, S.Analyzing)
    views = list_runs(project.root)
    [failed] = [v for v in views if v.status.state == RunState.failed]
    assert any(v.record.retry_of == failed.record.run_id for v in views)
    assert any(c.prompt_id == "experiment/coder" for c in engine.llm.backend.calls)
    assert summary(project)["complete"]
    assert (project.root / "src/session_check.py").exists()


def test_same_error_repair_limit_and_user_skip(air_home):
    def outcome(rec, cfg):
        return (
            FakeOutcome.fail(stderr="RuntimeError: persistent")
            if rec.task_key == "trial-E1"
            else simulated_outcome(rec, cfg)
        )

    project, engine, _ = prepared(air_home, outcomes=outcome)
    drive(engine, S.Paused)
    entry = engine.ck.scratch["experiment"]["entries"]["trial-E1"]
    assert entry["repair_counts"]["runtime_error"] == 2
    assert len([c for c in engine.llm.backend.calls if c.prompt_id == "experiment/coder"]) == 2
    q = project.questions.pending()
    assert {o.id for o in q.options} == {"retry", "skip", "revise_plan"}
    project.questions.answer(q.question_id, "skip", "", request_id="skip")
    drive(engine, S.Analyzing)
    assert not any(v.record.kind != "trial" and v.record.experiment_id == "E1" for v in list_runs(project.root))


def test_missing_dependency_asks_without_coder_or_install(air_home):
    project, engine, _ = prepared(
        air_home, outcomes=lambda r, c: FakeOutcome.fail(stderr="ModuleNotFoundError: absent")
    )
    drive(engine, S.Paused)
    assert "manually" in project.questions.pending().text
    assert not engine.llm.backend.calls


def test_needs_code_and_restricted_trial_tool(air_home, monkeypatch):
    import threading

    from scripts.plan.fixtures import fixture_idea, preset_draft

    draft = preset_draft(fixture_idea("tasks/smoke"), "tasks/smoke")
    draft["experiments"][0]["needs_code"] = True
    tool_calls = coder_reply()
    tool_calls.calls.insert(1, ("run_trial", {"task_key": "E1-seed=0"}))
    project, engine, _ = prepared(air_home, draft=draft, coder=tool_calls)
    engine_thread = threading.get_ident()
    original = project.workspace.commit

    def serialized_commit(*args, **kwargs):
        assert threading.get_ident() == engine_thread, "Workspace commits must execute on the engine thread"
        return original(*args, **kwargs)

    monkeypatch.setattr(project.workspace, "commit", serialized_commit)
    drive(engine, S.Analyzing)
    assert summary(project)["complete"]
    assert any(v.record.task_key.startswith("coder-E1-") for v in list_runs(project.root))


def test_coder_plan_change_rolls_back_then_requests_review(air_home):
    from scripts.plan.fixtures import fixture_idea, preset_draft

    draft = preset_draft(fixture_idea("tasks/smoke"), "tasks/smoke")
    draft["experiments"][0]["needs_code"] = True
    calls = coder_reply()
    calls.calls[-1][1].update(needs_plan_change=True, plan_change_reason="Method change required")
    project, engine, executor = prepared(air_home, draft=draft, coder=calls)
    drive(engine, S.Paused)
    assert not (project.root / "src/session_check.py").exists() and not executor.submitted
    q = project.questions.pending()
    project.questions.answer(q.question_id, "revise_plan", "", request_id="revision")
    drive(engine, S.LogPending)
    assert engine.ck.scratch["experiment"]["analysis_decision"]["decision"] == "revise_plan"
    approve_pending(project)
    drive(engine, S.PlanDrafting)


def test_prepare_submit_gap_resumes_same_run_id(air_home, monkeypatch):
    project, engine, executor = prepared(air_home)
    original = executor.submit

    def interrupted(spec):
        raise OSError("interrupted")

    monkeypatch.setattr(executor, "submit", interrupted)
    drive(engine, S.Paused)
    [orphan] = list_runs(project.root)
    monkeypatch.setattr(executor, "submit", original)
    engine.request_action("resume")
    drive(engine, S.Analyzing)
    assert len(list_runs(project.root)) == 12 and executor.submitted.count(orphan.record.run_id) == 1


def test_background_collects_without_repairs_or_submissions(air_home):
    project, engine, executor = prepared(air_home)
    engine.tick()
    engine.tick()
    count = len(executor.submitted)
    ctx = engine.build_context()
    ctx.state = S.Paused
    ExperimentStage().background(ctx)
    assert len(executor.submitted) == count and not engine.llm.backend.calls
    assert all(project.archive.latest(f"runs/{v.record.run_id}") for v in list_runs(project.root))


def test_summary_handoff_waits_for_log_approval_and_feedback_preserves_raw(air_home):
    project, engine, _ = prepared(air_home)
    drive(engine, S.LogPending)
    before = {p: sha256_file(p) for p in (project.root / "runs").rglob("metrics.jsonl")}
    assert engine.state == S.LogPending
    approve_pending(project, "changes_requested", comment="Clarify simulation limitations")
    drive(engine, S.LogPending)
    assert len(project.archive.versions("logs/round_1.md")) == 2
    assert all(sha256_file(p) == v for p, v in before.items())
    approve_pending(project)
    drive(engine, S.Writing)
    assert all(sha256_file(p) == v for p, v in before.items())
    assert not (project.root / "paper/manuscript.md").exists()


def test_restart_does_not_duplicate_runs(air_home):
    project, engine, executor = prepared(air_home)
    drive(engine, S.Analyzing)
    before = len(executor.submitted)
    resumed = ProjectEngine(project, executor=executor, llm=engine.llm, retry_delay=0)
    drive(resumed, S.LogPending)
    assert len(executor.submitted) == before
    aggregate = json.loads((project.root / "artifacts/aggregates/all.json").read_text(encoding="utf-8"))
    assert aggregate["script"]["path"].endswith("experiment/aggregate.py")


def test_unsupported_protected_schema_stops_before_coder_or_execution(air_home, monkeypatch):
    from scripts.plan.fixtures import fixture_idea, preset_draft

    draft = preset_draft(fixture_idea("tasks/smoke"), "tasks/smoke")
    draft["experiments"][0]["needs_code"] = True
    project, engine, executor = prepared(air_home, draft=draft)
    schema = {"type": "object", "properties": {"method": {"enum": ["main", "ablation"]}}}
    monkeypatch.setattr("airesearcher.stages.experiment.stage._schema", lambda *args: schema)
    drive(engine, S.Paused)
    assert "protected task schema" in project.questions.pending().text
    assert not executor.submitted and not engine.llm.backend.calls


def api_fixture(project, monkeypatch, script, *, usd=0.002, transient_once=False):
    """Exercise the production backend boundary with local FakeLLM replies; never call HTTP."""
    project.save_config(project.config.model_copy(update={
        "experiment": {**project.config.experiment, "llm_backend": "api"},
        "dev": project.config.dev.model_copy(update={"stage_impl": {"plan": "real", "experiment": "real"}}),
        "models": {
            "models": {"api-offline": {
                "model": "anthropic/offline-fixture", "api_base": "https://openrouter.ai/api/v1",
                "key_env": "AIR_OFFLINE_ADAPTER_KEY", "max_tokens": 256, "price": [1, 2],
            }},
            "roles": {r: "api-offline" for r in ("exp.planner", "exp.coder", "exp.analyst")},
            "fallbacks": {"api-offline": []},
        },
    }))
    monkeypatch.setenv("AIR_OFFLINE_ADAPTER_KEY", "offline-placeholder-never-send")
    fake = FakeLLM(script)
    attempts = []

    def simulated_transport(self, request):
        attempts.append(request)
        if transient_once and len(attempts) == 1:
            raise TransientLLMError("Offline injected rate limit")
        kwargs = litellm_kwargs(request)
        assert kwargs["model"] == "openrouter/anthropic/offline-fixture"
        assert kwargs["api_base"] == "https://openrouter.ai/api/v1"
        assert kwargs["api_key"] == "offline-placeholder-never-send"
        return fake.call(request).model_copy(update={"usd": usd})

    monkeypatch.setattr(LiteLLMBackend, "call", simulated_transport)
    return LLMGateway(project, backoff_base=0), fake, attempts


@pytest.mark.parametrize("mode", ["fake", "typo", None, {}])
def test_api_mode_needs_explicit_valid_selection(air_home, monkeypatch, mode):
    project, engine, _, _ = seeded(air_home)
    project.save_config(project.config.model_copy(update={
        "experiment": {**project.config.experiment, "llm_backend": mode},
    }))
    ctx = engine.build_context()
    ctx.llm = LLMGateway(project)
    assert isinstance(PlanStage().step(ctx), Error)
    assert isinstance(ExperimentStage().step(ctx), Error)
    assert not (project.root / ".llm/calls.jsonl").exists()


def test_api_mode_does_not_silently_use_fake_backend(air_home):
    project, engine, _, _ = seeded(air_home)
    project.save_config(project.config.model_copy(update={
        "experiment": {**project.config.experiment, "llm_backend": "api"},
    }))
    ctx = engine.build_context()
    assert isinstance(PlanStage().step(ctx), Error)
    assert isinstance(ExperimentStage().step(ctx), Error)
    assert not engine.llm.backend.calls


def test_api_adapter_full_flow_with_offline_transport(air_home, monkeypatch):
    project, engine, idea, idea_ref = seeded(air_home)
    draft = preset_draft(idea, project.config.task)
    draft["experiments"][0]["needs_code"] = True
    gateway, fake, _ = api_fixture(project, monkeypatch, {
        "plan/draft": [draft], "experiment/coder": [coder_reply()], "experiment/analyze": [analysis_reply()],
    })
    engine._llm = gateway
    engine._executor = FakeExecutor(project, outcomes=simulated_outcome)
    start_plan(engine, project)
    assert engine.ck.approved["idea"] == idea_ref
    text = (project.root / "plan/experiment_plan.md").read_text(encoding="utf-8")
    assert "# Experiment Plan\n" in text and "FakeLLM Development Validation" not in text
    assert "FakeLLM development validation" not in project.approvals.pending()[0].summary
    approve_pending(project)
    drive(engine, S.LogPending)
    assert summary(project)["complete"]
    assert {r.role for r in fake.calls} == {"exp.planner", "exp.coder", "exp.analyst"}
    assert any(r.tools for r in fake.calls if r.role == "exp.coder")
    assert "approved_idea" in fake.calls[0].messages[1]["content"]
    assert "untrusted_document" in fake.calls[0].messages[1]["content"]
    assert all(r.alias == "api-offline" for r in fake.calls)
    assert project.budget.used()["llm_usd"] == pytest.approx(len(fake.calls) * 0.002)
    assert check_project(project.root, owners=["实验"]).ok
    report = (project.root / "logs/round_1.md").read_text(encoding="utf-8")
    assert "SIMULATED" in report and "LLM backend: api" in report
    assert "offline-placeholder-never-send" not in (project.root / ".llm/calls.jsonl").read_text(encoding="utf-8")
    approve_pending(project)
    drive(engine, S.Writing)


def test_api_adapter_reuses_gateway_json_and_transient_retries(air_home, monkeypatch):
    project, engine, idea, _ = seeded(air_home)
    gateway, fake, attempts = api_fixture(project, monkeypatch, {
        "plan/draft": ["invalid JSON", preset_draft(idea, project.config.task)],
    }, transient_once=True)
    engine._llm = gateway
    start_plan(engine, project)
    assert len(attempts) == 3 and len(fake.calls) == 2
    assert "格式校验" in fake.calls[-1].messages[-1]["content"]
    assert project.budget.used()["llm_usd"] == pytest.approx(0.004)
    assert not engine.executor.submitted


def test_api_budget_exhaustion_stops_before_next_step(air_home, monkeypatch):
    project, engine, idea, _ = seeded(air_home, budget={"llm_usd": {"hard": 0.001}})
    gateway, fake, _ = api_fixture(project, monkeypatch, {
        "plan/draft": [preset_draft(idea, project.config.task)],
    })
    engine._llm = gateway
    drive(engine, S.IdeaPending)
    approve_pending(project)
    drive(engine, S.BudgetExhausted)
    assert len(fake.calls) == 1 and not engine.executor.submitted
    assert not project.approvals.pending()


def test_api_coder_checks_budget_between_tool_turns_and_rolls_back(air_home, monkeypatch):
    project, engine, executor = prepared(air_home)
    engine.tick()  # Process the persisted approval and freeze the execution workspace.
    ctx = engine.build_context()
    approved = load_approved(ctx)
    first = FakeToolCalls([("write_file", {"path": "src/budget_fixture.py", "content": "VALUE = 1\n"})])
    gateway, fake, _ = api_fixture(project, monkeypatch, {"experiment/coder": [first, coder_reply()]}, usd=6)
    ctx.llm = gateway
    before = sha256_file(project.root / "src/run.py")
    with pytest.raises(ValueError, match="Budget exhausted"):
        run_coder(ctx, approved, approved.plan.experiments[0], ctx.scratch["experiment"]["entries"],
                  lambda _: pytest.fail("No training allowed"))
    assert len(fake.calls) == 1
    assert not (project.root / "src/budget_fixture.py").exists()
    assert sha256_file(project.root / "src/run.py") == before and not executor.submitted


def test_api_analysis_config_error_stops_without_paid_retry(air_home, monkeypatch):
    from airesearcher.llm.models import MissingAPIKey

    project, engine, _ = prepared(air_home)
    drive(engine, S.Analyzing)
    gateway, _, _ = api_fixture(project, monkeypatch, {"experiment/analyze": [analysis_reply()]})

    def missing(self, request):
        raise MissingAPIKey("Set the configured environment variable")

    monkeypatch.setattr(LiteLLMBackend, "call", missing)
    engine._llm = gateway
    result = ExperimentStage().step(engine.build_context())
    assert isinstance(result, Error) and not result.retryable


def test_api_planner_missing_key_is_nonretryable_and_unbilled(air_home, monkeypatch):
    from airesearcher.llm.models import MissingAPIKey

    project, engine, idea, _ = seeded(air_home)
    gateway, _, _ = api_fixture(project, monkeypatch, {"plan/draft": [preset_draft(idea, project.config.task)]})

    def missing(self, request):
        raise MissingAPIKey("Set the configured environment variable")

    monkeypatch.setattr(LiteLLMBackend, "call", missing)
    engine._llm = gateway
    drive(engine, S.IdeaPending)
    approve_pending(project)
    drive(engine, S.Failed)
    assert project.budget.used().get("llm_usd", 0) == 0
    assert not engine.executor.submitted
