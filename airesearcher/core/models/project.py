"""项目配置 project.yaml（详细设计 1 第 3.10 节）。

各阶段新增配置项时，先在文档 3.10 的表中登记字段名，再在这里加字段。未登记的字段也能读进来
（extra="allow"），不会报错，方便开发中试验。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Limit(BaseModel):
    model_config = ConfigDict(extra="allow")
    soft: float | None = None
    hard: float | None = None


def _default_budget() -> dict[str, Limit]:
    return {
        "llm_usd": Limit(soft=4.0, hard=5.0),
        "wall_hours": Limit(hard=6),
        "gpu_hours": Limit(hard=2),
        "storage_gb": Limit(hard=5),
    }


MAX_LITERATURE_PAPERS = 20


class Limits(BaseModel):
    model_config = ConfigDict(extra="allow")
    search_papers: int = Field(default=20, ge=1, le=MAX_LITERATURE_PAPERS, strict=True)
    read_papers: int = Field(default=8, ge=0, le=MAX_LITERATURE_PAPERS, strict=True)
    reader_concurrency: int = 2
    max_rounds: int = 3
    max_repair_retries: int = 2


class NetworkPerm(BaseModel):
    allow: list[str] = Field(
        default_factory=lambda: [
            "api.semanticscholar.org", "export.arxiv.org", "arxiv.org",
            "huggingface.co", "cdn-lfs.huggingface.co",
            # 大模型接口（LLM 网关调用前会检查）；本地模型（如 Ollama）走 localhost
            "api.anthropic.com", "api.openai.com", "generativelanguage.googleapis.com", "localhost", "127.0.0.1",
        ]
    )


class ExecPerm(BaseModel):
    local: bool = True
    gpu_serial: bool = True
    max_parallel_cpu: int = 2


class WritePerm(BaseModel):
    roots: list[str] = Field(default_factory=lambda: [".", "${AIR_HOME}/data"])


class Permissions(BaseModel):
    network: NetworkPerm = Field(default_factory=NetworkPerm)
    exec: ExecPerm = Field(default_factory=ExecPerm)
    write: WritePerm = Field(default_factory=WritePerm)
    publish: bool = False


class DevConfig(BaseModel):
    """开发用配置（主干）：选择每个阶段用真实实现还是假实现，以及假实现的参数。

    stage_impl 例：{"idea": "fake", "plan": "real", "experiment": "fake", "writing": "fake"}
    环境变量 AIR_STAGE_IMPL（如 "fake" 或 "idea=real,plan=fake"）优先于这里。
    """

    model_config = ConfigDict(extra="allow")
    stage_impl: dict[str, str] = Field(default_factory=dict)
    smoke_seconds: float = 2.0  # 假实验：每个冒烟运行跑几秒
    smoke_fail: dict[str, str] = Field(default_factory=dict)  # 假实验：{task_key: fail_mode}，用于演示提问
    poll_seconds: int = 2  # 假实验：等待运行时多久查一次
    compile: bool = True  # 假论文：有 latexmk 时是否真的编译


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    project_id: str
    title: str
    created_at: datetime
    goal: str
    constraints: dict[str, Any] = Field(default_factory=dict)
    task: str
    evidence_check: bool = True
    reading_mode: Literal["multi", "single"] = "multi"
    # 覆盖模型配置（结构同 configs/models.yaml：tiers / stages / roles / models / fallbacks）；
    # 旧写法 {fast: 模型名, strong: 模型名} 当作 tiers。见 llm/models.py
    models: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Limit] = Field(default_factory=_default_budget)
    limits: Limits = Field(default_factory=Limits)
    permissions: Permissions = Field(default_factory=Permissions)
    llm_cache: Literal["off", "record", "replay"] = "off"
    prompt_pins: dict[str, int] = Field(default_factory=dict)  # 固定提示版本 {prompt_id: version}，评价实验用
    template: dict | None = None
    literature: dict[str, Any] = Field(default_factory=lambda: {"year_from": 2021, "replay_snapshots": None})
    experiment: dict[str, Any] = Field(
        default_factory=lambda: {"trial_timeout_s": 300, "python": None, "prepare_timeout_s": 1800}
    )
    paper: dict[str, Any] = Field(
        default_factory=lambda: {
            "max_review_rounds": 2, "max_compile_fixes": 2, "review_build": False, "figure_style": "air",
        }
    )
    dev: DevConfig = Field(default_factory=DevConfig)
