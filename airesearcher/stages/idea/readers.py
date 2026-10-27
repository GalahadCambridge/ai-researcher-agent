"""Resumable multi-role reading; at most six tasks per step, with project-configured concurrency."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from airesearcher.core.fsutil import now, sha256_bytes
from airesearcher.core.models.literature import ReadingCard
from airesearcher.engine.stage import Continue, Error, Stop
from airesearcher.llm.gateway import extract_json
from airesearcher.services.literature import load_papers

from .models import GapAnalysis, ReaderOutput, reader_output_schema
from .quotes import QuoteVerifier
from .reading_state import load_receipt, mark_failed, reading_decision, start_attempt, write_receipt
from .storage import save_json

METHOD_FIELDS = ["method", "task", "dataset", "metric", "baseline", "main_result", "setting", "limitation"]
FEASIBILITY_FIELDS = ["code_available", "data_available", "compute", "dependencies", "license", "reproduction_risk"]
ROLE_FIELDS = {"methods": METHOD_FIELDS, "feasibility": FEASIBILITY_FIELDS,
               "single": METHOD_FIELDS + FEASIBILITY_FIELDS}


def card_id(paper_id, role):
    if re.fullmatch(r"arxiv:\d{4}\.\d{4,5}", paper_id):
        base = paper_id.replace(":", "_")
    else:
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", paper_id)[:80]
        base = safe + "-" + hashlib.sha256(paper_id.encode()).hexdigest()[:12]
    return base + "__" + role


def _source(ctx, paper):
    warning = ""
    if paper.access_level == "full_text" and paper.fulltext_path:
        try:
            text = ctx.archive.path_of(paper.fulltext_path).read_text(encoding="utf-8")
            if text.strip():
                words = list(re.finditer(r"\S+", text))
                text = text[:words[11999].end()] if len(words) > 12000 else text
                return text, "full_text", paper.fulltext_path, warning
        except Exception:
            pass
        warning = "Full-text content is missing or unreadable; reading input falls back to the abstract"
    return paper.abstract or "", "abstract_only", None, warning


def _manifest(ctx):
    path = "idea/reading_input.json"
    if not ctx.archive.latest(path):
        papers = []
        for paper in load_papers(ctx.root).values():
            if not paper.selected_for_reading:
                continue
            text, based_on, source_path, warning = _source(ctx, paper)
            papers.append({"paper_id": paper.paper_id, "title": paper.title, "text": text,
                           "based_on": based_on, "source_path": source_path, "warning": warning,
                           "input_sha256": sha256_bytes(text.encode())})
        save_json(ctx, path, {"goal": ctx.project.goal, "reading_mode": ctx.project.reading_mode, "papers": papers})
    return json.loads((ctx.root / path).read_text(encoding="utf-8"))


def _validate_items(items, role, source, audit=None):
    fields = [item.field for item in items]
    if len(fields) != len(set(fields)) or set(fields) != set(ROLE_FIELDS[role]):
        raise ValueError(
            "Reading output must cover each required role field exactly once, without "
            "duplicates or extra fields"
        )
    checker = QuoteVerifier(source["text"])
    checked = [checker.check_with_report(item, source["based_on"]) for item in items]
    if audit is not None:
        audit.extend(report for _, report in checked)
    return [item for item, _ in checked]


def _read_one(ctx, source, task, goal):
    """Generate once; the scheduler saves the raw response before any card validation."""
    role = task["role"]
    version = task["prompt_version"]
    schema = reader_output_schema(role, tuple(ROLE_FIELDS[role])) if version >= 4 else ReaderOutput
    example = {"items": [{"field": field, "content": "Not mentioned", "kind": "agent_inference",
                           "source_loc": "abstract", "quote": None, "confidence": "low"}
                          for field in ROLE_FIELDS[role]], "relevance_to_question": "State the limited relevance."}
    return ctx.llm.complete(
        role=f"idea.reader.{role}", prompt_id=f"idea/{role}_reader", prompt_version=version,
        tier="fast", response_schema=None if version >= 4 else ReaderOutput, max_retries=0,
        variables={"research_question": goal, "paper_id": source["paper_id"], "based_on": source["based_on"],
                   "required_fields": json.dumps(ROLE_FIELDS[role]), "schema_json": schema.model_json_schema(),
                   "example_json": json.dumps(example), "validation_errors": task.get("validation_errors", ""),
                   "previous_output": ctx.llm.wrap_untrusted(task.get("previous_output", ""),
                                                            source="previous_invalid_reading_output"),
                   "paper_block": ctx.llm.wrap_untrusted(source["text"], source=source["paper_id"])},
    )


def _parse_response(response, source, task):
    if response.get("finish_reason") == "length":
        raise ValueError("Output reached the length limit; return a shorter complete JSON object")
    out = ReaderOutput.model_validate_json(extract_json(response["text"]))
    # Diagnose duplicate names explicitly before the fixed tuple reports positional errors.
    fields = [item.field for item in out.items]
    expected = ROLE_FIELDS[task["role"]]
    duplicates = {field: count for field, count in Counter(fields).items() if count > 1}
    missing, extra = sorted(set(expected) - set(fields)), sorted(set(fields) - set(expected))
    if duplicates or missing or extra:
        raise ValueError(f"Required role fields must appear exactly once; missing={missing}, "
                         f"extra={extra}, duplicates={duplicates}. Return one item per field.")
    if response["prompt_version"] >= 4:
        schema = reader_output_schema(task["role"], tuple(expected))
        out = schema.model_validate_json(extract_json(response["text"]))
    if not out.relevance_to_question.strip():
        raise ValueError("Relevance to the research question must not be empty")
    audit = []
    checked = _validate_items(out.items, task["role"], source, audit)
    card = ReadingCard(card_id=task["card_id"], paper_id=source["paper_id"], role=task["role"],
                       based_on=source["based_on"], items=checked,
                       relevance_to_question=out.relevance_to_question, model=response["model"],
                       prompt_id=response["prompt_id"], prompt_version=response["prompt_version"],
                       call_id=response["call_id"], created_at=now())
    return card, audit


def _save_card(ctx, card):
    path = f"idea/reading_cards/{card.card_id}.json"
    content = card.model_dump(mode="json")
    target = ctx.root / path
    text = json.dumps(content, ensure_ascii=False, indent=2) + "\n"
    if ctx.archive.latest(path) and target.exists() and target.read_text(encoding="utf-8") == text:
        return
    save_json(ctx, path, content, "reading_card")


def _finish_response(ctx, task, source, saved, response):
    try:
        if response["input_sha256"] != source["input_sha256"]:
            raise ValueError("Saved reading response belongs to different input")
        card, audit = _parse_response(response, source, task)
    except (ValueError, KeyError, TypeError) as exc:
        mark_failed(ctx, task, saved, f"{type(exc).__name__}: {exc}")
        ctx.events.append("literature.reading_failed", f"Invalid reading card: {task['card_id']}",
                          actor="agent:idea", data={"card_id": task["card_id"], "attempts": saved["attempts"],
                                                   "error": saved["last_error"], "call_id": response.get("call_id")})
        return
    saved.update(status="done", last_error="", card=card.model_dump(mode="json"), quote_checks=audit,
                 response_path=saved["history"][-1]["response_path"])
    saved["history"][-1].update(status="done", call_id=card.call_id)
    write_receipt(ctx, task, saved)
    _save_card(ctx, card)


def _restore_card(ctx, task, source, saved):
    card = ReadingCard.model_validate(saved["card"])
    if (card.card_id, card.paper_id, card.role, card.based_on) != (
            task["card_id"], task["paper_id"], task["role"], source["based_on"]):
        raise ValueError("Reading-card identity does not match the task")
    card.items = _validate_items(card.items, task["role"], source)
    _save_card(ctx, card)
    if saved.get("quote_checks") is not None:
        path = f"idea/quote_checks/{card.card_id}.json"
        data = {"items": saved["quote_checks"], "input_sha256": source["input_sha256"], "call_id": card.call_id}
        if not ctx.archive.latest(path) or json.loads((ctx.root / path).read_text(encoding="utf-8")) != data:
            save_json(ctx, path, data)


def _reading_report(ctx, state, manifest, tasks, sources, receipts):
    cards, audits = [], []
    for task in tasks:
        if task["status"] == "done":
            saved = receipts[task["card_id"]]
            _restore_card(ctx, task, sources[task["paper_id"]], saved)
            cards.append(ReadingCard.model_validate(saved["card"]))
            audits.extend(saved.get("quote_checks", []))
    items = [item for card in cards for item in card.items]
    counts = dict(Counter(t["status"] for t in tasks))
    report = {
        "quote_check_reasons": dict(Counter(a["reason"] for a in audits)),
        "reading_mode": manifest["reading_mode"], "planned_tasks": len(tasks), "done": len(cards),
        "failed": [dict(t) for t in tasks if t["status"] in {"failed", "skipped"}],
        "status_counts": counts, "card_ids": [card.card_id for card in cards],
        "quote_verified_count": sum(i.quote_verified is True for i in items),
        "quote_failed_count": sum(i.quote_verified is False for i in items),
        "without_quote_count": sum(i.quote_verified is None for i in items),
        "source_warnings": [{"paper_id": p["paper_id"], "warning": p["warning"]}
                            for p in manifest["papers"] if p["warning"]],
        "note": "Text matching does not verify semantics. Missing reading requires explicit user acknowledgement.",
        "active_seconds": state.get("reading_active_seconds", 0),
    }
    state["_reading_report"] = report
    failed = counts.get("failed", 0)
    pending = counts.get("pending", 0) + counts.get("started", 0)
    ctx.progress(f"Reading: {len(cards)} valid, {failed} blocked, {pending} pending, "
                 f"{counts.get('skipped', 0)} accepted missing",
                 done=len(cards), total=len(tasks), unit="reading tasks")
    return report


def read_step(ctx, state):
    started, result = time.perf_counter(), None
    try:
        result = _read_step(ctx, state)
        return result
    finally:
        state["reading_active_seconds"] = state.get("reading_active_seconds", 0.0) + time.perf_counter() - started
        report = state.pop("_reading_report", None)
        if report is not None:
            report["active_seconds"] = state["reading_active_seconds"]
            report["timing_definition"] = ("Reading-step active wall time; "
                                           "excludes inter-step waits and offline downtime.")
            path = "idea/reading_status.json"
            previous = (json.loads((ctx.root / path).read_text(encoding="utf-8"))
                        if ctx.archive.latest(path) else {})
            def substantive(value):
                return {k: v for k, v in value.items() if k != "active_seconds"}
            if substantive(previous) != substantive(report):
                save_json(ctx, path, report)
            if isinstance(result, Stop) and result.question is not None:
                result.question.refs = [ctx.archive.latest(path)]


def _read_step(ctx, state):
    concurrency = ctx.project.limits.reader_concurrency
    if concurrency < 1:
        return Error("reader_concurrency must be greater than 0", retryable=False)
    manifest = _manifest(ctx)
    sources = {p["paper_id"]: p for p in manifest["papers"]}
    roles = ["single"] if manifest["reading_mode"] == "single" else ["methods", "feasibility"]
    tasks = state.setdefault("read_tasks", [
        {"paper_id": pid, "role": role, "card_id": card_id(pid, role), "status": "pending", "attempts": 0}
        for pid in sources for role in roles
    ])
    receipts = {}
    try:
        for task in tasks:
            source = sources[task["paper_id"]]
            saved = load_receipt(ctx, task, source)
            receipts[task["card_id"]] = saved
            if saved["status"] == "started":
                path = saved["history"][-1]["response_path"] if saved["history"] else None
                if path and ctx.archive.latest(path):
                    response = json.loads((ctx.root / path).read_text(encoding="utf-8"))
                    _finish_response(ctx, task, source, saved, response)
                else:
                    mark_failed(ctx, task, saved,
                                "Reading was interrupted before a response was saved; prior cost is uncertain.",
                                interrupted=True)
            if saved["status"] == "done":
                _restore_card(ctx, task, source, saved)
            elif not source["text"].strip() and saved["status"] != "skipped":
                if saved.get("retryable") is not False:
                    saved.update(status="failed", retryable=False,
                                 last_error="The frozen input has no readable full text or abstract; "
                                            "retrying cannot supply missing source text.")
                    write_receipt(ctx, task, saved)
        _reading_report(ctx, state, manifest, tasks, sources, receipts)
        decision = reading_decision(ctx, state, tasks, receipts)
        if decision is not None:
            return decision
    except (ValueError, KeyError, TypeError) as exc:
        return Error(f"Reading recovery validation failed: {exc}", retryable=False)

    # Admit only in-flight workers: no queued batch continues charging after a terminal failure.
    pending = sorted((t for t in tasks if t["status"] == "pending"), key=lambda t: -t["attempts"])
    pending = pending[:min(concurrency, 6)]
    if pending:
        with ThreadPoolExecutor(max_workers=len(pending)) as pool:
            futures = {}
            for task in pending:
                if ctx.budget.status().exhausted:
                    break
                source, saved = sources[task["paper_id"]], receipts[task["card_id"]]
                prompt_id = f"idea/{task['role']}_reader"
                version = ctx.llm.prompts.load(prompt_id, ctx.project.prompt_pins.get(prompt_id)).version
                job = {**task, "prompt_version": version, "validation_errors": saved.get("last_error", "")}
                if saved["history"]:
                    old_path = saved["history"][-1]["response_path"]
                    if ctx.archive.latest(old_path):
                        job["previous_output"] = json.loads(
                            (ctx.root / old_path).read_text(encoding="utf-8")).get("text", "")[:12000]
                response_path = start_attempt(ctx, task, saved, version)
                futures[pool.submit(_read_one, ctx, source, job, manifest["goal"])] = (task, response_path)
            for future in as_completed(futures):
                task, response_path = futures[future]
                source, saved = sources[task["paper_id"]], receipts[task["card_id"]]
                try:
                    out = future.result()
                except Exception as exc:
                    mark_failed(ctx, task, saved, f"Model request failed ({type(exc).__name__}): {str(exc)[:1800]}")
                    continue
                response = {"text": out.text, "finish_reason": out.finish_reason, "usage": out.usage,
                            "call_id": out.call_id, "model": out.model, "prompt_id": out.prompt_id,
                            "prompt_version": out.prompt_version, "input_sha256": source["input_sha256"]}
                save_json(ctx, response_path, response)
                _finish_response(ctx, task, source, saved, response)
        _reading_report(ctx, state, manifest, tasks, sources, receipts)
        decision = reading_decision(ctx, state, tasks, receipts)
        return decision or Continue("Reading batch saved; inspect valid, blocked and pending counts.")
    _reading_report(ctx, state, manifest, tasks, sources, receipts)
    state["phase"] = "gap"
    return Continue("Reading complete; missing tasks were explicitly acknowledged before gap analysis.")

def gap_step(ctx, state):
    path = "idea/gaps.json"
    if not ctx.archive.latest(path):
        report = json.loads((ctx.root / "idea/reading_status.json").read_text(encoding="utf-8"))
        cards = [ReadingCard.model_validate_json((ctx.root / f"idea/reading_cards/{cid}.json").read_text(
            encoding="utf-8")) for cid in report["card_ids"]]
        refs = {f"{card.card_id}#{i}": (card, item) for card in cards for i, item in enumerate(card.items)
                if item.field in METHOD_FIELDS}
        trace = {}
        if refs:
            compact = [{"ref": ref, "based_on": card.based_on, **item.model_dump(mode="json")}
                       for ref, (card, item) in refs.items()]
            out = ctx.llm.complete(
                role="idea.gap", prompt_id="idea/gap_analyst", tier="fast", response_schema=GapAnalysis,
                variables={"goal": ctx.project.goal, "schema_json": GapAnalysis.model_json_schema(),
                           "cards_block": ctx.llm.wrap_untrusted(json.dumps(compact, ensure_ascii=False),
                                                                  source="reading_cards")},
            )
            gaps = out.parsed.gaps
            if len({g.gap_id for g in gaps}) != len(gaps) or any(ref not in refs for g in gaps for ref in g.evidence):
                return Error((
                    "Gap candidates contain duplicate IDs or nonexistent reading-card item references"
                ), retryable=True)
            for gap in gaps:
                if not any(refs[r][0].based_on == "full_text" and refs[r][1].kind == "author_claim"
                           and refs[r][1].quote_verified is True for r in gap.evidence):
                    gap.status = "to_verify"
            trace = {"model": out.model, "prompt_id": out.prompt_id,
                     "prompt_version": out.prompt_version, "call_id": out.call_id}
        else:
            gaps = []
        save_json(ctx, path, {"gaps": [g.model_dump(mode="json") for g in gaps], **trace,
                              "incomplete_reading": [t["card_id"] for t in report["failed"]],
                              "note": (
                                  "These are gap candidates within the retrieval scope, pending coordinator "
                                  "review; they do not establish a global research gap or novelty."
                              )})
    state["phase"] = "merge"
    return Continue("Gap candidates saved; coordinator merging and conflict checks come next")
