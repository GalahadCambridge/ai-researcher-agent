# Phase 2A-5: real control-arm WritingStage

This example runs **ProjectEngine -> real WritingStage -> ManuscriptPending** with
`evidence_check=false` and the existing FakeLLM/LLMGateway interfaces. It uses the
public sample's exact approved idea/plan, C1/C2 observations and frozen run environment
summary. All manuscript prose is explicitly scripted simulation, not real-model
scientific output. Figures are actual deterministic scientific-plotting outputs and
the PDF is compiled by the production restricted latexmk path.

From the repository root, using the existing project environment:

```bash
AIR_REAL_LLM=0 .venv/bin/python examples/writing/phase-2a-5-stage/reproduce.py --output /new/unused/output
```

On Windows use the equivalent `.venv\Scripts\python.exe` command and a fresh output
directory outside the checkout. The script requires existing latexmk; it installs
nothing and makes no paid/model or online citation requests. The new output contains
the full disposable `project/` and the curated `example/` files. Old sample paper and
figures are preserved in that temporary project. Canonical fixtures and the previous
Phase 2A-4 example remain immutable.

For honest no-TeX development, run:

```bash
AIR_REAL_LLM=0 .venv/bin/python -m scripts.writing.demo --workspace /another/new/project --no-compile
```

That route submits sources with a disabled/unsuccessful CompileReport and no PDF;
it is distinct from this retained real-TeX artifact.

The retained files are byte-identical compilation inputs/results: `main.tex`, eight
sections, `references.bib`, deterministic `ai_use.tex`, v2 `evidence_bundle.json`,
provisioned figure PDFs, the actual `main.pdf` and `compile_report.json`. Each
`figure_artifacts/fig_c*/` also retains the exact spec, data, renderer, metadata, PDF
and PNG. `generation.json` records source/code hashes, exact refs, actual call counts,
configuration, approval, page count and PDF size. Its baseline SHA plus implementation
hashes/tracked/untracked changes identify the working code used to generate this
pre-commit example; it does not invent a future commit SHA.

The report's `log_path` points to the real temporary project's source-specific log;
that log, build intermediates, entire archives, model logs and internal receipts are
not distributed. Regeneration recreates them. Byte-identical source inputs can be
verified from recorded hashes; runtime timestamps/refs in freshly generated figure
metadata and approval records may differ across runs.

FakeLLM proves orchestration, restart interfaces, prompts, schemas, artifacts,
compilation and approval handoff. It does **not** prove scientific prose quality,
semantic citation correctness or real-model discipline. Evidence checking is disabled;
no claims.jsonl, air_values.tex or review report is created, and approval grants no
publication permission. Phase 2B remains unimplemented.
