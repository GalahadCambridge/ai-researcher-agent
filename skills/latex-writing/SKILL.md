# LaTeX template infrastructure (0.1.0)

This version supplies deterministic infrastructure only. It does not generate prose,
sections, claims, value macros, citations, reviews, or approval transitions.

- Resolve `ProjectConfig.template` with `resolve_template(root, config.template)`.
  A null selection uses `templates/default/` in this verified skill. Uploaded versions
  use the backbone's `templates/<id>/v<N>/original/`; never edit that directory.
- `inspect_template` detects top-level documentclass entries and dependencies.
  Zero entries fail; multiple entries require explicit user selection. Never choose
  the first candidate or silently substitute the default. Inspection is conservative:
  macro-generated paths/entry declarations require template adaptation by the user.
- `check_template` compiles an unchanged disposable copy. `write_check` optionally
  persists adjacent check.json and a separate frozen CompileReport. Default metadata
  uses paper/.template_check_default.json and paper/.template_compile_default.json.
  Check JSON has stable ordering and no timing; report seconds remain truthful.
  Sanitized compile diagnostics are retained beside that report as a .log file.
- `prepare_template` requires a new destination directory. It copies local files/assets,
  normalizes the chosen entry to main.tex, and inserts already supplied body content.
  Later Stage code must decide how to replace a previous paper directory.
- A standalone `%% AIR:BODY` line has priority. Otherwise retain the preamble and
  document start through maketitle or titlepage, replace the sample body, and retain
  the bibliography tail. Refuse unsafe nested boundaries. Use an explicit marker for
  complex layouts. Optional title/author are escaped text; optional preamble is a
  structural injection hook, not generated evidence macros.
- Dependencies include class, package, bibliography style/database, literal inputs and
  graphics. Local files take precedence; otherwise use kpsewhich without a shell.
  No package installation/network calls. Missing dependencies are explicit. Without
  kpsewhich, system availability is unresolved, not falsely labeled missing; structural
  preparation remains possible while actual template checks cannot pass.
- `compile_paper(paper, enabled=config.dev.compile)` runs latexmk into build/ with
  a 180-second timeout, no shell escape, no rc files, and openin_any/openout_any=p.
  Failure/disabled/unavailable states return ok=false and never create placeholder PDFs.
  A failed attempt removes its PDF; undefined citations/refs remain reported warnings
  when TeX successfully emits a PDF. Overfull boxes count only above 10pt.
- Parse the final TeX log, so resolved first-pass references do not linger. Native logs
  and latexmk stdout remain under build/. compile_report.json uses the shared model.
- Symlinks and traversing/nonliteral resource paths are rejected. Do not execute scripts
  shipped with templates. These restrictions reduce risk; they are not an OS sandbox
  for arbitrary hostile TeX. TeX distributions can read their installed resources, and
  full hostile-input isolation/resource quotas belong to the execution environment.
- Compilation uses fixed SOURCE_DATE_EPOCH/FORCE_SOURCE_DATE/TZ and forces a fresh run.
  PDF bytes can still vary with TeX/fonts/tool versions. Check metadata is deterministic;
  compilation timing and diagnostics report actual execution facts.

Skill samples always verify structure/filling/immutability. If latexmk and kpsewhich
are installed they also require a real successful compile. On systems without TeX,
only the runtime compile portion is skipped; install TeX separately for runtime use.
Later writing must use approved evidence, allowed citation keys, and real figure paths;
only abstract-access literature cannot justify experimental details. Writer and evidence
enforcement are deferred to later milestones.
