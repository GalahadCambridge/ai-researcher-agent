"""可恢复的多角色阅读；每步最多六个任务，并发数由项目配置控制。"""

from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from airesearcher.core.fsutil import now, sha256_bytes
from airesearcher.core.models.literature import ReadingCard
from airesearcher.engine.stage import Continue, Error
from airesearcher.services.literature import load_papers

from .models import GapAnalysis, ReaderOutput
from .quotes import QuoteVerifier
from .storage import save_json

METHOD_FIELDS = ["method", "task", "dataset", "metric", "baseline", "main_result", "setting", "limitation"]
FEASIBILITY_FIELDS = ["code_available", "data_available", "compute", "dependencies", "license", "reproduction_risk"]
ROLE_FIELDS = {"methods": METHOD_FIELDS, "feasibility": FEASIBILITY_FIELDS,
               "single": METHOD_FIELDS + FEASIBILITY_FIELDS}


def card_id(paper_id, role):
    if re.fullmatch(r"arxiv:\d{4}\.\d{4,5}", paper_id):
        base = paper_id.replace(":", "_")
    else:
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", paper_id)[:80]
        base = safe + "-" + hashlib.sha256(paper_id.encode()).hexdigest()[:12]
    return base + "__" + role


def _source(ctx, paper):
    warning = ""
    if paper.access_level == "full_text" and paper.fulltext_path:
        try:
            text = ctx.archive.path_of(paper.fulltext_path).read_text(encoding="utf-8")
            if text.strip():
                words = list(re.finditer(r"\S+", text))
                text = text[:words[11999].end()] if len(words) > 12000 else text
                return text, "full_text", paper.fulltext_path, warning
        except Exception:
            pass
        warning = "全文文本缺失或不可读，阅读输入降为摘要"
    return paper.abstract or "", "abstract_only", None, warning


def _manifest(ctx):
    path = "idea/reading_input.json"
    if not ctx.archive.latest(path):
        papers = []
        for paper in load_papers(ctx.root).values():
            if not paper.selected_for_reading:
                continue
            text, based_on, source_path, warning = _source(ctx, paper)
            papers.append({"paper_id": paper.paper_id, "title": paper.title, "text": text,
                           "based_on": based_on, "source_path": source_path, "warning": warning,
                           "input_sha256": sha256_bytes(text.encode())})
        save_json(ctx, path, {"goal": ctx.project.goal, "reading_mode": ctx.project.reading_mode, "papers": papers})
    return json.loads((ctx.root / path).read_text(encoding="utf-8"))


def _validate_items(items, role, source):
    fields = [item.field for item in items]
    if len(fields) != len(set(fields)) or set(fields) != set(ROLE_FIELDS[role]):
        raise ValueError("阅读结果必须覆盖角色全部字段一次，不能重复或添加字段")
    checker = QuoteVerifier(source["text"])
    return [checker.check(item, source["based_on"]) for item in items]


def _read_one(ctx, source, task, goal):
    if not source["text"].strip():
        raise ValueError("论文没有可读全文或摘要")
    role = task["role"]
    prompt = f"idea/{role}_reader"
    out = ctx.llm.complete(
        role=f"idea.reader.{role}", prompt_id=prompt, tier="fast", response_schema=ReaderOutput,
        variables={"research_question": goal, "paper_id": source["paper_id"], "based_on": source["based_on"],
                   "required_fields": json.dumps(ROLE_FIELDS[role]), "schema_json": ReaderOutput.model_json_schema(),
                   "paper_block": ctx.llm.wrap_untrusted(source["text"], source=source["paper_id"])},
    )
    if not out.parsed.relevance_to_question.strip():
        raise ValueError("与研究问题的关系不能为空")
    return ReadingCard(card_id=task["card_id"], paper_id=source["paper_id"], role=role,
                       based_on=source["based_on"], items=_validate_items(out.parsed.items, role, source),
                       relevance_to_question=out.parsed.relevance_to_question, model=out.model,
                       prompt_id=out.prompt_id, prompt_version=out.prompt_version,
                       call_id=out.call_id, created_at=now())


def _save_card(ctx, card):
    path = f"idea/reading_cards/{card.card_id}.json"
    content = card.model_dump(mode="json")
    target = ctx.root / path
    text = json.dumps(content, ensure_ascii=False, indent=2) + "\n"
    if ctx.archive.latest(path) and target.exists() and target.read_text(encoding="utf-8") == text:
        return
    save_json(ctx, path, content, "reading_card")


def read_step(ctx, state):
    concurrency = ctx.project.limits.reader_concurrency
    if concurrency < 1:
        return Error("reader_concurrency 必须大于 0", retryable=False)
    manifest = _manifest(ctx)
    sources = {p["paper_id"]: p for p in manifest["papers"]}
    roles = ["single"] if manifest["reading_mode"] == "single" else ["methods", "feasibility"]
    tasks = state.setdefault("read_tasks", [
        {"paper_id": pid, "role": role, "card_id": card_id(pid, role), "status": "pending", "attempts": 0}
        for pid in sources for role in roles
    ])
    for task in tasks:
        path = f"idea/reading_receipts/{task['card_id']}.json"
        if not ctx.archive.latest(path):
            continue
        saved = json.loads((ctx.root / path).read_text(encoding="utf-8"))
        source = sources[task["paper_id"]]
        if saved["input_sha256"] != source["input_sha256"]:
            return Error("阅读缓存与本次输入不一致，请使用独立项目或重新生成输入", retryable=False)
        if saved["status"] == "done":
            card = ReadingCard.model_validate(saved["card"])
            if (card.card_id, card.paper_id, card.role, card.based_on) != (
                    task["card_id"], task["paper_id"], task["role"], source["based_on"]):
                return Error("阅读卡身份与任务不匹配", retryable=False)
            card.items = _validate_items(card.items, task["role"], source)
            _save_card(ctx, card)
        task.update(status=saved["status"], attempts=saved["attempts"], last_error=saved.get("last_error", ""))
    pending = [t for t in tasks if t["status"] == "pending"][:6]
    if pending:
        # Workers only call/read/validate; task state and artifact writes stay on the scheduling thread.
        with ThreadPoolExecutor(max_workers=min(concurrency, len(pending), 6)) as pool:
            futures = {pool.submit(_read_one, ctx, sources[t["paper_id"]], t, manifest["goal"]): t for t in pending}
            for future in as_completed(futures):
                task = futures[future]
                task["attempts"] += 1
                saved = {"input_sha256": sources[task["paper_id"]]["input_sha256"],
                         "attempts": task["attempts"], "last_error": ""}
                try:
                    card = future.result()
                    task.update(status="done", last_error="")
                    saved.update(status="done", card=card.model_dump(mode="json"))
                except Exception as exc:
                    task.update(status="failed" if task["attempts"] >= 2 else "pending",
                                last_error=f"{type(exc).__name__}: {exc}")
                    saved.update(status=task["status"], last_error=task["last_error"])
                save_json(ctx, f"idea/reading_receipts/{task['card_id']}.json", saved)
                if task["status"] == "done":
                    _save_card(ctx, card)
        return Continue(f"阅读任务进度：{sum(t['status'] == 'done' for t in tasks)}/{len(tasks)} 完成")
    cards = [ReadingCard.model_validate_json((ctx.root / f"idea/reading_cards/{t['card_id']}.json").read_text(
        encoding="utf-8")) for t in tasks if t["status"] == "done"]
    items = [item for card in cards for item in card.items]
    report = {"reading_mode": manifest["reading_mode"], "planned_tasks": len(tasks), "done": len(cards),
              "failed": [t for t in tasks if t["status"] == "failed"], "card_ids": [c.card_id for c in cards],
              "quote_verified_count": sum(i.quote_verified is True for i in items),
              "quote_failed_count": sum(i.quote_verified is False for i in items),
              "without_quote_count": sum(i.quote_verified is None for i in items),
              "source_warnings": [{"paper_id": p["paper_id"], "warning": p["warning"]}
                                  for p in manifest["papers"] if p["warning"]],
              "note": "文字核对不等于语义和结论核验；失败任务和缺少证据的条目必须保留。"}
    save_json(ctx, "idea/reading_status.json", report)
    state["phase"] = "gap"
    return Continue(f"阅读卡准备完成：{len(cards)} 张，{len(report['failed'])} 项失败；下一步生成差距候选")


def gap_step(ctx, state):
    path = "idea/gaps.json"
    if not ctx.archive.latest(path):
        report = json.loads((ctx.root / "idea/reading_status.json").read_text(encoding="utf-8"))
        cards = [ReadingCard.model_validate_json((ctx.root / f"idea/reading_cards/{cid}.json").read_text(
            encoding="utf-8")) for cid in report["card_ids"]]
        refs = {f"{card.card_id}#{i}": (card, item) for card in cards for i, item in enumerate(card.items)
                if item.field in METHOD_FIELDS}
        trace = {}
        if refs:
            compact = [{"ref": ref, "based_on": card.based_on, **item.model_dump(mode="json")}
                       for ref, (card, item) in refs.items()]
            out = ctx.llm.complete(
                role="idea.gap", prompt_id="idea/gap_analyst", tier="fast", response_schema=GapAnalysis,
                variables={"goal": ctx.project.goal, "schema_json": GapAnalysis.model_json_schema(),
                           "cards_block": ctx.llm.wrap_untrusted(json.dumps(compact, ensure_ascii=False),
                                                                  source="reading_cards")},
            )
            gaps = out.parsed.gaps
            if len({g.gap_id for g in gaps}) != len(gaps) or any(ref not in refs for g in gaps for ref in g.evidence):
                return Error("差距候选含重复 ID 或不存在的阅读卡条目引用", retryable=True)
            for gap in gaps:
                if not any(refs[r][0].based_on == "full_text" and refs[r][1].kind == "author_claim"
                           and refs[r][1].quote_verified is True for r in gap.evidence):
                    gap.status = "to_verify"
            trace = {"model": out.model, "prompt_id": out.prompt_id,
                     "prompt_version": out.prompt_version, "call_id": out.call_id}
        else:
            gaps = []
        save_json(ctx, path, {"gaps": [g.model_dump(mode="json") for g in gaps], **trace,
                              "incomplete_reading": [t["card_id"] for t in report["failed"]],
                              "note": "仅为检索范围内的差距候选，尚未协调合并，不证明全球研究空白或新颖性。"})
    state["phase"] = "merge"
    return Continue("差距候选已保存，下一步由协调者合并与检查冲突")
