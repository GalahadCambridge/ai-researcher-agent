"""Read pinned literature evidence and preserve uncertainty across downstream artifacts."""

from __future__ import annotations

import json

from airesearcher.core.fsutil import sha256_bytes
from airesearcher.core.models.literature import ReadingCard
from airesearcher.services.literature import load_papers

from .conflicts import conflict_refs, indexed_items
from .models import ReadingConflict, ReadingSummary


def load_json(ctx, path):
    return json.loads(ctx.archive.path_of(path).read_text(encoding="utf-8"))


def material(ctx):
    snapshot = load_json(ctx, "idea/merge_input.json")
    content = {k: v for k, v in snapshot.items() if k != "input_sha256"}
    digest = sha256_bytes(json.dumps(content, ensure_ascii=False, sort_keys=True).encode())
    if snapshot["input_sha256"] != digest:
        raise ValueError("The merge evidence snapshot has changed")
    summary = ReadingSummary.model_validate(load_json(ctx, "idea/reading_summary.json"))
    if load_json(ctx, "idea/reading_summary.json")["input_sha256"] != digest:
        raise ValueError("The reading summary belongs to different evidence")
    cards = [ReadingCard.model_validate(card) for card in snapshot["cards"]]
    refs = indexed_items(cards)
    papers = load_papers(ctx.root)
    if any(card.paper_id not in papers for card in cards):
        raise ValueError("Reading cards reference unregistered papers")
    weak = {ref for ref, (card, item) in refs.items()
            if card.based_on != "full_text" or item.kind != "author_claim" or item.quote_verified is not True}
    for value in load_json(ctx, "idea/conflicts.json")["conflicts"]:
        conflict = ReadingConflict.model_validate(value)
        evidence = set(conflict_refs(conflict))
        weak.update(evidence if conflict.resolution != "resolved_by_reread"
                    else evidence - set(conflict.accepted_evidence))
    return summary, papers, refs, weak


def check_evidence(evidence, papers, refs, weak, *, paper_id=None):
    aliases = {}
    for canonical, (card, _) in refs.items():
        alias = f"{card.paper_id}__{card.role}#{canonical.rsplit('#', 1)[1]}"
        aliases.setdefault(alias, set()).add(canonical)
    for i, ref in enumerate(evidence):
        if ref not in papers and ref not in refs and len(aliases.get(ref, set())) == 1:
            evidence[i] = next(iter(aliases[ref]))
    if len(set(evidence)) != len(evidence):
        raise ValueError("Evidence references must be unique")
    for ref in evidence:
        if ref not in papers and ref not in refs:
            raise ValueError(f"Unknown evidence reference: {ref}")
        if paper_id is not None and (ref if ref in papers else refs[ref][0].paper_id) != paper_id:
            raise ValueError("Gap-table evidence belongs to another paper")
    # A paper ID alone identifies a source, not a checked supporting passage.
    return not evidence or any(ref not in refs or ref in weak for ref in evidence)


def compact_material(ctx):
    summary, papers, refs, _ = material(ctx)
    return {
        "summary": summary.model_dump(mode="json"),
        "papers": [{"paper_id": p.paper_id, "title": p.title, "access_level": p.access_level}
                   for p in papers.values()],
        "items": [{"ref": ref, "paper_id": card.paper_id, "based_on": card.based_on,
                   **item.model_dump(mode="json")} for ref, (card, item) in refs.items()],
    }
