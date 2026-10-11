"""项目对象：把档案、事件、审批、问题、预算、权限、工作区绑定到一个项目目录（详细设计 1 第 4 节）。

    proj = Project.open(path)
    proj.archive.put(...)

阶段内部通过 ctx.archive 等访问，不需要自己打开项目。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .approvals import Approvals
from .archive import Archive
from .budget import Budget
from .errors import NotFound
from .events import Events
from .fsutil import now
from .models.project import ProjectConfig
from .permissions import Permissions
from .questions import Questions
from .requests import RequestLog
from .workspace import Workspace, air_home, init_project_dirs, new_project_id, register_project


def _dump_config(cfg: ProjectConfig) -> str:
    return yaml.safe_dump(cfg.model_dump(mode="json"), allow_unicode=True, sort_keys=False, width=100)


class Project:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        if not (self.root / "project.yaml").exists():
            raise NotFound(f"{self.root} 不是项目目录（没有 project.yaml）")
        self.config = self._read_config()
        self.events = Events(self.root)
        self.archive = Archive(self.root, self.events)
        self.approvals = Approvals(self.root, self.events, self.archive)
        self.questions = Questions(self.root, self.events)
        self.budget = Budget(self.root, self.events, lambda: self.config.budget)
        self.permissions = Permissions(self.root, self.events, lambda: self.config.permissions)
        self.workspace = Workspace(self.root)
        self.requests = RequestLog(self.root / ".state" / "requests.jsonl")

    @property
    def project_id(self) -> str:
        return self.config.project_id

    def _read_config(self) -> ProjectConfig:
        path = self.root / "project.yaml"
        self._config_mtime = path.stat().st_mtime_ns
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return ProjectConfig.model_validate(data)

    def maybe_reload_config(self) -> ProjectConfig:
        """project.yaml 在磁盘上被改过（例如用户手工修改）时重新读取。"""
        try:
            if (self.root / "project.yaml").stat().st_mtime_ns != self._config_mtime:
                self.config = self._read_config()
        except (OSError, ValueError):
            pass
        return self.config

    def reload_config(self) -> ProjectConfig:
        self.config = self._read_config()
        return self.config

    def save_config(self, cfg: ProjectConfig, summary: str = "修改项目配置", actor: str = "system") -> None:
        """project.yaml 只允许主干写。每次修改都登记新版本并写事件。"""
        self.config = cfg
        self._config_mtime = -1
        self.archive.put("project.yaml", "project", _dump_config(cfg), producer="system", note=summary)
        self.events.append("project.config_changed", summary, actor=actor)

    @classmethod
    def open(cls, root: Path | str) -> Project:
        return cls(Path(root))

    @classmethod
    def create(
        cls,
        *,
        goal: str,
        task: str,
        title: str | None = None,
        home: Path | None = None,
        root: Path | None = None,
        project_id: str | None = None,
        register: bool = True,
        **fields: Any,
    ) -> Project:
        """创建项目目录、写 project.yaml、初始化工作区 git、登记到全局注册表。"""
        home = home or air_home()
        pid = project_id or new_project_id()
        root = Path(root) if root else home / "projects" / pid
        if (root / "project.yaml").exists():
            raise FileExistsError(f"{root} 已经是一个项目")
        init_project_dirs(root)
        first_line = goal.strip().splitlines()[0] if goal.strip() else pid
        cfg = ProjectConfig.model_validate(
            {"project_id": pid, "title": title or first_line[:40], "created_at": now(), "goal": goal,
             "task": task, **{k: v for k, v in fields.items() if v is not None}}
        )
        (root / "project.yaml").write_text(_dump_config(cfg), encoding="utf-8")
        proj = cls(root)
        proj.workspace.init_git()
        proj.archive.put("project.yaml", "project", root / "project.yaml", producer="system", note="创建项目")
        proj.events.append(
            "project.created", f"创建项目：{cfg.title}", actor="user", data={"project_id": pid, "task": task}
        )
        if register:
            register_project(pid, root, home)
        return proj
