"""Durable reading receipts and explicit decisions; never reset historical attempts or costs."""

import json

from airesearcher.core.models.question import Question, QuestionOption
from airesearcher.engine.stage import Stop

from .storage import save_json

ATTEMPTS_PER_WINDOW = 2


def receipt_path(task):
    return f"idea/reading_receipts/{task['card_id']}.json"


def sync_task(task, saved):
    task.update(status=saved["status"], attempts=saved["attempts"], last_error=saved.get("last_error", ""))


def write_receipt(ctx, task, saved):
    sync_task(task, saved)
    save_json(ctx, receipt_path(task), saved)


def load_receipt(ctx, task, source):
    path = receipt_path(task)
    saved = (json.loads((ctx.root / path).read_text(encoding="utf-8"))
             if ctx.archive.latest(path) else
             {"input_sha256": source["input_sha256"], "status": task["status"],
              "attempts": task["attempts"], "last_error": task.get("last_error", "")})
    if saved["input_sha256"] != source["input_sha256"]:
        raise ValueError("Cached reading input does not match the frozen source")
    if type(saved["attempts"]) is not int or saved["attempts"] < 0:
        raise ValueError("Invalid saved reading attempt count")
    if saved["status"] not in {"pending", "started", "done", "failed", "skipped"}:
        raise ValueError("Unknown saved reading task status")
    # Legacy failures have no trustworthy response journal; require a decision, not another paid call.
    if saved.get("format_version") != 2:
        saved.update(format_version=2, history=[], decisions=[], window_start=0)
        if saved["status"] == "pending" and saved["attempts"]:
            saved["status"] = "failed"
            saved["last_error"] = "Legacy reading failure requires explicit retry authorization. " + saved["last_error"]
        write_receipt(ctx, task, saved)
    if saved["status"] == "skipped" and not any(
            d["choice"] == "skip" and d["answer"]["answered_by"] == "user" for d in saved.get("decisions", [])):
        raise ValueError("Missing explicit user authorization for skipped reading")
    sync_task(task, saved)
    return saved


def start_attempt(ctx, task, saved, prompt_version):
    saved["attempts"] += 1
    number = saved["attempts"]
    response_path = f"idea/reading_attempts/{task['card_id']}/attempt-{number:04d}.json"
    if ctx.archive.latest(response_path):
        raise ValueError("An existing reading response must never be overwritten")
    saved["history"].append({"number": number, "status": "started",
                             "prompt_version": prompt_version, "response_path": response_path})
    saved.update(status="started", prompt_version=prompt_version)
    write_receipt(ctx, task, saved)
    return response_path


def mark_failed(ctx, task, saved, error, interrupted=False):
    saved["status"] = "failed"
    saved["last_error"] = error[:2400]
    if saved.get("history"):
        saved["history"][-1].update(status="interrupted" if interrupted else "failed", error=saved["last_error"])
    if not interrupted and saved["attempts"] - saved.get("window_start", 0) < ATTEMPTS_PER_WINDOW:
        saved["status"] = "pending"
    write_receipt(ctx, task, saved)


def reading_decision(ctx, state, tasks, receipts):
    """Only current explicit user answers can retry or acknowledge missing reading."""
    blocked = [task for task in tasks if task["status"] == "failed"]
    if not blocked:
        state.pop("reading_decision", None)
        return None
    waiting = state.get("reading_decision")
    answer = ctx.answer
    if (waiting and answer and answer.question_id == state.get("asked")
            and answer.answered_by == "user" and answer.choice in {"retry", "skip"}):
        targets = waiting["card_ids"]
        if answer.choice == "skip" and not any(task["status"] == "done" for task in tasks):
            raise ValueError("Reading cannot continue with no successful cards")
        for task in tasks:
            if task["card_id"] not in targets:
                continue
            saved = receipts[task["card_id"]]
            if any(d["answer"]["question_id"] == answer.question_id for d in saved["decisions"]):
                continue
            if saved["status"] != "failed":
                raise ValueError("The reading decision no longer matches the blocked tasks")
            saved["decisions"].append({"choice": answer.choice, "answer": answer.model_dump(mode="json"),
                                       "previous_window_start": saved.get("window_start", 0),
                                       "attempts_at_decision": saved["attempts"]})
            saved["status"] = "pending" if answer.choice == "retry" else "skipped"
            saved["window_start"] = saved["attempts"]
            write_receipt(ctx, task, saved)
        state.pop("reading_decision", None)
        state.pop("asked", None)
        ctx.events.append("literature.reading_decision", "User decided how to handle incomplete reading",
                          actor="agent:idea", data={"choice": answer.choice, "card_ids": targets,
                                                    "question_id": answer.question_id})
        return None
    ids = [task["card_id"] for task in blocked]
    state["reading_decision"] = {"card_ids": ids}
    used = ctx.budget.status().categories["llm_usd"].used
    text = (f"Reading is incomplete: {sum(t['status'] == 'done' for t in tasks)}/{len(tasks)} valid cards; "
            f"{len(blocked)} blocked task(s). Project model spend recorded so far: USD {used:.6f}. "
            "Retrying these tasks may incur additional charges; in-flight/failed transport costs may be uncertain. "
            "Inspect the saved attempts before deciding. Blocked tasks: " + ", ".join(ids))
    options = [QuestionOption(id="retry", label="Retry blocked tasks (may incur additional model charges)")]
    if any(task["status"] == "done" for task in tasks):
        options.append(QuestionOption(id="skip", label="Accept missing reading and continue with valid cards"))
    options.append(QuestionOption(id="end", label="End this project", default=True))
    ref = ctx.archive.latest("idea/reading_status.json")
    return Stop("Reading failed; an explicit user decision is required.",
                question=Question(text=text, options=options, refs=[ref] if ref else []))
