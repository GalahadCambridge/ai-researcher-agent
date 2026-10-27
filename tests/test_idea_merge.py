"""Test coordinator evidence, conflict rereads, bounded retries, and crash recovery with fake models."""

import json

import pytest

from airesearcher.core.fsutil import now, sha256_bytes
from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.models.literature import CardItem, ReadingCard
from airesearcher.core.project import Project
from airesearcher.engine.engine import ProjectEngine
from airesearcher.engine.stage import Error
from airesearcher.stages.idea.conflicts import indexed_items, review_snippets, rule_conflicts
from airesearcher.stages.idea.stage import IdeaStage
from airesearcher.stages.idea.storage import append_papers, save_json
from airesearcher.testing.fake_literature import FakeLiterature
from airesearcher.testing.fake_llm import fake_gateway
from scripts.literature.demo import DemoLLM, demo_gateway

from .helpers import drive

QUOTE = "The method uses SST-2"
SOURCE = ("[p.1]\nThe method uses SST-2. Accuracy is 90 percent. Training uses 8 GB of memory.\n\n"
          "[p.2]\nUNRELATED_SECTION_MARKER. The bibliography is omitted from conflict excerpts.")
HUMAN = {"resolution": "needs_human", "accepted_evidence": [], "quotes": [],
         "explanation": "The available excerpts do not establish which experimental conditions apply."}


def summary_output(cards):
    return {"themes": [{"theme": "Method coverage", "points": [
        {"text": "A reading-card observation.", "evidence": [f"{card.card_id}#0"], "to_verify": False}
        for card in cards]}] if cards else [], "feasibility_notes": [], "gaps": [], "conflicts": []}


def seed(air_home, *, count=1, fields=None, full=True, output=None, review=None, backend=None):
    project = Project.create(goal="Compare fine-tuning methods under limited resources", task="tasks/smoke",
                             home=air_home, dev={"stage_impl": {"idea": "real"}})
    base = FakeLiterature().get("arxiv:2106.09685")
    papers, sources, cards = [], [], []
    fields = fields or ["method"]
    for i in range(count):
        pid = f"s2:p{i}"
        text = SOURCE if full else SOURCE.split("[p.2]")[0].replace("[p.1]", "").strip()
        papers.append(base.model_copy(update={"paper_id": pid, "selected_for_reading": True}, deep=True))
        sources.append({"paper_id": pid, "title": f"Test paper {i}", "text": text,
                        "based_on": "full_text" if full else "abstract_only", "source_path": None,
                        "warning": "", "input_sha256": sha256_bytes(text.encode())})
        for role, side in (("methods", 0), ("single", 1)):
            values = {"method": "The method compares fine-tuning approaches.",
                      "dataset": ("SST-2", "MNLI")[side], "compute": ("8 GB", "16 GB")[side],
                      "setting": ("Batch size 32", "Batch size 64")[side],
                      "main_result": ("Accuracy 90 percent", "Accuracy 80 percent")[side]}
            cards.append(ReadingCard(
                card_id=f"s2_p{i}__{role}", paper_id=pid, role=role,
                based_on="full_text" if full else "abstract_only",
                items=[CardItem(field=field, content=values[field], kind="author_claim",
                                source_loc="p.1" if full else "abstract", quote=QUOTE,
                                confidence="high", quote_verified=True) for field in fields],
                relevance_to_question="Relevant method comparison.", model="test/reader",
                prompt_id=f"idea/{role}_reader", prompt_version=1, call_id=f"reader-{i}-{role}", created_at=now(),
            ))
    script = {"idea/coordinator_merge": [output(cards) if output else summary_output(cards)],
              "idea/adjudicate_conflict": [review(cards) if callable(review) else review or HUMAN]}
    gateway = demo_gateway(project, script)
    if backend is not None:
        from airesearcher.llm.gateway import LLMGateway
        gateway = LLMGateway(project, backend=backend(script), backoff_base=0)
    engine = ProjectEngine(project, llm=gateway,
                           stages={"idea": IdeaStage(fulltext_download=False, stop_after="merge")},
                           retry_delay=0)
    ctx = engine.build_context()
    append_papers(ctx, papers)
    save_json(ctx, "idea/reading_input.json", {"goal": project.config.goal, "reading_mode": "multi", "papers": sources})
    save_json(ctx, "idea/reading_status.json", {"card_ids": [c.card_id for c in cards], "failed": []})
    save_json(ctx, "idea/gaps.json", {"gaps": []})
    for card in cards:
        save_json(ctx, f"idea/reading_cards/{card.card_id}.json", card.model_dump(mode="json"), "reading_card")
    engine.ck.scratch["idea"] = {"phase": "merge"}
    engine._save()
    return project, engine, cards


def read(project, path):
    return json.loads((project.root / "idea" / path).read_text(encoding="utf-8"))


def resolved(cards):
    ref = cards[0].card_id + "#0"
    return {"resolution": "resolved_by_reread", "accepted_evidence": [ref],
            "quotes": [{"ref": ref, "quote": QUOTE}], "explanation": "The original excerpt names SST-2."}


def test_merge_summary_evidence_and_trace_without_modifying_cards(air_home):
    project, engine, cards = seed(air_home)
    originals = {c.card_id: (project.root / f"idea/reading_cards/{c.card_id}.json").read_bytes() for c in cards}
    drive(engine, S.Paused)
    result = read(project, "reading_summary.json")
    assert result["themes"] and not result["themes"][0]["points"][0]["to_verify"]
    assert result["coordinator_trace"]["call_id"] and not result["abstract_only_papers"]
    assert read(project, "merge_status.json")["conflicts"] == 0
    assert engine.ck.scratch["idea"]["phase"] == "refine"
    assert not (project.root / "idea/selected.md").exists() and not project.approvals.pending()
    markdown = (project.root / "idea/reading_summary.md").read_text(encoding="utf-8")
    assert "[s2:p0]" in markdown and all(card.card_id + "#0" in markdown for card in cards)
    assert all((project.root / f"idea/reading_cards/{cid}.json").read_bytes() == text
               for cid, text in originals.items())
    [request] = engine.llm.backend.calls
    assert request.prompt_id == "idea/coordinator_merge" and "<untrusted_document" in str(request.messages)
    assert request.tools is None


def test_abstract_only_points_and_gaps_remain_to_verify(air_home):
    project, engine, cards = seed(air_home, full=False)
    gap = {"gap_id": "G1", "description": "Candidate requiring confirmation",
           "evidence": [cards[0].card_id + "#0"], "status": "supported"}
    save_json(engine.build_context(), "idea/gaps.json", {"gaps": [gap]})
    drive(engine, S.Paused)
    result = read(project, "reading_summary.json")
    assert result["abstract_only_papers"] == ["s2:p0"]
    assert all(p["to_verify"] for p in result["themes"][0]["points"])
    assert result["gaps"][0]["status"] == "to_verify"


@pytest.mark.parametrize("field,left,right,expected", [
    ("dataset", "SST-2", "MNLI", True),
    ("dataset", "The SST-2 dataset", "SST-2", False),
    ("compute", "8 GB", "16 GB", True),
    ("main_result", "90 percent", "90.0 percent", False),
    ("code_available", "Yes, available", "No, unavailable", True),
    ("method", "Different wording", "Another wording", False),
    ("dataset", "Not mentioned", "MNLI", False),
])
def test_rule_candidates_are_conservative(field, left, right, expected):
    from types import SimpleNamespace
    cards = [SimpleNamespace(card_id=f"card{i}", paper_id="s2:p",
                             items=[CardItem(field=field, content=value, kind="agent_inference",
                                             source_loc="abstract", quote=None, confidence="low")],
                             based_on="abstract_only")
             for i, value in enumerate((left, right))]
    assert bool(rule_conflicts(indexed_items(cards))) == expected


def test_conflict_reread_is_bounded_cited_and_logged(air_home):
    project, engine, _ = seed(air_home, fields=["dataset"], review=resolved)
    drive(engine, S.Paused)
    ledger = read(project, "conflicts.json")
    assert ledger["review_attempts"] == 1
    assert ledger["conflicts"][0]["resolution"] == "resolved_by_reread"
    assert len(ledger["conflicts"][0]["positions"]) == 2
    receipt = read(project, "conflict_reviews/K1.json")
    assert receipt["status"] == "done" and receipt["trace"]["call_id"]
    events = project.events.query(types=["literature.conflict_reviewed"])
    assert len(events) == 1 and events[0].data["usage"]["usd"] == 0
    requests = [r for r in engine.llm.backend.calls if r.prompt_id == "idea/adjudicate_conflict"]
    assert len(requests) == 1 and "UNRELATED_SECTION_MARKER" not in str(requests[0].messages)
    points = read(project, "reading_summary.json")["themes"][0]["points"]
    assert not points[0]["to_verify"] and points[1]["to_verify"]


@pytest.mark.parametrize("bad", ["reference", "quote", "empty", "inference"])
def test_invalid_resolution_cannot_clear_conflict(air_home, bad):
    def review(cards):
        result = resolved(cards)
        if bad == "reference":
            result["accepted_evidence"] = ["invented#0"]
        elif bad == "quote":
            result["quotes"][0]["quote"] = "This statement is absent from the supplied original."
        elif bad == "empty":
            result["accepted_evidence"] = []
        return result
    project, engine, cards = seed(air_home, fields=["dataset"], review=review)
    if bad == "inference":
        cards[0].items[0].kind = "agent_inference"
        save_json(engine.build_context(), f"idea/reading_cards/{cards[0].card_id}.json",
                  cards[0].model_dump(mode="json"), "reading_card")
    drive(engine, S.Paused)
    assert read(project, "conflicts.json")["conflicts"][0]["resolution"] == "needs_human"
    assert read(project, "conflict_reviews/K1.json")["status"] == "failed"
    assert len([r for r in engine.llm.backend.calls if r.prompt_id == "idea/adjudicate_conflict"]) == 1


def test_review_limit_marks_remaining_conflicts_for_humans(air_home):
    project, engine, _ = seed(air_home, count=2, fields=["dataset", "compute"])
    drive(engine, S.Paused)
    ledger = read(project, "conflicts.json")
    assert len(ledger["conflicts"]) == 4 and ledger["review_attempts"] == 3
    assert all(c["resolution"] == "needs_human" for c in ledger["conflicts"])
    assert read(project, "conflict_reviews/K4.json")["status"] == "skipped"
    assert len([r for r in engine.llm.backend.calls if r.prompt_id == "idea/adjudicate_conflict"]) == 3


def test_insufficient_original_source_skips_reread(air_home):
    project, engine, cards = seed(air_home, fields=["dataset"], review=resolved)
    cards[1].items[0].source_loc = "p.999"
    save_json(engine.build_context(), f"idea/reading_cards/{cards[1].card_id}.json",
              cards[1].model_dump(mode="json"), "reading_card")
    drive(engine, S.Paused)
    assert read(project, "conflicts.json")["review_attempts"] == 0
    assert read(project, "conflict_reviews/K1.json")["status"] == "skipped"


def test_saved_results_restore_missing_projection_without_model_calls(air_home):
    project, engine, _ = seed(air_home, fields=["dataset"], review=resolved)
    drive(engine, S.Paused)
    missing = project.root / "idea/reading_summary.md"
    missing.unlink()
    resumed = ProjectEngine(Project.open(project.root), llm=fake_gateway(project, {}),
                            stages={"idea": IdeaStage(fulltext_download=False, stop_after="merge")},
                           retry_delay=0)
    resumed.ck.state = S.IdeaDrafting
    resumed.ck.scratch["idea"] = {"phase": "merge"}
    resumed._save()
    drive(resumed, S.Paused)
    assert missing.exists() and not resumed.llm.backend.calls
    assert read(project, "conflicts.json")["review_attempts"] == 1


def test_interrupted_review_is_not_repeated(air_home):
    project, engine, _ = seed(air_home, fields=["dataset"])
    engine.tick()
    engine.tick()
    snapshot = read(project, "merge_input.json")
    save_json(engine.build_context(), "idea/conflict_reviews/K1.json", {
        "input_sha256": snapshot["input_sha256"], "conflict_id": "K1", "status": "started",
        "resolution": "needs_human", "accepted_evidence": [], "trace": {}, "note": "Interrupted",
    })
    resumed = ProjectEngine(Project.open(project.root), llm=fake_gateway(project, {}),
                            stages={"idea": IdeaStage(fulltext_download=False, stop_after="merge")},
                           retry_delay=0)
    drive(resumed, S.Paused)
    assert read(project, "conflict_reviews/K1.json")["status"] == "interrupted"
    assert not resumed.llm.backend.calls and read(project, "conflicts.json")["review_attempts"] == 1


@pytest.mark.parametrize("bad", ["reference", "duplicate", "coverage"])
def test_coordinator_invalid_evidence_is_rejected(air_home, bad):
    def output(cards):
        result = summary_output(cards)
        if bad == "reference":
            result["themes"][0]["points"][0]["evidence"] = ["invented#0"]
        elif bad == "duplicate":
            result["themes"][0]["points"][0]["evidence"] *= 2
        else:
            result["themes"][0]["points"].pop()
        return result
    project, engine, _ = seed(air_home, output=output)
    drive(engine, S.Paused)
    assert project.questions.pending()
    assert len(engine.llm.backend.calls) == 2
    assert [a["status"] for a in read(project, "merge_attempts.json")["attempts"]] == ["failed", "failed"]
    assert not (project.root / "idea/merge_coordinator.json").exists()


def test_coordinator_can_add_cross_paper_conflict(air_home):
    def output(cards):
        result = summary_output(cards)
        result["conflicts"] = [{"field": "method", "evidence": [cards[0].card_id + "#0", cards[2].card_id + "#0"],
                                "note": "A possible cross-paper inconsistency requires checking conditions."}]
        return result
    project, engine, _ = seed(air_home, count=2, output=output)
    drive(engine, S.Paused)
    [conflict] = read(project, "conflicts.json")["conflicts"]
    assert conflict["paper_id"] is None and conflict["detected_by"] == "coordinator"


def test_duplicate_conflict_candidate_is_not_reviewed_twice(air_home):
    def output(cards):
        result = summary_output(cards)
        result["conflicts"] = [{"field": "dataset", "evidence": [c.card_id + "#0" for c in cards],
                                "note": "Different dataset names."}]
        return result
    project, engine, _ = seed(air_home, fields=["dataset"], output=output)
    drive(engine, S.Paused)
    ledger = read(project, "conflicts.json")
    assert len(ledger["conflicts"]) == 1 and ledger["review_attempts"] == 1


def test_failed_review_is_not_retried(air_home):
    class FailingReview(DemoLLM):
        attempts = 0
        def call(self, req):
            if req.prompt_id == "idea/adjudicate_conflict":
                self.attempts += 1
                raise RuntimeError("Simulated provider failure")
            return super().call(req)
    project, engine, _ = seed(air_home, fields=["dataset"], backend=FailingReview)
    drive(engine, S.Paused)
    assert engine.llm.backend.attempts == 1
    assert read(project, "conflict_reviews/K1.json")["status"] == "failed"
    engine.ck.state = S.IdeaDrafting
    engine.ck.scratch["idea"] = {"phase": "merge"}
    engine._save()
    drive(engine, S.Paused)
    assert engine.llm.backend.attempts == 1


def test_no_cards_retains_incomplete_reading_without_calls(air_home):
    project, engine, _ = seed(air_home, count=0)
    save_json(engine.build_context(), "idea/reading_status.json",
              {"card_ids": [], "failed": [{"card_id": "missing__methods"}]})
    drive(engine, S.Paused)
    summary = read(project, "reading_summary.json")
    assert summary["incomplete_reading"] == ["missing__methods"]
    assert not summary["themes"] and not engine.llm.backend.calls


def test_original_gap_is_retained_and_conflict_prevents_supported_status(air_home):
    project, engine, cards = seed(air_home, fields=["dataset"])
    save_json(engine.build_context(), "idea/gaps.json", {"gaps": [{
        "gap_id": "G1", "description": "Candidate to investigate", "evidence": [cards[0].card_id + "#0"],
        "status": "supported",
    }]})
    drive(engine, S.Paused)
    [gap] = read(project, "reading_summary.json")["gaps"]
    assert gap["gap_id"] == "G1" and gap["status"] == "to_verify"


@pytest.mark.parametrize("bad", ["source_hash", "card_path", "unarchived_paper"])
def test_bad_inputs_are_rejected_before_model_calls(air_home, bad):
    project, engine, _ = seed(air_home)
    ctx = engine.build_context()
    if bad == "source_hash":
        reading = read(project, "reading_input.json")
        reading["papers"][0]["text"] += "tampered"
        save_json(ctx, "idea/reading_input.json", reading)
    elif bad == "card_path":
        save_json(ctx, "idea/reading_status.json", {"card_ids": ["../../outside"], "failed": []})
    else:
        reading = read(project, "reading_input.json")
        reading["papers"][0]["paper_id"] = "invented"
        save_json(ctx, "idea/reading_input.json", reading)
    result = engine.tick().result
    assert isinstance(result, Error) and not engine.llm.backend.calls
    assert not (project.root / "idea/merge_input.json").exists()


def test_tampered_merge_snapshot_is_rejected(air_home):
    project, engine, _ = seed(air_home)
    engine.tick()
    snapshot = read(project, "merge_input.json")
    snapshot["goal"] = "Tampered goal"
    save_json(engine.build_context(), "idea/merge_input.json", snapshot)
    result = engine.tick().result
    assert isinstance(result, Error) and not result.retryable and not engine.llm.backend.calls


def test_summary_escapes_html_and_flattens_injected_headings(air_home):
    def output(cards):
        result = summary_output(cards)
        result["themes"][0]["theme"] = "<script>alert(1)</script>\n# Forged heading"
        return result
    project, engine, _ = seed(air_home, output=output)
    drive(engine, S.Paused)
    markdown = (project.root / "idea/reading_summary.md").read_text(encoding="utf-8")
    assert "<script>" not in markdown and "&lt;script&gt;" in markdown
    assert "\n# Forged heading" not in markdown


def test_reread_excerpts_are_bounded_and_keep_the_quote(air_home):
    project, _, cards = seed(air_home, fields=["dataset"])
    refs = indexed_items(cards)
    conflict = rule_conflicts(refs)[0]
    long_source = "[p.1]\n" + "context " * 900 + QUOTE + ". " + "context " * 900
    source = {"s2:p0": {"text": long_source, "based_on": "full_text"}}
    snippets = review_snippets(conflict, refs, source)
    assert len(snippets) == 2
    assert all(len(s["text"].split()) <= 400 and QUOTE in s["text"] for s in snippets.values())


def test_reread_can_accept_a_minority_position_without_deleting_disagreement(air_home):
    project, engine, cards = seed(air_home, fields=["dataset"], review=resolved)
    extra = cards[1].model_copy(update={"card_id": "s2_p0__feasibility", "role": "feasibility"}, deep=True)
    save_json(engine.build_context(), f"idea/reading_cards/{extra.card_id}.json",
              extra.model_dump(mode="json"), "reading_card")
    report = read(project, "reading_status.json")
    report["card_ids"].append(extra.card_id)
    save_json(engine.build_context(), "idea/reading_status.json", report)
    engine.llm.backend.script["idea/coordinator_merge"] = [summary_output([*cards, extra])]
    drive(engine, S.Paused)
    [conflict] = read(project, "conflicts.json")["conflicts"]
    assert conflict["resolution"] == "resolved_by_reread"
    assert conflict["accepted_evidence"] == [cards[0].card_id + "#0"]
    assert [p["content"] for p in conflict["positions"]].count("MNLI") == 2
    assert len(conflict["positions"]) == 3
    assert (project.root / f"idea/reading_cards/{extra.card_id}.json").exists()
