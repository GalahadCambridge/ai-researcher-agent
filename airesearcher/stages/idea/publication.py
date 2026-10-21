"""Publish a validated English idea document and submit its exact version for human approval."""

from __future__ import annotations

from airesearcher.core.models.frontmatter import render_front_matter
from airesearcher.core.models.idea import IdeaScores, SelectedIdea, parse_selected_md
from airesearcher.engine.stage import Continue, Error, NeedsApproval
from airesearcher.services.literature import load_papers

from .evidence import check_evidence, load_json, material
from .render import _inline
from .selection import DIMENSIONS, normalize_idea, task_config
from .storage import save_json

DEPENDENCIES = (
    "idea/selected.json", "idea/scores.json", "idea/gap_table.json", "idea/gap_table.md",
    "idea/reading_summary.json", "idea/conflicts.json", "idea/literature.jsonl",
)


def render_selected(idea, papers, revision_note=""):
    lines = [f"# {_inline(idea.title)}", "",
             "Draft for human approval. Claims and task readiness require the checks listed below.", "",
             "## Research Question", "", _inline(idea.research_question), "",
             "## Motivation", "", _inline(idea.motivation), "", "## Hypotheses", ""]
    lines += [f"- **{h.id}**: {_inline(h.statement)} Falsified if: {_inline(h.falsified_if)}"
              for h in idea.hypotheses]
    lines += ["", "## Contributions", ""] + [f"- {_inline(c)}" for c in idea.contributions]
    lines += ["", "## Task and Data", "", f"- Task configuration: {_inline(idea.task['task_config'])}",
              f"- Domain: {_inline(idea.task['domain'])}", _inline(idea.task["description"])]
    lines += [f"- {_inline(d.name)}: {_inline(d.source)}; license: {_inline(d.license)}; {_inline(d.usage)}"
              for d in idea.data]
    if not idea.data:
        lines += ["No dataset is registered. Training readiness remains unverified."]
    lines += ["", "## Baselines and Methods", ""]
    for item in [*idea.baselines, *idea.methods]:
        description = item.why if hasattr(item, "why") else item.description
        lines.append(f"- **{item.id} {_inline(item.name)}**: {_inline(description)} "
                     + " ".join(f"[{_inline(pid)}]" for pid in item.citations))
    lines += ["", "## Evaluation Metrics", ""]
    lines += [f"- {_inline(m.name)}: {m.direction} is better" + ("; primary metric." if m.primary else ".")
              for m in idea.metrics]
    lines += ["", "## Budget and Stopping Conditions", ""]
    lines += [f"- {_inline(k)}: {v}" for k, v in idea.budget.items()]
    lines += [f"- {_inline(s)}" for s in idea.stopping_conditions]
    lines += ["", "## Expected Difficulties", ""] + [f"- {_inline(d)}" for d in idea.expected_difficulties]
    lines += ["", "## Open Questions", ""] + [f"- {_inline(q)}" for q in idea.open_questions]
    if not idea.open_questions:
        lines.append("None recorded; this does not guarantee scientific validity.")
    lines += ["", "## Scores and Comparison", "",
              "[Evidence-linked scores](scores.json); higher risk scores mean greater risk.",
              "[Literature comparison](gap_table.md).", "", "## References", ""]
    for pid in idea.citations:
        p = papers[pid]
        lines.append(f"- [{_inline(pid)}] {_inline('; '.join(p.authors))}. {p.year}. "
                     f"{_inline(p.title)}. {_inline(p.url)}")
    if not idea.citations:
        lines.append("No registered references; literature support remains unverified.")
    if revision_note:
        lines += ["", "## Revision Note", "", _inline(revision_note)]
    lines += ["", "## Approval Status", "", "Pending human review; no approval timestamp has been assigned."]
    return render_front_matter(idea.model_dump(mode="json"), "\n".join(lines) + "\n")


def validate_selected(ctx):
    idea = SelectedIdea.model_validate(load_json(ctx, "idea/selected.json"))
    normalized = normalize_idea(ctx, idea, task_config(ctx),
                                load_json(ctx, "idea/refinement_status.json")["task_fit"])
    if normalized != idea:
        raise ValueError("The selected idea changed after validation; regenerate its scores and comparison")
    from .models import GapTableOutput, ScoreOutput
    summary, papers, refs, weak = material(ctx)
    checked_scores = ScoreOutput.model_validate(load_json(ctx, "idea/scores.json"))
    for name in DIMENSIONS:
        dimension = getattr(checked_scores, name)
        if check_evidence(dimension.evidence, papers, refs, weak) and not dimension.to_verify:
            raise ValueError("Weak score evidence must remain marked for verification")
    table = GapTableOutput.model_validate(load_json(ctx, "idea/gap_table.json"))
    if {row.paper_id for row in table.rows} != set(idea.citations) or len(table.rows) != len(idea.citations):
        raise ValueError("The comparison does not cover the selected references")
    for row in table.rows:
        if set(row.cells) != {"task_data", "method", "setting", "compute", "evaluation", "difference"}:
            raise ValueError("The comparison must retain all required columns")
        for cell in row.cells.values():
            if check_evidence(cell.evidence, papers, refs, weak, paper_id=row.paper_id) and not cell.to_verify:
                raise ValueError("Weak comparison evidence must remain marked for verification")
    for path in DEPENDENCIES:
        if not ctx.archive.latest(path):
            raise ValueError(f"Missing required artifact: {path}")
        if ctx.archive.latest(path).sha256 != ctx.archive.current_sha(path):
            raise ValueError(f"Unregistered edits must be reviewed before publication: {path}")
    return idea


def write_step(ctx, state):
    try:
        idea = validate_selected(ctx)
        papers = load_papers(ctx.root)
        text = render_selected(idea, papers, state.get("revision_note", ""))
        if parse_selected_md(text) != idea:
            raise ValueError("The rendered document does not round-trip to the selected idea")
        parents = [ctx.archive.latest(path) for path in DEPENDENCIES]
        previous = ctx.archive.latest("idea/selected.md")
        if previous:
            parents.append(previous)
        ref = ctx.archive.put("idea/selected.md", "idea", text, parents=parents,
                              producer="agent:idea/writer", note="Validated draft awaiting human approval")
        save_json(ctx, "idea/validation_report.json", {
            "selected": ref.model_dump(mode="json"), "checks": ["task_constraints", "citation_registry",
                "required_artifacts", "archive_hashes", "frontmatter_roundtrip"],
            "dependencies": [r.model_dump(mode="json") for r in parents if r.artifact_id != "idea/selected.md"],
            "open_questions": idea.open_questions,
            "scientific_validity": "Not established by structural checks; inspect evidence and task readiness.",
        })
    except (ValueError, KeyError, OSError) as exc:
        return Error(f"Final idea validation failed: {exc}", retryable=False)
    state["phase"] = "submit"
    return Continue("English selected.md saved and validated; human approval comes next")


def submit_step(ctx, state):
    try:
        idea = validate_selected(ctx)
        ref = ctx.archive.latest("idea/selected.md")
        report = load_json(ctx, "idea/validation_report.json")
        if not ref or report["selected"] != ref.model_dump(mode="json"):
            raise ValueError("The validation report does not identify the current selected document")
        if ctx.archive.current_sha("idea/selected.md") != ref.sha256:
            raise ValueError("The selected document has unregistered edits")
        for expected in report["dependencies"]:
            if ctx.archive.latest(expected["artifact_id"]).model_dump(mode="json") != expected:
                raise ValueError("A supporting artifact changed after document validation")
        if parse_selected_md(ctx.archive.path_of("idea/selected.md").read_text(encoding="utf-8")) != idea:
            raise ValueError("The current document does not match the selected JSON")
        summary, _, _, _ = material(ctx)
        scores = IdeaScores.model_validate(load_json(ctx, "idea/scores.json"))
        pending_scores = [name for name in DIMENSIONS if getattr(scores, name).to_verify]
        notes = [f"{idea.title}: {len(idea.hypotheses)} hypotheses, {len(idea.citations)} registered references.",
                 "Scores requiring verification: " + (", ".join(pending_scores) or "none recorded"),
                 "Unresolved conflicts: " + (", ".join(summary.unresolved_conflicts) or "none recorded")]
        notes += ["Open question: " + q for q in idea.open_questions]
        extras = [ctx.archive.latest(path) for path in DEPENDENCIES if path != "idea/selected.json"]
        extras += [ctx.archive.latest("idea/validation_report.json")]
        return NeedsApproval(
            target=ref, kind="idea", summary="\n".join(notes),
            impact=("The approved experiment plan must be reassessed after this idea changes."
                    if "plan" in ctx.approved else "Initial idea approval; execution needs an approved plan."),
            extra_refs=[r for r in extras if r],
        )
    except (ValueError, KeyError, OSError) as exc:
        return Error(f"Approval submission validation failed: {exc}", retryable=False)
