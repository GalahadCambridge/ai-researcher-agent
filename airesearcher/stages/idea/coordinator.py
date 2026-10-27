"""Archive coordinator generations and require explicit decisions after bounded repairs."""

import json

from airesearcher.llm.gateway import extract_json

from .attempts import ATTEMPTS_PER_WINDOW, prepare_window, request_retry
from .models import CoordinatorOutput
from .storage import save_json

ATTEMPTS_PATH = "idea/merge_attempts.json"


def _load(ctx, path):
    return json.loads((ctx.root / path).read_text(encoding="utf-8"))


def _finish(ctx, attempts, record, response, validate):
    """Validate only an archived response; a restart never repeats a completed request."""
    try:
        if response["input_sha256"] != attempts["input_sha256"]:
            raise ValueError("Coordinator response belongs to different frozen input")
        if response.get("finish_reason") == "length":
            raise ValueError("Output reached the length limit; return shorter, complete JSON")
        output = CoordinatorOutput.model_validate_json(extract_json(response["text"]))
        validate(output)
    except (ValueError, KeyError, TypeError) as exc:
        record.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:4000])
        ctx.events.append("literature.merge_failed", "Coordinator output failed validation",
                          actor="agent:idea", data={"error": record["error"],
                                                   "response_path": record["response_path"],
                                                   "call_id": response.get("call_id")})
    else:
        record.update(status="done", output=output.model_dump(mode="json"), trace=response["trace"])
    save_json(ctx, ATTEMPTS_PATH, attempts)


def request_coordinator(ctx, state, digest, variables, validate):
    """Generate at most once per step and twice per explicitly authorized window."""
    attempts = (_load(ctx, ATTEMPTS_PATH) if ctx.archive.latest(ATTEMPTS_PATH)
                else {"input_sha256": digest, "attempts": []})
    if attempts["input_sha256"] != digest:
        raise ValueError("Coordinator attempts belong to different frozen input")
    records = attempts["attempts"]
    if records and records[-1]["status"] == "started":
        record = records[-1]
        if ctx.archive.latest(record["response_path"]):
            _finish(ctx, attempts, record, _load(ctx, record["response_path"]), validate)
    if records and records[-1]["status"] == "done":
        record = records[-1]
        output = CoordinatorOutput.model_validate(record["output"])
        validate(output)
        return {"output": output.model_dump(mode="json"), "trace": record["trace"]}
    start = prepare_window(ctx, state, "merge", ATTEMPTS_PATH, attempts)
    previous = records[-1] if records else {}
    prior = (_load(ctx, previous["response_path"]).get("text", "")
             if previous.get("response_path") and ctx.archive.latest(previous["response_path"]) else "")
    response_path = f"idea/merge_attempts/attempt-{len(records) + 1:04d}.json"
    if ctx.archive.latest(response_path):
        raise ValueError("An archived coordinator response must never be overwritten")
    record = {"status": "started", "response_path": response_path}
    records.append(record)
    save_json(ctx, ATTEMPTS_PATH, attempts)
    try:
        out = ctx.llm.complete(
            role="idea.coordinator", prompt_id="idea/coordinator_merge", tier="strong",
            response_schema=None, max_retries=0,
            variables={**variables, "validation_errors": previous.get("error", ""),
                       "previous_output": ctx.llm.wrap_untrusted(prior[:16000],
                                                                source="previous_invalid_coordinator_output")},
        )
    except Exception as exc:
        record.update(status="interrupted", error=f"Model request failed ({type(exc).__name__}): {exc}"[:4000])
        save_json(ctx, ATTEMPTS_PATH, attempts)
        request_retry(ctx, state, "merge", ATTEMPTS_PATH, attempts,
                      "failed before a response was archived; prior cost may be uncertain")
    trace = {"call_id": out.call_id, "model": out.model, "prompt_id": out.prompt_id,
             "prompt_version": out.prompt_version, "usage": out.usage}
    save_json(ctx, response_path, {"text": out.text, "finish_reason": out.finish_reason,
                                   "trace": trace, "input_sha256": digest})
    _finish(ctx, attempts, record, _load(ctx, response_path), validate)
    if record["status"] == "done":
        return {"output": record["output"], "trace": trace}
    if len(records) - start >= ATTEMPTS_PER_WINDOW:
        request_retry(ctx, state, "merge", ATTEMPTS_PATH, attempts, "exhausted its bounded attempts")
    return None
