"""批量运行评价轨迹（详细设计 1 第 12.1 节）。"""

from __future__ import annotations

import csv

import pytest
import yaml

from airesearcher.core.project import Project
from airesearcher.eval.batch import run_batch

DEV = {"smoke_seconds": 0.3, "poll_seconds": 1, "compile": False}


def write_config(tmp_path, **over):
    inst = tmp_path / "inst.yaml"
    inst.write_text(yaml.safe_dump({"id": "i1", "idea_text": "比较基线、主方法和消融在冒烟任务上的得分",
                                    "task": "tasks/smoke"}, allow_unicode=True), encoding="utf-8")
    cfg = {"name": "t", "instances": [str(inst)], "repeats": 1, "arms": {"full": {"evidence_check": True}},
           "fixed": {"dev": DEV}, "approval_delay_s": 0, "question_delay_s": 0, "timeout_hours": 0.1, **over}
    path = tmp_path / "batch.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return path


def rows(summary):
    return list(csv.DictReader(summary.open(encoding="utf-8")))


def test_batch_runs_and_resumes(tmp_path):
    cfg = write_config(tmp_path, arms={"full": {"evidence_check": True}, "ablation": {"evidence_check": False}})
    summary = run_batch(cfg, results_dir=tmp_path / "out", log=lambda m: None)
    r = rows(summary)
    assert [(x["arm"], x["outcome"]) for x in r] == [("ablation", "completed"), ("full", "completed")]
    assert all(x["runs_succeeded"] == "9" and x["approvals_auto"] == "4" for x in r)
    ws = tmp_path / "out/t/ablation/i1/rep1"
    assert Project.open(ws).config.evidence_check is False
    assert Project.open(tmp_path / "out/t/full/i1/rep1").config.evidence_check is True
    assert {a.decided_by for a in Project.open(ws).approvals.list()} == {"auto-approve(eval)"}
    # 再跑一次：已完成的轨迹直接用 .eval/result.json，不重新跑
    before = (ws / "research_log.jsonl").read_text(encoding="utf-8")
    assert rows(run_batch(cfg, results_dir=tmp_path / "out", log=lambda m: None)) == r
    assert (ws / "research_log.jsonl").read_text(encoding="utf-8") == before
    with pytest.raises(ValueError, match="没有这些组"):
        run_batch(cfg, only_arms=["nope"], results_dir=tmp_path / "out", log=lambda m: None)


def test_question_policies(tmp_path):
    fail = {"dev": {**DEV, "smoke_fail": {"E2-seed=2": "exit1"}}}
    auto = run_batch(write_config(tmp_path, fixed=fail), results_dir=tmp_path / "a", log=lambda m: None)
    [r] = rows(auto)
    assert r["outcome"] == "completed" and r["questions_asked"] == "1" and r["questions_auto"] == "1"
    assert r["runs_failed"] == "1"  # 默认选项是“跳过这个组合”
    stop = run_batch(write_config(tmp_path, fixed=fail, question_policy="stop"), results_dir=tmp_path / "b",
                     log=lambda m: None)
    [r] = rows(stop)
    assert r["outcome"] == "needs_intervention" and r["final_state"] == "Paused" and r["questions_auto"] == "0"


def test_approval_policy_stop_and_prompt_pins(tmp_path):
    pins = tmp_path / "pins.yaml"
    pins.write_text("example/summarize: 1\n", encoding="utf-8")
    summary = run_batch(write_config(tmp_path, approval_policy="stop", prompt_pins=str(pins)),
                        results_dir=tmp_path / "out", log=lambda m: None)
    [r] = rows(summary)
    assert r["outcome"] == "needs_intervention" and r["final_state"] == "IdeaPending"
    p = Project.open(tmp_path / "out/t/full/i1/rep1")
    assert p.config.prompt_pins == {"example/summarize": 1}


def test_gateway_uses_prompt_pins(project):
    from airesearcher.llm.prompts import PromptNotFound
    from airesearcher.testing.fake_llm import fake_gateway

    llm = fake_gateway(project, {"example/summarize": [{"title": "t", "points": []}]})
    call = {"role": "r", "prompt_id": "example/summarize", "variables": {"goal": "g", "note": ""}}
    assert llm.complete(**call).prompt_version == 1
    project.config.prompt_pins = {"example/summarize": 9}
    with pytest.raises(PromptNotFound):
        llm.complete(**call)
