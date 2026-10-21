"""Audit frozen reading replies offline without changing research artifacts or calling a model."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from airesearcher.core.models.idea import SelectedIdea
from airesearcher.core.project import Project
from airesearcher.llm.cache import CacheMiss, LLMCache
from airesearcher.llm.gateway import UNTRUSTED_NOTICE, LLMGateway, extract_json
from airesearcher.stages.idea.goal_scope import validate_goal_scope
from airesearcher.stages.idea.models import ReaderOutput
from airesearcher.stages.idea.quotes import QuoteVerifier
from airesearcher.stages.idea.readers import ROLE_FIELDS


class NoModelBackend:
    def call(self, request):
        raise RuntimeError("Offline quality auditing must never call a model")


def archived_json(project, path):
    ref = project.archive.latest(path)
    if ref is None:
        raise ValueError(f"Missing archived input: {path}")
    return json.loads(project.archive.get(ref))


def audit_cached_reading(workspace):
    project = Project.open(Path(workspace))
    gateway = LLMGateway(project, backend=NoModelBackend())
    manifest = archived_json(project, "idea/reading_input.json")
    status = archived_json(project, "idea/reading_status.json")
    sources = {source["paper_id"]: source for source in manifest["papers"]}
    log = project.root / ".llm/calls.jsonl"
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    calls = {call["call_id"]: call for call in rows}
    items, unavailable = [], []
    for cid in status["card_ids"]:
        receipt = archived_json(project, f"idea/reading_receipts/{cid}.json")
        card = receipt["card"]
        source = sources[card["paper_id"]]
        source_hash = hashlib.sha256(source["text"].encode()).hexdigest()
        if source_hash != source["input_sha256"] or source_hash != receipt["input_sha256"]:
            raise ValueError("The frozen source no longer matches the reading receipt")
        call = calls[card["call_id"]]
        if (call["prompt_id"], call["prompt_version"], call["model"]) != (
                card["prompt_id"], card["prompt_version"], card["model"]):
            raise ValueError("The archived card does not match its call trace")
        prompt = gateway.prompts.load(card["prompt_id"], card["prompt_version"])
        system, user = prompt.render({"research_question": manifest["goal"], "paper_id": source["paper_id"],
            "based_on": source["based_on"], "required_fields": json.dumps(ROLE_FIELDS[card["role"]]),
            "schema_json": ReaderOutput.model_json_schema(),
            "paper_block": gateway.wrap_untrusted(source["text"], source=source["paper_id"])})
        candidates = gateway.resolve(call["role"], card["prompt_id"], "fast", prompt.tier)
        entry = next((m.entry for m in candidates if m.entry.litellm_model == card["model"]), None)
        if entry is None:
            unavailable.append({"card_id": cid, "reason": "original_model_configuration_unavailable"})
            continue
        key = LLMCache.key(entry.litellm_model,
            [{"role": "system", "content": system + UNTRUSTED_NOTICE}, {"role": "user", "content": user}],
            "ReaderOutput", entry.temperature)
        try:
            cached = LLMCache().get(key)
        except CacheMiss:
            unavailable.append({"card_id": cid, "reason": "original_reply_cache_unavailable"})
            continue
        output = ReaderOutput.model_validate_json(extract_json(cached["text"]))
        if {item.field for item in output.items} != set(ROLE_FIELDS[card["role"]]):
            raise ValueError("The cached reply does not cover the required role fields")
        old = {item["field"]: item for item in card["items"]}
        checker = QuoteVerifier(source["text"])
        for index, raw in enumerate(output.items):
            checked, report = checker.check_with_report(raw, source["based_on"])
            items.append({"ref": f"{cid}#{index}", "paper_id": card["paper_id"], **report,
                          "previous_quote_verified": old[raw.field].get("quote_verified"),
                          "rechecked_quote_verified": checked.quote_verified,
                          "cache_key": key, "call_id": card["call_id"]})
    scope = {"status": "no_selected_draft"}
    if project.archive.latest("idea/selected.json"):
        try:
            scope = validate_goal_scope(project.config.goal, SelectedIdea.model_validate(
                archived_json(project, "idea/selected.json")))
        except ValueError as exc:
            scope = {"status": "mismatch", "reason": str(exc)}
    return {"workspace": str(project.root), "mode": "offline_original_reply_recheck", "new_model_calls": 0,
            "archive_modified": False, "items": items, "unavailable": unavailable,
            "previous_quote_matches": sum(i["previous_quote_verified"] is True for i in items),
            "rechecked_quote_matches": sum(i["rechecked_quote_verified"] is True for i in items),
            "rechecked_reasons": dict(Counter(i["reason"] for i in items)), "goal_scope": scope,
            "semantic_accuracy": None,
            "limitations": ("Quote matching is not semantic evaluation. "
                            "No cards, scores, approvals, or drafts were updated.")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new report path; previous audit reports are preserved")
    try:
        report = audit_cached_reading(args.workspace)
        if args.output.resolve().is_relative_to(args.workspace.resolve()):
            parser.error("Save the audit outside the original research workspace")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(1, f"Offline audit failed: {exc}\n")
    print(json.dumps({key: report[key] for key in ("new_model_calls", "previous_quote_matches",
        "rechecked_quote_matches", "rechecked_reasons", "unavailable", "goal_scope")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
