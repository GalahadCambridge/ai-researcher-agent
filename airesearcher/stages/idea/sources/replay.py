"""固定快照回放：只读本地文件，缺少匹配时明确报错，不回退联网。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from airesearcher.core.models.literature import PaperRecord, SearchSnapshot

from .arxiv import ArxivSource
from .http import SourceFailure
from .semantic_scholar import SemanticScholarSource


class _SavedHTTP:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def get(self, url, params, **kwargs):
        raw = self.snapshot.raw_response
        if "xml" in raw:
            return httpx.Response(200, text=raw["xml"], request=httpx.Request("GET", url))
        return httpx.Response(200, json=raw, request=httpx.Request("GET", url))


class ReplaySource:
    def __init__(self, snapshot_dir):
        self.root = Path(snapshot_dir).resolve()
        self.name = "replay:" + str(self.root)
        self.last_papers = {}

    def search(self, query, *, year_from=None, limit=20):
        for path in sorted(self.root.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            snap = SearchSnapshot.model_validate(data.get("snapshot", data))
            if snap.status != "ok" or snap.query != query:
                continue
            params = snap.params
            saved_year = params.get("year_from")
            if "year" in params:
                saved_year = int(params["year"].rstrip("-"))
            if "search_query" in params and "submittedDate:[" in params["search_query"]:
                saved_year = int(params["search_query"].split("submittedDate:[")[1][:4])
            saved_limit = int(params.get("limit", params.get("max_results", 20)))
            if saved_year != year_from or saved_limit < limit:
                continue
            if "papers" in data:
                papers = [PaperRecord.model_validate(p) for p in data["papers"]][:limit]
            else:
                source_cls = {"arxiv": ArxivSource, "semantic_scholar": SemanticScholarSource}.get(snap.source)
                if source_cls is None:
                    continue
                _, papers = source_cls(_SavedHTTP(snap)).search(query, year_from=year_from, limit=limit)
                for p in papers:
                    p.snapshot_id, p.retrieved_at = snap.snapshot_id, snap.requested_at
            self.last_papers.update({p.paper_id: p for p in papers})
            return snap, papers
        raise SourceFailure(f"回放缺少查询快照：{query}（年份和数量也必须匹配）；未联网")

    def get(self, paper_id):
        return self.last_papers.get(paper_id)
