"""Idea stage: requirement parsing, query planning, resumable retrieval, and relevance screening."""

from __future__ import annotations

import hashlib
import json

from airesearcher.core.models.literature import PaperRecord
from airesearcher.core.models.project import MAX_LITERATURE_PAPERS
from airesearcher.engine.stage import Continue, Error, StageContext, StepResult, Stop, Wait
from airesearcher.services.literature import load_papers

from .dedup import PaperIdentity, deduplicate, same_paper
from .fulltext import fulltext_step
from .merge import merge_step
from .models import IdeaBrief, QueryPlan
from .publication import submit_step, write_step
from .readers import gap_step, read_step
from .revision import detect_revision, incremental_step, revise_step
from .screening import screen_step
from .selection import gap_table_step, refine_step, score_step
from .sources.arxiv import ArxivSource
from .sources.base import LiteratureSource
from .sources.http import GuardedHTTP, SourceWait
from .sources.metadata import unique_citation_keys
from .sources.replay import ReplaySource
from .sources.router import FallbackSource
from .sources.semantic_scholar import SemanticScholarSource
from .storage import append_papers
from .storage import save_json as _save


class IdeaStage:
    name = "idea"

    def __init__(self, source: LiteratureSource | None = None, *, fulltext_download=True,
                 fulltext_http=None, stop_after=None):
        self.stop_after = stop_after
        self.source = source
        self.fulltext_download, self.fulltext_http = fulltext_download, fulltext_http

    def step(self, ctx: StageContext) -> StepResult:
        state = ctx.scratch.setdefault("idea", {})
        detect_revision(ctx, state)
        phase = state.setdefault("phase", "parse")
        ctx.progress(f"Literature stage: {phase}")
        if phase == "parse":
            path = "idea/brief.json"
            if not ctx.archive.latest(path):
                out = ctx.llm.complete(
                    role="idea.parse", prompt_id="idea/parse_input", tier="strong",
                    variables={"goal": ctx.project.goal, "constraints": ctx.project.constraints,
                               "task": ctx.project.task, "schema_json": IdeaBrief.model_json_schema()},
                    response_schema=IdeaBrief,
                )
                brief = out.parsed
                # User/project constraints must not silently change through model output.
                brief.constraints = {**brief.constraints, **ctx.project.constraints}
                brief.task = ctx.project.task
                _save(ctx, path, brief.model_dump(mode="json"))
            state["phase"] = "plan_queries"
            return Continue("Research requirements saved to idea/brief.json")
        if phase == "plan_queries":
            path = "idea/queries.json"
            if not ctx.archive.latest(path):
                brief = (ctx.root / "idea/brief.json").read_text(encoding="utf-8")
                out = ctx.llm.complete(
                    role="idea.plan_queries", prompt_id="idea/plan_queries", tier="strong",
                    variables={"brief": brief, "schema_json": QueryPlan.model_json_schema()},
                    response_schema=QueryPlan,
                )
                _save(ctx, path, out.parsed.model_dump(mode="json"))
            state["phase"] = "search"
            return Continue("Search subquestions and English queries saved")
        if phase == "search":
            return self._search(ctx, state)
        if phase == "screen":
            return screen_step(ctx, state)
        if phase == "fulltext":
            return fulltext_step(ctx, state, download=self.fulltext_download, http=self.fulltext_http)
        if phase == "read":
            return read_step(ctx, state)
        if phase == "gap":
            return gap_step(ctx, state)
        if phase == "merge":
            return merge_step(ctx, state)
        if phase == "refine" and self.stop_after == "merge":
            return Stop("Merge complete; stopped at the requested development checkpoint.")
        if phase == "refine":
            return refine_step(ctx, state)
        if phase == "score":
            return score_step(ctx, state)
        if phase == "gap_table":
            return gap_table_step(ctx, state)
        if phase == "write" and self.stop_after == "gap_table":
            return Stop("Selection complete; stopped at the requested development checkpoint.")
        if phase == "write":
            return write_step(ctx, state)
        if phase == "submit":
            return submit_step(ctx, state)
        if phase == "revise":
            return revise_step(ctx, state)
        if phase == "revision_search":
            return incremental_step(ctx, state, self)
        return Error(f"Unknown idea phase: {phase}", retryable=False)

    def _source(self, ctx: StageContext, state: dict) -> LiteratureSource:
        if self.source is not None:
            return self.source
        replay = ctx.project.literature.get("replay_snapshots")
        if replay:
            from pathlib import Path
            path = Path(replay)
            return ReplaySource(path if path.is_absolute() else ctx.root / path)

        def audit(snapshot):
            sid = hashlib.sha256(snapshot.snapshot_id.encode()).hexdigest()[:20]
            _save(ctx, f"idea/search_snapshots/{sid}.json", snapshot.model_dump(mode="json"), "search_snapshot")

        http = GuardedHTTP(ctx.permissions.guard, state.setdefault("network", {}))
        return FallbackSource([SemanticScholarSource(http, audit), ArxivSource(http, audit)],
                              state.setdefault("source_retries", {}), ctx.events)

    def _search(self, ctx: StageContext, state: dict) -> StepResult:
        plan = QueryPlan.model_validate_json((ctx.root / "idea/queries.json").read_text(encoding="utf-8"))
        queries = plan.queries()
        limit = ctx.project.limits.search_papers
        if type(limit) is not int or not 1 <= limit <= MAX_LITERATURE_PAPERS:
            return Error("limits.search_papers must be an integer between 1 and 20", retryable=False)
        excluded = [PaperIdentity.model_validate(p) for p in state.get("exclude_papers", [])]
        retain_limit = min(limit, MAX_LITERATURE_PAPERS - len({p.paper_id for p in excluded}))
        if retain_limit <= 0:
            return Error("The cumulative 20-paper literature limit has been reached", retryable=False)
        registered = load_papers(ctx.root)
        if len(registered) > MAX_LITERATURE_PAPERS:
            return Error("Existing literature exceeds the cumulative 20-paper limit", retryable=False)
        if len(registered) == MAX_LITERATURE_PAPERS:
            state.update(phase="screen", paper_count=len(registered))
            return Continue("20 papers already retained; additional retrieval skipped before screening")
        source = self._source(ctx, state)
        receipts = []
        for query in queries:
            # Saved receipts are the recovery boundary, independent of scratch/checkpoint timing.
            key = hashlib.sha256(json.dumps([query, ctx.project.literature.get("year_from"), limit,
                                            source.name]).encode()).hexdigest()[:20]
            path = f"idea/search_batches/{key}.json"
            if not ctx.archive.latest(path):
                try:
                    snapshot, papers = source.search(
                        query, year_from=ctx.project.literature.get("year_from"), limit=limit,
                    )
                except SourceWait as exc:
                    return Wait(exc.reason, exc.seconds)
                except (ConnectionError, TimeoutError, OSError) as exc:
                    return Error(f"Retrieval failed ({query}): {exc}", retryable=True)
                # Store normalized results together with the raw response before acknowledging progress.
                _save(ctx, path, {"snapshot": snapshot.model_dump(mode="json"),
                                  "papers": [p.model_dump(mode="json") for p in papers]}, "search_snapshot")
                state["completed_queries"] = len(receipts) + 1
                return Continue(f"Retrieval batch saved: {query}")
            receipts.append(json.loads((ctx.root / path).read_text(encoding="utf-8")))
        papers = []
        for receipt in receipts:
            snap = receipt["snapshot"]
            # Use a local hash for filenames; never use an external ID as an unchecked filesystem path.
            sid = hashlib.sha256(snap["snapshot_id"].encode()).hexdigest()[:20]
            _save(ctx, f"idea/search_snapshots/{sid}.json", snap, "search_snapshot")
            papers.extend(PaperRecord.model_validate(p) for p in receipt["papers"])
        candidates = deduplicate(papers)
        candidates = [p for p in candidates if not any(same_paper(p, old) for old in excluded)]
        existing = load_papers(ctx.root)
        known = set(existing) | {p.paper_id for p in excluded}
        slots = MAX_LITERATURE_PAPERS - len(known)
        unique = []
        for paper in candidates[:retain_limit]:
            if paper.paper_id in existing:
                unique.append(paper)
            elif slots > 0:
                unique.append(paper)
                slots -= 1
        unique = unique_citation_keys(unique, existing)
        # Re-finalizing search must preserve later screening/full-text annotations.
        for paper in unique:
            if paper.paper_id in existing:
                old = existing[paper.paper_id]
                paper.relevance, paper.selected_for_reading = old.relevance, old.selected_for_reading
                if old.access_level == "full_text":
                    paper.access_level, paper.fulltext_path = old.access_level, old.fulltext_path
        append_papers(ctx, unique)
        state.update(phase="screen", completed_queries=len(queries), paper_count=len(unique))
        return Continue(
            f"Retrieval complete: {len(queries)} queries, {len(unique)} papers retained after "
            f"deduplication; screening and reading have not started"
        )


def create_stage() -> IdeaStage:
    return IdeaStage()
