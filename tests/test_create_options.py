"""建项目的运行模式、预算部分覆盖、默认值接口、事件取最新 N 条（PR #13 审查里主干承诺的改动）。"""

from __future__ import annotations

import uuid

import pytest
import yaml
from fastapi.testclient import TestClient

from airesearcher.cli import main as cli
from airesearcher.core.fsutil import iter_jsonl_reverse
from airesearcher.core.models.project import ProjectConfig
from airesearcher.server.app import create_app
from airesearcher.stages import real_stage_names

from .test_cli import runner


@pytest.fixture
def client(air_home):
    # 不启动引擎：mode=real 的项目只看 project.yaml，不真的调用大模型
    with TestClient(create_app(home=air_home, start_engines=False)) as c:
        yield c


def new(c, **body):
    return c.post("/api/projects", json={"idea_text": "测试想法", "request_id": str(uuid.uuid4()), **body})


def config(c, pid) -> dict:
    root = c.app.state.manager.get(pid).root
    return yaml.safe_load((root / "project.yaml").read_text(encoding="utf-8"))


def test_default_mode_is_fake(client):
    r = new(client)
    assert r.status_code == 200, r.text
    cfg = config(client, r.json()["project_id"])
    assert cfg["dev"]["stage_impl"] == {}
    assert "llm_backend" not in cfg["experiment"]
    assert "allow_local_execution" not in cfg["experiment"]


def test_real_mode_sets_stage_impl_and_backend(client):
    r = new(client, mode="real")
    assert r.status_code == 200, r.text
    cfg = config(client, r.json()["project_id"])
    assert cfg["dev"]["stage_impl"] == {name: "real" for name in real_stage_names()}
    assert {"idea", "plan", "experiment"} <= set(real_stage_names())
    assert cfg["experiment"]["llm_backend"] == "api"
    assert "allow_local_execution" not in cfg["experiment"]  # 本地训练不默认打开
    assert cfg["experiment"]["trial_timeout_s"] == 300  # 其他实验默认值保留


def test_real_mode_local_execution_and_dev_override(client):
    r = new(client, mode="real", allow_local_execution=True, dev={"stage_impl": {"plan": "fake"}})
    assert r.status_code == 200, r.text
    cfg = config(client, r.json()["project_id"])
    assert cfg["experiment"]["allow_local_execution"] is True
    assert cfg["dev"]["stage_impl"]["plan"] == "fake"  # 显式的 dev.stage_impl 优先于 mode
    assert cfg["dev"]["stage_impl"]["idea"] == "real"


def test_local_execution_requires_real_mode(client):
    r = new(client, allow_local_execution=True)
    assert r.status_code == 422
    assert r.json()["error"]["message_code"] == "error.local_exec_needs_real"
    assert new(client, mode="nope").status_code == 422


def test_budget_partial_override(client):
    r = new(client, budget={"llm_usd": {"soft": 1, "hard": 2}})
    assert r.status_code == 200, r.text
    cfg = config(client, r.json()["project_id"])
    default = ProjectConfig.model_fields["budget"].get_default(call_default_factory=True)
    assert cfg["budget"]["llm_usd"] == {"soft": 1.0, "hard": 2.0}
    for cat in default.keys() - {"llm_usd"}:  # 没传的类别保留默认值
        assert cfg["budget"][cat] == default[cat].model_dump()
    bad = new(client, budget={"llm_usd": {"hard": "很多"}})
    assert bad.status_code == 422 and bad.json()["error"]["message_code"] == "error.bad_budget"


def test_defaults_endpoint(client):
    d = client.get("/api/defaults").json()
    assert d["mode"] == "fake" and d["allow_local_execution"] is False and d["task"] == "tasks/smoke"
    assert d["budget"]["llm_usd"] == {"soft": 4.0, "hard": 5.0}
    assert d["real_stages"] == real_stage_names()


def test_cli_new_passes_mode(monkeypatch):
    sent = []

    def fake_api(method, path, **kw):
        if method == "POST":
            sent.append(kw["json"])
            return {"project_id": "p1", "root": "/tmp/p1"}
        return {}

    monkeypatch.setattr(cli, "api", fake_api)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli, "print_status", lambda st: None)
    r = runner.invoke(cli.app, ["new", "--idea", "x", "--mode", "real", "--allow-local-execution"])
    assert r.exit_code == 0, r.output
    assert sent[0]["mode"] == "real" and sent[0]["allow_local_execution"] is True
    r = runner.invoke(cli.app, ["new", "--idea", "x"])
    assert sent[1]["mode"] == "fake" and sent[1]["allow_local_execution"] is False
    assert runner.invoke(cli.app, ["new", "--idea", "x", "--mode", "nope"]).exit_code == 1


# ---------------------------------------------------------------- 事件取最新 N 条
def test_events_tail(client):
    pid = new(client).json()["project_id"]
    ev = client.app.state.manager.get(pid).events
    for i in range(30):
        ev.append("test.even" if i % 2 == 0 else "test.odd", f"第 {i} 条事件", actor="system")
    last = ev.last_seq

    tail = ev.query(tail=5)
    assert [e.seq for e in tail] == list(range(last - 4, last + 1))  # 正序
    odd = ev.query(types=["test.odd"], tail=3)
    assert [e.summary for e in odd] == ["第 25 条事件", "第 27 条事件", "第 29 条事件"]
    assert [e.seq for e in ev.query(tail=100, after_seq=last - 2)] == [last - 1, last]
    assert len(ev.query(tail=1000)) == last  # 不够 N 条时返回全部

    r = client.get(f"/api/projects/{pid}/events", params={"tail": 3, "type": "test.*"})
    assert [e["summary"] for e in r.json()] == ["第 27 条事件", "第 28 条事件", "第 29 条事件"]
    assert client.get(f"/api/projects/{pid}/events", params={"tail": 0}).status_code == 422


def test_iter_jsonl_reverse_chunks_and_bad_lines(tmp_path):
    p = tmp_path / "log.jsonl"
    rows = [f'{{"i": {i}, "s": "中文内容{i}"}}' for i in range(20)]
    p.write_bytes(("\n".join(rows[:10]) + "\n不是json\n" + "\n".join(rows[10:]) + "\n{\"half\":").encode("utf-8"))
    for chunk in (1, 7, 64, 1 << 16):  # 块边界落在多字节字符中间也没关系
        assert [r["i"] for r in iter_jsonl_reverse(p, chunk_size=chunk)] == list(range(19, -1, -1))
    assert list(iter_jsonl_reverse(tmp_path / "missing.jsonl")) == []
