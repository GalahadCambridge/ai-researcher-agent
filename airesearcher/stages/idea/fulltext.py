"""Fetch selected arXiv papers; retain abstracts on failure and resume from saved results."""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urljoin, urlsplit

from airesearcher.core.fsutil import now, sha256_bytes
from airesearcher.core.models.project import MAX_LITERATURE_PAPERS
from airesearcher.engine.stage import Continue, Error, Wait
from airesearcher.services.literature import load_papers

from .pdf_text import EXTRACTION_VERSION, MAX_PDF_BYTES, extract_pdf
from .sources.http import GuardedHTTP, SourceWait, require_success
from .sources.metadata import arxiv_id
from .storage import append_papers, save_json

_AXID = re.compile(r"^(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?$")


def _paths(paper):
    axid = arxiv_id(paper.ids.get("arxiv", ""))
    key = hashlib.sha256(json.dumps([paper.paper_id, axid]).encode()).hexdigest()[:20]
    return axid, key


def fulltext_step(ctx, state, *, download=True, http=None):
    registered = load_papers(ctx.root)
    if len(registered) > MAX_LITERATURE_PAPERS:
        return Error("Existing literature exceeds the cumulative 20-paper limit", retryable=False)
    papers = [p for p in registered.values() if p.selected_for_reading]
    network = http or GuardedHTTP(ctx.permissions.guard, state.setdefault("network", {}))
    completed = []
    for paper in papers:
        axid, key = _paths(paper)
        receipt_path = f"idea/fulltext_records/{key}-{'fetch' if download else 'local'}.json"
        pdf_path, raw_path, reading_path = (
            f"idea/fulltext/{key}{suffix}" for suffix in (".pdf", ".txt", ".reading.txt"))
        cached_pdf = False
        pdf_ref = ctx.archive.latest(pdf_path)
        if pdf_ref and (ctx.root / pdf_path).exists():
            cached_pdf = sha256_bytes((ctx.root / pdf_path).read_bytes()) == pdf_ref.sha256
        if ctx.archive.latest(receipt_path):
            saved = json.loads((ctx.root / receipt_path).read_text(encoding="utf-8"))
            valid = True
            if saved["status"] == "full_text":
                cached_pdf = cached_pdf and sha256_bytes((ctx.root / pdf_path).read_bytes()) == saved["pdf_sha256"]
                try:
                    valid = (saved.get("extraction_version") == EXTRACTION_VERSION and
                             sha256_bytes(ctx.archive.path_of(saved["fulltext_path"]).read_bytes())
                             == saved["reading_sha256"] and
                             sha256_bytes(ctx.archive.path_of(saved["pdf_path"]).read_bytes()) == saved["pdf_sha256"])
                except (OSError, KeyError, TypeError, ValueError):
                    valid = False
            if valid:
                completed.append(saved)
                continue
        outcome = {"paper_id": paper.paper_id, "arxiv_id": axid or None, "checked_at": now().isoformat(),
                   "status": "abstract_only", "reason": "", "pdf_path": None, "raw_text_path": None,
                   "fulltext_path": None, "source_url": None, "extraction": None}
        pending = state.setdefault("fulltext_pending", {}).setdefault(key, {
            "url": "https://arxiv.org/pdf/" + axid, "redirects": 0})
        try:
            if not axid:
                outcome.update(status="skipped_no_arxiv", reason=(
                    "Non-arXiv paper: only the abstract or metadata is used in this version"
                ))
            elif not _AXID.fullmatch(axid):
                outcome.update(status="invalid_arxiv_id", reason=(
                    "Invalid arXiv identifier; no network request was made"
                ))
            elif not cached_pdf and not download:
                outcome.update(status="download_disabled", reason=(
                    "PDF downloading is disabled for this demo; the existing abstract is retained"
                ))
            else:
                if cached_pdf:
                    data = (ctx.root / pdf_path).read_bytes()
                    outcome["source_url"] = "https://arxiv.org/pdf/" + axid
                else:
                    url = pending["url"]
                    parts = urlsplit(url)
                    if (parts.scheme != "https" or parts.hostname not in {"arxiv.org", "export.arxiv.org"}
                            or parts.port not in (None, 443) or parts.username or parts.password):
                        raise ValueError("PDF redirect target is not an allowed arXiv HTTPS URL")
                    response = network.get(url, {}, interval=3, max_bytes=MAX_PDF_BYTES)
                    outcome["source_url"] = url
                    if response.is_redirect:
                        if pending["redirects"] >= 3 or not response.headers.get("location"):
                            raise ValueError("Too many PDF redirects or missing redirect target")
                        pending.update(url=urljoin(url, response.headers["location"]),
                                       redirects=pending["redirects"] + 1)
                        return Continue("Recorded arXiv PDF redirect; target permissions will be checked next")
                    require_success(response)
                    data = response.content
                    if not data.lstrip().startswith(b"%PDF-"):
                        raise ValueError("Response is not a PDF")
                    ctx.permissions.guard("write", pdf_path)
                    ctx.archive.put(pdf_path, "literature", data, producer="agent:idea/fulltext")
                outcome["pdf_path"] = pdf_path
                raw, reading, details = extract_pdf(data)
                pdf_ref = ctx.archive.latest(pdf_path)
                for path, text in ((raw_path, raw), (reading_path, reading)):
                    ctx.permissions.guard("write", path)
                    ctx.archive.put(path, "literature", text, parents=[pdf_ref] if pdf_ref else [],
                                    producer="agent:idea/fulltext")
                outcome.update(status="full_text", reason=(
                    "Saved the PDF and readable text; reading analysis has not started"
                ),
                               raw_text_path=raw_path, fulltext_path=reading_path, extraction=details,
                               extraction_version=EXTRACTION_VERSION, pdf_sha256=sha256_bytes(data),
                               reading_sha256=sha256_bytes(reading.encode()))
        except SourceWait as exc:
            return Wait(exc.reason, exc.seconds)
        except Exception as exc:
            # Parser/HTTP errors must not fabricate text or block other selected papers.
            outcome.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
        save_json(ctx, receipt_path, outcome)
        state["fulltext_pending"].pop(key, None)
        state["fulltext_completed"] = len(completed) + 1
        return Continue(f"Full-text processing: {paper.paper_id} → {outcome['status']}")
    by_id = {item["paper_id"]: item for item in completed}
    updates = []
    for paper in papers:
        item = by_id[paper.paper_id]
        record = paper.model_copy(deep=True)
        if item["status"] == "full_text":
            record.access_level, record.fulltext_path = "full_text", item["fulltext_path"]
        else:
            record.access_level = "abstract_only" if record.abstract else "metadata_only"
            record.fulltext_path = None
        updates.append(record)
    append_papers(ctx, updates)
    report = {"selected_count": len(papers), "full_text_count": sum(i["status"] == "full_text" for i in completed),
              "abstract_or_metadata_count": sum(i["status"] != "full_text" for i in completed), "papers": completed,
              "note": (
                  "Records retrieval and text extraction only; model reading has not started. The "
                  "original PDF, complete extracted text, and reading excerpt are saved separately."
              )}
    save_json(ctx, "idea/fulltext_summary.json", report)
    state.update(phase="read", fulltext_completed=len(papers), fulltext_count=report["full_text_count"])
    return Continue(
        f"Full-text preparation complete: {len(papers)} papers selected, "
        f"{report['full_text_count']} have readable full text; "
        "the remaining papers retain abstracts or metadata"
    )
