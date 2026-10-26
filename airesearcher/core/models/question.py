"""待回答问题 questions/<question_id>.json（详细设计 1 第 3.7 节）。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, model_validator

from ..i18n import Msg
from .common import VersionRef

END_OPTION_ID = "end"  # 保留选项：引擎直接把项目转 Failed（“用户选择结束”），阶段收不到这个回答


class QuestionOption(BaseModel):
    id: str  # 机器可读值，如 "skip"、"retry"
    label: str  # 给人看的说明
    default: bool = False  # 无人值守时可自动选择的安全选项，最多一个
    msg: Msg | None = None  # 可选：label 的消息代码，API 按请求语言显示（见 core/i18n.py）


class Question(BaseModel):
    question_id: str = ""  # "q-0001"，由引擎在 ask() 时分配
    stage: str = ""  # 提问的阶段，由引擎填写
    text: str
    msg: Msg | None = None  # 可选：text 的消息代码，API 按请求语言显示（见 core/i18n.py）
    options: list[QuestionOption] = []
    allow_text: bool = False
    refs: list[VersionRef] = []
    status: Literal["pending", "answered", "withdrawn"] = "pending"
    created_at: datetime | None = None

    @model_validator(mode="after")
    def _check(self) -> Question:
        if not self.options and not self.allow_text:
            # 选项为空时必须允许文字回答
            self.allow_text = True
        if sum(1 for o in self.options if o.default) > 1:
            raise ValueError("最多一个 default 选项")
        ids = [o.id for o in self.options]
        if len(ids) != len(set(ids)):
            raise ValueError("选项 id 不能重复")
        return self


class Answer(BaseModel):
    question_id: str
    choice: str | None = None
    text: str = ""
    answered_by: str = "user"  # "user" | "auto-default(eval)"
    answered_at: datetime
    request_ids: list[str] = []


class QuestionRecord(BaseModel):
    """questions/<id>.json 的文件内容：问题与回答放在一起。"""

    question: Question
    answer: Answer | None = None
