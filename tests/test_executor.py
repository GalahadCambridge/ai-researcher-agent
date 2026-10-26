from __future__ import annotations

import shutil
import time

import pytest
import yaml

from airesearcher.core.fsutil import now
from airesearcher.core.models.common import VersionRef
from airesearcher.core.models.plan import TaskConfig
from airesearcher.core.models.run import RunRecord, RunState
from airesearcher.core.workspace import resolve_task_dir
from airesearcher.runtime.executor import RunSpec
from airesearcher.runtime.local import LocalExecutor, UncertainSubmission, config_sha256, expand_entrypoint


def _spec(project, ex, run_id, config, timeout=30):
    task_dir = resolve_task_dir("tasks/smoke")
    task = TaskConfig.model_validate(yaml.safe_load((task_dir / "task.yaml").read_text(encoding="utf-8")))
    src = project.root / "src"
    shutil.copytree(task_dir / "template", src, dirs_exist_ok=True)
    commit = project.workspace.commit("smoke template")
    ref = VersionRef(artifact_id="plan/experiment_plan.md", version=1, sha256="x")
    d = ex.run_dir(run_id)
    rec = RunRecord(run_id=run_id, task_key="E1-seed=0", experiment_id="E1", kind="smoke", plan_ref=ref,
                    idea_ref=ref, code_commit=commit, config_sha256=config_sha256(config), eval_condition="final",
                    seed=0, command=expand_entrypoint(task, d, ex.python), timeout_s=timeout,
                    required_metrics=["score"], created_at=now())
    return RunSpec(record=rec, run_dir=d, config=config)


def _wait(ex, run_id, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = ex.status(run_id)
        if st.state in (RunState.succeeded, RunState.failed, RunState.cancelled):
            return st
        time.sleep(0.2)
    raise AssertionError(f"run {run_id} did not finish: {ex.status(run_id)}")


@pytest.mark.parametrize("fail_mode,state,reason", [
    ("none", RunState.succeeded, None),
    ("exit1", RunState.failed, "nonzero_exit"),
    ("no_metrics", RunState.failed, "missing_metrics"),
])
def test_smoke_run_outcomes(project, fail_mode, state, reason):
    ex = LocalExecutor(project)
    spec = _spec(project, ex, f"r-test-{fail_mode}", {"method": "main", "seed": 1, "seconds": 0.5,
                                                    "fail_mode": fail_mode})
    ex.prepare(spec)
    assert ex.status(spec.record.run_id).state == RunState.queued
    ex.submit(spec)
    st = _wait(ex, spec.record.run_id)
    assert st.state == state and st.failure_reason == reason
    out = ex.collect(spec.record.run_id)
    if state == RunState.succeeded:
        assert [m.name for m in out.metrics][0] == "score"
    assert project.archive.latest(f"runs/{spec.record.run_id}") is not None
    assert ex.logs(spec.record.run_id, "stdout", 0).text.startswith("smoke:")


def test_timeout_and_cancel(project):
    ex = LocalExecutor(project)
    spec = _spec(project, ex, "r-test-hang", {"method": "main", "seed": 0, "seconds": 0.2, "fail_mode": "hang"},
                 timeout=2)
    ex.prepare(spec)
    ex.submit(spec)
    assert _wait(ex, spec.record.run_id).failure_reason == "timeout"

    spec2 = _spec(project, ex, "r-test-cancel", {"method": "main", "seed": 0, "seconds": 30})
    ex.prepare(spec2)
    ex.submit(spec2)
    time.sleep(1.0)
    ex.cancel(spec2.record.run_id)
    assert _wait(ex, spec2.record.run_id).state == RunState.cancelled


def test_reconcile_lost_run(project):
    ex = LocalExecutor(project)
    spec = _spec(project, ex, "r-test-lost", {"method": "main", "seed": 0, "seconds": 1})
    ex.prepare(spec)
    st = ex.status(spec.record.run_id).model_copy(update={"state": RunState.running, "pid": 999999,
                                                           "wrapper_pid": 999998})
    ex._write_status(st)
    assert ex.reconcile(spec.record.run_id).failure_reason == "lost"


def test_submit_is_idempotent_during_wrapper_startup(project, monkeypatch):
    ex = LocalExecutor(project)
    spec = _spec(project, ex, "r-test-startup", {"method": "main", "seed": 0, "seconds": 1})
    ex.prepare(spec)
    queued = ex.status(spec.record.run_id)
    original = ex.status
    monkeypatch.setattr(ex, "status", lambda _: queued)
    first = ex.submit(spec)
    second = ex.submit(spec)
    assert first.wrapper_pid == second.wrapper_pid and len(ex._children) == 1
    monkeypatch.setattr(ex, "status", original)
    assert _wait(ex, spec.record.run_id).state == RunState.succeeded


def test_ambiguous_submit_intent_is_not_automatically_repeated(project):
    ex = LocalExecutor(project)
    spec = _spec(project, ex, "r-test-intent", {"method": "main", "seed": 0, "seconds": 1})
    ex.prepare(spec)
    (spec.run_dir / "launch.json").write_text('{"wrapper_pid":null}', encoding="utf-8")
    with pytest.raises(UncertainSubmission, match="uncertain"):
        ex.submit(spec)
    assert not ex._children
