"""从待审批计划创建独立执行副本；真实与模拟运行严格分开。"""

from __future__ import annotations

import shutil
from pathlib import Path

from airesearcher.core.fsutil import dumps, now
from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.project import Project
from airesearcher.core.workspace import new_project_id
from airesearcher.engine import checkpoint
from airesearcher.services.runs import list_runs
from scripts.plan.fixtures import offline_models


def copy_pending_plan(source: Path, destination: Path, *, simulation: bool = True) -> Project:
    source, destination = source.resolve(), destination.resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("执行工作区必须与原计划工作区分开，不能互相包含")
    original = Project.open(source)
    ck = checkpoint.load(source)
    if ck is None or ck.state != S.PlanPending or not ck.pending_approval:
        raise ValueError("请提供停在 PlanPending 的计划工作区；原计划不会被本演示批准")
    approval = original.approvals.get(ck.pending_approval)
    if approval.kind != "plan" or approval.status != "pending":
        raise ValueError("源项目没有待审批的实验计划")
    if list_runs(source):
        raise ValueError("本步不导入已有运行，避免混入真实结果")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("目标工作区必须为空；请换新目录名，不覆盖已有结果")
    shutil.copytree(source, destination, dirs_exist_ok=True)
    project = Project.open(destination)
    dev = project.config.dev.model_copy(
        update={"stage_impl": {"plan": "real", "experiment": "real", "idea": "fake", "writing": "fake"}}
    )
    title = "FakeExecutor offline execution validation" if simulation else "Real local execution validation"
    config = project.config.model_copy(
        update={
            "project_id": new_project_id(),
            "created_at": now(),
            "title": title,
            "models": offline_models(),
            "experiment": {**project.config.experiment, "llm_backend": "fake", "allow_local_execution": False},
            "dev": dev,
            "llm_cache": "off",
        }
    )
    project.save_config(config, "Create an isolated experiment execution copy", actor="system")
    marker = "artifacts/SIMULATION.json" if simulation else "artifacts/LOCAL_EXECUTION.json"
    project.archive.put(
        marker,
        "other",
        dumps(
            {
                "simulation": simulation,
                "source_workspace": str(source),
                "source_plan_ref": approval.target.model_dump(),
                "warning": (
                    "All runs in this workspace use preset simulated values, not research results."
                    if simulation
                    else "This copy uses FakeLLM during execution and permits real local training. "
                    "Source approval remains unchanged."
                ),
            }
        ),
        producer="system",
    )
    return project
