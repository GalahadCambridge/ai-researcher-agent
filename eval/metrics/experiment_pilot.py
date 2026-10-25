"""Read-only experiment cost pilot from detailed design 3, section 15.1."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from airesearcher.core.fsutil import atomic_write_json, atomic_write_text, read_jsonl
from airesearcher.core.project import Project
from airesearcher.services.runs import list_runs

from .experiment_capability import evaluate as capability

PENDING = {"IdeaPending", "PlanPending", "LogPending", "ManuscriptPending"}


def state_seconds(events, start, end):
    """Pending-state elapsed time is waiting time, not human labour time."""
    transitions = [e for e in events if e.type == "state.changed" and start <= e.ts <= end and "to" in e.data]
    state = transitions[0].data.get("from", "unknown") if transitions else "unknown"
    cursor, totals = start, defaultdict(float)
    for event in transitions:
        totals[state] += max(0, (event.ts - cursor).total_seconds())
        state, cursor = event.data["to"], event.ts
    totals[state] += max(0, (end - cursor).total_seconds())
    return dict(totals)


def evaluate(root, annotations=None):
    project = Project.open(root)
    start = project.config.created_at
    events = [e for e in project.events.all() if e.ts >= start]
    handoffs = [e.ts for e in events if e.type == "state.changed" and e.data.get("to") == "Writing"]
    end = min(handoffs) if handoffs else max((e.ts for e in events), default=start)
    events = [e for e in events if e.ts <= end]
    intervals = state_seconds(events, start, end)
    calls = [c for c in read_jsonl(project.root / ".llm/calls.jsonl")
             if start <= datetime.fromisoformat(c["ts"]) <= end]
    roles = {}
    for call in calls:
        row = roles.setdefault(call["role"], {"gateway_calls": 0, "reported_attempts": 0,
                                             "input_tokens": 0, "output_tokens": 0, "usd": 0.0})
        row["gateway_calls"] += 1
        row["reported_attempts"] += call.get("attempts", 1)
        for key in ("input_tokens", "output_tokens", "usd"):
            row[key] += call.get(key, 0)
    for row in roles.values():
        row["usd"] = round(row["usd"], 6)
    views = [v for v in list_runs(project.root) if start <= v.record.created_at <= end]
    charges = [e for e in read_jsonl(project.root / "budget.jsonl")
               if start <= datetime.fromisoformat(e["ts"]) <= end]
    charged = defaultdict(float)
    for entry in charges:
        charged[entry["category"]] += entry["amount"]
    pending = [name for name in ("useful_followups", "injected_anomalies_detected")
               if not (annotations or {}).get(name)]
    return {
        "scope": "Experiment-module trajectory only; live literature and paper-writing costs are not implied",
        "workspace": str(project.root), "since": start.isoformat(), "through": end.isoformat(),
        "elapsed_seconds": max(0, (end - start).total_seconds()),
        "approval_wait_seconds": sum(value for key, value in intervals.items() if key in PENDING),
        "paused_seconds": intervals.get("Paused", 0), "state_seconds": intervals,
        "agent_active_wall_seconds_recorded": charged["wall_hours"] * 3600,
        "experiment_attempt_wall_seconds_sum": sum(v.resources.wall_seconds for v in views if v.resources),
        "cpu_seconds": sum(v.resources.cpu_seconds for v in views if v.resources),
        "gpu_seconds": sum(v.resources.gpu_seconds for v in views if v.resources),
        "run_attempts": len(views), "failed_run_attempts": sum(v.status.state.value == "failed" for v in views),
        "gateway_repair_attempts": sum(max(c.get("attempts", 1) - 1, 0) for c in calls),
        "llm_by_role": roles, "llm_usd": round(sum(c.get("usd", 0) for c in calls), 6),
        "capability_metrics": capability(project.root, annotations),
        "pending_human_assessments": pending,
        "independent_reproduction": "pending: a different team member must perform and sign the reproduction",
        "notes": [
            "Copied historical calls before this project's creation are excluded from pilot costs.",
            "The interval ends at the first Writing handoff; later validation activity is excluded.",
            "Approval wait is elapsed time in pending states, not measured human work.",
            "Summed run durations can overlap; they are not the elapsed project duration.",
            "A controlled acceptance run with developer repairs is not a clean research-cost estimate.",
            "Null capability values are not successful evaluations; external ratings are never invented.",
        ],
    }


def render(report):
    lines = ["# Experiment-module cost pilot", "", report["scope"], "",
             f"Recorded LLM cost: USD {report['llm_usd']:.6f}.",
             f"Elapsed interval: {report['elapsed_seconds']:.3f} seconds.",
             f"Approval waiting: {report['approval_wait_seconds']:.3f} seconds.",
             f"Other paused time: {report['paused_seconds']:.3f} seconds.",
             f"Experiment attempt wall-time sum: {report['experiment_attempt_wall_seconds_sum']:.3f} seconds.",
             f"Failed run attempts: {report['failed_run_attempts']}.", "",
             "| Role | Gateway calls | Reported attempts | Input tokens | Output tokens | USD |",
             "|---|---|---|---|---|---|"]
    for role, row in sorted(report["llm_by_role"].items()):
        lines.append(f"| {role} | {row['gateway_calls']} | {row['reported_attempts']} | "
                     f"{row['input_tokens']} | {row['output_tokens']} | {row['usd']:.6f} |")
    lines += ["", "## Capability metrics", "", "| Metric | Value |", "|---|---|"]
    lines += [f"| {key} | {value if value is not None else 'not established'} |"
              for key, value in report["capability_metrics"].items()]
    lines += ["", "## Pending external work", "", report["independent_reproduction"], ""]
    lines += [f"- {name}: requires human annotations or a separate labelled evaluation."
              for name in report["pending_human_assessments"]]
    lines += ["", "## Measurement limits", "", *["- " + note for note in report["notes"]]]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--annotations", type=Path)
    parser.add_argument("--out", required=True, type=Path, help="Output JSON; also writes a sibling Markdown report")
    args = parser.parse_args()
    import json

    annotations = json.loads(args.annotations.read_text(encoding="utf-8")) if args.annotations else None
    report = evaluate(args.workspace, annotations)
    atomic_write_json(args.out, report)
    atomic_write_text(args.out.with_suffix(".md"), render(report))
    print(args.out)


if __name__ == "__main__":
    main()
