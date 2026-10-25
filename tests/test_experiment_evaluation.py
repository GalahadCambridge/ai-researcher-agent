"""Documented evaluation metrics and cost attribution; no real model calls."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from airesearcher.core.fsutil import append_jsonl
from airesearcher.core.models.common import ProjectState as S
from eval.metrics.experiment_capability import evaluate as capability
from eval.metrics.experiment_capability import repairable
from eval.metrics.experiment_pilot import evaluate, render, state_seconds

from .helpers import approve_pending, drive
from .test_experiment_stage import prepared


def test_pending_state_time_is_separate_from_active_time():
    from datetime import UTC, datetime

    start = datetime(2026, 10, 23, tzinfo=UTC)
    changes = [(10, "PlanDrafting", "PlanPending"), (30, "PlanPending", "Executing"),
               (40, "Executing", "LogPending"), (50, "LogPending", "Writing")]
    events = [SimpleNamespace(type="state.changed", ts=start + timedelta(seconds=second),
                              data={"from": before, "to": after}) for second, before, after in changes]
    result = state_seconds(events, start, start + timedelta(seconds=60))
    assert result == {"PlanDrafting": 10, "PlanPending": 20, "Executing": 10, "LogPending": 10, "Writing": 10}


def test_pilot_excludes_inherited_and_post_handoff_calls(air_home):
    project, engine, _ = prepared(air_home)
    drive(engine, S.LogPending)
    approve_pending(project)
    drive(engine, S.Writing)
    start = project.config.created_at
    path = project.root / ".llm/calls.jsonl"
    for timestamp, usd in ((start - timedelta(seconds=1), 99),
                           (start + timedelta(microseconds=1), 0.03),
                           (start + timedelta(days=1), 99)):
        append_jsonl(path, {"ts": timestamp.isoformat(), "role": "exp.analyst", "usd": usd,
                            "attempts": 2, "input_tokens": 10, "output_tokens": 5})
    report = evaluate(project.root)
    assert report["llm_usd"] == 0.03
    assert report["approval_wait_seconds"] >= 0 and report["failed_run_attempts"] == 0
    assert report["capability_metrics"]["plan_coverage"] == 1
    assert report["capability_metrics"]["effective_followup_fraction"] is None
    assert "useful_followups" in report["pending_human_assessments"]
    assert "not established" in render(report)
    assert "pending" in report["independent_reproduction"]


def test_human_assessment_metrics_require_explicit_labels(air_home):
    project, engine, _ = prepared(air_home)
    drive(engine, S.Analyzing)
    scores = capability(project.root, {"useful_followups": [True, False],
                                       "injected_anomalies_detected": [True]})
    assert scores["effective_followup_fraction"] == 0.5 and scores["anomaly_detection_rate"] == 1
    with pytest.raises(ValueError, match="explicit boolean"):
        capability(project.root, {"useful_followups": ["yes"]})


def test_recovery_denominator_excludes_manual_dependency_failures(tmp_path):
    view = SimpleNamespace(status=SimpleNamespace(error_class="import_error"))
    assert not repairable(tmp_path, view)
    view.status.error_class = "runtime_error"
    assert repairable(tmp_path, view)
    view.record = SimpleNamespace(run_id="failed-run")
    view.status = SimpleNamespace(error_class=None, failure_reason="nonzero_exit", exit_code=1)
    directory = tmp_path / "runs/failed-run"
    directory.mkdir(parents=True)
    (directory / "stderr.log").write_text("ModuleNotFoundError: absent_package", encoding="utf-8")
    assert not repairable(tmp_path, view)
