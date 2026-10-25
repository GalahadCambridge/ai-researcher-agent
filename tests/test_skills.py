"""skill 的版本登记与验证（详细设计 1 第 7.3 节）。"""

from __future__ import annotations

import shutil

import pytest
from typer.testing import CliRunner

from airesearcher.cli import main as cli
from airesearcher.core.workspace import repo_root
from airesearcher.skills.loader import SkillLoader, SkillNotVerified

runner = CliRunner()


@pytest.fixture
def skills_root(tmp_path, monkeypatch):
    """一个临时的仓库目录：skills/ 下只有 example-skill，可以随便改。"""
    root = tmp_path / "repo"
    shutil.copytree(repo_root() / "skills", root / "skills", ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setenv("AIR_REPO", str(root))
    monkeypatch.delenv("AIR_SKILLS_UNVERIFIED", raising=False)
    return root / "skills"


def bump(skills_root, version="0.2.0"):
    y = skills_root / "example-skill/skill.yaml"
    y.write_text(y.read_text(encoding="utf-8").replace("version: 0.1.0", f"version: {version}"), encoding="utf-8")


def test_repo_skills_are_verified_and_enabled():
    """仓库里每个 skill 的当前版本都已通过验证并登记（CI 中由这条测试把关）。"""
    loader = SkillLoader()
    assert loader.problems() == []
    for name in loader.available():
        rep = loader.verify(name, update_registry=False)
        assert rep.ok, rep.steps


def test_new_version_needs_verify(skills_root, monkeypatch):
    loader = SkillLoader()
    original_registry = loader.registry()
    bump(skills_root)
    with pytest.raises(SkillNotVerified, match="air skills verify example-skill"):
        loader.load("example-skill")
    assert "启用的是 0.1.0" in loader.problems()[0]
    assert loader.load("example-skill", allow_unverified=True).version == "0.2.0"
    monkeypatch.setenv("AIR_SKILLS_UNVERIFIED", "1")
    assert loader.load("example-skill").version == "0.2.0"
    monkeypatch.delenv("AIR_SKILLS_UNVERIFIED")

    rep = loader.verify("example-skill")
    assert rep.ok and rep.registry_changed == ("0.1.0", "0.2.0")
    assert loader.registry() == {**original_registry, "example-skill": "0.2.0"} and loader.problems() == []
    assert loader.load("example-skill").ref == "example-skill@0.2.0"
    assert "不要手改" in (skills_root / "registry.yaml").read_text(encoding="utf-8")


def test_failed_verify_keeps_old_version(skills_root):
    loader = SkillLoader()
    original_registry = loader.registry()
    bump(skills_root)
    (skills_root / "example-skill/sample/expected/summary.md").write_text("# 摘要\n\n- 别的内容\n", encoding="utf-8")
    rep = loader.verify("example-skill")
    assert not rep.ok and any(label == "比对结果" and not ok for label, ok, _ in rep.steps)
    assert loader.registry() == original_registry

    shutil.rmtree(skills_root / "example-skill/sample")
    rep = loader.verify("example-skill")
    assert not rep.ok and "sample/input" in rep.steps[-1][2]


def test_validators_must_catch_bad_samples(skills_root):
    (skills_root / "example-skill/sample/bad/looks_fine.md").write_text("# 标题\n内容\n", encoding="utf-8")
    rep = SkillLoader().verify("example-skill", update_registry=False)
    assert not rep.ok and "looks_fine.md" in rep.steps[-1][2]


def test_skills_cli(skills_root):
    r = runner.invoke(cli.app, ["skills", "list"], env={"COLUMNS": "160"})
    assert r.exit_code == 0 and "example-skill" in r.output and "0.1.0" in r.output
    bump(skills_root)
    r = runner.invoke(cli.app, ["skills", "verify", "--all", "--check"], env={"COLUMNS": "160"})
    assert r.exit_code == 1 and "启用的是 0.1.0" in r.output  # CI：改了版本号却没验证 → 失败
    r = runner.invoke(cli.app, ["skills", "verify", "example-skill"], env={"COLUMNS": "160"})
    assert r.exit_code == 0 and "已启用 0.2.0" in r.output
    r = runner.invoke(cli.app, ["skills", "verify", "--all", "--check"], env={"COLUMNS": "160"})
    assert r.exit_code == 0, r.output
