"""Deterministic retrieval-source HTTP tests without network access or real API keys."""

import json
from types import SimpleNamespace

import httpx
import pytest

from airesearcher.core.errors import PermissionDenied
from airesearcher.core.models.literature import SearchSnapshot
from airesearcher.stages.idea.sources.arxiv import ArxivSource
from airesearcher.stages.idea.sources.http import GuardedHTTP, SourceFailure, SourceWait
from airesearcher.stages.idea.sources.replay import ReplaySource
from airesearcher.stages.idea.sources.router import FallbackSource
from airesearcher.stages.idea.sources.semantic_scholar import SemanticScholarSource

S2_ROW = {
    "paperId": "abc123", "title": "LoRA: Low-Rank Adaptation of Large Language Models",
    "authors": [{"name": "Edward Hu"}], "year": 2022, "venue": "ICLR",
    "publicationTypes": ["Conference"], "externalIds": {"DOI": "10.1/Example", "ArXiv": "2106.09685v2"},
    "abstract": "Low rank adaptation.", "url": "https://www.semanticscholar.org/paper/abc123",
}
XML = b'''<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
<entry><id>http://arxiv.org/abs/2106.09685v2</id><title>LoRA: Low-Rank Adaptation</title>
<published>2021-06-17T00:00:00Z</published><summary>Low rank adaptation.</summary>
<author><name>Edward Hu</name></author><arxiv:doi>10.1/Example</arxiv:doi></entry></feed>'''


@pytest.fixture(autouse=True)
def reset_rate_limits():
    GuardedHTTP._next.clear()
    yield
    GuardedHTTP._next.clear()


def wire(handler, guard=lambda *args: None, clock=lambda: 1000):
    return GuardedHTTP(guard, client=httpx.Client(transport=httpx.MockTransport(handler)), clock=clock)


def test_s2_metadata_and_missing_abstract(monkeypatch):
    monkeypatch.delenv("S2_API_KEY", raising=False)
    def handler(request):
        assert request.url.params["year"] == "2021-"
        assert request.url.params["limit"] == "3"
        assert "x-api-key" not in request.headers
        return httpx.Response(200, json={"data": [S2_ROW, {**S2_ROW, "paperId": "other", "abstract": None}]})
    snap, papers = SemanticScholarSource(wire(handler)).search("LoRA", year_from=2021, limit=3)
    p = papers[0]
    assert p.paper_id == "doi:10.1/example" and p.ids["arxiv"] == "2106.09685"
    assert p.venue_type == "conference" and p.citation_key == "hu2022lora"
    assert "@inproceedings{hu2022lora" in p.bibtex
    assert papers[1].access_level == "metadata_only"
    assert snap.raw_response["data"][0] == S2_ROW and snap.status == "ok"


def test_s2_key_only_sent_to_s2(monkeypatch):
    monkeypatch.setenv("S2_API_KEY", "test-only-not-a-real-key")
    def handler(request):
        assert request.headers["x-api-key"] == "test-only-not-a-real-key"
        return httpx.Response(200, json={"data": []})
    SemanticScholarSource(wire(handler)).search("LoRA")


def test_arxiv_atom_and_plain_query():
    def handler(request):
        assert 'all:"LoRA" AND all:"fine"' in request.url.params["search_query"]
        assert "submittedDate:[202101010000" in request.url.params["search_query"]
        return httpx.Response(200, content=XML)
    snap, papers = ArxivSource(wire(handler)).search("LoRA fine", year_from=2021, limit=2)
    assert papers[0].ids["arxiv"] == "2106.09685" and papers[0].paper_id == "doi:10.1/example"
    assert papers[0].access_level == "abstract_only" and papers[0].fulltext_path is None
    assert snap.raw_response["xml"].encode() == XML


def test_arxiv_empty_and_error_entry():
    src = ArxivSource(wire(lambda r: httpx.Response(200, text='<feed xmlns="http://www.w3.org/2005/Atom"/>')))
    assert src.search("nothing")[1] == []
    GuardedHTTP._next.clear()
    src = ArxivSource(wire(lambda r: httpx.Response(200, text='<feed xmlns="http://www.w3.org/2005/Atom">'
                                               '<entry><id>http://arxiv.org/api/errors</id></entry></feed>')))
    with pytest.raises(SourceFailure):
        src.search("bad")


def test_429_preserves_snapshot_and_retry_after():
    saved = []
    src = SemanticScholarSource(wire(lambda r: httpx.Response(429, headers={"Retry-After": "7"},
                                                             json={"message": "limited"})), saved.append)
    with pytest.raises(SourceFailure) as exc:
        src.search("LoRA")
    assert exc.value.retry_after == 7
    assert saved[0].status == "429" and saved[0].raw_response == {"message": "limited"}


def test_permission_denied_before_request():
    calls = []
    def deny(*args):
        raise PermissionDenied("blocked")
    src = ArxivSource(wire(lambda r: calls.append(r), guard=deny))
    with pytest.raises(PermissionDenied):
        src.search("LoRA")
    assert calls == []


def test_rate_wait_and_timeout():
    calls = []
    http = wire(lambda r: (calls.append(r) or httpx.Response(200, json={})))
    http.get("https://export.arxiv.org/api/query", {}, interval=3)
    with pytest.raises(SourceWait) as exc:
        http.get("https://export.arxiv.org/api/query", {}, interval=3)
    assert exc.value.seconds == 3 and len(calls) == 1
    GuardedHTTP._next.clear()
    def timeout(request):
        raise httpx.ReadTimeout("timeout", request=request)
    with pytest.raises(SourceFailure, match="ReadTimeout"):
        wire(timeout).get("https://export.arxiv.org/api/query", {}, interval=3)


def test_retry_restart_and_switch():
    clock = [0]
    calls, events, state = [], [], {}
    def fail(*args, **kwargs):
        calls.append("s2")
        raise SourceFailure("limited", retry_after=2)
    primary = SimpleNamespace(name="semantic_scholar", search=fail)
    backup = SimpleNamespace(name="arxiv", search=lambda *a, **k: ("snapshot", []))
    def router():
        return FallbackSource([primary, backup], state,
                              SimpleNamespace(append=lambda *a, **k: events.append(k)), clock=lambda: clock[0])
    for _ in range(4):
        with pytest.raises(SourceWait) as exc:
            router().search("LoRA")
        clock[0] += exc.value.seconds
    assert len(calls) == 4 and len(events) == 1  # initial + three retries
    assert router().search("LoRA") == ("snapshot", [])
    assert not state


def test_both_sources_fail():
    clock = [0]
    def fail(*a, **k):
        raise SourceFailure("unavailable")
    src = SimpleNamespace(name="test", search=fail)
    router = FallbackSource([src, src], {}, SimpleNamespace(append=lambda *a, **k: None), clock=lambda: clock[0])
    for _ in range(7):
        with pytest.raises(SourceWait) as exc:
            router.search("LoRA")
        clock[0] += exc.value.seconds
    with pytest.raises(SourceFailure, match="All literature sources failed"):
        router.search("LoRA")


def test_replay_real_raw_snapshot_and_missing(tmp_path):
    snap, papers = ArxivSource(wire(lambda r: httpx.Response(200, content=XML))).search("LoRA", limit=2)
    (tmp_path / "snapshot.json").write_text(snap.model_dump_json(), encoding="utf-8")
    replay = ReplaySource(tmp_path)
    restored, result = replay.search("LoRA", limit=2)
    assert restored == snap and result[0].paper_id == papers[0].paper_id
    assert result[0].snapshot_id == snap.snapshot_id
    assert replay.get(result[0].paper_id) == result[0]
    with pytest.raises(SourceFailure, match="no network request was made"):
        replay.search("different", limit=2)
    with pytest.raises(SourceFailure):
        replay.search("LoRA", year_from=2021, limit=2)


def test_replay_saved_batch(tmp_path):
    from airesearcher.testing.fake_literature import FakeLiterature
    snap, papers = FakeLiterature().search("LoRA", year_from=2021, limit=3)
    data = {"snapshot": snap.model_dump(mode="json"), "papers": [p.model_dump(mode="json") for p in papers]}
    (tmp_path / "batch.json").write_text(json.dumps(data), encoding="utf-8")
    assert ReplaySource(tmp_path).search("LoRA", year_from=2021, limit=3)[1] == papers


def test_missing_data_is_not_empty_success():
    saved = []
    src = SemanticScholarSource(wire(lambda r: httpx.Response(200, json={"message": "error"})), saved.append)
    with pytest.raises(SourceFailure):
        src.search("LoRA")
    assert saved[0].status == "invalid_response"


def test_get_s2_and_arxiv():
    def handler(request):
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, content=XML)
        assert "/paper/DOI:" in str(request.url) or "/paper/DOI%3A" in str(request.url)
        return httpx.Response(200, json=S2_ROW)
    http = wire(handler)
    assert SemanticScholarSource(http).get("doi:10.1/example").paper_id == "doi:10.1/example"
    assert ArxivSource(http).get("arxiv:2106.09685").ids["arxiv"] == "2106.09685"


def test_snapshot_contract():
    snap, _ = SemanticScholarSource(wire(lambda r: httpx.Response(200, json={"data": []}))).search("LoRA")
    assert SearchSnapshot.model_validate_json(snap.model_dump_json()) == snap


def test_citation_key_collisions():
    from airesearcher.stages.idea.sources.metadata import unique_citation_keys

    _, papers = SemanticScholarSource(wire(lambda r: httpx.Response(200, json={"data": [S2_ROW]}))).search("LoRA")
    a = papers[0]
    b = a.model_copy(update={"paper_id": "s2:different"})
    c = a.model_copy(update={"paper_id": "s2:third"})
    result = unique_citation_keys([a, b, c], {})
    assert [p.citation_key for p in result] == ["hu2022lora", "hu2022lorab", "hu2022lorac"]
    assert "@inproceedings{hu2022lorab," in b.bibtex
