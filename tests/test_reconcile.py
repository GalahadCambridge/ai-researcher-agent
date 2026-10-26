"""Each recovery-table branch in detailed design 3 section 6.5."""

from datetime import timedelta

import pytest

from airesearcher.core.fsutil import now
from airesearcher.core.models.run import RunState
from airesearcher.runtime.local import LocalExecutor

from .test_executor import _spec


@pytest.mark.parametrize("state", [RunState.preparing, RunState.queued, RunState.created])
def test_never_started_states_reconcile_to_queued(project, state):
    ex = LocalExecutor(project)
    spec = _spec(project, ex, "r-queue", {"method": "main"})
    ex.prepare(spec)
    ex._write_status(ex.status("r-queue").model_copy(update={"state": state}))
    assert ex.reconcile("r-queue").state == RunState.queued


@pytest.mark.parametrize(
    "alive,stale,expected",
    [(True, False, RunState.running), (True, True, RunState.unknown), (False, False, RunState.failed)],
)
def test_running_heartbeat_and_identity(project, monkeypatch, alive, stale, expected):
    ex = LocalExecutor(project)
    spec = _spec(project, ex, "r-heartbeat", {"method": "main"})
    ex.prepare(spec)
    status = ex.status("r-heartbeat").model_copy(
        update={"state": RunState.running, "pid": 123, "heartbeat_at": now() - timedelta(seconds=120 if stale else 1)}
    )
    ex._write_status(status)
    monkeypatch.setattr("airesearcher.runtime.local.pid_matches", lambda *args: alive)
    assert ex.reconcile("r-heartbeat").state == expected


def test_missing_or_invalid_status_is_unknown(project):
    ex = LocalExecutor(project)
    assert ex.reconcile("r-absent").state == RunState.unknown
    spec = _spec(project, ex, "r-corrupt", {"method": "main"})
    ex.prepare(spec)
    (spec.run_dir / "status.json").write_text("broken", encoding="utf-8")
    assert ex.reconcile("r-corrupt").state == RunState.unknown
