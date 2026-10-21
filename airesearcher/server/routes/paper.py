"""文献、论文与证据（数据由【文献】【论文】的文件提供；详细设计 1 第 9.2 节）。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from airesearcher.core.errors import NotFound
from airesearcher.core.fsutil import read_json
from airesearcher.core.models.claim import EvidenceTrace
from airesearcher.core.models.literature import PaperRecord
from airesearcher.services import evidence, literature

from ..deps import manager
from ..manager import ProjectManager

router = APIRouter(prefix="/api/projects/{pid}", tags=["paper"])


@router.get("/literature", response_model=list[PaperRecord])
def list_literature(pid: str, m: ProjectManager = Depends(manager)) -> list[PaperRecord]:
    return list(literature.load_papers(m.get(pid).root).values())


@router.get("/literature/{paper_id:path}")
def get_paper(pid: str, paper_id: str, m: ProjectManager = Depends(manager)) -> dict:
    root = m.get(pid).root
    papers = literature.load_papers(root)
    if paper_id not in papers:
        raise NotFound(f"文献记录中没有 {paper_id}")
    # Match stored identities instead of deriving filenames from external paper IDs.
    cards = []
    for path in sorted((root / "idea" / "reading_cards").glob("*.json")):
        card = read_json(path)
        if card.get("paper_id") == paper_id:
            cards.append(card)
    return {"paper": papers[paper_id].model_dump(mode="json"), "reading_cards": cards}


@router.get("/figures")
def list_figures(pid: str, m: ProjectManager = Depends(manager)) -> list[dict]:
    root = m.get(pid).root
    return [read_json(p) for p in sorted((root / "artifacts" / "figures").glob("*/figure.json"))]


@router.get("/paper")
def paper_status(pid: str, m: ProjectManager = Depends(manager)) -> dict:
    p = m.get(pid)
    root = p.root
    reports = sorted((root / "paper" / "review").glob("review_v*.json"),
                     key=lambda x: int(x.stem.removeprefix("review_v") or 0))
    review = read_json(reports[-1]) if reports else None
    compile_report = root / "paper" / "build" / "compile_report.json"
    return {
        "versions": [v.model_dump(mode="json") for v in p.archive.versions("paper")],
        "pdf_versions": [v.model_dump(mode="json") for v in p.archive.versions("paper/build/main.pdf")],
        "pdf_path": str(root / "paper" / "build" / "main.pdf"),
        "compile_report": read_json(compile_report) if compile_report.exists() else None,
        "review_summary": {"file": reports[-1].relative_to(root).as_posix(), "counts": review["counts"],
                           "compile_ok": review["compile_ok"]} if review else None,
    }


@router.get("/paper/pdf")
def paper_pdf(pid: str, version: int | None = None, m: ProjectManager = Depends(manager)):
    p = m.get(pid)
    versions = p.archive.versions("paper/build/main.pdf")
    if not versions:
        raise NotFound("还没有 PDF")
    v = versions[-1] if version is None else next((x for x in versions if x.ref.version == version), None)
    if v is None:
        raise NotFound(f"没有 PDF v{version}")
    return FileResponse(p.archive.stored_path(v.ref), media_type="application/pdf")


@router.get("/claims")
def list_claims(pid: str, m: ProjectManager = Depends(manager)) -> dict:
    root = m.get(pid).root
    claims = [json.loads(line) for line in (root / "paper" / "claims.jsonl").read_text(encoding="utf-8").splitlines()
              if line.strip()] if (root / "paper" / "claims.jsonl").exists() else []
    reports = sorted((root / "paper" / "review").glob("review_v*.json"),
                     key=lambda x: int(x.stem.removeprefix("review_v") or 0))
    checks = {c["claim_id"]: c for c in read_json(reports[-1])["claims"]} if reports else {}
    return {"claims": claims, "checks": checks}


@router.get("/claims/{claim_id}/trace", response_model=EvidenceTrace)
def claim_trace(pid: str, claim_id: str, m: ProjectManager = Depends(manager)) -> EvidenceTrace:
    return evidence.trace(m.get(pid).root, claim_id)
