"""Prevent wrong-page verification and unsafe recovery of oversized quotations."""

import pytest

from airesearcher.core.models.literature import CardItem
from airesearcher.stages.idea.quotes import QuoteVerifier, tokens


def item(quote, location="p.2"):
    return CardItem(field="method", content="A bounded extracted fact.", kind="author_claim",
                    source_loc=location, quote=quote, confidence="high")


def test_existing_but_wrong_page_cannot_verify():
    checker = QuoteVerifier("[p.1]\nThe method is evaluated on a benchmark.\n[p.2]\nLicenses are not discussed.")
    checked, audit = checker.check_with_report(item("The method is evaluated on a benchmark"), "full_text")
    assert checked.quote_verified is False and checked.kind == "agent_inference"
    assert audit["reason"] == "text_mismatch"


def test_exact_long_quote_is_shortened_only_after_full_page_match():
    quote = " ".join(f"token{i}" for i in range(65))
    checked, audit = QuoteVerifier("[p.2]\n" + quote).check_with_report(item(quote), "full_text")
    assert checked.quote_verified is True and len(tokens(checked.quote)) <= 20
    assert audit["reason"] == "exact_long_quote_shortened" and audit["original_quote_words"] == 65
    assert audit["semantic_support"] == "not_evaluated"


def test_matching_prefix_cannot_rescue_fabricated_tail():
    source = " ".join(f"token{i}" for i in range(65))
    quote = " ".join(source.split()[:50] + ["invented"] * 15)
    checked, audit = QuoteVerifier("[p.2]\n" + source).check_with_report(item(quote), "full_text")
    assert checked.quote_verified is False and checked.quote is None
    assert audit["reason"] == "oversized_quote_unmatched"


@pytest.mark.parametrize("location", ["p.1", "p.999", "section 2", "pp.2-999999"])
def test_long_quote_on_another_or_invalid_page_is_not_rescued(location):
    quote = " ".join(f"word{i}" for i in range(60))
    checker = QuoteVerifier("[p.1]\nOther text.\n[p.2]\n" + quote)
    assert checker.check(item(quote, location), "full_text").quote_verified is False


def test_adjacent_page_range_is_supported_but_missing_pages_are_rejected():
    checker = QuoteVerifier("[p.2]\nFirst part of an exact quotation\n[p.3]\ncontinues on the next page.")
    quote = "First part of an exact quotation continues on the next page"
    assert checker.check(item(quote, "pp.2-3"), "full_text").quote_verified is True
    assert checker.check(item(quote, "pp.1-3"), "full_text").quote_verified is False


def test_page_lists_match_only_listed_pages_and_never_bridge_gaps():
    checker = QuoteVerifier("[p.1]\nFirst passage\n[p.2]\nHidden middle\n[p.3]\nLast passage")
    assert checker.check(item("Last passage", "p.1, p.3"), "full_text").quote_verified is True
    assert checker.check(item("Hidden middle", "p.1, p.3"), "full_text").quote_verified is False
    assert checker.check(item("First passage Last passage", "p.1, p.3"), "full_text").quote_verified is False


def test_cached_quality_audit_is_read_only_and_never_calls_model(air_home):
    import json

    from scripts.literature.quality import audit_cached_reading

    from .test_idea_publication import pending

    project, engine, _ = pending(air_home)
    before = {name: (project.root / name).read_bytes() for name in
              ["project.yaml", "idea/selected.md", "idea/selected.json", ".llm/calls.jsonl"]}
    calls = len(engine.llm.backend.calls)
    report = audit_cached_reading(project.root)
    assert report["new_model_calls"] == 0 and report["archive_modified"] is False
    assert not report["unavailable"] and len(report["items"]) == 28
    assert report["semantic_accuracy"] is None
    assert len(engine.llm.backend.calls) == calls
    assert all((project.root / name).read_bytes() == content for name, content in before.items())
    assert json.loads(json.dumps(report))["rechecked_quote_matches"] == 0
