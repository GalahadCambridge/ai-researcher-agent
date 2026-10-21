"""Exercise explicit, durable retry decisions through the real engine with simulated models."""

import json

import pytest

from airesearcher.core.errors import InvalidState
from airesearcher.core.fsutil import sha256_bytes
from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.project import Project
from airesearcher.engine.engine import ProjectEngine
from airesearcher.engine.stage import Continue, Error, Stop
from airesearcher.services.literature import load_papers
from airesearcher.stages.idea.dedup import PaperIdentity, same_paper
from airesearcher.stages.idea.evidence import load_json
from airesearcher.stages.idea.revision import incremental_step, prepare_incremental, revise_step, scoped_context
from airesearcher.stages.idea.selection import refine_step, round_path, score_step, selection_context
from airesearcher.stages.idea.stage import IdeaStage
from airesearcher.stages.idea.storage import save_json
from scripts.literature.demo import demo_gateway, simulated_selection

from .helpers import drive
from .test_idea_selection import ready


def prepared(air_home, phase):
    project, engine, ctx, _ = ready(air_home)
    state = engine.ck.scratch["idea"]
    if phase in {"score", "gap_table", "revise"}:
        assert isinstance(refine_step(ctx, state), Continue)
    if phase == "gap_table":
        assert isinstance(score_step(ctx, state), Continue)
    context = selection_context(ctx)
    if phase in {"score", "gap_table"}:
        context["idea"] = load_json(ctx, "idea/selected.json")
    if phase == "revise":
        state["revision_input"] = "idea/revision_inputs/test.json"
        request = {"request": {"comment": "Retain the scope and uncertainty."},
                   "original": load_json(ctx, "idea/selected.json")}
        save_json(ctx, state["revision_input"], request)
        context["revision_request"] = request
    state["phase"] = phase
    engine._stage_objects["idea"] = IdeaStage(fulltext_download=False)
    engine._save()
    return project, engine, ctx, state, context


def blocked(air_home, phase, failure):
    project, engine, ctx, state, context = prepared(air_home, phase)
    records = ([{"status": "started"}] if failure == "interrupted" else
               [{"status": "failed", "error": "Prior failure 1"},
                {"status": "failed", "error": "Prior failure 2"}])
    path = round_path(state, phase + "_attempts")
    original = {"input_sha256": sha256_bytes(json.dumps(context, ensure_ascii=False, sort_keys=True).encode()),
                "attempts": records}
    ref = save_json(ctx, path, original)
    engine.request_action("resume")
    count = len(engine.llm.backend.calls)
    drive(engine, S.Paused)
    assert len(engine.llm.backend.calls) == count
    question = project.questions.pending()
    assert question and {o.id for o in question.options} == {"retry", "end"}
    assert next(o for o in question.options if o.id == "end").default
    assert not next(o for o in question.options if o.id == "retry").default
    return project, engine, path, ref, original, question


@pytest.mark.parametrize("phase", ["refine", "score", "gap_table", "revise"])
@pytest.mark.parametrize("failure", ["interrupted", "exhausted"])
def test_retry_answer_survives_restart_and_reaches_pending(air_home, phase, failure):
    project, engine, path, old_ref, original, question = blocked(air_home, phase, failure)
    before = len(engine.llm.backend.calls)
    for _ in range(3):
        engine.tick()
    assert len(engine.llm.backend.calls) == before
    with pytest.raises(InvalidState):
        engine.request_action("resume")
    restarted = ProjectEngine(Project.open(project.root), llm=demo_gateway(project),
                              stages={"idea": IdeaStage(fulltext_download=False)}, retry_delay=0)
    restarted.tick()
    assert restarted.state == S.Paused and not restarted.llm.backend.calls
    project.questions.answer(question.question_id, "retry", request_id="retry-after-restart")
    budget = project.config.budget["llm_usd"].hard
    drive(restarted, S.IdeaPending)
    receipt = load_json(restarted.build_context(), path)
    assert receipt["window_start"] == len(original["attempts"])
    assert receipt["attempts"][-1]["status"] == "done"
    assert receipt["attempts"][0]["status"] == ("interrupted" if failure == "interrupted" else "failed")
    assert receipt["retry_windows"][0]["answer"]["question_id"] == question.question_id
    assert json.loads(project.archive.get(old_ref)) == original
    assert project.config.budget["llm_usd"].hard == budget
    assert sum(r.prompt_id == f"idea/{phase}" for r in restarted.llm.backend.calls) == 1
    after = len(restarted.llm.backend.calls)
    restarted.tick()
    assert len(restarted.llm.backend.calls) == after


@pytest.mark.parametrize("failure", ["interrupted", "exhausted"])
def test_end_answer_performs_no_more_model_calls(air_home, failure):
    project, engine, _, _, _, question = blocked(air_home, "refine", failure)
    before = len(engine.llm.backend.calls)
    project.questions.answer(question.question_id, "end", request_id="end-selection")
    drive(engine, S.Failed)
    assert len(engine.llm.backend.calls) == before
    assert not project.approvals.pending()


def test_new_retry_window_can_fail_and_be_retried_again(air_home):
    project, engine, path, _, original, question = blocked(air_home, "refine", "exhausted")
    bad = simulated_selection(selection_context(engine.build_context()))
    bad["idea"]["metrics"][0]["name"] = "invalid_metric"
    engine.llm.backend.script["idea/refine"] = [bad]
    engine.llm.backend._fixed_prompts.add("idea/refine")
    before = len(engine.llm.backend.calls)
    project.questions.answer(question.question_id, "retry", request_id="first-window")
    drive(engine, S.Paused)
    assert len(engine.llm.backend.calls) == before + 2
    second = project.questions.pending()
    assert second.question_id != question.question_id
    engine.llm.backend._fixed_prompts.discard("idea/refine")
    project.questions.answer(second.question_id, "retry", request_id="second-window")
    drive(engine, S.IdeaPending)
    attempts = load_json(engine.build_context(), path)
    assert len(attempts["retry_windows"]) == 2
    assert len(attempts["attempts"]) == len(original["attempts"]) + 3


def test_completed_attempt_is_recovered_without_repeating_model_call(air_home):
    _, engine, ctx, state, _ = prepared(air_home, "score")
    initial = load_json(ctx, round_path(state, "refine_attempts"))
    state.update(selection_round="completed-recovery", phase="refine")
    save_json(ctx, round_path(state, "refine_attempts"), initial)
    before = len(engine.llm.backend.calls)
    assert isinstance(refine_step(ctx, state), Continue)
    assert len(engine.llm.backend.calls) == before
    assert ctx.archive.latest(round_path(state, "refine"))


def test_blank_revision_queries_are_repaired_or_ask_for_retry(air_home):
    _, engine, ctx, state, _ = prepared(air_home, "revise")
    output = simulated_selection(selection_context(ctx))
    output.update(needs_more_literature=True, new_queries=[" ", "\t"])
    engine.llm.backend.script["idea/revise"] = [output]
    engine.llm.backend._fixed_prompts.add("idea/revise")
    result = revise_step(ctx, state)
    assert isinstance(result, Stop) and result.question is not None
    assert sum(r.prompt_id == "idea/revise" for r in engine.llm.backend.calls) == 2
    assert not ctx.archive.latest(round_path(state, "revise"))


def test_legacy_blank_queries_return_controlled_error_and_compact_exclusions(air_home):
    project, engine, ctx, state, _ = prepared(air_home, "revise")
    state.update(selection_round="incremental", revision_queries=[" ", "\t"],
                 revision_literature={"phase": "search"})
    result = incremental_step(ctx, state, engine._stage_objects["idea"])
    assert isinstance(result, Error) and "nonblank" in result.message
    state["revision_queries"] = [" LoRA ", "LoRA"]
    scoped = scoped_context(ctx, state)
    prepare_incremental(ctx, scoped, state)
    identities = state["revision_literature"]["exclude_papers"]
    assert identities and all(set(record) == {"paper_id", "ids", "title", "year"} for record in identities)
    original = project.root / "idea/literature.jsonl"
    original_bytes = original.read_bytes()
    old = next(iter(load_papers(project.root).values()))
    assert same_paper(old, PaperIdentity.model_validate(identities[0]))
    prepare_incremental(ctx, scoped, state)
    assert state["revision_literature"]["exclude_papers"] is identities
    assert original.read_bytes() == original_bytes
