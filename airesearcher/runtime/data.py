"""Data preparation and accepted input checksums (design 3, §3.5).

Preparation runs in a separate worker. Engine ticks return Wait rather than
blocking for a download. Changed bytes always require an explicit answer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from airesearcher.core.fsutil import atomic_write_json, sha256_file
from airesearcher.core.locks import _try_lock, _unlock
from airesearcher.core.models.question import Question, QuestionOption
from airesearcher.core.models.run import InputRef
from airesearcher.core.workspace import air_home, repo_root, resolve_task_dir

from .procs import detached_kwargs, pid_matches

CHECKSUMS = "environments/data_checksums.json"


@lru_cache(maxsize=512)
def _cached_sha256(path, fingerprint):
    return sha256_file(Path(path))


def prepared_sha256(path):
    """Cache planning checks by file identity/size/timestamps; submission still rehashes."""
    path = Path(path).resolve()
    stat = path.stat()
    fingerprint = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    result = _cached_sha256(str(path), fingerprint)
    after = path.stat()
    if fingerprint != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError(f"Prepared input changed while hashing: {path.name}")
    return result


@dataclass
class DataStatus:
    ready: bool
    detail: str = ""
    question: Question | None = None
    waiting: bool = False


def _relative(base, name):
    p = Path(name)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"Manifest entries require relative paths without '..': {name}")
    return base / p


def inventory(task):
    """Task metadata stays generic; optional preparation manifests pin files."""
    entries, mismatches, missing = [], [], []
    for data in task.data:
        base = Path(os.environ.get("AIR_DATA_DIR", air_home() / "data" / data.name)).expanduser().resolve()
        reference = {f["path"]: f.get("sha256") for f in data.files}
        manifest = base / "manifest.json"
        if manifest.exists():
            meta = json.loads(manifest.read_text(encoding="utf-8"))
            for name, info in meta.get("files", {}).items():
                reference[name] = reference.get(name) or info.get("sha256")
            entries.append(InputRef(name=f"{data.name}/manifest", path=str(manifest), sha256=prepared_sha256(manifest)))
        for name, expected in reference.items():
            path = _relative(base, name)
            if not path.is_file():
                missing.append((data.name, data.prepare, base))
                continue
            digest = prepared_sha256(path)
            entries.append(InputRef(name=f"{data.name}/{name}", path=str(path), sha256=digest))
            if expected and expected != digest:
                mismatches.append(f"{data.name}/{name}")
        if task.models:
            path = base / "model_manifest.json"
            if not path.is_file():
                prepare = resolve_task_dir(task.name) / "prepare_model.py"
                missing.append((f"{data.name}_model", ["{python}", str(prepare), "--out", str(path)], base))
                continue
            meta = json.loads(path.read_text(encoding="utf-8"))
            if meta["model_name"] not in task.models or not meta["files"]:
                raise ValueError("Model manifest does not match the task")
            model = Path(os.environ.get("AIR_MODEL_DIR", meta["local_dir"])).expanduser().resolve()
            entries.append(InputRef(name="model/manifest", path=str(path), sha256=prepared_sha256(path)))
            for name, expected in meta["files"].items():
                target = _relative(model, name)
                if not target.is_file():
                    missing.append((f"{data.name}_model", [], base))
                    continue
                digest = prepared_sha256(target)
                entries.append(InputRef(name=f"model/{name}", path=str(target), sha256=digest))
                if digest != expected:
                    mismatches.append(f"model/{name}")
    return entries, mismatches, missing


def _question(detail, choices):
    return DataStatus(False, detail, Question(text=detail, options=[QuestionOption(id=x, label=x) for x in choices]))


def ensure_data(ctx, task):
    state = ctx.scratch.setdefault("data", {})
    stage = "plan" if ctx.state.value == "PlanDrafting" else "experiment"
    asked = ctx.scratch.get(stage, {}).get("asked")
    answer = ctx.answer if ctx.answer and ctx.answer.question_id == asked else None
    if answer:
        state.pop("asked", None)
        if answer.choice == "end":
            return DataStatus(False, "Data preparation ended by the user")
        state["accept"] = answer.choice == "accept"
        if answer.choice == "retry":
            state.pop("job", None)
    job = state.get("job")
    if job:
        result = Path(job["result"])
        if not result.exists():
            identity = job.get("spec") or str(result.with_name(
                result.name.removesuffix(".result.json") + ".job.json"
            ))
            if not pid_matches(job["pid"], identity):
                return _question("Preparation worker disappeared; inspect " + job["log"], ["retry", "end"])
            return DataStatus(False, "Preparing data; see " + job["log"], waiting=True)
        outcome = json.loads(result.read_text(encoding="utf-8"))
        if outcome["exit_code"]:
            return _question("Preparation failed; inspect " + job["log"], ["retry", "end"])
        state.pop("job")
    refs, changed, missing = inventory(task)
    if missing:
        name, prepare, base = missing[0]
        if not prepare:
            return _question(f"Missing input {name}; prepare it explicitly before retrying", ["retry", "end"])
        ctx.permissions.guard("exec", "local", actor="agent:experiment/data")
        for data in task.data:
            if data.source.startswith("huggingface:"):
                ctx.permissions.guard("network", "huggingface.co", actor="agent:experiment/data")
        folder = ctx.root / "environments"
        ctx.permissions.guard("write", str(folder), actor="agent:experiment/data")
        folder.mkdir(exist_ok=True)
        log = folder / f"prepare_{name}.log"
        result = folder / f"prepare_{name}.result.json"
        result.unlink(missing_ok=True)
        command = [
            part.format(python=ctx.project.experiment.get("python") or sys.executable, data_dir=base)
            for part in prepare
        ]
        values = {
            "command": command,
            "base": str(base),
            "result": str(result),
            "log": str(log),
            "timeout": ctx.project.experiment.get("prepare_timeout_s", 1800),
        }
        spec = folder / f"prepare_{name}.job.json"
        atomic_write_json(spec, values)
        env = {**os.environ, "PYTHONPATH": str(repo_root()), "HF_HOME": str(air_home() / "data/hf_cache")}
        with log.open("ab") as stream:
            p = subprocess.Popen(
                [sys.executable, "-m", "airesearcher.runtime.data", str(spec)],
                cwd=repo_root(),
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                **detached_kwargs(),
            )
        state["job"] = {**values, "pid": p.pid, "spec": str(spec)}
        return DataStatus(False, "Data preparation started", waiting=True)
    path = ctx.root / CHECKSUMS
    values = [ref.model_dump() for ref in refs]
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    if previous is not None and previous != values:
        changed.append("previously accepted project input version")
    elif previous == values:
        changed = []  # This exact version has already been explicitly accepted.
    if changed and not state.pop("accept", False):
        return _question("Input version changed: " + ", ".join(changed), ["accept", "end"])
    if changed:
        ctx.events.append(
            "stage.decision", "User accepted changed input checksums", actor="user", data={"changed": changed}
        )
    ctx.archive.put(CHECKSUMS, "other", json.dumps(values, indent=2) + "\n", producer="agent:experiment/data")
    return DataStatus(True)


def input_refs(root):
    path = Path(root) / CHECKSUMS
    refs = [InputRef.model_validate(x) for x in json.loads(path.read_text(encoding="utf-8"))]
    for ref in refs:
        if not Path(ref.path).is_file() or sha256_file(Path(ref.path)) != ref.sha256:
            raise ValueError(f"Accepted input changed: {ref.name}")
    return refs


def _worker(path):
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    base = Path(spec["base"])
    base.mkdir(parents=True, exist_ok=True)
    fd = os.open(base / ".lock", os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + spec["timeout"]
    code = 1
    try:
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise TimeoutError("Shared data lock timeout")
            time.sleep(0.2)
        code = subprocess.run(spec["command"], cwd=repo_root(), timeout=max(1, deadline - time.monotonic())).returncode
    except Exception as exc:
        print(f"Preparation failed: {exc}", flush=True)
    finally:
        _unlock(fd)
        os.close(fd)
        atomic_write_json(Path(spec["result"]), {"exit_code": code})


if __name__ == "__main__":
    _worker(sys.argv[1])
