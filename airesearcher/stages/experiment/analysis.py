"""Evidence-checked interpretation and bounded decisions (design 3, §9)."""

from __future__ import annotations

import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from airesearcher.core.errors import NotFound
from airesearcher.core.fsutil import dumps, sha256_bytes
from airesearcher.core.models.idea import parse_selected_md
from airesearcher.runtime.task import uses_gpu
from airesearcher.services import runs
from airesearcher.stages.plan.precheck import inspect_inputs

from .aggregate import render_table


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Observation(StrictModel):
    text: str
    evidence: list[dict]


class Interpretation(StrictModel):
    text: str
    status: Literal["supported_by_experiment", "correlational", "speculation"]
    alternatives: list[str] = Field(default_factory=list)


class NextStepProposal(StrictModel):
    decision: Literal["run_next_planned", "replicate", "diagnose", "revise_plan", "revise_idea", "stop_and_write"]
    rationale: str
    trigger_evidence: list[dict]
    target_hypotheses: list[str]
    distinguishes: str
    est_cost: dict
    stop_condition: str


class AnalysisReport(StrictModel):
    observations: list[Observation]
    interpretations: list[Interpretation]
    hypothesis_status: dict[str, Literal["supported", "refuted", "inconclusive", "untested"]]
    proposal: NextStepProposal


def round_number(ctx):
    return ctx.scratch.get("experiment", {}).get("round", 1)


def log_id(ctx):
    return f"logs/round_{round_number(ctx)}.md"


def cost(tasks, task=None):
    return {
        "runs": len(tasks),
        "wall_hours": sum(t["est_minutes"] / 60 for t in tasks),
        "gpu_hours": sum(
            t["est_minutes"] / 60
            for t in tasks
            if uses_gpu(t["config"], task)
        ),
    }


def fits_budget(ctx, approved, tasks):
    if ctx.budget.check().exhausted:
        return False
    idea = parse_selected_md(ctx.archive.read_text(approved.plan.idea_ref))
    estimate, used = cost(tasks, approved.task), ctx.budget.used()
    for name in ("wall_hours", "gpu_hours"):
        limit = ctx.project.budget.get(name)
        bounds = [limit.hard] if limit and limit.hard is not None else []
        if idea.budget.get(name) is not None:
            bounds.append(float(idea.budget[name]))
        if any(not math.isfinite(b) or b < 0 for b in bounds):
            return False
        if (
            bounds
            and used.get(name, 0) + estimate[name]
            > min(bounds) * approved.plan.stopping_conditions.budget_fraction + 1e-12
        ):
            return False
    return True


def parse_trigger(text):
    """Compile the documented std/CI trigger; never eval model-supplied code."""
    if not text.strip():
        return None, None
    std = re.search(r"\bstd\s*>\s*(0?\.\d+|\d+(?:\.\d+)?)", text, re.I)
    difference = re.search(r"(?:mean\s+(?:difference|diff)|均值差)\s*<\s*(0?\.\d+|\d+(?:\.\d+)?)", text, re.I)
    overlap = re.search(r"overlap|重叠", text, re.I)
    if not std and not (difference and overlap):
        raise ValueError(
            "Replication trigger must state a std threshold or overlapping CI and mean-difference threshold"
        )
    return float(std[1]) if std else None, float(difference[1]) if difference and overlap else None


def checks(ctx, approved, aggregates):
    state, plan = ctx.scratch["experiment"], approved.plan
    entries = state["entries"]
    completed = sorted(
        e.id
        for e in plan.experiments
        if all(entries[t.task_key]["state"] == "done" for t in approved.tasks if t.experiment_id == e.id)
    )
    anomalies = []
    primary = parse_selected_md(ctx.archive.read_text(plan.idea_ref)).primary_metric().name
    std_limit, diff_limit = (
        parse_trigger(plan.replication_policy.trigger)
        if plan.replication_policy.max_extra_seeds_per_group
        else (None, None)
    )
    for agg in aggregates:
        if agg.aggregate_id == "all":
            continue
        if agg.comparison["metric"] == primary:
            ceiling = approved.task.sanity.get(primary, {}).get("max_expected")
            for row in agg.rows:
                if ceiling is not None and row.max > ceiling:
                    anomalies.append(
                        {
                            "type": "too_good",
                            "aggregate_id": agg.aggregate_id,
                            "group": row.group,
                            "metric": primary,
                            "stat": "max",
                            "value": row.max,
                        }
                    )
            baseline_ids = {r.record.run_id for r in runs.list_runs(ctx.root) if r.record.kind == "baseline"}
            baselines = [r for r in agg.rows if baseline_ids.intersection(r.run_ids)]
            gain = approved.task.sanity.get("max_gain_over_baseline")
            if gain is not None and baselines:
                reference = max(r.mean for r in baselines)
                for row in agg.rows:
                    if not baseline_ids.intersection(row.run_ids) and row.mean - reference > gain:
                        anomalies.append(
                            {
                                "type": "too_good",
                                "aggregate_id": agg.aggregate_id,
                                "group": row.group,
                                "metric": primary,
                                "stat": "mean",
                                "value": row.mean,
                            }
                        )
        unstable = std_limit is not None and any(r.std is not None and r.std > std_limit for r in agg.rows)
        if diff_limit is not None:
            for i, a in enumerate(agg.rows):
                for b in agg.rows[i + 1 :]:
                    if (
                        a.ci95
                        and b.ci95
                        and max(a.ci95[0], b.ci95[0]) <= min(a.ci95[1], b.ci95[1])
                        and abs(a.mean - b.mean) < diff_limit
                    ):
                        unstable = True
        if unstable:
            anomalies.append(
                {
                    "type": "unstable",
                    "aggregate_id": agg.aggregate_id,
                    "metric": agg.comparison["metric"],
                    "trigger": plan.replication_policy.trigger,
                }
            )
    views = [v for v in runs.list_runs(ctx.root) if v.record.plan_ref == approved.plan_ref]
    for e in plan.experiments:
        attempts = [v for v in views if v.record.experiment_id == e.id]
        if attempts and sum(v.status.state.value == "failed" for v in attempts) / len(attempts) > 0.3:
            anomalies.append({"type": "high_failure", "experiment_id": e.id})
    if any(a["type"] == "too_good" for a in anomalies):
        ctx.permissions.guard("exec", "local", actor="agent:experiment/diagnose")
        leakage, _ = inspect_inputs(approved.task, ctx.project.experiment.get("python"))
        for anomaly in anomalies:
            if anomaly["type"] == "too_good":
                anomaly["disposition"] = "Input/split diagnosis completed; independent replication is still required"
                anomaly["leakage_checks"] = [c.model_dump() for c in leakage]
    used = ctx.budget.used()
    budget_stop = ctx.budget.check().exhausted
    for name, limit in ctx.project.budget.items():
        if (
            limit.hard is not None
            and limit.hard > 0
            and used.get(name, 0) >= limit.hard * plan.stopping_conditions.budget_fraction
        ):
            budget_stop = True
    return {
        "completed_experiments": completed,
        "anomalies": anomalies,
        "budget_stop": budget_stop,
        "remaining": [
            k for k, e in entries.items() if not e["trial"] and e["state"] not in ("done", "skipped", "blocked")
        ],
    }


def validate_observations(ctx, report, aggregates):
    by_id = {a.aggregate_id: a for a in aggregates}
    accepted = []
    for observation in report.observations:
        try:
            if not observation.evidence:
                raise ValueError("Observation has no evidence")
            numbers = []
            for ref in observation.evidence:
                agg = by_id[ref["aggregate_id"]]
                stat = ref["stat"]
                if stat not in {
                    "mean",
                    "std",
                    "sem",
                    "n",
                    "min",
                    "max",
                    "ci95_low",
                    "ci95_high",
                    "diff_mean",
                    "p_value",
                }:
                    raise ValueError("Unsupported statistic")
                value = runs.resolve_value(
                    ctx.root,
                    ref["aggregate_id"],
                    ref.get("group", {}),
                    ref["metric"],
                    stat,
                    contrast=ref.get("contrast"),
                    aggregate=agg,
                )
                if not math.isfinite(value):
                    raise ValueError("Evidence statistic is unavailable")
                numbers.append(value)
                numbers.extend(float(v) for v in ref.get("group", {}).values() if type(v) in (int, float))
                if stat.startswith("ci95"):
                    numbers.append(95.0)
            for token, percent in re.findall(r"(?<![\w.])(-?\d+(?:\.\d+)?)(%)?", observation.text):
                places = len(token.partition(".")[2])
                value = float(token)
                candidates = [x * 100 if percent else x for x in numbers]
                if not any(abs(round(x, places) - value) <= 0.50000001 * 10 ** (-places) for x in candidates):
                    raise ValueError("Observation contains a number not supported by its evidence")
            accepted.append(observation)
        except (KeyError, ValueError, TypeError, AttributeError, LookupError, NotFound) as exc:
            ctx.events.append(
                "stage.decision", "Dropped unsupported observation: " + str(exc), actor="agent:experiment/analysis"
            )
    report.observations = accepted
    return report


def analyze(ctx, approved, aggregates, deterministic, feedback=""):
    idea = parse_selected_md(ctx.archive.read_text(approved.plan.idea_ref))
    tables = []
    for aggregate in aggregates:
        artifact_id = f"artifacts/aggregates/{aggregate.aggregate_id}.md"
        ref = ctx.archive.latest(artifact_id)
        if ref is None:
            raise ValueError(f"Archived Markdown aggregate is missing: {artifact_id}")
        tables.append({"artifact_id": artifact_id, "markdown": ctx.archive.read_text(ref)})
    context = {
        "plan": approved.plan.model_dump(mode="json"),
        "hypotheses": [h.model_dump() for h in idea.hypotheses],
        "aggregates": [a.model_dump(mode="json") for a in aggregates],
        "aggregate_tables": tables,
        "checks": deterministic,
        "previous_rounds": ctx.scratch["experiment"].get("analysis_history", []),
        "feedback": feedback,
    }
    reply = ctx.llm.complete(
        role="exp.analyst",
        prompt_id="experiment/analyze",
        tier="strong",
        max_retries=1,
        variables={
            "schema_json": dumps(AnalysisReport.model_json_schema()),
            "context_document": ctx.llm.wrap_untrusted(dumps(context), "analysis_inputs"),
        },
        response_schema=AnalysisReport,
    )
    report = validate_observations(ctx, reply.parsed, aggregates)
    ids = {h.id for h in idea.hypotheses}
    report.hypothesis_status = {h: report.hypothesis_status.get(h, "untested") for h in sorted(ids)}
    if not report.observations:
        report.hypothesis_status = {
            h: "untested" if status == "untested" else "inconclusive" for h, status in report.hypothesis_status.items()
        }
        for interpretation in report.interpretations:
            if interpretation.status == "supported_by_experiment":
                interpretation.status = "speculation"
    return report


def replication_tasks(ctx, approved, aggregates, deterministic):
    plan, state = approved.plan, ctx.scratch["experiment"]
    if state["round"] >= min(plan.stopping_conditions.max_rounds, ctx.project.limits.max_rounds):
        return []
    ids = {a["aggregate_id"] for a in deterministic["anomalies"] if a["type"] in ("unstable", "too_good")}
    experiment_ids = {e for c in plan.comparisons if c.id in ids for e in c.experiments}
    templates = {}

    def key(eid, config):
        return dumps([eid, {k: v for k, v in config.items() if k != "seed"}])

    for task in approved.tasks:
        if task.experiment_id in experiment_ids:
            templates[key(task.experiment_id, task.config)] = task
    planned = []
    # Use a common unused seed set across the compared configurations.
    unused = max((e["config"].get("seed", -1) for e in state["entries"].values()), default=-1) + 1
    remaining = []
    for group in templates:
        extra = {
            e["config"]["seed"]
            for e in state["entries"].values()
            if e.get("replication_round") and key(e["experiment_id"], e["config"]) == group
        }
        remaining.append(plan.replication_policy.max_extra_seeds_per_group - len(extra))
    count = min(remaining, default=0)
    if count <= 0 or unused + count > 4294967296:
        return []
    for template in templates.values():
        spec = next(e for e in plan.experiments if e.id == template.experiment_id)
        for seed in range(unused, unused + count):
            planned.append(
                {
                    "experiment_id": spec.id,
                    "kind": "replication",
                    "config": {**template.config, "seed": seed},
                    "eval_condition": template.eval_condition,
                    "depends_on": [],
                    "est_minutes": spec.est_minutes_per_run,
                }
            )
    if len(planned) + len(state["entries"]) > 256 or not fits_budget(ctx, approved, planned):
        return []
    return planned


def decide(ctx, approved, report, deterministic, aggregates):
    state, stop = ctx.scratch["experiment"], approved.plan.stopping_conditions
    previous = state.get("analysis_history", [])
    before = previous[-1] if previous else {}
    current_anomalies = sorted(
        {
            dumps({k: v for k, v in a.items() if k in ("type", "aggregate_id", "experiment_id", "disposition")})
            for a in deterministic["anomalies"]
        }
    )
    progress = bool(set(deterministic["completed_experiments"]) - set(before.get("completed_experiments", [])))
    progress |= bool(before.get("hypothesis_status") and report.hypothesis_status != before["hypothesis_status"])
    # A newly noticed anomaly alone is not effective progress. Resolving a
    # previously recorded anomaly, or confirming one through diagnosis, is.
    old_anomalies = set(before.get("anomalies", []))
    progress |= bool(old_anomalies - set(current_anomalies))
    progress |= any(a.get("status") in ("confirmed", "ruled_out") for a in deterministic["anomalies"])
    no_progress = 0 if progress else before.get("no_progress", 0) + 1
    complete_without_anomalies = (
        not deterministic["remaining"]
        and set(deterministic["completed_experiments"]) == {e.id for e in approved.plan.experiments}
        and not deterministic["anomalies"]
    )
    forced = (
        deterministic["budget_stop"]
        or state.get("budget_stop")
        or complete_without_anomalies
        or no_progress >= stop.no_progress_rounds
        or state["round"] >= min(stop.max_rounds, ctx.project.limits.max_rounds)
    )
    if (state.get("requested_revision") or state.get("plan_change_reason")) and not any(
        e["runs"] for e in state["entries"].values() if not e["trial"]
    ):
        # A rejected code proposal before execution is not an exhausted research round.
        forced = bool(deterministic["budget_stop"] or state.get("budget_stop"))
    decision = report.proposal.decision
    tasks = []
    if state.get("requested_revision") or state.get("plan_change_reason"):
        decision = "revise_plan"
    if forced:
        decision = "stop_and_write"
    elif decision == "replicate":
        tasks = replication_tasks(ctx, approved, aggregates, deterministic)
        if not tasks:
            decision = "revise_plan"
    elif decision == "run_next_planned" and not deterministic["remaining"]:
        decision = (
            "stop_and_write"
            if len(deterministic["completed_experiments"]) == len(approved.plan.experiments)
            else "revise_plan"
        )
    elif decision == "stop_and_write" and deterministic["remaining"]:
        decision = "run_next_planned"
    if decision in ("replicate", "diagnose") and (
        not report.proposal.distinguishes.strip() or not report.proposal.stop_condition.strip()
    ):
        decision, tasks = "revise_plan", []
    entry = {
        "round": state["round"],
        "hypothesis_status": report.hypothesis_status,
        "completed_experiments": deterministic["completed_experiments"],
        "anomalies": current_anomalies,
        "no_progress": no_progress,
        "effective_progress": progress,
    }
    return {
        "decision": decision,
        "tasks": tasks,
        "estimated_cost": cost(tasks, approved.task),
        "forced_stop": bool(forced),
        "progress": entry,
    }


def publish_log(ctx, approved, aggregates, report, deterministic, decision, feedback=""):
    simulation = ctx.scratch["experiment"]["simulation"]
    lines = [
        f"# Experiment round {round_number(ctx)}",
        "",
        "SIMULATED: preset FakeExecutor results; no model training or research evidence."
        if simulation
        else "Local experiment results; interpretations require review.",
        f"LLM backend: {ctx.project.experiment.get('llm_backend', 'fake')}.",
        "",
        "## Observations",
        "",
    ]
    for agg in aggregates:
        lines += ["\n".join("#" + line if line.startswith("#") else line for line in render_table(agg).splitlines())]
    lines += [o.text for o in report.observations]
    lines += ["", "## Interpretations", ""]
    if feedback:
        note = "See the archived approval feedback." if re.search(r"[\u3400-\u9fff]", feedback) else feedback
        lines += ["Review revision: " + note]
    for interpretation in report.interpretations:
        lines += [
            f"- [{interpretation.status}] {interpretation.text}",
            f"  Alternatives: {'; '.join(interpretation.alternatives) or 'None supplied.'}",
        ]
    lines += [
        "",
        "## Anomalies and disposition",
        "",
        "```json",
        dumps(deterministic["anomalies"]),
        "```",
        "",
        "## Hypothesis status",
        "",
        *[f"- {h}: {status}" for h, status in report.hypothesis_status.items()],
        "",
        "## Decision and rationale",
        "",
        f"Decision: `{decision['decision']}`.",
        "Execution decision SHA256: `" + sha256_bytes(dumps(decision).encode()) + "`.",
        (
            "All approved experiments are complete and no unresolved anomalies remain. "
            "Finish this plan and preserve its results regardless of the direction of the outcome."
            if decision["forced_stop"]
            and not deterministic["remaining"]
            and set(deterministic["completed_experiments"]) == {e.id for e in approved.plan.experiments}
            and not deterministic["anomalies"]
            else ""
        ),
        (
            f"Model proposal `{report.proposal.decision}` was not executed. Model rationale: "
            if report.proposal.decision != decision["decision"]
            else ""
        ) + report.proposal.rationale,
        f"Budget/round/no-progress override: {decision['forced_stop']}.",
        f"Distinguishes: {report.proposal.distinguishes}",
        f"Stop condition: {report.proposal.stop_condition}",
        "",
        "## Estimated cost",
        "",
        dumps(decision["estimated_cost"]),
        "",
        "## Affected experiments",
        "",
        ", ".join(e.id for e in approved.plan.experiments),
        "",
        "Source evidence: `artifacts/aggregates/*.json`, `runs/<run_id>/metrics.jsonl`, "
        "immutable code and environment snapshots.",
    ]
    parents = [
        approved.plan_ref,
        *[ctx.archive.latest(f"artifacts/aggregates/{a.aggregate_id}.json") for a in aggregates],
    ]
    return ctx.archive.put(
        log_id(ctx), "process_log", "\n".join(lines) + "\n", parents=parents, producer="agent:experiment/analysis"
    )
