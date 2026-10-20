"""Reproduce the public sample with FakeLLM and real TeX, outside this repository.

Run with the repository's Python environment; no pytest imports, real model calls,
downloads, installation, or production Stage orchestration are involved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from airesearcher.core.archive import dir_manifest
from airesearcher.core.project import Project
from airesearcher.stages.writing.bundle import build_bundle
from airesearcher.stages.writing.writer import GENERATION_ORDER, PROMPT_ID, SECTIONS, Writer
from airesearcher.testing.fake_llm import fake_gateway

REPO = Path(__file__).resolve().parents[3]
TITLE = "Sample Project: Writer Integration"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sample_responses(bundle):
    """Example-only English prose; every reported statistic is read from this bundle.

    This deliberately scripted response is not a scientific checker or a substitute
    for a model. It demonstrates the gateway, Writer and build interfaces only.
    """
    aggregates = {a.aggregate_id: a.data for a in bundle.aggregates}
    c1, c2 = aggregates["C1"], aggregates["C2"]
    rows = {r.group["method"]: r for r in c1.rows}
    main, baseline = rows["main"], rows["baseline"]
    ablation = next(r for r in c2.rows if r.group["method"] == "ablation")
    contrast1 = next(c for c in c1.contrasts if c.a == {"method": "main"} and c.b == {"method": "baseline"})
    contrast2 = next(c for c in c2.contrasts if c.a == {"method": "main"} and c.b == {"method": "ablation"})
    means = f"{main.mean:.6f} and {baseline.mean:.6f}"
    content = {
        "abstract": "We compare baseline, main and ablation conditions using the approved smoke-task evidence. "
                    f"The main and baseline C1 means are {means}, with {main.n} and {baseline.n} runs respectively. "
                    "The observations motivate cautious follow-up within the recorded scope.",
        "introduction": r"""\section{Introduction}
We compare baseline, main and ablation methods on the approved smoke task.
Pretraining and parameter-efficient adaptation motivate the comparison~\cite{devlin2019bert,hu2022lora}.
This literature motivation does not establish that the smoke implementation reproduces those methods.
""",
        "related_work": r"""\section{Related Work}
BERT describes pretraining and downstream fine-tuning~\cite{devlin2019bert}.
LoRA studies low-rank adaptation~\cite{hu2022lora}.
Only the supplied abstracts are available; detailed experimental settings are not inferred.
""",
        "method": r"""\section{Method}
The approved comparison contains baseline, main and ablation conditions.
The bundled plan and approved process log define the scope; unavailable implementation details remain unspecified.
""",
        "setup": "\\section{Experimental Setup}\n"
                 f"The bundled final-condition C1 score observations contain {main.n} main "
                 f"and {baseline.n} baseline runs. "
                 f"C2 contains {ablation.n} ablation runs. "
                 "Comparisons C1 and C2 retain the supplied uncertainty and contrast statistics. "
                 "Hardware details are not supplied in the evidence context.\n",
        "results": "\\section{Results}\n"
                   f"For C1, the mean scores for main and baseline are {means}. "
                   f"The supplied 95\\% intervals are [{main.ci95[0]:.6f}, {main.ci95[1]:.6f}] "
                   f"and [{baseline.ci95[0]:.6f}, {baseline.ci95[1]:.6f}]. "
                   f"The mean difference is {contrast1.diff_mean:.6f} with reported p = {contrast1.p_value:.4f}. "
                   f"For C2, the ablation mean is {ablation.mean:.6f} and the main-minus-ablation difference is "
                   f"{contrast2.diff_mean:.6f} (p = {contrast2.p_value:.4f}). "
                   "These are observations under the recorded final condition, rather than causal explanations.\n",
        "discussion": "\\section{Discussion}\n"
                      "The observed differences may motivate further evaluation; "
                      "they do not establish a general causal effect.\n\\subsection{Limitations}\n"
                      f"The evidence covers the smoke task, with C1 group counts {main.n} and {baseline.n}. "
                      "Exclusions and suspicious-run annotations retain their recorded meaning. "
                      "Local offline citation checks do not verify scientific claims online, "
                      "and abstract-only access does not support reconstruction of unavailable experimental details.\n",
        "conclusion": "\\section{Conclusion}\n"
                      "The supplied C1 and C2 observations suggest further investigation within the recorded scope; "
                      "they do not establish performance on new tasks.\n",
    }
    return [{"section": section, "latex": content[section]} for section in GENERATION_ORDER]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New output directory outside the repository")
    args = parser.parse_args()
    output = args.output.expanduser().resolve()
    if output == REPO or REPO in output.parents:
        parser.error("Output must be outside the repository; committed examples and fixtures are immutable inputs")
    if output.exists():
        parser.error("Output directory already exists; choose a new directory")
    if not shutil.which("latexmk") or not shutil.which("pdflatex"):
        parser.error("Real TeX (latexmk and pdflatex) is required; no placeholder PDF will be generated")
    fixture = REPO / "fixtures/sample_project"
    template = REPO / "skills/latex-writing/templates/default"
    fixture_before, template_before = dir_manifest(fixture, []), dir_manifest(template, [])
    output.mkdir(parents=True)
    # All mutable project state, caches and optional registry use stay in this output.
    os.environ["AIR_HOME"] = str(output / "air-home")
    os.environ["AIR_REPO"] = str(REPO)
    os.environ["MPLCONFIGDIR"] = str(output / "cache/matplotlib")
    os.environ["XDG_CACHE_HOME"] = str(output / "cache")
    root = output / "project"
    shutil.copytree(fixture, root)
    project = Project.open(root)
    (root / "paper").rename(root / "fixture-paper")
    bundle = build_bundle(root)
    llm = fake_gateway(project, {PROMPT_ID: sample_responses(bundle)})
    writer = Writer(root, bundle, llm=llm, language="English")
    result = writer.generate_draft(title=TITLE, compile_enabled=True)
    report = result.compile_report
    if (not report.ok or report.errors or report.undefined_citations or report.undefined_refs or report.missing_files):
        raise RuntimeError(f"Real compilation did not succeed: {report.model_dump_json()}")
    if len(llm.backend.calls) != 8 or tuple(s.output.section for s in result.sections) != GENERATION_ORDER:
        raise RuntimeError("Expected eight actual gateway calls in the Writer generation order")
    paper = root / "paper"
    if (paper / "claims.jsonl").exists() or (paper / "air_values.tex").exists():
        raise RuntimeError("The control-arm example must not generate evidence binding machinery")
    # Distribute only these selected, byte-identical inputs/results, never the project.
    selected = output / "example"
    selected.mkdir()
    copies = {"main.tex": result.main, "references.bib": result.references,
              "main.pdf": paper / "build/main.pdf", "compile_report.json": paper / "build/compile_report.json"}
    copies.update({f"sections/{name}.tex": paper / f"sections/{name}.tex" for name in SECTIONS})
    for relative, source in copies.items():
        destination = selected / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        if sha256(source) != sha256(destination):
            raise RuntimeError(f"Copied input/result differs: {relative}")
    if fixture_before != dir_manifest(fixture, []) or template_before != dir_manifest(template, []):
        raise RuntimeError("Canonical fixture or original template changed")
    metadata = {
        "code_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
        "tracked_changes": subprocess.check_output(
            ["git", "diff", "--name-only", "HEAD"], cwd=REPO, text=True).splitlines(),
        "implementation_sha256": {name: sha256(REPO / name) for name in [
            "airesearcher/stages/writing/writer.py", "airesearcher/stages/writing/_latex.py",
            "examples/writing/phase-2a-4-writer/reproduce.py"]},
        "bundle_sha256": hashlib.sha256(bundle.to_bytes()).hexdigest(),
        "idea_ref": bundle.idea_ref.model_dump(), "plan_ref": bundle.plan_ref.model_dump(),
        "aggregates": [a.ref.model_dump() for a in bundle.aggregates],
        "logs": [log.ref.model_dump() for log in bundle.logs],
        "literature_source": bundle.literature_source.model_dump(),
        "generation_order": list(GENERATION_ORDER), "language": "English", "title": TITLE,
        "template": "latex-writing@0.1.0/templates/default", "backend": type(llm.backend).__name__,
        "gateway_calls": len(llm.backend.calls),
        "citation_keys": sorted({key for section in result.sections for key in section.citations}),
        "pages": report.pages, "pdf_bytes": copies["main.pdf"].stat().st_size,
        "files_sha256": {name: sha256(path) for name, path in sorted(copies.items())},
        "fixture_unchanged": True, "original_template_unchanged": True,
        "scientific_validation": False,
    }
    (selected / "generation.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(f"Real PDF and selected materials: {selected}")
    print(f"Pages: {report.pages}; PDF bytes: {metadata['pdf_bytes']}; SHA-256: {sha256(copies['main.pdf'])}")


if __name__ == "__main__":
    main()
