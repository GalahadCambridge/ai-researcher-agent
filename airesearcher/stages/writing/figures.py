"""Plotting orchestration only: official Aggregate service → validated figure archive."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from types import ModuleType

from pydantic import ValidationError

from airesearcher.core.archive import Archive
from airesearcher.core.errors import AirError, ValidationFailed
from airesearcher.core.events import Events
from airesearcher.core.figure_errors import PresentationError, UnitLabelError
from airesearcher.core.fsutil import atomic_write_json, atomic_write_text, now, sha256_file
from airesearcher.core.models.aggregate import Aggregate
from airesearcher.core.models.common import VersionRef
from airesearcher.core.models.figure import Figure, FigureSpec
from airesearcher.core.workspace import repo_root
from airesearcher.services import runs
from airesearcher.skills.loader import Skill, SkillLoader


@lru_cache(maxsize=16)
def load_renderer(path: Path | None = None) -> ModuleType:
    """Load the fixed skill renderer, or its identical copy during generation."""
    path = path or repo_root() / "skills/scientific-plotting/scripts/plot.py"
    # Compile trusted skill source without writing __pycache__ into archived artifacts.
    module = ModuleType("air_scientific_plot")
    module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module


def default_spec(aggregate: Aggregate, *, fig_id: str | None = None, metric: str | None = None,
                 ylabel: str | None = None, scale: float = 1.0, distribution: bool = False) -> FigureSpec:
    """Documented defaults without guessing units, observations or hidden dimensions."""
    metric = metric or aggregate.comparison.get("metric")
    rows = [r for r in aggregate.rows if r.metric == metric]
    if not rows:
        raise ValueError(f"metric {metric!r} has no rows")
    fields = aggregate.comparison.get("group_by") or sorted(rows[0].group)
    if not fields or any(k not in r.group for r in rows for k in fields):
        raise ValueError("invalid Aggregate group_by")
    renderer = load_renderer()
    numeric = [k for k in fields if all(renderer.is_numeric(r.group[k]) for r in rows)]
    categorical = [k for k in fields if k not in numeric]
    if len(numeric) == 1 and len(categorical) <= 1 and len(fields) <= 2:
        x, hue, kind = numeric[0], categorical[0] if categorical else None, "line"
    elif len(fields) <= 2 and not numeric:
        x, hue, kind = fields[0], fields[1] if len(fields) == 2 else None, "bar"
    else:
        varying = [k for k in fields if len({renderer.canonical(r.group[k]) for r in rows}) > 1]
        x = next((k for k in numeric if k in varying), fields[0])
        hue = next((k for k in categorical + fields if k != x and k in varying), None)
        kind = "line" if x in numeric else "bar"
    facets = [k for k in fields if k not in {x, hue}
              and len({renderer.canonical(r.group[k]) for r in rows}) > 1]
    counts = ", ".join(f"n={r.n} ({renderer.canonical(r.group)})" for r in rows)
    prefix = "Exploratory: " if aggregate.eval_condition == "explore" else ""
    # An unknown physical unit must be supplied by the caller; never infer dimensionless.
    if ylabel is None:
        raise UnitLabelError("provide ylabel with the metric unit or explicit dimensionless designation")
    spec = FigureSpec(fig_id=fig_id or f"fig_{aggregate.aggregate_id.lower()}", kind="point" if distribution else kind,
                      aggregate_id=aggregate.aggregate_id, x=x, hue=hue, facets=facets, metric=metric,
                      error="none" if all(r.n == 1 and r.ci95 is None for r in rows) else "ci95",
                      title=prefix + aggregate.comparison.get("question", aggregate.aggregate_id), xlabel=x,
                      ylabel=ylabel, scale=scale, caption=f"{prefix}{counts}. Error bars: 95% CI.")
    if spec.error == "none":
        spec.caption = f"{prefix}{counts}. Single observations; no uncertainty estimate."
    renderer.select_rows(aggregate, spec)
    return spec


def prepare_data(aggregate: Aggregate, spec: FigureSpec) -> str:
    renderer = load_renderer()
    rows = renderer.select_rows(aggregate, spec)
    return renderer.csv_text(rows, spec, aggregate.eval_condition)


def presentation_error(exc: ValueError) -> bool:
    return isinstance(exc, (ValidationError, PresentationError))


def validate_source(aggregate: Aggregate) -> None:
    """Check source invariants independently of any proposed projection or units."""
    renderer = load_renderer()
    metric = aggregate.comparison.get("metric")
    rows = [r for r in aggregate.rows if r.metric == metric]
    if not rows:
        raise ValidationFailed("Aggregate comparison metric has no rows")
    keys, groups, runs = set(rows[0].group), set(), set()
    for row in rows:
        group = renderer.canonical(row.group)
        if (not keys or set(row.group) != keys or group in groups
                or not set(aggregate.comparison.get("group_by") or []) <= keys):
            raise ValidationFailed("Aggregate grouping metadata or complete groups are inconsistent")
        if runs.intersection(row.run_ids):
            raise ValidationFailed("duplicate run provenance")
        groups.add(group)
        runs.update(row.run_ids)
        # A one-row, non-rendered validation probe has no hidden dimensions.
        # Its label is internal only, never a scientific unit declaration.
        probe = FigureSpec(fig_id="source_validation", aggregate_id=aggregate.aggregate_id, kind="bar",
                           x=sorted(keys)[0], metric=metric, hue=None,
                           error="none" if row.n == 1 else "ci95" if row.ci95 is not None else "std",
                           title="Exploratory source validation", xlabel="group", ylabel="internal (unitless)",
                           caption=f"Exploratory n={row.n}; 95% CI / standard deviation.")
        try:
            renderer.validate_rows(probe, [row], aggregate.eval_condition)
        except ValueError as exc:
            raise ValidationFailed(f"Invalid Aggregate evidence: {exc}") from exc


def validate_proposal(aggregate: Aggregate, spec: FigureSpec) -> None:
    """Reject presentation mistakes, while source-integrity errors fail closed.

    Blank units remain a human clarification, not an invitation to invent units.
    This preflight neither edits Aggregate rows nor pools configurations.
    """
    rows = [row for row in aggregate.rows if row.metric == spec.metric]
    if not rows:
        raise ValueError(f"metric {spec.metric!r} has no rows")
    keys = set(rows[0].group)
    if any(set(row.group) != keys for row in rows) or not set(aggregate.comparison.get("group_by") or []) <= keys:
        raise ValidationFailed("Aggregate grouping metadata is inconsistent")
    represented = {spec.x, *spec.facets} | ({spec.hue} if spec.hue else set())
    if not represented <= keys:
        raise PresentationError("unknown grouping field in FigureSpec")
    hidden = sorted(k for k in keys - represented if len({load_renderer().canonical(r.group[k]) for r in rows}) > 1)
    if hidden:
        raise PresentationError(f"{', '.join(hidden)} varies but is not represented; "
                         "projection would pool distinct configurations")
    if not spec.ylabel.strip():
        return
    normalized = spec.model_copy(update={"xlabel": spec.xlabel if spec.xlabel.strip() else spec.x})
    try:
        prepare_data(aggregate, normalized)
    except ValueError as exc:
        # Only renderer diagnostics owned by presentation planning are recoverable.
        # n/run lineage, invalid source statistics and corrupt group values are not.
        if presentation_error(exc):
            raise
        raise ValidationFailed(f"Invalid Aggregate evidence: {exc}") from exc


def validate_artifact(directory: Path, aggregate: Aggregate, aggregate_ref: VersionRef,
                      *, script_path: Path | None = None, skill_ref: str = "scientific-plotting@0.2.0") -> list[str]:
    """Validate against independent source data. Never execute an artifact's script."""
    directory = Path(directory)
    try:
        spec = FigureSpec.model_validate_json((directory / "spec.json").read_text(encoding="utf-8"))
        figure = Figure.model_validate_json((directory / "figure.json").read_text(encoding="utf-8"))
        expected_data = prepare_data(aggregate, spec)
        problems = []
        if (directory / "data.csv").read_text(encoding="utf-8") != expected_data:
            problems.append("data.csv differs from source Aggregate or scale")
        renderer = load_renderer()
        rows = renderer.select_rows(aggregate, spec)
        expected_runs = [rid for row in rows for rid in row.run_ids]
        expected_counts = {renderer.canonical(row.group): row.n for row in rows}
        if figure.spec != spec or figure.fig_id != spec.fig_id:
            problems.append("Figure spec/fig_id mismatch")
        expected_id = f"artifacts/aggregates/{aggregate.aggregate_id}.json"
        if (aggregate_ref.artifact_id != expected_id or aggregate_ref.version < 1
                or not re.fullmatch(r"[0-9a-f]{64}", aggregate_ref.sha256)
                or figure.aggregate_ref != aggregate_ref):
            problems.append("incorrect Aggregate VersionRef")
        if figure.run_ids != expected_runs:
            problems.append("incorrect run provenance")
        if figure.n_per_group != expected_counts:
            problems.append("incorrect n_per_group")
        if figure.skill_ref != skill_ref:
            problems.append("incorrect skill_ref")
        source_script = script_path or repo_root() / "skills/scientific-plotting/scripts/plot.py"
        script = directory / "plot.py"
        if figure.script_sha256 != sha256_file(script) or script.read_bytes() != source_script.read_bytes():
            problems.append("plot.py or script_sha256 differs from actual skill renderer")
        expected_outputs = [f"artifacts/figures/{spec.fig_id}/{spec.fig_id}.{suffix}" for suffix in ("pdf", "png")]
        if figure.outputs != expected_outputs:
            problems.append("incorrect output paths")
        pdf, png = directory / f"{spec.fig_id}.pdf", directory / f"{spec.fig_id}.png"
        pdf_data, png_data = pdf.read_bytes(), png.read_bytes()
        if not pdf_data.startswith(b"%PDF-") or b"%%EOF" not in pdf_data[-1024:]:
            problems.append("missing/invalid PDF")
        if not png_data.startswith(b"\x89PNG\r\n\x1a\n") or len(png_data) < 33:
            problems.append("missing/invalid PNG")
        else:
            # Decode the image as well as checking its signature.
            from matplotlib import image as mpl_image

            pixels = mpl_image.imread(png)
            if pixels.size == 0 or pixels.max() == pixels.min():
                problems.append("PNG is empty or blank")
        return problems
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return [f"invalid scientific figure artifact: {exc}"]


def generate_figure(root: Path, aggregate_id: str, spec: FigureSpec | None = None, *,
                    version: int | None = None, ylabel: str | None = None, skill: Skill | None = None,
                    created_at: datetime | None = None) -> Figure:
    """Generate/register one figure from an exact archived Aggregate version.

    Does not change paper sources, shared fixtures, experiment data or project state.
    The caller supplies units when requesting automatic spec selection.
    """
    root = Path(root).resolve()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", aggregate_id):
        raise ValueError("aggregate_id must be a safe identifier")
    archive = Archive(root, Events(root))
    artifact_id = f"artifacts/aggregates/{aggregate_id}.json"
    versions = archive.versions(artifact_id)
    source = next((v for v in reversed(versions) if version is None or v.ref.version == version), None)
    if source is None:
        raise ValueError(f"Aggregate {aggregate_id} has no archived version {version}")
    archive.get(source.ref)  # Check archived bytes before assigning provenance.
    aggregate = runs.load_aggregate(root, aggregate_id, version=source.ref.version)
    spec = spec or default_spec(aggregate, ylabel=ylabel)
    data = prepare_data(aggregate, spec)
    skill = skill or SkillLoader().load("scientific-plotting")
    if skill.name != "scientific-plotting":
        raise ValueError("expected scientific-plotting skill")
    relative = f"artifacts/figures/{spec.fig_id}"
    destination = archive.path_of(relative)
    # Also reject in-root symlinks: do not accidentally overwrite a different artifact.
    lexical = root / relative
    if any(p.is_symlink() for p in [lexical, *lexical.parents] if p != root.parent):
        raise ValueError("figure destination must not use symlinks")
    known = {"spec.json", "data.csv", "plot.py", "figure.json", f"{spec.fig_id}.pdf", f"{spec.fig_id}.png"}
    if destination.exists() and any(p.name not in known or p.is_symlink() for p in destination.iterdir()):
        raise ValueError("refusing to overwrite an unrelated figure directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".plot-", dir=destination.parent) as tmp:
        stage = Path(tmp)
        atomic_write_json(stage / "spec.json", spec)
        atomic_write_text(stage / "data.csv", data)
        shutil.copyfile(skill.path("scripts/plot.py"), stage / "plot.py")
        renderer = load_renderer(stage / "plot.py")
        renderer.render(spec, stage / "data.csv", stage)
        rows = renderer.select_rows(aggregate, spec)
        figure = Figure(fig_id=spec.fig_id, spec=spec, aggregate_ref=source.ref,
                        run_ids=[rid for row in rows for rid in row.run_ids], skill_ref=skill.ref,
                        script_sha256=sha256_file(stage / "plot.py"),
                        outputs=[f"{relative}/{spec.fig_id}.{suffix}" for suffix in ("pdf", "png")],
                        n_per_group={renderer.canonical(row.group): row.n for row in rows},
                        created_at=created_at or now())
        # Keep a repeat generation with identical inputs/content idempotent.
        previous = destination / "figure.json"
        if created_at is None and previous.exists():
            old = Figure.model_validate_json(previous.read_text(encoding="utf-8"))
            if old.model_dump(exclude={"created_at"}) == figure.model_dump(exclude={"created_at"}):
                figure.created_at = old.created_at
        atomic_write_json(stage / "figure.json", figure)
        problems = validate_artifact(stage, aggregate, source.ref,
                                     script_path=skill.path("scripts/plot.py"), skill_ref=skill.ref)
        if problems:
            raise ValueError("; ".join(problems))
        shutil.copytree(stage, destination, dirs_exist_ok=True)
    archive.put(relative, "figure", destination, parents=[source.ref], producer="agent:writing/figures")
    return figure


def validate_spec_input(value: object) -> list[str]:
    """Preflight Aggregate/spec/optional CSV only; success does not prove any output."""
    try:
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, dict):
            return ["expected spec/Aggregate preflight envelope"]
        aggregate = Aggregate.model_validate(value["aggregate"])
        spec = FigureSpec.model_validate(value["spec"])
        expected = prepare_data(aggregate, spec)
        if "csv" in value and value["csv"] != expected:
            return ["data.csv differs from source Aggregate or scale"]
        return []
    except (ValueError, KeyError, TypeError) as exc:
        return [f"invalid scientific plotting spec input: {exc}"]


def check_output(output: object) -> list[str]:
    """Skill output protocol: require real artifacts and independent archived sources.

    Preflight-only envelopes remain useful negative samples: report their semantic
    failures, but never promote a successful preflight into output success.
    """
    try:
        if isinstance(output, str):
            output = json.loads(output)
        if not isinstance(output, dict):
            return ["expected generated artifact context"]
        if "artifact_dir" not in output:
            return validate_spec_input(output) or ["generated output requires artifact_dir and project_root"]
        root, directory = Path(output["project_root"]), Path(output["artifact_dir"])
        figure = Figure.model_validate_json((directory / "figure.json").read_text(encoding="utf-8"))
        archive = Archive(root, Events(root))
        ref = figure.aggregate_ref
        archive.get(ref)
        if ref.artifact_id != f"artifacts/aggregates/{figure.spec.aggregate_id}.json":
            return ["incorrect Aggregate provenance"]
        aggregate = runs.load_aggregate(root, figure.spec.aggregate_id, version=ref.version)
        return validate_artifact(directory, aggregate, ref)
    except (AirError, ValueError, OSError, KeyError, TypeError) as exc:
        return [f"invalid scientific plotting input: {exc}"]
