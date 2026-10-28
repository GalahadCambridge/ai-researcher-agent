"""load_stage：区分“找不到阶段模块”和“阶段模块的依赖包没装”。"""

from __future__ import annotations

import pytest

from airesearcher.stages import load_stage


def test_missing_dependency_is_not_reported_as_missing_module(monkeypatch):
    monkeypatch.setenv("AIR_STAGE_IMPL", "real")

    def fake_import(name):
        raise ModuleNotFoundError("No module named 'somepkg'", name="somepkg")

    monkeypatch.setattr("airesearcher.stages.importlib.import_module", fake_import)
    with pytest.raises(RuntimeError) as ei:
        load_stage("idea")
    msg = str(ei.value)
    assert "somepkg" in msg and "没有安装" in msg and "pip install" in msg
    assert "找不到模块" not in msg


@pytest.mark.parametrize("missing", ["airesearcher.stages.idea.stage", "airesearcher.stages.idea"])
def test_missing_stage_module_or_parent_keeps_old_hint(monkeypatch, missing):
    monkeypatch.setenv("AIR_STAGE_IMPL", "real")

    def fake_import(name):
        raise ModuleNotFoundError(f"No module named {missing!r}", name=missing)

    monkeypatch.setattr("airesearcher.stages.importlib.import_module", fake_import)
    with pytest.raises(RuntimeError, match="找不到模块"):
        load_stage("idea")
