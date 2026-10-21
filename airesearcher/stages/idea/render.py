"""Render evidence-linked reading summaries without generating research claims."""

import html
import re


def _inline(text):
    text = html.escape(re.sub(r"\s+", " ", str(text)).strip(), quote=False)
    for char in ("\\", chr(96), "*", "_", "[", "]"):
        text = text.replace(char, "\\" + char)
    return text


def render_reading_summary(summary, conflicts, refs):
    lines = ["# Literature Reading Summary", "",
             "Draft synthesis within the retrieval scope; this does not establish novelty.",
             "Unverified statements and unresolved conflicts require further review.", ""]

    def evidence_links(evidence):
        return "; ".join(f"[{_inline(refs[ref][0].paper_id)}] \u2014 \u0060{ref}\u0060" for ref in evidence)

    def point_line(point):
        pending = " **[TO VERIFY]**" if point.to_verify else ""
        return f"- {_inline(point.text)}{pending} {evidence_links(point.evidence)}"

    lines.extend(["## Themes", ""])
    for theme in summary.themes:
        lines.extend([f"### {_inline(theme.theme)}", ""])
        lines.extend(point_line(point) for point in theme.points)
        lines.append("")
    if not summary.themes:
        lines.extend(["No evidence-backed themes were generated.", ""])
    lines.extend(["## Feasibility", ""])
    lines.extend(point_line(point) for point in summary.feasibility_notes)
    if not summary.feasibility_notes:
        lines.append("No feasibility notes were generated.")
    lines.extend(["", "## Gap Candidates", ""])
    for gap in summary.gaps:
        lines.append(f"- **{gap.gap_id} [{gap.status}]** {_inline(gap.description)} "
                     + evidence_links(gap.evidence))
    if not summary.gaps:
        lines.append("No gap candidates were recorded; this is not evidence that no gaps exist.")
    lines.extend(["", "## Conflicts", ""])
    for conflict in conflicts:
        lines.append(f"- **{conflict.conflict_id}: {conflict.resolution}** {_inline(conflict.note)}")
        for position in conflict.positions:
            ref = f"{position['card_id']}#{position['item_index']}"
            lines.append(f"  - {_inline(position['content'])} ({_inline(position['source_loc'])}) "
                         + evidence_links([ref]))
        if conflict.accepted_evidence:
            lines.append("  - Accepted by reread: " + evidence_links(conflict.accepted_evidence))
    if not conflicts:
        lines.append("No conflict candidates were detected; this does not prove agreement or accuracy.")
    lines.extend(["", "## Abstract-Only Papers", ""])
    lines.extend(f"- {_inline(paper)}" for paper in summary.abstract_only_papers)
    if not summary.abstract_only_papers:
        lines.append("None.")
    lines.extend(["", "## Incomplete Reading Tasks", ""])
    lines.extend(f"- {_inline(task)}" for task in summary.incomplete_reading)
    if not summary.incomplete_reading:
        lines.append("None.")
    return "\n".join(lines) + "\n"


def render_gap_table(output, papers):
    columns = ("task_data", "method", "setting", "compute", "evaluation", "difference")
    lines = ["# Literature Comparison", "",
             "Comparison within the retrieval scope; proposed differences are not proof of novelty.", "",
             "| Work | Task / Data | Method | Setting | Compute | Evaluation | Proposed Difference |",
             "|---|---|---|---|---|---|---|"]
    for row in output.rows:
        cells = [f"[{_inline(row.paper_id)}] {_inline(papers[row.paper_id].title)}".replace("|", "&#124;")]
        for name in columns:
            cell = row.cells[name]
            text = _inline(cell.text).replace("|", "&#124;")
            if cell.to_verify:
                text += " **[TO VERIFY]**"
            text += " " + "; ".join(_inline(ref) for ref in cell.evidence)
            cells.append(text)
        lines.append("| " + " | ".join(cells) + " |")
    if not output.rows:
        lines.extend(["", "No registered citations are available; all comparisons remain unverified."])
    return "\n".join(lines) + "\n"
