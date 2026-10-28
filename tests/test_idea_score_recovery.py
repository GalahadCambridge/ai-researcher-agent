"""Test closed score references and durable retries using simulated models only."""

import json

import pytest

from airesearcher.core.fsutil import sha256_bytes
from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.project import Project
from airesearcher.engine.engine import ProjectEngine
from airesearcher.engine.stage import Continue, Stop
from airesearcher.stages.idea.evidence import material
from airesearcher.stages.idea.models import ScoreOutput
from airesearcher.stages.idea.scoring import decode_score, score_variables
from airesearcher.stages.idea.selection import normalize_scores, round_path, score_step, selection_context
from airesearcher.stages.idea.stage import IdeaStage
from airesearcher.stages.idea.storage import save_json
from scripts.literature.demo import demo_gateway

from .helpers import drive
from .test_idea_retry import prepared
from .test_idea_selection import scores


def load(project, path):
    return json.loads((project.root / path).read_text(encoding="utf-8"))


def test_score_schema_lists_only_existing_literature_and_allows_empty_evidence():
    context = {"literature": {"items": [{"ref": "card__methods#0"}], "papers": [{"paper_id": "s2:p"}]}}
    variables = score_variables(ScoreOutput, context, "")
    shape = json.loads(variables["schema_json"])
    evidence = shape["$defs"]["StrictDimension"]["properties"]["evidence"]
    assert evidence["items"]["enum"] == ["card__methods#0", "s2:p"]
    assert json.loads(variables["example_json"])["feasibility"]["evidence"] == []
    variables = score_variables(ScoreOutput, {"literature": {"items": [], "papers": []}}, "")
    assert json.loads(variables["schema_json"])["$defs"]["StrictDimension"]["properties"]["evidence"]["maxItems"] == 0


def test_context_path_failures_are_reported_together_and_repaired_once(air_home):
    project, engine, ctx, state, _ = prepared(air_home, "score")
    bad = scores()
    bad["novelty"]["evidence"] = ["literature#G1", "literature#gaps[0]"]
    bad["feasibility"]["evidence"] = ["task#methods_available", "task.resources"]
    engine.llm.backend.script["idea/score"] = [bad, scores()]
    engine.llm.backend._fixed_prompts.add("idea/score")
    assert isinstance(score_step(ctx, state), Continue)
    assert state["phase"] == "score" and not ctx.archive.latest("idea/scores.json")
    attempt_path = round_path(state, "score_attempts")
    first = load(project, attempt_path)["attempts"][0]
    assert all(ref in first["error"] for ref in ["literature#G1", "literature#gaps[0]", "task#methods_available"])
    raw = load(project, first["response_path"])
    assert json.loads(raw["text"]) == bad and raw["finish_reason"] == "stop"
    assert isinstance(score_step(ctx, state), Continue) and state["phase"] == "gap_table"
    calls = [c for c in engine.llm.backend.calls if c.prompt_id == "idea/score"]
    assert len(calls) == 2 and all(c.schema_name is None for c in calls)
    assert "task#methods_available" in calls[1].messages[1]["content"]
    assert "previous_invalid_score" in calls[1].messages[1]["content"]
    assert all(load(project, "idea/scores.json")[name]["to_verify"] for name in scores())


@pytest.mark.parametrize("bad", ['{"novelty":', scores(["task#data"])])
def test_failed_retry_opens_a_new_window_and_displays_current_errors(air_home, bad):
    project, engine, _, _, _ = prepared(air_home, "score")
    engine.llm.backend.script["idea/score"] = [bad]
    engine.llm.backend._fixed_prompts.add("idea/score")
    engine.request_action("resume")
    drive(engine, S.Paused)
    first = project.questions.pending()
    assert "2 total attempt(s)" in first.text and "Latest errors:" in first.text
    assert "Recorded project model spend" in first.text
    assert len([c for c in engine.llm.backend.calls if c.prompt_id == "idea/score"]) == 2
    original = load(project, "idea/selection_rounds/initial/score_attempts.json")
    original_bytes = {a["response_path"]: (project.root / a["response_path"]).read_bytes()
                      for a in original["attempts"]}
    project.questions.answer(first.question_id, "retry", request_id="score-window-retry")
    drive(engine, S.Paused)
    second = project.questions.pending()
    assert second.question_id != first.question_id and "4 total attempt(s)" in second.text
    saved = load(project, "idea/selection_rounds/initial/score_attempts.json")
    assert saved["window_start"] == 2 and len(saved["retry_windows"]) == 1
    assert len(saved["attempts"]) == 4
    assert all((project.root / p).read_bytes() == b for p, b in original_bytes.items())
    before = len(engine.llm.backend.calls)
    engine.tick()
    assert len(engine.llm.backend.calls) == before


def test_saved_raw_score_recovers_after_restart_without_another_score_call(air_home):
    project, engine, ctx, state, _ = prepared(air_home, "score")
    context = {**selection_context(ctx), "idea": load(project, "idea/selected.json")}
    digest = sha256_bytes(json.dumps(context, ensure_ascii=False, sort_keys=True).encode())
    response_path = round_path(state, "score_responses/attempt-0001")
    save_json(ctx, round_path(state, "score_attempts"), {"input_sha256": digest, "attempts": [
        {"status": "started", "response_path": response_path}]})
    save_json(ctx, response_path, {"input_sha256": digest, "text": json.dumps(scores()), "finish_reason": "stop",
                                   "trace": {"call_id": "completed-score", "model": "fake/score", "prompt_version": 2}})
    state["phase"] = "score"
    engine._save()
    fresh = Project.open(project.root)
    restarted = ProjectEngine(fresh, llm=demo_gateway(fresh),
                              stages={"idea": IdeaStage(fulltext_download=False)}, retry_delay=0)
    restarted.request_action("resume")
    drive(restarted, S.IdeaPending)
    assert not any(c.prompt_id == "idea/score" for c in restarted.llm.backend.calls)
    receipt = load(fresh, "idea/selection_rounds/initial/score.json")
    assert receipt["trace"]["call_id"] == "completed-score"
    assert all(d["to_verify"] for d in receipt["output"].values())


def test_unknown_references_remain_rejected_without_weakening_scores(air_home):
    _, _, ctx, _, _ = prepared(air_home, "score")
    _, papers, refs, weak = material(ctx)
    with pytest.raises(ValueError, match="all unknown references"):
        normalize_scores(ctx, ScoreOutput.model_validate(scores(["invented-paper#0"])), papers, refs, weak)
    output = ScoreOutput.model_validate(scores())
    normalize_scores(ctx, output, papers, refs, weak)
    assert all(getattr(output, name).to_verify for name in scores())


def test_pinned_v1_score_keeps_its_legacy_gateway_schema(air_home):
    project, engine, ctx, state, _ = prepared(air_home, "score")
    config = project.config.model_copy(deep=True)
    config.prompt_pins["idea/score"] = 1
    project.save_config(config)
    ctx = engine.build_context()
    assert isinstance(score_step(ctx, state), Continue)
    call = next(c for c in engine.llm.backend.calls if c.prompt_id == "idea/score")
    assert call.prompt_version == 1 and call.schema_name == "ScoreOutput"


def test_truncated_score_is_rejected_even_when_its_text_is_valid_json():
    with pytest.raises(ValueError, match="length limit"):
        decode_score({"text": json.dumps(scores()), "finish_reason": "length"}, ScoreOutput)


def test_score_without_a_saved_response_stops_before_paid_regeneration(air_home, monkeypatch):
    project, engine, ctx, state, _ = prepared(air_home, "score")
    calls = []

    def interrupted(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("Connection interrupted before a response was recorded")

    monkeypatch.setattr(engine.llm, "complete", interrupted)
    result = score_step(ctx, state)
    assert isinstance(result, Stop) and result.question
    receipt = load(project, round_path(state, "score_attempts"))
    assert len(calls) == 1 and receipt["attempts"][-1]["status"] == "interrupted"
    assert "cost may be uncertain" in result.question.text
    assert not ctx.archive.latest("idea/scores.json")


@pytest.mark.parametrize("finish_reason", ["stop", "length"])
def test_local_recovery_uses_archived_response_and_preserves_truncation_guard(air_home, finish_reason):
    from scripts.literature.recovery import recover_cached_score

    project, engine, ctx, state, _ = prepared(air_home, "score")
    context = {**selection_context(ctx), "idea": load(project, "idea/selected.json")}
    digest = sha256_bytes(json.dumps(context, ensure_ascii=False, sort_keys=True).encode())
    response_path = round_path(state, "score_responses/attempt-0001")
    trace = {"call_id": "saved-score", "model": "fake/score", "prompt_version": 2}
    save_json(ctx, response_path, {"input_sha256": digest, "trace": trace,
                                   "text": json.dumps(scores()), "finish_reason": finish_reason})
    save_json(ctx, round_path(state, "score_attempts"), {"input_sha256": digest, "attempts": [
        {"status": "failed", "response_path": response_path, "trace": trace}]})
    before = len(engine.llm.backend.calls)
    if finish_reason == "length":
        with pytest.raises(ValueError, match="length limit"):
            recover_cached_score(ctx, state)
        assert not ctx.archive.latest(round_path(state, "score"))
    else:
        recover_cached_score(ctx, state)
        assert load(project, round_path(state, "score"))["recovered_from_cache"]
    assert len(engine.llm.backend.calls) == before


def test_score_archive_failure_pauses_instead_of_regenerating(air_home, monkeypatch):
    import airesearcher.stages.idea.selection as selection

    project, engine, ctx, state, _ = prepared(air_home, "score")
    original_save = selection.save_json

    def fail_raw_save(ctx, path, value, *args):
        if "score_responses/" in path:
            raise OSError("Cannot persist raw response")
        return original_save(ctx, path, value, *args)

    monkeypatch.setattr(selection, "save_json", fail_raw_save)
    result = score_step(ctx, state)
    assert isinstance(result, Stop)
    attempt = load(project, round_path(state, "score_attempts"))["attempts"][-1]
    assert attempt["status"] == "interrupted" and attempt["trace"]["call_id"]
    assert "Cannot persist raw response" in result.question.text
    assert len([c for c in engine.llm.backend.calls if c.prompt_id == "idea/score"]) == 1
