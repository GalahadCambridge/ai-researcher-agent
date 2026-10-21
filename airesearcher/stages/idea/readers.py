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
from airesearcher.engine.stage import Continue, Error
from airesearcher.services.literature import load_papers

from .models import GapAnalysis, ReaderOutput
from .quotes import QuoteVerifier
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
    if not source["text"].strip():
        raise ValueError("The paper has no readable full text or abstract")
    role = task["role"]
    prompt = f"idea/{role}_reader"
    out = ctx.llm.complete(
        role=f"idea.reader.{role}", prompt_id=prompt, tier="fast", response_schema=ReaderOutput,
        variables={"research_question": goal, "paper_id": source["paper_id"], "based_on": source["based_on"],
                   "required_fields": json.dumps(ROLE_FIELDS[role]), "schema_json": ReaderOutput.model_json_schema(),
                   "paper_block": ctx.llm.wrap_untrusted(source["text"], source=source["paper_id"])},
    )
    if not out.parsed.relevance_to_question.strip():
        raise ValueError("Relevance to the research question must not be empty")
    audit = []
    card = ReadingCard(card_id=task["card_id"], paper_id=source["paper_id"], role=role,
                       based_on=source["based_on"], items=_validate_items(out.parsed.items, role, source, audit),
                       relevance_to_question=out.parsed.relevance_to_question, model=out.model,
                       prompt_id=out.prompt_id, prompt_version=out.prompt_version,
                       call_id=out.call_id, created_at=now())
    return card, audit


def _save_card(ctx, card):
    path = f"idea/reading_cards/{card.card_id}.json"
    content = card.model_dump(mode="json")
    target = ctx.root / path
    text = json.dumps(content, ensure_ascii=False, indent=2) + "\n"
    if ctx.archive.latest(path) and target.exists() and target.read_text(encoding="utf-8") == text:
        return
    save_json(ctx, path, content, "reading_card")


def read_step(ctx, state):
    started = time.perf_counter()
    try:
        return _read_step(ctx, state)
    finally:
        state["reading_active_seconds"] = state.get("reading_active_seconds", 0.0) + time.perf_counter() - started
        if state.get("phase") == "gap" and ctx.archive.latest("idea/reading_status.json"):
            report = json.loads((ctx.root / "idea/reading_status.json").read_text(encoding="utf-8"))
            report["active_seconds"] = state["reading_active_seconds"]
            report["timing_definition"] = ("Reading-step active wall time; "
                                           "excludes inter-step waits and offline downtime.")
            save_json(ctx, "idea/reading_status.json", report)


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
    for task in tasks:
        path = f"idea/reading_receipts/{task['card_id']}.json"
        if not ctx.archive.latest(path):
            continue
        saved = json.loads((ctx.root / path).read_text(encoding="utf-8"))
        source = sources[task["paper_id"]]
        if saved["input_sha256"] != source["input_sha256"]:
            return Error((
                "Cached reading input does not match; use a separate project or regenerate the "
                "input"
            ), retryable=False)
        if saved["status"] == "done":
            card = ReadingCard.model_validate(saved["card"])
            if (card.card_id, card.paper_id, card.role, card.based_on) != (
                    task["card_id"], task["paper_id"], task["role"], source["based_on"]):
                return Error("Reading-card identity does not match the task", retryable=False)
            card.items = _validate_items(card.items, task["role"], source)
            _save_card(ctx, card)
            if saved.get("quote_checks") is not None:
                save_json(ctx, f"idea/quote_checks/{card.card_id}.json", {"items": saved["quote_checks"],
                          "input_sha256": source["input_sha256"], "call_id": card.call_id})
        task.update(status=saved["status"], attempts=saved["attempts"], last_error=saved.get("last_error", ""))
    pending = [t for t in tasks if t["status"] == "pending"][:6]
    if pending:
        # Workers only call/read/validate; task state and artifact writes stay on the scheduling thread.
        with ThreadPoolExecutor(max_workers=min(concurrency, len(pending), 6)) as pool:
            futures = {pool.submit(_read_one, ctx, sources[t["paper_id"]], t, manifest["goal"]): t for t in pending}
            for future in as_completed(futures):
                task = futures[future]
                task["attempts"] += 1
                saved = {"input_sha256": sources[task["paper_id"]]["input_sha256"],
                         "attempts": task["attempts"], "last_error": ""}
                try:
                    card, audit = future.result()
                    task.update(status="done", last_error="")
                    saved.update(status="done", card=card.model_dump(mode="json"), quote_checks=audit)
                except Exception as exc:
                    task.update(status="failed" if task["attempts"] >= 2 else "pending",
                                last_error=f"{type(exc).__name__}: {exc}")
                    saved.update(status=task["status"], last_error=task["last_error"])
                save_json(ctx, f"idea/reading_receipts/{task['card_id']}.json", saved)
                if task["status"] == "done":
                    _save_card(ctx, card)
        return Continue(f"Reading task progress: {sum(t['status'] == 'done' for t in tasks)}/{len(tasks)} completed")
    cards = [ReadingCard.model_validate_json((ctx.root / f"idea/reading_cards/{t['card_id']}.json").read_text(
        encoding="utf-8")) for t in tasks if t["status"] == "done"]
    items = [item for card in cards for item in card.items]
    audits = []
    for card in cards:
        path = f"idea/quote_checks/{card.card_id}.json"
        if ctx.archive.latest(path):
            audits.extend(json.loads((ctx.root / path).read_text(encoding="utf-8"))["items"])
    report = {"quote_check_reasons": dict(Counter(a["reason"] for a in audits)),
              "reading_mode": manifest["reading_mode"], "planned_tasks": len(tasks), "done": len(cards),
              "failed": [t for t in tasks if t["status"] == "failed"], "card_ids": [c.card_id for c in cards],
              "quote_verified_count": sum(i.quote_verified is True for i in items),
              "quote_failed_count": sum(i.quote_verified is False for i in items),
              "without_quote_count": sum(i.quote_verified is None for i in items),
              "source_warnings": [{"paper_id": p["paper_id"], "warning": p["warning"]}
                                  for p in manifest["papers"] if p["warning"]],
              "note": (
                  "Text matching does not verify semantics or conclusions; failed tasks and items "
                  "without evidence must be retained."
              )}
    save_json(ctx, "idea/reading_status.json", report)
    state["phase"] = "gap"
    return Continue(
        f"Reading cards ready: {len(cards)} cards, {len(report['failed'])} failed tasks; "
        f"gap candidates will be generated next"
    )


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
