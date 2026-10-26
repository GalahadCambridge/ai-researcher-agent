"""Isolated FakeLLM/FakeExecutor acceptance; no paid API or model training."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.models.frontmatter import render_front_matter
from airesearcher.core.project import Project
from airesearcher.engine import checkpoint
from airesearcher.engine.engine import ProjectEngine
from airesearcher.engine.stage import describe
from airesearcher.runtime.data import inventory
from airesearcher.services.runs import list_runs
from airesearcher.stages.plan.expand import load_task
from airesearcher.testing.contracts import check_project
from airesearcher.testing.fake_executor import FakeExecutor, FakeOutcome
from airesearcher.testing.fake_llm import FakeToolCalls, fake_gateway
from scripts.experiment.workspace import copy_pending_plan
from scripts.plan.demo import FixtureIdeaStage, imported_idea
from scripts.plan.fixtures import fixture_idea, offline_models, preset_draft


def summary(project):
    state = checkpoint.load(project.root).scratch.get("experiment", {})
    entries = state.get("entries", {})
    formal = [e for e in entries.values() if not e["trial"]]
    return {
        "simulation": state.get("simulation", True),
        "complete": bool(formal) and all(e["state"] == "done" for e in formal),
        "successful_formal_tasks": sum(e["state"] == "done" for e in formal),
        "trials_complete": all(
            e["state"] == "done" for e in entries.values() if e["trial"] and not e.get("coder_trial")
        ),
        "formal_attempts": sum(len(e["runs"]) for e in formal),
        "trial_attempts": sum(len(e["runs"]) for e in entries.values() if e["trial"]),
        "tasks": [{"task_key": k, **e} for k, e in entries.items()],
    }


def simulated_outcome(record, config):
    values = {name: 0.01 if name == "train_seconds" else 0.5 for name in record.required_metrics}
    return FakeOutcome(
        metrics=values,
        wall_seconds=0.01,
        stdout="SIMULATED: preset metrics; no training.\n",
        metric_splits={"train_seconds": "train"},
    )


def analysis_reply(decision="stop_and_write"):
    return {
        "observations": [],
        "interpretations": [
            {
                "text": "Preset workflow validation only.",
                "status": "speculation",
                "alternatives": ["Simulated metrics do not test a research hypothesis."],
            }
        ],
        "hypothesis_status": {"H1": "inconclusive", "H2": "inconclusive"},
        "proposal": {
            "decision": decision,
            "rationale": "Follow the approved matrix and deterministic checks.",
            "trigger_evidence": [],
            "target_hypotheses": ["H1"],
            "distinguishes": "Whether variability persists under independent seeds.",
            "est_cost": {"runs": 0, "wall_hours": 0},
            "stop_condition": "Approved seed, round or budget limits.",
        },
    }


def coder_reply():
    return FakeToolCalls(
        [
            ("write_file", {"path": "src/session_check.py", "content": "SESSION_CHECK = True\n"}),
            (
                "finish",
                {
                    "ok": True,
                    "changed_files": ["src/session_check.py"],
                    "summary": "Synthetic coding-session fixture, not an algorithmic repair.",
                    "trial_run_id": None,
                    "needs_plan_change": False,
                },
            ),
        ]
    )


def copy_plan_workspace(source, destination):
    return copy_pending_plan(source, destination, simulation=True)


def run_demo(source, destination, scenario="happy", *, idea_source=None):
    stages = None
    if source:
        project = copy_plan_workspace(source, destination)
        _, task_config, _ = load_task(project.config.task)
        if inventory(task_config)[2]:
            raise ValueError("Prepare the local data/model before this offline acceptance; no downloads are started")
        script = {}
    else:
        provenance = None
        if idea_source:
            idea, text, task, provenance = imported_idea(idea_source)
        else:
            task = "tasks/smoke"
            idea = fixture_idea(task)
            text = render_front_matter(idea.model_dump(mode="json"), "# Synthetic input idea\n")
        _, task_config, _ = load_task(task)
        if inventory(task_config)[2]:
            raise ValueError("Prepare the local data/model before this offline acceptance; no downloads are started")
        project = Project.create(
            root=destination,
            register=False,
            task=task,
            goal=idea.research_question,
            models=offline_models(),
            dev={"stage_impl": {"plan": "real", "experiment": "real", "writing": "fake"}},
        )
        parents = []
        if provenance:
            source_ref = project.archive.put(
                "idea/import_source.json", "other", json.dumps(provenance, indent=2) + "\n", producer="system"
            )
            parents.append(source_ref)
        ref = project.archive.put("idea/selected.md", "idea", text, parents=parents, producer="system")
        draft = preset_draft(idea, task)
        if scenario == "replicate":
            draft["replication_policy"] = {"max_extra_seeds_per_group": 1, "trigger": "std > 0.03"}
            draft["stopping_conditions"].update(max_rounds=3, no_progress_rounds=2)
        if scenario == "code":
            draft["experiments"][0]["needs_code"] = True
        script = {"plan/draft": [draft]}
        stages = {"idea": FixtureIdeaStage(ref)}
    script.update(
        {
            "experiment/analyze": [analysis_reply("replicate"), analysis_reply()]
            if scenario == "replicate"
            else [analysis_reply()],
            "experiment/coder": [coder_reply()],
        }
    )
    target = None

    def outcome(record, config):
        nonlocal target
        if target is None and record.kind != "trial":
            target = record.task_key
        if (
            scenario in ("retry", "fail", "missing-metrics", "skip")
            and record.task_key == target
            and record.kind != "trial"
            and (record.retry_of is None or scenario != "retry")
        ):
            return (
                FakeOutcome.missing_metrics(stderr="Synthetic missing-metric failure")
                if scenario == "missing-metrics"
                else FakeOutcome.fail(stderr="RuntimeError: synthetic recovery test")
            )
        if scenario == "replicate" and record.kind != "trial":
            return FakeOutcome(
                metrics={m: [0.35, 0.55, 0.75][config.get("seed", 0) % 3] for m in record.required_metrics},
                wall_seconds=0.01,
            )
        return simulated_outcome(record, config)

    executor = FakeExecutor(project, outcomes=outcome)
    gateway = fake_gateway(project, script)
    engine = ProjectEngine(project, executor=executor, llm=gateway, stages=stages, retry_delay=0)
    logs_approved = set()
    for _ in range(1200):
        tick = engine.tick()
        if tick.result:
            print(describe(tick.result), flush=True)
        pending = project.approvals.pending()
        if pending:
            [approval] = pending
            if (
                approval.kind == "log"
                and engine.ck.scratch["experiment"]["analysis_decision"]["decision"] != "replicate"
            ):
                break
            project.approvals.decide(
                approval.approval_id,
                "approved",
                approval.target.sha256,
                comment="Synthetic human approval in isolated Fake-only acceptance",
                by="demo",
            )
            if approval.kind == "log":
                logs_approved.add(approval.approval_id)
        if engine.state == S.Paused:
            if scenario == "skip" and project.questions.pending():
                q = project.questions.pending()
                project.questions.answer(q.question_id, "skip", "Synthetic demo choice", request_id=q.question_id)
            else:
                break
        if engine.state in (S.Failed, S.BudgetExhausted):
            break
        if tick.seconds:
            time.sleep(min(0.02, tick.seconds))
    else:
        raise RuntimeError("Fake acceptance exceeded the bounded tick count")
    contract = check_project(project.root, owners=["实验"])
    print(contract.text())
    result = {
        "state": engine.state.value,
        **summary(project),
        "run_records": len(list_runs(project.root)),
        "fake_llm_calls": len(gateway.backend.calls),
        "llm_usd": project.budget.used().get("llm_usd", 0),
        "training_submissions": 0,
        "approved_replication_logs": len(logs_approved),
        "workspace": str(project.root),
    }
    result.pop("tasks", None)
    print(json.dumps({k: v for k, v in result.items() if k != "tasks"}, indent=2, default=str))
    expected = S.Paused if scenario in ("fail", "missing-metrics") else S.LogPending
    if (
        engine.state != expected
        or not contract.ok
        or result["llm_usd"]
        or any(v.record.executor != "fake" for v in list_runs(project.root))
    ):
        raise RuntimeError(f"Fake acceptance failed: {engine.ck.reason}")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--plan-workspace", type=Path)
    parser.add_argument("--from-project", type=Path)
    parser.add_argument(
        "--scenario",
        choices=["happy", "code", "retry", "replicate", "fail", "skip", "missing-metrics"],
        default="happy",
    )
    parser.add_argument("--approve-plan-for-simulation", action="store_true")
    args = parser.parse_args()
    if not args.approve_plan_for_simulation:
        parser.error("Isolated demo approvals require --approve-plan-for-simulation")
    if args.workspace.exists() and any(args.workspace.iterdir()):
        parser.error("Choose an empty destination")
    print("[FAKE ONLY] FakeLLM + FakeExecutor. No paid API; no CPU/GPU training.")
    run_demo(args.plan_workspace, args.workspace, args.scenario, idea_source=args.from_project)


if __name__ == "__main__":
    main()
