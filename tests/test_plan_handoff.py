"""Verify the real literature, planning and execution stages together without model API calls."""

from __future__ import annotations

import json
import socket

import pytest

from airesearcher.core.fsutil import sha256_file
from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.models.plan import parse_plan_md
from airesearcher.core.project import Project
from airesearcher.engine import checkpoint
from airesearcher.engine.engine import ProjectEngine
from airesearcher.engine.stage import Stop
from airesearcher.llm.gateway import LiteLLMBackend
from airesearcher.services.runs import list_runs
from airesearcher.stages.plan.stage import PLAN, PlanStage
from airesearcher.testing.contracts import check_project
from airesearcher.testing.fake_llm import FakeLLM, fake_gateway
from scripts.experiment.local import configure_local, execute
from scripts.experiment.workspace import copy_pending_plan
from scripts.plan.handoff import (
    approve_test_idea,
    create_literature,
    drive_to,
    plan_engine,
    run_handoff,
)


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Handoff tests must not access the network or a paid model")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(LiteLLMBackend, "call", forbidden)


def test_full_handoff_fake_success_retry_and_real_local_execution(tmp_path):
    root = tmp_path / "joint"
    result = run_handoff(root, "tasks/smoke")
    assert result["complete"] and result["llm_usd"] == 0
    assert result["methods"] == ["baseline", "main"] and result["planned_runs"] == 6
    # A code repair invalidates both old same-commit trial gates: two new trials.
    assert result["fake_success"]["run_records"] == 8 and result["fake_retry"]["run_records"] == 11
    source = Project.open(root / "plan")
    selected = source.archive.latest("idea/selected.md")
    assert source.archive.version_info(selected).producer == "agent:idea/writer"
    assert result["idea_ref"] == selected.model_dump(mode="json")
    assert (source.root / "idea/reading_summary.json").exists()
    assert len(list((source.root / "idea/reading_cards").glob("*.json"))) == 4
    assert check_project(source.root, owners=["文献", "实验"]).ok

    local = copy_pending_plan(source.root, root / "real_local", simulation=False)
    configure_local(local, "all")
    [approval] = local.approvals.pending()
    local.approvals.decide(approval.approval_id, "approved", approval.target.sha256,
                           comment="Test-only local smoke execution", by="test:offline-handoff")
    executed = execute(local, "all", max_seconds=90)
    assert executed["complete"] and executed["formal_attempts"] == 6 and executed["trial_attempts"] == 2
    assert executed["execution_llm_calls"] == executed["llm_usd"] == 0
    assert all(r.record.idea_ref == selected and r.record.executor == "local" for r in list_runs(local.root))
    assert checkpoint.load(source.root).state == S.PlanPending and not list_runs(source.root)
    assert source.approvals.pending()[0].status == "pending"


def test_revised_idea_and_repaired_plan_reach_execution_with_exact_new_version(tmp_path):
    root = tmp_path / "revision"
    result = run_handoff(root, "tasks/smoke", revision=True, repair=True)
    assert result["complete"] and result["idea_ref"]["version"] == 2
    assert result["fake_plan_calls"] == 2
    source = Project.open(root / "plan")
    plan = parse_plan_md((source.root / PLAN).read_text(encoding="utf-8"))
    [old, new] = source.archive.versions("idea/selected.md")
    assert plan.idea_ref == new.ref and plan.idea_ref != old.ref
    versions = source.archive.versions(PLAN)
    assert len(versions) == 2
    for path in (root / "fake_success", root / "fake_retry"):
        assert all(r.record.idea_ref == new.ref and r.record.plan_ref == versions[-1].ref for r in list_runs(path))


def test_unapproved_upstream_idea_never_reaches_planning(tmp_path):
    project, engine, _ = create_literature(tmp_path / "unapproved", "tasks/smoke")
    drive_to(engine, S.IdeaPending)
    ref = project.archive.latest("idea/selected.md")
    with pytest.raises(ValueError, match="approval"):
        plan_engine(project, ref)
    ctx = engine.build_context()
    ctx.llm = fake_gateway(project)
    ctx.state = S.PlanDrafting
    result = PlanStage().step(ctx)
    assert isinstance(result, Stop) and not ctx.llm.backend.calls and not list_runs(project.root)


def test_pending_plan_restart_does_not_repeat_upstream_or_model_calls(tmp_path):
    root = tmp_path / "restart"
    run_handoff(root, "tasks/smoke")
    project = Project.open(root / "plan")
    paths = [project.root / "idea/selected.md", project.root / PLAN]
    before = [sha256_file(p) for p in paths]
    gateway = fake_gateway(project)
    engine = ProjectEngine(project, llm=gateway)
    engine.tick()
    assert engine.state == S.PlanPending and not gateway.backend.calls
    assert before == [sha256_file(p) for p in paths]


def test_test_approval_rejects_modified_selected_document(tmp_path):
    project, engine, _ = create_literature(tmp_path / "changed", "tasks/smoke")
    drive_to(engine, S.IdeaPending)
    path = project.root / "idea/selected.md"
    path.write_text(path.read_text(encoding="utf-8") + "\nUnapproved edit.\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        approve_test_idea(project, engine)
    assert project.approvals.pending()[0].status == "pending"


def test_switch_to_strict_fake_planner_and_reject_overwrite(tmp_path):
    project, engine, _ = create_literature(tmp_path / "switch", "tasks/smoke")
    drive_to(engine, S.IdeaPending)
    ref = approve_test_idea(project, engine)
    planner = plan_engine(project, ref)
    assert type(planner.llm.backend) is FakeLLM
    drive_to(planner, S.PlanPending)
    with pytest.raises(ValueError, match="empty workspace"):
        create_literature(project.root, "tasks/smoke")
    with pytest.raises(ValueError, match="empty workspace"):
        run_handoff(tmp_path / "switch", "tasks/smoke")
    checks = json.loads((project.root / "plan/precheck.json").read_text(encoding="utf-8"))
    assert not any(c["status"] == "fail" for c in checks)
