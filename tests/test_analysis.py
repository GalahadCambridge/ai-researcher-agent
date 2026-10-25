"""Analysis evidence, replication, budget, no-progress and approval boundaries."""

import pytest

from airesearcher.core.models.common import ProjectState as S
from airesearcher.services import runs
from airesearcher.stages.experiment.aggregate import publish_results
from airesearcher.stages.experiment.analysis import AnalysisReport, checks, decide, parse_trigger, validate_observations
from airesearcher.stages.experiment.stage import load_approved
from airesearcher.testing.fake_executor import FakeOutcome
from scripts.experiment.demo import analysis_reply
from scripts.plan.fixtures import fixture_idea, preset_draft

from .helpers import approve_pending, drive
from .test_experiment_stage import prepared


def unstable(rec, cfg):
    return FakeOutcome(metrics={"score": [0.3, 0.5, 0.7][cfg.get("seed", 0) % 3]}, wall_seconds=0.01)


def replication_project(air_home):
    draft = preset_draft(fixture_idea("tasks/smoke"), "tasks/smoke")
    draft["replication_policy"] = {
        "max_extra_seeds_per_group": 1,
        "trigger": "std > 0.03 or overlapping 95% intervals and mean difference < 0.01",
    }
    draft["stopping_conditions"].update(max_rounds=3, no_progress_rounds=2)
    return prepared(air_home, draft=draft, outcomes=unstable, analysis=[analysis_reply("replicate"), analysis_reply()])


def test_replication_log_approval_then_second_round_and_stop(air_home):
    project, engine, executor = replication_project(air_home)
    drive(engine, S.LogPending)
    assert (
        len(executor.submitted) == 12
        and engine.ck.scratch["experiment"]["analysis_decision"]["decision"] == "replicate"
    )
    approve_pending(project)
    drive(engine, S.LogPending)
    assert engine.ck.scratch["experiment"]["round"] == 2
    replicas = [v for v in runs.list_runs(project.root) if v.record.kind == "replication"]
    assert len(replicas) == 3 and {v.record.seed for v in replicas} == {3}
    assert len(executor.submitted) == 15
    assert engine.ck.scratch["experiment"]["analysis_decision"]["decision"] == "stop_and_write"
    approve_pending(project)
    drive(engine, S.Writing)


def test_budget_is_checked_again_after_log_approval(air_home):
    project, engine, executor = replication_project(air_home)
    drive(engine, S.LogPending)
    used = project.budget.used().get("wall_hours", 0)
    limit = project.config.budget["wall_hours"].model_copy(update={"hard": used + 0.001})
    project.save_config(project.config.model_copy(update={"budget": {**project.config.budget, "wall_hours": limit}}))
    approve_pending(project)
    drive(engine, S.LogPending)
    assert len(executor.submitted) == 12
    assert engine.ck.scratch["experiment"]["analysis_decision"]["decision"] == "stop_and_write"


def test_observation_numbers_are_verified_against_exact_evidence(air_home):
    project, engine, _ = prepared(air_home)
    drive(engine, S.Analyzing)
    ctx = engine.build_context()
    approved = load_approved(ctx)
    aggregates = publish_results(ctx, approved)
    a = aggregates[0]
    row = a.rows[0]
    report = AnalysisReport.model_validate(analysis_reply())
    evidence = {"aggregate_id": a.aggregate_id, "group": row.group, "metric": row.metric, "stat": "mean"}
    observations = [
        {"text": f"Observed mean {row.mean:.3f}", "evidence": [evidence]},
        {"text": "Invented mean 0.999", "evidence": [evidence]},
        {"text": "Missing statistic 0.5", "evidence": [{**evidence, "stat": "imaginary"}]},
    ]
    report = AnalysisReport.model_validate({**report.model_dump(), "observations": observations})
    validate_observations(ctx, report, aggregates)
    assert len(report.observations) == 1


def test_no_progress_and_max_rounds_override_model(air_home):
    _, engine, _ = replication_project(air_home)
    drive(engine, S.Analyzing)
    ctx = engine.build_context()
    approved = load_approved(ctx)
    aggregates = publish_results(ctx, approved)
    deterministic = checks(ctx, approved, aggregates)
    report = AnalysisReport.model_validate(analysis_reply("replicate"))
    first = decide(ctx, approved, report, deterministic, aggregates)
    ctx.scratch["experiment"]["analysis_history"] = [first["progress"]]
    ctx.scratch["experiment"]["analysis_history"][-1]["no_progress"] = (
        approved.plan.stopping_conditions.no_progress_rounds - 1
    )
    next_decision = decide(ctx, approved, report, deterministic, aggregates)
    assert next_decision["decision"] == "stop_and_write" and next_decision["forced_stop"]
    ctx.scratch["experiment"]["round"] = approved.plan.stopping_conditions.max_rounds
    assert decide(ctx, approved, report, deterministic, aggregates)["decision"] == "stop_and_write"


@pytest.mark.parametrize("proposal", ["revise_plan", "revise_idea", "diagnose"])
def test_completed_negative_result_stops_instead_of_chasing_a_positive_result(air_home, proposal):
    def negative_outcome(record, config):
        return FakeOutcome(metrics={"score": 0.5 if config["method"] == "main" else 0.6}, wall_seconds=0.01)

    project, engine, executor = prepared(
        air_home, outcomes=negative_outcome, analysis=[analysis_reply(proposal)]
    )
    drive(engine, S.LogPending)
    decision = engine.ck.scratch["experiment"]["analysis_decision"]
    assert decision["decision"] == "stop_and_write" and decision["forced_stop"]
    log = (project.root / "logs/round_1.md").read_text(encoding="utf-8")
    assert f"Model proposal `{proposal}` was not executed" in log
    assert "All approved experiments are complete" in log
    submitted = list(executor.submitted)
    approve_pending(project)
    drive(engine, S.Writing)
    assert executor.submitted == submitted
    assert not any(call.prompt_id == "plan/draft" for call in engine.llm.backend.calls)


def test_primary_metric_gain_does_not_apply_to_training_seconds(air_home):
    _, engine, _ = prepared(air_home)
    drive(engine, S.Analyzing)
    ctx = engine.build_context()
    approved = load_approved(ctx)
    aggregates = publish_results(ctx, approved)
    fake_cost = aggregates[0].model_copy(deep=True)
    fake_cost.comparison["metric"] = "train_seconds"
    for row in fake_cost.rows:
        row.mean = 500.0
        row.max = 600.0
    assert not any(a["type"] == "too_good" for a in checks(ctx, approved, [fake_cost])["anomalies"])


def test_text_trigger_is_parsed_without_eval():
    assert parse_trigger("同组 std > 0.03 或两组 95% 区间重叠且均值差 < 0.01") == (0.03, 0.01)
    with pytest.raises(ValueError):
        parse_trigger("__import__('os').system('anything')")
