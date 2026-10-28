"""样例项目不能带入生成者本机的信息。"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "sample_project"
ABS_PATH = re.compile(r"/Users/|/home/|/private/|/var/folders/|/tmp/|[A-Za-z]:\\")


def test_sample_project_has_no_absolute_local_paths():
    bad = []
    for f in sorted(ROOT.rglob("*")):
        if f.is_file():
            try:
                text = f.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if ABS_PATH.search(text):
                bad.append(f.relative_to(ROOT).as_posix())
    assert not bad, f"样例项目含本机绝对路径：{bad}"


def test_sample_project_has_no_machine_package_lock():
    assert not [f for f in (ROOT / "environments").rglob("*") if f.is_file()]
