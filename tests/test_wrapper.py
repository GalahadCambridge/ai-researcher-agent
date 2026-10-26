"""Wrapper metrics originate only from tagged stdout, not arbitrary messages."""

import json

import pytest

from airesearcher.core.models.run import RunState
from airesearcher.runtime.local import LocalExecutor

from .test_executor import _spec, _wait


def test_tagged_stdout_is_the_only_metric_source(project):
    ex = LocalExecutor(project)
    spec = _spec(project, ex, "r-metrics-protocol", {"method": "main", "seed": 0, "seconds": 0.01})
    ex.prepare(spec)
    ex.submit(spec)
    assert _wait(ex, spec.record.run_id).state == RunState.succeeded
    lines = (spec.run_dir / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines]
    assert rows and all(set(row) == {"seq", "ts", "name", "value", "split", "step"} for row in rows)
    stdout = (spec.run_dir / "stdout.log").read_text(encoding="utf-8")
    assert len(rows) == sum(line.startswith("@@AIR_METRIC ") for line in stdout.splitlines())


@pytest.mark.parametrize("gpu_requested", [False, True])
def test_gpu_accounting_uses_run_spec_without_task_method_names(project, gpu_requested):
    ex = LocalExecutor(project)
    spec = _spec(project, ex, "r-resource-flag", {
        "method": "queue_policy", "seed": 0, "seconds": 0.01, "device": "cuda",
    })
    spec.gpu = gpu_requested
    ex.prepare(spec)
    ex.submit(spec)
    assert _wait(ex, spec.record.run_id).state == RunState.succeeded
    resources = ex.collect(spec.record.run_id).resources
    assert resources.gpu_seconds == (resources.wall_seconds if gpu_requested else 0)
