"""执行前检查：只读本地文件；不下载、不启动训练、不请求 API。"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

from airesearcher.core.models.idea import SelectedIdea
from airesearcher.core.models.plan import ExperimentPlan, PlanTask, PrecheckItem, TaskConfig
from airesearcher.core.models.project import ProjectConfig
from airesearcher.core.workspace import air_home, resolve_task_dir

from .expand import estimate_budget, selected_methods


def item(name: str, problems: list[str], ok: str, *, warning: bool = False) -> PrecheckItem:
    detail = "; ".join(problems) if problems else ok
    if re.search(r"[\u3400-\u9fff]", detail):
        descriptions = {
            "leakage_data": "Verify fixed split hashes, row counts, labels, IDs, and cross-split exact text duplicates",
            "model": "Verify prepared local model/tokenizer files, paths, revisions, and SHA256 checksums",
            "scope": "Verify task, data source, approved methods, and explicit method aliases",
            "license": "Dataset license remains unverified; inspect the dataset card before distribution",
            "metric": "Verify primary/comparison metric names and optimization directions",
            "secondary_metrics": "Verify secondary metric names and optimization directions",
            "leakage_split": "Verify explore/final split roles; final data cannot be used for tuning",
            "config_schema": "Validate every configuration against the schema and available sample counts",
            "coverage": "Verify every approved hypothesis is covered by experiments and comparisons",
            "fairness": "Verify matched data sizes, controls, effective batch sizes, and evaluation conditions",
            "repeats": "Verify matched seed sets (at least three) and equal parameter-combination counts",
            "budget": "Verify remaining wall/GPU budget, stopping fraction, and reserved extra seeds",
            "open_questions": "Review unresolved questions in the exact approved idea version",
            "draft": "The model response did not pass structured draft validation",
            "structure": "The experiment graph or configuration matrix is invalid",
        }
        fields = sorted(set(re.findall(r"[A-Za-z_][A-Za-z0-9_.-]{2,}", detail)))[:20]
        detail = (
            f"{descriptions.get(name, name)}. Issues: {len(problems)}. "
            f"Associated fields/files: {', '.join(fields) or 'see the approved source'}. "
            "This check does not establish semantic validity or scientific quality."
        )
    return PrecheckItem(check=name, status=("warn" if warning else "fail") if problems else "pass", detail=detail)


def inspect_inputs(task: TaskConfig, python=None) -> tuple[list[PrecheckItem], dict]:
    """Delegate domain checks to the task's fixed preparation/check entrypoint."""
    import subprocess
    import sys

    from airesearcher.core.workspace import repo_root
    from airesearcher.runtime.data import inventory

    try:
        refs, changed, missing = inventory(task)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [item("leakage_data", [str(exc)], ""), item("model", [str(exc)], "")], {}
    if not task.data:
        return [
            item("leakage_data", [], "No external data required"),
            item("model", [], "No external model required"),
        ], {}
    failures = ["Missing prepared input: " + x[0] for x in missing]
    failures += ["Input SHA256 mismatch: " + x for x in changed]
    evidence = {"files": {Path(ref.path).name: {"sha256": ref.sha256} for ref in refs}, "checks": []}
    for data in task.data:
        base = Path(os.environ.get("AIR_DATA_DIR", air_home() / "data" / data.name)).expanduser().resolve()
        if not data.prepare:
            continue
        command = [part.format(python=python or sys.executable, data_dir=base) for part in data.prepare] + ["--check"]
        try:
            output = subprocess.run(
                command,
                cwd=repo_root(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=45,
                env={**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
            )
            evidence["checks"].append({"dataset": data.name, "exit_code": output.returncode})
            if output.returncode:
                failures.append(f"{data.name}: task leakage/data check failed")
        except (OSError, subprocess.TimeoutExpired) as exc:
            failures.append(f"{data.name}: task check failed: {exc}")
    return [
        item("leakage_data", failures, "Task-provided leakage checks and prepared file checksums passed"),
        item("model", [x for x in failures if "model" in x.casefold()], "Local model files match pinned checksums"),
    ], evidence


def structural_problems(plan: ExperimentPlan) -> list[str]:
    problems = []
    ids = [e.id for e in plan.experiments]
    comparison_ids = [c.id for c in plan.comparisons]
    if len(ids) != len(set(ids)) or len(comparison_ids) != len(set(comparison_ids)):
        problems.append("Duplicate experiment or comparison IDs")
    graph = {e.id: set(e.depends_on) for e in plan.experiments}
    for e in plan.experiments:
        if e.target_env != "local":
            problems.append(f"{e.id}: only local execution is supported")
        if any(d not in graph for d in e.depends_on):
            problems.append(f"{e.id}: nonexistent dependency")
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node):
        if node in visiting:
            raise ValueError("Cyclic experiment dependencies")
        if node in done:
            return
        visiting.add(node)
        for dep in graph.get(node, set()):
            visit(dep)
        visiting.remove(node)
        done.add(node)

    try:
        for node in graph:
            visit(node)
    except ValueError as exc:
        problems.append(str(exc))
    for c in plan.comparisons:
        if len(set(c.experiments)) < 2 or len(c.experiments) != len(set(c.experiments)):
            problems.append(f"{c.id}: compare at least two distinct experiments")
        if set(c.experiments) - set(ids):
            problems.append(f"{c.id}: nonexistent experiment reference")
    stop = plan.stopping_conditions
    if not math.isfinite(stop.budget_fraction) or not 0 < stop.budget_fraction <= 1:
        problems.append("budget_fraction must be within (0, 1]")
    if stop.max_rounds < 1 or stop.no_progress_rounds < 1:
        problems.append("Stopping round counts must be positive")
    if not 0 <= plan.replication_policy.max_extra_seeds_per_group <= 10:
        problems.append("Extra seed count must be within 0..10")
    return problems


def planning_budget_context(project, idea, used, budget_fraction=0.9):
    """Give the planner the same remaining limits enforced by prechecks."""
    if not math.isfinite(budget_fraction) or not 0 < budget_fraction <= 1:
        raise ValueError("budget_fraction must be within (0, 1]")
    categories = {}
    for name in ("llm_usd", "wall_hours", "gpu_hours"):
        limit = project.budget.get(name)
        bounds = [limit.hard] if limit else []
        if idea.budget.get(name) is not None:
            bounds.append(float(idea.budget[name]))
        caps = [float(x) for x in bounds if x is not None]
        if any(not math.isfinite(x) or x < 0 for x in caps):
            raise ValueError(f"{name}: hard limits must be finite and nonnegative")
        already_used = float(used.get(name, 0))
        if not math.isfinite(already_used) or already_used < 0:
            raise ValueError(f"{name}: recorded usage must be finite and nonnegative")
        cap = min(caps) if caps else None
        categories[name] = {
            "hard_limit": cap,
            "used": already_used,
            "remaining_before_stop_line": max(0, cap * budget_fraction - already_used) if cap is not None else None,
        }
    return {
        "budget_fraction": budget_fraction,
        "categories": categories,
        "run_cost_rule": (
            "For each experiment, grid_size is the Cartesian product of variable-axis lengths (1 if empty). "
            "Formal runs = grid_size * number_of_seeds. Reserved extra runs = grid_size * "
            "max_extra_seeds_per_group. Estimated hours = sum((formal + reserved extra runs) * "
            "est_minutes_per_run) / 60. GPU hours count only experiments using a GPU. "
            "Fit BOTH remaining wall_hours and gpu_hours. Shrink optional grids/extra seeds or use cheaper "
            "approved settings, keeping comparison controls matched. Do not raise limits or invent smaller estimates."
        ),
    }


def run_prechecks(
    plan: ExperimentPlan,
    tasks: list[PlanTask],
    idea: SelectedIdea,
    task: TaskConfig,
    schema: dict | None,
    project: ProjectConfig,
    used: dict[str, float],
    input_checks: list[PrecheckItem],
    evidence: dict,
) -> list[PrecheckItem]:
    checks = list(input_checks)
    scope = []
    try:
        if resolve_task_dir(idea.task["task_config"]) != resolve_task_dir(project.task):
            scope.append("Approved idea task_config differs from this project; direct plan reuse is blocked")
        approved = selected_methods(idea, task)
        planned = {e.method for e in plan.experiments}
        if planned != approved:
            scope.append(f"Planned methods {sorted(planned)} differ from approved idea methods {sorted(approved)}")
        if any(d.source not in {x.source for x in task.data} for d in idea.data):
            scope.append("The idea data source is unsupported by the task")
    except (ValueError, OSError, KeyError) as exc:
        scope.append(str(exc))
    checks.append(item("scope", scope, "Task, data source and approved methods match; only explicit aliases are used"))
    checks.append(
        item(
            "license",
            [
                f"{d.name}: {d.license or 'unspecified'}"
                for d in task.data
                if not d.license or any(x in d.license.casefold() for x in ("unknown", "未核实"))
            ],
            "Data license recorded, or no external data required",
            warning=True,
        )
    )
    available = {m.name: m.direction for m in task.metrics}
    primary = idea.primary_metric()
    metric_errors = []
    if available.get(primary.name) != primary.direction:
        metric_errors.append(f"Primary metric {primary.name} is unavailable or has a different direction")
    metric_errors += [
        f"{c.id}: unavailable comparison metric {c.metric}" for c in plan.comparisons if c.metric not in available
    ]
    checks.append(item("metric", metric_errors, f"Primary metric {primary.name} and comparison metrics are supported"))
    checks.append(
        item(
            "secondary_metrics",
            [m.name for m in idea.metrics if not m.primary and available.get(m.name) != m.direction],
            "Secondary metric names and directions are supported",
            warning=True,
        )
    )
    leakage = []
    for t in tasks:
        role = task.splits.get(t.config.get("eval_split"), {}).get("role")
        if role not in ("explore", "final") or role != t.eval_condition:
            leakage.append(f"{t.task_key}: eval_split role differs from {t.eval_condition}")
        if t.kind == "exploration" and role == "final":
            leakage.append(f"{t.task_key}: exploration cannot use final evaluation")
    checks.append(item("leakage_split", leakage, "Explore/final split roles match the plan"))
    config_errors = []
    if schema is not None:
        from jsonschema import Draft202012Validator

        validator = Draft202012Validator(schema)
        for t in tasks:
            errors = list(validator.iter_errors(t.config))
            experiment = next(e for e in plan.experiments if e.id == t.experiment_id)
            if errors and not experiment.needs_code:
                config_errors.append(f"{t.task_key}: {errors[0].message}")
    elif task.name != "smoke":
        config_errors.append("No configuration schema is available; executable parameters cannot be verified")
    if task.name == "text_cls_lowres" and "train_label_counts" in evidence:
        counts = evidence["train_label_counts"]
        for t in tasks:
            n = t.config.get("train_size", 0)
            if isinstance(n, int) and (n > sum(counts.values()) or n // 2 > min(counts.values())):
                config_errors.append(f"{t.task_key}: training size exceeds locally available data")
    checks.append(item("config_schema", config_errors, "All configurations pass schema and available-data-size checks"))
    unknown_h = {h for e in plan.experiments for h in e.hypotheses} - {h.id for h in idea.hypotheses}
    covered = {h for c in plan.comparisons for e in plan.experiments if e.id in c.experiments for h in e.hypotheses}
    missing = {h.id for h in idea.hypotheses} - covered
    coverage = [f"Uncovered hypotheses: {sorted(missing)}"] if missing else []
    if unknown_h:
        coverage.append(f"Unknown hypothesis references: {sorted(unknown_h)}")
    checks.append(item("coverage", coverage, "All approved idea hypotheses are covered by experiments and comparisons"))
    fairness, repeat_warnings = [], []
    controlled = {"train_size", "model_name", "epochs", "max_length", "eval_split"}
    method_specific = {"learning_rate", "lora_rank", "lora_alpha", "lora_dropout", "classifier_c", "max_features"}
    for c in plan.comparisons:
        scoped = [t for t in tasks if t.experiment_id in c.experiments]
        available_keys = {key for t in scoped for key in t.config}
        invalid_keys = set(c.filter) - available_keys
        if "eval_condition" in c.filter:
            fairness.append(
                f"{c.id}: eval_condition is experiment metadata, not a configuration filter. "
                "Select experiments with the same eval_condition and use filter={} or an actual eval_split value."
            )
        elif invalid_keys:
            fairness.append(
                f"{c.id}: unknown configuration filter keys {sorted(invalid_keys)}; "
                f"available configuration keys: {sorted(available_keys)}"
            )
        if invalid_keys:
            continue
        groups = [
            [
                t for t in scoped
                if t.experiment_id == exp and all(k in t.config and t.config[k] == v for k, v in c.filter.items())
            ]
            for exp in c.experiments
        ]
        if any(not group for group in groups):
            empty = [exp for exp, group in zip(c.experiments, groups, strict=True) if not group]
            fairness.append(
                f"{c.id}: configuration filter {json.dumps(c.filter, sort_keys=True)} matches no tasks for {empty}. "
                "Use configuration values present in those experiments, or filter={} when no filtering is needed."
            )
            continue
        if len({t.eval_condition for group in groups for t in group}) != 1:
            fairness.append(f"{c.id}: a comparison must not mix explore and final experiments")
        if not c.group_by or any(k not in group[0].config for group in groups for k in c.group_by):
            fairness.append(f"{c.id}: empty group_by or unavailable configuration fields")
        varied = {k for e in plan.experiments if e.id in c.experiments for k in e.variables} - method_specific
        keys = sorted(controlled | varied)
        grids = [{json.dumps({k: t.config.get(k) for k in keys}, sort_keys=True) for t in group} for group in groups]

        def batch_key(t):
            size, steps = t.config.get("batch_size", 1), t.config.get("grad_accum_steps", 1)
            return size * steps if type(size) is int and type(steps) is int else "invalid"

        batches = [{batch_key(t) for t in group} for group in groups]
        if any(grid != grids[0] for grid in grids[1:]) or any(b != batches[0] for b in batches[1:]):
            fairness.append(f"{c.id}: unmatched data size, evaluation, training controls or effective batch size")
        seeds = [{t.config["seed"] for t in group} for group in groups]
        if any(s != seeds[0] for s in seeds[1:]) or any(len(s) < 3 for s in seeds):
            repeat_warnings.append(f"{c.id}: unmatched seed sets or fewer than three seeds")
        combinations = [len(group) // len(seeds[i]) for i, group in enumerate(groups)]
        if len(set(combinations)) > 1:
            repeat_warnings.append(f"{c.id}: methods use different grid sizes; review tuning-budget fairness")
    checks.append(item("fairness", fairness, "Compared data sizes, controls and effective batch sizes match"))
    checks.append(
        item(
            "repeats",
            repeat_warnings,
            "Comparisons use matched sets of at least three seeds and equal grid sizes",
            warning=True,
        )
    )
    budget_errors, budget_warnings = [], []
    try:
        plan.budget_estimate = estimate_budget(plan, tasks, task)
        if plan.stopping_conditions.max_rounds > project.limits.max_rounds:
            budget_errors.append("Plan rounds exceed project limits.max_rounds")
        remaining = planning_budget_context(project, idea, used, plan.stopping_conditions.budget_fraction)
        for name in ("wall_hours", "gpu_hours"):
            category = remaining["categories"][name]
            cap = category["hard_limit"]
            if cap is None:
                budget_warnings.append(f"{name}: no hard limit configured")
                continue
            estimate = plan.budget_estimate[name]
            available = category["remaining_before_stop_line"]
            if estimate > available + 1e-12:
                budget_errors.append(
                    f"{name}: estimated {estimate:.4f}; remaining before the stop line {available:.4f}. "
                    f"Hard limit={cap:g}, already used={category['used']:g}, "
                    f"budget_fraction={plan.stopping_conditions.budget_fraction:g}, "
                    f"formal runs={plan.budget_estimate['runs']}, "
                    f"reserved extra runs={plan.budget_estimate['extra_runs_reserved']}. "
                    "Reduce parameter-grid combinations, optional extra seeds or approved training cost; "
                    "keep comparisons fair. Do not increase budget limits or invent lower per-run estimates."
                )
            elif estimate + used.get(name, 0) > cap * 0.6:
                budget_warnings.append(f"{name}: estimated usage exceeds 60% of the budget")
    except (ValueError, TypeError, KeyError) as exc:
        budget_errors.append(str(exc))
    checks.append(
        item(
            "budget",
            budget_errors or budget_warnings,
            "Estimated cost includes permitted extra seeds and stays within the remaining budget",
            warning=not budget_errors,
        )
    )
    checks.append(
        item("open_questions", list(idea.open_questions), "No unresolved questions in the approved idea", warning=True)
    )
    return checks
