"""Exercise configured and cumulative literature caps without network or paid calls."""

from dataclasses import replace

import pytest
from pydantic import ValidationError

from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.models.project import Limits
from airesearcher.core.project import Project
from airesearcher.engine.stage import Continue, Error, Stop
from airesearcher.services.literature import load_papers
from airesearcher.stages.idea.revision import incremental_step, prepare_incremental, scoped_context
from airesearcher.stages.idea.storage import append_papers, save_json
from airesearcher.testing.fake_literature import FakeLiterature
from scripts.literature.demo import demo_responses

from .helpers import drive
from .test_idea_screening import records
from .test_idea_stage import make_engine, make_project


@pytest.mark.parametrize("field,bad", [("search_papers", 0), ("search_papers", 21),
                                     ("read_papers", -1), ("read_papers", 21),
                                     ("search_papers", True), ("read_papers", 2.5)])
def test_invalid_paper_configuration_is_rejected(field, bad):
    with pytest.raises(ValidationError):
        Limits.model_validate({field: bad})


def test_twenty_papers_are_supported_but_twenty_first_is_never_written(air_home):
    project = make_project(air_home, limits={"search_papers": 20, "read_papers": 20})
    engine = make_engine(project, FakeLiterature())
    ctx = engine.build_context()
    papers = records(21)
    append_papers(ctx, papers[:20])
    original = (project.root / "idea/literature.jsonl").read_bytes()
    ref = project.archive.latest("idea/literature.jsonl")
    with pytest.raises(ValueError, match="at most 20"):
        append_papers(ctx, papers)
    assert (project.root / "idea/literature.jsonl").read_bytes() == original
    assert project.archive.latest("idea/literature.jsonl") == ref
    papers[0].relevance = {"score": 4}
    append_papers(ctx, [papers[0]])
    assert len(load_papers(project.root)) == 20


class ManyPapers:
    name = "many-fixture-papers"

    def __init__(self):
        self.calls = []

    def search(self, query, *, year_from=None, limit=20):
        self.calls.append((query, limit))
        snapshot, _ = FakeLiterature().search(query, year_from=year_from, limit=limit)
        return snapshot, records(30)


def inputs(ctx):
    save_json(ctx, "idea/brief.json", demo_responses()["idea/parse_input"][0])
    save_json(ctx, "idea/queries.json", demo_responses()["idea/plan_queries"][0])


def search_to_screen(stage, ctx, state):
    for _ in range(6):
        result = stage._search(ctx, state)
        assert isinstance(result, Continue)
        if state["phase"] == "screen":
            return
    pytest.fail("Search did not terminate within the bounded fixture query plan")


def test_multiple_queries_cannot_retain_more_than_twenty_even_if_source_overreturns(air_home):
    project = make_project(air_home)
    source = ManyPapers()
    engine = make_engine(project, source)
    ctx = engine.build_context()
    inputs(ctx)
    state = {"phase": "search"}
    stage = engine._stage_objects["idea"]
    search_to_screen(stage, ctx, state)
    assert len(load_papers(project.root)) == 20
    assert len(source.calls) == 3 and all(limit == 20 for _, limit in source.calls)
    before = len(source.calls)
    state["phase"] = "search"
    search_to_screen(stage, ctx, state)
    assert len(source.calls) == before and len(load_papers(project.root)) == 20


def test_incremental_search_uses_only_the_remaining_project_capacity(air_home):
    project = make_project(air_home)
    source = ManyPapers()
    engine = make_engine(project, source)
    ctx = engine.build_context()
    inputs(ctx)
    append_papers(ctx, records(19))
    state = {"selection_round": "remaining-slot", "revision_queries": ["revision evidence"],
             "revision_literature": {"phase": "search"}}
    scoped = scoped_context(ctx, state)
    prepare_incremental(ctx, scoped, state)
    search_to_screen(engine._stage_objects["idea"], scoped, state["revision_literature"])
    assert len(load_papers(scoped.root)) == 1
    assert all(limit == 20 for _, limit in source.calls)  # Candidate retrieval may include old papers.
    append_papers(ctx, load_papers(scoped.root).values())
    assert len(load_papers(project.root)) == 20


def test_full_project_pauses_and_requires_explicit_acceptance_of_existing_evidence(air_home):
    project = make_project(air_home)
    source = ManyPapers()
    engine = make_engine(project, source)
    ctx = engine.build_context()
    append_papers(ctx, records(20))
    save_json(ctx, "idea/selected.json", {"open_questions": []})
    state = {"phase": "revision_search", "selection_round": "cap-decision",
             "revision_queries": ["new evidence"], "revision_literature": {"phase": "search"}}
    engine.ck.scratch["idea"] = state
    engine._save()
    drive(engine, S.Paused)
    question = project.questions.pending()
    assert question and {o.id for o in question.options} == {"use_existing", "end"}
    assert not source.calls and not engine.llm.backend.calls
    project = Project.open(project.root)
    resumed = make_engine(project, source)
    project.questions.answer(question.question_id, "use_existing", request_id="accept-literature-cap")
    for _ in range(5):
        resumed.tick()
        if resumed.ck.scratch["idea"]["phase"] == "score":
            break
    assert resumed.ck.scratch["idea"]["phase"] == "score"
    assert "request remains unverified" in (project.root / "idea/selected.json").read_text(encoding="utf-8")
    assert not source.calls and not resumed.llm.backend.calls
    assert project.archive.latest("idea/literature_limit_decisions/cap-decision.json")


def test_automatic_default_cannot_accept_missing_new_evidence(air_home):
    from airesearcher.core.fsutil import now
    from airesearcher.core.models.question import Answer

    project = make_project(air_home)
    engine = make_engine(project, ManyPapers())
    ctx = engine.build_context()
    append_papers(ctx, records(20))
    state = {"selection_round": "cap", "revision_literature": {"phase": "search"},
             "asked": "q-cap", "literature_cap_waiting": True}
    ctx = replace(ctx, answer=Answer(question_id="q-cap", choice="use_existing",
                                    answered_by="auto-default(eval)", answered_at=now()))
    result = incremental_step(ctx, state, engine._stage_objects["idea"])
    assert isinstance(result, Stop) and result.question
    assert not engine.llm.backend.calls


def test_oversized_frozen_reading_input_is_rejected_before_dispatch(air_home):
    from airesearcher.stages.idea.readers import read_step

    project = make_project(air_home)
    engine = make_engine(project, FakeLiterature())
    ctx = engine.build_context()
    save_json(ctx, "idea/reading_input.json", {"goal": project.config.goal, "reading_mode": "multi",
                                              "papers": [{"paper_id": f"s2:p{i}"} for i in range(21)]})
    result = read_step(ctx, {})
    assert isinstance(result, Error) and not result.retryable and "20-paper limit" in result.message
    assert not engine.llm.backend.calls


def test_legacy_oversized_registry_is_rejected_before_fulltext_fetch(air_home):
    from airesearcher.stages.idea.fulltext import fulltext_step

    project = make_project(air_home)
    engine = make_engine(project, FakeLiterature())
    ctx = engine.build_context()
    ctx.archive.put("idea/literature.jsonl", "literature", "".join(p.model_dump_json() + "\n" for p in records(21)))
    result = fulltext_step(ctx, {}, download=True)
    assert isinstance(result, Error) and not result.retryable and "20-paper limit" in result.message
    assert not engine.llm.backend.calls


def test_full_initial_registry_does_not_issue_more_search_requests(air_home):
    project = make_project(air_home)
    source = ManyPapers()
    engine = make_engine(project, source)
    ctx = engine.build_context()
    inputs(ctx)
    append_papers(ctx, records(20))
    state = {"phase": "search"}
    result = engine._stage_objects["idea"]._search(ctx, state)
    assert isinstance(result, Continue) and state["phase"] == "screen"
    assert not source.calls and not engine.llm.backend.calls
