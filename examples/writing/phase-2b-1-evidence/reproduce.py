"""Retain a real WritingStage run from public inputs, without paid model calls."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

from pypdf import PdfReader

from airesearcher.core.archive import dir_manifest
from airesearcher.core.fsutil import sha256_file
from airesearcher.core.models.claim import CheckReport, CompileReport
from airesearcher.core.workspace import repo_root
from airesearcher.services import evidence


def main():
    # Repository development tools are intentionally not part of the installed package.
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from scripts.writing.demo import prepare_sample, run

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--control", action="store_true", help="Run the false arm in a fresh workspace")
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    repo = repo_root()
    if output.exists() or output == repo or repo in output.parents:
        parser.error("choose a new output directory outside the repository")
    if not shutil.which("latexmk"):
        parser.error("real latexmk is required; no installation or placeholder PDF is performed")
    before = dir_manifest(repo / "fixtures/sample_project", [])
    previous_example = dir_manifest(repo / "examples/writing/phase-2a-4-writer", [])
    os.environ.update(
        AIR_REAL_LLM="0",
        AIR_HOME=str(output / "air-home"),
        MPLCONFIGDIR=str(output / "cache/matplotlib"),
        XDG_CACHE_HOME=str(output / "cache"),
    )
    output.mkdir(parents=True)
    project, engine = prepare_sample(output / "project", compile_enabled=True, evidence_check=not args.control)
    approval = run(engine)
    paper = project.root / "paper"
    report = CompileReport.model_validate_json((paper / "build/compile_report.json").read_bytes())
    if not report.ok or report.errors or report.undefined_citations or report.undefined_refs or report.missing_files:
        raise RuntimeError(report.model_dump_json())
    reader = PdfReader(paper / "build/main.pdf")
    text = "\n".join(page.extract_text() for page in reader.pages)
    if len(reader.pages) != report.pages or "Generative AI Use" not in text:
        raise RuntimeError("actual PDF does not contain expected Stage prose/disclosure")
    check_report = None
    claims = []
    if args.control:
        if (
            (paper / "claims.jsonl").exists()
            or (paper / "air_values.tex").exists()
            or list((paper / "review").glob("review_v*.json"))
        ):
            raise RuntimeError("unexpected Phase 2B control-arm output")
        if "0.779900" not in text:
            raise RuntimeError("control PDF is missing actual result prose")
    else:
        claims = evidence.load_claims(project.root)
        check_report = CheckReport.model_validate_json((paper / "review/review_v1.json").read_bytes())
        if (
            check_report.paper_ref != approval.target
            or not check_report.compile_ok
            or check_report.counts.get("blocker")
            or check_report.counts.get("major")
        ):
            raise RuntimeError("true manuscript has deterministic errors or incorrect provenance")
        if not claims or any(v.rendered not in text for c in claims for v in c.values):
            raise RuntimeError("real PDF does not contain the bound values")
        if "Semantic LLM claim review was not run" not in text:
            raise RuntimeError("missing deterministic-only disclosure")
    selected = output / "example"
    selected.mkdir()
    copies = {name: paper / name for name in ("main.tex", "references.bib", "ai_use.tex", "evidence_bundle.json")}
    if not args.control:
        copies.update(
            {
                name: paper / name
                for name in ("claims.jsonl", "air_values.tex", "review/review_v1.json", "review/review_v1.md")
            }
        )
    copies.update({"main.pdf": paper / "build/main.pdf", "compile_report.json": paper / "build/compile_report.json"})
    copies.update(
        {
            p.relative_to(paper).as_posix(): p
            for sub in ("sections", "figures")
            for p in (paper / sub).iterdir()
            if p.is_file()
        }
    )
    state = engine.ck.scratch["writing"]
    for fid in state["figures"]:
        directory = project.root / f"artifacts/figures/{fid}"
        copies.update({f"figure_artifacts/{fid}/{p.name}": p for p in directory.iterdir() if p.is_file()})
    for name, source in sorted(copies.items()):
        target = selected / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if target.read_bytes() != source.read_bytes():
            raise RuntimeError(f"retained artifact differs from compilation input: {name}")
    implementation = [
        *sorted((repo / "airesearcher/stages/writing").glob("*.py")),
        *sorted((repo / "airesearcher/stages/writing/prompts").glob("*.md")),
        repo / "scripts/writing/demo.py",
        repo / "airesearcher/services/evidence.py",
        repo / "airesearcher/services/runs.py",
        repo / "airesearcher/services/_tex.py",
        repo / "skills/latex-writing/SKILL.md",
        repo / "skills/latex-writing/skill.yaml",
        repo / "skills/registry.yaml",
        Path(__file__).resolve(),
    ]
    calls = Counter(c.prompt_id for c in engine.llm.backend.calls)
    metadata = {
        "code_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip(),
        "implementation_sha256": {p.relative_to(repo).as_posix(): sha256_file(p) for p in implementation},
        "tracked_changes": subprocess.check_output(
            ["git", "diff", "--name-only", "HEAD"], cwd=repo, text=True
        ).splitlines(),
        "untracked_files": subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard"], cwd=repo, text=True
        ).splitlines(),
        "input": "fresh copy of fixtures/sample_project; original fake paper and figures preserved outside paper/",
        "configuration": {
            "evidence_check": not args.control,
            "writing": "real",
            "compile": True,
            "title": project.config.title,
        },
        "template": "verified latex-writing default",
        "model_backend": type(engine.llm.backend).__name__,
        "AIR_REAL_LLM": "0",
        "model_calls": dict(calls),
        "paid_calls": 0,
        "online_citation_checks": 0,
        "approved": {k: v.model_dump() for k, v in engine.ck.approved.items()},
        "selected_aggregates": state["aggregates"],
        "bundle_ref": state["bundle_ref"],
        "figure_refs": state["figures"],
        "paper_ref": state["paper_ref"],
        "extra_refs": state["extra_refs"],
        "claims_ref": state.get("claims_ref"),
        "review_ref": state.get("review_ref"),
        "claims_count": len(claims),
        "values_count": len({v.key for c in claims for v in c.values}),
        "deterministic_counts": check_report.counts if check_report else None,
        "archive_graph": [
            {"ref": info.ref.model_dump(), "parents": [r.model_dump() for r in info.parents]}
            for aid in [
                "paper",
                "paper/claims.jsonl",
                "paper/evidence_bundle.json",
                "paper/review/review_v1.json",
                "paper/review/review_v1.md",
                "paper/build/compile_report.json",
                "paper/build/main.pdf",
                *[r["artifact_id"] for r in state["aggregates"]],
                *[r["artifact_id"] for r in state["figures"].values()],
            ]
            if (info := project.archive.latest_version(aid))
        ],
        "state": engine.state.value,
        "approval": approval.model_dump(mode="json"),
        "citation_keys": sorted(re.findall(r"@\w+\{([^,\s]+),", (paper / "references.bib").read_text())),
        "pdf_bytes": (paper / "build/main.pdf").stat().st_size,
        "pages": len(reader.pages),
        "sha256": {name: sha256_file(selected / name) for name in sorted(copies)},
        "latexmk": subprocess.check_output(["latexmk", "-v"], text=True).strip(),
        "report_log": "source-specific real workspace log; build logs and internal receipts are not distributed",
    }
    (selected / "generation.json").write_text(
        json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    if before != dir_manifest(repo / "fixtures/sample_project", []):
        raise RuntimeError("canonical fixture changed")
    if previous_example != dir_manifest(repo / "examples/writing/phase-2a-4-writer", []):
        raise RuntimeError("previous retained example changed")
    print(
        json.dumps(
            {
                "workspace": str(project.root),
                "example": str(selected),
                "calls": dict(calls),
                "pdf_bytes": metadata["pdf_bytes"],
                "pages": metadata["pages"],
                "paper_ref": state["paper_ref"],
                "state": engine.state.value,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
