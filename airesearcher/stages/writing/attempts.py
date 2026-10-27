"""Durable bounded semantic attempts; explicit human decisions authorize more work."""
from pydantic import BaseModel

from airesearcher.core.models.question import Question, QuestionOption
from airesearcher.engine.stage import Continue, Stop
from airesearcher.llm.gateway import LLMOutputError


class DecisionRequired(Exception):
    def __init__(self, result):
        self.result = result


def complete(ctx, name, load, save, call, validate, *, allow_skip=True):
    journal = load(ctx, name) or {"attempts": [], "window_start": 0, "answers": []}
    records = journal["attempts"]

    def persist():
        save(ctx, name, journal)
        path = f"paper/.writing/{name}.json"
        return ctx.archive.put(path, "other", ctx.root / path, producer="agent:writing/attempts")

    if journal.get("skipped"):
        return None
    if records and records[-1]["status"] == "accepted":
        validate(records[-1]["response"])
        return records[-1]
    if records and records[-1]["status"] == "returned":
        try:
            validate(records[-1]["response"])
        except ValueError as exc:
            records[-1].update(status="rejected", reason=str(exc))
        else:
            records[-1]["status"] = "accepted"
            persist()
            return records[-1]
        persist()
    if records and records[-1]["status"] == "started":
        records[-1].update(status="interrupted", reason="Response not recorded; cost uncertain")
        persist()
    limit = 1 if journal["window_start"] else 2
    interrupted = records and records[-1]["status"] == "interrupted"
    if interrupted or len(records) - journal["window_start"] >= limit:
        text = f"Semantic unit {name} exhausted model responses or was interrupted. Inspect attempts before proceeding."
        answer = ctx.answer
        if answer and answer.question_id not in journal["answers"]:
            record = ctx.questions.record(answer.question_id)
            if (record.question.text != text or record.question.stage != "writing" or record.answer != answer
                    or ctx.questions.records()[-1].question.question_id != answer.question_id
                    or answer.answered_by != "user"):
                raise ValueError("Stale semantic recovery answer")
            journal["answers"].append(answer.question_id)
            journal.setdefault("decisions", []).append(answer.model_dump(mode="json"))
            if answer.choice == "skip" and allow_skip:
                journal["skipped"] = "Model responses exhausted; user skipped; human approval required"
                persist()
                return None
            if answer.choice != "retry":
                raise ValueError("Unexpected semantic recovery answer")
            journal["window_start"] = len(records)
            persist()
        else:
            ref = persist()
            raise DecisionRequired(Stop(text, Question(text=text, refs=[ref], options=[
                QuestionOption(id="retry", label="Retry once (may incur additional model charges)"),
                *([QuestionOption(id="skip", label="Skip unit; semantic review INCOMPLETE; require human approval")]
                  if allow_skip else []),
                QuestionOption(id="end", label="End project", default=True),
            ])))
    attempt = {"number": len(records) + 1, "status": "started"}
    records.append(attempt)
    persist()  # An ambiguous call never becomes a free restart retry.
    try:
        response = call()
        payload = response.parsed.model_dump(mode="json") if isinstance(response.parsed, BaseModel) else response.parsed
        attempt.update(response=payload, raw=response.text, call_id=response.call_id, status="returned",
                       prompt_id=getattr(response, "prompt_id", None),
                       prompt_version=getattr(response, "prompt_version", None))
        persist()  # A completed call is durable before validating or installing output.
    except LLMOutputError as exc:
        attempt.update(status="rejected", reason=str(exc), raw=getattr(exc, "response_text", None),
                       call_id=getattr(exc, "call_id", None))
        persist()
        raise DecisionRequired(Continue("Rejected semantic response retained; bounded retry pending")) from exc
    try:
        validate(attempt["response"])
    except ValueError as exc:
        attempt.update(status="rejected", reason=str(exc))
        persist()
        raise DecisionRequired(Continue("Rejected semantic response retained; bounded retry pending")) from exc
    attempt["status"] = "accepted"
    persist()
    return attempt
