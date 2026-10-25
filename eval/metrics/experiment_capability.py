"""Experiment capability metrics (detailed design 3, §15.2).

Human-rated follow-up usefulness and deliberately injected anomaly labels must
be supplied; they cannot be inferred from an LLM claiming success.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from airesearcher.core.archive import Archive
from airesearcher.core.events import Events
from airesearcher.core.models.idea import parse_selected_md
from airesearcher.services.runs import list_runs, load_plan
from airesearcher.stages.experiment.repair import error_class

REPAIRABLE = {"config_error", "data_error", "oom", "timeout", "runtime_error", "missing_metrics", "lost"}


def repairable(root, view):
    category = view.status.error_class
    if category is None:
        path = Path(root) / "runs" / view.record.run_id / "stderr.log"
        category = error_class(view.status, path.read_text(encoding="utf-8") if path.exists() else "")
    return category in REPAIRABLE


def evaluate(root, annotations=None):
    annotations = annotations or {}
    for name in ("useful_followups", "injected_anomalies_detected"):
        values = annotations.get(name, [])
        if not isinstance(values, list) or any(type(value) is not bool for value in values):
            raise ValueError(f"{name} must be a list of explicit boolean assessments")
    plan = load_plan(root)
    views = list_runs(root)
    experiments = {e.id for e in plan.experiments}
    first = {}
    for view in views:
        if view.record.kind == "trial":
            first.setdefault(view.record.experiment_id, view)
    failed = [v for v in views if v.status.state.value == "failed" and repairable(root, v)]
    recovered = {v.record.retry_of for v in views if v.record.retry_of and v.status.state.value == "succeeded"}
    actual = sum(v.resources.wall_seconds for v in views if v.resources) / 3600
    estimate = plan.budget_estimate.get("wall_hours", 0)
    idea = parse_selected_md(Archive(Path(root), Events(Path(root))).read_text(plan.idea_ref))
    hypotheses = {h.id for h in idea.hypotheses}
    covered = {h for e in plan.experiments for h in e.hypotheses}
    usefulness = annotations.get("useful_followups", [])
    anomalies = annotations.get("injected_anomalies_detected", [])
    return {
        "first_trial_success_rate": sum(v.status.state.value == "succeeded" for v in first.values()) / len(experiments)
        if experiments
        else None,
        "failure_recovery_rate": sum(v.record.run_id in recovered for v in failed) / len(failed) if failed else None,
        "effective_followup_fraction": sum(usefulness) / len(usefulness) if usefulness else None,
        "plan_coverage": len(hypotheses & covered) / len(hypotheses) if hypotheses else None,
        "budget_error": abs(actual - estimate) / estimate if estimate > 0 else None,
        "anomaly_detection_rate": sum(anomalies) / len(anomalies) if anomalies else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--annotations", type=Path)
    args = parser.parse_args()
    annotations = json.loads(args.annotations.read_text(encoding="utf-8")) if args.annotations else None
    print(json.dumps(evaluate(args.workspace, annotations), indent=2))


if __name__ == "__main__":
    main()
