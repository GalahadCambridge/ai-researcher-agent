"""Detect conflict candidates and build bounded excerpts for evidence-based review."""

from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal

from .models import ConflictCandidate, ReadingConflict
from .quotes import QuoteVerifier, tokens


def indexed_items(cards):
    return {f"{card.card_id}#{i}": (card, item)
            for card in cards for i, item in enumerate(card.items)}


def _position(ref, refs):
    card, item = refs[ref]
    return {"card_id": card.card_id, "item_index": int(ref.rsplit("#", 1)[1]),
            "paper_id": card.paper_id, **item.model_dump(mode="json"), "based_on": card.based_on}


def conflict_refs(conflict):
    return [f"{p['card_id']}#{p['item_index']}" for p in conflict.positions]


def _different(field, left, right):
    if tokens(left) == tokens(right):
        return False
    if field == "dataset":
        def names(text):
            return set(re.findall(r"\b[A-Z][A-Z0-9]+(?:[-_][A-Za-z0-9]+)*\b", text))
        return bool(names(left) and names(right) and names(left) != names(right))
    if field in {"setting", "compute", "main_result", "metric"}:
        def numbers(text):
            return {Decimal(n) for n in re.findall(r"(?<!\w)\d+(?:\.\d+)?", text)}
        return bool(numbers(left) and numbers(right) and numbers(left) != numbers(right))
    if field in {"code_available", "data_available"}:
        def available(text):
            if re.search(r"\b(no|unavailable|not available)\b", text, re.I):
                return False
            return True if re.search(r"\b(yes|available)\b|https?://", text, re.I) else None
        a, b = available(left), available(right)
        return a is not None and b is not None and a != b
    return False


def rule_conflicts(refs):
    groups = defaultdict(list)
    for ref, (card, item) in refs.items():
        if item.content.strip().casefold().startswith(("not mentioned", "not analyzed", "unknown")):
            continue
        groups[card.paper_id, item.field].append(ref)
    result = []
    for (paper_id, field), evidence in sorted(groups.items()):
        evidence.sort()
        if not any(refs[a][0].card_id != refs[b][0].card_id
                   and _different(field, refs[a][1].content, refs[b][1].content)
                   for i, a in enumerate(evidence) for b in evidence[i + 1:]):
            continue
        result.append(ReadingConflict(
            conflict_id=f"K{len(result) + 1}", paper_id=paper_id, field=field,
            positions=[_position(ref, refs) for ref in evidence], detected_by="rule",
            note="Heuristic candidate: values or named datasets differ. Context may explain the difference.",
        ))
    return result


def add_coordinator_conflicts(candidates: list[ConflictCandidate], refs, existing):
    conflicts = list(existing)
    seen = {frozenset(conflict_refs(conflict)) for conflict in conflicts}
    for candidate in candidates:
        evidence = candidate.evidence
        if len(set(evidence)) != len(evidence) or any(ref not in refs for ref in evidence):
            raise ValueError("Conflict candidates must cite distinct existing reading-card items")
        key = frozenset(evidence)
        if key in seen:
            continue
        seen.add(key)
        papers = {refs[ref][0].paper_id for ref in evidence}
        conflicts.append(ReadingConflict(
            conflict_id=f"K{len(conflicts) + 1}", paper_id=next(iter(papers)) if len(papers) == 1 else None,
            field=candidate.field, positions=[_position(ref, refs) for ref in evidence],
            detected_by="coordinator", note=candidate.note,
        ))
    return conflicts


def _snippet(source, item):
    text = source["text"]
    page = re.fullmatch(r"p\.?\s*(\d+)", item.source_loc, re.I)
    if page:
        match = re.search(r"\[p\." + page.group(1) + r"\](.*?)(?=\[p\.\d+\]|\Z)", text, re.S)
        if not match:
            return None
        text = match.group(0)
    words = list(re.finditer(r"\S+", text))
    if not words:
        return None
    # Overlapping windows retain original wording and bound each supplied excerpt to 400 words.
    if item.quote and item.quote_verified is True:
        for start in range(0, len(words), 200):
            end = min(start + 400, len(words))
            excerpt = text[words[start].start():words[end - 1].end()]
            if QuoteVerifier(excerpt).verify(item.quote):
                return excerpt
        return None
    if source["based_on"] == "abstract_only" or page:
        return text[:words[min(400, len(words)) - 1].end()]
    return None


def review_snippets(conflict, refs, sources):
    snippets = {}
    for ref in conflict_refs(conflict):
        card, item = refs[ref]
        source = sources[card.paper_id]
        text = _snippet(source, item)
        if text:
            snippets[ref] = {"paper_id": card.paper_id, "based_on": source["based_on"],
                             "source_loc": item.source_loc, "text": text}
    return snippets


def verified_acceptance(review, conflict, refs, snippets):
    positions = set(conflict_refs(conflict))
    if (len(set(review.accepted_evidence)) != len(review.accepted_evidence)
            or any(ref not in positions for ref in review.accepted_evidence)
            or any(q.ref not in positions for q in review.quotes)):
        raise ValueError("Review evidence must refer to this conflict's positions only")
    if review.resolution == "needs_human":
        return []
    quotes = {q.ref: q.quote for q in review.quotes}
    if not review.accepted_evidence or len(quotes) != len(review.quotes):
        raise ValueError("A resolved review requires accepted evidence and unique supporting quotes")
    for ref in review.accepted_evidence:
        item = refs[ref][1]
        if (ref not in snippets or ref not in quotes or item.kind != "author_claim"
                or item.quote_verified is not True or not QuoteVerifier(snippets[ref]["text"]).verify(quotes[ref])):
            raise ValueError("A resolved review requires verified author claims and matching original excerpts")
    return review.accepted_evidence
