"""Prepared runs export src contents directly into code/ and bind inputs."""

import pytest

from airesearcher.core.errors import InvalidState
from airesearcher.core.fsutil import atomic_write_json
from airesearcher.runtime.local import LocalExecutor

from .test_executor import _spec


def test_export_layout_and_relative_entrypoint(project):
    executor = LocalExecutor(project)
    spec = _spec(project, executor, "r-layout", {"method": "main", "seed": 0, "seconds": 0.01})
    executor.prepare(spec)
    assert (spec.run_dir / "code/run.py").exists()
    assert not (spec.run_dir / "code/src").exists()
    assert "../config.yaml" in spec.record.command and "../outputs" in spec.record.command


def test_prepare_rejects_input_version_missing_from_record(project, tmp_path):
    path = tmp_path / "input.txt"
    path.write_text("fixture", encoding="utf-8")
    from airesearcher.core.fsutil import sha256_file

    atomic_write_json(
        project.root / "environments/data_checksums.json",
        [{"name": "fixture/input", "path": str(path), "sha256": sha256_file(path)}],
    )
    executor = LocalExecutor(project)
    spec = _spec(project, executor, "r-input-check", {"method": "main", "seed": 0, "seconds": 0.01})
    with pytest.raises(InvalidState, match="accepted project"):
        executor.prepare(spec)
    assert not spec.run_dir.exists()
