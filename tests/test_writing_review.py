"""Semantic trust boundary, bounded context and immutable targeted revision policy."""

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from airesearcher.core.models.claim import CheckReport, Claim, ClaimCheck, Issue, ValueRef
from airesearcher.core.models.common import VersionRef
from airesearcher.stages.writing import review as r


def unit(claim_id="CL1", section="results"):
    return {
        "claim_id": claim_id,
        "sentence": "Our method works everywhere.",
        "section": section,
        "location": {"file": f"paper/sections/{section}.tex", "line": 2, "start": 12, "end": 40},
        "nearby": [],
        "scope": {},
        "source": "Our method works everywhere.",
    }


def response(u, code="OVERCLAIM_SCOPE"):
    return {
        "sentence": u["sentence"],
        "claim_id": u["claim_id"],
        "issues": [{"code": code, "detail": "Scope not established.", "suggestion": "Restrict scope."}],
    }


@pytest.mark.parametrize("code", list(r.SEVERITIES))
def test_semantic_codes_and_code_owned_severity(code):
    u = unit(None if code == "NO_EVIDENCE" else "CL1")
    (issue,) = r.normalize(u, response(u, code))
    assert issue.severity == ("minor" if code == "MIXED_INTERPRETATION" else "major")
    assert issue.location == u["location"] and issue.sentence == u["sentence"]


@pytest.mark.parametrize("change", ["sentence", "claim_id", "code", "severity", "support", "reference"])
def test_untrusted_output_rejected(change):
    u = unit()
    raw = response(u)
    if change in {"sentence", "claim_id"}:
        raw[change] = "invented"
    elif change == "code":
        raw["issues"][0]["code"] = "NUM_MISMATCH"
    else:
        raw["issues"][0][change] = "invented"
    with pytest.raises((ValueError, ValidationError)):
        r.normalize(u, raw)


@pytest.mark.parametrize(
    "claim_id,section,code",
    [
        (None, "abstract", "OVERCLAIM_SCOPE"),
        ("CL1", "results", "NO_EVIDENCE"),
        ("CL1", "discussion", "MIXED_INTERPRETATION"),
    ],
)
def test_semantic_code_scope(claim_id, section, code):
    u = unit(claim_id, section)
    with pytest.raises(ValueError):
        r.normalize(u, response(u, code))


def report(support="supported", issue=None):
    return CheckReport(
        paper_ref=VersionRef(artifact_id="paper", version=1, sha256="a" * 64),
        mode="full",
        claims=[ClaimCheck(claim_id="CL1", support=support, issues=[issue] if issue else [])],
        compile_ok=True,
        created_at=datetime.now(UTC),
    )


@pytest.mark.parametrize(
    "support,code,severity,expected",
    [
        ("supported", "OVERCLAIM_SCOPE", "major", "partially_supported"),
        ("supported", "MIXED_INTERPRETATION", "minor", "supported"),
        ("unsupported", "NUM_MISMATCH", "blocker", "unsupported"),
        ("contradicted", "CONTRADICTED", "blocker", "contradicted"),
        ("unverifiable", "STALE_EVIDENCE", "major", "unverifiable"),
    ],
)
def test_merge_preserves_deterministic_and_support_precedence(support, code, severity, expected):
    deterministic = report(support, Issue(code=code, severity=severity, claim_id="CL1"))
    before = deterministic.model_dump()
    merged = r.merge(
        deterministic,
        r.normalize(unit(), response(unit(), "MIXED_INTERPRETATION" if severity == "minor" else "OVERCLAIM_SCOPE")),
    )
    assert deterministic.model_dump() == before
    assert merged.claims[0].issues[0] == deterministic.claims[0].issues[0]
    assert merged.claims[0].support == expected
    assert merged.counts[severity] >= 1
    clean_semantic = r.merge(deterministic, [])
    assert clean_semantic.claims[0].issues == deterministic.claims[0].issues


def test_unbound_issue_and_hard_evidence_never_revisable():
    u = unit(None)
    combined = r.merge(report(), r.normalize(u, response(u, "NO_EVIDENCE")))
    assert len(combined.unbound_issues) == 1 and combined.counts["major"] == 1
    assert len(r.revision_targets(combined, [u])) == 1
    for code in r.HARD:
        combined.claims[0].issues = [Issue(code=code, severity="blocker", claim_id="CL1")]
        assert r.revision_targets(combined, [u]) == []


def claim():
    return Claim(
        claim_id="CL1",
        section="results",
        type="numeric",
        statement_kind="observation",
        hedge="none",
        importance="major",
        evidence=[{"type": "aggregate", "ref": "C1", "version": 1}],
        text=r"\claim{CL1}{Mean was \airval{exact.key}.}",
        values=[
            ValueRef(
                key="exact.key",
                aggregate_id="C1",
                aggregate_version=1,
                group={"method": "a"},
                metric="accuracy",
                stat="mean",
                rendered="0.8",
            )
        ],
    )


def revision_case():
    c = claim()
    u = unit()
    u["sentence"] = r.inner(c)
    u["scope"] = {"hedge": "none", "values": [c.values[0].model_dump()], "recomputed": {"exact.key": "0.8"}}
    context, placeholders = r.revision_context(u, r.normalize(u, response(u)), c)
    raw = {
        "sentence": context["sentence"],
        "claim_id": "CL1",
        "hedge": "possible",
        "replacement": "Within the evaluated scope, mean was [[VALUE_1]].",
    }
    return c, context, placeholders, raw


def test_revision_placeholders_and_immutable_claim_fields():
    c, context, placeholders, raw = revision_case()
    assert "exact.key" not in json.dumps(context)
    text, revised = r.validate_revision(context, placeholders, raw, c)
    assert text == revised.text and r"\airval{exact.key}" in text
    assert revised.model_dump(exclude={"text", "hedge", "location"}) == c.model_dump(
        exclude={"text", "hedge", "location"}
    )
    assert revised.hedge == "possible"


@pytest.mark.parametrize(
    "replacement",
    [
        "Value removed.",
        "[[VALUE_1]] and [[VALUE_1]].",
        "[[VALUE_2]].",
        r"\airval{other}",
        "[[VALUE_1]] was 99.5.",
        r"[[VALUE_1]] \cite{fake}.",
        r"[[VALUE_1]] \claim{CL99}{fake}.",
        "[[VALUE_1]]% conceal",
        "[[VALUE_1]] [[UNKNOWN]]",
    ],
)
def test_reviser_cannot_manufacture_evidence(replacement):
    c, context, placeholders, raw = revision_case()
    raw["replacement"] = replacement
    with pytest.raises(ValueError):
        r.validate_revision(context, placeholders, raw, c)


def test_unbound_revision_can_remove_exact_sentence():
    u = unit(None)
    context, placeholders = r.revision_context(u, r.normalize(u, response(u, "NO_EVIDENCE")), None)
    raw = {"sentence": u["sentence"], "claim_id": None, "replacement": "", "hedge": "none"}
    assert r.validate_revision(context, placeholders, raw, None) == ("", None)
    raw["sentence"] += " invented"
    with pytest.raises(ValueError):
        r.validate_revision(context, placeholders, raw, None)


def test_sentence_offsets_comments_literals_citations_and_figures():
    text = (
        r"\section{Results}" + "\n% We win everywhere.\n" + r"\verb|False result.|" + "\n"
        r"Our method wins\cite{author2020}. Another result." + "\n"
        r"\begin{figure}\caption{Figure sentence.}\end{figure}"
    )
    spans = list(r.sentence_spans(text))
    assert [text[a:b] for a, b in spans] == [r"Our method wins\cite{author2020}.", "Another result."]


@pytest.mark.parametrize("value", [0, -1, True, False, 1.2, "2", None, 21])
def test_review_limit_rejects_invalid_config(value):
    with pytest.raises(ValueError, match="max_review_rounds"):
        r.round_limit(value)


def test_positive_bounded_review_limit():
    assert r.round_limit(1) == 1 and r.round_limit(2) == 2


@pytest.mark.parametrize("word", ["lower", "negative", "not"])
def test_revision_cannot_reverse_comparison_language(word):
    c, context, placeholders, raw = revision_case()
    raw["replacement"] = f"Mean was {word} [[VALUE_1]]."
    with pytest.raises(ValueError, match="direction"):
        r.validate_revision(context, placeholders, raw, c)


def test_existing_citation_preserved_by_code_owned_placeholder():
    c = claim()
    c.text = c.text[:-1] + r" As described\cite{existing2020}.}"
    u = unit()
    u["sentence"] = r.inner(c)
    context, placeholders = r.revision_context(u, [], c)
    assert r"\cite" not in context["sentence"] and "existing2020" not in context["sentence"]
    raw = {"sentence": context["sentence"], "claim_id": "CL1", "replacement": context["sentence"], "hedge": "none"}
    assert r.validate_revision(context, placeholders, raw, c)[1] == c
    raw["replacement"] = raw["replacement"].replace("[[CITATION_1]]", "")
    with pytest.raises(ValueError, match="placeholder"):
        r.validate_revision(context, placeholders, raw, c)
