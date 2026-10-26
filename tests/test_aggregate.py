"""Frozen aggregate schema, exclusion, statistics and downstream recomputation."""

import json
import sys
from types import SimpleNamespace

import pytest

from airesearcher.core.fsutil import append_jsonl, now, sha256_file
from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.models.run import RunLabel
from airesearcher.runtime.local import LocalExecutor
from airesearcher.services import runs
from airesearcher.stages.experiment.aggregate import publish_results
from airesearcher.stages.experiment.coder import preflight
from airesearcher.stages.experiment.stage import load_approved
from airesearcher.testing.fake_executor import FakeOutcome

from .helpers import drive
from .test_experiment_stage import prepared


def test_aggregate_exact_schema_and_numeric_recomputation(air_home):
    def outcome(rec, cfg):
        return FakeOutcome(metrics={"score": [0.4, 0.5, 0.6][cfg.get("seed", 0)]}, wall_seconds=0.01)

    project, engine, _ = prepared(air_home, outcomes=outcome)
    drive(engine, S.Analyzing)
    a = runs.load_aggregate(project.root, "C1")
    row = a.rows[0]
    assert row.mean == pytest.approx(0.5) and row.std == pytest.approx(0.1)
    assert row.sem == pytest.approx(0.1 / (3**0.5))
    assert row.ci95 == pytest.approx([0.2515862288, 0.7484137712])
    raw = runs.recompute_aggregate(project.root, "C1")
    assert a.rows == raw.rows and a.contrasts == raw.contrasts
    assert any(e["reason"] == "kind=trial" for e in a.excluded)
    for name in ("C1", "all"):
        for suffix in ("json", "csv", "md"):
            assert (project.root / f"artifacts/aggregates/{name}.{suffix}").exists()
    assert set(json.loads((project.root / "artifacts/aggregates/all.json").read_text(encoding="utf-8"))) == {
        "aggregate_id",
        "comparison",
        "plan_ref",
        "script",
        "eval_condition",
        "rows",
        "contrasts",
        "excluded",
        "cost",
        "created_at",
    }


def test_labels_exclude_invalid_keep_suspicious_and_preserve_raw(air_home):
    project, engine, _ = prepared(air_home)
    drive(engine, S.Analyzing)
    formal = [v for v in runs.list_runs(project.root) if v.record.kind != "trial"]
    raw = {p: sha256_file(p) for p in (project.root / "runs").rglob("metrics.jsonl")}
    for view, label in zip(formal[:2], ["invalid", "suspicious"], strict=True):
        append_jsonl(
            project.root / "artifacts/run_labels.jsonl",
            RunLabel(run_id=view.record.run_id, label=label, reason="test label", by="test", ts=now()),
        )
    ctx = engine.build_context()
    aggregates = publish_results(ctx, load_approved(ctx))
    all_agg = next(a for a in aggregates if a.aggregate_id == "all")
    assert any(e["run_id"] == formal[0].record.run_id for e in all_agg.excluded)
    assert any(formal[1].record.run_id in row.suspicious for row in all_agg.rows)
    assert all(sha256_file(p) == digest for p, digest in raw.items())


def test_preflight_checks_actual_interpreter_without_installing(air_home):
    project, engine, _ = prepared(air_home)
    src = project.root / "src"
    src.mkdir(exist_ok=True)
    ctx = engine.build_context()
    ctx.executor = SimpleNamespace(python=sys.executable)
    for code in ("import absent_air_test_package\n", "def broken(\n", "print(undefined_symbol)\n"):
        (src / "run.py").write_text(code, encoding="utf-8")
        with pytest.raises(ValueError, match="preflight failed"):
            preflight(ctx)
    (src / "run.py").write_text("print('valid')\n", encoding="utf-8")
    preflight(ctx)
    result = json.loads((project.root / "environments/preflight.json").read_text(encoding="utf-8"))
    assert result["python"] == sys.executable


def test_environment_lock_excludes_secrets_and_is_reproducible(air_home, monkeypatch):
    project, engine, _ = prepared(air_home)
    drive(engine, S.Analyzing)
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-secret-not-to-record")
    rec = runs.list_runs(project.root)[0].record.model_copy(update={"executor": "local"})
    env = LocalExecutor(project, python=sys.executable)._env_snapshot(rec)
    assert env["python_executable"] == sys.executable
    assert sha256_file(project.root / env["packages_lock"]) == env["packages_lock_sha256"]
    assert (project.root / env["packages_lock"]).with_name("REPRODUCE.md").exists()
    assert "synthetic-secret-not-to-record" not in json.dumps(env)
