"""Keep repository-only command entry points usable after moving validation tools."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("module", ["demo", "live", "evaluation", "quality"])
def test_repository_tool_help_without_model_credentials(module, tmp_path):
    env = dict(os.environ, AIR_HOME=str(tmp_path / "air"), AIR_REAL_LLM="0")
    env.pop("OPENROUTER_API_KEY", None)
    result = subprocess.run([sys.executable, "-m", f"scripts.literature.{module}", "--help"],
                            cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()


def test_evaluation_wrapper_works_from_another_directory(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "eval/metrics/literature_compare.py"), "--help"],
                            cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert "--workspace" in result.stdout
