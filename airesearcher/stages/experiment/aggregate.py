"""Deterministic aggregates in the frozen §8.2 schema."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from airesearcher.core.models.aggregate import Aggregate
from airesearcher.core.models.plan import Comparison
from airesearcher.services import runs


def english(value, fallback="See the original versioned source for its wording."):
    text = str(value)
    return fallback if re.search(r"[\u3400-\u9fff]", text) else text


def render_table(aggregate):
    keys = sorted({k for row in aggregate.rows for k in row.group})
    lines = [
        f"## {aggregate.aggregate_id}: {english(aggregate.comparison.get('question', ''))}",
        "",
        f"Metric: `{aggregate.comparison['metric']}`. Evaluation condition: `{aggregate.eval_condition}`.",
        "",
        "| " + " | ".join([*keys, "n", "mean", "sample std", "95% t CI", "min", "max"]) + " |",
        "|" + "---|" * (len(keys) + 6),
    ]
    for row in aggregate.rows:
        ci = f"[{row.ci95[0]:.6f}, {row.ci95[1]:.6f}]" if row.ci95 else "not estimable"
        std = f"{row.std:.6f}" if row.std is not None else "not estimable"
        values = [
            *(str(row.group.get(k)) for k in keys),
            str(row.n),
            f"{row.mean:.6f}",
            std,
            ci,
            f"{row.min:.6f}",
            f"{row.max:.6f}",
        ]
        lines.append("| " + " | ".join(values) + " |")
    if aggregate.contrasts:
        lines += ["", "| Contrast (a minus b) | Mean difference | Welch t | Two-sided p |", "|---|---|---|---|"]
        for c in aggregate.contrasts:
            lines.append(
                f"| `{json.dumps(c.a, sort_keys=True)}` vs `{json.dumps(c.b, sort_keys=True)}` "
                f"| {c.diff_mean:+.6f} | {c.welch_t} | {c.p_value} |"
            )
    lines += [
        "",
        "Outliers are flagged using 3 MAD and are not automatically removed.",
        "With fewer than two seeds, standard deviation and confidence intervals are not estimable.",
        "Small-sample intervals and unadjusted Welch tests are descriptive; they do not establish causality.",
        "",
        "### Excluded runs",
        "",
    ]
    lines += [
        f"- `{row['run_id']}`: {english(row['reason'], 'Excluded; consult the source run or label.')}"
        for row in aggregate.excluded
    ] or ["None."]
    lines += [
        "",
        "### Cost and failures (includes trial and failed attempts)",
        "",
        "```json",
        json.dumps(aggregate.cost, indent=2),
        "```",
    ]
    return "\n".join(lines) + "\n"


def publish_results(ctx, approved):
    plan = approved.plan
    conditions = {e.eval_condition for e in plan.experiments}
    # Never silently mix exploratory and final evidence in one comparison.
    specs = []
    for c in plan.comparisons:
        selected = {e.eval_condition for e in plan.experiments if e.id in c.experiments}
        if len(selected) != 1:
            raise ValueError(f"{c.id}: comparison mixes evaluation conditions")
        specs.append((c, next(iter(selected))))
    # 'all' is a final-only summary when the plan contains both conditions.
    condition = "final" if "final" in conditions else "explore"
    specs.append(
        (
            Comparison(
                id="all",
                question="All experiments",
                experiments=[e.id for e in plan.experiments],
                group_by=[
                    "experiment_id",
                    "method",
                    "eval_split",
                    *sorted({k for e in plan.experiments for k in e.variables}),
                ],
                metric=plan.comparisons[0].metric,
            ),
            condition,
        )
    )
    if len({c.id for c, _ in specs}) != len(specs):
        raise ValueError("Comparison IDs must be unique and cannot use reserved 'all'")
    records = runs.list_runs(ctx.root)
    if any(r.status.state in runs.ACTIVE for r in records):
        raise ValueError("Collect active runs before aggregation")
    inputs = []
    for view in records:
        ref = ctx.archive.latest(f"runs/{view.record.run_id}")
        if ref:
            if ctx.archive.current_sha(ref.artifact_id) != ref.sha256:
                raise ValueError(f"Archived source run was modified: {view.record.run_id}")
            inputs.append(ref)
    parents = [approved.plan_ref, *inputs]
    source = Path(__file__)
    blob = subprocess.run(["git", "hash-object", str(source)], capture_output=True, text=True, timeout=5)
    out = []
    for comp, condition in specs:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,80}", comp.id):
            raise ValueError("Unsafe comparison ID")
        agg = runs.compute_aggregate(ctx.root, plan, approved.plan_ref, comp, condition)
        agg.script["path"] = "airesearcher/stages/experiment/aggregate.py"
        agg.script["git_blob"] = blob.stdout.strip() if blob.returncode == 0 else None
        for excluded in agg.excluded:
            excluded["reason"] = (
                excluded["reason"]
                .replace("属于旧计划版本", "Older plan version")
                .replace("人工标注为无效：", "Manually invalidated: ")
            )
        aid = f"artifacts/aggregates/{comp.id}"
        previous = ctx.archive.latest(aid + ".json")
        if previous:
            old = Aggregate.model_validate_json(ctx.archive.read_text(previous))
            if old.model_dump(exclude={"created_at"}) == agg.model_dump(exclude={"created_at"}):
                agg.created_at = old.created_at
        ref = ctx.archive.put(
            aid + ".json",
            "aggregate",
            agg.model_dump_json(indent=2) + "\n",
            parents=parents,
            producer="agent:experiment/aggregate",
        )
        ctx.archive.put(
            aid + ".csv", "aggregate", runs.aggregate_csv(agg), parents=[ref], producer="agent:experiment/aggregate"
        )
        simulated = any(v.record.executor == "fake" for v in records)
        header = "SIMULATED: preset values, not research evidence.\n\n" if simulated else ""
        ctx.archive.put(
            aid + ".md", "aggregate", header + render_table(agg), parents=[ref], producer="agent:experiment/aggregate"
        )
        out.append(agg)
    return out
