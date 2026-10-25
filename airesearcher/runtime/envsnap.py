"""Capture the actual experiment interpreter, package lock and host hardware without loading a model."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from airesearcher.core.errors import PermissionDenied
from airesearcher.core.fsutil import atomic_write_bytes, atomic_write_text, sha256_bytes


def capture(root: Path, python: str, *, guard):
    """Best-effort inspection; unavailable tooling never fabricates a package lock."""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    unknown = []
    data = None
    try:
        lock = subprocess.run(
            [python, "-m", "pip", "freeze"], capture_output=True, text=True, encoding="utf-8",
            timeout=45, env=env, check=True,
        ).stdout
        data = lock.replace("\r\n", "\n").encode("utf-8")
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        unknown.append(f"Package lock unavailable: pip freeze failed ({type(exc).__name__}).")
    code = (
        "import sys,json,importlib.metadata as m; names=['torch','transformers','peft','scikit-learn','numpy']; "
        "installed={d.metadata['Name'].lower():d.version for d in m.distributions()}; "
        "print(json.dumps({'python':sys.version,'executable':sys.executable,"
        "'frameworks':{n:installed.get(n) for n in names}}))"
    )
    info = {"python": None, "executable": python, "frameworks": {}}
    try:
        info.update(json.loads(subprocess.run(
            [python, "-c", code], capture_output=True, text=True, encoding="utf-8", timeout=20, env=env, check=True
        ).stdout))
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError, TypeError) as exc:
        unknown.append(f"Interpreter/package metadata unavailable ({type(exc).__name__}).")
    try:
        gpu = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError, UnicodeError):
        gpu = None
    if not gpu:
        unknown.append("GPU hardware/driver query unavailable: nvidia-smi did not return usable output.")
    digest = sha256_bytes(data if data is not None else json.dumps(info, sort_keys=True).encode("utf-8"))[:16]
    directory = root / "environments" / digest
    version = (info["python"] or "unknown").splitlines()[0]
    install = (
        "2. Install the recorded packages with `python -m pip install -r requirements.lock`.\n"
        if data is not None else
        "2. No package lock was captured; inspect the unavailable metadata and record dependencies manually.\n"
    )
    text = (
        "# Reproducing an archived experiment\n\n"
        f"Recorded interpreter: `{info['executable']}`. Python: `{version}`.\n\n"
        "1. Create a compatible isolated Python environment.\n"
        f"{install}"
        "3. Restore prepared data/model files and verify the SHA256 values listed in the run's inputs.\n"
        "4. Set AIR_DATA_DIR and AIR_MODEL_DIR to those verified local directories.\n"
        "5. Copy the archived run directory, including config.yaml, then enter its code/ directory.\n"
        "   Execute run.json's command, changing the output directory to a new empty location.\n"
        "   Preserve the configuration and random seed.\n\n"
        "HF_HUB_OFFLINE=1 and TRANSFORMERS_OFFLINE=1 are required. CUDA hardware and a compatible driver\n"
        "are required for CUDA configurations. A different platform may require compatible wheel versions;\n"
        "the lock is an environment record, not a guarantee of bitwise reproducibility.\n"
    )
    lock_path = lock_hash = None
    if unknown:
        text += "\n## Unavailable metadata\n\n" + "\n".join(f"- {item}" for item in unknown) + "\n"
    try:
        if data is not None:
            path = directory / "requirements.lock"
            guard("write", str(path), actor="executor")
            atomic_write_bytes(path, data)
            lock_path = path.relative_to(root).as_posix()
            lock_hash = sha256_bytes(data)
        path = directory / "REPRODUCE.md"
        guard("write", str(path), actor="executor")
        atomic_write_text(path, text)
    except (PermissionDenied, OSError) as exc:
        unknown.append(f"Environment archival incomplete ({type(exc).__name__}); no complete reproduction is claimed.")
    return {
        **info,
        "lock_path": lock_path,
        "lock_sha256": lock_hash,
        "gpu": gpu,
        "unknown": unknown,
    }
