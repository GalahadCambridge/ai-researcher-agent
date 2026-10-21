# Writing: independent Writer and real control-arm Stage

`Writer(root, bundle, llm=ctx.llm, skills=ctx.skills)` is used by the real Stage below;
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
  with role `writing.writer`, tier `strong`, prompt `writing/section.v2.md` and the
  local SectionOutput schema. The current default is v2; v1 remains pinnable. Omitting a prompt version lets gateway pins apply.
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
relabelled or reconciled. The historical bundle closure gap was fixed in Phase 2A-4.1.

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

Standalone Writer leaves an AIR:AI_USE comment hook unless ai_use=True requests the
Stage-owned source. The real Stage described below owns lifecycle, disclosure,
archive registration and approval; Writer itself remains independent. Neither
component implements claims, value macros or evidence review in this control arm.

## Retained functional examples

See [examples/writing](../../../examples/writing/README.md) for the stage index and
the real compiled Phase 2A-4 PDF with source, report and standalone reproduction.
Each completed Writing feature phase must add its own example subdirectory and
update that index. Preserve historical examples; do not overwrite an earlier
phase's output. These are demonstration materials, not production configuration
or a new orchestration mechanism.

## Real WritingStage (Phase 2A-5)

Select `dev.stage_impl.writing: real` and `evidence_check: false`. The stage registry
loads `stage.py:create_stage()` without registry changes. `evidence_check: true`
returns a non-retryable Stage error before any output/model call; Phase 2B is not
implemented. There are no claims, evidence macros, semantic review or audit outputs.
The old independent Writer API remains available.

The engine calls these bounded phases through `StageContext` / `StepResult`:

```
bundle -> figures -> template -> write -> compile -> submit -> NeedsApproval(manuscript)
                                  ^          |
                                  |     compile_fix (bounded by paper.max_compile_fixes)
                                  |          |
                                  +--- manual retry / template choice
```

Each write step generates at most one section, using Writer's `GENERATION_ORDER`
(abstract last). Figure planning, generation, template validation and compilation
also finish at recoverable boundaries. The shared engine owns transitions,
checkpoints, questions, approval records and completion; production Writing code
never edits engine checkpoints. Six principal progress steps use `ctx.progress`.

### Factual inputs and schema compatibility

The exact `ctx.approved['idea']` and `['plan']` are required. For each comparison in
the approved plan, select the greatest archived canonical Aggregate version whose
`plan_ref` equals that approved ref. Historical versions remain unchanged; unrelated
`all` summaries are not duplicated into the manuscript. A missing/empty required
comparison stops explicitly. Exploratory conditions, negative/non-significant
contrasts and exclusion annotations are retained.

`build_bundle(..., include_runtime=True)` deliberately produces Writing-owned schema
**v2**. Its frozen runtime input includes:

- validated TaskConfig selected by the **approved plan's** `task_config`, resolved
  from project-local resources first, then the repository (directory/task.yaml or
  explicit relative YAML path); a portable projection and its SHA-256;
- only run IDs in the selected Aggregate observation rows, loaded exclusively through
  `services.runs.load_run(..., with_metrics=False)`;
- run identity, experiment/kind/condition, exact plan/idea refs, commit/config hashes,
  seed/executor, status/label, resource totals and a restricted environment summary;
- a deterministic SHA-256 over the frozen task/run input. Live changes to that
  projection invalidate the Stage basis before further generation/submission.

Environment data uses an allowlist (OS, Python, hardware, frameworks, recorded
unknowns and three reproducibility environment variables). Command/workdir/host,
arbitrary environment variables and credential-like keys are omitted; absolute
paths become `<external-path>`. Omitted credentials and machine paths are not paper
facts. This is a snapshot of service-visible metadata, not a new verification of
run results. Task/run snapshots are embedded in the archived bundle; no shared
Run/Task/core model or runs service changes were necessary.

Default `build_bundle()` remains v1, and historical v1 bytes round-trip unchanged.
A v1 bundle never claims to contain runtime context; v2 requires it. Writer can read
both. Method and Setup receive v2 runtime fields from the detached bundle, never
from mutable run files. The existing historical Aggregate -> Plan -> Idea closure
and bundle parent/idempotency checks still apply. Committed historical examples are
not migrated or rewritten.

### Figures and templates

One figure per selected comparison uses the existing scientific-plotting renderer,
validator and Archive registration with its exact Aggregate parent. Explicit optional
TaskConfig metric `unit` metadata permits deterministic `default_spec`; otherwise
`writing/figure_spec.v1`, role `writing.figure_spec`, strong tier, returns a strict
FigureSpec with no additional values/code fields. The planner may choose presentation
only. Unknown units must remain unknown and fail validation, not be invented.
Ambiguous grouping also needs explicit supported presentation. No plotting Python
or measurement is accepted from the model.

Matching Aggregate ref, spec, verified skill ref, renderer hash and current artifact
hash allow reuse. Provisioned `paper/figures/<fig_id>.pdf` is byte-identical to the
validated artifact; Writer sees only this fixed inventory. The English FakeLLM demo
explicitly declares the synthetic smoke score dimensionless and uses English figure
titles; production code contains no domain-specific unit inference.

Template resolution/inspection/minimal compilation uses the existing immutable
module. Multiple entries ask for a filename with no default. Missing dependencies,
invalid inspection or failed minimal compilation ask `uploaded_fix/use_default/end`.
Only `use_default` authorizes fallback, recorded for the AI-use statement. A new
upload is reread from project configuration. `end` is handled by the existing engine.
In compile-disabled development, structural inspection is retained and minimal
compilation is explicitly skipped. Template-only changes preserve current prose;
only unchanged, previously tracked template resources may be replaced. Edited
resource collisions stop for explicit adaptation.

### Receipts, feedback and edits

`Writer.section_context()` and `section_fingerprint()` share exactly the generation
projection. The fingerprint includes full bundle identity, context, figure inventory,
verified skill instructions/ref, prompt content/version and user guidance. Stage
adds exact Figure refs and relevant writing configuration. Template identity is
separate so a template change does not invalidate unrelated prose.

Fixed `paper/.writing/section-<name>.json` files record validated output, fingerprint,
call/prompt metadata and installed content hash. Output is durable before installation;
recovery replays installation without another call. Figure-spec receipts and a small
runtime receipt retain question answers, feedback IDs, compile reports/attempts and
assembly identity. These files are internal and excluded from the manuscript archive.
The engine scratch remains the phase machine; Writing does not implement a generic
checkpoint framework. A crash before a completed response is durably stored cannot
prove success; repair attempts are recorded before calling and consume their bounded
budget if interrupted.

Actual manuscript `changes_requested` / rejected-then-redo feedback is consumed once
by approval ID, retained as user writing guidance and triggers section regeneration.
It cannot replace bundle facts, citation/figure allowlists or structural checks. Factual
basis changes rebuild inputs and invalidate old prose. Human-edited sections with
matching factual context are validated, preserved and disclosed, and their citations
regenerate the official BibTeX before compilation. If both human text and its factual
basis changed, stop for explicit reconciliation rather than silently overwrite it.
Existing unreceipted sections also require explicit reconciliation. A human-edited
main.tex is preserved; the user owns its template/assembly correctness.

### AI-use, compilation and approval

Code generates `paper/ai_use.tex`; the model does not author it. It reports actual
recorded prompts/roles/backend, compile-repair attempts, feedback/approval history,
preserved human edits and explicit fallback. It states that evidence checking is
disabled and compilation/offline citations do not establish scientific correctness.
`Writer.assemble_draft(ai_use=True)` fills its fixed hook. Deterministic invisible PDF
metadata identifies source content so even a comment-only source revision yields a
PDF tied to the correct manuscript parent under Archive's content deduplication.

`compile_paper` retains its security restrictions and real CompileReport. Errors and
warnings are logged via `ctx.log`. Disabled compilation returns `ok=false`,
`engine=disabled`, and no PDF; source-only development approval explicitly says so.
A source-specific copy of the actual compile log gives the separately archived
CompileReport an unambiguous source identity, including identical disabled builds.

On a real compilation failure, only an implicated **generated** section is eligible
for repair. `writing/compile_repair.v1`, role `writing.compile_repair`, strong tier,
receives that fragment, its errors, skill rules and citation/figure allowlists. It
returns strict SectionOutput, has no path field and passes Writer validation before
replacement. At most `paper.max_compile_fixes` attempts (default 2), each with gateway
schema retries disabled, are allowed per basis/writing/template cycle. Invalid repairs
preserve prior source. Template-level errors do not authorize section edits. Exhaustion
asks `retry/end`; retry validates/recompiles current manual edits before any further
model work and does not reset the automatic attempt budget.

The approval target is the complete `paper/` directory, including bundle, sections,
references, figures and AI-use source, excluding build/review and internal receipt or
template-check files. Exact bundle/Aggregate/Figure refs are provenance parents. PDF
and CompileReport are separately archived with that source ref as parent. Recompilation
without source changes retains the source version. Approval marks completion, not
permission to publish. The summary reports figures/pages/compile state and the control
arm; it invents no claim support counts. Reopen an approved project through the existing
engine action to process changed factual inputs.

### Zero-cost execution

From the repository with its existing Python environment:

```bash
AIR_REAL_LLM=0 python -m scripts.writing.demo --workspace /new/disposable/project
# No TeX / CI orchestration (no PDF, explicit disabled CompileReport):
AIR_REAL_LLM=0 python -m scripts.writing.demo --workspace /another/new/project --no-compile
```

The harness copies the canonical sample, preserves its old fake paper/figures in
separate directories, configures the real false arm, initializes the documented
Writing entry checkpoint and drives **ProjectEngine**, never Writer.generate_draft.
No real-model key, model dependency or online citation request is required. FakeLLM
proves orchestration, recovery, prompts, schemas, artifacts, compilation and approval
lifecycle; it does not prove prose quality, semantic citation correctness or real-model
discipline. The new retained example is in `examples/writing/phase-2a-5-stage/`.
