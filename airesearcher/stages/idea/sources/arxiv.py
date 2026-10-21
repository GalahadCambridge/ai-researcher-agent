"""arXiv Atom API adapter; retrieves metadata without treating a PDF link as text already read."""

from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET

from airesearcher.core.fsutil import now
from airesearcher.core.models.literature import PaperRecord, SearchSnapshot

from .http import SourceFailure, require_success
from .metadata import arxiv_id, bibtex, citation_key, doi_id

NS = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}
BASE = "https://export.arxiv.org/api/query"


class ArxivSource:
    name = "arxiv"

    def __init__(self, http, audit=lambda snapshot: None):
        self.http, self.audit = http, audit

    def _fetch(self, params, query):
        requested = now()
        response = self.http.get(BASE, params, interval=3)
        snapshot = SearchSnapshot(snapshot_id="ax-" + uuid.uuid4().hex, source=self.name, query=query,
                                  params=params, requested_at=requested, status=str(response.status_code),
                                  raw_response={"xml": response.text}, paper_ids=[])
        if not response.is_success:
            self.audit(snapshot)
        require_success(response)
        try:
            root = ET.fromstring(response.content)
            if root.tag != "{" + NS["a"] + "}feed":
                raise ValueError("Response is not an Atom feed")
            papers = []
            for entry in root.findall("a:entry", NS):
                def text(tag, entry=entry):
                    return " ".join(entry.findtext(tag, default="", namespaces=NS).split())
                entry_id = text("a:id")
                if "/abs/" not in entry_id:
                    raise ValueError("arXiv error entry or missing identifier")
                axid = arxiv_id(entry_id)
                ids = {"arxiv": axid}
                if text("x:doi"):
                    ids["doi"] = doi_id(text("x:doi"))
                title, abstract = text("a:title"), text("a:summary")
                if not title:
                    raise ValueError("Missing title")
                year = int(text("a:published")[:4])
                authors = [" ".join(a.itertext()).strip() for a in entry.findall("a:author/a:name", NS)]
                identity = "doi" if "doi" in ids else "arxiv"
                paper = PaperRecord(
                    paper_id=f"{identity}:{ids[identity]}", ids=ids, title=title, authors=authors, year=year,
                    venue=text("x:journal_ref") or None, venue_type="preprint", url="https://arxiv.org/abs/" + axid,
                    abstract=abstract or None, access_level="abstract_only" if abstract else "metadata_only",
                    fulltext_path=None, source=self.name, snapshot_id=snapshot.snapshot_id, retrieved_at=now(),
                    queries=[query] if query else [], citation_key=citation_key(authors, year, title), bibtex="",
                )
                paper.bibtex = bibtex(paper)
                papers.append(paper)
        except (ET.ParseError, ValueError, TypeError) as exc:
            snapshot.status = "invalid_response"
            self.audit(snapshot)
            raise SourceFailure("arXiv returned invalid Atom metadata") from exc
        snapshot.status, snapshot.paper_ids = "ok", [p.paper_id for p in papers]
        return snapshot, papers

    def search(self, query, *, year_from=None, limit=20):
        if limit <= 0:
            raise ValueError("limit must be greater than 0")
        # QueryPlan emits plain English. Quote tokens so arXiv boolean operators stay controlled by code.
        terms = " AND ".join('all:"' + t.replace('"', '') + '"' for t in query.split())
        if year_from is not None:
            terms += f" AND submittedDate:[{int(year_from)}01010000 TO 999912312359]"
        return self._fetch({"search_query": terms, "start": 0, "max_results": min(limit, 100)}, query)

    def get(self, paper_id):
        if not paper_id.startswith("arxiv:"):
            return None
        snapshot, papers = self._fetch({"id_list": arxiv_id(paper_id), "max_results": 1}, "")
        self.audit(snapshot)
        return papers[0] if papers else None
