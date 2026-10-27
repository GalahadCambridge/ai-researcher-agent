"""Only exact Writing-owned bytes can be restored; conflicting human bytes are archived."""
from airesearcher.core.models.question import Question, QuestionOption
from airesearcher.engine.stage import Continue, Stop

from .writer import SECTIONS


class ReconcileRequired(ValueError):
    def __init__(self, reason, targets):
        super().__init__(reason)
        self.targets = targets


def recover(ctx, conflict, load, save, path, write):
    pending = load(ctx, "reconcile")
    if not pending or pending.get("done"):
        pending = {"reason": str(conflict), "targets": {k: v.hex() if v is not None else None
                   for k, v in conflict.targets.items()}, "before": {}}
        for name in pending["targets"]:
            target = path(ctx, name)  # strict trusted-tree checks, before any read/write
            pending["before"][name] = target.read_bytes().hex() if target.exists() else None
        save(ctx, "reconcile", pending)
    owned = {"paper/claims.jsonl", "paper/air_values.tex"}
    owned.update(f"paper/sections/{section}.tex" for section in SECTIONS)
    owned.update(f"paper/.writing/section-{section}.json" for section in SECTIONS)
    if not pending["targets"] or not set(pending["targets"]) <= owned:
        raise ValueError("Reconciliation target is not a Writing-owned file")
    if set(pending["targets"]) != set(pending["before"]):
        raise ValueError("Reconciliation backup does not cover exact targets")
    text = ("Writing reconciliation: " + pending["reason"]
            + ". Preserve a backup and restore/regenerate exact owned files?")
    answer = ctx.answer
    if answer and not ctx.questions.record(answer.question_id).question.text.startswith("Writing reconciliation:"):
        answer = None
    if answer and answer.question_id != pending.get("answer"):
        record = ctx.questions.record(answer.question_id)
        if (record.question.text != text or record.question.stage != "writing" or record.answer != answer
                or ctx.questions.records()[-1].question.question_id != answer.question_id
                or answer.answered_by != "user" or answer.choice != "restore"):
            raise ValueError("Stale/invalid Writing reconciliation answer")
        # No changed bytes may be silently overwritten after the Question was asked.
        for name, expected in pending["targets"].items():
            target = path(ctx, name)
            current = target.read_bytes().hex() if target.exists() else None
            if current not in (pending["before"][name], expected):
                raise ValueError("File changed after reconciliation Question; preserve and resolve again")
        backup = "paper/.writing/recovery-" + answer.question_id
        save(ctx, backup.removeprefix("paper/.writing/"), pending)
        backup_ref = ctx.archive.put(backup + ".json", "other", ctx.root / (backup + ".json"),
                                     producer="agent:writing/recovery")
        pending["backup_ref"] = backup_ref.model_dump()
        for name, expected in pending["targets"].items():
            target = path(ctx, name)
            if expected is None:
                target.unlink(missing_ok=True)
            else:
                write(target, bytes.fromhex(expected))
        pending.update(done=True, answer=answer.question_id)
        save(ctx, "reconcile", pending)
        return Continue("Exact Writing files restored; normal evidence/structure validation resumes")
    return Stop(text, Question(text=text, options=[
        QuestionOption(id="restore", label="Back up edits; restore exact files / regenerate unowned section"),
        QuestionOption(id="end", label="End project and preserve all files", default=True),
    ]))
