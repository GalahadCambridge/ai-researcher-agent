"""Data preparation, changed-version questions and accepted input hashes."""

import sys
import time

from airesearcher.core.fsutil import sha256_file
from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.models.plan import TaskConfig
from airesearcher.core.models.question import Answer
from airesearcher.runtime.data import ensure_data, input_refs

from .test_plan_stage import seeded


def context(air_home):
    _, engine, _, _ = seeded(air_home)
    ctx = engine.build_context()
    ctx.state = S.PlanDrafting
    return ctx


def task(base, prepare=None):
    return TaskConfig(
        name="data_fixture",
        domain="generic",
        description="Standard-library fixture",
        template_dir="tasks/smoke/template",
        entrypoint=["{python}", "run.py"],
        metrics=[],
        data=[
            {
                "name": "fixture",
                "source": "local:fixture",
                "files": [{"path": "values.txt", "sha256": None}],
                "prepare": prepare or [],
            }
        ],
    )


def test_existing_files_are_recorded_and_checked(air_home, tmp_path, monkeypatch):
    monkeypatch.setenv("AIR_DATA_DIR", str(tmp_path))
    (tmp_path / "values.txt").write_text("version one", encoding="utf-8")
    ctx = context(air_home)
    assert ensure_data(ctx, task(tmp_path)).ready
    assert input_refs(ctx.root)[0].sha256 == sha256_file(tmp_path / "values.txt")


def test_changed_bytes_require_explicit_acceptance(air_home, tmp_path, monkeypatch):
    monkeypatch.setenv("AIR_DATA_DIR", str(tmp_path))
    path = tmp_path / "values.txt"
    path.write_text("one", encoding="utf-8")
    ctx = context(air_home)
    t = task(tmp_path)
    assert ensure_data(ctx, t).ready
    path.write_text("two", encoding="utf-8")
    status = ensure_data(ctx, t)
    assert not status.ready and {o.id for o in status.question.options} == {"accept", "end"}
    assert not any(o.default for o in status.question.options)
    # Match the engine's persisted question association.
    from airesearcher.core.fsutil import now

    ctx.scratch["plan"] = {"asked": "q-test"}
    ctx.answer = Answer(question_id="q-test", choice="accept", text="", answered_by="test", answered_at=now())
    assert ensure_data(ctx, t).ready
    assert input_refs(ctx.root)[0].sha256 == sha256_file(path)


def test_missing_files_prepare_asynchronously_and_failure_asks(air_home, tmp_path, monkeypatch):
    monkeypatch.setenv("AIR_DATA_DIR", str(tmp_path / "data"))
    ctx = context(air_home)
    command = [
        sys.executable,
        "-c",
        "from pathlib import Path; p=Path(r'{data_dir}'); "
        "p.mkdir(parents=True,exist_ok=True); (p/'values.txt').write_text('ready',encoding='utf-8')",
    ]
    t = task(tmp_path, command)
    status = ensure_data(ctx, t)
    assert status.waiting
    for _ in range(200):
        time.sleep(0.01)
        status = ensure_data(ctx, t)
        if not status.waiting:
            break
    assert status.ready
    assert (ctx.root / "environments/prepare_fixture.log").exists()
    (tmp_path / "data/values.txt").unlink()
    t.data[0].prepare = [sys.executable, "-c", "raise SystemExit(3)"]
    status = ensure_data(ctx, t)
    for _ in range(200):
        time.sleep(0.01)
        status = ensure_data(ctx, t)
        if not status.waiting:
            break
    assert status.question and {o.id for o in status.question.options} == {"retry", "end"}


def test_inventory_caches_hashes_but_execution_rechecks_bytes(air_home, tmp_path, monkeypatch):
    from airesearcher.runtime import data

    monkeypatch.setenv("AIR_DATA_DIR", str(tmp_path))
    path = tmp_path / "values.txt"
    path.write_text("first", encoding="utf-8")
    original = data.sha256_file
    hashed = []

    def counted(p):
        hashed.append(p)
        return original(p)

    monkeypatch.setattr(data, "sha256_file", counted)
    ctx, definition = context(air_home), task(tmp_path)
    assert ensure_data(ctx, definition).ready
    assert ensure_data(ctx, definition).ready
    assert hashed == [path]
    input_refs(ctx.root)
    assert hashed == [path, path]  # Execution bypasses the planning cache.
    path.write_text("changed contents", encoding="utf-8")
    assert ensure_data(ctx, definition).question
    assert hashed == [path, path, path]


def test_preparation_checks_process_identity_and_uses_detached_flags(air_home, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from airesearcher.runtime import data

    monkeypatch.setenv("AIR_DATA_DIR", str(tmp_path / "missing"))
    ctx, definition = context(air_home), task(tmp_path, [sys.executable, "-c", "pass"])
    launches = []
    monkeypatch.setattr(data, "detached_kwargs", lambda: {"creationflags": 1234})

    def start(command, **kwargs):
        launches.append((command, kwargs))
        return SimpleNamespace(pid=42)

    monkeypatch.setattr(data.subprocess, "Popen", start)
    assert ensure_data(ctx, definition).waiting
    assert launches[0][1]["creationflags"] == 1234
    checked = []
    monkeypatch.setattr(data, "pid_matches", lambda pid, identity: checked.append((pid, identity)) or False)
    status = ensure_data(ctx, definition)
    assert status.question and "disappeared" in status.detail
    assert checked == [(42, ctx.scratch["data"]["job"]["spec"])]
    assert len(launches) == 1  # A reused PID does not keep the project waiting forever.
