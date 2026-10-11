"""第二批 GUI 联调接口：SSE 全部消息类型、日志逐行推送、运行标签、模板、导出、回滚。"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from airesearcher.server.app import create_app
from airesearcher.server.sse import ProjectStream

from .api_helpers import create, decide, wait_state


@pytest.fixture
def client(air_home):
    with TestClient(create_app(home=air_home, engine_kwargs={"retry_delay": 0})) as c:
        yield c


def parse(messages):
    out = []
    for m in messages:
        fields = dict(line.split(": ", 1) for line in m.strip().splitlines())
        out.append((fields["event"], json.loads(fields["data"])))
    return out


def test_sse_message_types(client):
    c = client
    pid = create(c, dev={"smoke_seconds": 1.0})
    ps = ProjectStream(c.app.state.manager, pid)
    seen = parse(ps.poll())
    assert [e for e, _ in seen[:2]] == ["state", "budget"]  # 连接时先推完整状态和预算
    wait_state(c, pid, "IdeaPending")
    decide(c, pid)
    wait_state(c, pid, "PlanPending")
    decide(c, pid)
    t0 = time.time()
    while time.time() - t0 < 60:
        seen += parse(ps.poll())
        if c.get(f"/api/projects/{pid}").json()["state"] == "LogPending":
            break
        time.sleep(0.2)
    seen += parse(ps.poll())
    kinds = {e for e, _ in seen}
    assert {"state", "budget", "event", "approval", "run"} <= kinds
    approvals = [d for e, d in seen if e == "approval"]
    assert {"status": "pending"}.items() <= approvals[0].items()
    assert any(d["status"] == "approved" for d in approvals)
    run_status = {}
    for e, d in seen:
        if e == "run":
            run_status.setdefault(d["run_id"], []).append(d["status"])
    assert len(run_status) == 9
    assert all(s[-1] == "succeeded" for s in run_status.values())
    # 断线重连：带上次的事件 seq，只补发之后的事件
    last = max(json.loads(m.split("data: ", 1)[1])["seq"] for m in ProjectStream(c.app.state.manager, pid).poll()
               if m.startswith("id:"))
    again = parse(ProjectStream(c.app.state.manager, pid, last_seq=last).poll())
    assert not [d for e, d in again if e == "event"]


def test_sse_pushes_status_changes_without_events(monkeypatch):
    """包装器直接改 status.json（如 queued → running）没有事件：靠比对活动运行补推 run 消息。不依赖运行快慢。"""
    from types import SimpleNamespace

    from airesearcher.core.models.budget import BudgetStatus

    active: list = []
    final = {}
    project = SimpleNamespace(root="unused", events=SimpleNamespace(query=lambda after_seq, limit: []))
    status = lambda: SimpleNamespace(  # noqa: E731
        state=SimpleNamespace(value="Executing"), stage_label="x", blocking_reason=None,
        budget=BudgetStatus(categories={}), active_runs=list(active))
    m = SimpleNamespace(get=lambda pid: project, status=lambda pid: status())
    ps = ProjectStream(m, "p")
    run = lambda rid, state: SimpleNamespace(run_id=rid, state=SimpleNamespace(value=state))  # noqa: E731

    def runs_of(msgs):
        return [d for e, d in parse(msgs) if e == "run"]

    active[:] = [run("r1", "queued")]
    assert runs_of(ps.poll()) == [{"run_id": "r1", "status": "queued"}]
    assert runs_of(ps.poll()) == []  # 没变化不推
    active[:] = [run("r1", "running")]
    assert runs_of(ps.poll()) == [{"run_id": "r1", "status": "running"}]
    active[:] = []  # 结束了：从活动列表消失，读 status.json 得到最终状态
    final["r1"] = "succeeded"
    import airesearcher.server.sse as sse_mod

    monkeypatch.setattr(sse_mod.run_service, "load_run", lambda root, rid, with_metrics=False: SimpleNamespace(
        status=SimpleNamespace(state=SimpleNamespace(value=final[rid]))))
    assert runs_of(ps.poll()) == [{"run_id": "r1", "status": "succeeded"}]


def test_sse_derived_messages():
    from airesearcher.core.fsutil import now
    from airesearcher.core.models.event import Event
    from airesearcher.server.sse import derived

    def ev(type, data=None, run_id=None):
        return Event(seq=1, ts=now(), type=type, actor="x", summary="s", data=data or {},
                     run_id=run_id)

    assert derived(ev("question.asked", {"question_id": "q-1"})) == ("question", {"question_id": "q-1",
                                                                                  "status": "pending"})
    assert derived(ev("question.withdrawn", {"question_id": "q-1"}))[1]["status"] == "withdrawn"
    assert derived(ev("approval.decided", {"approval_id": "ap-1", "decision": "rejected"}))[1]["status"] == "rejected"
    assert derived(ev("run.labeled", {"label": "invalid"}, run_id="r-1"))[1] == {"run_id": "r-1",
                                                                                 "status": "labeled",
                                                                                 "label": "invalid"}
    assert derived(ev("stage.decision")) is None


class FakeLogs:
    """按调用次数返回日志的执行器替身：模拟“一行写了一半”和运行结束。"""

    def __init__(self, data: bytes, steps: list[tuple[int, bool]]):
        self.data, self.steps = data, steps  # 每次 logs() 时文件已写到第几个字节、运行是否已结束

    def logs(self, run_id, stream, offset):
        from airesearcher.runtime.executor import LogChunk

        size, ended = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        part = self.data[offset:size]
        end = offset + len(part)
        return LogChunk(text=part.decode("utf-8"), next_offset=end, eof=ended and end >= size)

    def status(self, run_id):
        from airesearcher.core.models.run import RunState, RunStatus

        return RunStatus(run_id=run_id, state=RunState.succeeded, host="h")


def test_log_stream_lines_and_resume():
    from airesearcher.server.sse import RunLogStream

    data = "第一行\nsecond li".encode() + b"ne\nlast"
    first = len("第一行\nsecond li".encode())
    ls = RunLogStream(FakeLogs(data, [(first, False), (len(data), True)]), "r-1")
    msgs, done = ls.poll()
    assert [d for _, d in parse(msgs)] == [{"text": "第一行"}] and not done  # 半行先不推
    msgs, done = ls.poll()
    assert parse(msgs) == [("line", {"text": "second line"}), ("line", {"text": "last"}),
                           ("end", {"run_id": "r-1", "status": "succeeded"})] and done
    # 断线重连：从第一行结束处继续，不重复
    resumed = RunLogStream(FakeLogs(data, [(len(data), True)]), "r-1", offset=len("第一行\n".encode()))
    assert [d["text"] for e, d in parse(resumed.poll()[0]) if e == "line"] == ["second line", "last"]


def test_log_stream_endpoint(client):
    c = client
    pid = create(c)
    wait_state(c, pid, "IdeaPending")
    decide(c, pid)
    wait_state(c, pid, "PlanPending")
    decide(c, pid)
    wait_state(c, pid, "LogPending", timeout=90)
    rid = c.get(f"/api/projects/{pid}/runs").json()[0]["run_id"]
    with c.stream("GET", f"/api/projects/{pid}/runs/{rid}/logs/stream") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    events = parse([m for m in body.split("\n\n") if m.strip()])
    assert events[-1] == ("end", {"run_id": rid, "status": "succeeded"})
    whole = c.get(f"/api/projects/{pid}/runs/{rid}/logs").json()["text"]
    assert [d["text"] for e, d in events if e == "line"] == whole.rstrip("\n").split("\n")
    assert c.get(f"/api/projects/{pid}/runs/r-nope/logs/stream").status_code == 404


@pytest.fixture
def sample_client(tmp_path, air_home):
    """样例项目登记到后台（不启动引擎线程），用来测只读和标注类接口。"""
    from airesearcher.core.workspace import register_project
    from airesearcher.testing.sample import copy_sample_project

    p = copy_sample_project(tmp_path / "sample")
    register_project(p.project_id, p.root, home=air_home)
    with TestClient(create_app(home=air_home, start_engines=False)) as c:
        yield c, p


def test_run_labels(sample_client):
    from airesearcher.services import runs as run_service

    c, p = sample_client
    base = f"/api/projects/{p.project_id}/runs"
    c1 = run_service.load_aggregate(p.root, "C1")
    bad, odd = c1.rows[0].run_ids[0], c1.rows[0].run_ids[1]
    r = c.post(f"{base}/{bad}/label", json={"label": "invalid", "reason": "数据泄漏", "request_id": "l1"})
    assert r.status_code == 200 and r.json()["label"] == "invalid"
    again = c.post(f"{base}/{bad}/label", json={"label": "invalid", "reason": "数据泄漏", "request_id": "l1"})
    assert again.json()["ts"] == r.json()["ts"]  # 同一 request_id 不重复写
    c.post(f"{base}/{odd}/label", json={"label": "suspicious", "reason": "曲线异常", "request_id": "l2"})
    labels = {x["run_id"]: x["label"] for x in c.get(base).json()}
    assert labels[bad] == "invalid" and labels[odd] == "suspicious"
    assert c.get(f"{base}/{bad}").json()["label"]["reason"] == "数据泄漏"

    agg = run_service.recompute_aggregate(p.root, "C1")
    row = next(x for x in agg.rows if x.group == c1.rows[0].group)
    assert bad not in row.run_ids and row.n == c1.rows[0].n - 1 and row.suspicious == [odd]
    assert any(e["run_id"] == bad and "人工标注为无效" in e["reason"] for e in agg.excluded)
    assert "可疑" in run_service.aggregate_markdown(agg)
    assert len((p.root / "artifacts/run_labels.jsonl").read_text(encoding="utf-8").splitlines()) == 2
    assert [e.type for e in p.events.all()].count("run.labeled") == 2

    assert c.post(f"{base}/r-nope/label", json={"label": "invalid"}).status_code == 404
    assert c.post(f"{base}/{bad}/label", json={"label": "maybe"}).status_code == 422


def make_zip(files: dict[str, bytes | str], symlink: str | None = None) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = 0o120777 << 16
            zf.writestr(info, "/etc/passwd")
    return buf.getvalue()


def test_template_upload_and_list(sample_client):
    c, p = sample_client
    base = f"/api/projects/{p.project_id}/templates"
    assert c.get(base).json() == {"current": None, "templates": [], "default_check": None}
    tpl = make_zip({"acl/main.tex": "\\documentclass{article}\n", "acl/acl.sty": "%", "acl/__MACOSX/x": "",
                    "acl/figs/logo.png": b"\x89PNG"})
    r = c.post(base, files={"file": ("acl.zip", tpl, "application/zip")}, data={"request_id": "t1"})
    assert r.status_code == 200, r.text
    info = r.json()
    assert info["template_id"] == "tpl-01" and info["version"] == 1 and info["in_use"]
    assert info["files"] == ["acl.sty", "figs/logo.png", "main.tex"] and info["tex_files"] == ["main.tex"]
    assert (p.root / "templates/tpl-01/v1/original/main.tex").exists()  # 顶层文件夹 acl/ 被去掉
    assert c.post(base, files={"file": ("acl.zip", tpl)}, data={"request_id": "t1"}).json() == info  # 去重

    # 修复后作为同一模板的新版本上传；项目改用新版本
    r = c.post(base, files={"file": ("acl-fixed.zip", tpl)}, data={"template_id": "tpl-01", "request_id": "t2"})
    assert r.json()["version"] == 2
    lst = c.get(base).json()
    assert lst["current"] == {"id": "tpl-01", "version": 2}
    assert [(t["version"], t["in_use"]) for t in lst["templates"]] == [(1, False), (2, True)]
    from airesearcher.core.project import Project

    assert Project.open(p.root).config.template == {"id": "tpl-01", "version": 2}
    assert p.archive.latest("templates/tpl-01/v2") is not None
    assert "template.uploaded" in [e.type for e in p.events.all()]
    # 论文阶段写了 check.json 后，列表里能看到
    (p.root / "templates/tpl-01/v2/check.json").write_text('{"entry": "main.tex", "missing": []}', encoding="utf-8")
    assert c.get(base).json()["templates"][1]["check"]["entry"] == "main.tex"


@pytest.mark.parametrize("files,symlink,msg", [
    ({"../evil.tex": "x"}, None, "不安全的路径"),
    ({"/abs/main.tex": "x"}, None, "不安全的路径"),
    ({"main.tex": "x"}, "link.tex", "符号链接"),
    ({"readme.txt": "x"}, None, "没有 .tex"),
])
def test_template_upload_rejects_bad_zips(sample_client, files, symlink, msg):
    c, p = sample_client
    r = c.post(f"/api/projects/{p.project_id}/templates", files={"file": ("t.zip", make_zip(files, symlink))})
    assert r.status_code == 422 and msg in r.json()["error"]["message"]
    assert not (p.root / "templates").exists() or not any((p.root / "templates").rglob("*.tex"))


def test_template_upload_not_zip_and_unknown_id(sample_client):
    c, p = sample_client
    base = f"/api/projects/{p.project_id}/templates"
    assert c.post(base, files={"file": ("t.zip", b"not a zip")}).json()["error"]["message"] == "上传的文件不是 zip"
    r = c.post(base, files={"file": ("t.zip", make_zip({"main.tex": "x"}))}, data={"template_id": "tpl-09"})
    assert r.status_code == 404


def test_export_paper_and_archive(sample_client):
    import io
    import zipfile

    c, p = sample_client
    pid = p.project_id
    r = c.get(f"/api/projects/{pid}/export", params={"what": "paper"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert f'{pid}-paper.zip' in r.headers["content-disposition"]
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    pre = f"{pid}-paper/"
    assert all(n.startswith(pre) for n in names)
    rel = {n[len(pre):] for n in names}
    assert {"ARCHIVE_INDEX.md", "paper/main.tex", "paper/claims.jsonl", "paper/build/main.pdf",
            "paper/review/review_v2.json", "artifacts/figures/fig_c1/figure.json"} <= rel
    assert "paper/build/main.aux" not in rel and not any(x.startswith("runs/") for x in rel)
    index = zipfile.ZipFile(io.BytesIO(r.content)).read(pre + "ARCHIVE_INDEX.md").decode("utf-8")
    assert "fig_c1" in index and "contradicted" in index and "E2-seed=2" in index

    r = c.get(f"/api/projects/{pid}/export", params={"what": "archive"})
    rel = {n.split("/", 1)[1] for n in zipfile.ZipFile(io.BytesIO(r.content)).namelist()}
    assert {"project.yaml", "research_log.jsonl", ".state/checkpoint.json", "ARCHIVE_INDEX.md"} <= rel
    assert any(x.startswith("runs/") for x in rel) and any(x.startswith(".archive/") for x in rel)
    assert not any(x.startswith(".git/") for x in rel)
    assert c.get(f"/api/projects/{pid}/export", params={"what": "nope"}).status_code == 422
    assert [e.type for e in p.events.all()].count("project.exported") == 2
