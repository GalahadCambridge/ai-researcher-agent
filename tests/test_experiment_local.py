"""真实本地执行专项：只运行轻量 smoke 子进程，阻断真实 LLM 调用。"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from airesearcher.core.fsutil import sha256_file
from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.models.run import RunState
from airesearcher.engine.engine import ProjectEngine
from airesearcher.engine.stage import Error
from airesearcher.llm.gateway import LiteLLMBackend, LLMGateway
from airesearcher.runtime.local import LocalExecutor
from airesearcher.services.runs import list_runs
from airesearcher.stages.experiment.stage import ExperimentStage
from airesearcher.testing.fake_llm import fake_gateway
from scripts.experiment.local import configure_local, execute
from scripts.experiment.workspace import copy_pending_plan

from .test_experiment_stage import prepared
from .test_plan_stage import seeded, start_plan


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("真实执行专项测试不允许网络或真实 LLM API")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(LiteLLMBackend, "call", forbidden)


def test_real_trial_pause_and_formal_resume_without_duplicate_runs(air_home):
    project, _, _ = prepared(air_home)
    configure_local(project, "trial")
    result = execute(project, "trial", max_seconds=90)
    assert result["state"] == "Paused" and result["trials_complete"] and not result["complete"]
    trials = list_runs(project.root)
    assert len(trials) == 3 and all(r.record.executor == "local" for r in trials)
    hashes = {r.record.run_id: sha256_file(project.root / "runs" / r.record.run_id / "run.json") for r in trials}
    assert all(not (project.root / "runs" / r.record.run_id / "outputs/SIMULATED.txt").exists() for r in trials)
    configure_local(project, "all")
    result = execute(project, "all", max_seconds=90)
    assert result["state"] == "Analyzing" and result["complete"] and result["successful_formal_tasks"] == 9
    runs = list_runs(project.root)
    assert len(runs) == 12 and all(r.status.state == RunState.succeeded for r in runs)
    assert result["execution_llm_calls"] == result["llm_usd"] == 0
    for r in runs:
        assert r.resources.gpu_seconds == 0
        if r.record.kind != "trial":
            assert all(t.status.ended_at < r.record.created_at for t in trials)
    assert all(sha256_file(project.root / "runs" / rid / "run.json") == digest for rid, digest in hashes.items())


def test_real_execution_still_rejects_real_llm(air_home):
    project, _, _ = prepared(air_home)
    configure_local(project, "trial")
    engine = ProjectEngine(project, executor=LocalExecutor(project), llm=LLMGateway(project))
    ctx = engine.build_context()
    ctx.state = S.Executing
    result = ExperimentStage().step(ctx)
    assert isinstance(result, Error) and "FakeLLM" in result.message and not list_runs(project.root)


def test_local_copy_never_approves_or_changes_source(air_home, tmp_path):
    source, engine, _, _ = seeded(air_home)
    start_plan(engine, source)
    source.save_config(source.config.model_copy(update={
        "experiment": {**source.config.experiment, "llm_backend": "api"},
    }))
    before = sha256_file(source.root / ".state/checkpoint.json")
    copied = copy_pending_plan(source.root, tmp_path / "local-copy", simulation=False)
    assert copied.config.experiment["llm_backend"] == "fake"
    assert source.config.experiment["llm_backend"] == "api"
    assert (copied.root / "artifacts/LOCAL_EXECUTION.json").exists()
    assert not (copied.root / "artifacts/SIMULATION.json").exists()
    assert copied.approvals.pending()[0].status == "pending"
    assert source.approvals.pending()[0].status == "pending"
    assert sha256_file(source.root / ".state/checkpoint.json") == before


@pytest.mark.parametrize("json_format", [True, False])
def test_training_entry_preserves_numeric_learning_rate(tmp_path, json_format):
    template = Path(__file__).resolve().parents[1] / "tasks/text_cls_lowres/template"
    path = tmp_path / "config.yaml"
    path.write_text(
        json.dumps({"learning_rate": 3e-5}) if json_format else "learning_rate: 0.00003\n", encoding="utf-8"
    )
    code = ("import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
            "from run import load_config; cfg,_=load_config(Path(sys.argv[2])); "
            "assert isinstance(cfg['learning_rate'],float); assert cfg['learning_rate']==3e-5")
    subprocess.run([sys.executable, "-c", code, str(template), str(path)], check=True)


def test_changing_backend_on_existing_work_is_rejected(air_home):
    project, old_engine, _ = prepared(air_home)
    old_engine.tick()  # Freeze the fake workspace; no training.
    configure_local(project, "trial")
    engine = ProjectEngine(project, executor=LocalExecutor(project), llm=fake_gateway(project))
    ctx = engine.build_context()
    ctx.state = S.Executing
    result = ExperimentStage().step(ctx)
    assert "切换" in result.reason and not list_runs(project.root)


def test_offline_local_entry_explicitly_keeps_fake_llm(air_home):
    project, _, _, _ = seeded(air_home)
    project.save_config(project.config.model_copy(update={
        "experiment": {**project.config.experiment, "llm_backend": "api"},
    }))
    configure_local(project, "trial")
    assert project.config.experiment["llm_backend"] == "fake"
