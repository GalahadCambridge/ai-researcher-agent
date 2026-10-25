"""项目状态与转换表（详细设计 1 第 5.2 节）。

D1 的落点：LLM 只能“请求”转换（Advance / NeedsApproval），能否转换由这张表决定。
"""

from __future__ import annotations

from airesearcher.core.i18n import DEFAULT_LANG, MESSAGES
from airesearcher.core.models.common import ProjectState as S

ACTIVE_STATES = frozenset({S.IdeaDrafting, S.PlanDrafting, S.Executing, S.Analyzing, S.Writing})
PENDING_STATES = frozenset({S.IdeaPending, S.PlanPending, S.LogPending, S.ManuscriptPending})
WAITING_STATES = frozenset(set(S) - ACTIVE_STATES)

# NeedsApproval 的 kind 必须与当前状态匹配
APPROVAL_KIND_FOR_STATE = {
    S.IdeaDrafting: "idea",
    S.PlanDrafting: "plan",
    S.Analyzing: "log",
    S.Writing: "manuscript",
}
PENDING_FOR_KIND = {"idea": S.IdeaPending, "plan": S.PlanPending, "log": S.LogPending,
                    "manuscript": S.ManuscriptPending}
DRAFTING_FOR_KIND = {"idea": S.IdeaDrafting, "plan": S.PlanDrafting, "log": S.Analyzing,
                     "manuscript": S.Writing}
KIND_FOR_PENDING = {v: k for k, v in PENDING_FOR_KIND.items()}

# 阶段可以请求的自主转换（表中“由阶段触发”的 Advance 行）
ALLOWED_ADVANCES = frozenset({
    (S.Executing, S.Analyzing),
    (S.Analyzing, S.Executing),
    (S.Analyzing, S.PlanDrafting),
    (S.Analyzing, S.IdeaDrafting),
    (S.Analyzing, S.Writing),
})

# 用户审批决定后的下一状态；None 表示“转 Paused 并由引擎提问 redo/end”
DECISION_NEXT: dict[tuple[str, str], S | None] = {
    ("idea", "approved"): S.PlanDrafting,
    ("idea", "changes_requested"): S.IdeaDrafting,
    ("idea", "rejected"): None,
    ("plan", "approved"): S.Executing,
    ("plan", "changes_requested"): S.PlanDrafting,
    ("plan", "rejected"): None,
    ("log", "approved"): S.Analyzing,
    ("log", "changes_requested"): S.Analyzing,
    ("log", "rejected"): S.Analyzing,
    ("manuscript", "approved"): S.Completed,
    ("manuscript", "changes_requested"): S.Writing,
    ("manuscript", "rejected"): None,
}

# 用户操作允许的起始状态
PAUSABLE = ACTIVE_STATES
RESUMABLE = frozenset({S.Paused, S.BudgetExhausted, S.Failed})
REOPENABLE = frozenset({S.Completed})

# 中文显示（与详细设计 5 第 7 节一致）；文字登记在 core/i18n.py，给界面的英文也在那里
STATE_LABELS = {s: MESSAGES[f"state.{s.value}"][DEFAULT_LANG] for s in S}
KIND_LABELS = {k: MESSAGES[f"kind.{k}"][DEFAULT_LANG] for k in PENDING_FOR_KIND}


def advance_allowed(frm: S, to: S) -> bool:
    return (frm, to) in ALLOWED_ADVANCES
