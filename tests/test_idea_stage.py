"""Offline literature-stage integration tests for parsing, retrieval, deduplication, recovery, failures, and limits."""

import json

from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.project import Project
from airesearcher.engine.engine import ProjectEngine
from airesearcher.engine.stage import Error
from airesearcher.services.literature import load_papers
from airesearcher.stages import load_stage
from airesearcher.stages.idea.stage import IdeaStage
from airesearcher.testing.fake_literature import FakeLiterature
from airesearcher.testing.fake_llm import fake_gateway
from scripts.literature.demo import demo_gateway

from .helpers import drive


def make_project(air_home, **kwargs):
    return Project.create(goal=(
        "Compare LoRA and full fine-tuning for low-resource text classification"
    ), task="tasks/smoke", home=air_home,
                          dev={"stage_impl": {"idea": "real"}}, literature={"year_from": 2018}, **kwargs)


def make_engine(project, source, script=None):
    return ProjectEngine(project, llm=fake_gateway(project, script) if script else demo_gateway(project),
                         stages={"idea": IdeaStage(source, fulltext_download=False, stop_after="merge")}, retry_delay=0)


def test_parse_search_and_dedup(air_home):
    project = make_project(air_home, constraints={"device": "cpu"})
    assert isinstance(load_stage("idea", project.config), IdeaStage)
    source = FakeLiterature()
    engine = make_engine(project, source)
    drive(engine, S.Paused)
    brief = json.loads((project.root / "idea/brief.json").read_text(encoding="utf-8"))
    assert brief["constraints"]["device"] == "cpu" and brief["task"] == "tasks/smoke"
    assert brief["missing_info"]
    papers = load_papers(project.root)
    assert papers and len(papers) <= 20
    assert len(source.calls) == 3
    assert all(p.access_level != "full_text" for p in papers.values())
    assert all(p.paper_id in source.papers for p in papers.values())
    bert = [p for p in papers.values() if "BERT: Pre-training" in p.title]
    assert len(bert) == 1 and bert[0].venue_type == "conference" and bert[0].same_as
    assert len(list((project.root / "idea/search_snapshots").glob("*.json"))) == 3
    for path in (project.root / "idea/search_snapshots").glob("*.json"):
        assert json.loads(path.read_text(encoding="utf-8"))["raw_response"]["fake"] is True
    assert engine.ck.scratch["idea"]["phase"] == "refine"
    assert sum(p.selected_for_reading for p in papers.values()) == 8
    assert all(p.relevance for p in papers.values())
    assert not project.approvals.pending() and not (project.root / "idea/selected.md").exists()


def test_restart_reuses_saved_batch_and_model_outputs(air_home):
    project = make_project(air_home)
    source = FakeLiterature()
    engine = make_engine(project, source)
    for _ in range(3):  # parse, plan, first query
        engine.tick()
    assert len(source.calls) == 1
    # Simulate restart after writing a receipt, with older scratch progress.
    engine.ck.scratch["idea"] = {"phase": "parse"}
    engine._save()
    resumed = make_engine(Project.open(project.root), source)
    drive(resumed, S.Paused)
    assert len(source.calls) == len(set(source.calls)) == 3
    assert all(req.prompt_id not in {"idea/parse_input", "idea/plan_queries"}
               for req in resumed.llm.backend.calls)
    assert project.archive.latest("idea/brief.json").version == 1
    text = (project.root / "idea/literature.jsonl").read_text(encoding="utf-8")
    # A repeated finalization must not append identical records or create another version.
    resumed.ck.state = S.IdeaDrafting
    resumed.ck.scratch["idea"]["phase"] = "search"
    resumed._save()
    drive(resumed, S.Paused)
    assert (project.root / "idea/literature.jsonl").read_text(encoding="utf-8") == text
    assert len(source.calls) == 3
    assert project.archive.latest("idea/literature.jsonl").version == 2


def test_failure_does_not_advance_or_fabricate(air_home):
    project = make_project(air_home)
    source = FakeLiterature(fail_queries={"LoRA fine tuning"})
    engine = make_engine(project, source)
    engine.tick()
    engine.tick()
    tick = engine.tick()
    assert isinstance(tick.result, Error) and tick.result.retryable
    assert engine.ck.scratch["idea"]["phase"] == "search"
    assert not (project.root / "idea/literature.jsonl").exists()
    source.fail_queries.clear()
    drive(engine, S.Paused)
    assert load_papers(project.root)


def test_empty_results_are_not_invented(air_home):
    project = make_project(air_home)
    source = FakeLiterature()
    source.fixed = dict.fromkeys(["LoRA fine tuning", "BERT pretraining", "low resource text classification"], [])
    engine = make_engine(project, source)
    drive(engine, S.Paused)
    assert load_papers(project.root) == {}
    assert engine.ck.scratch["idea"]["paper_count"] == 0


def test_total_limit_after_dedup(air_home):
    project = make_project(air_home, limits={"search_papers": 2})
    engine = make_engine(project, FakeLiterature())
    drive(engine, S.Paused)
    assert 0 < len(load_papers(project.root)) <= 2


def test_replay_missing_snapshot_is_explicit(air_home, monkeypatch):
    from airesearcher.stages.idea.sources.replay import ReplaySource

    project = make_project(air_home)
    monkeypatch.setattr(IdeaStage, "_source", lambda self, ctx, state: ReplaySource(project.root / "missing"))
    engine = make_engine(project, None)
    engine.tick()
    engine.tick()
    result = engine.tick().result
    assert isinstance(result, Error) and "Replay snapshot missing for query" in result.message
    assert not (project.root / "idea/literature.jsonl").exists()
