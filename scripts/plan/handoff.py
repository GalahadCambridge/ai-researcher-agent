"""Literature -> plan -> execution integration using preset LLM replies only."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from airesearcher.core.fsutil import atomic_write_json, dumps
from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.models.idea import parse_selected_md
from airesearcher.core.models.plan import parse_plan_md
from airesearcher.core.project import Project
from airesearcher.engine.engine import ProjectEngine
from airesearcher.engine.stage import describe
from airesearcher.services.runs import list_runs
from airesearcher.stages.idea.stage import IdeaStage
from airesearcher.stages.plan.expand import load_task, selected_methods
from airesearcher.stages.plan.stage import PLAN, PROMPT, PlanStage
from airesearcher.testing.contracts import check_project
from airesearcher.testing.fake_executor import FakeExecutor
from airesearcher.testing.fake_literature import FakeLiterature
from airesearcher.testing.fake_llm import fake_gateway
from scripts.experiment.demo import run_demo
from scripts.literature.demo import demo_gateway, demo_responses
from scripts.plan.fixtures import leaking_draft, offline_models, preset_draft

MARKER = "artifacts/OFFLINE_HANDOFF.json"


def drive_to(engine, target, *, max_seconds=90):
    deadline = time.monotonic() + max_seconds
    for _ in range(300):
        tick = engine.tick()
        if tick.result:
            print(describe(tick.result), flush=True)
        if engine.state == target:
            return
        if engine.state in (S.Paused, S.Failed, S.BudgetExhausted, S.Executing, S.Analyzing):
            raise ValueError(f"Unexpected state {engine.state.value}: {engine.ck.reason}")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Did not reach {target.value} within the integration deadline")
        if tick.seconds:
            time.sleep(min(tick.seconds, 0.1))
    raise TimeoutError(f"Too many steps before {target.value}")


def create_literature(root: Path, task_path: str):
    root = root.resolve()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ValueError("Use a new empty workspace; existing results are never overwritten")
    _, task, _ = load_task(task_path)
    if task.name == "text_cls_lowres":
        goal = "Compare full_ft and lora for low-resource text classification on SST-2."
    elif task.name == "smoke":
        goal = "Compare baseline and main for an explicitly simulated workflow test."
    else:
        raise ValueError("The integration fixture supports smoke and text_cls_lowres only")
    project = Project.create(
        root=root,
        register=False,
        task=task_path,
        goal=goal,
        title="Offline literature-plan handoff",
        models=offline_models(),
        llm_cache="off",
        reading_mode="multi",
        limits={"read_papers": 2, "reader_concurrency": 1},
        literature={"year_from": 2018},
        dev={"stage_impl": {"idea": "real", "plan": "real", "experiment": "real", "writing": "fake"}},
    )
    project.archive.put(
        MARKER,
        "other",
        dumps(
            {
                "simulation": True,
                "llm": "FakeLLM",
                "retrieval": "FakeLiterature",
                "warning": "Integration test only. Preset replies do not establish literature or research quality.",
                "test_approvals_only": True,
            }
        ),
        producer="system",
    )
    responses = demo_responses()
    responses["idea/parse_input"][0].update(
        task=task_path,
        missing_info=["Offline integration: model replies and research judgments are simulated."],
    )
    source = FakeLiterature()
    gateway = demo_gateway(project, responses)
    executor = FakeExecutor(project)
    engine = ProjectEngine(
        project,
        llm=gateway,
        executor=executor,
        retry_delay=0,
        stages={"idea": IdeaStage(source, fulltext_download=False)},
    )
    return project, engine, source


def approve_test_idea(project, engine):
    marker = project.root / MARKER
    if not marker.is_file() or not json.loads(marker.read_text(encoding="utf-8")).get("test_approvals_only"):
        raise ValueError("Test approval is restricted to this runner's explicitly simulated workspace")
    if engine.state != S.IdeaPending:
        raise ValueError("The literature stage must first submit its idea for approval")
    [approval] = project.approvals.pending()
    if approval.kind != "idea" or approval.target != project.archive.latest("idea/selected.md"):
        raise ValueError("The pending approval does not identify the generated idea version")
    if project.archive.current_sha("idea/selected.md") != approval.target.sha256:
        raise ValueError("The selected idea changed after approval submission")
    project.approvals.decide(
        approval.approval_id,
        "approved",
        approval.target.sha256,
        by="test:offline-handoff",
        comment="Simulated approval for this isolated integration test; no real research approval is implied.",
    )
    return approval.target


def plan_engine(project, ref, *, repair=False):
    approved = [a for a in project.approvals.list(kind="idea") if a.status == "approved" and a.target == ref]
    if not approved:
        raise ValueError("An exact-version idea approval is required before planning")
    idea = parse_selected_md(project.archive.read_text(ref))
    _, task, _ = load_task(project.config.task)
    selected_methods(idea, task)  # Fail explicitly on an unsupported upstream method.
    draft = preset_draft(idea, project.config.task)
    script = [leaking_draft(draft), draft] if repair else [draft]
    gateway = fake_gateway(project, {PROMPT: script})
    engine = ProjectEngine(
        project, llm=gateway, executor=FakeExecutor(project), retry_delay=0, stages={"plan": PlanStage()}
    )
    return engine


def build_plan(root: Path, task_path: str, *, revision=False, repair=False):
    project, idea_engine, source = create_literature(root, task_path)
    print("[1/3] Running the teammate's IdeaStage with FakeLLM + local literature fixtures.", flush=True)
    drive_to(idea_engine, S.IdeaPending)
    previous = project.archive.latest("idea/selected.md")
    if revision:
        [approval] = project.approvals.pending()
        project.approvals.decide(
            approval.approval_id,
            "changes_requested",
            approval.target.sha256,
            comment="Offline test revision: keep the same task and comparison targets.",
            by="test:offline-handoff",
        )
        drive_to(idea_engine, S.IdeaPending)
        if project.archive.latest("idea/selected.md").version != previous.version + 1:
            raise ValueError("The requested idea revision did not create a new version")
    ref = approve_test_idea(project, idea_engine)
    print("[2/3] Test idea approval recorded; PlanStage will read that exact archived version.", flush=True)
    engine = plan_engine(project, ref, repair=repair)
    drive_to(engine, S.PlanPending)
    plan = parse_plan_md(project.archive.read_text(project.archive.latest(PLAN)))
    if plan.idea_ref != ref or engine.ck.approved.get("idea") != ref:
        raise ValueError("The plan did not bind the approved upstream idea version")
    if idea_engine.executor.submitted or engine.executor.submitted or list_runs(project.root):
        raise ValueError("Planning unexpectedly submitted training")
    report = check_project(project.root, owners=["文献", "实验"])
    print(report.text(), flush=True)
    if not report.ok:
        raise ValueError("Literature/plan contract checks failed")
    result = {
        "idea_implementation": "airesearcher.stages.idea.stage.IdeaStage",
        "plan_implementation": "airesearcher.stages.plan.stage.PlanStage",
        "idea_ref": ref.model_dump(mode="json"),
        "task": project.config.task,
        "methods": [e.method for e in plan.experiments],
        "planned_runs": len(json.loads((project.root / "plan/tasks.json").read_text(encoding="utf-8"))),
        "fake_idea_calls": len(idea_engine.llm.backend.calls),
        "fake_plan_calls": len(engine.llm.backend.calls),
        "local_retrieval_queries": len(source.calls),
        "plan_state": engine.state.value,
    }
    return project, result


def run_handoff(root: Path, task_path="tasks/text_cls_lowres", *, revision=False, repair=False):
    root = root.resolve()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ValueError("Use a new empty workspace; existing results are never overwritten")
    root.mkdir(parents=True, exist_ok=True)
    project, result = build_plan(root / "plan", task_path, revision=revision, repair=repair)
    print("[3/3] Completing experiment scheduling with FakeExecutor, including a failure/retry copy.", flush=True)
    happy = run_demo(project.root, root / "fake_success", "happy")
    retried = run_demo(project.root, root / "fake_retry", "retry")
    if project.approvals.pending()[0].kind != "plan" or list_runs(project.root):
        raise ValueError("The frozen source plan must remain unchanged for independent execution verification")
    if project.budget.used().get("llm_usd", 0) != 0:
        raise ValueError("The integration test must have zero model API cost")
    result.update(
        simulation=True,
        complete=happy["complete"] and retried["complete"],
        fake_success=happy,
        fake_retry=retried,
        llm_usd=0,
        workspace=str(root),
    )
    atomic_write_json(root / "handoff_report.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--task", choices=["smoke", "text_cls_lowres"], default="text_cls_lowres")
    parser.add_argument("--revise-idea", action="store_true")
    parser.add_argument("--repair-plan", action="store_true")
    parser.add_argument("--approve-test-idea", action="store_true")
    args = parser.parse_args()
    if not args.approve_test_idea:
        parser.error("Pass --approve-test-idea to simulate approval in the new isolated test workspace only")
    print(
        "[FAKE ONLY] Real stage implementations; preset model replies, local fixtures, no API, no training.", flush=True
    )
    print("Idea and plan approvals are simulated only in independent test workspaces.", flush=True)
    try:
        run_handoff(args.workspace, f"tasks/{args.task}", revision=args.revise_idea, repair=args.repair_plan)
    except (OSError, ValueError, TimeoutError) as exc:
        parser.exit(1, f"Offline handoff stopped: {exc}\n")


if __name__ == "__main__":
    main()
