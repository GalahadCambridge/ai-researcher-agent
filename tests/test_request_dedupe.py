"""同一 request_id 重复提交返回第一次的结果——后台重启后也一样（详细设计 1 第 9.1 节）。"""

from __future__ import annotations

import io
import zipfile

from fastapi.testclient import TestClient

from airesearcher.server.app import create_app

from .api_helpers import DEV, wait_state


def app(home):
    return TestClient(create_app(home=home, engine_kwargs={"retry_delay": 0}))


def body(rid):
    return {"idea_text": "比较基线、主方法和消融在冒烟任务上的得分", "task": "tasks/smoke", "dev": DEV,
            "request_id": rid}


def test_dedupe_survives_restart(air_home):
    with app(air_home) as c:
        pid = c.post("/api/projects", json=body("create-1")).json()["project_id"]
        wait_state(c, pid, "IdeaPending")
        assert c.post(f"/api/projects/{pid}/actions", json={"action": "cancel", "request_id": "act-1"}).json()[
            "state"] == "Failed"
        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w") as zf:
            zf.writestr("main.tex", "\\documentclass{article}")
        tpl = c.post(f"/api/projects/{pid}/templates", files={"file": ("t.zip", zbuf.getvalue())},
                     data={"request_id": "tpl-1"}).json()

    with app(air_home) as c:  # 后台重启：新的进程内状态，只有文件
        again = c.post("/api/projects", json=body("create-1"))
        assert again.status_code == 200 and again.json()["project_id"] == pid
        assert len(c.get("/api/projects").json()) == 1  # 没有建第二个项目
        r = c.post(f"/api/projects/{pid}/actions", json={"action": "cancel", "request_id": "act-1"})
        assert r.status_code == 200 and r.json()["state"] == "Failed"  # 没有报“已取消的项目不能再取消”
        r = c.post(f"/api/projects/{pid}/actions", json={"action": "cancel", "request_id": "act-2"})
        assert r.status_code == 409  # 新的请求照常校验
        r = c.post(f"/api/projects/{pid}/templates", files={"file": ("t.zip", zbuf.getvalue())},
                   data={"request_id": "tpl-1"})
        assert (r.json()["template_id"], r.json()["version"]) == (tpl["template_id"], tpl["version"])
        assert len(c.get(f"/api/projects/{pid}/templates").json()["templates"]) == 1


def test_edit_dedupe(sample_project, air_home):
    from airesearcher.core.fsutil import sha256_file
    from airesearcher.core.workspace import register_project

    p = sample_project
    register_project(p.project_id, p.root, home=air_home)
    path = p.root / "paper/sections/results.tex"
    req = {"artifact_id": "paper/sections/results.tex", "content": "% 用户改过\n", "base_sha256": sha256_file(path),
           "request_id": "edit-1"}
    with TestClient(create_app(home=air_home, start_engines=False)) as c:
        first = c.put(f"/api/projects/{p.project_id}/artifacts/content", json=req)
        assert first.status_code == 200
    with TestClient(create_app(home=air_home, start_engines=False)) as c:
        again = c.put(f"/api/projects/{p.project_id}/artifacts/content", json=req)
        assert again.status_code == 200 and again.json() == first.json()  # 不是 409“文件已被修改”
        other = c.put(f"/api/projects/{p.project_id}/artifacts/content", json={**req, "request_id": "edit-2"})
        assert other.status_code == 409
