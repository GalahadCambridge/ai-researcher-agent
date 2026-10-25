"""Render the frozen ExperimentPlan contract (§4.2)."""

import json

from airesearcher.core.models.frontmatter import render_front_matter
from airesearcher.core.models.plan import ExperimentPlan, PlanTask, PrecheckItem


def render_plan(
    plan: ExperimentPlan, tasks: list[PlanTask], checks: list[PrecheckItem], note: str, *, llm_backend: str = "fake"
) -> str:
    lines = [
        "# Experiment Plan (FakeLLM Development Validation)" if llm_backend == "fake" else "# Experiment Plan",
        "",
        "Model replies are preset fixtures, not independent research judgments. No training is performed here."
        if llm_backend == "fake"
        else "LLM-generated draft checked against the approved idea and task. No training is performed here.",
        "",
        "## Purpose and hypotheses",
        f"Approved idea: `{plan.idea_ref.short()}`; exact hash is in the front matter.",
        "",
        "## Experiment matrix",
        "| ID | Type | Method | Hypotheses | Variables | Seeds | Evaluation |",
        "|---|---|---|---|---|---|---|",
    ]
    for e in plan.experiments:
        lines.append(
            f"| {e.id} | {e.kind} | {e.method} | {e.hypotheses} | {e.variables} | {e.seeds} | {e.eval_condition} |"
        )
    lines += [
        "",
        "## Variables and controls",
        f"The code expands this matrix into {len(tasks)} runs using schema defaults.",
    ]
    lines += [
        f"- {e.id}: {e.purpose}; fixed settings: `{json.dumps(e.fixed, ensure_ascii=False)}`." for e in plan.experiments
    ]
    lines += ["", "## Comparisons and statistics"]
    lines += [
        f"- {c.id}: {c.question}; experiments {c.experiments}, groups {c.group_by}, metric {c.metric}, "
        f"filter {c.filter}."
        for c in plan.comparisons
    ]
    lines += [
        "- Deterministic result aggregation will compute means, sample standard deviations, and intervals.",
        "",
        "## Resource estimate",
        f"`{plan.budget_estimate}` (includes permitted extra seeds; not measured cost).",
        "",
        "## Stopping conditions",
        f"`{plan.stopping_conditions.model_dump()}`",
        "",
        "## Replication policy",
        f"Trigger: {plan.replication_policy.trigger or 'Not specified; no automatic replication.'}",
        f"Extra seed allowance per group: `{plan.replication_policy.max_extra_seeds_per_group}`.",
        "Replicate matched comparison groups only, using unused seeds, unchanged configurations and code.",
        "A round review must be approved before additional execution; budget and round limits are rechecked.",
        "",
        "## Pre-execution checks",
        *[f"- {c.check}: **{c.status}** - {c.detail}" for c in checks],
        "",
        "## Approval points",
        *[f"- {p}" for p in plan.approval_points],
        "",
        "## Revision note",
        note or "Initial version.",
    ]
    return render_front_matter(plan.model_dump(mode="json"), "\n".join(lines) + "\n")
