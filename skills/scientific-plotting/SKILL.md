# Scientific plotting

Use `airesearcher.stages.writing.figures.generate_figure(root, aggregate_id, spec=None, version=None)`
to load an archived Aggregate through `services.runs`, render it, validate the complete
artifact, and register its Aggregate parent. `default_spec(aggregate)` selects numeric-x
lines (with one categorical hue), or categorical bars. Set `distribution=True` for
actual per-run points. Explicit FigureSpec also supports boxes from actual observations.

- Statistical values must come from Aggregate rows. Never invent, interpolate, smooth,
  or reconstruct seed values from summary statistics. No LLM, image generation, or network.
- Use the frozen FigureSpec and Figure models. Do not assume percentages or accuracy.
- Specify units in `ylabel`, e.g. `Latency (ms)`, or explicitly say `dimensionless` / `无量纲`.
- Positive finite `scale` multiplies means, standard deviations, CI bounds and observations.
  `data.csv` retains unscaled source columns and explicit `display_*` scaled columns.
- For n >= 2, uncertainty is mandatory, including point/box plots: choose `ci95` or `std`.
  A box's IQR is not a substitute for uncertainty of the mean. Boxes additionally show
  actual observations and the supplied mean/error bars. For n=1 use `none` explicitly
  when uncertainty is unavailable; never substitute zero for a missing CI/std.
- Caption must state actual n (e.g. `n=3` or `over 3 seeds`) and define the error bars
  (`95% CI` / `95% confidence interval` / `95%置信区间`, or `SD` / `standard deviation` / `标准差`).
  For heterogeneous counts bind each n to its group, e.g. `n=2 (A), n=3 (B)` or
  `A: n=2; B: n=3`. Short labels must identify a unique complete group; use the
  default `n=2 ({"method":"A","size":10})` form for ambiguous/multidimensional
  groups. Once groups are explicitly named, cover every group, even for equal n.
  A common-n shorthand (`n=3`, `over 3 seeds`, `每组 n=3`) remains valid when all
  groups genuinely have the same n, including a single group.
- Exploratory Aggregate results require `exploratory` or `探索性` in title/caption.
- Numeric x is sorted numerically. Lines need numeric x; log x additionally requires
  positive values and is not supported on categorical bars. Unplotted varying grouping
  dimensions are rejected rather than silently collapsed or pooled.
- Each observation maps to an actual run ID; duplicates and inconsistent counts fail.
- Files: `spec.json`, `data.csv`, `plot.py`, `figure.json`, `<fig_id>.pdf`, `<fig_id>.png`.
  Figure records the exact Aggregate VersionRef, included run IDs, group counts, script
  SHA-256 and this skill's version. Preserve all these files together.
- Replay with the installed project and matplotlib:
  `python plot.py --spec spec.json --data data.csv --out replay`.
  The copied script embeds the default style, matching `templates/air.mplstyle`, so replay
  needs no external style file. PDF dates are disabled; no random jitter is used.
  Identical inputs/tool versions reproduce bytes; fonts/matplotlib/platform versions may
  affect rendering across environments. Colors are paired with markers/hatches.
- Runtime validation uses `check_output({"artifact_dir": path, "project_root": root})`;
  it checks archived source data, metadata, source/script provenance and file signatures,
  not only file existence. It never executes an artifact's potentially edited script.
  A bare Aggregate/spec envelope cannot pass output validation. For pre-render checks,
  use `figures.validate_spec_input({"aggregate": ..., "spec": ...})` (optionally with
  `csv`); preflight success does not prove an artifact exists. Invalid preflight-only
  skill samples retain their semantic diagnostics, while valid envelopes still fail
  output validation because generated artifacts are required.

The canonical figure artifact is complete here. Copying PDFs into `paper/figures/`
is deferred to later Writing integration. This skill does not write paper sources.
