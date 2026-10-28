"""图表 artifacts/figures/<fig_id>/（详细设计 4 第 3.1 节；论文同学起草）。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .common import VersionRef


class FigureSpec(BaseModel):
    fig_id: str
    kind: Literal["bar", "line", "point", "box"]
    aggregate_id: str
    x: str
    hue: str | None = None
    metric: str
    error: Literal["ci95", "std", "none"]
    title: str
    xlabel: str
    ylabel: str
    scale: float = 1.0
    log_x: bool = False
    caption: str
    facets: list[str] = Field(default_factory=list, max_length=8)


class Figure(BaseModel):
    fig_id: str
    spec: FigureSpec
    aggregate_ref: VersionRef
    run_ids: list[str]
    skill_ref: str
    script_sha256: str
    outputs: list[str]
    n_per_group: dict[str, int]
    created_at: datetime
