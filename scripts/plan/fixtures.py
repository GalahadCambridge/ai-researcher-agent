"""明确标注的测试选题与预设模型回复，不产生研究结论。"""

from __future__ import annotations

from copy import deepcopy

import yaml

from airesearcher.core.models.idea import Baseline, DataSpec, Hypothesis, Method, Metric, SelectedIdea
from airesearcher.stages.plan.expand import canonical_method, load_task


def fixture_idea(task_path: str) -> SelectedIdea:
    _, task, _ = load_task(task_path)
    if task.name == "text_cls_lowres":
        baseline_names = ["TF-IDF + LR", "Full fine-tuning"]
        method_names = ["LoRA"]
    elif task.name == "smoke":
        baseline_names = ["baseline"]
        method_names = ["main", "ablation"]
    else:
        raise ValueError("离线演示内置选题只支持 smoke/text_cls_lowres")
    primary = next(m for m in task.metrics if m.required)
    return SelectedIdea(
        idea_id="idea-plan-fixture",
        title="Offline planning handoff fixture (not a research finding)",
        research_question=(
            "Compare the listed methods with matched sample sizes and random seeds to validate planning handoff."
        ),
        motivation="Offline planning test; the idea, hypothesis and model replies are preset fixtures.",
        hypotheses=[
            Hypothesis(
                id="H1",
                statement="The main method may differ from the baseline on the primary metric (test hypothesis).",
                falsified_if="The prespecified comparison does not support the hypothesized direction.",
            )
        ],
        contributions=["Validate software flow only; no novel research contribution is claimed."],
        task={
            "domain": task.domain,
            "task_config": task_path,
            "description": "Lightweight synthetic workflow test" if task.name == "smoke" else task.description,
        },
        data=[
            DataSpec(name=d.name, source=d.source, license=d.license, usage="Use the prepared fixed data splits")
            for d in task.data
        ],
        baselines=[
            Baseline(id=f"B{i}", name=name, why="Preset test baseline", citations=[])
            for i, name in enumerate(baseline_names, 1)
        ],
        methods=[
            Method(id=f"M{i}", name=name, description="Preset test method", citations=[])
            for i, name in enumerate(method_names, 1)
        ],
        metrics=[Metric(name=m.name, direction=m.direction, primary=m.name == primary.name) for m in task.metrics],
        budget={"wall_hours": 6, "gpu_hours": 2},
        stopping_conditions=["Stop when planned tasks finish or the budget stop line is reached"],
        open_questions=["This is a test idea and cannot support final research conclusions."],
        citations=[],
        scores_ref="idea/scores.json",
        gap_table_ref="idea/gap_table.md",
    )


def preset_draft(idea: SelectedIdea, task_path: str, *, train_size: int = 500) -> dict:
    """按模板填充固定答案；这是测试夹具，不是会理解选题的规划算法。"""
    directory, task, _ = load_task(task_path)
    experiments = []
    baseline_methods = {canonical_method(x.name, task) for x in idea.baselines}
    methods = list(dict.fromkeys(canonical_method(x.name, task) for x in [*idea.baselines, *idea.methods]))
    if len(methods) < 2:
        raise ValueError("演示至少需要两个可比较的方法")
    for index, method in enumerate(methods, 1):
        variables = {}
        if task.name == "text_cls_lowres":
            path = directory / "configs" / f"{method}.yaml"
            fixed = yaml.safe_load(path.read_text(encoding="utf-8"))
            for key in ("method", "seed", "train_size"):
                fixed.pop(key, None)
            fixed["eval_split"] = "dev"
            variables = {"train_size": [train_size]}
            condition = "explore"
        elif task.name == "smoke":
            fixed = {"seconds": 0.01, "eval_split": "test", "fail_mode": "none"}
            condition = "final"
        else:
            raise ValueError("本步演示暂不支持该任务")
        experiments.append(
            {
                "id": f"E{index}",
                "kind": "baseline" if method in baseline_methods else "main",
                "purpose": f"Preset fixture comparison: {method}",
                "hypotheses": [h.id for h in idea.hypotheses],
                "method": method,
                "variables": variables,
                "fixed": fixed,
                "seeds": [0, 1, 2],
                "eval_condition": condition,
                "depends_on": [],
                "target_env": "local",
                "needs_code": False,
                "est_minutes_per_run": float(task.resources.get("est_minutes_per_run", 1)),
                "success_criteria": "All prespecified combinations finish and report required metrics",
                "failure_criteria": "Repair or budget limit reached",
            }
        )
    return {
        "experiments": experiments,
        "comparisons": [
            {
                "id": "C1",
                "question": "Compare methods in the approved idea (preset test response)",
                "experiments": [e["id"] for e in experiments],
                "group_by": ["method", "train_size"] if task.name == "text_cls_lowres" else ["method"],
                "metric": idea.primary_metric().name,
            }
        ],
        "replication_policy": {"max_extra_seeds_per_group": 0, "trigger": "No automatic additional seeds in this test"},
        "stopping_conditions": {"max_rounds": 1, "no_progress_rounds": 1, "budget_fraction": 0.9},
        "approval_points": [
            "Approve the plan before execution",
            "Experiments outside the approved plan",
            "Important round reviews",
            "Final manuscript",
        ],
    }


def leaking_draft(good: dict) -> dict:
    bad = deepcopy(good)
    bad["experiments"][0]["eval_condition"] = "explore"
    bad["experiments"][0]["fixed"]["eval_split"] = "test"
    return bad


def offline_models() -> dict:
    # ollama 只是登记用的供应商格式；FakeLLM 不连接 Ollama，也不要求安装它。
    return {
        "models": {"plan-offline": {"model": "ollama/fake-plan", "price": [0, 0], "max_tokens": 4096}},
        "tiers": {"fast": "plan-offline", "strong": "plan-offline"},
        "stages": {"plan": {"strong": "plan-offline"}},
        "roles": {"exp.planner": "plan-offline"},
        "fallbacks": {"plan-offline": []},
    }
