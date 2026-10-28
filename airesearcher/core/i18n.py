"""界面文字的中英文（详细设计 1 第 9.6 节）：消息代码 + 参数，按请求的语言显示。

    Msg(code="blocking.approval", params={"kind": "plan", ...})   # 存/传代码和参数
    render(msg)                                                   # 按当前请求的语言变成文字

当前语言由后台在每个请求开始时设置（?lang= 优先，其次 Accept-Language，默认 zh），
主干代码直接调用 render() / t()，不用一层层传 lang。只有主干自己的文字在这里登记；
各阶段写的文字（进度标签、问题、错误）暂时原样返回，见文档 9.6 的清单。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from pydantic import BaseModel

LANGS = ("zh", "en")
DEFAULT_LANG = "zh"

_lang: ContextVar[str] = ContextVar("air_lang", default=DEFAULT_LANG)


class Msg(BaseModel):
    """一条可翻译的文字：代码 + 参数。参数里以 state./kind. 开头的代码值也会被翻译（见 render）。"""

    code: str
    params: dict[str, Any] = {}


# code → {语言: 模板}。模板用 str.format 的 {参数名}；缺英文时退回中文。
MESSAGES: dict[str, dict[str, str]] = {
    # 项目状态（与详细设计 5 第 7 节一致）
    "state.IdeaDrafting": {"zh": "选题中", "en": "Selecting a research idea"},
    "state.IdeaPending": {"zh": "等待审批 idea", "en": "Waiting for idea approval"},
    "state.PlanDrafting": {"zh": "制定实验计划", "en": "Drafting the experiment plan"},
    "state.PlanPending": {"zh": "等待审批计划", "en": "Waiting for plan approval"},
    "state.Executing": {"zh": "实验执行中", "en": "Running experiments"},
    "state.Analyzing": {"zh": "分析结果", "en": "Analyzing results"},
    "state.LogPending": {"zh": "等待审批过程日志", "en": "Waiting for process log approval"},
    "state.Writing": {"zh": "撰写论文", "en": "Writing the paper"},
    "state.ManuscriptPending": {"zh": "等待审批最终手稿", "en": "Waiting for manuscript approval"},
    "state.Completed": {"zh": "已完成", "en": "Completed"},
    "state.Paused": {"zh": "已暂停", "en": "Paused"},
    "state.BudgetExhausted": {"zh": "预算已用尽", "en": "Budget exhausted"},
    "state.Failed": {"zh": "出错停止", "en": "Stopped with an error"},
    # 审批类型
    "kind.idea": {"zh": "idea", "en": "idea"},
    "kind.plan": {"zh": "实验计划", "en": "experiment plan"},
    "kind.log": {"zh": "过程日志", "en": "process log"},
    "kind.manuscript": {"zh": "最终手稿", "en": "final manuscript"},
    # 项目状态页的“卡在哪里”（ProjectStatus.blocking）
    "blocking.approval": {
        "zh": "等待你审批「{kind}」（{artifact_id} v{version}）",
        "en": "Waiting for your approval of the {kind} ({artifact_id} v{version})",
    },
    "blocking.question": {"zh": "等待你回答问题：{text}", "en": "Waiting for your answer: {text}"},
    "blocking.state": {"zh": "{state}", "en": "{state}"},
    "blocking.state_reason": {"zh": "{state}：{reason}", "en": "{state}: {reason}"},
    # 引擎自己提的问题：审批被拒绝后选择重写还是结束
    "question.rejected": {
        "zh": "你拒绝了「{kind}」（{artifact_id} v{version}）。接下来怎么做？",
        "en": "You rejected the {kind} ({artifact_id} v{version}). What next?",
    },
    "question.rejected.redo": {
        "zh": "回到起草状态重写（会参考你的拒绝意见）",
        "en": "Go back and redraft it (your rejection comment will be taken into account)",
    },
    "question.rejected.end": {"zh": "结束项目", "en": "End the project"},
    # 后台 API 的错误提示
    "error.invalid_params": {"zh": "参数不合法", "en": "Invalid parameters"},
    "error.project_not_found": {"zh": "找不到项目 {pid}", "en": "Project {pid} not found"},
    "error.need_idea": {"zh": "请填写研究想法（idea_text）", "en": "Please enter a research idea (idea_text)"},
    "error.bad_budget": {
        "zh": "预算 {category} 应为 {{soft, hard}} 数字：{detail}",
        "en": "Budget {category} must be {{soft, hard}} numbers: {detail}",
    },
    "error.budget_exceeded": {
        "zh": "预算 {category} 已用 {used}，达到上限 {hard}，不再发起新的模型调用",
        "en": "Budget {category} used {used} has reached its limit {hard}; no further model calls",
    },
    "error.local_exec_needs_real": {
        "zh": "allow_local_execution 只能和 mode=real 一起使用（假实现不会真的训练）",
        "en": "allow_local_execution requires mode=real (fake stages never train for real)",
    },
}


def current_lang() -> str:
    return _lang.get()


def set_lang(lang: str) -> None:
    _lang.set(lang if lang in LANGS else DEFAULT_LANG)


@contextmanager
def use_lang(lang: str) -> Iterator[None]:
    token = _lang.set(lang if lang in LANGS else DEFAULT_LANG)
    try:
        yield
    finally:
        _lang.reset(token)


def pick_lang(query: str | None = None, accept_language: str | None = None) -> str:
    """?lang=en 优先；否则按 Accept-Language（如 "en-US,en;q=0.9,zh;q=0.8"）的权重选第一个支持的语言。"""
    if query and query.split("-")[0].lower() in LANGS:
        return query.split("-")[0].lower()
    ranked: list[tuple[float, int, str]] = []
    for i, part in enumerate((accept_language or "").split(",")):
        tag, _, q = part.strip().partition(";q=")
        try:
            weight = float(q) if q else 1.0
        except ValueError:
            weight = 0.0
        ranked.append((-weight, i, tag.split("-")[0].lower()))
    for _, _, tag in sorted(ranked):
        if tag in LANGS:
            return tag
    return DEFAULT_LANG


def t(code: str, lang: str | None = None, **params: Any) -> str:
    """按代码取文字。没登记的代码原样返回（不报错）。"""
    lang = lang or current_lang()
    table = MESSAGES.get(code)
    if table is None:
        return code
    tpl = table.get(lang) or table[DEFAULT_LANG]
    shown = {k: t(v, lang) if isinstance(v, str) and v.split(".")[0] in ("state", "kind") and v in MESSAGES else v
             for k, v in params.items()}
    try:
        return tpl.format(**shown)
    except (KeyError, IndexError, ValueError):
        return tpl


def render(msg: Msg | None, lang: str | None = None) -> str | None:
    return None if msg is None else t(msg.code, lang, **msg.params)
