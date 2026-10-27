"""Resumable coordinator merging, bounded conflict review, and summary publication."""

from __future__ import annotations

import json
import re

from airesearcher.core.fsutil import sha256_bytes
from airesearcher.core.models.literature import ReadingCard
from airesearcher.engine.stage import Continue, Error
from airesearcher.services.literature import load_papers

from .attempts import SelectionNeedsDecision
from .conflicts import (
    add_coordinator_conflicts,
    conflict_refs,
    indexed_items,
    review_snippets,
    rule_conflicts,
    verified_acceptance,
)
from .coordinator import request_coordinator
from .models import Adjudication, CoordinatorOutput, ReadingConflict, ReadingSummary, ResearchGap
from .quotes import QuoteVerifier
from .render import render_reading_summary
from .storage import save_json

MAX_REVIEWS = 3


def _load(ctx, path):
    return json.loads(ctx.archive.path_of(path).read_text(encoding="utf-8"))


def _put(ctx, path, value, kind="idea"):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    target = ctx.root / path
    if ctx.archive.latest(path) and target.exists() and target.read_text(encoding="utf-8") == text:
        return
    previous = ctx.archive.latest(path)
    ctx.archive.put(path, kind, text, parents=[previous] if previous else [], producer="agent:idea/merge")


def _trace(out):
    return {"call_id": out.call_id, "model": out.model, "prompt_id": out.prompt_id,
            "prompt_version": out.prompt_version, "usage": out.usage}


def _snapshot(ctx):
    reading = _load(ctx, "idea/reading_input.json")
    report = _load(ctx, "idea/reading_status.json")
    gaps = _load(ctx, "idea/gaps.json")["gaps"]
    sources = {p["paper_id"]: p for p in reading["papers"]}
    papers = load_papers(ctx.root)
    if any(pid not in papers for pid in sources):
        raise ValueError("Reading sources must exist in the archived literature records")
    if len(sources) != len(reading["papers"]) or len(set(report["card_ids"])) != len(report["card_ids"]):
        raise ValueError("Duplicate paper or reading-card identities in merge input")
    for source in sources.values():
        if sha256_bytes(source["text"].encode()) != source["input_sha256"]:
            raise ValueError("Reading source hash does not match the frozen input")
    cards = []
    for cid in report["card_ids"]:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", cid):
            raise ValueError("Unsafe reading-card identifier")
        card = ReadingCard.model_validate(_load(ctx, f"idea/reading_cards/{cid}.json"))
        source = sources[card.paper_id]
        if card.card_id != cid or card.based_on != source["based_on"]:
            raise ValueError("Reading-card identity or source does not match the reading input")
        if not card.items:
            raise ValueError("Successful reading cards must contain items")
        checker = QuoteVerifier(source["text"])
        card.items = [checker.check(item, card.based_on) for item in card.items]
        cards.append(card)
    refs = indexed_items(cards)
    if len({g["gap_id"] for g in gaps}) != len(gaps):
        raise ValueError("Duplicate gap IDs in merge input")
    for gap in gaps:
        ResearchGap.model_validate(gap)
        if any(ref not in refs for ref in gap["evidence"]):
            raise ValueError("Gap evidence refers to a nonexistent reading-card item")
    snapshot = {"goal": reading["goal"], "sources": reading["papers"],
                "cards": [c.model_dump(mode="json") for c in cards], "gaps": gaps,
                "incomplete_reading": [t["card_id"] for t in report["failed"]]}
    digest = sha256_bytes(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode())
    return {**snapshot, "input_sha256": digest}


def _validate_output(output, refs):
    groups = [(f"themes[{i}].points[{j}]", point.evidence, False)
              for i, theme in enumerate(output.themes) for j, point in enumerate(theme.points)]
    groups += [(f"feasibility_notes[{i}]", point.evidence, False)
               for i, point in enumerate(output.feasibility_notes)]
    groups += [(f"conflicts[{i}]", conflict.evidence, False) for i, conflict in enumerate(output.conflicts)]
    for gap in output.gaps:
        # Unsupported candidates remain open questions, never established research gaps.
        if not gap.evidence:
            gap.status = "to_verify"
        groups.append((f"gaps[{gap.gap_id}]", gap.evidence, True))
    for location, item, allow_empty in groups:
        unknown = [ref for ref in item if ref not in refs]
        if (not item and not allow_empty) or len(set(item)) != len(item) or unknown:
            raise ValueError(f"{location}: evidence must cite distinct existing reading-card items; "
                             f"empty={not item}, duplicates={len(set(item)) != len(item)}, unknown={unknown}")
    evidence = [item for _, item, _ in groups]
    covered = {refs[ref][0].card_id for item in evidence for ref in item}
    if covered != {card.card_id for card, _ in refs.values()}:
        raise ValueError("Coordinator output must cover every successful reading card")
    if len({gap.gap_id for gap in output.gaps}) != len(output.gaps):
        raise ValueError("Coordinator output contains duplicate gap IDs")


def _coordinate(ctx, state, snapshot, refs, rules):
    if not refs:
        return {"input_sha256": snapshot["input_sha256"],
                "output": CoordinatorOutput(themes=[], feasibility_notes=[], gaps=[], conflicts=[]).model_dump(),
                "conflicts": [], "trace": {}}
    cards = [{"paper_id": card["paper_id"], "card_id": card["card_id"], "role": card["role"],
              "based_on": card["based_on"], "relevance_to_question": card["relevance_to_question"], "items": [
                  {"ref": f"{card['card_id']}#{i}", **item} for i, item in enumerate(card["items"])]}
             for card in snapshot["cards"]]
    result = request_coordinator(
        ctx, state, snapshot["input_sha256"],
        variables={"goal": snapshot["goal"], "schema_json": CoordinatorOutput.model_json_schema(),
                   "merge_block": ctx.llm.wrap_untrusted(json.dumps({
                       "cards": cards, "gap_candidates": snapshot["gaps"],
                       "rule_conflicts": [c.model_dump(mode="json") for c in rules],
                       "incomplete_reading": snapshot["incomplete_reading"],
                   }, ensure_ascii=False), source="merge_input")},
        validate=lambda output: _validate_output(output, refs),
    )
    if result is None:
        return None
    output = CoordinatorOutput.model_validate(result["output"])
    conflicts = add_coordinator_conflicts(output.conflicts, refs, rules)
    return {"input_sha256": snapshot["input_sha256"], **result,
            "conflicts": [c.model_dump(mode="json") for c in conflicts]}


def _review(ctx, conflict, snapshot, refs, receipts):
    snippets = review_snippets(conflict, refs, {p["paper_id"]: p for p in snapshot["sources"]})
    receipt = {"input_sha256": snapshot["input_sha256"], "conflict_id": conflict.conflict_id,
               "status": "skipped", "resolution": "needs_human", "accepted_evidence": [], "trace": {},
               "original_excerpts": snippets}
    path = f"idea/conflict_reviews/{conflict.conflict_id}.json"
    if len(snippets) != len(conflict.positions):
        receipt["note"] = "Original excerpts are insufficient for all positions; human review is required."
    elif sum(r["status"] != "skipped" for r in receipts) >= MAX_REVIEWS:
        receipt["note"] = "The three-review limit was reached; human review is required."
    else:
        receipt.update(status="started", note="A single strong-model reread was scheduled.")
        save_json(ctx, path, receipt)
        ctx.events.append("literature.conflict_review_started", "Reread requested for a literature conflict",
                          actor="agent:idea", data={"conflict_id": conflict.conflict_id,
                                                   "reason": conflict.note, "tier": "strong"})
        try:
            out = ctx.llm.complete(
                role="idea.adjudicator", prompt_id="idea/adjudicate_conflict", tier="strong", max_retries=0,
                response_schema=Adjudication,
                variables={"schema_json": Adjudication.model_json_schema(),
                           "conflict_block": ctx.llm.wrap_untrusted(
                               conflict.model_dump_json(), source="conflict"),
                           "source_block": ctx.llm.wrap_untrusted(
                               json.dumps(snippets, ensure_ascii=False), source="original_excerpts")},
            )
            receipt["trace"] = _trace(out)
            accepted = verified_acceptance(out.parsed, conflict, refs, snippets)
            receipt.update(status="done", resolution=out.parsed.resolution, accepted_evidence=accepted,
                           note=out.parsed.explanation, review=out.parsed.model_dump(mode="json"))
        except Exception as exc:
            receipt.update(status="failed",
                           note=f"Reread failed; human review is required. {type(exc).__name__}: {exc}")
        ctx.events.append("literature.conflict_reviewed", "Literature conflict reread recorded",
                          actor="agent:idea", data={"conflict_id": conflict.conflict_id,
                                                   "resolution": receipt["resolution"], **receipt["trace"]})
    save_json(ctx, path, receipt)
    return receipt


def _summary(snapshot, output, conflicts, refs):
    pending, rejected = set(), set()
    for conflict in conflicts:
        evidence = set(conflict_refs(conflict))
        if conflict.resolution != "resolved_by_reread":
            pending.update(evidence)
        else:
            rejected.update(evidence - set(conflict.accepted_evidence))
    weak = {ref for ref, (card, item) in refs.items()
            if card.based_on != "full_text" or item.kind != "author_claim" or item.quote_verified is not True}
    for point in [p for theme in output.themes for p in theme.points] + output.feasibility_notes:
        point.to_verify = point.to_verify or bool(set(point.evidence) & (weak | pending | rejected))
    # Preserve every upstream gap, including candidates omitted by the coordinator.
    gaps = {g["gap_id"]: ResearchGap.model_validate(g) for g in snapshot["gaps"]}
    for gap in output.gaps:
        old = gaps.get(gap.gap_id)
        if old:
            gap.evidence = list(dict.fromkeys(old.evidence + gap.evidence))
            if old.status == "to_verify":
                gap.status = "to_verify"
        gaps[gap.gap_id] = gap
    for gap in gaps.values():
        if not gap.evidence or set(gap.evidence) <= weak or set(gap.evidence) & (pending | rejected):
            gap.status = "to_verify"
    return ReadingSummary(
        themes=output.themes, gaps=list(gaps.values()), feasibility_notes=output.feasibility_notes,
        abstract_only_papers=sorted(p["paper_id"] for p in snapshot["sources"] if p["based_on"] == "abstract_only"),
        incomplete_reading=snapshot["incomplete_reading"],
        unresolved_conflicts=[c.conflict_id for c in conflicts if c.resolution != "resolved_by_reread"],
    )


def merge_step(ctx, state):
    input_path, coordinator_path = "idea/merge_input.json", "idea/merge_coordinator.json"
    try:
        if not ctx.archive.latest(input_path):
            save_json(ctx, input_path, _snapshot(ctx))
            return Continue("Merge inputs snapshotted; rule detection and coordinator synthesis come next")
        snapshot = _load(ctx, input_path)
        content = {key: value for key, value in snapshot.items() if key != "input_sha256"}
        if sha256_bytes(json.dumps(content, ensure_ascii=False, sort_keys=True).encode()) != snapshot["input_sha256"]:
            return Error("Merge input hash does not match the frozen snapshot", retryable=False)
        cards = [ReadingCard.model_validate(card) for card in snapshot["cards"]]
        refs = indexed_items(cards)
        if not ctx.archive.latest(coordinator_path):
            coordinated = _coordinate(ctx, state, snapshot, refs, rule_conflicts(refs))
            if coordinated is None:
                return Continue("Invalid coordinator response saved; one targeted repair remains")
            save_json(ctx, coordinator_path, coordinated)
            return Continue("Coordinator synthesis and conflict candidates saved")
        coordinated = _load(ctx, coordinator_path)
        if coordinated["input_sha256"] != snapshot["input_sha256"]:
            return Error("Coordinator receipt does not match the merge input", retryable=False)
        output = CoordinatorOutput.model_validate(coordinated["output"])
        _validate_output(output, refs)
        conflicts = [ReadingConflict.model_validate(c) for c in coordinated["conflicts"]]
        receipts = []
        for conflict in conflicts:
            path = f"idea/conflict_reviews/{conflict.conflict_id}.json"
            if ctx.archive.latest(path):
                receipt = _load(ctx, path)
                if (receipt["input_sha256"], receipt["conflict_id"]) != (
                        snapshot["input_sha256"], conflict.conflict_id):
                    return Error("Conflict-review receipt does not match the merge input", retryable=False)
                if receipt["status"] == "started":
                    receipt.update(status="interrupted", resolution="needs_human", accepted_evidence=[],
                                   note="Previous review was interrupted; it will not be repeated automatically.")
                    save_json(ctx, path, receipt)
            else:
                _review(ctx, conflict, snapshot, refs, receipts)
                return Continue(f"Conflict {conflict.conflict_id} processed; original cards remain unchanged")
            receipts.append(receipt)
            conflict.resolution, conflict.accepted_evidence = receipt["resolution"], receipt["accepted_evidence"]
            conflict.note += " Review: " + receipt["note"]
        summary = _summary(snapshot, output, conflicts, refs)
        _put(ctx, "idea/conflicts.json", {"conflicts": [c.model_dump(mode="json") for c in conflicts],
                                        "review_limit": MAX_REVIEWS,
                                        "review_attempts": sum(r["status"] != "skipped" for r in receipts)})
        _put(ctx, "idea/reading_summary.json", {**summary.model_dump(mode="json"),
                                              "input_sha256": snapshot["input_sha256"],
                                              "coordinator_trace": coordinated["trace"]})
        _put(ctx, "idea/reading_summary.md", render_reading_summary(summary, conflicts, refs), "literature")
        _put(ctx, "idea/merge_status.json", {
            "input_sha256": snapshot["input_sha256"], "cards": len(cards), "conflicts": len(conflicts),
            "resolved": sum(c.resolution == "resolved_by_reread" for c in conflicts),
            "needs_human": summary.unresolved_conflicts, "incomplete_reading": summary.incomplete_reading,
            "review_attempts": sum(r["status"] != "skipped" for r in receipts),
            "note": "Original reading cards are preserved. Rule matches and model reviews are not factual guarantees.",
        })
    except SelectionNeedsDecision as exc:
        return exc.stop
    except (ValueError, KeyError, TypeError, OSError) as exc:
        return Error(f"Merge input or output validation failed: {exc}", retryable=False)
    state["phase"] = "refine"
    return Continue("Reading summary and conflict ledger saved; research-question refinement comes next")
