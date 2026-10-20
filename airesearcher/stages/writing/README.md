# Independent Writer (Phase 2A-4)

`Writer(root, bundle, llm=ctx.llm, skills=ctx.skills)` is intended for later Stage use;
it does not import StageContext or implement a Stage. The only factual input is a
detached EvidenceBundle. The implementation is the future `evidence_check=false`
arm: direct numeric prose is allowed, and no scientific review/binding is performed.

The frozen section vocabulary is abstract, introduction, related_work, method, setup,
results, discussion, conclusion. Discussion includes a Limitations subsection, as in
the authoritative design. Generation follows that order with abstract last. Paper
display order starts with the abstract. The model supplies localized section headings;
abstract output contains only abstract text. `language=None` follows the approved
research question; an explicit caller language is supported without a config change.

## Calls and files

- `build_section_context(bundle, section, language=None, figures=None)` is pure. It
  projects exact approved content, preserves observation/contrast/run alignment,
  includes unfavorable evidence, diagnostics and offline/access limitations. Results
  also receives approved logs so recorded hypothesis failures remain visible. Summary
  sections omit raw seed values; method/setup use approved plan material. No current
  idea/plan/run/log reads, statistics recomputation or model-generated summaries.
- `Writer.generate_section(section, prompt_version=None)` calls `llm.complete` once
  with role `writing.writer`, tier `strong`, prompt `writing/section.v1.md` and the
  local SectionOutput schema. Omitting a prompt version lets gateway pins apply.
  Gateway schema retries/accounting remain gateway-owned. Structural rejection does
  not start a Writer retry loop. Verified skill instructions are explicitly supplied.
- `validate_section(output, expected_section)` checks identity, nonempty UTF-8 body,
  command/environment allowlists, brace balance, citations and optional figure paths.
  It reuses the existing scanner, so comments and literals are inert. Unsupported
  macros, I/O, document wrappers, input/include and TeX character-code escapes fail.
  Generated literal examples are limited to verb/verb* and verbatim/verbatim*;
  listings escape options and minted external processing are not accepted.
  This is a conservative fragment grammar, not a complete TeX sandbox or reviewer.
- `write_section(output)` validates before an atomic UTF-8 write to a fixed filename.
  `read_section(section)` returns a validated existing section or None. It does not
  establish factual freshness. GeneratedSection returns context SHA-256, skill ref,
  prompt/version and gateway call ID for later explicit Stage checkpoints. No hidden
  reuse or global mutable state exists.
- `generate_references()` reads validated section files, gathers only active cite
  keys, checks the live literature SHA-256 against the bundle, copies checked bytes
  to a private temporary service workspace, and uses official resolve_citation_key
  and bibtex_for there. This prevents mixed snapshots even during transient edits.
  The live hash is checked again before replacing references.bib. Missing, ambiguous,
  unresolved identities or a BibTeX entry-set mismatch fail without replacing it.
- `assemble_draft(template=None, entry=None, title=None)` receives the explicit
  ProjectConfig.template selection. It uses resolve/inspect/prepare_template in a new
  temporary directory, then copies prepared resources into existing paper/. Conflicting
  resources fail; model section files are not overwritten by template assets. main.tex
  is written last. All sections must already validate. This is not a multi-file crash
  transaction; valid sections remain inspectable on failure.
- `generate_draft(..., compile_enabled=False)` is a small standalone integration
  helper: generate/write sections, assemble, call existing compile_paper. Disabled
  compilation returns the existing explicit failure/disabled report. Real compile
  errors are returned in CompileReport and never repaired or replaced by a fake PDF.

## Boundaries

Any bundle provenance diagnostic blocks generation/writes. Pure context projection
still exposes the exact diagnostics/refs for inspection. No historical evidence is
relabelled or reconciled. This policy does not fix the deferred bundle archive gap.

Citation status is local_offline only. Verified metadata is not online/scientific or
full-text verification. All literature access/status information stays visible; only
known records with verified/metadata_only status and BibTeX may be cited. Fulltext
paths are not treated as supplied fulltext contents.

Optional figure inventory is `fig_id -> figures/<fig_id>.pdf`. The caller supplies
known assets in paper/figures/; Writer does not select, copy or archive figures.
Active ref/pageref/label calls, including those immediately after a line break,
require a direct braced literal label; fig: labels must belong to that inventory.
Starred, optional, unbraced and macro-expanded reference arguments require explicit
adaptation. Comments and supported literals do not participate in these checks.

Default and simple uploaded BibTeX templates are supported. Existing styles are kept;
the bibliography database must be references. Biblatex/manual bibliographies and
templates with reserved section/build/claim/value resources require explicit adaptation.
Active printbibliography/addbibresource commands and directly declared biblatex in
usepackage/RequirePackage lists are rejected before any bibliography or template
output is replaced. This does not discover arbitrary macro expansion or indirect
dependencies hidden in custom packages; it does not implement biblatex/biber.
No silent template fallback occurs. An existing paper tree is preserved, not cleaned;
the runtime harness moves the sample's old paper aside before generating a fresh one.

There is no registered paper-language setting in the current model. The caller must
choose a TeX-compatible template/font environment for the requested language; Writer
does not silently translate unsupported titles or scientific content. Runtime FakeLLM
examples explicitly request English and supply an ASCII test title.

AI-use disclosure requires actual runtime/approval history and is deferred. main.tex
has an AIR:AI_USE comment hook, not a fabricated production disclosure. No claims,
value macros, evidence review, Stage orchestration, approvals or archive registration
are implemented. FakeLLM/PDF success proves integration, not prose quality or evidence
correctness. Phase 2A-5 must own lifecycle, permissions, stale/recovery and checkpoints.

## Retained functional examples

See [examples/writing](../../../examples/writing/README.md) for the stage index and
the real compiled Phase 2A-4 PDF with source, report and standalone reproduction.
Each completed Writing feature phase must add its own example subdirectory and
update that index. Preserve historical examples; do not overwrite an earlier
phase's output. These are demonstration materials, not production configuration
or a new orchestration mechanism.
