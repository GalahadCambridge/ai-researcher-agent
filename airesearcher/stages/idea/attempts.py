"""Authorize bounded selection retries without discarding earlier attempts or costs."""

from airesearcher.core.models.question import Question, QuestionOption
from airesearcher.engine.stage import Stop

from .storage import save_json

ATTEMPTS_PER_WINDOW = 2


class SelectionNeedsDecision(RuntimeError):
    def __init__(self, stop):
        self.stop = stop
        super().__init__(stop.reason)


def request_retry(ctx, state, name, path, attempts, reason):
    attempts["blocked_reason"] = reason
    ref = save_json(ctx, path, attempts)
    state["selection_retry"] = {"name": name, "path": path}
    question = Question(
        text=(f"The {name} model call {reason}. Inspect the saved attempts and usage before retrying. "
              "Retrying may incur additional model charges; existing project budget limits still apply."),
        options=[QuestionOption(id="retry", label="Retry (may incur additional model charges)"),
                 QuestionOption(id="end", label="End this project", default=True)],
        refs=[ref],
    )
    raise SelectionNeedsDecision(Stop("A decision is required before another selection call.", question=question))


def prepare_window(ctx, state, name, path, attempts):
    """Accept each explicit user answer once; old and interrupted attempts remain archived."""
    records = attempts["attempts"]
    start = attempts.get("window_start", 0)
    if type(start) is not int or not 0 <= start <= len(records):
        raise ValueError("Invalid selection attempt-window boundary")
    active = records[start:]
    if active and active[-1]["status"] == "started":
        active[-1].update(status="interrupted",
                          error="The call did not record a completed response; cost is uncertain.")
        save_json(ctx, path, attempts)
    interrupted = active and active[-1]["status"] == "interrupted"
    if not attempts.get("blocked_reason") and not interrupted and len(active) < ATTEMPTS_PER_WINDOW:
        return start
    reason = "was interrupted" if interrupted else "exhausted its bounded attempts"
    waiting = state.get("selection_retry", {})
    answer = ctx.answer
    used = {window["answer"]["question_id"] for window in attempts.get("retry_windows", [])}
    if (answer and answer.choice == "retry" and answer.answered_by == "user"
            and answer.question_id == state.get("asked") and answer.question_id not in used
            and waiting == {"name": name, "path": path}):
        attempts.setdefault("retry_windows", []).append({
            "previous_start": start, "start": len(records), "reason": reason,
            "answer": answer.model_dump(mode="json"),
        })
        attempts["window_start"] = len(records)
        attempts.pop("blocked_reason", None)
        save_json(ctx, path, attempts)
        state.pop("selection_retry", None)
        state.pop("asked", None)
        return len(records)
    request_retry(ctx, state, name, path, attempts, reason)
