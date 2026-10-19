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

from airesearcher.core.archive import Archive
from airesearcher.core.errors import AirError
from airesearcher.core.events import Events
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
        raise ValueError("ambiguous grouping: provide an explicit FigureSpec")
    counts = ", ".join(f"n={r.n} ({renderer.canonical(r.group)})" for r in rows)
    prefix = "Exploratory: " if aggregate.eval_condition == "explore" else ""
    # An unknown physical unit must be supplied by the caller; never infer dimensionless.
    if ylabel is None:
        raise ValueError("provide ylabel with the metric unit or explicit dimensionless designation")
    spec = FigureSpec(fig_id=fig_id or f"fig_{aggregate.aggregate_id.lower()}", kind="point" if distribution else kind,
                      aggregate_id=aggregate.aggregate_id, x=x, hue=hue, metric=metric, error="ci95",
                      title=prefix + aggregate.comparison.get("question", aggregate.aggregate_id), xlabel=x,
                      ylabel=ylabel, scale=scale, caption=f"{prefix}{counts}. Error bars: 95% CI.")
    renderer.select_rows(aggregate, spec)
    return spec


def prepare_data(aggregate: Aggregate, spec: FigureSpec) -> str:
    renderer = load_renderer()
    rows = renderer.select_rows(aggregate, spec)
    return renderer.csv_text(rows, spec, aggregate.eval_condition)


def validate_artifact(directory: Path, aggregate: Aggregate, aggregate_ref: VersionRef,
                      *, script_path: Path | None = None, skill_ref: str = "scientific-plotting@0.1.0") -> list[str]:
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


def check_output(output: object) -> list[str]:
    """Skill protocol: artifact context, or self-contained spec/Aggregate negative sample."""
    try:
        if isinstance(output, str):
            output = json.loads(output)
        if not isinstance(output, dict):
            return ["expected artifact context or spec/Aggregate validation envelope"]
        if "artifact_dir" not in output:
            aggregate = Aggregate.model_validate(output["aggregate"])
            spec = FigureSpec.model_validate(output["spec"])
            expected = prepare_data(aggregate, spec)
            if "csv" in output and output["csv"] != expected:
                return ["data.csv differs from source Aggregate or scale"]
            return []
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
