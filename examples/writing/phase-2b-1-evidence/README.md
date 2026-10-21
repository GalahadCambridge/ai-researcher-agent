# Phase 2B-1: Evidence Binding + Deterministic Verification

This example is a real `WritingStage` run with `evidence_check=true`, a scripted
`SampleWritingLLM` (zero paid/network model calls), and actual `latexmk` compilation.
`main.pdf` contains the two bound Results observations, six code-generated numeric
values, both deterministic figures, citations and the code-generated AI-use statement.
The deterministic report records two supported claims and zero blocker/major/minor
issues. This is a synthetic functionality example, not scientific validation.
Semantic LLM review, revision and evaluation are deferred.

From the repository root, using the existing environment:

```sh
AIR_REAL_LLM=0 .venv/bin/python examples/writing/phase-2b-1-evidence/reproduce.py --output /tmp/my-new-2b1
.venv/bin/air dev check /tmp/my-new-2b1/project --owner 论文
```

Choose a **new directory outside the repository**. TeX must already be installed;
the script does not install it, mock compilation or substitute a placeholder PDF.
Use `--control` with another fresh output directory to exercise the false arm. It
produces an actual PDF without claims, value macros or deterministic review files.

The script copies public sample inputs into the new workspace, preserves the old
fixture paper/figures separately and uses the real Engine/Stage/gateway. It verifies
PDF text against bound values and checks the actual compile/report references. The
sample fixture has existing archive history, so new artifacts need not start at v1.
All mutable inputs, caches, run state, receipts and logs stay in that temporary tree.
No canonical fixture or historical example is overwritten.

Retained files:

- `main.pdf`, `main.tex`, eight `sections/*.tex`, `references.bib`, `ai_use.tex`;
- `claims.jsonl`, `air_values.tex`, frozen `evidence_bundle.json`;
- actual `compile_report.json`, `review/review_v1.json` and `.md`;
- both manuscript figure PDFs and `figure_artifacts/*` metadata, script, CSV, PNG, PDF;
- `generation.json` with exact input/output refs, non-cyclic archive parents, call counts,
  hashes, configuration, tool version and the implementation state used for generation;
- this reproduction script and documentation.

`generation.json.code_head` identifies the pre-commit base; implementation hashes and
recorded dirty/untracked paths identify the actual generated code. Timestamps, paths,
PDF metadata and archive hashes vary in a new run. Reproduction checks behavior and
source identity, not byte equality between separately timed runs. The real compile
report keeps its original timing and source-specific temporary log path; build logs,
full archives, raw runs, LLM logs and internal recovery receipts are not distributed.

The sample explicitly declares a dimensionless metric scale of 1. Difference values
use four decimals, p-values four decimals and seed counts integers. Neither the model
nor the example treats scores as percentages. Claims and their values are inserted by
code; the prose model receives markers rather than raw Aggregate result tables.

Provenance flows from approved inputs to bundle/Aggregates/Figures to claims to paper;
review depends on paper + claims, and compile/PDF depend on paper. No paper→claims→paper
cycle is introduced. The approval remains pending for a human and does not authorize
publication. The two earlier retained example directories are unchanged.
