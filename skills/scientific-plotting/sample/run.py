"""Synthetic verification data, intentionally separate from sample_project results."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from airesearcher.core.archive import Archive
from airesearcher.core.events import Events
from airesearcher.core.fsutil import atomic_write_json
from airesearcher.core.models.aggregate import Aggregate
from airesearcher.core.models.figure import FigureSpec
from airesearcher.skills.loader import Skill
from airesearcher.stages.writing.figures import generate_figure


def run(skill: Skill, input_dir: Path, out_dir: Path) -> dict:
    aggregate = Aggregate.model_validate_json((input_dir / "aggregate.json").read_text(encoding="utf-8"))
    spec = FigureSpec.model_validate_json((input_dir / "spec.json").read_text(encoding="utf-8"))
    root = out_dir / "project"
    archive = Archive(root, Events(root))
    archive.put(f"artifacts/aggregates/{aggregate.aggregate_id}.json", "aggregate", aggregate.model_dump_json(),
                producer="system", note="synthetic skill verification data")
    figure = generate_figure(root, aggregate.aggregate_id, spec, skill=skill,
                             created_at=datetime.fromisoformat("2026-01-01T00:00:00+00:00"))
    atomic_write_json(out_dir / "summary.json", {
        "source": "synthetic verification fixture", "groups": len(figure.n_per_group),
        "runs": len(figure.run_ids), "kind": figure.spec.kind, "scale": figure.spec.scale,
    })
    return {"artifact_dir": str(root / "artifacts/figures" / spec.fig_id), "project_root": str(root)}
