"""Best-effort environment capture and byte-exact, permission-checked archival."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from airesearcher.core.errors import PermissionDenied
from airesearcher.core.fsutil import sha256_file
from airesearcher.runtime.envsnap import capture
from airesearcher.runtime.local import LocalExecutor

from .test_executor import _spec, _wait


def inspection(monkeypatch, *, pip_missing=False):
    real_run = subprocess.run

    def run(command, **kwargs):
        if command[1:4] == ["-m", "pip", "freeze"]:
            if pip_missing:
                raise subprocess.CalledProcessError(1, command, stderr="No module named pip")
            return SimpleNamespace(stdout="example==1.0\r\nother==2.0\r\n")
        if command[0] == "nvidia-smi":
            raise FileNotFoundError("No GPU query tool")
        return real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)


def test_lock_hashes_exact_written_bytes_even_with_text_newline_translation(project, monkeypatch):
    inspection(monkeypatch)
    real_write = Path.write_text

    def windows_text(self, data, *args, **kwargs):
        if self.name == "requirements.lock":
            data = data.replace("\n", "\r\n")
        return real_write(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", windows_text)
    seen = []

    def guard(action, target, **kwargs):
        seen.append((action, Path(target).name))
        project.permissions.guard(action, target, **kwargs)

    result = capture(project.root, sys.executable, guard=guard)
    path = project.root / result["lock_path"]
    assert path.read_bytes() == b"example==1.0\nother==2.0\n"
    assert sha256_file(path) == result["lock_sha256"]
    assert seen == [("write", "requirements.lock"), ("write", "REPRODUCE.md")]


def test_shared_local_executor_runs_without_pip_or_nvidia_smi(project, monkeypatch):
    inspection(monkeypatch, pip_missing=True)
    executor = LocalExecutor(project, python=sys.executable)
    spec = _spec(project, executor, "r-without-pip", {"method": "main", "seed": 0, "seconds": 0.01})
    executor.prepare(spec)
    environment = executor.snapshot_env(spec.record.run_id)
    assert environment["packages_lock"] is None and environment["packages_lock_sha256"] is None
    assert any("pip freeze" in item for item in environment["unknown"])
    assert any("nvidia-smi" in item for item in environment["unknown"])
    executor.submit(spec)
    assert _wait(executor, spec.record.run_id).state.value == "succeeded"


def test_denied_environment_write_is_reported_without_creating_files(project, monkeypatch):
    inspection(monkeypatch)

    def deny(*args, **kwargs):
        raise PermissionDenied("Environment archive is read-only")

    result = capture(project.root, sys.executable, guard=deny)
    assert result["lock_path"] is None
    assert any("PermissionDenied" in item for item in result["unknown"])
    assert not list((project.root / "environments").rglob("requirements.lock"))
    assert "PermissionDenied" in json.dumps(result)
