"""Batch relevance scoring and reading selection using the frozen literature handoff schemas."""

from __future__ import annotations

import hashlib
import json

from airesearcher.core.models.literature import PaperRecord
from airesearcher.core.models.project import MAX_LITERATURE_PAPERS
from airesearcher.engine.stage import Continue, Error
from airesearcher.services.literature import load_papers

from .models import ScreenBatch
from .storage import append_papers, save_json

BATCH_SIZE = 10


def select_reading(papers, scores: dict, plan: dict, limit: int):
    eligible = [p for p in papers if (p.abstract and p.abstract.strip()) or
                (p.access_level == "full_text" and p.fulltext_path)]
    ranked = sorted(eligible, key=lambda p: -scores[p.paper_id].score)
    groups = plan["subquestions"]

    def membership(p):
        return {i for i, group in enumerate(groups) if set(p.queries) & set(group["queries"])}

    selected = ranked[:limit]
    covered = set().union(*(membership(p) for p in selected))
    adjusted = False
    # Design 6.2: when all top papers come from one subquestion, reserve space for other subquestions.
    if selected and len(covered) == 1:
        chosen, coverage = [], set()
        for i in range(len(groups)):
            if i in coverage:
                continue
            candidate = next((p for p in ranked if i in membership(p) and p not in chosen), None)
            if candidate is not None and len(chosen) < limit:
                chosen.append(candidate)
                coverage.update(membership(candidate))
        for paper in ranked:
            if paper not in chosen and len(chosen) < limit:
                chosen.append(paper)
        chosen_ids = {p.paper_id for p in chosen}
        selected = [p for p in ranked if p.paper_id in chosen_ids]
        adjusted = {p.paper_id for p in selected} != {p.paper_id for p in ranked[:limit]}
    covered = set().union(*(membership(p) for p in selected))
    missing = [group["question"] for i, group in enumerate(groups) if i not in covered]
    return selected, missing, adjusted


def screen_step(ctx, state):
    limit = ctx.project.limits.read_papers
    if type(limit) is not int or not 0 <= limit <= MAX_LITERATURE_PAPERS:
        return Error("limits.read_papers must be an integer between 0 and 20", retryable=False)
    if len(load_papers(ctx.root)) > MAX_LITERATURE_PAPERS:
        return Error("Existing literature exceeds the cumulative 20-paper limit", retryable=False)
    manifest_path = "idea/screen_input.json"
    if not ctx.archive.latest(manifest_path):
        data = {
            "brief": json.loads((ctx.root / "idea/brief.json").read_text(encoding="utf-8")),
            "plan": json.loads((ctx.root / "idea/queries.json").read_text(encoding="utf-8")),
            "papers": [p.model_dump(mode="json") for p in load_papers(ctx.root).values()],
        }
        save_json(ctx, manifest_path, data)
    text = (ctx.root / manifest_path).read_text(encoding="utf-8")
    data = json.loads(text)
    papers = [PaperRecord.model_validate(p) for p in data["papers"]]
    if len(papers) > MAX_LITERATURE_PAPERS:
        return Error("Frozen screening input exceeds the 20-paper limit", retryable=False)
    run_id = hashlib.sha256(text.encode()).hexdigest()[:20]
    scores = {}
    for start in range(0, len(papers), BATCH_SIZE):
        batch = papers[start:start + BATCH_SIZE]
        path = f"idea/screen_batches/{run_id}-{start:04d}.json"
        if not ctx.archive.latest(path):
            blocks = []
            for paper in batch:
                content = json.dumps({"paper_id": paper.paper_id, "title": paper.title,
                                      "abstract": paper.abstract}, ensure_ascii=False)
                blocks.append(ctx.llm.wrap_untrusted(content, source=paper.paper_id))
            out = ctx.llm.complete(
                role="idea.screen", prompt_id="idea/screen", tier="fast",
                variables={"brief": json.dumps(data["brief"], ensure_ascii=False),
                           "expected_ids": [p.paper_id for p in batch], "paper_blocks": "\n".join(blocks),
                           "schema_json": ScreenBatch.model_json_schema()}, response_schema=ScreenBatch,
            )
            result = out.parsed
            ids = [p.paper_id for p in result.papers]
            if len(ids) != len(set(ids)) or set(ids) != {p.paper_id for p in batch}:
                return Error((
                    "Screening must score each paper in the current batch exactly once, without "
                    "duplicate, missing, or extra IDs"
                ), retryable=True)
            save_json(ctx, path, {"result": result.model_dump(mode="json"), "model": out.model,
                                  "prompt_id": out.prompt_id, "prompt_version": out.prompt_version,
                                  "call_id": out.call_id})
            state["screened_papers"] = start + len(batch)
            return Continue(f"Scored {start + len(batch)}/{len(papers)} papers; screening batch saved")
        receipt = json.loads((ctx.root / path).read_text(encoding="utf-8"))
        result = ScreenBatch.model_validate(receipt["result"])
        ids = [p.paper_id for p in result.papers]
        if len(ids) != len(set(ids)) or set(ids) != {p.paper_id for p in batch}:
            return Error("Saved screening batches do not match the input papers", retryable=False)
        scores.update({p.paper_id: p for p in result.papers})
    selected, missing, adjusted = select_reading(papers, scores, data["plan"], limit)
    selected_ids = {p.paper_id for p in selected}
    current = load_papers(ctx.root)
    updates = []
    for paper in papers:
        record = current.get(paper.paper_id, paper).model_copy(deep=True)
        score = scores[paper.paper_id]
        record.relevance = {"score": score.score, "reason": score.reason}
        record.selected_for_reading = record.paper_id in selected_ids
        updates.append(record)
    append_papers(ctx, updates)
    metadata_only = [p.paper_id for p in papers if not ((p.abstract and p.abstract.strip()) or
                     (p.access_level == "full_text" and p.fulltext_path))]
    report = {
        "candidate_count": len(papers), "read_limit": limit,
        "selected_ids": [p.paper_id for p in selected], "selected_count": len(selected),
        "coverage_adjusted": adjusted, "uncovered_subquestions": missing,
        "excluded_without_readable_text": metadata_only,
        "low_relevance_selected": [p.paper_id for p in selected if scores[p.paper_id].score <= 2],
        "note": "Relevance is assessed using titles and abstracts only; scores do not verify paper conclusions.",
    }
    save_json(ctx, "idea/screening.json", report)
    state.update(phase="fulltext", screened_papers=len(papers), selected_count=len(selected))
    return Continue(
        f"Screening complete: {len(papers)} candidates, {len(selected)} selected for "
        f"reading; full-text retrieval and reading have not started"
    )
