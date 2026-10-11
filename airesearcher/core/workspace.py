"""项目工作区：目录创建、AIR_HOME、全局注册表、工作区 git（详细设计 1 第 3.2、3.8 节）。"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
import threading
from pathlib import Path

from .fsutil import atomic_write_json, now, rand_hex, read_json

PROJECT_SUBDIRS = [
    "approvals", "questions", "idea", "plan", "src", "configs", "environments", "templates", "runs",
    "artifacts", "paper", "logs", ".archive", ".state/checkpoints", ".llm",
]

# 工作区 git 只跟踪 src/ 和 configs/。规则写在 .git/info/exclude（而不是项目根目录的 .gitignore），
# 这样把项目目录复制进别的 git 仓库（如 fixtures/sample_project/）时不会影响那个仓库。
GIT_EXCLUDE = "/*\n!/src/\n!/configs/\n"

_REGISTRY_LOCK = threading.Lock()


def air_home() -> Path:
    return Path(os.environ.get("AIR_HOME", Path.home() / "air")).expanduser().resolve()


def repo_root() -> Path:
    """代码仓库根目录（tasks/、skills/、configs/ 所在位置）。可用 AIR_REPO 覆盖。"""
    env = os.environ.get("AIR_REPO")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def resolve_task_dir(task: str) -> Path:
    """project.yaml 的 task（如 "tasks/smoke"）→ 任务目录。先看绝对路径，再看当前目录，最后看仓库根目录。"""
    p = Path(task).expanduser()
    candidates = [p] if p.is_absolute() else [Path.cwd() / p, repo_root() / p]
    for c in candidates:
        if (c / "task.yaml").exists():
            return c.resolve()
    raise FileNotFoundError(f"找不到任务配置 {task}/task.yaml")


def new_project_id() -> str:
    return f"p-{now():%Y%m%d}-{rand_hex(4)}"


# ---------- 全局注册表 ~/air/projects.json：只记录 project_id → 路径 ----------


def registry_path(home: Path | None = None) -> Path:
    return (home or air_home()) / "projects.json"


def load_registry(home: Path | None = None) -> dict[str, str]:
    p = registry_path(home)
    if not p.exists():
        return {}
    return dict(read_json(p))


def register_project(project_id: str, root: Path, home: Path | None = None) -> None:
    with _REGISTRY_LOCK:
        reg = load_registry(home)
        reg[project_id] = str(Path(root).resolve())
        atomic_write_json(registry_path(home), reg)


def init_project_dirs(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for d in PROJECT_SUBDIRS:
        (root / d).mkdir(parents=True, exist_ok=True)


class Workspace:
    """工作区 git 的封装。机器上没有 git 时，所有操作退化为空操作（commit 返回 "nogit"）。"""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self._lock = threading.RLock()

    @property
    def has_git(self) -> bool:
        return shutil.which("git") is not None

    def _git(self, *args: str, author: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
        name = author or "system"
        cmd = [
            "git", "-c", f"user.name={name}", "-c", "user.email=air@localhost", "-c", "commit.gpgsign=false",
            *args,
        ]
        return subprocess.run(cmd, cwd=self.root, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", check=check)

    def init_git(self) -> None:
        if not self.has_git:
            return
        for d in ("src", "configs"):
            keep = self.root / d / ".gitkeep"
            keep.parent.mkdir(parents=True, exist_ok=True)
            keep.touch()
        if not (self.root / ".git").exists():
            self._git("init", "-q")
        exclude = self.root / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(GIT_EXCLUDE, encoding="utf-8")
        self.commit("初始化工作区", author="system")

    def head(self) -> str | None:
        if not self.has_git or not (self.root / ".git").exists():
            return None
        r = self._git("rev-parse", "HEAD", check=False)
        return r.stdout.strip() if r.returncode == 0 else None

    def dirty(self) -> bool:
        return bool(self.changed_files())

    def changed_files(self) -> list[str]:
        """还没提交的改动（只看 src/、configs/，其他目录不进 git）。"""
        if not self.has_git or not (self.root / ".git").exists():
            return []
        r = self._git("status", "--porcelain", "--untracked-files=all", check=False)
        return [line[3:].strip().strip('"') for line in r.stdout.splitlines() if len(line) > 3]

    def commit(self, message: str, author: str = "agent:experiment") -> str:
        """仅当有变化时提交，返回 commit sha。

        用户手工改动由引擎在每一步之前单独以 human-edited 提交（engine._commit_workspace），
        所以阶段调用这里时，提交里只有阶段自己的改动。
        """
        if not self.has_git:
            return "nogit"
        with self._lock:
            if not (self.root / ".git").exists():
                self.init_git()
            self._git("add", "-A")
            if self._git("diff", "--cached", "--quiet", check=False).returncode != 0:
                self._git("commit", "-q", "-m", message, author=author)
            return self.head() or "nogit"

    def export_src(self, commit: str, dest: Path) -> None:
        """把某个 commit 的 src/ 内容导出到 dest（运行用的代码快照）。"""
        dest.mkdir(parents=True, exist_ok=True)
        if commit == "nogit" or not self.has_git:
            shutil.copytree(self.root / "src", dest, dirs_exist_ok=True)
            return
        archive = subprocess.run(
            ["git", "archive", "--format=tar", f"{commit}:src"], cwd=self.root, capture_output=True, check=True
        )
        # 用 Python 自带的 tarfile 解包（Windows 上不一定有 tar 命令）
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tf:
            tf.extractall(dest, filter="data")
