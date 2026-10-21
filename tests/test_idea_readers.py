"""Test reading cards, quote checks, concurrency, recovery, and gap references using simulated models."""

import json
import threading
import time

import pytest

from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.models.literature import CardItem, ReadingCard
from airesearcher.core.project import Project
from airesearcher.engine.engine import ProjectEngine
from airesearcher.engine.stage import Error
from airesearcher.llm.gateway import LLMGateway
from airesearcher.stages.idea.quotes import QuoteVerifier
from airesearcher.stages.idea.readers import FEASIBILITY_FIELDS, METHOD_FIELDS, card_id
from airesearcher.stages.idea.stage import IdeaStage
from airesearcher.stages.idea.storage import append_papers
from airesearcher.testing.fake_literature import FakeLiterature
from scripts.literature.demo import DemoLLM, demo_gateway

from .helpers import drive

SOURCE = ("We compare low rank adaptation with full fine tuning on a classification benchmark. "
          "Experiments report accuracy under limited training data.")
QUOTE = "We compare low rank adaptation with full fine tuning"


def output(fields, *, quote=QUOTE, location="abstract"):
    return {"items": [{"field": field, "content": "The paper compares the methods.", "kind": "author_claim",
                       "source_loc": location, "quote": quote, "quote_verified": True, "confidence": "high"}
                      for field in fields], "relevance_to_question": "Relevant comparison for the question."}


def scripts(full=False, mode="multi"):
    location = "p.1" if full else "abstract"
    return {"idea/methods_reader": [output(METHOD_FIELDS, location=location)],
            "idea/feasibility_reader": [output(FEASIBILITY_FIELDS, location=location)],
            "idea/single_reader": [output(METHOD_FIELDS + FEASIBILITY_FIELDS, location=location)],
            "idea/gap_analyst": [{"gaps": []}]}


def seed(air_home, *, count=1, full=False, mode="multi", script=None, concurrency=2, backend=None):
    project = Project.create(goal="Compare low resource fine tuning methods", task="tasks/smoke", home=air_home,
                             reading_mode=mode, limits={"reader_concurrency": concurrency},
                             dev={"stage_impl": {"idea": "real"}})
    base = FakeLiterature().get("arxiv:2106.09685")
    papers = [base.model_copy(update={"paper_id": f"s2:test{i}", "abstract": SOURCE,
                                      "selected_for_reading": True}, deep=True) for i in range(count)]
    gateway = LLMGateway(project, backend=backend, backoff_base=0) if backend else demo_gateway(
        project, script if script is not None else scripts(full, mode))
    engine = ProjectEngine(project, llm=gateway,
                           stages={"idea": IdeaStage(fulltext_download=False, stop_after="merge")},
                           retry_delay=0)
    if full:
        for i, paper in enumerate(papers):
            path = f"idea/fulltext/test{i}.reading.txt"
            project.archive.put(path, "literature", "[p.1]\n" + SOURCE)
            paper.access_level, paper.fulltext_path = "full_text", path
    append_papers(engine.build_context(), papers)
    engine.ck.scratch["idea"] = {"phase": "read"}
    engine._save()
    return project, engine, papers


def cards(project):
    return [ReadingCard.model_validate_json(p.read_text(encoding="utf-8"))
            for p in sorted((project.root / "idea/reading_cards").glob("*.json"))]


def test_two_roles_actual_source_and_trace(air_home):
    project, engine, papers = seed(air_home)
    drive(engine, S.Paused)
    result = cards(project)
    assert {c.role for c in result} == {"methods", "feasibility"}
    assert all(c.paper_id == papers[0].paper_id and c.based_on == "abstract_only" for c in result)
    assert all(c.call_id and c.model and c.prompt_version == 3 for c in result)
    assert all(i.quote_verified is True and i.kind == "author_claim" for c in result for i in c.items)
    assert engine.ck.scratch["idea"]["phase"] == "refine"
    assert not project.approvals.pending() and not (project.root / "idea/selected.md").exists()
    requests = [r for r in engine.llm.backend.calls if "_reader" in r.prompt_id]
    assert len(requests) == 2 and all("<untrusted_document" in str(r.messages) for r in requests)


def test_single_role_covers_both_field_sets(air_home):
    project, engine, _ = seed(air_home, mode="single")
    drive(engine, S.Paused)
    result = cards(project)
    assert len(result) == 1 and result[0].role == "single"
    assert {i.field for i in result[0].items} == set(METHOD_FIELDS + FEASIBILITY_FIELDS)


def test_fulltext_source_locations(air_home):
    project, engine, _ = seed(air_home, full=True)
    drive(engine, S.Paused)
    assert all(c.based_on == "full_text" for c in cards(project))
    assert all(i.quote_verified is True for c in cards(project) for i in c.items)


def test_invented_quote_is_downgraded(air_home):
    script = scripts(full=True)
    script["idea/methods_reader"] = [output(METHOD_FIELDS, quote="This claim is absent from the supplied text.",
                                            location="p.1")]
    project, engine, _ = seed(air_home, full=True, script=script)
    drive(engine, S.Paused)
    method = next(c for c in cards(project) if c.role == "methods")
    assert all(i.quote_verified is False and i.kind == "agent_inference" and i.confidence == "low"
               for i in method.items)


def test_missing_quote_cannot_be_author_claim(air_home):
    script = scripts()
    script["idea/methods_reader"] = [output(METHOD_FIELDS, quote=None)]
    project, engine, _ = seed(air_home, script=script)
    drive(engine, S.Paused)
    method = next(c for c in cards(project) if c.role == "methods")
    assert all(i.quote_verified is None and i.kind == "agent_inference" for i in method.items)


def test_failure_retry_once_then_continue(air_home):
    script = scripts()
    invalid = output(METHOD_FIELDS[:-1])
    script["idea/methods_reader"] = [invalid, output(METHOD_FIELDS)]
    project, engine, _ = seed(air_home, script=script)
    drive(engine, S.Paused)
    assert len(cards(project)) == 2
    assert sum(r.prompt_id == "idea/methods_reader" for r in engine.llm.backend.calls) == 2
    assert not json.loads((project.root / "idea/reading_status.json").read_text(encoding="utf-8"))["failed"]


@pytest.mark.parametrize("bad", ["fields", "abstract_location"])
def test_repeated_bad_output_is_recorded_as_failed(air_home, bad):
    script = scripts()
    if bad == "fields":
        result = output(METHOD_FIELDS[:-1])
    elif bad == "abstract_location":
        result = output(METHOD_FIELDS, location="Table 3")
    else:
        result = output(METHOD_FIELDS, quote="word " * 41)
    script["idea/methods_reader"] = [result]
    project, engine, _ = seed(air_home, script=script)
    drive(engine, S.Paused)
    report = json.loads((project.root / "idea/reading_status.json").read_text(encoding="utf-8"))
    assert report["done"] == 1 and len(report["failed"]) == 1
    assert report["failed"][0]["attempts"] == 2
    assert len(cards(project)) == 1


def test_saved_receipt_restores_card_without_reading_again(air_home):
    project, engine, _ = seed(air_home)
    engine.tick()
    missing = next((project.root / "idea/reading_cards").glob("*.json"))
    missing.unlink()
    engine.ck.scratch["idea"] = {"phase": "read"}
    engine._save()
    resumed = ProjectEngine(Project.open(project.root), llm=demo_gateway(project, {
        "idea/gap_analyst": [{"gaps": []}]}),
                           stages={"idea": IdeaStage(fulltext_download=False, stop_after="merge")},
                           retry_delay=0)
    drive(resumed, S.Paused)
    assert len(cards(project)) == 2 and missing.exists()
    assert [r.prompt_id for r in resumed.llm.backend.calls] == ["idea/gap_analyst", "idea/coordinator_merge"]


def test_concurrency_and_max_six_tasks_per_step(air_home):
    class Tracker(DemoLLM):
        def __init__(self):
            super().__init__(scripts())
            self.mutex = threading.Lock()
            self.active = self.peak = 0

        def call(self, req):
            with self.mutex:
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                time.sleep(0.03)
                return super().call(req)
            finally:
                with self.mutex:
                    self.active -= 1
    backend = Tracker()
    project, engine, _ = seed(air_home, count=4, backend=backend)
    engine.tick()
    assert len(backend.calls) == 6 and backend.peak == 2
    assert sum(t["status"] == "pending" for t in engine.ck.scratch["idea"]["read_tasks"]) == 2
    drive(engine, S.Paused)
    assert len(cards(project)) == 8 and backend.peak == 2


def test_missing_fulltext_falls_back_to_actual_abstract(air_home):
    project, engine, papers = seed(air_home, full=True, script=scripts())
    (project.root / papers[0].fulltext_path).unlink()
    drive(engine, S.Paused)
    assert all(c.based_on == "abstract_only" for c in cards(project))
    report = json.loads((project.root / "idea/reading_status.json").read_text(encoding="utf-8"))
    assert report["source_warnings"]


def test_no_text_does_not_call_model_or_fabricate_card(air_home):
    project, engine, papers = seed(air_home)
    papers[0].abstract = None
    append_papers(engine.build_context(), papers)
    drive(engine, S.Paused)
    assert not cards(project) and not engine.llm.backend.calls
    report = json.loads((project.root / "idea/reading_status.json").read_text(encoding="utf-8"))
    assert len(report["failed"]) == 2


def test_gap_unknown_evidence_is_rejected(air_home):
    script = scripts()
    script["idea/gap_analyst"] = [{"gaps": [{"gap_id": "G1", "description": "Candidate",
                                           "evidence": ["invented#0"], "status": "supported"}]}]
    project, engine, _ = seed(air_home, script=script)
    engine.tick()  # one reading batch
    engine.tick()  # finalize reading
    result = engine.tick().result
    assert isinstance(result, Error) and "nonexistent" in result.message
    assert not (project.root / "idea/gaps.json").exists()


@pytest.mark.parametrize("full,expected", [(False, "to_verify"), (True, "supported")])
def test_gap_support_requires_verified_fulltext_quote(air_home, full, expected):
    script = scripts(full)
    ref = card_id("s2:test0", "methods") + "#0"
    script["idea/gap_analyst"] = [{"gaps": [{"gap_id": "G1", "description": "Candidate to examine",
                                           "evidence": [ref], "status": "supported"}]}]
    project, engine, _ = seed(air_home, full=full, script=script)
    drive(engine, S.Paused)
    result = json.loads((project.root / "idea/gaps.json").read_text(encoding="utf-8"))
    assert result["gaps"][0]["status"] == expected


def test_bad_page_cannot_verify_quote():
    item = CardItem(field="method", content="Claim", kind="author_claim", source_loc="p.999",
                    quote=QUOTE, confidence="high")
    checked = QuoteVerifier("[p.1]\n" + SOURCE).check(item, "full_text")
    assert checked.quote_verified is False and checked.kind == "agent_inference"


def test_quote_normalization_and_contiguous_threshold():
    checker = QuoteVerifier("We use fine-\ntuning. ONE two three four five six seven eight nine ten.")
    assert checker.verify("fine-tuning")
    assert checker.verify("one two three four five six seven eight nine wrong")
    assert not checker.verify("one two three WRONG five six seven eight nine WRONG")
    assert not checker.verify("absent phrase")


def test_safe_card_filename():
    a, b = card_id("doi:10.1/a_b", "methods"), card_id("doi:10.1/a/b", "methods")
    assert a != b and "/" not in a and "\\" not in a and ":" not in a


def test_invalid_concurrency_fails_without_call(air_home):
    _, engine, _ = seed(air_home, concurrency=0)
    result = engine.tick().result
    assert isinstance(result, Error) and not result.retryable and not engine.llm.backend.calls


def test_oversized_quote_downgrades_item_without_losing_other_reading_fields(air_home):
    script = scripts()
    result = output(METHOD_FIELDS)
    result["items"][0]["quote"] = "word " * 41
    script["idea/methods_reader"] = [result]
    project, engine, _ = seed(air_home, script=script)
    drive(engine, S.Paused)
    method = next(c for c in cards(project) if c.role == "methods")
    item = method.items[0]
    assert item.quote is None and item.quote_verified is False
    assert item.kind == "agent_inference" and item.confidence == "low"
    assert "oversized quote discarded" in item.content
    assert len(cards(project)) == 2
    assert sum(r.prompt_id == "idea/methods_reader" for r in engine.llm.backend.calls) == 1
