"""实验调度：模型后端与本地执行分别显式开启，默认保持离线。"""

from __future__ import annotations

import itertools
import json
import math
import queue
import re
import shutil
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from airesearcher.core.errors import InvalidState
from airesearcher.core.fsutil import append_jsonl, dumps, now, rand_hex, sha256_bytes, sha256_file
from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.models.common import VersionRef
from airesearcher.core.models.plan import ExperimentPlan, PlanTask, PrecheckItem, TaskConfig, parse_plan_md
from airesearcher.core.models.question import Question, QuestionOption
from airesearcher.core.models.run import TERMINAL_RUN_STATES, RunLabel, RunRecord, RunState
from airesearcher.core.workspace import repo_root, resolve_task_dir
from airesearcher.engine.stage import Advance, Continue, Error, NeedsApproval, StageContext, Stop, Wait
from airesearcher.llm.models import MissingAPIKey, UnknownModel
from airesearcher.runtime.data import ensure_data, input_refs
from airesearcher.runtime.executor import Executor, RunSpec
from airesearcher.runtime.local import LocalExecutor, UncertainSubmission, config_sha256, config_text, expand_entrypoint
from airesearcher.runtime.task import load_task_config, trial_overrides, uses_gpu
from airesearcher.services import runs as run_service

from .aggregate import publish_results
from .analysis import analyze, checks, decide, fits_budget, log_id, publish_log
from .coder import CoderRollbackRequired, preflight, restore, run_coder, snapshot, source_hashes
from .repair import error_class, recovery

SETTLED = {"done", "skipped", "blocked"}
_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="experiment-coder")
_JOBS = {}
_COMMITS = {}
CODER_DISPATCH_TIMEOUT_S = 330


class ExperimentStage:
    name = "experiment"

    def step(self, ctx: StageContext):
        mode = ctx.project.experiment.get("llm_backend", "fake")
        backend = getattr(ctx.llm, "backend", None)
        if mode not in ("fake", "api") or not callable(getattr(backend, "call", None)) or (
            getattr(backend, "simulation", False) is True
        ) != (mode == "fake"):
            return Error(
                "执行后端与 experiment.llm_backend 不匹配：默认 fake 只接受 FakeLLM；"
                "真实阶段请设置 experiment.llm_backend: api；离线演示请使用 scripts.experiment.demo。",
                retryable=False,
            )
        simulated = getattr(ctx.executor, "simulation", False) is True
        local = not simulated and ctx.project.experiment.get("allow_local_execution") is True
        if not isinstance(ctx.executor, Executor) or not (simulated or local):
            return Error(
                "本地训练需要明确开启 allow_local_execution；模拟执行请使用 FakeExecutor。", retryable=False
            )
        if ctx.state == S.Analyzing:
            return self._review(ctx)
        if ctx.state != S.Executing:
            return Stop("实验执行阶段只能在计划审批通过后的 Executing 状态运行。")
        try:
            approved = load_approved(ctx)
            s = ctx.scratch.setdefault("experiment", {})
            identity = approved.plan_ref.model_dump(mode="json")
            if s.get("plan_ref") != identity:
                if any(e.get("state") == "running" for e in s.get("entries", {}).values()):
                    return Stop("计划发生变化且仍有活动运行，请先处理这些运行，再开始新计划。")
                ready = ensure_data(ctx, approved.task)
                if not ready.ready:
                    return (
                        Wait(ready.detail, seconds=1) if ready.waiting else Stop(ready.detail, question=ready.question)
                    )
                self._setup(ctx, s, approved)
                return Continue(f"{'本地' if local else '模拟'}执行准备完成：冻结配置与代码，每个实验先进行 trial")
            if s["simulation"] != (not local):
                raise ValueError("不能在已有运行工作区内切换真实/模拟执行器；请使用独立工作区")
            if ctx.answer and ctx.answer.question_id == s.get("asked"):
                if s.get("uncertain_submission"):
                    return self._answer_submission(ctx, s)
                if not s.pop("data_question", False):
                    answer = self._answer(ctx, s)
                    if answer:
                        return answer
            if s.get("coder_job") or s.get("code_queue"):
                return self._coding(ctx, s, approved)
            self._guard_workspace(ctx, s)
            self._adopt(ctx, s)
            s["phase"] = "monitor"
            failure = self._monitor(ctx, s)
            if failure:
                return self._repair(ctx, s, approved, failure)
            self._block_dependents(s)
            entries = s["entries"]
            formal = [e for e in entries.values() if not e["trial"]]
            if local and ctx.project.experiment.get("stop_after_trials") is True:
                if all(e["state"] == "done" for e in entries.values() if e["trial"]):
                    return Stop("真实 trial 全部通过；按设置在正式训练前暂停，可显式恢复正式任务。")
            if all(e["state"] in SETTLED for e in formal):
                s["phase"] = "aggregate"
                publish_results(ctx, approved)
                count = sum(e["state"] == "done" for e in formal)
                return Advance(
                    S.Analyzing,
                    f"{'本地' if local else '模拟'}执行本轮结束：{count}/{len(formal)} 个正式任务成功；已保存记录",
                )
            for key, entry in entries.items():
                if entry["state"] != "todo" or not self._ready(entry, s):
                    continue
                schema = _schema(approved.task, approved.task_dir)
                if schema:
                    errors = list(Draft202012Validator(schema).iter_errors(entry["config"]))
                    if errors:
                        return self._question(
                            ctx,
                            s,
                            key,
                            detail="Configuration is unsupported by the protected task schema: " + errors[0].message,
                        )
                if local and ctx.project.experiment.get("stop_after_trials") is True and not entry["trial"]:
                    continue
                active = [e for e in entries.values() if e["state"] == "running"]
                gpu = uses_gpu(entry["config"], approved.task)
                if gpu and any(uses_gpu(e["config"], approved.task) for e in active):
                    continue
                if not gpu and sum(not uses_gpu(e["config"], approved.task) for e in active) >= max(
                    1, ctx.project.permissions.exec.max_parallel_cpu
                ):
                    continue
                if not self._within_budget(ctx, s, entry):
                    if active:
                        return Wait("Collect active runs before budget-stop analysis", seconds=1)
                    s["budget_stop"] = True
                    return Advance(S.Analyzing, "Estimated next-run cost reaches the approved budget stop line.")
                refs = input_refs(ctx.root)
                s["phase"] = "trial" if entry["trial"] else "submit"
                if local and entry["trial"]:
                    try:
                        preflight(ctx)
                    except ValueError as exc:
                        entry.update(
                            state="failed",
                            error_class="import_error" if "missing" in str(exc) else "config_error",
                            failure_detail=str(exc),
                        )
                        return self._repair(ctx, s, approved, key)
                rid = self._prepare(ctx, s, approved, key, entry, refs)
                ctx.executor.submit(self._spec(ctx, rid))
                entry["state"] = "running"
                tag = "[LOCAL]" if local else "[SIMULATED]"
                ctx.events.append(
                    "run.created",
                    f"{tag} 提交 {key}: {rid}",
                    actor="agent:experiment",
                    run_id=rid,
                    data={"simulation": not local, "trial": entry["trial"]},
                )
            done = sum(e["state"] in SETTLED for e in formal)
            ctx.progress(f"{'本地' if local else '模拟'}实验执行：{done}/{len(formal)}", done, len(formal), "runs")
            if not any(e["state"] == "running" for e in entries.values()):
                return Stop("任务无法继续调度，请检查依赖和试运行状态。")
            return Wait(
                "等待本地训练结果" if local else "等待 FakeExecutor 结果（未启动真实进程）", seconds=0.5 if local else 0
            )
        except UncertainSubmission as exc:
            return self._submission_question(ctx, s, exc.run_id)
        except (OSError, ValueError, KeyError, TypeError, InvalidState) as exc:
            return Stop(f"执行前校验或恢复失败：{exc}")

    @staticmethod
    def _submission_question(ctx, state, run_id):
        key = next(key for key, entry in state["entries"].items() if run_id in entry["runs"])
        state["uncertain_submission"] = {"task_key": key, "run_id": run_id}
        text = (f"Run {run_id} has an uncertain launch record. Inspect its processes and logs before deciding. "
                "Retry preserves this attempt as lost and uses a new run ID; no automatic resubmission is allowed.")
        return Stop(text, question=Question(text=text, options=[
            QuestionOption(id="mark_lost_retry", label="I checked: mark this attempt lost and retry with a new ID"),
            QuestionOption(id="end", label="End the project"),
        ]))

    @staticmethod
    def _answer_submission(ctx, state):
        pending = state["uncertain_submission"]
        if ctx.answer.choice != "mark_lost_retry":
            return Stop("Submission recovery ended by the user")
        try:
            status = ctx.executor.mark_submission_lost(pending["run_id"])
        except InvalidState as exc:
            result = ExperimentStage._submission_question(ctx, state, pending["run_id"])
            result.question.text += " " + str(exc)
            return result
        entry = state["entries"][pending["task_key"]]
        if status.state == RunState.failed and status.failure_reason == "lost":
            entry.update(state="failed", error_class="lost", failure_detail="Human-confirmed lost submission")
        else:
            entry["state"] = "running"  # Normal monitoring collects an already-finished attempt.
        ctx.events.append("stage.decision", "User confirmed submission recovery", actor="user", data=dict(pending))
        state.pop("uncertain_submission")
        state.pop("asked", None)
        return Continue("Submission recovery recorded; apply bounded lost-run handling")

    @staticmethod
    def _review(ctx):
        try:
            approved = load_approved(ctx)
            state = ctx.scratch["experiment"]
            review = state.setdefault("review", {})
            feedback = next(
                (f for f in reversed(ctx.feedback) if f.kind == "log" and f.target.artifact_id == log_id(ctx)), None
            )
            latest = ctx.archive.latest(log_id(ctx))
            if (
                feedback
                and feedback.status == "approved"
                and feedback.target == latest
                and review.get("log_ref") == latest.model_dump(mode="json")
            ):
                # Recheck approved evidence and live budget before executing a decision.
                if ctx.archive.current_sha(log_id(ctx)) != latest.sha256:
                    return Stop("Review log changed after approval")
                for ref in ctx.archive.version_info(latest).parents:
                    if ctx.archive.current_sha(ref.artifact_id) != ref.sha256:
                        return Stop("Review evidence changed after approval")
                    if ref.artifact_id.startswith("artifacts/aggregates/"):
                        for source in ctx.archive.version_info(ref).parents:
                            if ctx.archive.current_sha(source.artifact_id) != source.sha256:
                                return Stop("Raw aggregate input changed after review")
                return ExperimentStage._apply_decision(ctx, state, approved, latest)
            if review.get("prepared_round") != state["round"] or (
                feedback and review.get("feedback") != feedback.approval_id
            ):
                state["phase"] = "aggregate"
                aggregates = publish_results(ctx, approved)
                state["phase"] = "check"
                deterministic = checks(ctx, approved, aggregates)
                state["phase"] = "analyze"
                report = analyze(
                    ctx, approved, aggregates, deterministic, feedback.decision_comment if feedback else ""
                )
                state["phase"] = "decide"
                decision = decide(ctx, approved, report, deterministic, aggregates)
                state["analysis_decision"] = decision
                latest = publish_log(
                    ctx,
                    approved,
                    aggregates,
                    report,
                    deterministic,
                    decision,
                    (f"{feedback.approval_id}: " + (feedback.decision_comment or "Review requested"))
                    if feedback
                    else "",
                )
                state["phase"] = "log"
                review.update(
                    prepared_round=state["round"],
                    feedback=feedback.approval_id if feedback else None,
                    log_ref=latest.model_dump(mode="json"),
                )
                if decision["decision"] == "run_next_planned" and not deterministic["anomalies"]:
                    return ExperimentStage._apply_decision(ctx, state, approved, latest)
                return Continue("Aggregates, checked interpretations and the round log are archived.")
            if latest.model_dump(mode="json") != review["log_ref"]:
                return Stop("The round log was edited; request a fresh interpretation before approval")
            return NeedsApproval(
                latest,
                "log",
                "Experiment round review",
                "Approve the documented decision; raw runs remain unchanged.",
                extra_refs=[
                    r
                    for r in ctx.archive.version_info(latest).parents
                    if r.artifact_id.startswith("artifacts/aggregates/")
                ],
            )
        except (MissingAPIKey, UnknownModel) as exc:
            return Error(f"LLM configuration requires correction: {exc}", retryable=False)
        except (OSError, ValueError, KeyError, TypeError, InvalidState) as exc:
            return Stop(f"Analysis failed: {exc}")

    @staticmethod
    def _apply_decision(ctx, state, approved, log):
        decision = state["analysis_decision"]
        bound = "Execution decision SHA256: `" + sha256_bytes(dumps(decision).encode()) + "`."
        if bound not in ctx.archive.read_text(log):
            return Stop("Pending decision does not match the reviewed round log")
        choice = decision["decision"]
        progress = decision.get("progress")
        if progress and not any(h["round"] == progress["round"] for h in state.setdefault("analysis_history", [])):
            state["analysis_history"].append(progress)
        ctx.events.append(
            "stage.decision", f"Round {state['round']}: {choice}", actor="agent:experiment", refs=[log], data=decision
        )
        if choice == "replicate":
            if not fits_budget(ctx, approved, decision["tasks"]):
                state["analysis_decision"]["decision"] = "stop_and_write"
                state["review"] = {}
                state["budget_stop"] = True
                return Continue("Remaining budget changed; create a revised stopping log.")
            ExperimentStage._guard_workspace(ctx, state)
            for i, task in enumerate(decision["tasks"]):
                key = (
                    f"{task['experiment_id']}-replication-round={state['round'] + 1}"
                    f"-cell={i}-seed={task['config']['seed']}"
                )
                entry = ExperimentStage._entry(
                    task["experiment_id"],
                    "replication",
                    task["config"],
                    task["eval_condition"] == "explore",
                    [],
                    False,
                    task["est_minutes"],
                )
                entry["replication_round"] = state["round"] + 1
                state["entries"][key] = entry
                path = ctx.root / "configs" / task["experiment_id"] / f"{key}.yaml"
                path.write_text(config_text(task["config"]), encoding="utf-8")
            state["workspace_commit"] = ctx.workspace.commit(
                "Generate plan-authorized extra seeds", author="agent:experiment"
            )
            state["round"] += 1
            state["review"] = {}
            return Advance(S.Executing, "Execute plan-authorized replication after round-log approval.", refs=[log])
        if choice == "run_next_planned":
            state["round"] += 1
            state["review"] = {}
            return Advance(S.Executing, "Continue the approved experiment plan.", refs=[log])
        target = (
            S.IdeaDrafting
            if choice == "revise_idea"
            else S.PlanDrafting
            if choice in ("revise_plan", "diagnose")
            else S.Writing
        )
        return Advance(target, "Approved experiment decision", refs=[log])

    def _setup(self, ctx, s, approved):
        input_refs(ctx.root)
        template = Path(approved.task.template_dir)
        template = template if template.is_absolute() else repo_root() / template
        if not (ctx.root / "src" / "run.py").exists():
            shutil.copytree(
                template,
                ctx.root / "src",
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
            )
        entries = {}
        for spec in approved.plan.experiments:
            first = next(t for t in approved.tasks if t.experiment_id == spec.id)
            config = dict(first.config)
            explore = next((name for name, meta in approved.task.splits.items() if meta.get("role") == "explore"), None)
            if explore:
                config["eval_split"] = explore
            config.update(trial_overrides(approved.task))
            trial_key = f"trial-{spec.id}"
            if any(t.task_key == trial_key for t in approved.tasks):
                raise ValueError("任务键与 trial 前缀冲突")
            entries[trial_key] = self._entry(
                spec.id, "trial", config, bool(explore), spec.depends_on, True, spec.est_minutes_per_run
            )
        for task in approved.tasks:
            spec = next(e for e in approved.plan.experiments if e.id == task.experiment_id)
            entries[task.task_key] = self._entry(
                task.experiment_id,
                task.kind,
                task.config,
                task.eval_condition == "explore",
                task.depends_on,
                False,
                spec.est_minutes_per_run,
            )
        for key, entry in entries.items():
            path = ctx.root / "configs" / entry["experiment_id"] / f"{key}.yaml"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(config_text(entry["config"]), encoding="utf-8")
        commit = ctx.workspace.commit("冻结实验试运行和正式任务的配置与代码", author="agent:experiment")
        protected = {}
        for name in approved.task.protected_files:
            path = ctx.root / "src" / name
            if not path.is_file():
                raise ValueError(f"模板缺少受保护文件：{name}")
            protected[name] = sha256_file(path)
        s.clear()
        s.update(
            plan_ref=approved.plan_ref.model_dump(mode="json"),
            idea_ref=approved.plan.idea_ref.model_dump(mode="json"),
            commit=commit,
            workspace_commit=commit,
            protected=protected,
            entries=entries,
            phase="code",
            code_queue=[e.id for e in approved.plan.experiments if e.needs_code],
            round=1,
            simulation=getattr(ctx.executor, "simulation", False) is True,
            required_metrics=[m.name for m in approved.task.metrics if m.required],
            source_hashes=source_hashes(ctx.root),
        )

    @staticmethod
    def _entry(experiment, kind, config, explore, dependencies, trial, minutes):
        return {
            "experiment_id": experiment,
            "kind": kind,
            "config": dict(config),
            "trial": trial,
            "eval_condition": "explore" if explore else "final",
            "depends_on": list(dependencies),
            "est_minutes": float(minutes),
            "state": "todo",
            "runs": [],
            "retry_count": 0,
            "repair_counts": {},
            "attempts": {},
        }

    @staticmethod
    def _guard_workspace(ctx, s):
        if s["commit"] != "nogit" and ctx.workspace.head() != s.get("workspace_commit", s["commit"]):
            raise ValueError("工作区代码或配置已改变；本步不自动改代码，请重新验证试运行和审批")
        if source_hashes(ctx.root) != s["source_hashes"]:
            raise ValueError("代码快照与试运行代码不一致")
        for key, entry in s["entries"].items():
            path = ctx.root / "configs" / entry["experiment_id"] / f"{key}.yaml"
            config = json.loads(path.read_text(encoding="utf-8"))
            if config_sha256(config) != config_sha256(entry["config"]):
                raise ValueError(f"配置被修改：{key}")
        for name, checksum in s["protected"].items():
            if sha256_file(ctx.root / "src" / name) != checksum:
                raise ValueError(f"受保护文件变化：{name}")

    def _adopt(self, ctx, s):
        for rv in run_service.list_runs(ctx.root):
            record = rv.record
            if record.plan_ref.model_dump(mode="json") != s["plan_ref"]:
                continue
            entry = s["entries"].get(record.task_key)
            if entry is None:
                raise ValueError("当前计划出现未知任务的运行记录")
            if (
                record.executor != ("fake" if s["simulation"] else "local")
                or record.idea_ref.model_dump(mode="json") != s["idea_ref"]
                or record.config_sha256
                != entry.get("attempts", {}).get(record.run_id, {}).get("config_sha256", config_sha256(entry["config"]))
                or record.code_commit
                != entry.get("attempts", {}).get(record.run_id, {}).get("code_commit", s["commit"])
                or record.kind != entry["kind"]
                or record.eval_condition != entry["eval_condition"]
                or record.experiment_id != entry["experiment_id"]
                or set(record.required_metrics) != set(s["required_metrics"])
            ):
                raise ValueError(f"运行 {record.run_id} 的执行器、选题、配置或代码版本不匹配")
            if record.run_id in entry["runs"]:
                continue
            if entry["runs"] and record.retry_of != entry["runs"][-1]:
                raise ValueError("发现不明确的重复运行，停止以避免重复提交")
            if not entry["runs"] and record.retry_of is not None:
                raise ValueError("重试记录缺少前一次运行")
            entry["runs"].append(record.run_id)
            entry["retry_count"] = record.repair_attempt
            entry["state"] = "running"

    @staticmethod
    def _spec(ctx, rid):
        d = ctx.executor.run_dir(rid)
        record = RunRecord.model_validate_json((d / "run.json").read_text(encoding="utf-8"))
        config = json.loads((d / "config.yaml").read_text(encoding="utf-8"))
        if config_sha256(config) != record.config_sha256:
            raise ValueError("运行配置快照被修改")
        environment_path = d / "environment.json"
        environment = json.loads(environment_path.read_text(encoding="utf-8")) if environment_path.exists() else {}
        gpu = environment.get("gpu_requested")
        if gpu is None:  # Compatibility with runs prepared before resource metadata was recorded.
            gpu = uses_gpu(config, load_task_config(ctx.project.task))
        if type(gpu) is not bool:
            raise ValueError("Invalid archived gpu_requested flag")
        return RunSpec(
            record=record, run_dir=d, config=config, trial=record.kind == "trial",
            gpu=gpu,
        )

    def _monitor(self, ctx, s, background=False):
        for key, entry in s["entries"].items():
            if entry["state"] == "failed":
                if background:
                    continue
                return key
            if entry["state"] != "running":
                continue
            rid = entry["runs"][-1]
            status = ctx.executor.status(rid)
            if status.state in (RunState.created, RunState.preparing, RunState.queued):
                if background:
                    continue
                spec = self._spec(ctx, rid)
                if status.state != RunState.queued:
                    ctx.executor.prepare(spec)
                ctx.executor.submit(spec)
                continue
            if status.state == RunState.unknown:
                raise ValueError(f"运行 {rid} 状态未知，不能自动重复提交")
            if status.state not in TERMINAL_RUN_STATES:
                continue
            first = ctx.archive.latest(f"runs/{rid}") is None
            result = ctx.executor.collect(rid)
            if first:
                tag = "[SIMULATED]" if s["simulation"] else "[LOCAL]"
                ctx.events.append(
                    "run.status_changed", f"{tag} {rid}: {status.state.value}", actor="agent:experiment", run_id=rid
                )
            invalid = []
            if status.state == RunState.succeeded:
                for metric in self._spec(ctx, rid).record.required_metrics:
                    split = "train" if metric == "train_seconds" else entry["config"].get("eval_split", "test")
                    if not any(
                        m.name == metric and m.split == split and math.isfinite(m.value) for m in result.metrics
                    ):
                        invalid.append(f"{metric}@{split}")
                if invalid:
                    labels = run_service.load_labels(ctx.root)
                    if rid not in labels or labels[rid].label != "invalid":
                        append_jsonl(
                            ctx.root / "artifacts/run_labels.jsonl",
                            RunLabel(
                                run_id=rid,
                                label="invalid",
                                reason=f"输出缺少有效指标：{invalid}",
                                by="agent:experiment",
                                ts=now(),
                            ),
                        )
            if status.state == RunState.succeeded and not invalid:
                entry["state"] = "done"
                entry["success_commit"] = self._spec(ctx, rid).record.code_commit
            else:
                entry["state"] = "failed"
                stderr = ctx.executor.logs(rid, "stderr", 0).text[-2000:]
                entry["error_class"] = "missing_metrics" if invalid else error_class(status, stderr)
                entry["failure_detail"] = f"{entry['error_class']}: {invalid or stderr or status.failure_reason}"
                if not background:
                    return key
        return None

    def background(self, ctx):
        """暂停/等待时只收集已结束的记录，绝不提交或重新提交。"""
        allowed = isinstance(ctx.executor, Executor) and (
            getattr(ctx.executor, "simulation", False) is True
            or ctx.project.experiment.get("allow_local_execution") is True
        )
        if not allowed or not ctx.scratch.get("experiment", {}).get("entries"):
            return
        s = ctx.scratch["experiment"]
        try:
            self._monitor(ctx, s, background=True)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            ctx.log(f"运行后台收集失败：{exc}")

    @staticmethod
    def _ready(entry, s):
        if not s["simulation"] and not entry["trial"]:
            if any(e["trial"] and not e.get("coder_trial") and e["state"] != "done" for e in s["entries"].values()):
                return False
        if any(
            e["state"] != "done"
            for e in s["entries"].values()
            if not e["trial"] and e["experiment_id"] in entry["depends_on"]
        ):
            return False
        return entry["trial"] or (
            s["entries"][f"trial-{entry['experiment_id']}"]["state"] == "done"
            and s["entries"][f"trial-{entry['experiment_id']}"]["success_commit"] == s["commit"]
        )

    @staticmethod
    def _block_dependents(s):
        changed = True
        while changed:
            changed = False
            for entry in s["entries"].values():
                if entry["state"] != "todo":
                    continue
                trial = s["entries"][f"trial-{entry['experiment_id']}"]
                blocked_trial = not entry["trial"] and trial["state"] in ("skipped", "blocked")
                blocked_parent = any(
                    e["state"] in ("skipped", "blocked")
                    for e in s["entries"].values()
                    if not e["trial"] and e["experiment_id"] in entry["depends_on"]
                )
                if blocked_trial or blocked_parent:
                    entry["state"] = "blocked"
                    changed = True

    @staticmethod
    def _within_budget(ctx, s, entry):
        active = [e for e in s["entries"].values() if e["state"] == "running"]
        return fits_budget(ctx, load_approved(ctx), [*active, entry])

    @staticmethod
    def _prepare(ctx, s, approved, key, entry, refs):
        rid = f"r-{now():%Y%m%d-%H%M%S}-{rand_hex(4)}"
        d = ctx.executor.run_dir(rid)
        while d.exists():
            rid = f"r-{now():%Y%m%d-%H%M%S}-{rand_hex(4)}"
            d = ctx.executor.run_dir(rid)
        record = RunRecord(
            run_id=rid,
            task_key=key,
            experiment_id=entry["experiment_id"],
            kind=entry["kind"],
            plan_ref=approved.plan_ref,
            idea_ref=approved.plan.idea_ref,
            executor="fake" if s["simulation"] else "local",
            code_commit=s["commit"],
            config_sha256=config_sha256(entry["config"]),
            inputs=refs,
            eval_condition=entry["eval_condition"],
            seed=entry["config"].get("seed"),
            command=expand_entrypoint(approved.task, d, ctx.executor.python, trial=entry["trial"]),
            timeout_s=int(
                entry.get(
                    "timeout_s",
                    ctx.project.experiment.get(
                        "trial_timeout_s" if entry["trial"] else "run_timeout_s", 300 if entry["trial"] else 600
                    ),
                )
            ),
            retry_of=entry["runs"][-1] if entry["runs"] else None,
            repair_attempt=entry["retry_count"],
            required_metrics=[m.name for m in approved.task.metrics if m.required],
            created_at=now(),
        )
        ctx.executor.prepare(
            RunSpec(
                record=record, run_dir=d, config=entry["config"], trial=entry["trial"],
                gpu=uses_gpu(entry["config"], approved.task),
            )
        )
        if s["simulation"]:
            (d / "outputs/SIMULATED.txt").write_text(
                "SIMULATED: preset FakeExecutor values, no training; not research evidence.\n", encoding="utf-8"
            )
        entry.setdefault("attempts", {})[rid] = {
            "code_commit": record.code_commit,
            "config_sha256": record.config_sha256,
        }
        entry["runs"].append(rid)
        entry["state"] = "running"
        return rid

    def _repair(self, ctx, state, approved, key):
        entry = state["entries"][key]
        self._monitor(ctx, state, background=True)
        if any(e["state"] == "running" for e in state["entries"].values()):
            return Wait("Collect other active runs before changing code", seconds=1)
        action, value = recovery(entry, approved.task, max(0, ctx.project.limits.max_repair_retries))
        if action in ("question", "install_question"):
            return self._question(ctx, state, key, install=action == "install_question")
        if action == "data":
            ready = ensure_data(ctx, approved.task)
            if not ready.ready:
                state["data_question"] = ready.question is not None
                return Wait(ready.detail, seconds=1) if ready.waiting else Stop(ready.detail, question=ready.question)
        counts = entry.setdefault("repair_counts", {})
        counts[entry["error_class"]] = counts.get(entry["error_class"], 0) + 1
        entry["retry_count"] += 1
        ctx.events.append(
            "stage.decision",
            f"{key}: {entry['error_class']} -> {action}",
            actor="agent:experiment",
            data={"counts": counts},
        )
        if action == "data":
            entry["state"] = "todo"
        elif action == "config":
            entry["config"] = value
            self._write_config(ctx, key, entry)
            state["workspace_commit"] = ctx.workspace.commit(
                "Preserve effective batch size after OOM", author="agent:experiment"
            )
            entry["state"] = "todo"
        elif action == "timeout":
            entry.update(timeout_s=value, timeout_extended=True, state="todo")
        elif action == "retry":
            entry["state"] = "todo"
        else:
            state["code_queue"] = [entry["experiment_id"]]
            state["repair_key"] = key
        return Continue(f"Bounded repair: {action}")

    @staticmethod
    def _write_config(ctx, key, entry):
        path = ctx.root / "configs" / entry["experiment_id"] / (key + ".yaml")
        path.write_text(config_text(entry["config"]), encoding="utf-8")

    def _coding(self, ctx, state, approved):
        state["phase"] = "code"
        identity = str(ctx.root)
        if state.get("coder_job"):
            future = _JOBS.get(identity)
            if future is None:
                # A process restart cannot silently reuse an unfinished tool session.
                state.pop("coder_job", None)
                key = state.get("repair_key", f"trial-{state['code_queue'][0]}")
                return self._question(
                    ctx, state, key, detail="Coding session interrupted; inspect source before retrying"
                )
            operations = _COMMITS.get(identity)
            if operations is not None:
                while not operations.empty():
                    action, pending = operations.get_nowait()
                    if future.done():
                        pending.cancel()
                        continue
                    if not pending.set_running_or_notify_cancel():
                        continue
                    try:
                        pending.set_result(action())
                    except Exception as exc:
                        pending.set_exception(exc)
            if not future.done():
                return Wait("Coding session / bounded trial in progress", seconds=0.1)
            try:
                result = future.result()
            except CoderRollbackRequired as exc:
                current = snapshot(ctx.root)
                reverted = sorted(
                    name for name in current.keys() | exc.before.keys() if current.get(name) != exc.before.get(name)
                )
                try:
                    ctx.workspace.commit("Failed coder candidate", author="agent:experiment")
                finally:
                    # Already on the engine thread: bypass the expired worker queue.
                    restore(ctx, exc.before, commit=ctx.workspace.commit)
                result = {
                    "ok": False,
                    "problems": [
                        "Unvalidated coder changes have been restored to the session-start snapshot. "
                        f"Session failure: {exc}. Files reverted: {', '.join(reverted) or 'none'}.",
                    ],
                    "report": {"needs_plan_change": False},
                    "rolled_back": True,
                    "reverted_files": reverted,
                }
            except Exception as exc:
                result = {"ok": False, "problems": [str(exc)], "report": {"needs_plan_change": False}}
            # Retain the snapshot-bearing future if rollback itself fails, so resume
            # retries cleanup before a new coding session or training can start.
            _JOBS.pop(identity)
            _COMMITS.pop(identity, None)
            state.pop("coder_job")
            state["workspace_commit"] = ctx.workspace.head()
            state["source_hashes"] = source_hashes(ctx.root)
            eid = state["code_queue"].pop(0)
            ctx.events.append("stage.decision", f"Coder session for {eid}", actor="agent:experiment", data=result)
            key = state.pop("repair_key", f"trial-{eid}")
            if not result["ok"]:
                if result["report"].get("needs_plan_change"):
                    state["plan_change_reason"] = result["report"].get("plan_change_reason", "Semantic change required")
                return self._question(ctx, state, key, detail="; ".join(result.get("problems", [])))
            state["commit"] = state["workspace_commit"]
            state["entries"][key]["state"] = "todo"
            # Any code edit invalidates the same-code trial gate for all experiments.
            for entry in state["entries"].values():
                if entry["trial"] and not entry.get("coder_trial"):
                    entry["state"] = "todo"
            return Continue("Coder changes passed semantic guard; verify current-commit trials before formal runs")
        eid = state["code_queue"][0]
        spec = next(e for e in approved.plan.experiments if e.id == eid)
        schema = _schema(approved.task, approved.task_dir)
        if schema:
            validator = Draft202012Validator(schema)
            invalid = next(
                (
                    e
                    for e in state["entries"].values()
                    if e["experiment_id"] == eid and list(validator.iter_errors(e["config"]))
                ),
                None,
            )
            if invalid:
                return self._question(
                    ctx,
                    state,
                    f"trial-{eid}",
                    detail="Approved configuration is not supported by the protected task schema; "
                    "no code or schema was changed",
                )
        state["coder_job"] = {"experiment": eid}
        operations = _COMMITS[identity] = queue.Queue()
        abandoned = threading.Event()

        def on_engine(action):
            # Keep workspace/state mutations on the engine thread. The worker
            # only calls the model and polls already submitted trials.
            if abandoned.is_set():
                raise RuntimeError("Coding operation abandoned after dispatch timeout")
            pending = Future()

            def guarded():
                if abandoned.is_set():
                    raise RuntimeError("Expired coding operation cannot execute")
                return action()

            operations.put((guarded, pending))
            try:
                return pending.result(timeout=CODER_DISPATCH_TIMEOUT_S)
            except TimeoutError:
                abandoned.set()
                pending.cancel()
                raise

        def commit(message, author="agent:experiment"):
            return on_engine(lambda: ctx.workspace.commit(message, author=author))

        def trial(key):
            # The coding worker may wait; the engine thread continues returning Wait.
            if isinstance(ctx.executor, LocalExecutor) and not getattr(ctx.executor, "simulation", False):
                preflight(ctx)
                ctx.executor._captured_environment = None
            trial_key = f"coder-{eid}-{rand_hex(8)}"
            original = state["entries"][key]
            entry = self._entry(
                eid,
                "trial",
                original["config"],
                original["eval_condition"] == "explore",
                [],
                True,
                original["est_minutes"],
            )
            entry["coder_trial"] = True
            entry["timeout_s"] = min(300, ctx.project.experiment.get("trial_timeout_s", 300))

            def submit_trial():
                if not fits_budget(ctx, approved, [entry]):
                    raise ValueError("Trial would exceed budget")
                self._write_config(ctx, trial_key, entry)
                candidate = ctx.workspace.commit("Coder trial configuration", author="agent:experiment")
                temporary = {**state, "commit": candidate}
                rid = self._prepare(ctx, temporary, approved, trial_key, entry, input_refs(ctx.root))
                state["entries"][trial_key] = entry
                ctx.executor.submit(self._spec(ctx, rid))
                return rid, candidate

            rid, candidate = on_engine(submit_trial)
            deadline = time.monotonic() + entry["timeout_s"] + 15
            while ctx.executor.status(rid).state not in TERMINAL_RUN_STATES:
                if time.monotonic() > deadline:
                    ctx.executor.cancel(rid)
                    raise ValueError("Coder trial did not terminate before its deadline")
                time.sleep(0.1)
            outputs = ctx.executor.collect(rid)
            status = ctx.executor.status(rid)
            on_engine(
                lambda: entry.update(
                    state="done" if status.state == RunState.succeeded else "skipped", success_commit=candidate
                )
            )
            return {
                "run_id": rid,
                "state": status.state.value,
                "stderr": "\n".join(ctx.executor.logs(rid, "stderr", 0).text.splitlines()[-60:]),
                "metrics": [m.model_dump(mode="json") for m in outputs.metrics],
            }

        error = state["entries"].get(state.get("repair_key"), {}).get("failure_detail", "")
        _JOBS[identity] = _POOL.submit(
            run_coder, ctx, approved, spec, state["entries"], trial, error, commit, on_engine
        )
        return Wait("Restricted coding session started", seconds=0.1)

    def _question(self, ctx, s, key, install=False, detail=""):
        entry = s["entries"][key]
        s["failed_key"] = key
        text = detail or entry.get("failure_detail", "Experiment requires a decision")
        if install:
            text += (
                f". Install the missing dependency manually using {ctx.executor.python}; "
                "packages are never auto-installed"
            )
        return Stop(
            text,
            question=Question(
                text=text,
                options=[
                    QuestionOption(id="skip", label="Skip this combination"),
                    QuestionOption(
                        id="retry", label="I installed dependencies or edited the source; validate and retry"
                    ),
                    QuestionOption(id="revise_plan", label="Propose a plan revision"),
                ],
                refs=[r for r in [ctx.archive.latest(f"runs/{entry['runs'][-1]}") if entry["runs"] else None] if r],
            ),
        )

    def _answer(self, ctx, s):
        key = s.pop("failed_key")
        s.pop("asked", None)
        entry = s["entries"][key]
        choice = ctx.answer.choice
        ctx.events.append("stage.decision", f"{key}: {choice}", actor="user")
        if choice == "skip":
            entry["state"] = "skipped"
            s["code_queue"] = []
        elif choice == "retry":
            # Protect evaluation/semantics even for human-edited retries.
            for name, checksum in s["protected"].items():
                if sha256_file(ctx.root / "src" / name) != checksum:
                    raise ValueError("Human edit changed a protected file")
            for task_key, other in s["entries"].items():
                path = ctx.root / "configs" / other["experiment_id"] / (task_key + ".yaml")
                if json.loads(path.read_text(encoding="utf-8")) != other["config"]:
                    raise ValueError("Human edit changed a generated configuration")
            if isinstance(ctx.executor, LocalExecutor) and not getattr(ctx.executor, "simulation", False):
                preflight(ctx)
                ctx.executor._captured_environment = None
            s["commit"] = s["workspace_commit"] = ctx.workspace.commit("human-edited", author="user")
            s["source_hashes"] = source_hashes(ctx.root)
            entry.update(state="todo", repair_counts={}, retry_count=0)
            s["code_queue"] = []
            for other in s["entries"].values():
                if other["trial"] and not other.get("coder_trial"):
                    other["state"] = "todo"
        elif choice == "revise_plan":
            # The important decision is reviewed through the ordinary round log.
            s["requested_revision"] = True
            return Advance(S.Analyzing, "User requested a plan revision; prepare the important review log")
        return None


def create_stage():
    return ExperimentStage()


PLAN = "plan/experiment_plan.md"
TASKS = "plan/tasks.json"
PRECHECK = "plan/precheck.json"
MAX_RUNS = 256


@dataclass
class ApprovedInputs:
    plan: ExperimentPlan
    plan_ref: VersionRef
    task: TaskConfig
    task_dir: Path
    tasks: list[PlanTask]
    tasks_ref: VersionRef
    precheck_ref: VersionRef


def safe_key(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.=%-]{1,180}", value)) and value not in (".", "..")


def _schema(task: TaskConfig, directory: Path) -> dict | None:
    if not task.config_schema:
        return None
    path = Path(task.config_schema)
    candidates = [path] if path.is_absolute() else [repo_root() / path, directory / path]
    target = next((p for p in candidates if p.is_file()), None)
    if target is None:
        raise ValueError("找不到任务的 config_schema")
    return json.loads(target.read_text(encoding="utf-8"))


def load_approved(ctx) -> ApprovedInputs:
    if "plan" not in ctx.approved or "idea" not in ctx.approved:
        raise ValueError("执行前必须先批准选题与实验计划")
    ref = ctx.approved["plan"]
    if any(x in ctx.stale for x in (PLAN, "idea/selected.md")):
        raise ValueError("上游版本已变化，计划过期；请先重新审批")
    if ctx.archive.current_sha(PLAN) != ref.sha256:
        raise ValueError("计划工作文件有未批准修改，停止执行")
    plan = parse_plan_md(ctx.archive.read_text(ref))
    if plan.idea_ref != ctx.approved["idea"] or plan.task_config != ctx.project.task:
        raise ValueError("批准计划的选题版本或任务与当前项目不一致")
    parents = ctx.archive.version_info(ref).parents

    def attachment(name):
        matches = [r for r in parents if r.artifact_id == name]
        if len(matches) != 1:
            raise ValueError(f"批准计划没有唯一绑定附件：{name}")
        bound = matches[0]
        if ctx.archive.current_sha(name) != bound.sha256:
            raise ValueError(f"附件有未批准修改：{name}")
        return bound

    tasks_ref, precheck_ref = attachment(TASKS), attachment(PRECHECK)
    checks = [PrecheckItem.model_validate(c) for c in json.loads(ctx.archive.read_text(precheck_ref))]
    if not checks:
        raise ValueError("Missing precheck evidence")
    if any(c.status == "fail" for c in checks):
        ctx.events.append(
            "stage.decision",
            "Execute explicitly approved plan with disclosed precheck failures",
            actor="agent:experiment",
            refs=[ref, precheck_ref],
        )
    tasks = [PlanTask.model_validate(t) for t in json.loads(ctx.archive.read_text(tasks_ref))]
    directory = resolve_task_dir(plan.task_config)
    task = TaskConfig.model_validate(yaml.safe_load((directory / "task.yaml").read_text(encoding="utf-8")))
    _validate_matrix(plan, tasks, task, _schema(task, directory))
    return ApprovedInputs(plan, ref, task, directory, tasks, tasks_ref, precheck_ref)


def _validate_matrix(plan, tasks, task, schema):
    if not tasks or len(tasks) > MAX_RUNS or len({t.task_key for t in tasks}) != len(tasks):
        raise ValueError("任务清单为空、重复或超过本步上限")
    specs = {e.id: e for e in plan.experiments}
    if len(specs) != len(plan.experiments):
        raise ValueError("实验 ID 重复")
    graph = {e.id: set(e.depends_on) for e in plan.experiments}
    done = set()
    while len(done) < len(graph):
        ready = {k for k, deps in graph.items() if k not in done and deps <= done}
        if not ready:
            raise ValueError("实验依赖循环或引用不存在的实验")
        done.update(ready)
    defaults = {k: v["default"] for k, v in (schema or {}).get("properties", {}).items() if "default" in v}
    expected = set()
    for e in plan.experiments:
        if (
            not safe_key(e.id)
            or e.target_env != "local"
            or (not e.needs_code and e.method not in task.methods_available)
        ):
            raise ValueError(f"{e.id} 不能由本步已有模板执行")
        if not e.seeds or len(set(e.seeds)) != len(e.seeds):
            raise ValueError(f"{e.id} 种子为空或重复")
        if not math.isfinite(e.est_minutes_per_run) or e.est_minutes_per_run <= 0:
            raise ValueError(f"{e.id} 的成本估计非法")
        keys = sorted(e.variables)
        count = len(e.seeds)
        for key in keys:
            count *= len(e.variables[key])
        if not count or len(expected) + count > MAX_RUNS:
            raise ValueError("计划矩阵为空或过大")
        for combo in itertools.product(*(e.variables[k] for k in keys)):
            for seed in e.seeds:
                config = {
                    **defaults,
                    **e.fixed,
                    **dict(zip(keys, combo, strict=True)),
                    "method": e.method,
                    "seed": seed,
                }
                expected.add((e.id, json.dumps(config, sort_keys=True, allow_nan=False)))
    actual = set()
    validator = Draft202012Validator(schema) if schema else None
    required = {m.name for m in task.metrics if m.required}
    for t in tasks:
        if not safe_key(t.task_key) or t.experiment_id not in specs:
            raise ValueError("任务键非法或引用未知实验")
        e = specs[t.experiment_id]
        if (t.kind, t.eval_condition, t.depends_on) != (e.kind, e.eval_condition, e.depends_on):
            raise ValueError(f"{t.task_key} 的类型、评价条件或依赖不匹配计划")
        if set(t.expected_metrics) != required:
            raise ValueError(f"{t.task_key} 的必需指标与任务不一致")
        role = task.splits.get(t.config.get("eval_split"), {}).get("role")
        if role != t.eval_condition or (t.kind == "exploration" and role != "explore"):
            raise ValueError(f"{t.task_key} 的评价划分不合法")
        if validator and not e.needs_code:
            validator.validate(t.config)
        actual.add((e.id, json.dumps(t.config, sort_keys=True, allow_nan=False)))
    if expected != actual or len(actual) != len(tasks):
        raise ValueError("tasks.json 与批准计划的矩阵不一致，不能执行")
    if not 0 < plan.stopping_conditions.budget_fraction <= 1:
        raise ValueError("预算停止比例非法")
