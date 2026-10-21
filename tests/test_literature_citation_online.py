"""Test optional citation HTTP checks without real network or model calls."""

import httpx
import pytest

from airesearcher.services import literature
from airesearcher.stages.fakes.idea import fake_papers


@pytest.fixture
def citation(project, monkeypatch):
    paper = fake_papers('q')[0].model_copy(update={'url': 'https://arxiv.org/abs/test'})
    registry = project.root / 'idea/literature.jsonl'
    registry.write_text(paper.model_dump_json() + '\n', encoding='utf-8')
    requests = []
    client_type = httpx.Client

    def install(handler):
        def send(request):
            requests.append(request)
            return handler(request)
        monkeypatch.setattr(literature.httpx, 'Client', lambda **kw: client_type(
            transport=httpx.MockTransport(send), **kw))
    return project, paper, registry, requests, install


def check(citation):
    project, paper, _, _, _ = citation
    return literature.verify_citation(project.root, paper.paper_id, online=True, guard=project.permissions.guard)


def test_default_offline_and_registry_precedence_never_request(citation):
    project, paper, registry, requests, install = citation
    install(lambda r: pytest.fail('Unexpected HTTP request'))
    assert literature.verify_citation(project.root, paper.paper_id).status == 'verified'
    assert literature.verify_citation(project.root, 'unknown', online=True).status == 'not_in_archive'
    for update in ({'access_level': 'metadata_only'}, {'url': ''}):
        registry.write_text(paper.model_copy(update=update).model_dump_json() + '\n', encoding='utf-8')
        assert check(citation).status == 'metadata_only'
    assert not requests


def test_online_success_preserves_artifacts_and_audits_permission(citation):
    project, _, _, requests, install = citation
    before = {str(p.relative_to(project.root)): p.read_bytes() for p in project.root.rglob('*')
              if p.is_file() and p.name != 'research_log.jsonl'}
    install(lambda r: httpx.Response(200))
    result = check(citation)
    assert result.status == 'verified' and 'content claims are not verified' in result.note
    assert [r.method for r in requests] == ['HEAD']
    after = {str(p.relative_to(project.root)): p.read_bytes() for p in project.root.rglob('*')
             if p.is_file() and p.name != 'research_log.jsonl'}
    assert after == before
    assert any(event.type == "permission.granted" and event.actor == "service:literature"
               for event in project.events.all())


@pytest.mark.parametrize('code', [403, 404, 429, 500])
def test_error_status_is_explicit(citation, code):
    *_, install = citation
    install(lambda r: httpx.Response(code))
    result = check(citation)
    assert result.status == 'source_unreachable' and str(code) in result.note


@pytest.mark.parametrize('code', [405, 501])
def test_head_fallback_streams_without_consuming_body(citation, code):
    class UnreadBody(httpx.SyncByteStream):
        def __iter__(self):
            pytest.fail('Response body must not be consumed')
            yield b''
    _, _, _, requests, install = citation
    install(lambda r: httpx.Response(code if r.method == 'HEAD' else 200, stream=UnreadBody()))
    assert check(citation).status == 'verified'
    assert [r.method for r in requests] == ['HEAD', 'GET']


@pytest.mark.parametrize('target', ['https://evil.test/paper', 'file:///etc/passwd',
                                  'https://user:secret@arxiv.org/paper'])
def test_redirect_targets_are_validated_before_contact(citation, target):
    _, _, _, requests, install = citation
    install(lambda r: httpx.Response(302, headers={'location': target}))
    result = check(citation)
    assert result.status == 'verified' and 'not completed' in result.note
    assert len(requests) == 1


def test_relative_redirect_and_limit(citation):
    _, _, _, requests, install = citation
    install(lambda r: httpx.Response(302, headers={'location': '/next'}) if len(requests) == 1
            else httpx.Response(200))
    assert check(citation).status == 'verified'
    assert requests[-1].url.path == '/next'
    requests.clear()
    install(lambda r: httpx.Response(302, headers={'location': '/loop'}))
    assert check(citation).status == 'source_unreachable'
    assert len(requests) == 6


def test_timeout_and_permission_failure_do_not_leak_urls(citation):
    project, _, _, requests, install = citation
    def timeout(request):
        raise httpx.ReadTimeout('sensitive transport details', request=request)
    install(timeout)
    result = check(citation)
    assert result.status == 'source_unreachable' and 'sensitive' not in result.note
    project.config.permissions.network.allow = []
    requests.clear()
    result = check(citation)
    assert result.status == 'verified' and 'not performed' in result.note
    assert not requests


def test_unlisted_doi_is_unchecked_and_denial_is_audited(citation):
    project, paper, registry, requests, install = citation
    registry.write_text(paper.model_copy(update={"url": "https://doi.org/10.5555/example"}).model_dump_json() + "\n",
                        encoding="utf-8")
    install(lambda r: pytest.fail("An unlisted DOI host must not be contacted"))
    result = check(citation)
    assert result.status == "verified" and "not performed" in result.note
    assert not requests
    assert any(event.type == "permission.denied" and event.actor == "service:literature"
               and event.data["target"] == "doi.org" for event in project.events.all())


def test_deadline_before_first_request_is_unchecked(citation, monkeypatch):
    from types import SimpleNamespace

    _, _, _, requests, install = citation
    install(lambda r: pytest.fail("An expired check must not send a request"))
    ticks = iter([0, 21])
    monkeypatch.setattr(literature, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    result = check(citation)
    assert result.status == "verified" and "not performed" in result.note
    assert not requests


def test_online_checks_share_event_sequence_listeners_and_grant_cache(citation):
    project, _, _, requests, install = citation
    received = []
    project.events.subscribe(received.append)
    before = project.events.append("test.before", "Before citation checks", actor="test")
    install(lambda r: httpx.Response(200))
    assert check(citation).status == "verified"
    assert check(citation).status == "verified"
    after = project.events.append("test.after", "After citation checks", actor="test")

    events = project.events.all()
    seqs = [event.seq for event in events]
    assert seqs == sorted(set(seqs))
    delta = project.events.query(after_seq=before.seq)
    assert [event.type for event in delta] == ["permission.granted", "test.after"]
    assert [event.seq for event in delta] == [before.seq + 1, after.seq]
    assert [event.model_dump() for event in received] == [
        event.model_dump() for event in events if event.seq >= before.seq
    ]
    assert len(requests) == 2


@pytest.mark.parametrize("explicit_none", [False, True])
def test_online_without_project_guard_stays_offline_and_writes_nothing(citation, monkeypatch, explicit_none):
    project, paper, _, requests, _ = citation
    before = {str(p.relative_to(project.root)): p.read_bytes()
              for p in project.root.rglob("*") if p.is_file()}
    def unexpected_client(**kwargs):
        pytest.fail("An online check without a project guard must not create an HTTP client")
    monkeypatch.setattr(literature.httpx, "Client", unexpected_client)
    kwargs = {"guard": None} if explicit_none else {}
    result = literature.verify_citation(project.root, paper.paper_id, online=True, **kwargs)
    assert result.status == "verified"
    assert "not performed" in result.note and "no project permission guard" in result.note
    assert not requests
    after = {str(p.relative_to(project.root)): p.read_bytes()
             for p in project.root.rglob("*") if p.is_file()}
    assert after == before
