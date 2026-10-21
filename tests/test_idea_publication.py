"""Test final handoff, exact-version approvals, revision recovery, and incremental reading with fake models."""


from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.models.idea import parse_selected_md
from airesearcher.core.project import Project
from airesearcher.engine.engine import ProjectEngine
from airesearcher.engine.stage import Error
from airesearcher.stages.idea.evidence import load_json
from airesearcher.stages.idea.publication import submit_step, write_step
from airesearcher.stages.idea.selection import selection_context
from airesearcher.stages.idea.stage import IdeaStage
from airesearcher.testing.fake_literature import FakeLiterature
from scripts.literature.demo import demo_gateway, simulated_selection

from .helpers import approve_pending, drive


def pending(air_home):
    project = Project.create(goal="Validate the LoRA literature and idea workflow", task="tasks/smoke",
                             home=air_home, dev={"stage_impl": {"idea": "real"}}, llm_cache="record",
                             literature={"year_from": 2018}, limits={"read_papers": 2})
    source = FakeLiterature()
    engine = ProjectEngine(project, llm=demo_gateway(project),
                           stages={"idea": IdeaStage(source, fulltext_download=False)}, retry_delay=0)
    drive(engine, S.IdeaPending)
    return project, engine, source


def test_english_handoff_roundtrips_and_waits_for_human_approval(air_home):
    project, engine, _ = pending(air_home)
    text = (project.root / "idea/selected.md").read_text(encoding="utf-8")
    idea = parse_selected_md(text)
    assert idea.model_dump(mode="json") == load_json(engine.build_context(), "idea/selected.json")
    assert "## Research Question" in text and "Pending human review" in text
    assert "TASK FIT TO VERIFY" in text
    [approval] = project.approvals.pending()
    assert approval.target == project.archive.latest("idea/selected.md")
    assert "TASK FIT" in approval.summary and "feasibility" in approval.summary
    assert {r.artifact_id for r in approval.extra_refs} >= {
        "idea/scores.json", "idea/gap_table.md", "idea/reading_summary.json", "idea/conflicts.json",
    }
    assert not engine.ck.approved and not (project.root / "plan/experiment_plan.md").exists()


def test_write_and_submit_reentry_do_not_duplicate_versions_or_calls(air_home):
    project, engine, _ = pending(air_home)
    ctx = engine.build_context()
    count = len(engine.llm.backend.calls)
    write_step(ctx, engine.ck.scratch["idea"])
    result = submit_step(ctx, engine.ck.scratch["idea"])
    assert result.target == project.archive.latest("idea/selected.md")
    assert result.target.version == 1 and len(engine.llm.backend.calls) == count
    assert project.archive.latest("idea/validation_report.json").version == 1


def test_supporting_artifact_changes_block_submission(air_home):
    project, engine, _ = pending(air_home)
    project.archive.put("idea/gap_table.md", "gap_table", "Changed after validation.")
    result = submit_step(engine.build_context(), engine.ck.scratch["idea"])
    assert isinstance(result, Error) and "changed after" in result.message


def test_unregistered_document_edits_block_submission(air_home):
    project, engine, _ = pending(air_home)
    path = project.root / "idea/selected.md"
    path.write_text(path.read_text(encoding="utf-8") + "\nUnregistered edit.\n", encoding="utf-8")
    result = submit_step(engine.build_context(), engine.ck.scratch["idea"])
    assert isinstance(result, Error) and "unregistered" in result.message


def test_changes_requested_create_a_new_version_without_rereading(air_home):
    project, engine, source = pending(air_home)
    previous = project.archive.latest("idea/selected.md")
    old_text = project.archive.read_text(previous)
    old_reads = [r for r in engine.llm.backend.calls if r.prompt_id.endswith("_reader")]
    queries = list(source.calls)
    approve_pending(project, "changes_requested", "Compare a smaller rank and retain the uncertainty notes.")
    drive(engine, S.IdeaPending)
    latest = project.archive.latest("idea/selected.md")
    assert latest.version == 2 and project.archive.read_text(previous) == old_text
    assert "Compare a smaller rank" in project.archive.read_text(latest)
    assert source.calls == queries
    assert len([r for r in engine.llm.backend.calls if r.prompt_id.endswith("_reader")]) == len(old_reads)
    assert sum(r.prompt_id == "idea/revise" for r in engine.llm.backend.calls) == 1
    assert any(r.sha256 == previous.sha256 for r in project.archive.version_info(latest).parents)


def test_rejected_idea_waits_for_redo_choice_then_revises(air_home):
    project, engine, _ = pending(air_home)
    approve_pending(project, "rejected", "Rewrite the proposal as an explicitly simulated workflow.")
    drive(engine, S.Paused)
    question = project.questions.pending()
    assert question and project.archive.latest("idea/selected.md").version == 1
    project.questions.answer(question.question_id, "redo", request_id="redo-simulated-test")
    drive(engine, S.IdeaPending)
    assert project.archive.latest("idea/selected.md").version == 2


def test_revision_restart_reuses_receipt_and_keeps_feedback(air_home):
    project, engine, source = pending(air_home)
    approve_pending(project, "changes_requested", "Keep the draft narrow.")
    for _ in range(8):
        engine.tick()
        if engine.ck.scratch["idea"]["phase"] == "score":
            break
    assert engine.ck.scratch["idea"]["phase"] == "score"
    resumed = ProjectEngine(Project.open(project.root), llm=demo_gateway(project),
                            stages={"idea": IdeaStage(source, fulltext_download=False)}, retry_delay=0)
    drive(resumed, S.IdeaPending)
    assert not any(r.prompt_id == "idea/revise" for r in resumed.llm.backend.calls)
    assert "Keep the draft narrow" in project.archive.read_text(project.archive.latest("idea/selected.md"))


def test_more_literature_reads_only_new_papers_and_preserves_previous_cards(air_home):
    project, engine, source = pending(air_home)
    ctx = engine.build_context()
    originals = {p.name: p.read_bytes() for p in (project.root / "idea/reading_cards").glob("*.json")}
    old_read_calls = len([r for r in engine.llm.backend.calls if r.prompt_id.endswith("_reader")])
    new_id = "arxiv:9999.12345"
    sample = dict(source.papers["arxiv:2106.09685"])
    sample.update(paper_id=new_id, ids={"arxiv": "9999.12345"}, title="New evidence fixture for feedback",
                  citation_key="fixture2026feedback", year=2026)
    source.papers[new_id] = sample
    source.fixed["revision evidence"] = ["arxiv:2106.09685", new_id]
    revised = simulated_selection(selection_context(ctx))
    revised.update(needs_more_literature=True, new_queries=["revision evidence"])
    engine.llm.backend.script["idea/revise"] = [revised]
    engine.llm.backend._fixed_prompts.add("idea/revise")
    approve_pending(project, "changes_requested", "Retrieve evidence for the newly specified condition.")
    drive(engine, S.IdeaPending, max_ticks=150, timeout=120)
    assert project.archive.latest("idea/selected.md").version == 2
    assert source.calls.count("revision evidence") == 1
    assert len([r for r in engine.llm.backend.calls if r.prompt_id.endswith("_reader")]) == old_read_calls + 2
    assert all((project.root / "idea/reading_cards" / name).read_bytes() == text
               for name, text in originals.items())
    reading = load_json(engine.build_context(), "idea/reading_input.json")
    assert len({p["paper_id"] for p in reading["papers"]}) == 3
    assert new_id in load_json(engine.build_context(), "idea/selected.json")["citations"]
    assert list((project.root / "idea/revision_literature").glob("*/idea/merge_input.json"))


def test_pending_restart_does_not_generate_new_calls(air_home):
    project, _, source = pending(air_home)
    resumed = ProjectEngine(Project.open(project.root), llm=demo_gateway(project),
                            stages={"idea": IdeaStage(source, fulltext_download=False)}, retry_delay=0)
    resumed.tick()
    assert resumed.state == S.IdeaPending and not resumed.llm.backend.calls
    assert len(project.approvals.pending()) == 1


def test_selected_document_lineage_contains_all_validated_inputs(air_home):
    project, _, _ = pending(air_home)
    info = project.archive.version_info(project.archive.latest("idea/selected.md"))
    parents = {r.artifact_id for r in info.parents}
    assert parents >= {"idea/selected.json", "idea/scores.json", "idea/gap_table.md",
                       "idea/reading_summary.json", "idea/conflicts.json", "idea/literature.jsonl"}


def test_cached_recovery_reuses_paid_reading_without_calling_backend(air_home):
    from scripts.literature.recovery import finish_replay, seed_cached_run
    original, _, source = pending(air_home)
    target = Project.create(goal=original.config.goal, task=original.config.task,
                            root=air_home / "recovered", register=False,
                            dev={"stage_impl": {"idea": "real"}}, literature={"year_from": 2018},
                            limits={"read_papers": 2})
    seed_cached_run(target, original.root)
    assert not target.archive.latest("idea/selected.md")
    assert target.config.llm_cache == "replay"
    assert target.config.prompt_pins["idea/methods_reader"] == 3
    engine = ProjectEngine(target, llm=demo_gateway(target),
                           stages={"idea": IdeaStage(source, fulltext_download=False)}, retry_delay=0)
    for _ in range(50):
        engine.tick()
        if engine.ck.scratch.get("idea", {}).get("phase") == "gap":
            break
    assert engine.ck.scratch["idea"]["phase"] == "gap"
    assert not engine.llm.backend.calls
    assert load_json(engine.build_context(), "idea/reading_status.json")["done"] == 4
    assert not load_json(engine.build_context(), "idea/reading_status.json")["failed"]
    finish_replay(target)
    assert target.config.llm_cache == "record" and not target.config.prompt_pins
