"""Presentation-only attempts, sharing the semantic/section durable budget protocol."""

from airesearcher.core.models.figure import FigureSpec

from . import attempts
from .figures import validate_proposal


def journal_name(fid, key):
    return f"figure-{fid}-{key}-attempts"


def validate(item, fid, value):
    spec = FigureSpec.model_validate(value)
    if (spec.fig_id != fid or spec.aggregate_id != item.aggregate_id
            or spec.metric != item.data.comparison.get("metric")):
        raise ValueError("FigureSpec changed the selected comparison identity")
    validate_proposal(item.data, spec)


def save_attempts(ctx, item, key, name, journal, save):
    identity = {"key": key, "aggregate_ref": item.ref.model_dump(mode="json")}
    prompt = ctx.llm.prompts.load("writing/figure_spec", ctx.project.prompt_pins.get("writing/figure_spec"))
    journal.update(identity)
    for record in journal["attempts"]:
        record.update(identity)
        record.setdefault("prompt_id", prompt.id)
        record.setdefault("prompt_version", prompt.version)
    save(ctx, name, journal)


def reject(ctx, item, fid, key, receipt, reason, load, save):
    """Adopt a legacy cache as a consumed attempt; never delete its raw response."""
    name = journal_name(fid, key)
    journal = load(ctx, name) or {"attempts": [], "window_start": 0, "answers": []}
    if not journal["attempts"]:
        journal["attempts"].append({
            "number": 1, "status": "returned", "response": receipt.get("original_proposal", receipt["spec"]),
            "raw": receipt.get("original_response"), "call_id": receipt.get("call_id"),
            "prompt_id": receipt.get("prompt_id"), "prompt_version": receipt.get("prompt_version"),
            "legacy_receipt": receipt.copy(), "origin": "legacy_cache", "source_key": receipt.get("key"),
        })
    record = journal["attempts"][-1]
    record.update(status="rejected", reason=str(reason), effective_spec=receipt["spec"])
    # Persist the audit record before invalidating the old cache. Repeating a
    # crash here updates the same attempt, never replenishing its budget.
    save_attempts(ctx, item, key, name, journal, save)
    path = f"paper/.writing/{name}.json"
    ctx.archive.put(path, "other", ctx.root / path, producer="agent:writing/figure-planning")
    receipt.update(status="rejected", rejection_reason=str(reason), attempt_journal=name)
    save(ctx, f"figure-{fid}", receipt)


def propose(ctx, item, fid, key, load, save, call):
    name = journal_name(fid, key)

    def request():
        journal = load(ctx, name)
        history = journal.get("attempts", [])
        reason = next((r["reason"] for r in reversed(history) if r.get("reason")), "")
        return call(reason)

    saved = attempts.complete(ctx, name, load,
                              lambda ctx, name, value: save_attempts(ctx, item, key, name, value, save),
                              request, lambda value: validate(item, fid, value), allow_skip=False)
    return {
        "key": key, "original_proposal": saved["response"], "original_response": saved["raw"],
        "spec": saved["response"], "call_id": saved["call_id"], "prompt_id": saved["prompt_id"],
        "prompt_version": saved["prompt_version"], "attempt_journal": name,
        "status": "pending_unit" if not saved["response"]["ylabel"].strip() else "accepted",
    }
