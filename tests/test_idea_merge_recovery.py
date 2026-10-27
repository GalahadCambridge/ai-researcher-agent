"""Exercise coordinator failures, durable raw responses, and explicit paid-retry boundaries."""

import json

import pytest

from airesearcher.core.errors import InvalidState
from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.project import Project
from airesearcher.engine.engine import ProjectEngine
from airesearcher.stages.idea.coordinator import ATTEMPTS_PATH
from airesearcher.stages.idea.stage import IdeaStage
from airesearcher.stages.idea.storage import save_json
from scripts.literature.demo import DemoLLM, demo_gateway

from .helpers import drive
from .test_idea_merge import read, seed, summary_output


def bad_output(cards):
    result = summary_output(cards)
    result["themes"][0]["points"][0]["evidence"] = ["invented#0"]
    return result


@pytest.mark.parametrize("status", ["to_verify", "supported"])
def test_empty_gap_cannot_become_supported_or_block_the_coordinator(air_home, status):
    def output(cards):
        result = summary_output(cards)
        result["gaps"] = [{"gap_id": "G5", "description": "The low-resource setting is not established.",
                           "evidence": [], "status": status}]
        return result
    project, engine, _ = seed(air_home, output=output)
    save_json(engine.build_context(), "idea/gaps.json", {"gaps": [{
        "gap_id": "G5", "description": "The low-resource setting is not established.",
        "evidence": [], "status": "to_verify",
    }]})
    drive(engine, S.Paused)
    assert not project.questions.pending() and len(engine.llm.backend.calls) == 1
    gap = read(project, "reading_summary.json")["gaps"][0]
    assert gap["evidence"] == [] and gap["status"] == "to_verify"
    assert read(project, "merge_coordinator.json")["output"]["gaps"][0]["status"] == "to_verify"


def test_unknown_reference_repair_receives_error_and_archives_each_generation(air_home):
    project, engine, cards = seed(air_home, output=bad_output)
    engine.llm.backend.script["idea/coordinator_merge"].append(summary_output(cards))
    drive(engine, S.Paused)
    calls = engine.llm.backend.calls
    assert len(calls) == 2 and all(c.schema_name is None for c in calls)
    assert "unknown=['invented#0']" in calls[1].messages[1]["content"]
    assert "themes[0].points[0]" in str(calls[1].messages)
    assert "previous_invalid_coordinator_output" in str(calls[1].messages)
    assert calls[0].messages != calls[1].messages
    attempts = read(project, "merge_attempts.json")["attempts"]
    assert [a["status"] for a in attempts] == ["failed", "done"]
    raws = [json.loads((project.root / a["response_path"]).read_text()) for a in attempts]
    assert all(r["trace"]["call_id"] and r["finish_reason"] == "stop" for r in raws)
    assert 'invented#0' in raws[0]["text"] and 'invented#0' not in raws[1]["text"]
    assert len(project.events.query(types=["literature.merge_failed"])) == 1


@pytest.mark.parametrize("failure", ["invalid_json", "unknown_reference"])
def test_two_invalid_generations_pause_instead_of_engine_retrying(air_home, failure):
    project, engine, _ = seed(air_home, output=bad_output)
    if failure == "invalid_json":
        engine.llm.backend.script["idea/coordinator_merge"] = ['{"themes":[']
    drive(engine, S.Paused)
    assert len(engine.llm.backend.calls) == 2
    question = project.questions.pending()
    assert {o.id for o in question.options} == {"retry", "end"}
    assert next(o for o in question.options if o.id == "end").default
    assert question.refs[0].artifact_id == ATTEMPTS_PATH
    assert not project.events.query(types=["engine.error"])
    with pytest.raises(InvalidState):
        engine.request_action("resume")
    for _ in range(3):
        engine.tick()
    assert len(engine.llm.backend.calls) == 2


def test_retry_survives_restart_preserves_history_and_never_rereads_cards(air_home):
    project, engine, cards = seed(air_home, output=bad_output)
    project.budget.charge("llm_usd", 0.01, "prior paid work")
    drive(engine, S.Paused)
    q = project.questions.pending()
    old = read(project, "merge_attempts.json")["attempts"]
    originals = {a["response_path"]: (project.root / a["response_path"]).read_bytes() for a in old}
    fresh = Project.open(project.root)
    restarted = ProjectEngine(fresh, llm=demo_gateway(fresh, {"idea/coordinator_merge": [summary_output(cards)]}),
                              stages={"idea": IdeaStage(fulltext_download=False, stop_after="merge")}, retry_delay=0)
    fresh.questions.answer(q.question_id, "retry", request_id="merge-retry-after-restart")
    drive(restarted, S.Paused)
    saved = read(fresh, "merge_attempts.json")
    assert saved["window_start"] == 2
    assert [a["status"] for a in saved["attempts"]] == ["failed", "failed", "done"]
    assert saved["retry_windows"][0]["answer"]["question_id"] == q.question_id
    assert fresh.budget.status().categories["llm_usd"].used == 0.01
    assert len(restarted.llm.backend.calls) == 1
    assert restarted.llm.backend.calls[0].prompt_id == "idea/coordinator_merge"
    assert all((project.root / p).read_bytes() == text for p, text in originals.items())


@pytest.mark.parametrize("choice,by,expected", [("end", "user", S.Failed),
                                               ("retry", "auto-default(eval)", S.Paused)])
def test_end_or_automatic_answer_cannot_generate_again(air_home, choice, by, expected):
    project, engine, _ = seed(air_home, output=bad_output)
    drive(engine, S.Paused)
    project.questions.answer(project.questions.pending().question_id, choice, by=by, request_id="merge-no-call")
    drive(engine, expected)
    assert len(engine.llm.backend.calls) == 2


@pytest.mark.parametrize("saved_response", [True, False])
def test_crash_after_raw_response_recovers_and_unknown_cost_requires_decision(air_home, saved_response):
    project, engine, cards = seed(air_home)
    engine.tick()
    ctx = engine.build_context()
    digest = read(project, "merge_input.json")["input_sha256"]
    path = "idea/merge_attempts/attempt-0001.json"
    save_json(ctx, ATTEMPTS_PATH, {"input_sha256": digest, "attempts": [
        {"status": "started", "response_path": path}]})
    if saved_response:
        save_json(ctx, path, {"input_sha256": digest, "text": json.dumps(summary_output(cards)),
                              "finish_reason": "stop", "trace": {"call_id": "completed-before-crash"}})
    fresh = Project.open(project.root)
    restarted = ProjectEngine(fresh, llm=demo_gateway(fresh, {}),
                              stages={"idea": IdeaStage(fulltext_download=False, stop_after="merge")}, retry_delay=0)
    drive(restarted, S.Paused)
    assert not restarted.llm.backend.calls
    if saved_response:
        assert read(fresh, "merge_coordinator.json")["trace"]["call_id"] == "completed-before-crash"
        assert not fresh.questions.pending()
    else:
        assert fresh.questions.pending()
        assert read(fresh, "merge_attempts.json")["attempts"][0]["status"] == "interrupted"


def test_length_finish_reason_repairs_a_shorter_complete_response(air_home):
    class TruncatedOnce(DemoLLM):
        def call(self, request):
            response = super().call(request)
            if len(self.calls) == 1:
                return response.model_copy(update={"finish_reason": "length"})
            return response
    project, engine, _ = seed(air_home, backend=TruncatedOnce)
    drive(engine, S.Paused)
    assert len(engine.llm.backend.calls) == 2
    assert "length limit" in str(engine.llm.backend.calls[1].messages)
    assert read(project, "merge_attempts.json")["attempts"][-1]["status"] == "done"


def test_incremental_merge_retry_uses_the_current_question_after_restart(air_home):
    from airesearcher.services.literature import load_papers
    from airesearcher.stages.idea.revision import scoped_context
    from airesearcher.stages.idea.storage import append_papers

    project, engine, cards = seed(air_home, output=bad_output)
    state = engine.ck.scratch["idea"]
    state.update(phase="revision_search", selection_round="nested-retry",
                 revision_literature={"phase": "merge", "exclude_identity_version": 1})
    ctx = engine.build_context()
    scoped = scoped_context(ctx, state)
    save_json(scoped, "idea/queries.json", {})
    append_papers(scoped, list(load_papers(project.root).values()))
    for name in ["reading_input.json", "reading_status.json", "gaps.json"]:
        save_json(scoped, "idea/" + name, read(project, name))
    for card in cards:
        save_json(scoped, f"idea/reading_cards/{card.card_id}.json", card.model_dump(mode="json"))
    engine._save()
    drive(engine, S.Paused)
    q = project.questions.pending()
    assert q and q.refs[0].artifact_id.startswith('idea/revision_literature/nested-retry/')
    fresh = Project.open(project.root)
    restarted = ProjectEngine(fresh, llm=demo_gateway(fresh, {"idea/coordinator_merge": [summary_output(cards)]}),
                              stages={"idea": IdeaStage(fulltext_download=False, stop_after="merge")}, retry_delay=0)
    fresh.questions.answer(q.question_id, "retry", request_id="nested-merge-retry")
    drive(restarted, S.Paused)
    assert not fresh.questions.pending()
    assert restarted.ck.scratch["idea"]["phase"] == "refine"
    assert len(restarted.llm.backend.calls) == 1
    attempt = read(fresh, "revision_literature/nested-retry/idea/merge_attempts.json")
    assert attempt["window_start"] == 2 and attempt["attempts"][-1]["status"] == "done"
