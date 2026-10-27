"""Bounded semantic review and revision policy; deterministic evidence stays in services."""

from __future__ import annotations

import re
from collections import Counter
from typing import Literal

from pydantic import Field

from airesearcher.core.models.claim import Issue

from . import claims as binding
from ._latex import active_text, commands

REVIEW_PROMPT = "writing/review"
REVISE_PROMPT = "writing/revise"
SemanticCode = Literal[
    "OVERCLAIM_CAUSAL", "OVERCLAIM_SCOPE", "MISSING_LIMITATION", "NO_EVIDENCE", "MIXED_INTERPRETATION"
]
SEVERITIES = {
    "OVERCLAIM_CAUSAL": "major",
    "OVERCLAIM_SCOPE": "major",
    "MISSING_LIMITATION": "major",
    "NO_EVIDENCE": "major",
    "MIXED_INTERPRETATION": "minor",
}
# Never send hard deterministic evidence failures to a prose reviser.
HARD = {"NUM_MISMATCH", "INVALID_RUN", "STALE_EVIDENCE", "CONTRADICTED", "CITATION_NOT_IN_ARCHIVE"}


class SemanticFinding(binding.StrictModel):
    code: SemanticCode
    detail: str = Field(min_length=1, max_length=1200)
    suggestion: str = Field(min_length=1, max_length=1200)


class ReviewResponse(binding.StrictModel):
    sentence: str
    claim_id: str | None
    issues: list[SemanticFinding] = Field(max_length=5)


class RevisionResponse(binding.StrictModel):
    sentence: str
    claim_id: str | None
    replacement: str = Field(max_length=2400)
    hedge: Literal["none", "likely", "possible"]


def round_limit(value):
    if type(value) is not int or not 1 <= value <= 20:
        raise ValueError("paper.max_review_rounds must be an integer between 1 and 20")
    return value


def inner(claim):
    prefix = "\\claim{" + claim.claim_id + "}{"
    if not claim.text.startswith(prefix) or not claim.text.endswith("}"):
        raise ValueError("unsupported bound claim wrapper")
    return claim.text[len(prefix) : -1]


def _blank(text, start, end):
    return text[:start] + "".join(c if c in "\r\n" else " " for c in text[start:end]) + text[end:]


def sentence_spans(text):
    """Conservative literal sentence spans, outside commands' arguments and inactive TeX.

    Commands remain in a sentence (e.g. citations); their punctuation cannot split it.
    Structural commands and complete figure/table environments are excluded.
    """
    clean = active_text(text)
    stack = []
    for cmd in commands(text):
        if cmd.name == "begin" and cmd.value in {"figure", "table"}:
            stack.append((cmd.value, cmd.start))
        elif cmd.name == "end" and stack and cmd.value == stack[-1][0]:
            _, start = stack.pop()
            clean = _blank(clean, start, cmd.end)
        elif cmd.name in {"section", "subsection", "subsubsection", "label", "begin", "end"}:
            clean = _blank(clean, cmd.start, cmd.end)
    # Ignore punctuation inside braced commands, while preserving original source spans.
    boundaries = clean
    for cmd in commands(clean):
        boundaries = _blank(boundaries, cmd.start, cmd.end)
    start = 0
    for match in re.finditer(r"[.!?。！？](?=\s|$)|\n\s*\n|$", boundaries):
        end = match.end()
        part = clean[start:end]
        left, right = len(part) - len(part.lstrip()), len(part.rstrip())
        if right > left and re.search(r"[A-Za-z\u4e00-\u9fff]", part):
            yield start + left, start + right
        start = end


def _scope(claim, bundle, checked):
    values = claim.values
    selected = []
    for source in bundle.aggregates:
        refs = [v for v in values if v.aggregate_id == source.aggregate_id]
        links = [e for e in claim.evidence if e.type == "aggregate" and e.ref == source.aggregate_id]
        if not refs and not links:
            continue
        rows = [
            r
            for r in source.data.rows
            if any(
                r.metric == v.metric and (r.group == v.group or (v.contrast and r.group in v.contrast.values()))
                for v in refs
            )
        ]
        selected.append(
            {
                "ref": source.ref.model_dump(),
                "eval_condition": source.data.eval_condition,
                "rows": [{"group": r.group, "metric": r.metric, "n": r.n} for r in rows],
            }
        )
    papers = [
        {"paper_id": p.paper_id, "access_level": p.record.get("access_level")}
        for p in bundle.papers
        if any(e.type == "paper" and e.ref == p.paper_id for e in claim.evidence)
    ]
    return {
        "statement_kind": claim.statement_kind,
        "hedge": claim.hedge,
        "importance": claim.importance,
        "hypothesis": {"id": claim.hypothesis, "approved": binding.hypotheses(bundle).get(claim.hypothesis)},
        "task_scope": {k: bundle.runtime.task.get(k) for k in ("name", "domain", "description", "data", "models")}
        if bundle.runtime
        else None,
        "comparison": claim.comparison.model_dump() if claim.comparison else None,
        "values": [v.model_dump() for v in values],
        "recomputed": checked.recomputed,
        "aggregates": selected,
        "papers": papers,
        "evidence": [e.model_dump(exclude={"note"}) for e in claim.evidence],
    }


def units(source, claims, bundle, report):
    """Build exact anchors from the archived manuscript, never the mutable live paper."""
    result = []
    checks = {c.claim_id: c for c in report.claims}
    for section in binding.SECTIONS:
        text = (source / f"sections/{section}.tex").read_bytes().decode("utf-8")
        binding.validate_bound_section(text, claims, section)
        remainder = active_text(text)
        bound = []
        for claim in claims:
            if claim.section != section:
                continue
            start = next(
                m.start()
                for m in re.finditer(re.escape(claim.text), text)
                if remainder[m.start() : m.start() + 6] == r"\claim"
            )
            end = start + len(claim.text)
            bound.append((start, end, claim))
            remainder = _blank(remainder, start, end)
        prose = list(sentence_spans(remainder))
        targets = [(a, b, c) for a, b, c in bound]
        if section in {"abstract", "results", "conclusion"}:
            targets += [(a, b, None) for a, b in prose]
        for start, end, claim in sorted(targets, key=lambda t: t[0]):
            before = [(a, b) for a, b in prose if b <= start]
            after = [(a, b) for a, b in prose if a >= end]
            nearby = [text[a:b][:800] for a, b in (before[-1:] + after[:1])]
            sentence = inner(claim) if claim else text[start:end]
            if len(sentence) > 2400:
                raise ValueError("semantic review sentence exceeds bounded 2400-character limit; split explicitly")
            unit = {
                "claim_id": claim.claim_id if claim else None,
                "sentence": sentence,
                "section": section,
                "nearby": nearby,
                "scope": _scope(claim, bundle, checks[claim.claim_id]) if claim else {},
                "location": {
                    "file": f"paper/sections/{section}.tex",
                    "line": text[:start].count("\n") + 1,
                    "start": start,
                    "end": end,
                },
                "source": text[start:end],
            }
            if len(binding.encoded(unit)) > 24000:
                raise ValueError("semantic review unit exceeds bounded evidence context")
            result.append(unit)
    return result


def model_context(unit):
    return {k: v for k, v in unit.items() if k not in {"location", "source"}}


def normalize(unit, raw):
    response = ReviewResponse.model_validate(raw)
    if response.sentence != unit["sentence"] or response.claim_id != unit["claim_id"]:
        raise ValueError("untrusted semantic response: exact sentence/claim identity mismatch")
    issues = []
    for finding in response.issues:
        if (finding.code == "NO_EVIDENCE") != (unit["claim_id"] is None):
            raise ValueError("semantic code does not apply to this bound/unbound review unit")
        if finding.code == "MIXED_INTERPRETATION" and unit["section"] != "results":
            raise ValueError("MIXED_INTERPRETATION only applies to Results")
        issues.append(
            Issue(
                code=finding.code,
                severity=SEVERITIES[finding.code],
                claim_id=unit["claim_id"],
                sentence=unit["sentence"],
                location=unit["location"],
                detail=finding.detail,
                suggestion=finding.suggestion,
            )
        )
    return issues


def merge(deterministic, semantic):
    report = deterministic.model_copy(deep=True)
    for issue in semantic:
        if issue.claim_id is None:
            report.unbound_issues.append(issue)
        else:
            check = next(c for c in report.claims if c.claim_id == issue.claim_id)
            check.issues.append(issue)
    for check in report.claims:
        if any(i.code == "CONTRADICTED" for i in check.issues):
            check.support = "contradicted"
        elif check.support != "unverifiable":
            check.support = (
                "unsupported"
                if any(i.severity == "blocker" for i in check.issues)
                else "partially_supported"
                if any(i.severity == "major" for i in check.issues)
                else "supported"
            )
    counts = {"blocker": 0, "major": 0, "minor": 0}
    for issue in all_issues(report):
        counts[issue.severity] += 1
    counts.update(Counter(c.support for c in report.claims))
    report.counts = counts
    return report


def all_issues(report):
    return [i for c in report.claims for i in c.issues] + report.unbound_issues


def revision_targets(report, review_units):
    # A hard evidence failure anywhere disables automatic prose correction for this round.
    if any(i.code in HARD for i in all_issues(report)):
        return []
    return [
        (
            u,
            [
                i
                for i in all_issues(report)
                if i.code in SEVERITIES
                and i.severity == "major"
                and i.claim_id == u["claim_id"]
                and i.sentence == u["sentence"]
                and i.location == u["location"]
            ],
        )
        for u in review_units
        if any(
            i.code in SEVERITIES
            and i.severity == "major"
            and i.claim_id == u["claim_id"]
            and i.sentence == u["sentence"]
            and i.location == u["location"]
            for i in all_issues(report)
        )
    ]


# A deliberately finite wording surface, not relation extraction. Unrecognized
# prose stays entirely immutable: no heuristic can prove it is only qualification.
COMPARISON_QUALIFIERS = (
    "This observation is limited to the evaluated scope.",
    "This observation is limited to the evaluated setting.",
    "Further evaluation is needed to assess generalization.",
    "These observations do not establish causality.",
)
COMPARISON_SHELLS = (
    "This establishes universal superiority.",
    "These results generalize to all settings.",
    "This establishes a causal mechanism.",
    *COMPARISON_QUALIFIERS,
)


def _comparison_surface(sentence):
    for suffix in COMPARISON_SHELLS:
        if sentence.endswith(suffix):
            core = sentence[: -len(suffix)]
            # Only a standalone terminal shell after complete prose; preserve all
            # preceding bytes, including every factual sentence and its whitespace.
            if re.search(r"[.!?]\s+$", core):
                return core, suffix
    return sentence, ""


def comparison_revisable(unit, claim):
    return (
        not claim
        or not (claim.comparison or claim.type == "comparative")
        or bool(_comparison_surface(unit["sentence"])[1])
    )


def revision_context(unit, issues, claim):
    context = model_context(unit)
    sentence = unit["sentence"]
    placeholders = {}
    if claim:
        for index, value in enumerate(claim.values, 1):
            macro = r"\airval{" + value.key + "}"
            placeholder = f"[[VALUE_{index}]]"
            placeholders[placeholder] = macro
            sentence = sentence.replace(macro, placeholder)
        # Preserve existing citation/reference provenance without letting the model
        # author new citation keys or resource identifiers.
        citations = [c for c in commands(sentence) if c.name in {"cite", "ref", "pageref"}]
        for index, command in reversed(list(enumerate(citations, 1))):
            placeholder = f"[[CITATION_{index}]]"
            placeholders[placeholder] = sentence[command.start : command.end]
            sentence = sentence[: command.start] + placeholder + sentence[command.end :]
    # The reviser receives placeholders, never trusted macro identifiers.
    context["sentence"] = sentence
    if claim and (claim.comparison or claim.type == "comparative"):
        core, shell = _comparison_surface(sentence)
        context.update(immutable_core=core, editable_suffix=shell, allowed_suffixes=list(COMPARISON_QUALIFIERS))
    context["scope"] = {k: v for k, v in context["scope"].items() if k not in {"values", "recomputed"}}
    context["allowed_placeholders"] = list(placeholders)
    context["issues"] = [{"code": i.code, "detail": i.detail, "suggestion": i.suggestion} for i in issues]
    return context, placeholders


def validate_revision(context, placeholders, raw, claim):
    response = RevisionResponse.model_validate(raw)
    if response.sentence != context["sentence"] or response.claim_id != context["claim_id"]:
        raise ValueError("revision exact sentence/claim identity mismatch")
    text = response.replacement
    token = r"\[\[(?:VALUE|CITATION)_\d+\]\]"
    expected = Counter(re.findall(token, context["sentence"]))
    actual = Counter(re.findall(token, text))
    if actual != expected:
        raise ValueError("revision must preserve exact value placeholder multiplicities")
    surface = re.sub(token, "", text)
    if "[[" in surface or "]]" in surface:
        raise ValueError("unknown revision placeholder")
    # Restrict revision to literal wording + existing value slots. Citations, figures,
    # TeX commands, comments and numbers cannot be manufactured or concealed.
    if re.search(r"[\\{}%$#&^~]|(?<![\w])[-+]?\d+(?:\.\d+)?", surface):
        raise ValueError("revision may not add commands, citations, figures or raw numbers")
    if claim:
        if not text.strip():
            raise ValueError("bound Claim cannot be removed")
        if claim.comparison or claim.type == "comparative":
            # Recompute the boundary from the exact original, never from model fields.
            core, shell = _comparison_surface(context["sentence"])
            if text != context["sentence"] and (
                not shell or not text.startswith(core) or text[len(core) :] not in COMPARISON_QUALIFIERS
            ):
                raise ValueError("revision changed immutable comparison facts or unsupported semantic shell")
        # Retain the existing lexical defense for non-comparative Claims. Comparative
        # facts are protected by exact bytes above, not by counts of direction words.
        direction = (
            r"\b(?:higher|lower|greater|less|larger|smaller|positive|negative|"
            r"increased?|decreased?|not|non-significant)\b"
        )
        if not (claim.comparison or claim.type == "comparative") and Counter(
            re.findall(direction, context["sentence"].lower())
        ) != Counter(re.findall(direction, text.lower())):
            raise ValueError("revision changed comparison direction or negative-result wording")
        if {"none": 0, "likely": 1, "possible": 2}[response.hedge] < {"none": 0, "likely": 1, "possible": 2}[
            claim.hedge
        ]:
            raise ValueError("revision cannot strengthen hedge")
        for placeholder, macro in placeholders.items():
            text = text.replace(placeholder, macro)
        revised = claim.model_copy(update={"text": f"\\claim{{{claim.claim_id}}}{{{text}}}", "hedge": response.hedge})
        return revised.text, revised
    if response.hedge != "none":
        raise ValueError("unbound prose has no Claim hedge field")
    return text, None


def markdown(report, metadata):
    lines = [
        f"# Combined evidence review v{metadata['number']}",
        "",
        f"Manuscript: {report.paper_ref.model_dump()}",
        f"Claims: {metadata['claims_ref']}",
        f"Review round: {metadata['round']}",
        f"Automatic revision rounds completed: {metadata['revision_rounds']}",
        f"Completed revision receipt identities: {metadata['revision_receipts']}",
        "Deterministic check: completed; semantic review: completed (exact sentence validated)",
        f"Compile success: {report.compile_ok}",
        f"Counts: {report.counts}",
        f"Review prompt: {metadata['route']['prompt']}",
        f"Claim-review skill: {metadata['route']['skill_ref']}",
        "",
    ]
    for c in report.claims:
        lines.append(f"- {c.claim_id}: {c.support}")
        lines.extend(f"  - {i.severity} {i.code}: {i.sentence} — {i.detail}" for i in c.issues)
    lines += [f"- Unbound {i.severity} {i.code} {i.location}: {i.sentence} — {i.detail}" for i in report.unbound_issues]
    return "\n".join(lines) + "\n"
