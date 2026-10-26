"""Semantic changes and protected files must be rejected and rolled back."""

import json

from airesearcher.stages.experiment.coder import CoderReport, restore, semantic_guard, snapshot
from airesearcher.stages.experiment.stage import load_approved

from .test_experiment_stage import prepared


def test_protected_file_change_is_rejected_and_reverted(air_home):
    project, engine, _ = prepared(air_home)
    engine.tick()
    ctx = engine.build_context()
    approved = load_approved(ctx)
    before = snapshot(ctx.root)
    target = ctx.root / "src/air_report.py"
    target.write_text("# attempted evaluator edit\n", encoding="utf-8")
    report = CoderReport(
        ok=True, changed_files=["src/air_report.py"], summary="test", trial_run_id=None, needs_plan_change=False
    )
    errors = semantic_guard(ctx, approved.task, ctx.scratch["experiment"]["entries"], before, report)
    assert any("Protected" in error for error in errors)
    ctx.workspace.commit("Rejected candidate", author="agent:experiment")
    restore(ctx, before)
    assert target.read_bytes() == before["src/air_report.py"]


def test_changed_semantic_config_is_rejected(air_home):
    _, engine, _ = prepared(air_home)
    engine.tick()
    ctx = engine.build_context()
    approved = load_approved(ctx)
    before = snapshot(ctx.root)
    entries = ctx.scratch["experiment"]["entries"]
    key = "E1-seed=0"
    entry = entries[key]
    path = ctx.root / "configs" / entry["experiment_id"] / (key + ".yaml")
    path.write_text(json.dumps({**entry["config"], "method": "main"}), encoding="utf-8")
    report = CoderReport(ok=True, changed_files=[], summary="test", trial_run_id=None, needs_plan_change=False)
    assert any("semantics" in e for e in semantic_guard(ctx, approved.task, entries, before, report))


def test_oom_preserves_effective_batch_and_per_error_limits():
    from airesearcher.core.models.plan import TaskConfig
    from airesearcher.stages.experiment.repair import recovery

    task = TaskConfig(
        name="fixture",
        domain="generic",
        description="test",
        template_dir="",
        entrypoint=[],
        metrics=[],
        repair_tunable=["batch_size", "grad_accum_steps"],
    )
    entry = {"error_class": "oom", "config": {"batch_size": 8, "grad_accum_steps": 2}, "trial": False}
    action, cfg = recovery(entry, task, 2)
    assert action == "config" and cfg["batch_size"] == 4 and cfg["grad_accum_steps"] == 4
    entry["repair_counts"] = {"oom": 2}
    assert recovery(entry, task, 2)[0] == "question"
    entry["error_class"] = "lost"
    assert recovery(entry, task, 2)[0] == "retry"
    entry["repair_counts"]["lost"] = 1
    assert recovery(entry, task, 2)[0] == "question"


def test_scientific_notation_preserves_numeric_semantics(air_home):
    from airesearcher.runtime.local import config_text

    _, engine, _ = prepared(air_home)
    engine.tick()
    ctx = engine.build_context()
    approved = load_approved(ctx)
    entries = ctx.scratch["experiment"]["entries"]
    entry = entries["E1-seed=0"]
    entry["config"]["learning_rate"] = 3e-5
    (ctx.root / "configs/E1/E1-seed=0.yaml").write_text(config_text(entry["config"]), encoding="utf-8")
    approved.task.semantic_keys.append("learning_rate")
    report = CoderReport(ok=True, changed_files=[], summary="unchanged", trial_run_id=None, needs_plan_change=False)
    assert semantic_guard(ctx, approved.task, entries, snapshot(ctx.root), report) == []
