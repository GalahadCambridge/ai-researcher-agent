"""界面文字中英文（详细设计 1 第 9.6 节）：消息代码 + 参数，按 ?lang= / Accept-Language 显示。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from airesearcher.core.i18n import MESSAGES, Msg, pick_lang, render, t, use_lang
from airesearcher.core.models.common import ProjectState
from airesearcher.engine.states import KIND_LABELS, STATE_LABELS
from airesearcher.server.app import create_app

from .api_helpers import create, decide, wait_state


def test_catalog_complete():
    for code, table in MESSAGES.items():
        assert table.get("zh") and table.get("en"), code
    assert STATE_LABELS[ProjectState.Executing] == "实验执行中" and KIND_LABELS["plan"] == "实验计划"
    assert all(f"state.{s.value}" in MESSAGES for s in ProjectState)


def test_pick_lang():
    assert pick_lang() == "zh"
    assert pick_lang(None, "en-US,en;q=0.9,zh;q=0.8") == "en"
    assert pick_lang(None, "fr-FR,zh-CN;q=0.9,en;q=0.8") == "zh"
    assert pick_lang(None, "zh;q=0.5,en;q=0.9") == "en"
    assert pick_lang("en", "zh-CN") == "en"
    assert pick_lang("xx", "en") == "en"  # 不支持的 lang 参数忽略


def test_render_translates_nested_codes():
    m = Msg(code="blocking.approval", params={"kind": "kind.plan", "artifact_id": "plan/x.md", "version": 2})
    assert render(m, "zh") == "等待你审批「实验计划」（plan/x.md v2）"
    assert render(m, "en") == "Waiting for your approval of the experiment plan (plan/x.md v2)"
    with use_lang("en"):
        assert t("state.Paused") == "Paused"
    assert t("state.Paused") == "已暂停"
    assert t("no.such.code") == "no.such.code"


def test_api_language(air_home):
    with TestClient(create_app(home=air_home, engine_kwargs={"retry_delay": 0})) as c:
        pid = create(c)
        wait_state(c, pid, "IdeaPending")
        zh = c.get(f"/api/projects/{pid}").json()
        assert zh["stage_label"] == "等待审批 idea" and zh["blocking_reason"].startswith("等待你审批")
        assert zh["blocking"]["code"] == "blocking.approval"
        en = c.get(f"/api/projects/{pid}", headers={"Accept-Language": "en-US,en;q=0.9"}).json()
        assert en["stage_label"] == "Waiting for idea approval"
        assert en["blocking_reason"].startswith("Waiting for your approval of the idea")
        assert c.get(f"/api/projects/{pid}?lang=en").json()["stage_label"] == "Waiting for idea approval"
        assert c.get("/api/projects?lang=en").json()[0]["stage_label"] == "Waiting for idea approval"

        # 引擎自己提的问题：拒绝 idea 后的“重写还是结束”
        decide(c, pid, "rejected", "不行")
        wait_state(c, pid, "Paused")
        q = c.get(f"/api/projects/{pid}/question?lang=en").json()
        assert q["text"].startswith("You rejected the idea") and q["options"][1]["label"] == "End the project"
        assert c.get(f"/api/projects/{pid}/question").json()["text"].startswith("你拒绝了「idea」")
        st = c.get(f"/api/projects/{pid}?lang=en").json()
        assert st["blocking_reason"].startswith("Waiting for your answer: You rejected")

        # 错误提示
        err = c.get("/api/projects/nope?lang=en").json()["error"]
        assert err == {"code": "NOT_FOUND", "message": "Project nope not found", "detail": {},
                       "message_code": "error.project_not_found", "params": {"pid": "nope"}}
        assert c.get("/api/projects/nope").json()["error"]["message"] == "找不到项目 nope"
        bad = c.post("/api/projects", json={}, headers={"Accept-Language": "en"}).json()["error"]
        assert bad["code"] == "VALIDATION" and bad["message"] == "Invalid parameters"
