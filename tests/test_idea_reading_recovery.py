"""Exercise paid-retry boundaries with simulated responses and durable engine decisions."""

import json

import pytest

from airesearcher.core.errors import InvalidState
from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.project import Project
from airesearcher.engine.engine import ProjectEngine
from airesearcher.stages.idea import readers
from airesearcher.stages.idea.models import reader_output_schema
from airesearcher.stages.idea.reading_state import load_receipt, start_attempt
from airesearcher.stages.idea.stage import IdeaStage
from airesearcher.stages.idea.storage import save_json
from scripts.literature.demo import demo_gateway

from .helpers import drive
from .test_idea_readers import METHOD_FIELDS, cards, output, scripts, seed


def read(project, path):
    return json.loads((project.root / path).read_text(encoding="utf-8"))


def broken(air_home, *, concurrency=2, count=1):
    script = scripts()
    value = output(METHOD_FIELDS)
    value["items"].append(dict(value["items"][0]))
    script["idea/methods_reader"] = [value]
    project, engine, papers = seed(air_home, count=count, script=script, concurrency=concurrency)
    drive(engine, S.Paused)
    assert project.questions.pending()
    return project, engine, papers


def test_schema_has_fixed_length_literal_positions_and_bounded_content():
    schema = reader_output_schema("methods", tuple(METHOD_FIELDS))
    items = schema.model_json_schema()["properties"]["items"]
    assert items["minItems"] == items["maxItems"] == 8 and len(items["prefixItems"]) == 8
    with pytest.raises(ValueError):
        schema.model_validate(output(METHOD_FIELDS + ["method"]))
    bad = output(METHOD_FIELDS)
    bad["items"][0]["content"] = "x" * 801
    with pytest.raises(ValueError):
        schema.model_validate(bad)


def test_duplicate_repair_receives_exact_error_and_preserves_both_raw_outputs(air_home):
    script = scripts()
    first = output(METHOD_FIELDS)
    first["items"].append(dict(first["items"][0]))
    script["idea/methods_reader"] = [first, output(METHOD_FIELDS)]
    project, engine, _ = seed(air_home, script=script)
    drive(engine, S.Paused)
    calls = [c for c in engine.llm.backend.calls if c.prompt_id == "idea/methods_reader"]
    assert len(calls) == 2
    second = str(calls[1].messages)
    assert "duplicates=" in second and "previous_invalid_reading_output" in second
    assert calls[0].messages != calls[1].messages
    assert all(c.schema_name is None for c in calls)
    receipt = read(project, f"idea/reading_receipts/{readers.card_id('s2:test0', 'methods')}.json")
    assert [h["status"] for h in receipt["history"]] == ["failed", "done"]
    raw = [read(project, h["response_path"]) for h in receipt["history"]]
    assert len(json.loads(raw[0]["text"])["items"]) == 9
    assert len(json.loads(raw[1]["text"])["items"]) == 8
    assert all(r["call_id"] and r["finish_reason"] == "stop" for r in raw)


def test_invalid_json_generates_twice_not_six_times_then_asks(air_home):
    script = scripts()
    script["idea/methods_reader"] = ["{\"items\":["]
    project, engine, _ = seed(air_home, script=script, concurrency=1)
    drive(engine, S.Paused)
    calls = engine.llm.backend.calls
    assert len(calls) == 2 and all(c.prompt_id == "idea/methods_reader" for c in calls)
    receipt = read(project, f"idea/reading_receipts/{readers.card_id('s2:test0', 'methods')}.json")
    assert receipt["status"] == "failed" and len(receipt["history"]) == 2
    assert all(read(project, h["response_path"])["text"] == "{\"items\":[" for h in receipt["history"])
    assert not cards(project) and engine.ck.scratch["idea"]["phase"] == "read"
    assert not (project.root / "idea/gaps.json").exists()
    question = project.questions.pending()
    assert {o.id for o in question.options} == {"retry", "end"}
    assert next(o for o in question.options if o.id == "end").default


def test_terminal_failure_does_not_dispatch_remaining_papers(air_home):
    project, engine, _ = broken(air_home, concurrency=1, count=3)
    assert len(engine.llm.backend.calls) == 2
    report = read(project, "idea/reading_status.json")
    assert report["status_counts"] == {"failed": 1, "pending": 5}
    assert "0/6 valid cards" in project.questions.pending().text
    with pytest.raises(InvalidState):
        engine.request_action("resume")
    for _ in range(3):
        engine.tick()
    assert len(engine.llm.backend.calls) == 2


def test_explicit_retry_survives_restart_and_never_rereads_successful_card(air_home):
    project, engine, _ = broken(air_home)
    question = project.questions.pending()
    cid = readers.card_id("s2:test0", "methods")
    original = read(project, f"idea/reading_receipts/{cid}.json")
    old_responses = {h["response_path"]: (project.root / h["response_path"]).read_bytes()
                     for h in original["history"]}
    good = next(c for c in cards(project) if c.role == "feasibility")
    good_bytes = (project.root / f"idea/reading_cards/{good.card_id}.json").read_bytes()
    fresh = Project.open(project.root)
    restarted = ProjectEngine(fresh, llm=demo_gateway(fresh, scripts()),
                              stages={"idea": IdeaStage(fulltext_download=False, stop_after="merge")}, retry_delay=0)
    fresh.questions.answer(question.question_id, "retry", request_id="retry-reading")
    budget = fresh.config.budget["llm_usd"].hard
    drive(restarted, S.Paused)
    assert not fresh.questions.pending() and restarted.ck.scratch["idea"]["phase"] == "refine"
    assert not any(c.prompt_id == "idea/feasibility_reader" for c in restarted.llm.backend.calls)
    updated = read(fresh, f"idea/reading_receipts/{cid}.json")
    assert updated["attempts"] == 3 and updated["window_start"] == 2
    assert [h["status"] for h in updated["history"]] == ["failed", "failed", "done"]
    assert updated["decisions"][0]["answer"]["question_id"] == question.question_id
    assert fresh.config.budget["llm_usd"].hard == budget
    assert (project.root / f"idea/reading_cards/{good.card_id}.json").read_bytes() == good_bytes
    assert all((project.root / p).read_bytes() == value for p, value in old_responses.items())


def test_explicit_skip_keeps_missing_reading_visible(air_home):
    project, engine, _ = broken(air_home)
    q = project.questions.pending()
    assert "skip" in {o.id for o in q.options}
    before = len(engine.llm.backend.calls)
    project.questions.answer(q.question_id, "skip", request_id="accept-missing")
    drive(engine, S.Paused)
    assert engine.ck.scratch["idea"]["phase"] == "refine"
    report = read(project, "idea/reading_status.json")
    assert len(report["failed"]) == 1 and report["failed"][0]["status"] == "skipped"
    assert read(project, "idea/gaps.json")["incomplete_reading"]
    assert not any(c.prompt_id.endswith("_reader") for c in engine.llm.backend.calls[before:])


def test_end_performs_no_further_calls(air_home):
    project, engine, _ = broken(air_home)
    before = len(engine.llm.backend.calls)
    project.questions.answer(project.questions.pending().question_id, "end", request_id="end-reading")
    drive(engine, S.Failed)
    assert len(engine.llm.backend.calls) == before


def start_saved(project, engine):
    ctx = engine.build_context()
    manifest = readers._manifest(ctx)
    source = manifest["papers"][0]
    task = {"paper_id": source["paper_id"], "role": "methods",
            "card_id": readers.card_id(source["paper_id"], "methods"), "status": "pending", "attempts": 0}
    saved = load_receipt(ctx, task, source)
    path = start_attempt(ctx, task, saved, 4)
    return ctx, source, task, saved, path


def test_interruption_without_saved_response_requires_decision_and_no_call(air_home):
    project, engine, _ = seed(air_home)
    _, _, task, _, _ = start_saved(project, engine)
    drive(engine, S.Paused)
    assert not engine.llm.backend.calls and project.questions.pending()
    saved = read(project, f"idea/reading_receipts/{task['card_id']}.json")
    assert saved["history"][-1]["status"] == "interrupted" and saved["status"] == "failed"


def test_saved_response_recovers_without_repeating_generation(air_home):
    project, engine, _ = seed(air_home)
    ctx, source, task, _, path = start_saved(project, engine)
    save_json(ctx, path, {"text": json.dumps(output(METHOD_FIELDS)), "finish_reason": "stop",
                         "usage": {"usd": 0}, "call_id": "completed-before-crash", "model": "offline-test",
                         "prompt_id": "idea/methods_reader", "prompt_version": 4,
                         "input_sha256": source["input_sha256"]})
    engine.tick()
    assert not any(c.prompt_id == "idea/methods_reader" for c in engine.llm.backend.calls)
    card = next(c for c in cards(project) if c.role == "methods")
    assert card.call_id == "completed-before-crash"
    assert read(project, f"idea/reading_receipts/{task['card_id']}.json")["status"] == "done"


def test_legacy_failure_is_not_automatically_rebilled(air_home):
    project, engine, _ = seed(air_home)
    ctx, source, task, _, _ = start_saved(project, engine)
    save_json(ctx, f"idea/reading_receipts/{task['card_id']}.json",
              {"input_sha256": source["input_sha256"], "status": "pending",
               "attempts": 1, "last_error": "Old duplicate fields"})
    drive(engine, S.Paused)
    assert project.questions.pending() and not engine.llm.backend.calls
    saved = read(project, f"idea/reading_receipts/{task['card_id']}.json")
    assert saved["attempts"] == 1 and "Legacy" in saved["last_error"]


def test_truncation_feedback_is_saved_and_repairs_once(air_home):
    from scripts.literature.demo import DemoLLM

    class TruncatedOnce(DemoLLM):
        def __init__(self):
            super().__init__(scripts())
            self.truncated = False

        def call(self, request):
            response = super().call(request)
            if request.prompt_id == "idea/methods_reader" and not self.truncated:
                self.truncated = True
                return response.model_copy(update={"finish_reason": "length"})
            return response

    backend = TruncatedOnce()
    project, engine, _ = seed(air_home, backend=backend)
    drive(engine, S.Paused)
    calls = [c for c in backend.calls if c.prompt_id == "idea/methods_reader"]
    assert len(calls) == 2 and "length limit" in str(calls[1].messages)
    saved = read(project, f"idea/reading_receipts/{readers.card_id('s2:test0', 'methods')}.json")
    assert read(project, saved["history"][0]["response_path"])["finish_reason"] == "length"
    assert saved["status"] == "done"


def test_automatic_retry_answer_cannot_authorize_paid_generation(air_home):
    project, engine, _ = broken(air_home)
    q = project.questions.pending()
    before = len(engine.llm.backend.calls)
    project.questions.answer(q.question_id, "retry", by="auto-default(eval)", request_id="automatic-retry")
    drive(engine, S.Paused)
    assert len(engine.llm.backend.calls) == before
    assert project.questions.pending().question_id != q.question_id


def test_failed_manual_window_is_bounded_and_requires_another_answer(air_home):
    project, engine, _ = broken(air_home, concurrency=1)
    q = project.questions.pending()
    project.questions.answer(q.question_id, "retry", request_id="retry-failed-window")
    drive(engine, S.Paused)
    assert len(engine.llm.backend.calls) == 4
    assert project.questions.pending().question_id != q.question_id
    saved = read(project, f"idea/reading_receipts/{readers.card_id('s2:test0', 'methods')}.json")
    assert saved["attempts"] == 4 and len(saved["decisions"]) == 1
    assert [h["status"] for h in saved["history"]] == ["failed"] * 4


def test_pinned_legacy_prompts_still_replay_without_changing_shared_cards(air_home):
    project, engine, _ = seed(air_home)
    cfg = project.config.model_copy(deep=True)
    cfg.prompt_pins.update({"idea/methods_reader": 3, "idea/feasibility_reader": 3})
    project.save_config(cfg)
    drive(engine, S.Paused)
    assert len(cards(project)) == 2 and all(c.prompt_version == 3 for c in cards(project))
    assert all(c.schema_name == "ReaderOutput" for c in engine.llm.backend.calls
               if c.prompt_id.endswith("_reader"))


@pytest.mark.parametrize("concurrency", [1, 2])
def test_empty_source_does_not_block_readable_papers_or_offer_paid_retry(air_home, concurrency):
    from airesearcher.stages.idea.storage import append_papers

    project, engine, papers = seed(air_home, count=2, concurrency=concurrency)
    papers[0].abstract = ""
    append_papers(engine.build_context(), [papers[0]])
    drive(engine, S.Paused)
    assert len(cards(project)) == 2 and all(c.paper_id == papers[1].paper_id for c in cards(project))
    assert len(engine.llm.backend.calls) == 2
    q = project.questions.pending()
    assert {o.id for o in q.options} == {"skip", "end"}
    assert q.refs[0] == project.archive.latest("idea/reading_status.json")
    report = read(project, "idea/reading_status.json")
    assert report["done"] == 2 and len(report["failed"]) == 2
    assert all(t["attempts"] == 0 and t["retryable"] is False for t in report["failed"])
    project.questions.answer(q.question_id, "skip", request_id="accept-empty-source")
    drive(engine, S.Paused)
    assert engine.ck.scratch["idea"]["phase"] == "refine"
    assert len(read(project, "idea/merge_input.json")["incomplete_reading"]) == 2
    assert not any('s2:test0' in str(c.messages) for c in engine.llm.backend.calls if c.prompt_id.endswith('_reader'))


def test_all_empty_sources_only_offer_end_without_calling_models(air_home):
    from airesearcher.stages.idea.storage import append_papers

    project, engine, papers = seed(air_home)
    papers[0].abstract = ""
    append_papers(engine.build_context(), papers)
    drive(engine, S.Paused)
    assert not engine.llm.backend.calls and not cards(project)
    assert {o.id for o in project.questions.pending().options} == {"end"}


def test_reading_status_saved_once_per_change_and_not_for_timing_only(air_home):
    project, engine, _ = seed(air_home, count=5, concurrency=1)
    while engine.ck.scratch["idea"]["phase"] == "read":
        engine.tick()
    status = project.archive.latest("idea/reading_status.json")
    assert status.version == 10
    events = [e for e in project.events.query(types=["artifact.created"])
              if any(r.artifact_id == "idea/reading_status.json" for r in e.refs)]
    assert len(events) == 10
    state = engine.ck.scratch["idea"]
    before = state["reading_active_seconds"]
    readers.read_step(engine.build_context(), state)
    assert project.archive.latest("idea/reading_status.json") == status
    assert state["reading_active_seconds"] > before
