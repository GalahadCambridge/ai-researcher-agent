"""批量相关性评分与精读选择，不修改已经冻结的文献交接结构。"""

from __future__ import annotations

import hashlib
import json

from airesearcher.core.models.literature import PaperRecord
from airesearcher.engine.stage import Continue, Error
from airesearcher.services.literature import load_papers

from .models import ScreenBatch
from .storage import append_papers, save_json

BATCH_SIZE = 10


def select_reading(papers, scores: dict, plan: dict, limit: int):
    eligible = [p for p in papers if (p.abstract and p.abstract.strip()) or
                (p.access_level == "full_text" and p.fulltext_path)]
    ranked = sorted(eligible, key=lambda p: -scores[p.paper_id].score)
    groups = plan["subquestions"]

    def membership(p):
        return {i for i, group in enumerate(groups) if set(p.queries) & set(group["queries"])}

    selected = ranked[:limit]
    covered = set().union(*(membership(p) for p in selected))
    adjusted = False
    # Design 6.2: when all top papers come from one subquestion, reserve space for other subquestions.
    if selected and len(covered) == 1:
        chosen, coverage = [], set()
        for i in range(len(groups)):
            if i in coverage:
                continue
            candidate = next((p for p in ranked if i in membership(p) and p not in chosen), None)
            if candidate is not None and len(chosen) < limit:
                chosen.append(candidate)
                coverage.update(membership(candidate))
        for paper in ranked:
            if paper not in chosen and len(chosen) < limit:
                chosen.append(paper)
        chosen_ids = {p.paper_id for p in chosen}
        selected = [p for p in ranked if p.paper_id in chosen_ids]
        adjusted = {p.paper_id for p in selected} != {p.paper_id for p in ranked[:limit]}
    covered = set().union(*(membership(p) for p in selected))
    missing = [group["question"] for i, group in enumerate(groups) if i not in covered]
    return selected, missing, adjusted


def screen_step(ctx, state):
    limit = ctx.project.limits.read_papers
    if limit < 0:
        return Error("limits.read_papers 不能小于 0", retryable=False)
    manifest_path = "idea/screen_input.json"
    if not ctx.archive.latest(manifest_path):
        data = {
            "brief": json.loads((ctx.root / "idea/brief.json").read_text(encoding="utf-8")),
            "plan": json.loads((ctx.root / "idea/queries.json").read_text(encoding="utf-8")),
            "papers": [p.model_dump(mode="json") for p in load_papers(ctx.root).values()],
        }
        save_json(ctx, manifest_path, data)
    text = (ctx.root / manifest_path).read_text(encoding="utf-8")
    data = json.loads(text)
    papers = [PaperRecord.model_validate(p) for p in data["papers"]]
    run_id = hashlib.sha256(text.encode()).hexdigest()[:20]
    scores = {}
    for start in range(0, len(papers), BATCH_SIZE):
        batch = papers[start:start + BATCH_SIZE]
        path = f"idea/screen_batches/{run_id}-{start:04d}.json"
        if not ctx.archive.latest(path):
            blocks = []
            for paper in batch:
                content = json.dumps({"paper_id": paper.paper_id, "title": paper.title,
                                      "abstract": paper.abstract}, ensure_ascii=False)
                blocks.append(ctx.llm.wrap_untrusted(content, source=paper.paper_id))
            out = ctx.llm.complete(
                role="idea.screen", prompt_id="idea/screen", tier="fast",
                variables={"brief": json.dumps(data["brief"], ensure_ascii=False),
                           "expected_ids": [p.paper_id for p in batch], "paper_blocks": "\n".join(blocks),
                           "schema_json": ScreenBatch.model_json_schema()}, response_schema=ScreenBatch,
            )
            result = out.parsed
            ids = [p.paper_id for p in result.papers]
            if len(ids) != len(set(ids)) or set(ids) != {p.paper_id for p in batch}:
                return Error("筛选结果必须对当前批次的每篇论文评分一次，不能重复、遗漏或添加 ID", retryable=True)
            save_json(ctx, path, {"result": result.model_dump(mode="json"), "model": out.model,
                                  "prompt_id": out.prompt_id, "prompt_version": out.prompt_version,
                                  "call_id": out.call_id})
            state["screened_papers"] = start + len(batch)
            return Continue(f"已评分 {start + len(batch)}/{len(papers)} 篇，评分批次已保存")
        receipt = json.loads((ctx.root / path).read_text(encoding="utf-8"))
        result = ScreenBatch.model_validate(receipt["result"])
        ids = [p.paper_id for p in result.papers]
        if len(ids) != len(set(ids)) or set(ids) != {p.paper_id for p in batch}:
            return Error("已保存的评分批次与输入文献不匹配", retryable=False)
        scores.update({p.paper_id: p for p in result.papers})
    selected, missing, adjusted = select_reading(papers, scores, data["plan"], limit)
    selected_ids = {p.paper_id for p in selected}
    current = load_papers(ctx.root)
    updates = []
    for paper in papers:
        record = current.get(paper.paper_id, paper).model_copy(deep=True)
        score = scores[paper.paper_id]
        record.relevance = {"score": score.score, "reason": score.reason}
        record.selected_for_reading = record.paper_id in selected_ids
        updates.append(record)
    append_papers(ctx, updates)
    metadata_only = [p.paper_id for p in papers if not ((p.abstract and p.abstract.strip()) or
                     (p.access_level == "full_text" and p.fulltext_path))]
    report = {
        "candidate_count": len(papers), "read_limit": limit,
        "selected_ids": [p.paper_id for p in selected], "selected_count": len(selected),
        "coverage_adjusted": adjusted, "uncovered_subquestions": missing,
        "excluded_without_readable_text": metadata_only,
        "low_relevance_selected": [p.paper_id for p in selected if scores[p.paper_id].score <= 2],
        "note": "仅根据标题与摘要判断相关性；评分不代表论文结论已核验。",
    }
    save_json(ctx, "idea/screening.json", report)
    state.update(phase="fulltext", screened_papers=len(papers), selected_count=len(selected))
    return Continue(f"筛选完成：{len(papers)} 篇候选，选择 {len(selected)} 篇待精读；尚未获取全文或阅读")
