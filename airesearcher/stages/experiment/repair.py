"""Failure classification and bounded recovery (§7.4); never install packages."""

from __future__ import annotations


def error_class(status, stderr):
    text = stderr.casefold()
    if "modulenotfounderror" in text or "importerror" in text:
        return "import_error"
    if status.failure_reason == "oom" or status.exit_code == 137 or "out of memory" in text:
        return "oom"
    if status.failure_reason in ("missing_metrics", "timeout", "cancelled", "lost"):
        return status.failure_reason
    if status.exit_code == 2:
        return "config_error"
    if status.exit_code == 3:
        return "data_error"
    return "runtime_error"


def recovery(entry, task, maximum):
    """Return a finite recovery action; counts are per task and error class."""
    category = entry["error_class"]
    attempts = entry.setdefault("repair_counts", {}).get(category, 0)
    limit = min(1, maximum) if category == "lost" else maximum
    if category == "cancelled" or attempts >= limit:
        return "question", None
    if category == "import_error":
        return "install_question", None
    if category == "data_error":
        return "data", None
    if category == "lost":
        return "retry", None
    if category == "oom" and {"batch_size", "grad_accum_steps"} <= set(task.repair_tunable):
        config = entry["config"]
        batch, steps = config.get("batch_size"), config.get("grad_accum_steps", 1)
        if type(batch) is int and type(steps) is int and batch > 1 and batch % 2 == 0 and steps > 0:
            return "config", {**config, "batch_size": batch // 2, "grad_accum_steps": steps * 2}
    if category == "timeout" and not entry["trial"] and not entry.get("timeout_extended"):
        return "timeout", max(1, int(entry["est_minutes"] * 180), entry.get("timeout_s", 0))
    return "coder", None
