"""Refine a task-constrained idea, score it, and render an evidence-linked comparison."""

from __future__ import annotations

import json
import re

import yaml

from airesearcher.core.fsutil import sha256_bytes
from airesearcher.core.models.idea import DataSpec, IdeaScores, SelectedIdea
from airesearcher.core.models.plan import TaskConfig
from airesearcher.core.workspace import resolve_task_dir
from airesearcher.engine.stage import Continue, Error, Stop

from .attempts import ATTEMPTS_PER_WINDOW, SelectionNeedsDecision, prepare_window, request_retry
from .evidence import check_evidence, compact_material, load_json, material
from .goal_scope import goal_contract, validate_goal_scope
from .models import GapTableOutput, RefinementOutput, ScoreOutput
from .render import render_gap_table
from .scoring import decode_score, restore_score_attempt, score_variables
from .storage import save_json

DIMENSIONS = ("novelty", "feasibility", "impact", "testability", "risk")


def task_config(ctx):
    path = resolve_task_dir(ctx.project.task) / "task.yaml"
    task = TaskConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    if not task.metrics or len({m.name for m in task.metrics}) != len(task.metrics):
        raise ValueError("The selected task must define distinct evaluation metrics")
    return task


def round_path(state, name):
    return f"idea/selection_rounds/{state.get('selection_round', 'initial')}/{name}.json"


def trace(out):
    return {"call_id": out.call_id, "model": out.model, "prompt_id": out.prompt_id,
            "prompt_version": out.prompt_version, "usage": out.usage}


def normalize_idea(ctx, idea, task, fit="unknown"):
    idea = idea.model_copy(deep=True)
    validate_goal_scope(ctx.project.goal, idea)
    summary, papers, _, _ = material(ctx)
    for field in ("idea_id", "title", "research_question", "motivation"):
        if not getattr(idea, field).strip():
            raise ValueError(f"{field} must not be blank")
    for collection in (idea.hypotheses, idea.baselines, idea.methods):
        ids = [item.id for item in collection]
        if len(ids) != len(set(ids)) or any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", i) for i in ids):
            raise ValueError("Hypothesis, baseline, and method IDs must be safe and unique within their groups")
    if any(not h.statement.strip() or not h.falsified_if.strip() for h in idea.hypotheses):
        raise ValueError("Each hypothesis requires a statement and a falsification condition")
    if not idea.contributions or any(not c.strip() for c in idea.contributions):
        raise ValueError("At least one explicit contribution is required")
    if not idea.stopping_conditions or any(not c.strip() for c in idea.stopping_conditions):
        raise ValueError("Explicit stopping conditions are required")
    metrics = {m.name: m.direction for m in task.metrics}
    names = [m.name for m in idea.metrics]
    if len(names) != len(set(names)) or any(metrics.get(m.name) != m.direction for m in idea.metrics):
        raise ValueError("Metrics must be distinct and match the selected task's names and directions")
    if not {m.name for m in task.metrics if m.required} <= set(names):
        raise ValueError("All required task metrics must be included")
    questions = list(idea.open_questions)
    cited = list(idea.citations)
    for item in [*idea.baselines, *idea.methods]:
        cited.extend(item.citations)
        invalid = [pid for pid in item.citations if pid not in papers]
        item.citations = list(dict.fromkeys(pid for pid in item.citations if pid in papers))
        questions.extend(f"Unverified reference removed: {pid}" for pid in invalid)
    questions.extend(f"Unverified reference removed: {pid}" for pid in cited if pid not in papers)
    idea.citations = list(dict.fromkeys(pid for pid in cited if pid in papers))

    def clean_text(value):
        if isinstance(value, str):
            def replace(match):
                pid = match.group(1)
                if pid not in papers:
                    questions.append(f"Unverified inline reference removed: {pid}")
                    return "[unverified reference]"
                if pid not in idea.citations:
                    idea.citations.append(pid)
                return match.group(0)
            return re.sub(r"\[((?:arxiv|doi|s2):[^\]\n]+)\]", replace, value)
        if isinstance(value, list):
            return [clean_text(v) for v in value]
        if isinstance(value, dict):
            return {k: clean_text(v) for k, v in value.items()}
        return value

    idea = SelectedIdea.model_validate(clean_text(idea.model_dump(mode="json")))
    idea.task = {"domain": task.domain, "task_config": ctx.project.task, "description": task.description}
    idea.budget = {k: limit.hard for k, limit in ctx.project.budget.items() if limit.hard is not None}
    idea.scores_ref, idea.gap_table_ref = "idea/scores.json", "idea/gap_table.md"
    proposed = {d.name: d for d in idea.data}
    idea.data = [DataSpec(name=d.name, source=d.source, license=d.license or "To verify",
                          usage=proposed[d.name].usage if d.name in proposed else "As specified by the task")
                 for d in task.data]
    if not task.data:
        questions.append("No dataset is registered in the selected task; execution readiness is unverified.")
    if fit != "supported" or task.name == "smoke" or task.domain == "generic":
        questions.append("TASK FIT TO VERIFY: the selected task may be a workflow fixture; "
                         "this idea does not establish an executable research setup.")
    for item in [*idea.baselines, *idea.methods]:
        if item.name not in task.methods_available:
            questions.append(f"Task extension required before execution: method '{item.name}' is not registered.")
    questions.extend(f"Unresolved literature conflict: {cid}" for cid in summary.unresolved_conflicts)
    questions.extend(f"Abstract-only evidence requires verification: {pid}" for pid in summary.abstract_only_papers)
    questions.extend(f"Incomplete reading task: {cid}" for cid in summary.incomplete_reading)
    questions.extend(load_json(ctx, "idea/brief.json").get("missing_info", []))
    if not papers:
        questions.append("No literature was retrieved; novelty and evidence claims are unverified.")
    idea.open_questions = list(dict.fromkeys(q.strip() for q in questions if q.strip()))
    return idea


def request_output(ctx, state, name, schema, validate, context):
    """Persist calls; allow one repair per window and ask before retrying blocked calls."""
    path = round_path(state, name)
    digest = sha256_bytes(json.dumps(context, ensure_ascii=False, sort_keys=True).encode())
    if ctx.archive.latest(path):
        receipt = load_json(ctx, path)
        if receipt["input_sha256"] != digest:
            raise ValueError("Saved selection output belongs to different input; start a new selection round.")
        validate(schema.model_validate(receipt["output"]))
        return receipt
    attempts_path = round_path(state, name + "_attempts")
    attempts = (load_json(ctx, attempts_path) if ctx.archive.latest(attempts_path)
                else {"attempts": [], "input_sha256": digest})
    if attempts["input_sha256"] != digest:
        raise ValueError("Selection attempt inputs changed; start a new selection round.")
    records = attempts["attempts"]
    if name == "score" and records:
        restore_score_attempt(ctx, attempts_path, attempts, schema, validate)
    if records and records[-1]["status"] == "done":
        output = schema.model_validate(records[-1]["raw_output"])
        validate(output)
        receipt = {"output": output.model_dump(mode="json"), "trace": records[-1]["trace"],
                   "input_sha256": digest}
        save_json(ctx, path, receipt)
        return receipt
    start = prepare_window(ctx, state, name, attempts_path, attempts)
    while len(attempts["attempts"]) - start < ATTEMPTS_PER_WINDOW:
        previous = records[-1] if records else {}
        attempt = {"status": "started"}
        attempts["attempts"].append(attempt)
        if name == "score":
            attempt["response_path"] = round_path(state, f"score_responses/attempt-{len(records):04d}")
            if ctx.archive.latest(attempt["response_path"]):
                raise ValueError("An archived score response must never be overwritten")
        save_json(ctx, attempts_path, attempts)
        variables = {"schema_json": schema.model_json_schema(), "goal": ctx.project.goal,
                     "context_block": ctx.llm.wrap_untrusted(json.dumps(context, ensure_ascii=False),
                                                             source="selection_context"),
                     "validation_errors": [a.get("error", "") for a in records[:-1]]}
        raw_score = (name == "score" and ctx.llm.prompts.load(
            "idea/score", ctx.project.prompt_pins.get("idea/score")).version >= 2)
        if raw_score:
            prior = json.dumps(previous.get("raw_output", {}), ensure_ascii=False)
            if previous.get("response_path") and ctx.archive.latest(previous["response_path"]):
                prior = load_json(ctx, previous["response_path"]).get("text", "")
            variables.update(score_variables(schema, context, ctx.llm.wrap_untrusted(
                prior[:16000], source="previous_invalid_score")))
            variables["validation_errors"] = [a.get("error", "")[:6000] for a in records[:-1][-2:]]
        try:
            out = ctx.llm.complete(
                role=f"idea.{name}", prompt_id=f"idea/{name}", tier="strong", max_retries=0,
                response_schema=None if raw_score else schema, variables=variables,
            )
            attempt["trace"] = trace(out)
            output = out.parsed
            if raw_score:
                response = {"text": out.text, "finish_reason": out.finish_reason, "trace": trace(out),
                            "input_sha256": digest}
                save_json(ctx, attempt["response_path"], response)
                output = decode_score(response, schema)
            attempt["raw_output"] = output.model_dump(mode="json")
            validate(output)
        except Exception as exc:
            attempt.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            save_json(ctx, attempts_path, attempts)
            if name == "score":
                if raw_score and (not attempt.get("trace")
                                  or not ctx.archive.latest(attempt["response_path"])):
                    attempt["status"] = "interrupted"
                    save_json(ctx, attempts_path, attempts)
                    request_retry(ctx, state, name, attempts_path, attempts,
                                  "failed without an archived response; prior cost may be uncertain")
                ctx.events.append("literature.score_failed", "Score validation failed",
                                  actor="agent:idea", data={"attempt": len(records), "error": attempt["error"][:6000],
                                                           "call_id": attempt.get("trace", {}).get("call_id")})
                if len(records) - start >= ATTEMPTS_PER_WINDOW:
                    request_retry(ctx, state, name, attempts_path, attempts, "exhausted its bounded attempts")
                return None
            continue
        attempt["status"] = "done"
        attempt["raw_output"] = output.model_dump(mode="json")
        save_json(ctx, attempts_path, attempts)
        receipt = {"output": output.model_dump(mode="json"), "trace": trace(out), "input_sha256": digest}
        save_json(ctx, path, receipt)
        return receipt
    request_retry(ctx, state, name, attempts_path, attempts, "exhausted its bounded attempts")


def selection_context(ctx):
    return {"goal_scope": goal_contract(ctx.project.goal),
            "brief": load_json(ctx, "idea/brief.json"), "literature": compact_material(ctx),
            "task": task_config(ctx).model_dump(mode="json"),
            "project_budget": {k: v.model_dump() for k, v in ctx.project.budget.items()},
            "constraints": ctx.project.constraints}


def refine_step(ctx, state):
    try:
        task = task_config(ctx)
        context = selection_context(ctx)
        if state.get("revision_input"):
            context["revision_request"] = load_json(ctx, state["revision_input"])
        receipt = request_output(ctx, state, "refine", RefinementOutput,
                                 lambda out: normalize_idea(ctx, out.idea, task, out.task_fit),
                                 context)
        output = RefinementOutput.model_validate(receipt["output"])
        idea = normalize_idea(ctx, output.idea, task, output.task_fit)
        save_json(ctx, "idea/goal_scope.json", validate_goal_scope(ctx.project.goal, idea))
        save_json(ctx, "idea/selected.json", idea.model_dump(mode="json"))
        save_json(ctx, "idea/refinement_status.json", {"task_fit": output.task_fit,
                                                      "task_fit_reason": output.task_fit_reason,
                                                      "trace": receipt["trace"]})
    except SelectionNeedsDecision as exc:
        return exc.stop
    except RuntimeError as exc:
        return Stop(str(exc))
    except (ValueError, KeyError, OSError) as exc:
        return Error(f"Idea refinement failed: {exc}", retryable=False)
    state["phase"] = "score"
    return Continue("Task-constrained idea saved; scoring comes next")




def normalize_scores(ctx, output, papers, refs, weak):
    """Keep task-context and circular score references out of the literature evidence list."""
    idea = load_json(ctx, "idea/selected.json")
    errors = []
    for name in DIMENSIONS:
        dimension = getattr(output, name)
        retained, removed = [], []
        for ref in dimension.evidence:
            if ref.startswith("idea#") and ref.split("#", 1)[1] in idea:
                removed.append(ref)
            elif re.fullmatch(r"(novelty|feasibility|impact|testability|risk)#rationale", ref):
                removed.append(ref)
            else:
                retained.append(ref)
        dimension.evidence = retained
        try:
            dimension.to_verify |= check_evidence(retained, papers, refs, weak)
        except ValueError as exc:
            unknown = sorted({ref for ref in retained if ref not in papers and ref not in refs})
            errors.append(f"{name}: {exc}; all unknown references={unknown}")
        if removed:
            dimension.to_verify = True
            dimension.rationale += (" Context or circular references excluded from literature evidence: "
                                    + ", ".join(removed))
    if errors:
        raise ValueError("; ".join(errors) + ". Copy only allowlisted paper/card references. "
                         "Task, idea, and summary paths are context; use evidence=[] and to_verify=true "
                         "when the rationale has no supporting literature.")


def score_step(ctx, state):
    try:
        summary, papers, refs, weak = material(ctx)

        def validate(output):
            normalize_scores(ctx, output, papers, refs, weak)

        receipt = request_output(ctx, state, "score", ScoreOutput, validate,
                                 {**selection_context(ctx), "idea": load_json(ctx, "idea/selected.json")})
        if receipt is None:
            return Continue("Score response and validation errors saved; one targeted repair remains")
        output = ScoreOutput.model_validate(receipt["output"])
        for name in DIMENSIONS:
            dim = getattr(output, name)
            dim.to_verify |= check_evidence(dim.evidence, papers, refs, weak)
            if name in {"novelty", "feasibility", "risk"}:
                dim.to_verify |= bool(summary.unresolved_conflicts or summary.incomplete_reading)
        status = load_json(ctx, "idea/refinement_status.json")
        if status["task_fit"] != "supported" or task_config(ctx).name == "smoke" or not task_config(ctx).data:
            output.feasibility.to_verify = output.risk.to_verify = True
        scores = IdeaScores(**output.model_dump(), note="Advisory scores; a higher risk score means greater risk.",
                            model=receipt["trace"]["model"], prompt_version=receipt["trace"]["prompt_version"])
        save_json(ctx, "idea/scores.json", scores.model_dump(mode="json"), "scores")
        save_json(ctx, "idea/score_status.json", {"trace": receipt["trace"]})
    except SelectionNeedsDecision as exc:
        return exc.stop
    except RuntimeError as exc:
        return Stop(str(exc))
    except (ValueError, KeyError, OSError) as exc:
        return Error(f"Idea scoring failed: {exc}", retryable=False)
    state["phase"] = "gap_table"
    return Continue("Five evidence-linked scores saved; a higher risk score means greater risk")


def gap_table_step(ctx, state):
    try:
        _, papers, refs, weak = material(ctx)
        columns = {"task_data", "method", "setting", "compute", "evaluation", "difference"}

        def validate(output):
            ids = [r.paper_id for r in output.rows]
            if len(ids) != len(set(ids)):
                raise ValueError("Gap-table rows must have unique paper IDs")
            if set(ids) != set(load_json(ctx, "idea/selected.json")["citations"]):
                raise ValueError("The comparison must cover every selected citation exactly once")
            for row in output.rows:
                if row.paper_id not in papers or set(row.cells) != columns:
                    raise ValueError("Each comparison row needs a registered paper and all six columns")
                for cell in row.cells.values():
                    check_evidence(cell.evidence, papers, refs, weak, paper_id=row.paper_id)

        receipt = request_output(ctx, state, "gap_table", GapTableOutput, validate,
                                 {**selection_context(ctx), "idea": load_json(ctx, "idea/selected.json")})
        output = GapTableOutput.model_validate(receipt["output"])
        for row in output.rows:
            for cell in row.cells.values():
                cell.to_verify |= check_evidence(cell.evidence, papers, refs, weak, paper_id=row.paper_id)
                if not cell.evidence:
                    cell.text = "To verify"
        save_json(ctx, "idea/gap_table.json",
                  {**output.model_dump(mode="json"), "trace": receipt["trace"]}, "gap_table")
        ctx.archive.put("idea/gap_table.md", "gap_table", render_gap_table(output, papers),
                        parents=[ctx.archive.latest("idea/gap_table.json")], producer="agent:idea/gap_table")
    except SelectionNeedsDecision as exc:
        return exc.stop
    except RuntimeError as exc:
        return Stop(str(exc))
    except (ValueError, KeyError, OSError) as exc:
        return Error(f"Gap-table validation failed: {exc}", retryable=False)
    state["phase"] = "write"
    return Continue("Evidence-linked comparison saved; final idea document comes next")
