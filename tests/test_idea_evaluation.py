"""Keep missing labels null, invalid runs visible, and comparison inputs fair."""

import copy

import pytest

from airesearcher.core.models.common import ProjectState as S
from scripts.literature.evaluation import annotation_id, annotation_template, evaluate

from .helpers import drive
from .test_idea_readers import QUOTE, cards, seed


def run(air_home, mode="multi"):
    project, engine, _ = seed(air_home, mode=mode)
    drive(engine, S.Paused)
    return project, engine


def labeled(run_data):
    template = annotation_template([run_data])
    source = run_data["items"][0]
    template["facts"] = [{"fact_id": "F1", "paper_id": source["paper_id"], "field": source["field"],
                          "statement": "An explicit comparison is reported.", "source_loc": "abstract",
                          "quote": QUOTE, "reviewer": "Human fixture for evaluator unit tests"}]
    template["gold_scope_complete"] = True
    for judgment in template["judgments"]:
        judgment.update(verdict="correct", reviewer="Human fixture for evaluator unit tests")
        if judgment["item_id"] == annotation_id(run_data["workspace_id"], source["ref"]):
            judgment["covered_fact_ids"] = ["F1"]
    return template


def test_missing_gold_cannot_become_a_quality_score(air_home):
    project, _ = run(air_home)
    report = evaluate([project.root])
    [data] = report["runs"]
    assert data["semantic_accuracy"] is None and data["key_information_recall"] is None
    assert report["new_model_calls"] == 0 and not report["full_protocol_ready"]
    assert data["expected_item_count"] == 14 and data["quote_match_rate"] == 1
    assert data["usage"]["elapsed_seconds"] > 0
    template = annotation_template([data])
    assert not template["facts"] and all(j["verdict"] == "pending" for j in template["judgments"])
    assert all(not {"workspace_id", "ref", "reading_mode", "role"} & j.keys() for j in template["judgments"])


@pytest.mark.parametrize("covered_item", [False, True])
def test_complete_labels_enable_metrics_and_partial_labels_remain_explicit(air_home, covered_item):
    project, _ = run(air_home)
    [data] = evaluate([project.root])["runs"]
    gold = labeled(data)
    [complete] = evaluate([project.root], gold)["runs"]
    assert complete["semantic_accuracy"] == 1 and complete["key_information_recall"] == 1
    partial = copy.deepcopy(gold)
    pending = next(judgment for judgment in partial["judgments"]
                   if bool(judgment["covered_fact_ids"]) == covered_item)
    # Pending labels discard prior fact coverage, regardless of blinded item ordering.
    pending.update(verdict="pending", reviewer="", covered_fact_ids=[])
    [data] = evaluate([project.root], partial)["runs"]
    assert data["semantic_accuracy"] is None and data["key_information_recall"] is None
    assert data["accuracy_on_reviewed_items"] == 1 and data["annotation_coverage"] == 13 / 14


def test_invented_gold_quotes_and_unknown_item_refs_are_rejected(air_home):
    project, _ = run(air_home)
    [data] = evaluate([project.root])["runs"]
    gold = labeled(data)
    gold["facts"][0]["quote"] = "This passage does not exist."
    with pytest.raises(ValueError, match="source quotation"):
        evaluate([project.root], gold)
    gold = labeled(data)
    gold["judgments"][0]["item_id"] = "unknown#9"
    with pytest.raises(ValueError, match="must exist"):
        evaluate([project.root], gold)


def test_fair_single_multi_pilot_and_failed_run_denominator(air_home):
    multi, _ = run(air_home, "multi")
    single, _ = run(air_home, "single")
    report = evaluate([multi.root, single.root, air_home / "missing-run"])
    assert report["attempted_runs"] == 3 and report["complete_runs"] == 2
    assert report["completion_rate"] == 2 / 3
    assert report["comparisons"][0]["pilot_comparable"] is True
    assert not report["comparisons"][0]["two_repeats_per_arm"] and not report["full_protocol_ready"]
    assert len(cards(multi)) == 2 and len(cards(single)) == 1


def test_duplicate_workspaces_are_not_counted_as_independent_repetitions(air_home):
    project, _ = run(air_home)
    with pytest.raises(ValueError, match="distinct workspace ID"):
        evaluate([project.root, project.root])


def test_cached_replay_is_not_an_independent_live_repetition(air_home):
    import json

    project, _ = run(air_home)
    path = project.root / ".llm/calls.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        row["cached"] = True
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    [comparison] = evaluate([project.root])["comparisons"]
    assert any("not an independent" in reason for reason in comparison["reasons"])
