"""Semantic Scholar Graph API 适配器，一次调用只发一个请求。"""

from __future__ import annotations

import os
import uuid
from urllib.parse import quote

from airesearcher.core.fsutil import now
from airesearcher.core.models.literature import PaperRecord, SearchSnapshot

from .http import SourceFailure, require_success
from .metadata import arxiv_id, bibtex, citation_key, doi_id

FIELDS = "title,authors,year,venue,publicationTypes,externalIds,abstract,url,citationStyles"
BASE = "https://api.semanticscholar.org/graph/v1/paper"


class SemanticScholarSource:
    name = "semantic_scholar"

    def __init__(self, http, audit=lambda snapshot: None):
        self.http, self.audit = http, audit

    def _request(self, url, params, query):
        requested = now()
        key = os.environ.get("S2_API_KEY")
        response = self.http.get(url, params, interval=1, headers={"x-api-key": key} if key else None)
        try:
            raw = response.json()
        except ValueError:
            raw = {"body": response.text}
        snapshot = SearchSnapshot(snapshot_id="ss-" + uuid.uuid4().hex, source=self.name, query=query,
                                  params=params, requested_at=requested, status=str(response.status_code),
                                  raw_response=raw if isinstance(raw, dict | list) else {"body": raw}, paper_ids=[])
        if not response.is_success:
            self.audit(snapshot)
        require_success(response)
        return response, raw, snapshot

    def _record(self, row, snapshot):
        external = row.get("externalIds") or {}
        ids = {}
        if external.get("DOI"):
            ids["doi"] = doi_id(external["DOI"])
        if external.get("ArXiv"):
            ids["arxiv"] = arxiv_id(external["ArXiv"])
        if row.get("paperId"):
            ids["s2"] = row["paperId"]
        if not ids or not row.get("title"):
            raise ValueError("文献缺少标题或标识")
        identity = next(k for k in ("doi", "arxiv", "s2") if k in ids)
        authors = [a["name"] for a in row.get("authors", []) if a.get("name")]
        types = row.get("publicationTypes") or []
        venue_type = "journal" if "JournalArticle" in types else "conference" if "Conference" in types else (
            "preprint" if "arxiv" in ids and not row.get("venue") else "unknown")
        url = ("https://doi.org/" + ids["doi"] if "doi" in ids else
               "https://arxiv.org/abs/" + ids["arxiv"] if "arxiv" in ids else row.get("url") or "")
        paper = PaperRecord(
            paper_id=f"{identity}:{ids[identity]}", ids=ids, title=row["title"], authors=authors,
            year=row.get("year"), venue=row.get("venue"), venue_type=venue_type, url=url,
            abstract=row.get("abstract"), access_level="abstract_only" if row.get("abstract") else "metadata_only",
            fulltext_path=None, source=self.name, snapshot_id=snapshot.snapshot_id, retrieved_at=now(),
            queries=[snapshot.query] if snapshot.query else [], citation_key=citation_key(authors, row.get("year"),
                                                                                       row["title"]), bibtex="",
        )
        # Generate from returned metadata so the key and escaped fields stay consistent.
        paper.bibtex = bibtex(paper)
        return paper

    def search(self, query, *, year_from=None, limit=20):
        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        params = {"query": query, "limit": min(limit, 100), "fields": FIELDS}
        if year_from is not None:
            params["year"] = f"{year_from}-"
        _, raw, snapshot = self._request(BASE + "/search", params, query)
        try:
            if not isinstance(raw, dict) or not isinstance(raw.get("data"), list):
                raise ValueError("响应缺少 data 列表")
            papers = [self._record(row, snapshot) for row in raw["data"][:limit]]
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            snapshot.status = "invalid_response"
            self.audit(snapshot)
            raise SourceFailure("Semantic Scholar 返回无效元数据") from exc
        snapshot.status, snapshot.paper_ids = "ok", [p.paper_id for p in papers]
        return snapshot, papers

    def get(self, paper_id):
        prefix, _, value = paper_id.partition(":")
        lookup = {"doi": "DOI:", "arxiv": "ARXIV:", "s2": ""}.get(prefix)
        if lookup is None:
            return None
        try:
            _, raw, snapshot = self._request(BASE + "/" + quote(lookup + value, safe=""), {"fields": FIELDS}, "")
        except SourceFailure as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        try:
            paper = self._record(raw, snapshot)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise SourceFailure("Semantic Scholar 单篇元数据无效") from exc
        snapshot.status, snapshot.paper_ids = "ok", [paper.paper_id]
        self.audit(snapshot)
        return paper
