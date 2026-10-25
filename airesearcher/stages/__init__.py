"""状态 → 阶段 的注册表（详细设计 1 第 5.3 节），以及“真实实现 / 假实现”的选择。

选择规则（优先级从高到低）：
1. 环境变量 AIR_STAGE_IMPL：如 "fake"（全部用假实现）或 "idea=real,plan=fake"；
2. project.yaml 的 dev.stage_impl：如 {idea: real, plan: fake}；
3. 默认 "fake"（真实阶段还在开发中）。

实现的位置约定：
- 假实现：airesearcher/stages/fakes/<name>.py，提供 create_stage()；
- 真实实现：airesearcher/stages/<name>/stage.py，提供 create_stage()（组员按这个约定新建）；
- "example"：示例阶段 airesearcher/stages/example/stage.py。
"""

from __future__ import annotations

import importlib
import importlib.util
import os
from typing import Any

from airesearcher.core.models.common import ProjectState
from airesearcher.core.models.project import ProjectConfig

STAGE_FOR_STATE = {
    ProjectState.IdeaDrafting: "idea",  # 文献
    ProjectState.PlanDrafting: "plan",  # 实验（是否改归文献，组会决定；代码位置不受影响）
    ProjectState.Executing: "experiment",  # 实验
    ProjectState.Analyzing: "experiment",  # 实验
    ProjectState.Writing: "writing",  # 论文
}
# 等待状态下有活动运行时，调用实验阶段的 background()（见 5.2 附加规则）
BACKGROUND_STAGE = "experiment"
BACKGROUND_STATES = {
    ProjectState.IdeaPending, ProjectState.PlanPending, ProjectState.LogPending,
    ProjectState.ManuscriptPending, ProjectState.Paused, ProjectState.BudgetExhausted,
}
STAGE_NAMES = ("idea", "plan", "experiment", "writing")
DEFAULT_IMPL = "fake"


def real_stage_names() -> list[str]:
    """已经有真实实现（stages/<name>/stage.py）的阶段；“真实模式”建项目时只给它们选 real。"""
    out = []
    for name in STAGE_NAMES:
        try:
            found = importlib.util.find_spec(f"airesearcher.stages.{name}.stage") is not None
        except ModuleNotFoundError:  # 连 stages/<name>/ 包都还没有
            found = False
        if found:
            out.append(name)
    return out


def stage_impl(name: str, config: ProjectConfig | None = None) -> str:
    env = os.environ.get("AIR_STAGE_IMPL", "").strip()
    if env:
        if "=" not in env:
            return env
        for part in env.split(","):
            k, _, v = part.partition("=")
            if k.strip() == name and v.strip():
                return v.strip()
    if config is not None and name in config.dev.stage_impl:
        return config.dev.stage_impl[name]
    return DEFAULT_IMPL


def load_stage(name: str, config: ProjectConfig | None = None) -> Any:
    impl = stage_impl(name, config)
    if name == "example" or impl == "example":
        module = "airesearcher.stages.example.stage"
    elif impl == "fake":
        module = f"airesearcher.stages.fakes.{name}"
    elif impl == "real":
        module = f"airesearcher.stages.{name}.stage"
    else:
        module = impl  # 允许直接写模块路径，如 "mypkg.my_stage"
    try:
        mod = importlib.import_module(module)
    except ModuleNotFoundError as e:
        raise RuntimeError(
            f"阶段 {name} 选择了实现 {impl!r}，但找不到模块 {module}。"
            f"可以在 project.yaml 的 dev.stage_impl 中改为 fake。"
        ) from e
    return mod.create_stage()
