"""PlanDrafting：默认离线，显式选择 api 后复用主干 LLMGateway。"""

from __future__ import annotations

import hashlib

from pydantic import ValidationError

from airesearcher.core.fsutil import dumps
from airesearcher.core.models.common import VersionRef
from airesearcher.core.models.idea import parse_selected_md
from airesearcher.core.models.plan import ExperimentPlan, PrecheckItem, parse_plan_md
from airesearcher.core.models.question import Question, QuestionOption
from airesearcher.engine.stage import Continue, Error, NeedsApproval, StageContext, StepResult, Stop, Wait
from airesearcher.llm.gateway import LLMOutputError, extract_json
from airesearcher.llm.models import MissingAPIKey, UnknownModel
from airesearcher.runtime.data import ensure_data

from .expand import PlanDraft, expand_tasks, load_task, make_plan
from .precheck import inspect_inputs, item, planning_budget_context, run_prechecks, structural_problems
from .render import render_plan

PLAN = "plan/experiment_plan.md"
TASKS = "plan/tasks.json"
PRECHECK = "plan/precheck.json"
PROMPT = "plan/draft"
MAX_REVISIONS = 2


class PlanStage:
    name = "plan"

    def step(self, ctx: StageContext) -> StepResult:
        mode = ctx.project.experiment.get("llm_backend", "fake")
        backend = getattr(ctx.llm, "backend", None)
        if mode not in ("fake", "api") or not callable(getattr(backend, "call", None)) or (
            getattr(backend, "simulation", False) is True
        ) != (mode == "fake"):
            return Error(
                "计划后端与 experiment.llm_backend 不匹配：默认 fake 只接受 FakeLLM；"
                "真实阶段请设置 experiment.llm_backend: api；离线演示请使用 scripts.plan.demo。",
                retryable=False,
            )
        if "idea" not in ctx.approved:
            return Stop("缺少已批准的 idea；请先完成选题审批，不能直接读取未批准草稿。")
        try:
            idea_ref = ctx.approved["idea"]
            idea = parse_selected_md(ctx.archive.read_text(idea_ref))
            _, task, schema = load_task(ctx.project.task)
            readiness = ensure_data(ctx, task)
            if not readiness.ready:
                if readiness.waiting:
                    return Wait(readiness.detail, seconds=1)
                return Stop(readiness.detail, question=readiness.question)
            ctx.permissions.guard("exec", "local", actor="agent:plan/precheck")
            input_checks, evidence = inspect_inputs(task, ctx.project.experiment.get("python"))
        except (OSError, ValueError, KeyError) as exc:
            return Stop(f"读取计划输入失败：{exc}")
        if any(c.status == "fail" for c in input_checks):
            return Stop("；".join(c.detail for c in input_checks if c.status == "fail"))

        feedback = next((f for f in reversed(ctx.feedback) if f.kind == "plan"), None)
        material = {
            "idea_ref": idea_ref.model_dump(),
            "task_path": ctx.project.task,
            "task": task.model_dump(),
            "schema": schema,
            "budget": {k: v.model_dump() for k, v in ctx.project.budget.items()},
            "constraints": ctx.project.constraints,
            "limits": ctx.project.limits.model_dump(),
            "evidence": evidence,
            "feedback": feedback.model_dump(mode="json") if feedback else None,
            "entry": ctx.entry.to_json() if ctx.entry else None,
            "stale": ctx.stale,
            "llm_backend": mode,
        }
        signature = hashlib.sha256(dumps(material).encode()).hexdigest()
        s = ctx.scratch.setdefault("plan", {})
        if s.get("signature") != signature:
            s.clear()
            s.update(
                signature=signature,
                phase="draft",
                revisions=0,
                note=feedback.decision_comment
                if feedback
                else (
                    "Idea, task, budget or upstream inputs changed; draft a new plan."
                    if ctx.archive.latest(PLAN)
                    else "Initial version."
                ),
            )

        if ctx.answer and ctx.answer.question_id == s.get("asked"):
            s.pop("asked", None)
            if ctx.answer.choice == "submit_anyway" and s.get("plan_ref"):
                s.update(phase="submit", accept_failed_checks=True)
                ctx.events.append(
                    "stage.decision",
                    "用户要求将仍有 fail 项的计划提交审批；未批准或执行实验",
                    actor="user",
                    refs=[VersionRef.model_validate(s["plan_ref"])],
                )
            elif ctx.answer.choice == "retry":
                s.update(phase="draft", revisions=0, note=ctx.answer.text or "User requested another attempt.")

        phase = s["phase"]
        if phase == "draft":
            ctx.progress(f"计划：{'FakeLLM 离线' if mode == 'fake' else 'API'}起草", 0, 3, "steps")
            # 新一轮若连结构都不合法，不能把上一轮草稿冒充为本轮可提交的计划。
            for key in ("plan_ref", "tasks_ref", "precheck_ref", "accept_failed_checks"):
                s.pop(key, None)
            prompt_input = {
                "approved_idea": idea.model_dump(mode="json"),
                "task": task.model_dump(),
                "config_schema": schema,
                "budget": material["budget"],
                "constraints": material["constraints"],
                "limits": material["limits"],
                "feedback": s["note"],
                "entry": material["entry"],
                "stale": ctx.stale,
                "previous_draft": s.get("candidate"),
                "precheck": s.get("checks", []),
            }
            try:
                fraction = s.get("candidate", {}).get("stopping_conditions", {}).get("budget_fraction", 0.9)
                if not isinstance(fraction, (int, float)) or not 0 < fraction <= 1:
                    fraction = 0.9
                prompt_input["budget_context"] = planning_budget_context(
                    ctx.project, idea, ctx.budget.used(), fraction
                )
                skill = ctx.skills.load("experiment-planning")
                prompt_input["planning_rules"] = skill.instructions
                response = ctx.llm.complete(
                    role="exp.planner",
                    prompt_id=PROMPT,
                    tier="strong",
                    max_retries=1,
                    variables={
                        "schema_json": dumps(PlanDraft.model_json_schema()),
                        "context_document": ctx.llm.wrap_untrusted(dumps(prompt_input), "plan_inputs"),
                    },
                    response_schema=PlanDraft,
                )
                latest = ctx.archive.latest(PLAN)
                plan = make_plan(response.parsed, idea_ref, ctx.project.task, task, latest.version + 1 if latest else 1)
                s["candidate"] = plan.model_dump(mode="json")
                s["phase"] = "check"
                return Continue("模型回复已解析；下一步由代码展开与检查")
            except (MissingAPIKey, UnknownModel) as exc:
                return Error(f"LLM configuration requires correction: {exc}", retryable=False)
            except LLMOutputError as exc:
                # The gateway's human-readable exception includes Chinese prose.
                # Preserve validation locations/reasons instead of passing it to
                # item()'s generic English fallback and losing repair guidance.
                detail = "The model did not return a usable PlanDraft JSON reply."
                try:
                    PlanDraft.model_validate_json(extract_json(getattr(exc, "response_text", "")))
                except ValidationError as validation:
                    errors = validation.errors(include_input=False, include_context=False, include_url=False)
                    detail = "; ".join(
                        f"{'.'.join(str(part) for part in error['loc']) or 'reply'}: {error['msg']}"
                        for error in errors
                    )
                return self._failed(s, [item("draft", [detail], "")])
            except ValueError as exc:
                return self._failed(s, [item("draft", [str(exc)], "")])

        if phase == "check":
            ctx.progress("计划：展开运行清单与预检查", 1, 3, "steps")
            plan = ExperimentPlan.model_validate(s["candidate"])
            structure = structural_problems(plan)
            try:
                tasks = expand_tasks(plan, task, schema) if not structure else []
            except (ValueError, TypeError) as exc:
                structure.append(str(exc))
                tasks = []
            if structure:
                return self._failed(s, [item("structure", structure, "")])
            checks = run_prechecks(
                plan, tasks, idea, task, schema, ctx.project, ctx.budget.used(), input_checks, evidence
            )
            self._save(ctx, s, plan, tasks, checks)
            if any(c.status == "fail" for c in checks):
                return self._failed(s, checks)
            s["phase"] = "submit"
            return Continue("计划、运行清单与预检查已保存；尚未批准或执行")

        if phase == "submit":
            # 本阶段拥有的产物由自己同步；人工编辑也必须重新展开和校验。
            ctx.archive.sync([PLAN])
            latest = ctx.archive.latest(PLAN)
            if latest is None:
                return Error("计划产物丢失，请重新创建演示工作区。", retryable=False)
            plan = parse_plan_md(ctx.archive.read_text(latest))
            if plan.idea_ref != idea_ref or plan.task_config != ctx.project.task:
                return Stop("当前计划引用的选题版本或任务不一致，请重新起草，不能批准过期计划。")
            if latest.model_dump(mode="json") != s["plan_ref"]:
                s.update(
                    candidate=plan.model_dump(mode="json"),
                    phase="check",
                    accept_failed_checks=False,
                    note="Re-expand and validate the edited plan; retain the previous archived version.",
                )
                return Continue("检测到人工修改，重新预检查")
            # 附件被改写时恢复为本候选的确定性产物，绝不信任手写的 pass。
            tasks = expand_tasks(plan, task, schema)
            checks = run_prechecks(
                plan, tasks, idea, task, schema, ctx.project, ctx.budget.used(), input_checks, evidence
            )
            if any(c.status == "fail" for c in checks) and not s.get("accept_failed_checks"):
                s.update(candidate=plan.model_dump(mode="json"), phase="check")
                return Continue("提交前检查发现 fail，返回检查阶段")
            self._save(ctx, s, plan, tasks, checks)
            counts = {status: sum(c.status == status for c in checks) for status in ("warn", "fail")}
            ctx.progress("计划：等待人工审批", 3, 3, "steps")
            return NeedsApproval(
                target=VersionRef.model_validate(s["plan_ref"]),
                kind="plan",
                summary=f"{'FakeLLM development validation' if mode == 'fake' else 'Experiment plan'}: "
                f"{len(plan.experiments)} experiments, {len(tasks)} runs; "
                f"{counts['warn']} warnings, {counts['fail']} failures; no training performed",
                impact=(
                    ("Workflow validation only. " if mode == "fake" else "")
                    + "Review the plan and warnings before approval. "
                    "Old runs are not silently reused."
                ),
                extra_refs=[VersionRef.model_validate(s[k]) for k in ("tasks_ref", "precheck_ref")],
            )
        return Stop("预检查未通过且自动修订已用尽，请回答当前问题。")

    @staticmethod
    def _failed(s: dict, checks: list[PrecheckItem]) -> StepResult:
        s["checks"] = [c.model_dump() for c in checks]
        if s["revisions"] < MAX_REVISIONS:
            s["revisions"] += 1
            s["phase"] = "draft"
            return Continue(f"预检查失败，进行第 {s['revisions']}/{MAX_REVISIONS} 次模型修订")
        s["phase"] = "blocked"
        options = [QuestionOption(id="submit_anyway", label="将 fail 项连同计划提交人工审批，仍不执行")]
        if not s.get("plan_ref"):
            options = [QuestionOption(id="retry", label="修正测试输入后，再尝试一轮")]
        options.append(QuestionOption(id="end", label="结束这个开发项目"))
        refs = [VersionRef.model_validate(s["plan_ref"])] if s.get("plan_ref") else []
        return Stop(
            "预检查失败，最多 2 次自动修订已用尽；没有默认选择。",
            question=Question(
                text="计划仍有问题：" + "；".join(c.detail for c in checks if c.status == "fail"),
                options=options,
                refs=refs,
            ),
        )

    @staticmethod
    def _save(ctx, s, plan, tasks, checks):
        previous = ctx.archive.latest(PLAN)
        parents = [plan.idea_ref]
        tasks_ref = ctx.archive.put(
            TASKS,
            "plan",
            dumps([t.model_dump(mode="json") for t in tasks]),
            parents=parents,
            producer="agent:plan/expand",
        )
        precheck_ref = ctx.archive.put(
            PRECHECK,
            "precheck",
            dumps([c.model_dump() for c in checks]),
            parents=parents,
            producer="agent:plan/precheck",
        )
        md = render_plan(plan, tasks, checks, s["note"], llm_backend=ctx.project.experiment.get("llm_backend", "fake"))
        parse_plan_md(md)
        ref = ctx.archive.put(
            PLAN,
            "plan",
            md,
            parents=[*([previous] if previous else []), plan.idea_ref, tasks_ref, precheck_ref],
            producer="agent:plan/writer",
            note=s["note"],
        )
        s.update(
            plan_ref=ref.model_dump(mode="json"),
            tasks_ref=tasks_ref.model_dump(mode="json"),
            precheck_ref=precheck_ref.model_dump(mode="json"),
            checks=[c.model_dump() for c in checks],
        )


def create_stage() -> PlanStage:
    return PlanStage()
