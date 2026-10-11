"""端到端（HTTP API，引擎线程真实运行）：创建 → 4 次审批 → Completed；去重与过期；问题回答。"""

from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient

from airesearcher.server.app import create_app

from .api_helpers import create, decide, wait_state, wait_until


@pytest.fixture
def client(air_home):
    with TestClient(create_app(home=air_home, engine_kwargs={"retry_delay": 0})) as c:
        yield c


def test_api_end_to_end(client):
    c = client
    pid = create(c)
    st = wait_state(c, pid, "IdeaPending")
    assert st["next_actions"][0].startswith("approve:") and "idea" in st["blocking_reason"]
    aid = st["pending_approvals"][0]["approval_id"]
    detail = c.get(f"/api/projects/{pid}/approvals/{aid}").json()
    assert detail["target_content"].startswith("---") and detail["extra"]

    # 同一 request_id 重复提交：返回第一次的结果（200）；之后不同 request_id → 409
    _, r1 = decide(c, pid, request_id="same-req")
    assert r1.status_code == 200 and r1.json()["status"] == "approved"
    r2 = c.post(f"/api/projects/{pid}/approvals/{aid}/decision",
                json={"decision": "approved", "expected_sha256": detail["approval"]["target"]["sha256"],
                      "request_id": "same-req"})
    assert r2.status_code == 200 and r2.json()["request_ids"] == ["same-req"]
    r3 = c.post(f"/api/projects/{pid}/approvals/{aid}/decision",
                json={"decision": "rejected", "comment": "x", "expected_sha256": detail["approval"]["target"]["sha256"],
                      "request_id": "other-req"})
    assert r3.status_code == 409 and r3.json()["error"]["code"] == "STALE_VERSION"

    wait_state(c, pid, "PlanPending")
    # 过期的 expected_sha256 → 409，审批被标为 superseded，阶段重新提交新的审批
    old_aid, r = decide(c, pid, sha="0" * 64)
    assert r.status_code == 409
    # 引擎需要片刻才能让阶段重新提交：等到出现新的待审批
    st = wait_until(c, pid, lambda st: st["pending_approvals"] and st["pending_approvals"][0]["approval_id"] != old_aid,
                    what="新的计划审批")
    assert st["state"] == "PlanPending"
    decide(c, pid)

    wait_state(c, pid, "LogPending", timeout=90)
    runs = c.get(f"/api/projects/{pid}/runs").json()
    assert len(runs) == 9 and {r["state"] for r in runs} == {"succeeded"}
    run = c.get(f"/api/projects/{pid}/runs/{runs[0]['run_id']}").json()
    assert run["metrics"][0]["name"] == "score"
    logs = c.get(f"/api/projects/{pid}/runs/{runs[0]['run_id']}/logs").json()
    assert logs["eof"] and "smoke:" in logs["text"]
    decide(c, pid)

    wait_state(c, pid, "ManuscriptPending")
    paper = c.get(f"/api/projects/{pid}/paper").json()
    assert paper["review_summary"]["counts"]["blocker"] == 0
    claims = c.get(f"/api/projects/{pid}/claims").json()
    trace = c.get(f"/api/projects/{pid}/claims/{claims['claims'][0]['claim_id']}/trace").json()
    assert trace["runs"] and trace["aggregates"][0]["recomputed"] == trace["aggregates"][0]["rendered"]
    assert c.get(f"/api/projects/{pid}/paper/pdf").headers["content-type"] == "application/pdf"
    decide(c, pid)

    st = wait_state(c, pid, "Completed")
    assert st["next_actions"] == ["reopen"]
    events = c.get(f"/api/projects/{pid}/events", params={"type": "approval.*"}).json()
    assert len([e for e in events if e["type"] == "approval.decided"]) == 4
    assert c.get(f"/api/projects/{pid}/artifacts", params={"kind": "aggregate"}).json()
    lineage = c.get(f"/api/projects/{pid}/artifacts/lineage", params={"artifact_id": "paper"}).json()
    assert lineage["parents"]
    assert c.get("/api/projects").json()[0]["state"] == "Completed"
    # 已完成后不能暂停
    r = c.post(f"/api/projects/{pid}/actions", json={"action": "pause", "request_id": "p1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_STATE"


def test_api_question_flow(client):
    c = client
    pid = create(c, dev={"smoke_fail": {"E3-seed=2": "exit1"}})
    wait_state(c, pid, "IdeaPending")
    decide(c, pid)
    wait_state(c, pid, "PlanPending")
    decide(c, pid)
    st = wait_state(c, pid, "Paused", timeout=90)
    q = st["pending_question"]
    assert q and "answer:" + q["question_id"] in st["next_actions"]
    # 有待回答问题时 resume → 409
    r = c.post(f"/api/projects/{pid}/actions", json={"action": "resume", "request_id": str(uuid.uuid4())})
    assert r.status_code == 409
    # 不存在的选项 → 422
    r = c.post(f"/api/projects/{pid}/question/answer",
               json={"question_id": q["question_id"], "choice": "nope", "request_id": "a0"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION"
    r = c.post(f"/api/projects/{pid}/question/answer",
               json={"question_id": q["question_id"], "choice": "retry", "request_id": "a1"})
    assert r.status_code == 200
    # 重复回答（同一 request_id）不报错；过期问题 → 409
    assert c.post(f"/api/projects/{pid}/question/answer",
                  json={"question_id": q["question_id"], "choice": "retry", "request_id": "a1"}).status_code == 200
    assert c.post(f"/api/projects/{pid}/question/answer",
                  json={"question_id": q["question_id"], "choice": "skip", "request_id": "a2"}).status_code == 409
    wait_state(c, pid, "LogPending", timeout=90)
    runs = c.get(f"/api/projects/{pid}/runs").json()
    assert len(runs) == 10
    retried = [r for r in runs if r["retry_of"]]
    assert len(retried) == 1 and retried[0]["state"] == "succeeded"


def test_pause_resume_and_reject(client):
    c = client
    pid = create(c)
    wait_state(c, pid, "IdeaPending")
    _, r = decide(c, pid, "rejected", "方向不对")
    st = wait_state(c, pid, "Paused")
    q = st["pending_question"]
    assert {o["id"] for o in q["options"]} == {"redo", "end"}
    c.post(f"/api/projects/{pid}/question/answer", json={"question_id": q["question_id"], "choice": "redo",
                                                        "request_id": "r1"})
    st = wait_state(c, pid, "IdeaPending")
    detail = c.get(f"/api/projects/{pid}/approvals/{st['pending_approvals'][0]['approval_id']}").json()
    assert "方向不对" in detail["target_content"]
    decide(c, pid)
    wait_state(c, pid, ["PlanDrafting", "PlanPending"])
    r = c.post(f"/api/projects/{pid}/actions", json={"action": "cancel", "request_id": "c1"})
    assert r.json()["state"] == "Failed"
    r = c.post(f"/api/projects/{pid}/actions", json={"action": "resume", "request_id": "c2"})
    assert r.status_code == 200
    wait_state(c, pid, "PlanPending")


def test_budget_patch_and_errors(client):
    c = client
    pid = create(c)
    r = c.patch(f"/api/projects/{pid}/budget", json={"llm_usd": {"hard": 8}, "request_id": "b1"})
    assert r.status_code == 200 and r.json()["categories"]["llm_usd"]["hard"] == 8
    assert c.get("/api/projects/p-nope").json()["error"]["code"] == "NOT_FOUND"
    assert c.get(f"/api/projects/{pid}/files", params={"path": "../../etc/passwd"}).status_code == 403
    assert c.get("/api/tasks").json()[0]["name"] == "smoke"


def test_status_is_a_consistent_snapshot(client):
    """有待审批时状态一定是“等待审批”——状态接口不能读到引擎改到一半的样子（CI 上出现过）。"""
    c = client
    pid = create(c)
    seen = 0
    t0 = time.time()
    approved = set()
    while time.time() - t0 < 60:
        st = c.get(f"/api/projects/{pid}").json()
        if st["pending_approvals"]:
            assert st["state"].endswith("Pending"), st
            seen += 1
            aid = st["pending_approvals"][0]["approval_id"]
            if aid not in approved and len(approved) < 2:
                approved.add(aid)
                decide(c, pid)
        if st["state"] == "LogPending":
            break
    assert seen and len(approved) == 2
