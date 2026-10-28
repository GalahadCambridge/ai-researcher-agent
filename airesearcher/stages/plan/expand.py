"""计划展开、方法名对接和资源估算；不调用模型、不执行实验。"""

from __future__ import annotations

import itertools
import json
import math
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from airesearcher.core.models.common import VersionRef
from airesearcher.core.models.idea import SelectedIdea
from airesearcher.core.models.plan import (
    Comparison,
    ExperimentPlan,
    ExperimentSpec,
    PlanTask,
    ReplicationPolicy,
    StoppingConditions,
    TaskConfig,
)
from airesearcher.core.workspace import repo_root, resolve_task_dir
from airesearcher.runtime.task import load_task_config, uses_gpu

MAX_TASKS = 256
IDENTIFIER_PATTERN = r"^[A-Za-z][A-Za-z0-9_]*$"
_IDENTIFIER = re.compile(IDENTIFIER_PATTERN)



class DraftExperiment(ExperimentSpec):
    """Expose the existing executor ID restriction in the model's reply schema."""

    id: str = Field(
        pattern=IDENTIFIER_PATTERN,
        description="Start with a letter; use only letters, digits and underscores (e.g. E1). No hyphens or spaces.",
    )


class DraftComparison(Comparison):
    filter: dict[str, Any] = Field(
        default_factory=dict,
        description="Exact matches on task configuration fields only, e.g. eval_split. Not experiment metadata.",
    )

    @field_validator("filter")
    @classmethod
    def configuration_filter(cls, value):
        if "eval_condition" in value:
            raise ValueError(
                "Comparison.filter accepts task configuration fields only. eval_condition is experiment metadata, "
                "not a configuration field. Select experiments with the same eval_condition; use filter={} or "
                "filter={\"eval_split\": \"<actual split name>\"} when eval_split is in the task schema."
            )
        return value


class PlanDraft(BaseModel):
    """模型只起草内容；idea_ref、task_config、预算数字由代码填写。"""

    model_config = ConfigDict(extra="forbid")
    experiments: list[DraftExperiment] = Field(min_length=1, max_length=32)
    comparisons: list[DraftComparison] = Field(min_length=1, max_length=32)
    replication_policy: ReplicationPolicy = Field(
        default_factory=lambda: ReplicationPolicy(max_extra_seeds_per_group=0)
    )
    stopping_conditions: StoppingConditions = Field(default_factory=StoppingConditions)
    approval_points: list[str] = Field(
        default_factory=lambda: ["Out-of-plan experiments", "Round reviews", "Final manuscript"]
    )


def load_task(task_path: str) -> tuple[Path, TaskConfig, dict | None]:
    directory = resolve_task_dir(task_path)
    task = TaskConfig.model_validate(yaml.safe_load((directory / "task.yaml").read_text(encoding="utf-8")))
    schema = None
    if task.config_schema:
        raw = Path(task.config_schema)
        candidates = [raw] if raw.is_absolute() else [repo_root() / raw, directory / raw]
        path = next((p for p in candidates if p.is_file()), None)
        if path is None:
            raise ValueError(f"Configuration schema not found: {task.config_schema}")
        schema = json.loads(path.read_text(encoding="utf-8"))
    return directory, task, schema


def canonical_method(name: str, task: TaskConfig) -> str:
    """仅使用明确列出的别名；LoRA-RITE 等未知方法绝不降级成普通 LoRA。"""
    if name in task.methods_available:
        return name
    def normalize(value):
        return " ".join(value.casefold().split())
    aliases = {normalize(k): v for k, v in (task.model_extra or {}).get("method_aliases", {}).items()}
    aliases.update({normalize(method): method for method in task.methods_available})
    value = aliases.get(normalize(name))
    if value in task.methods_available:
        return value
    raise ValueError(
        f"Method {name!r} is unsupported by this template. Available methods: {task.methods_available}. "
        "Use a canonical name or an explicit task.method_aliases entry; do not substitute another method."
    )


def selected_methods(idea: SelectedIdea, task: TaskConfig) -> set[str]:
    return {canonical_method(x.name, task) for x in [*idea.baselines, *idea.methods]}


def defaults_for(schema: dict | None) -> dict[str, Any]:
    return {k: v["default"] for k, v in (schema or {}).get("properties", {}).items() if "default" in v}


def make_plan(draft: PlanDraft, idea_ref: VersionRef, task_path: str, task: TaskConfig, version: int) -> ExperimentPlan:
    if any(e.model_extra for e in draft.experiments):
        raise ValueError("Experiment entries must use the frozen ExperimentSpec fields")
    experiments = [
        e.model_copy(update={"method": e.method if e.needs_code else canonical_method(e.method, task)}, deep=True)
        for e in draft.experiments
    ]
    return ExperimentPlan(
        plan_id=f"plan-{version:03d}",
        idea_ref=idea_ref,
        task_config=task_path,
        experiments=experiments,
        comparisons=draft.comparisons,
        replication_policy=draft.replication_policy,
        stopping_conditions=draft.stopping_conditions,
        approval_points=draft.approval_points,
    )


def expand_tasks(plan: ExperimentPlan, task: TaskConfig, schema: dict | None) -> list[PlanTask]:
    """笛卡尔积 × 种子；拒绝空轴、重复组合、路径字符和过大矩阵。"""
    required = [m.name for m in task.metrics if m.required]
    tasks: list[PlanTask] = []
    seen: set[str] = set()
    for experiment in plan.experiments:
        e = experiment
        if not _IDENTIFIER.fullmatch(e.id):
            raise ValueError(
                f"Experiment ID {e.id!r} must match {IDENTIFIER_PATTERN}: "
                "start with a letter and use only letters, digits and underscores; no hyphens or spaces."
            )
        if not e.seeds or len(e.seeds) != len(set(e.seeds)):
            raise ValueError(f"{e.id}: seeds must be nonempty and unique")
        if any(type(seed) is not int or not 0 <= seed <= 4294967295 for seed in e.seeds):
            raise ValueError(f"{e.id}: every seed must be an integer in 0..4294967295")
        keys = sorted(e.variables)
        if {"method", "seed"} & (set(e.fixed) | set(keys)) or set(e.fixed) & set(keys):
            raise ValueError(f"{e.id}: fixed/variables overlap or override reserved method/seed fields")
        for key in keys:
            values = e.variables[key]
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or not values:
                raise ValueError(f"{e.id}: variable {key!r} must have a valid configuration name and a nonempty axis")
            if any(type(v) not in (str, int, float, bool) for v in values):
                raise ValueError(f"{e.id}.{key}: variable values must be JSON scalars")
            encoded = [json.dumps(v, allow_nan=False, sort_keys=True) for v in values]
            if len(encoded) != len(set(encoded)):
                raise ValueError(f"{e.id}.{key}: variable values must be unique")
        count = math.prod(len(e.variables[k]) for k in keys) * len(e.seeds)
        if len(tasks) + count > MAX_TASKS:
            raise ValueError(f"The matrix exceeds the {MAX_TASKS}-run limit; reduce parameter-grid sizes")
        for values in itertools.product(*(e.variables[k] for k in keys)):
            for seed in e.seeds:
                config = {
                    **defaults_for(schema),
                    **e.fixed,
                    **dict(zip(keys, values, strict=True)),
                    "method": e.method,
                    "seed": seed,
                }
                json.dumps(config, allow_nan=False)
                parts = [f"{k}={quote(str(v), safe='._')}" for k, v in zip(keys, values, strict=True)]
                key = "-".join([e.id, *parts, f"seed={seed}"])
                if key in seen:
                    raise ValueError(f"Duplicate task key: {key}")
                seen.add(key)
                tasks.append(
                    PlanTask(
                        task_key=key,
                        experiment_id=e.id,
                        kind=e.kind,
                        config=config,
                        eval_condition=e.eval_condition,
                        depends_on=e.depends_on,
                        expected_metrics=required,
                    )
                )
    return tasks


def estimate_budget(plan: ExperimentPlan, tasks: list[PlanTask], task=None) -> dict[str, int | float]:
    task = task or load_task_config(plan.task_config)
    minutes = gpu_minutes = 0.0
    reserved = 0
    for e in plan.experiments:
        if not math.isfinite(e.est_minutes_per_run) or e.est_minutes_per_run <= 0:
            raise ValueError(f"{e.id}: est_minutes_per_run must be finite and positive")
        group = [t for t in tasks if t.experiment_id == e.id]
        combinations = len(group) // len(e.seeds)
        extra = combinations * plan.replication_policy.max_extra_seeds_per_group
        reserved += extra
        cost = (len(group) + extra) * e.est_minutes_per_run
        minutes += cost
        if any(uses_gpu(t.config, task) for t in group):
            gpu_minutes += cost
    return {
        "runs": len(tasks),
        "extra_runs_reserved": reserved,
        "wall_hours": minutes / 60,
        "gpu_hours": gpu_minutes / 60,
    }
