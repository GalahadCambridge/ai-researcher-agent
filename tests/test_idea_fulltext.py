"""Test full-text preparation using an existing repository PDF and mocked HTTP without network access."""

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from airesearcher.core.errors import PermissionDenied
from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.project import Project
from airesearcher.engine.engine import ProjectEngine
from airesearcher.engine.stage import Wait
from airesearcher.services.literature import load_papers
from airesearcher.stages.idea import pdf_text
from airesearcher.stages.idea.pdf_text import extract_pdf, reading_excerpt
from airesearcher.stages.idea.sources.http import GuardedHTTP, SourceFailure
from airesearcher.stages.idea.stage import IdeaStage
from airesearcher.stages.idea.storage import append_papers
from airesearcher.testing.fake_literature import FakeLiterature
from scripts.literature.demo import demo_gateway

from .helpers import drive


@pytest.fixture
def existing_pdf():
    # Read an existing repository artifact; tests never create or alter PDFs.
    return (Path(__file__).resolve().parents[1] / "docs/AI Researcher Agent Proposal/main.pdf").read_bytes()


@pytest.fixture(autouse=True)
def reset_limits():
    GuardedHTTP._next.clear()
    yield
    GuardedHTTP._next.clear()


def paper():
    return FakeLiterature().get("arxiv:2106.09685").model_copy(update={"selected_for_reading": True}, deep=True)


def make_engine(air_home, rows, handler, *, download=True, clock=None):
    project = Project.create(goal="Full-text extraction test", task="tasks/smoke", home=air_home,
                             dev={"stage_impl": {"idea": "real"}})
    ticks = iter(range(1000, 100000, 10))
    wire = GuardedHTTP(project.permissions.guard, client=httpx.Client(transport=httpx.MockTransport(handler)),
                       clock=clock or (lambda: next(ticks)))
    stage = IdeaStage(fulltext_download=download, fulltext_http=wire, stop_after="merge")
    engine = ProjectEngine(project, llm=demo_gateway(project), stages={"idea": stage}, retry_delay=0)
    append_papers(engine.build_context(), rows)
    engine.ck.scratch["idea"] = {"phase": "fulltext"}
    engine._save()
    return project, engine, stage


def test_actual_pdf_parser(existing_pdf):
    raw, reading, details = extract_pdf(existing_pdf)
    assert "[p.1]" in raw and len(raw.split()) > 30
    assert details["pages"] > 0 and len(reading.split()) <= 12000


def test_download_extract_and_resume(air_home, existing_pdf):
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=existing_pdf, headers={"Content-Type": "application/pdf"})
    p = paper()
    project, engine, stage = make_engine(air_home, [p], handler)
    engine.tick()  # PDF/text/receipt persisted, literature not yet finalized.
    assert len(calls) == 1
    resumed = ProjectEngine(Project.open(project.root), llm=demo_gateway(project),
                            stages={"idea": stage}, retry_delay=0)
    drive(resumed, S.Paused)
    result = load_papers(project.root)[p.paper_id]
    assert result.access_level == "full_text" and (project.root / result.fulltext_path).exists()
    report = json.loads((project.root / "idea/fulltext_summary.json").read_text(encoding="utf-8"))
    assert report["full_text_count"] == 1
    assert (project.root / report["papers"][0]["pdf_path"]).read_bytes() == existing_pdf
    assert len(calls) == 1 and resumed.ck.scratch["idea"]["phase"] == "refine"
    before = (project.root / "idea/literature.jsonl").read_text(encoding="utf-8")
    resumed.ck.state, resumed.ck.scratch["idea"]["phase"] = S.IdeaDrafting, "fulltext"
    resumed._save()
    drive(resumed, S.Paused)
    assert len(calls) == 1
    assert (project.root / "idea/literature.jsonl").read_text(encoding="utf-8") == before


def test_only_selected_arxiv_is_requested(air_home, existing_pdf):
    selected = paper()
    unselected = selected.model_copy(update={"paper_id": "s2:unselected", "selected_for_reading": False})
    no_arxiv = selected.model_copy(update={"paper_id": "doi:10.1/no-arxiv", "ids": {"doi": "10.1/no-arxiv"}})
    calls = []
    project, engine, _ = make_engine(air_home, [selected, unselected, no_arxiv],
                                    lambda r: (calls.append(r) or httpx.Response(200, content=existing_pdf)))
    drive(engine, S.Paused)
    result = load_papers(project.root)
    assert len(calls) == 1 and result[unselected.paper_id].access_level == "abstract_only"
    report = json.loads((project.root / "idea/fulltext_summary.json").read_text(encoding="utf-8"))
    assert report["selected_count"] == 2 and report["abstract_or_metadata_count"] == 1


@pytest.mark.parametrize("mode", ["http", "timeout", "html", "broken_pdf"])
def test_failures_fall_back_to_abstract(air_home, mode):
    def handler(request):
        if mode == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(503 if mode == "http" else 200,
                              content=b"%PDF-1.7 broken" if mode == "broken_pdf" else b"<html>error</html>")
    p = paper()
    project, engine, _ = make_engine(air_home, [p], handler)
    drive(engine, S.Paused)
    result = load_papers(project.root)[p.paper_id]
    assert result.access_level == "abstract_only" and result.fulltext_path is None
    report = json.loads((project.root / "idea/fulltext_summary.json").read_text(encoding="utf-8"))
    assert report["papers"][0]["status"] == "failed" and report["full_text_count"] == 0


def test_download_disabled_and_invalid_id_do_not_connect(air_home):
    p = paper()
    bad = p.model_copy(update={"paper_id": "s2:bad", "ids": {"arxiv": "../../secret"}})
    calls = []
    project, engine, _ = make_engine(air_home, [p, bad], lambda r: calls.append(r), download=False)
    drive(engine, S.Paused)
    assert calls == []
    report = json.loads((project.root / "idea/fulltext_summary.json").read_text(encoding="utf-8"))
    assert {i["status"] for i in report["papers"]} == {"download_disabled", "invalid_arxiv_id"}


def test_redirect_is_checked_before_following(air_home):
    calls = []
    project, engine, _ = make_engine(air_home, [paper()], lambda r: (
        calls.append(str(r.url)) or httpx.Response(302, headers={"Location": "https://example.com/private"})))
    drive(engine, S.Paused)
    assert len(calls) == 1 and "arxiv.org" in calls[0]
    assert load_papers(project.root)[paper().paper_id].access_level == "abstract_only"


def test_allowed_redirect_and_wait(air_home, existing_pdf):
    clock = [1000]
    calls = []
    def handler(request):
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(302, headers={"Location": "https://arxiv.org/pdf/2106.09685v2"})
        return httpx.Response(200, content=existing_pdf)
    project, engine, _ = make_engine(air_home, [paper()], handler, clock=lambda: clock[0])
    engine.tick()
    assert isinstance(engine.tick().result, Wait)
    clock[0] += 3
    drive(engine, S.Paused)
    assert len(calls) == 2 and load_papers(project.root)[paper().paper_id].access_level == "full_text"


def test_missing_cached_text_is_rebuilt_without_download(air_home, existing_pdf):
    calls = []
    project, engine, _ = make_engine(air_home, [paper()],
                                    lambda r: (calls.append(r) or httpx.Response(200, content=existing_pdf)))
    drive(engine, S.Paused)
    record = load_papers(project.root)[paper().paper_id]
    (project.root / record.fulltext_path).unlink()
    engine.ck.state, engine.ck.scratch["idea"]["phase"] = S.IdeaDrafting, "fulltext"
    engine._save()
    drive(engine, S.Paused)
    assert (project.root / record.fulltext_path).exists() and len(calls) == 1


def test_bounded_download_and_permission(air_home):
    _, engine, stage = make_engine(air_home, [], lambda r: httpx.Response(200, content=b"x" * 100))
    wire = stage.fulltext_http
    with pytest.raises(SourceFailure, match="exceeds"):
        wire.get("https://arxiv.org/pdf/2106.09685", {}, interval=3, max_bytes=10)
    with pytest.raises(PermissionDenied):
        wire.get("https://example.com/private", {}, interval=3, max_bytes=10)
    assert engine.llm.backend.calls == []


def test_empty_selection_does_not_download(air_home):
    calls = []
    project, engine, _ = make_engine(air_home, [], lambda r: calls.append(r))
    drive(engine, S.Paused)
    report = json.loads((project.root / "idea/fulltext_summary.json").read_text(encoding="utf-8"))
    assert calls == [] and report["selected_count"] == 0


def test_section_excerpt_and_word_limit():
    sections = ["Abstract\n" + "abstract " * 1000,
                "1 Introduction\nFirst paragraph.\n\nSecond paragraph.\n\nLast paragraph.",
                "2 Related Work\n" + "background " * 5000,
                "3 Method\n" + "method " * 5000,
                "4 Experiments\n" + "experiment " * 5000,
                "5 Limitations\n" + "limits " * 1000]
    text, details = reading_excerpt("[p.1]\n" + "\n\n".join(sections))
    assert len(text.split()) <= 12000 and details["strategy"] == "section_headings_heuristic"
    assert "Last paragraph" in text and "5 Limitations" in text and "background background" not in text


def test_blank_pdf_is_not_full_text(monkeypatch):
    blank = SimpleNamespace(get_contents=lambda: None, extract_text=lambda: "")
    monkeypatch.setattr(pdf_text, "PdfReader", lambda *a: SimpleNamespace(is_encrypted=False, pages=[blank]))
    with pytest.raises(ValueError, match="scanned"):
        extract_pdf(b"%PDF-1.7")


def test_encrypted_pdf_is_not_readable(monkeypatch):
    monkeypatch.setattr(pdf_text, "PdfReader", lambda *a: SimpleNamespace(is_encrypted=True, decrypt=lambda p: 0))
    with pytest.raises(ValueError, match="encrypted"):
        extract_pdf(b"%PDF-1.7")


def test_small_caps_and_subsections_are_kept():
    source = ("ABSTRACT\n" + "abstract " * 1000 + "\n1 I NTRODUCTION\n" + "intro " * 1000 +
              "\n2 R ELATED WORK\n" + "background " * 6000 +
              "\n3 O UR METHOD\n3.1 L OW R ANK\nMETHOD_MARKER " + "method " * 4000 +
              "\n4 E MPIRICAL EXPERIMENTS\n4.1 B ASELINES\nEXPERIMENT_MARKER " + "experiment " * 4000 +
              "\n5 L IMITATIONS\nLIMIT_MARKER " + "limits " * 1000)
    excerpt, details = reading_excerpt(source)
    assert details["strategy"] == "section_headings_heuristic"
    assert all(marker in excerpt for marker in ("METHOD_MARKER", "EXPERIMENT_MARKER", "LIMIT_MARKER"))
    assert len(excerpt.split()) <= 12000


def test_tiny_section_match_falls_back_to_raw_text():
    source = "ABSTRACT\n" + "abstract " * 100 + "\n1 Related Work\n" + "body " * 13000
    excerpt, details = reading_excerpt(source)
    assert details["strategy"] == "leading_text_fallback"
    assert len(excerpt.split()) == 12000


def test_altered_cached_pdf_is_downloaded_again(air_home, existing_pdf):
    calls = []
    project, engine, _ = make_engine(air_home, [paper()],
                                    lambda r: (calls.append(r) or httpx.Response(200, content=existing_pdf)))
    drive(engine, S.Paused)
    report = json.loads((project.root / "idea/fulltext_summary.json").read_text(encoding="utf-8"))
    pdf_path = project.root / report["papers"][0]["pdf_path"]
    pdf_path.write_bytes(b"changed-cache")
    engine.ck.state, engine.ck.scratch["idea"]["phase"] = S.IdeaDrafting, "fulltext"
    engine._save()
    drive(engine, S.Paused)
    assert len(calls) == 2 and pdf_path.read_bytes() == existing_pdf
    assert load_papers(project.root)[paper().paper_id].access_level == "full_text"
