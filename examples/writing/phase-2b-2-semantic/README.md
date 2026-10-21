# Phase 2B-2: semantic review and targeted revision

[main.pdf](main.pdf) is the actual final three-page PDF from the real WritingStage,
LLMGateway with SampleWritingLLM, and production latexmk compilation. The synthetic
sample intentionally starts with an overbroad sentence in CL1. Review round one
reports OVERCLAIM_SCOPE (major); a targeted revision limits the observation to the
evaluated scope; real recompilation, deterministic recheck and semantic rereview
produce round two with two supported Claims and zero blocker/major/minor issues.
The final state is ManuscriptPending, approval ap-0007. No approval was granted.

- [before/results.tex](before/results.tex) and [before/claims.jsonl](before/claims.jsonl)
  preserve the initial wording. These are explicitly flawed synthetic test inputs.
- [review/review_v1.md](review/review_v1.md) and [review/review_v2.md](review/review_v2.md)
  record the exact historical paper/claims refs, combined findings and completed rounds.
- [generation.json](generation.json) records implementation/artifact hashes, calls,
  both actually compiled manuscript refs and their exact archived PDF/report refs.
- Final source is at this directory's root, with sections and Figure PDFs. Numeric
  values, Claim IDs, Comparison, ValueRefs and EvidenceLinks survive revision unchanged.

The code baseline is 57ec9d8317bcb57784fa80b382a9a6b989b709ff plus the Phase 2B-2
implementation recorded by file hashes. Inputs are a disposable copy of the public
sample fixture. Neither canonical fixtures nor earlier examples were rewritten.
There were two Figure planner calls, one Claim planner, eight section calls,
twelve sentence-review calls (six units per round), one reviser and no compile repair.
These are FakeLLM engineering checks, not real-model validation or Phase 3 evaluation.

## Inspect or rerun manually

Use the repository's existing Python environment, from the commit containing this
example. Choose a new directory outside the repository. TeX must already be installed;
these commands do not install dependencies or request model credentials.

```bash
AIR_REAL_LLM=0 MPLCONFIGDIR=/tmp/air-mpl XDG_CACHE_HOME=/tmp/air-cache \
  python -m scripts.writing.demo --workspace /new/disposable/project \
  --evidence-check --semantic-revision
air dev check /new/disposable/project --owner 论文
```

For the unchanged control path, omit both flags and choose another fresh directory.
For explicitly source-only engineering use `--no-compile`; that mode produces no PDF.
This is the existing sample harness, not a new reproduction or real-model runner.

To compile these retained sources, first copy this directory to a private temporary
location, then call `airesearcher.stages.writing.compile.compile_paper` on that copy.
Never compile into this checked-in directory. PDF bytes can vary with TeX/font versions.
The stored CompileReport preserves actual timing and the workspace-relative log path;
the referenced build log and internal receipts are not distributed with this example.

## Validation limits

Both original and revised manuscripts compiled successfully with real latexmk.
The final report has no errors, undefined citations/references or missing files.
It retains one existing overfull related-work box. PDF text inspection confirms
0.0857, 0.0017, 0.0428 and 0.0343, the revised scope sentence, and removal of the
overclaim. The final PDF is 116208 bytes.

Writing dev checks pass (two Figure checks, fourteen paper/Claim checks). The global
dev check also reports three pre-existing event sequence duplicates (102, 104, 105).
The same failures were reproduced with the unmodified 57ec9d8 baseline; this phase
leaves the shared event system unchanged. The false arm has the same global issue,
with its two Figure and ten paper checks passing.

Raw-run historical recomputation still pins supporting run IDs but rereads current
raw metrics. Semantic quality has only been exercised using scripted responses;
real-model smoke testing remains a separate next task.
