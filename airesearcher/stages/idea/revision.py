"""Versioned feedback handling and incremental literature rounds without rereading old papers."""

from __future__ import annotations

import json
from dataclasses import replace

from airesearcher.core.fsutil import sha256_bytes
from airesearcher.core.models.idea import parse_selected_md
from airesearcher.engine.stage import Continue, Error, Stop
from airesearcher.services.literature import load_papers

from .attempts import SelectionNeedsDecision
from .dedup import PaperIdentity
from .evidence import load_json
from .goal_scope import validate_goal_scope
from .models import QueryPlan, RevisionOutput
from .selection import normalize_idea, request_output, selection_context, task_config
from .storage import append_papers, save_json


class ScopedArchive:
    """Map a revision workspace onto the same shared, versioned project archive."""

    def __init__(self, archive, prefix):
        self.archive, self.prefix = archive, prefix

    def path_of(self, path):
        return self.archive.path_of(f"{self.prefix}/{path}")

    def latest(self, path):
        return self.archive.latest(f"{self.prefix}/{path}")

    def put(self, path, *args, **kwargs):
        return self.archive.put(f"{self.prefix}/{path}", *args, **kwargs)


def detect_revision(ctx, state):
    feedback = [a for a in ctx.feedback if a.kind == "idea" and a.status in {"changes_requested", "rejected"}]
    if feedback:
        last = feedback[-1]
        payload = {"approval_id": last.approval_id, "comment": last.decision_comment,
                   "target": last.target.model_dump(mode="json")}
        original = parse_selected_md(ctx.archive.read_text(last.target)).model_dump(mode="json")
    elif ctx.entry is not None:
        payload = {"entry": ctx.entry.to_json()}
        original = load_json(ctx, "idea/selected.json")
    else:
        return
    key = sha256_bytes(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode())[:20]
    if key in state.get("handled_revisions", []):
        return
    path = f"idea/revision_inputs/{key}.json"
    if not ctx.archive.latest(path):
        save_json(ctx, path, {"request": payload, "original": original})
    note = payload.get("comment", "") or (ctx.entry.reason if ctx.entry else "")
    state.update(phase="revise", selection_round=key, revision_input=path, revision_note=note)
    state.setdefault("handled_revisions", []).append(key)


def revise_step(ctx, state):
    try:
        task = task_config(ctx)
        request = load_json(ctx, state["revision_input"])

        def validate(output):
            output.new_queries = list(dict.fromkeys(q.strip() for q in output.new_queries if q.strip()))
            if output.needs_more_literature and not output.new_queries:
                raise ValueError("A literature revision requires at least one nonblank new query")
            normalize_idea(ctx, output.idea, task, output.task_fit)

        receipt = request_output(
            ctx, state, "revise", RevisionOutput, validate,
            {**selection_context(ctx), "revision_request": request},
        )
        output = RevisionOutput.model_validate(receipt["output"])
        if output.needs_more_literature and not output.new_queries:
            raise ValueError("A literature revision requires at least one new query")
        idea = normalize_idea(ctx, output.idea, task, output.task_fit)
        save_json(ctx, "idea/goal_scope.json", validate_goal_scope(ctx.project.goal, idea))
        save_json(ctx, "idea/selected.json", idea.model_dump(mode="json"))
        save_json(ctx, "idea/refinement_status.json", {"task_fit": output.task_fit,
                                                      "task_fit_reason": output.task_fit_reason,
                                                      "trace": receipt["trace"]})
        if output.needs_more_literature:
            state.update(phase="revision_search", revision_literature={"phase": "search"},
                         revision_queries=output.new_queries)
        else:
            state["phase"] = "score"
    except SelectionNeedsDecision as exc:
        return exc.stop
    except RuntimeError as exc:
        return Stop(str(exc))
    except (ValueError, KeyError, OSError) as exc:
        return Error(f"Idea revision failed: {exc}", retryable=False)
    return Continue("Versioned feedback applied; additional retrieval requested" if output.needs_more_literature
                    else "Versioned feedback applied; scores and comparison will be regenerated")


def scoped_context(ctx, state):
    prefix = f"idea/revision_literature/{state['selection_round']}"
    return replace(ctx, root=ctx.root / prefix, archive=ScopedArchive(ctx.archive, prefix))


def prepare_incremental(ctx, scoped, state):
    if not scoped.archive.latest("idea/queries.json"):
        queries = list(dict.fromkeys(q.strip() for q in state["revision_queries"] if q.strip()))
        if not queries:
            raise ValueError("Incremental retrieval requires at least one nonblank query")
        groups = [{"question": f"Feedback-driven retrieval {i + 1}", "purpose": "Resolve a revision question",
                   "queries": [queries[i % len(queries)]]} for i in range(max(3, len(queries)))]
        plan = QueryPlan.model_validate({"subquestions": groups})
        save_json(scoped, "idea/queries.json", plan.model_dump(mode="json"))
        save_json(scoped, "idea/brief.json", load_json(ctx, "idea/brief.json"))
    child = state["revision_literature"]
    if child.get("exclude_identity_version") != 1:
        records = child.get("exclude_papers")
        if records is None:
            records = [paper.model_dump(mode="json") for paper in load_papers(ctx.root).values()]
        child["exclude_papers"] = [PaperIdentity.model_validate(record).model_dump(mode="json") for record in records]
        child["exclude_identity_version"] = 1


def combine_reading(ctx, scoped):
    """Reuse successful old cards; only newly retrieved papers enter the read scheduler."""
    if scoped.archive.latest("idea/combined_input.json"):
        return
    old_input = load_json(ctx, "idea/reading_input.json")
    new_input = load_json(scoped, "idea/reading_input.json")
    old_status = load_json(ctx, "idea/reading_status.json")
    new_status = load_json(scoped, "idea/reading_status.json")
    for cid in old_status["card_ids"]:
        save_json(scoped, f"idea/reading_cards/{cid}.json", load_json(ctx, f"idea/reading_cards/{cid}.json"),
                  "reading_card")
    # Merge reads source texts from the frozen input, without fetching old PDFs again.
    old_papers = list(load_papers(ctx.root).values())
    append_papers(scoped, old_papers)
    prefix = scoped.root.relative_to(ctx.root).as_posix()
    for paper in new_input["papers"]:
        if paper.get("source_path"):
            paper["source_path"] = f"{prefix}/{paper['source_path']}"
    combined_input = {**old_input, "papers": old_input["papers"] + new_input["papers"]}
    combined_status = {**new_status,
                       "card_ids": list(dict.fromkeys(old_status["card_ids"] + new_status["card_ids"])),
                       "failed": old_status["failed"] + new_status["failed"]}
    save_json(scoped, "idea/combined_input.json", {"reading_input": combined_input, "reading_status": combined_status})


def publish_combined_input(scoped):
    combined = load_json(scoped, "idea/combined_input.json")
    save_json(scoped, "idea/reading_input.json", combined["reading_input"])
    save_json(scoped, "idea/reading_status.json", combined["reading_status"])


def incremental_step(ctx, state, stage):
    from .fulltext import fulltext_step
    from .merge import merge_step
    from .readers import gap_step, read_step
    from .screening import screen_step

    scoped = scoped_context(ctx, state)
    child = state["revision_literature"]
    # The engine stores child-stage questions on the outer idea state.
    if ctx.answer is not None and ctx.answer.question_id == state.get("asked"):
        child["asked"] = ctx.answer.question_id
    try:
        prepare_incremental(ctx, scoped, state)
        phase = child["phase"]
        if phase == "search":
            return stage._search(scoped, child)
        if phase == "screen":
            return screen_step(scoped, child)
        if phase == "fulltext":
            return fulltext_step(scoped, child, download=stage.fulltext_download, http=stage.fulltext_http)
        if phase == "read":
            return read_step(scoped, child)
        if phase == "gap":
            combine_reading(ctx, scoped)
            publish_combined_input(scoped)
            result = gap_step(scoped, child)
            if child["phase"] == "merge":
                old = load_json(ctx, "idea/gaps.json")["gaps"]
                new = load_json(scoped, "idea/gaps.json")["gaps"]
                merged = {g["gap_id"]: g for g in old}
                for gap in new:
                    if gap in merged.values():
                        continue
                    value = dict(gap)
                    if value["gap_id"] in merged:
                        value["gap_id"] = f"G{max([int(i[1:]) for i in merged] + [0]) + 1}"
                    merged[value["gap_id"]] = value
                receipt = load_json(scoped, "idea/gaps.json")
                save_json(scoped, "idea/gaps.json", {**receipt, "gaps": list(merged.values())})
            return result
        if phase == "merge":
            return merge_step(scoped, child)
        if phase != "refine":
            raise ValueError(f"Unknown incremental literature phase: {phase}")
        prefix = f"idea/revision_literature/{state['selection_round']}"
        papers = []
        for paper in load_papers(scoped.root).values():
            record = paper.model_copy(deep=True)
            if record.paper_id not in load_papers(ctx.root) and record.fulltext_path:
                record.fulltext_path = f"{prefix}/{record.fulltext_path}"
            papers.append(record)
        append_papers(ctx, papers)
        paths = ["reading_input.json", "reading_status.json", "gaps.json", "merge_input.json",
                 "reading_summary.json", "reading_summary.md", "conflicts.json", "merge_status.json"]
        paths += [f"reading_cards/{cid}.json" for cid in load_json(scoped, "idea/reading_status.json")["card_ids"]]
        for name in paths:
            path = "idea/" + name
            ref = scoped.archive.latest(path)
            previous = ctx.archive.latest(path)
            content = scoped.archive.path_of(path).read_text(encoding="utf-8")
            kind = ("reading_card" if name.startswith("reading_cards/")
                    else "literature" if name.endswith(".md") else "idea")
            ctx.archive.put(path, kind, content, parents=[r for r in (previous, ref) if r],
                            producer="agent:idea/revision")
        state["phase"] = "refine"
        return Continue("New literature merged with preserved reading cards; refine the revised idea next")
    except (ValueError, KeyError, OSError) as exc:
        return Error(f"Incremental literature revision failed: {exc}", retryable=False)
