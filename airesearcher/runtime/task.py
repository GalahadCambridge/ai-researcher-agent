"""Task-declared aliases, trial overrides and accelerator requirements."""

from __future__ import annotations

import yaml

from airesearcher.core.models.plan import TaskConfig
from airesearcher.core.workspace import resolve_task_dir


def load_task_config(path):
    directory = resolve_task_dir(path)
    return TaskConfig.model_validate(yaml.safe_load((directory / "task.yaml").read_text(encoding="utf-8")))


def uses_gpu(config, task=None):
    """CUDA requests consume GPU time unless the task declares a CPU-only method."""
    methods = (task.model_extra or {}).get("method_resources", {}) if task is not None else {}
    enabled = methods.get(config.get("method"), {}).get("gpu", True)
    if type(enabled) is not bool:
        raise ValueError("method_resources.<method>.gpu must be a boolean")
    return config.get("device") == "cuda" and enabled


def trial_overrides(task):
    values = (task.model_extra or {}).get("trial_overrides", {})
    if not isinstance(values, dict):
        raise ValueError("trial_overrides must be a configuration mapping")
    return values
