# Phase 2A-4: independent Writer, after review fixes

[View the actual compiled PDF](main.pdf). This public sample demonstrates eight
Writer section calls through the real LLMGateway with **FakeLLM**, official local
literature services, the existing default template, and real `compile_paper`.
It proves interface/build integration, **not real-model quality or scientific
correctness**. No claims, air_values, scientific review or WritingStage is generated.

## Generation state and sources

Generated from baseline commit `01deac12b4a52203499a81aeba09fa6d2d20b1ea`
plus the uncommitted Phase 2A-4 review fixes and this new reproduction script.
The final commit did not yet exist when this PDF was compiled. This statement is
retained rather than substituting a future commit. [generation.json](generation.json)
records that baseline, tracked changes at generation, exact implementation/script
hashes, input references and hashes of the distributed compilation inputs/results.
New untracked example files are not included in its `tracked_changes` list.

The script copies `fixtures/sample_project` into the caller's new output workspace,
moves the copied old paper aside, and uses the real `build_bundle`. All reported
numbers are read from the sample aggregate rows/contrasts in the bundle; no scientific
conclusions are hardcoded in production code. Source references:

| Source | Version |
| --- | --- |
| `idea/selected.md` | 2 |
| `plan/experiment_plan.md` | 1 |
| `artifacts/aggregates/C1.json` | 1 |
| `artifacts/aggregates/C2.json` | 1 |
| `artifacts/aggregates/all.json` | 1 |
| `logs/round_1.md` | 1 |
| `idea/literature.jsonl` | 1, archived, `local_offline` |

The bundle SHA-256 is
`e5bac43ece06bab84b2ecbf338d0a2cb001752f843bd93f96cfe017a9193be4b`.
Full source reference SHA-256 values are in `generation.json`. Its three literature
records have abstract-only access; only the two cited keys `devlin2019bert` and
`hu2022lora` are exported to [references.bib](references.bib) by official services.
No online citation verification or full-text scientific validation is implied.

The explicit example language is English, with title **Sample Project: Writer
Integration**. The verified template is `latex-writing@0.1.0/templates/default`.
Abstract appears first in the PDF and is generated last. Discussion includes the
Limitations subsection; the frozen eight-section vocabulary is unchanged.

## Reproduce

Use the repository's existing editable Python environment with its existing
dependencies and TeX installed. No API key, real model, downloads or automatic
installation are used. Starting inside the checkout, the following invocation
also demonstrates that the script works after changing the working directory:

```sh
REPO="$(git rev-parse --show-toplevel)"
OUT="$(mktemp -d)/writer-run"
cd /
"$REPO/.venv/bin/python" \
  "$REPO/examples/writing/phase-2a-4-writer/reproduce.py" --output "$OUT"
```

`--output` is required and must name a new directory outside the checkout. Existing
directories are rejected. Missing latexmk/pdflatex is an explicit failure, with no
placeholder PDF. From any other working directory, invoke the script by its absolute
path using the same installed environment. The initial generation was also executed
from outside the checkout. Runtime products stay under `$OUT`: the mutable project,
copied fixture/archive, gateway logs, caches, complete TeX build and selected results
in `$OUT/example`. Only selected results are distributed here. This script does not
import pytest or a test module and adds no production configuration or Stage.

Generation environment: Python **3.12.14**, latexmk **4.86** (11 Dec 2024), pdfTeX
**3.141592653-2.6-1.40.26**, TeX Live **2025/dev/Debian**, kpathsea **6.4.0/dev**,
Linux. Other environments may produce different PDF bytes or runtime timing.

## Actual output and validation

- PDF: **2 pages**, **97,164 bytes**.
- SHA-256: `2b8667ecebbb53b834770ca503017287d86fa7eb4bf21a2534dac508d875c6b0`.
- [compile_report.json](compile_report.json) is copied byte-for-byte from the real
  production compiler report. It records `ok: true`, engine `latexmk -pdf`, 2 pages,
  **0.52 seconds**, zero overfull boxes, and empty errors, undefined citations,
  undefined references and missing files. Its real timing has not been edited.
- `log_path: paper/build/main.log` refers to the reproduction workspace's log.
  That log is not distributed and is not an existing file in this example directory.
- `pdfinfo`, `pdftotext -layout` and rendered-page inspection confirmed a readable
  two-page paper with its title, Abstract, Introduction, Related Work, Method,
  Experimental Setup, Results, Discussion, Conclusion, Limitations and references.
  The extracted sample means include `0.779900`, `0.694233` and `0.737100`.
  No obvious clipping, overlap, anomalous blank page or missing glyph was observed.
- Every distributed TeX input, bibliography, PDF and report was copied unchanged
  from the same successful build. `generation.json` records their byte hashes.
  Canonical fixtures and the original skill template were verified unchanged.

The PDF itself was never edited. No raw runs, archive, .llm directory, build tree,
aux/fdb files, machine configuration, secrets or private material are committed.
Future Writing phases should add separate sibling directories and update the
[Writing examples index](../README.md), preserving this historical example.
