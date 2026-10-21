"""Compute offline literature metrics; semantic scores require independent human annotations."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from airesearcher.core.errors import AirError
from airesearcher.core.models.literature import ReadingCard
from airesearcher.core.project import Project
from airesearcher.stages.idea.quotes import QuoteVerifier
from airesearcher.stages.idea.readers import FEASIBILITY_FIELDS, METHOD_FIELDS, ROLE_FIELDS

from .quality import archived_json

FIELDS = set(METHOD_FIELDS + FEASIBILITY_FIELDS)
SYNTHESIS = {"idea/gap_analyst", "idea/coordinator_merge", "idea/adjudicate_conflict"}


def collect_workspace(workspace):
    project = Project.open(Path(workspace))
    manifest = archived_json(project, "idea/reading_input.json")
    status = archived_json(project, "idea/reading_status.json")
    sources = {source["paper_id"]: source for source in manifest["papers"]}
    if len(sources) != len(manifest["papers"]) or not sources:
        raise ValueError("Reading inputs must contain distinct papers")
    for source in sources.values():
        if hashlib.sha256(source["text"].encode()).hexdigest() != source["input_sha256"]:
            raise ValueError("Frozen reading input hash mismatch")
    mode = manifest["reading_mode"]
    if mode not in {"single", "multi"}:
        raise ValueError("Unknown reading mode")
    items, models, profiles, covered, errors = [], set(), set(), Counter(), []
    for cid in status["card_ids"]:
        card = ReadingCard.model_validate(archived_json(project, f"idea/reading_cards/{cid}.json"))
        if card.card_id != cid or card.paper_id not in sources:
            raise ValueError("Reading-card identity does not match the frozen input")
        expected_roles = {"single"} if mode == "single" else {"methods", "feasibility"}
        if card.role not in expected_roles:
            errors.append(f"Unexpected reading role: {cid}")
        if Counter(item.field for item in card.items) != Counter(ROLE_FIELDS[card.role]):
            errors.append(f"Incomplete or duplicate role fields: {cid}")
        models.add(card.model)
        profiles.add((card.role, card.model, card.prompt_id, card.prompt_version))
        source = sources[card.paper_id]
        checker = QuoteVerifier(source["text"])
        for index, item in enumerate(card.items):
            checked, audit = checker.check_with_report(item, source["based_on"])
            covered[(card.paper_id, item.field)] += 1
            items.append({"ref": f"{cid}#{index}", "paper_id": card.paper_id, "field": item.field,
                          "candidate_text": item.content, "source_loc": item.source_loc, "quote": item.quote,
                          "stored_quote_verified": item.quote_verified,
                          "rechecked_quote_verified": checked.quote_verified,
                          "reason": audit["reason"]})
    expected = {(pid, field) for pid in sources for field in FIELDS}
    missing = sorted(expected - covered.keys())
    duplicate = [list(key) for key, count in covered.items() if count > 1]
    relevant = []
    log = project.root / ".llm/calls.jsonl"
    if log.exists():
        relevant = [row for row in (json.loads(line) for line in log.read_text(encoding="utf-8").splitlines())
                    if row.get("prompt_id") in SYNTHESIS or row.get("prompt_id", "").endswith("_reader")]
    if len({row["call_id"] for row in relevant}) != len(relevant):
        raise ValueError("Duplicate model call IDs in usage log")
    paid = [row for row in relevant if not row.get("cached")]
    known = [row["usd"] for row in paid if row.get("usd") is not None]
    if any(not math.isfinite(value) or value < 0 for value in known):
        raise ValueError("Usage costs must be finite and nonnegative")
    fingerprint = {"goal": manifest["goal"], "sources": sorted(
        (pid, source["input_sha256"], source["based_on"]) for pid, source in sources.items())}
    digest = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()
    failures = status.get("failed", [])
    count = len(items)
    return {"workspace": str(project.root), "workspace_id": project.project_id, "reading_mode": mode,
            "outcome": "complete" if not failures and not missing and not duplicate and not errors else "incomplete",
            "input_fingerprint": digest, "models": sorted(models), "reader_profiles": sorted(profiles),
            "llm_budget_usd": project.config.budget["llm_usd"].hard, "paper_ids": sorted(sources),
            "expected_item_count": len(expected), "observed_item_count": count,
            "missing_fields": missing, "duplicate_fields": duplicate, "failed_tasks": failures,
            "validation_errors": errors, "items": items,
            "stored_quote_matches": sum(item["stored_quote_verified"] is True for item in items),
            "rechecked_quote_matches": sum(item["rechecked_quote_verified"] is True for item in items),
            "quote_match_rate": (sum(item["rechecked_quote_verified"] is True for item in items) / len(expected)
                                 if not duplicate and not errors else None),
            "usage": {"known_uncached_usd": sum(known), "cost_complete": len(known) == len(paid),
                      "uncached_calls": len(paid), "cached_calls": len(relevant) - len(paid),
                      "input_tokens": sum(row.get("input_tokens", 0) for row in paid),
                      "output_tokens": sum(row.get("output_tokens", 0) for row in paid),
                      "elapsed_seconds": status.get("active_seconds"),
                      "timing_definition": status.get("timing_definition", "Not measured in this historical run")},
            "semantic_accuracy": None, "key_information_recall": None, "annotation_coverage": 0.0}


def annotation_id(workspace_id, ref):
    return hashlib.sha256((workspace_id + "\0" + ref).encode()).hexdigest()[:24]


def annotation_template(runs):
    candidates = []
    for run in runs:
        for item in run.get("items", []):
            candidates.append({"item_id": annotation_id(run["workspace_id"], item["ref"]),
                **{key: item[key] for key in ("paper_id", "field", "candidate_text", "source_loc", "quote")},
                "verdict": "pending", "covered_fact_ids": [], "reviewer": "", "note": ""})
    return {"schema_version": 1, "gold_scope_complete": False, "facts": [],
            "adjudication": {"reviewers": [], "disagreements_resolved": False, "record": ""},
            "instructions": "Humans must define source-backed facts and label candidate items. "
                            "Candidate text is model output, not ground truth. Arm, role, and workspace are hidden.",
            "judgments": sorted(candidates, key=lambda item: item["item_id"])}


def apply_annotations(runs, gold):
    by_id = {run["workspace_id"]: run for run in runs if "items" in run}
    facts = {}
    for fact in gold.get("facts", []):
        fid = fact["fact_id"]
        if fid in facts or not fid or not all(str(fact.get(key, "")).strip() for key in
                                             ["paper_id", "field", "statement", "source_loc", "quote", "reviewer"]):
            raise ValueError("Gold facts require unique IDs, source evidence, and an identified human reviewer")
        if fact["field"] not in FIELDS:
            raise ValueError("Gold fact has an unknown field")
        # Textual source validation supplements, but cannot replace, the human semantic judgment.
        for run in by_id.values():
            if fact["paper_id"] not in run["paper_ids"]:
                continue
            project = Project.open(Path(run["workspace"]))
            source = next(p for p in archived_json(project, "idea/reading_input.json")["papers"]
                          if p["paper_id"] == fact["paper_id"])
            from airesearcher.core.models.literature import CardItem
            checked = QuoteVerifier(source["text"]).check(CardItem(
                field=fact["field"], content=fact["statement"], kind="author_claim",
                source_loc=fact["source_loc"], quote=fact["quote"], confidence="high"), source["based_on"])
            if checked.quote_verified is not True:
                raise ValueError("A gold fact's source quotation does not match the frozen reading input")
        if not any(fact["paper_id"] in run["paper_ids"] for run in by_id.values()):
            raise ValueError("Gold fact refers to a paper absent from all evaluated runs")
        facts[fid] = fact
    item_lookup = {annotation_id(rid, item["ref"]): (rid, item["ref"])
                   for rid, run in by_id.items() for item in run["items"]}
    labels = defaultdict(dict)
    for judgment in gold.get("judgments", []):
        if judgment.get("item_id") not in item_lookup:
            raise ValueError("Annotation references must exist in the evaluated runs")
        rid, ref = item_lookup[judgment["item_id"]]
        if rid not in by_id:
            raise ValueError("Annotation refers to an unknown workspace")
        candidates = {item["ref"]: item for item in by_id[rid]["items"]}
        if ref not in candidates or ref in labels[rid]:
            raise ValueError("Annotation references must exist and be unique within a workspace")
        verdict = judgment["verdict"]
        if verdict not in {"pending", "correct", "incorrect"}:
            raise ValueError("Unknown annotation verdict")
        covered = judgment.get("covered_fact_ids", [])
        if verdict == "pending":
            if covered:
                raise ValueError("Pending judgments cannot claim recalled facts")
        elif not judgment.get("reviewer", "").strip():
            raise ValueError("Semantic judgments require an identified human reviewer")
        if covered and verdict != "correct":
            raise ValueError("Only correct items may cover gold facts")
        for fid in covered:
            if fid not in facts or (facts[fid]["paper_id"], facts[fid]["field"]) != (
                    candidates[ref]["paper_id"], candidates[ref]["field"]):
                raise ValueError("Covered facts must belong to the same paper and field as the labeled item")
        labels[rid][ref] = judgment
    for rid, run in by_id.items():
        reviewed = [label for label in labels[rid].values() if label["verdict"] != "pending"]
        denominator = max(run["expected_item_count"], run["observed_item_count"])
        run["annotation_coverage"] = len(reviewed) / denominator
        run["accuracy_on_reviewed_items"] = (
            sum(label["verdict"] == "correct" for label in reviewed) / len(reviewed) if reviewed else None)
        if len(reviewed) == denominator and run["outcome"] == "complete":
            run["semantic_accuracy"] = run["accuracy_on_reviewed_items"]
        eligible = {fid for fid, fact in facts.items() if fact["paper_id"] in run["paper_ids"]}
        found = {fid for label in reviewed for fid in label.get("covered_fact_ids", [])}
        run["gold_fact_count"] = len(eligible)
        has_all_papers = {facts[fid]["paper_id"] for fid in eligible} == set(run["paper_ids"])
        if gold.get("gold_scope_complete") is True and eligible and has_all_papers and len(reviewed) == denominator:
            run["key_information_recall"] = len(found & eligible) / len(eligible)


def evaluate(workspaces, gold=None):
    runs = []
    for workspace in workspaces:
        try:
            runs.append(collect_workspace(workspace))
        except (AirError, ValueError, KeyError, OSError) as exc:
            runs.append({"workspace": str(workspace), "outcome": "invalid_or_incomplete", "error": str(exc)})
    ids = [run["workspace_id"] for run in runs if "workspace_id" in run]
    if len(ids) != len(set(ids)):
        raise ValueError("Each independent run must have a distinct workspace ID")
    if gold is not None:
        apply_annotations(runs, gold)
    groups = defaultdict(list)
    for run in runs:
        if "input_fingerprint" in run:
            groups[run["input_fingerprint"]].append(run)
    comparisons = []
    for fingerprint, group in sorted(groups.items()):
        modes = Counter(run["reading_mode"] for run in group)
        reasons = []
        if not {"single", "multi"} <= modes.keys():
            reasons.append("Both single and multi arms are required for the same frozen inputs")
        if len({tuple(run["models"]) for run in group}) != 1:
            reasons.append("Reading models differ across arms")
        if len({run["llm_budget_usd"] for run in group}) != 1 or any(
                run["llm_budget_usd"] is None for run in group):
            reasons.append("Configured LLM budget caps differ")
        for mode in modes:
            profiles = {json.dumps(run["reader_profiles"]) for run in group if run["reading_mode"] == mode}
            if len(profiles) != 1:
                reasons.append(f"Reader model or prompt versions differ within the {mode} arm")
        if any(run["usage"]["cached_calls"] for run in group):
            reasons.append("Cached reply replay is not an independent live-model repetition")
        if any(run["outcome"] != "complete" for run in group):
            reasons.append("Incomplete runs remain in the denominator")
        comparisons.append({"input_fingerprint": fingerprint, "runs_per_arm": dict(modes),
                            "pilot_comparable": not reasons, "reasons": reasons,
                            "two_repeats_per_arm": modes["single"] >= 2 and modes["multi"] >= 2})
    complete = sum(run["outcome"] == "complete" for run in runs)
    adjudication = (gold or {}).get("adjudication", {})
    reviewers = {str(name).strip() for name in adjudication.get("reviewers", []) if str(name).strip()}
    independent_review = (len(reviewers) >= 2 and adjudication.get("disagreements_resolved") is True
                          and bool(adjudication.get("record", "").strip()))
    return {"schema_version": 1, "new_model_calls": 0, "runs": runs, "comparisons": comparisons,
            "attempted_runs": len(runs), "complete_runs": complete,
            "completion_rate": complete / len(runs) if runs else None,
            "independent_review_complete": independent_review,
            "full_protocol_ready": independent_review and len(comparisons) >= 3 and all(
                group["pilot_comparable"] and group["two_repeats_per_arm"] for group in comparisons)
                and complete == len(runs) and all(run.get("semantic_accuracy") is not None
                    and run.get("key_information_recall") is not None
                    and run["usage"]["elapsed_seconds"] is not None for run in runs),
            "limitations": ["Artifact metrics are not a real-model quality experiment by themselves.",
                            "Missing human labels remain null; matching quotations do not establish factual accuracy.",
                            "Historical runs without reading-step timing keep elapsed_seconds null.",
                            "Costs cover uncached reading/synthesis in this workspace; prior paid history is excluded.",
                            "Independent raters and adjudication must be recorded outside this aggregate."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gold", type=Path)
    parser.add_argument("--init-gold", action="store_true")
    args = parser.parse_args()
    if args.output.exists() or args.gold and args.init_gold:
        parser.error("Use a new output path and choose either --gold or --init-gold")
    if any(args.output.resolve().is_relative_to(workspace.resolve()) for workspace in args.workspace):
        parser.error("Evaluation output must be outside all original research workspaces")
    try:
        gold = json.loads(args.gold.read_text(encoding="utf-8")) if args.gold else None
        report = evaluate(args.workspace, gold)
        result = annotation_template(report["runs"]) if args.init_gold else report
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (AirError, ValueError, KeyError, OSError) as exc:
        parser.exit(1, f"Literature evaluation failed: {exc}\n")
    print(json.dumps({"output": str(args.output), "new_model_calls": 0,
                      "attempted_runs": report["attempted_runs"], "complete_runs": report["complete_runs"],
                      "full_protocol_ready": report["full_protocol_ready"]}))


if __name__ == "__main__":
    main()
