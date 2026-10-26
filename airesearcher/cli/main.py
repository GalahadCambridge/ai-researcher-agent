"""air 命令行（详细设计 1 第 8.2 节）。

除 air serve 和 air dev * 外，所有命令都通过 HTTP 调用后台（与 GUI 行为一致，FR-72）。
后台地址默认 http://127.0.0.1:8765，可用环境变量 AIR_SERVER 修改。
大多数命令的项目编号 <pid> 可以省略：省略时使用最近创建的项目。
"""

from __future__ import annotations

import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.table import Table

from airesearcher.core.models.frontmatter import split_front_matter

from . import labels as L

app = typer.Typer(help="AI Researcher Agent 命令行。先运行 air serve 启动后台，再在另一个终端使用其他命令。",
                  no_args_is_help=True, add_completion=False, rich_markup_mode="rich")
dev_app = typer.Typer(help="开发调试命令（不经过后台，直接操作工作区）。", no_args_is_help=True)
app.add_typer(dev_app, name="dev")
models_app = typer.Typer(help="查看、测试已配置的大模型。不带子命令时列出当前配置。", invoke_without_command=True)
app.add_typer(models_app, name="models")
template_app = typer.Typer(help="论文模板：上传 zip、查看已上传的版本。", no_args_is_help=True)
app.add_typer(template_app, name="template")
eval_app = typer.Typer(help="评价工具（不经过后台，在本进程里跑）。", no_args_is_help=True)
app.add_typer(eval_app, name="eval")
skills_app = typer.Typer(help="查看、验证 skill（不经过后台）。", no_args_is_help=True)
app.add_typer(skills_app, name="skills")
console = Console()

SERVER = os.environ.get("AIR_SERVER", "http://127.0.0.1:8765")
_client: Any = None  # 测试时替换为 FastAPI TestClient


# ====================================================================== HTTP
def client() -> Any:
    global _client
    if _client is None:
        _client = httpx.Client(base_url=SERVER, timeout=httpx.Timeout(60.0, read=600.0))  # 导出大档案时要等较久
    return _client


def fail(msg: str) -> None:
    console.print(f"[bold red]✗ {msg}[/]")
    raise typer.Exit(1)


def api(method: str, path: str, **kw: Any) -> Any:
    try:
        r = client().request(method, path, **kw)
    except httpx.ConnectError:
        fail(f"连不上后台（{SERVER}）。请先在另一个终端运行：air serve")
    if r.status_code >= 400:
        try:
            err = r.json().get("error", {})
        except ValueError:
            err = {"message": r.text}
        fail(f"{err.get('message', r.text)}（{err.get('code', r.status_code)}）")
    return r.json()


def rid() -> str:
    return str(uuid.uuid4())


def resolve_pid(pid: str | None) -> str:
    if pid:
        return pid
    projects = api("GET", "/api/projects")
    if not projects:
        fail('还没有项目。先运行：air new --idea "你的研究想法"')
    console.print(f"[dim]（未指定项目，使用最近创建的 {projects[0]['project_id']}）[/]")
    return projects[0]["project_id"]


def resolve_approval(aid: str, pid: str | None) -> str:
    """审批编号只在项目内唯一：没给 -p 时，找有这个待审批的项目。"""
    if pid:
        return pid
    projects = api("GET", "/api/projects")
    hits = [p["project_id"] for p in projects
            if any(a["approval_id"] == aid for a in api("GET", f"/api/projects/{p['project_id']}/approvals",
                                                         params={"status": "pending"}))]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        fail(f"多个项目都有待审批 {aid}，请用 -p 指定项目：{', '.join(hits)}")
    return resolve_pid(None)


# ====================================================================== 显示
def _state(s: str) -> str:
    color = L.STATE_COLOR.get(s, "cyan")
    return f"[{color}]{L.label(L.STATE, s)}[/]（{s}）"


def _budget_line(b: dict) -> str:
    parts = []
    for name, c in b["categories"].items():
        if c["hard"] is None and c["used"] == 0:
            continue
        lim = f"/{c['hard']:g}" if c["hard"] is not None else ""
        warn = {"warn": " [yellow]⚠[/]", "exhausted": " [red]✗[/]"}.get(c["level"], "")
        parts.append(f"{L.label(L.BUDGET, name)} {_num(c['used'])}{lim}{warn}")
    return " · ".join(parts)


def _num(x: float) -> str:
    if x == 0:
        return "0"
    if abs(x) < 0.01:
        return "<0.01"
    return f"{x:.2f}".rstrip("0").rstrip(".")


def print_next(st: dict) -> None:
    cmds: list[tuple[str, str]] = []
    for a in st["pending_approvals"]:
        aid = a["approval_id"]
        cmds += [(f"air show {aid}", "查看待审内容"), (f"air approve {aid}", "批准"),
                 (f'air revise {aid} -m "修改意见"', "退回修改"), (f'air reject {aid} -m "理由"', "拒绝")]
    q = st["pending_question"]
    if q:
        cmds.append(("air question", "查看问题和选项"))
        if q["options"]:
            for o in q["options"]:
                cmds.append((f"air answer {o['id']}", o["label"]))
        else:
            cmds.append(('air answer -m "你的回答"', "文字回答"))
    elif "resume" in st["next_actions"]:
        cmds.append(("air resume", "继续项目"))
    if st["state"] in ("IdeaDrafting", "PlanDrafting", "Executing", "Analyzing", "Writing") and not cmds:
        cmds.append(("air status --wait", "等待，直到下一步需要你处理"))
        if st["state"] == "Executing":
            cmds.append(("air runs", "查看运行"))
    if st["state"] == "Completed":
        console.print(f"[green]🎉 项目已完成。论文 PDF：{st['root']}/paper/build/main.pdf[/]")
        cmds.append(("air reopen", "重新打开项目修改论文"))
    if cmds:
        console.print("[bold]接下来可以输入：[/]")
        w = max(len(c) for c, _ in cmds)
        for c, d in cmds:
            console.print(f"  [bold cyan]{escape(c.ljust(w))}[/]   {escape(d)}")


def print_status(st: dict, next_steps: bool = True) -> None:
    console.rule(f"[bold]项目 {st['project_id']}：{st['title']}")
    console.print(f"状态：{_state(st['state'])}")
    prog = st.get("progress") or {}
    ptxt = f"   进度：{prog['done']}/{prog['total']} {prog.get('unit', '')}" if prog.get("total") else ""
    console.print(f"阶段：{st['stage_label']}{ptxt}")
    if st["blocking_reason"]:
        console.print(f"[bold yellow]👉 需要你处理：{escape(st['blocking_reason'])}[/]")
    for a in st["pending_approvals"]:
        t = a["target"]
        console.print(f"  待审批 [bold]{a['approval_id']}[/]（{L.label(L.KIND, a['kind'])}）"
                      f"{escape(t['artifact_id'])} v{t['version']}")
        console.print(f"    [dim]{escape(a['summary'])}[/]")
    q = st["pending_question"]
    if q:
        console.print(f"  待回答问题 [bold]{q['question_id']}[/]：{escape(q['text'].strip().splitlines()[0])}")
    if st["active_runs"]:
        console.print(f"运行中：{len(st['active_runs'])} 个  " + "，".join(
            f"{r['task_key']}（{L.label(L.RUN, r['state'])}）" for r in st["active_runs"][:4]))
    console.print(f"[dim]预算：{_budget_line(st['budget'])}[/]")
    if next_steps:
        print_next(st)


def print_question(q: dict) -> None:
    console.rule(f"[bold]问题 {q['question_id']}（来自 {q['stage']}）")
    console.print(Markdown(q["text"]))
    if q["options"]:
        console.print("[bold]选项：[/]")
        for o in q["options"]:
            d = "（默认）" if o.get("default") else ""
            console.print(f"  [bold cyan]air answer {o['id']}[/]   {escape(o['label'])}{d}")
        if q["allow_text"]:
            console.print('  可以加 -m "补充说明"')
    else:
        console.print('请用文字回答：[bold cyan]air answer -m "你的回答"[/]')


# ====================================================================== 命令
@app.command()
def serve(port: int = typer.Option(8765, help="端口"), host: str = typer.Option("127.0.0.1", help="只监听本机")):
    """启动后台（流程引擎 + API）。保持这个终端开着，在另一个终端输入其他命令。"""
    import uvicorn

    from airesearcher.core.workspace import air_home
    from airesearcher.server.app import create_app

    console.print(f"[bold green]AI Researcher 后台启动：http://{host}:{port}[/]   接口说明：http://{host}:{port}/docs")
    console.print(f"[dim]项目目录：{air_home()}/projects   按 Ctrl+C 停止[/]")
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


@app.command()
def new(
    idea: str = typer.Option(..., "--idea", help="研究想法（一两句话）"),
    task: str = typer.Option("tasks/smoke", "--task", help="任务配置目录"),
    title: str = typer.Option(None, "--title", help="项目标题（默认取想法的第一句）"),
    no_evidence_check: bool = typer.Option(False, "--no-evidence-check", help="关闭论断-证据检查（对照实验用）"),
    mode: str = typer.Option("fake", "--mode",
                             help="fake：全部假实现（不花钱）；real：已有真实实现的阶段用 real，调用真实大模型"),
    allow_local_execution: bool = typer.Option(False, "--allow-local-execution",
                                               help="允许在本机真的跑训练（只能和 --mode real 一起用）"),
    impl: str = typer.Option(None, "--impl", help='逐个指定阶段实现，如 "idea=real,plan=fake"（优先于 --mode）'),
    smoke_seconds: float = typer.Option(None, "--smoke-seconds", help="假实验：每个运行跑几秒"),
    smoke_fail: list[str] = typer.Option(None, "--smoke-fail", help="假实验：让某个运行失败，如 E2-seed=1:exit1"),
):
    """创建项目并开始。"""
    dev: dict[str, Any] = {}
    if impl:
        dev["stage_impl"] = _parse_impl(impl)
    if smoke_seconds is not None:
        dev["smoke_seconds"] = smoke_seconds
    if smoke_fail:
        dev["smoke_fail"] = dict(x.rsplit(":", 1) for x in smoke_fail)
    if mode not in ("fake", "real"):
        fail(f"--mode 只能是 fake 或 real，不是 {mode}")
    body = {"title": title, "idea_text": idea, "task": task, "evidence_check": not no_evidence_check,
            "mode": mode, "allow_local_execution": allow_local_execution, "request_id": rid(), "dev": dev or None}
    p = api("POST", "/api/projects", json=body)
    console.print(f"[bold green]✓ 已创建项目 {p['project_id']}[/]  目录：{p['root']}")
    time.sleep(1.5)
    print_status(api("GET", f"/api/projects/{p['project_id']}"))


def _parse_impl(s: str) -> dict[str, str]:
    if "=" not in s:
        return {name: s for name in ("idea", "plan", "experiment", "writing")}
    return {k.strip(): v.strip() for k, v in (x.split("=", 1) for x in s.split(","))}


@app.command("list")
def list_projects():
    """列出所有项目。"""
    rows = api("GET", "/api/projects")
    if not rows:
        console.print('还没有项目。运行：air new --idea "你的研究想法"')
        return
    t = Table("项目编号", "标题", "状态", "待审批", "问题", "创建时间")
    for r in rows:
        t.add_row(r["project_id"], r["title"], L.label(L.STATE, r["state"]), str(r["pending_approvals"]),
                  "有" if r["has_question"] else "", r["created_at"][:16].replace("T", " "))
    console.print(t)


@app.command()
def status(
    pid: str = typer.Argument(None, help="项目编号（省略 = 最近的项目）"),
    wait: bool = typer.Option(False, "--wait", "-w", help="一直等到需要你处理（审批/问题）或项目结束"),
    timeout: int = typer.Option(900, help="--wait 最多等多少秒"),
):
    """查看项目当前状态、需要你做什么、以及下一步命令。"""
    pid = resolve_pid(pid)
    st = api("GET", f"/api/projects/{pid}")
    if wait:
        t0, last = time.time(), None
        while time.time() - t0 < timeout:
            if st["blocking_reason"] or st["state"] in ("Completed", "Failed", "Paused", "BudgetExhausted"):
                break
            prog = st.get("progress") or {}
            line = f"{L.label(L.STATE, st['state'])} · {st['stage_label']}"
            if prog.get("total"):
                line += f" · {prog['done']}/{prog['total']}"
            if line != last:
                console.print(f"[dim]{time.strftime('%H:%M:%S')}[/] {line}")
                last = line
            time.sleep(2)
            st = api("GET", f"/api/projects/{pid}")
    print_status(st)


@app.command()
def approvals(pid: str = typer.Argument(None, help="项目编号"),
              all: bool = typer.Option(False, "--all", help="包括已处理的")):
    """列出待审批（--all 显示全部）。"""
    pid = resolve_pid(pid)
    rows = api("GET", f"/api/projects/{pid}/approvals", params=None if all else {"status": "pending"})
    if not rows:
        console.print("没有待审批。")
        return
    t = Table("编号", "类别", "产物", "状态", "摘要")
    for a in rows:
        target = f"{a['target']['artifact_id']} v{a['target']['version']}"
        t.add_row(a["approval_id"], L.label(L.KIND, a["kind"]), escape(target), L.label(L.APPROVAL, a["status"]),
                  escape(a["summary"][:60]))
    console.print(t)


@app.command()
def show(aid: str = typer.Argument(..., help="审批编号，如 ap-0001"), pid: str = typer.Option(None, "-p", "--project")):
    """查看待审内容（终端里显示 Markdown）。"""
    pid = resolve_approval(aid, pid)
    d = api("GET", f"/api/projects/{pid}/approvals/{aid}")
    a = d["approval"]
    t = a["target"]
    console.rule(f"[bold]审批 {aid}：{L.label(L.KIND, a['kind'])}  {t['artifact_id']} v{t['version']}")
    console.print(f"状态：{L.label(L.APPROVAL, a['status'])}")
    console.print(f"摘要：{escape(a['summary'])}")
    console.print(f"影响：{escape(a['impact'])}")
    content = d["target_content"]
    if content and t["artifact_id"].endswith(".md"):
        try:
            _, body = split_front_matter(content)
            console.print(f"[dim]（文件开头的 YAML 数据区已省略，完整文件：{d['target_path']}）[/]")
        except ValueError:
            body = content
        console.print(Markdown(body))
    elif t["artifact_id"] == "paper":
        console.print(f"论文目录：{d['target_path']}")
        console.print(f"[bold]PDF：{d['target_path']}/build/main.pdf[/]（用 PDF 阅读器打开）")
        info = api("GET", f"/api/projects/{pid}/paper")
        if info.get("review_summary"):
            md = info["review_summary"]["file"].removesuffix(".json") + ".md"
            review = api("GET", f"/api/projects/{pid}/files", params={"path": md})
            console.print(Markdown(review["content"]))
        console.print("[dim]（以下附件为原始数据，供需要时查看）[/]")
    elif content:
        console.print(content[:4000])
    for e in d["extra"]:
        console.print(f"\n[bold]附：{escape(e['artifact_id'])} v{e['version']}[/]")
        if e["content"] and e["artifact_id"].endswith(".md"):
            console.print(Markdown(e["content"]))
        elif e["content"] and t["artifact_id"] != "paper":
            console.print(escape("\n".join(e["content"].splitlines()[:25])), style="dim")
    console.print()
    if a["status"] == "pending":
        console.print(f"批准：[bold cyan]air approve {aid}[/]   退回：[bold cyan]air revise {aid} -m \"意见\"[/]"
                      f"   拒绝：[bold cyan]air reject {aid} -m \"理由\"[/]")


def _decide(aid: str, decision: str, comment: str, pid: str | None) -> None:
    pid = resolve_approval(aid, pid)
    d = api("GET", f"/api/projects/{pid}/approvals/{aid}")
    body = {"decision": decision, "comment": comment or "", "expected_sha256": d["approval"]["target"]["sha256"],
            "request_id": rid()}
    a = api("POST", f"/api/projects/{pid}/approvals/{aid}/decision", json=body)
    console.print(f"[bold green]✓ 审批 {aid}：{L.label(L.APPROVAL, a['status'])}[/]")
    time.sleep(0.5)
    print_status(api("GET", f"/api/projects/{pid}"))


@app.command()
def approve(aid: str = typer.Argument(..., help="审批编号"), m: str = typer.Option("", "-m", help="意见（可选）"),
            pid: str = typer.Option(None, "-p", "--project")):
    """批准。"""
    _decide(aid, "approved", m, pid)


@app.command()
def revise(aid: str = typer.Argument(..., help="审批编号"), m: str = typer.Option(..., "-m", help="修改意见"),
           pid: str = typer.Option(None, "-p", "--project")):
    """退回修改（必须写意见）。"""
    _decide(aid, "changes_requested", m, pid)


@app.command()
def reject(aid: str = typer.Argument(..., help="审批编号"), m: str = typer.Option(..., "-m", help="拒绝理由"),
           pid: str = typer.Option(None, "-p", "--project")):
    """拒绝（之后会问你：重写还是结束项目）。"""
    _decide(aid, "rejected", m, pid)


@app.command()
def question(pid: str = typer.Argument(None, help="项目编号")):
    """查看当前待回答的问题。"""
    pid = resolve_pid(pid)
    q = api("GET", f"/api/projects/{pid}/question")
    if not q:
        console.print("没有待回答的问题。")
        return
    print_question(q)


@app.command()
def answer(args: list[str] = typer.Argument(None, help="[项目编号] 选项，如 air answer skip 或 air answer p-xxx skip"),
           m: str = typer.Option("", "-m", help="文字回答 / 补充说明")):
    """回答当前问题：air answer <选项> [-m 文字]。"""
    args = args or []
    pid, choice = (args[0], args[1]) if len(args) >= 2 else (None, args[0] if args else None)
    pid = resolve_pid(pid)
    q = api("GET", f"/api/projects/{pid}/question")
    if not q:
        fail("没有待回答的问题。")
    st = api("POST", f"/api/projects/{pid}/question/answer",
             json={"question_id": q["question_id"], "choice": choice, "text": m, "request_id": rid()})
    console.print(f"[bold green]✓ 已回答 {q['question_id']}：{choice or m}[/]")
    print_status(st)


def _action(pid: str | None, action: str) -> None:
    pid = resolve_pid(pid)
    st = api("POST", f"/api/projects/{pid}/actions", json={"action": action, "request_id": rid()})
    print_status(st)


@app.command()
def pause(pid: str = typer.Argument(None)):
    """暂停项目（已在跑的实验会继续跑完）。"""
    _action(pid, "pause")


@app.command()
def resume(pid: str = typer.Argument(None)):
    """继续已暂停 / 预算用尽 / 出错的项目。"""
    _action(pid, "resume")


@app.command()
def cancel(pid: str = typer.Argument(None)):
    """取消项目。"""
    _action(pid, "cancel")


@app.command()
def reopen(pid: str = typer.Argument(None)):
    """重新打开已完成的项目，回到撰写论文。"""
    _action(pid, "reopen")


@app.command()
def checkpoints(pid: str = typer.Argument(None),
                all_: bool = typer.Option(False, "--all", help="也列出最近的普通检查点（默认只列状态切换时的里程碑）")):
    """列出可以回滚到的检查点。"""
    pid = resolve_pid(pid)
    rows = api("GET", f"/api/projects/{pid}/checkpoints")
    rows = rows if all_ else [r for r in rows if r["milestone"]]
    t = Table("检查点", "保存时间", "当时的状态", "里程碑")
    for r in rows:
        t.add_row(r["checkpoint_id"], r["saved_at"][:19].replace("T", " "), L.label(L.STATE, r["state"]),
                  "✓" if r["milestone"] else "")
    console.print(t)
    console.print("回滚：air rollback <检查点>（项目正在工作时先 air pause）")


@app.command()
def rollback(checkpoint_id: str = typer.Argument(..., help="检查点编号，如 ck-000012（air checkpoints 查看）"),
             pid: str = typer.Option(None, "-p", "--project", help="项目编号（省略时用最近创建的项目）")):
    """回到历史检查点（项目正在工作时须先 air pause）。不删除任何产物或运行，之后用 air resume 从那里重新开始。"""
    pid = resolve_pid(pid)
    st = api("POST", f"/api/projects/{pid}/rollback", json={"checkpoint_id": checkpoint_id, "request_id": rid()})
    note = st["blocking_reason"] or L.label(L.STATE, st["state"])
    console.print(f"[green]✓ 已回滚到 {checkpoint_id}[/]：{escape(note)}")
    if st["state"] == "Paused":
        console.print("确认无误后运行：air resume")


@app.command()
def runs(pid: str = typer.Argument(None)):
    """列出实验运行。"""
    pid = resolve_pid(pid)
    rows = api("GET", f"/api/projects/{pid}/runs")
    if not rows:
        console.print("还没有运行。")
        return
    t = Table("运行编号", "任务", "类型", "状态", "失败原因", "重试自", "人工标注")
    for r in rows:
        t.add_row(r["run_id"], r["task_key"], L.label(L.RUN_KIND, r["kind"]), L.label(L.RUN, r["state"]),
                  L.label(L.FAILURE, r["failure_reason"]), r["retry_of"] or "", L.label(L.RUN_LABEL, r.get("label")))
    console.print(t)


@app.command("cancel-run")
def cancel_run(run_id: str = typer.Argument(..., help="运行编号"),
               pid: str = typer.Option(None, "-p", "--project", help="项目编号（省略时用最近创建的项目）")):
    """取消一个正在跑的实验运行（项目本身不暂停）。"""
    pid = resolve_pid(pid)
    st = api("POST", f"/api/projects/{pid}/runs/{run_id}/cancel", json={"request_id": rid()})
    console.print(f"已请求取消 {run_id}，当前状态：{L.label(L.RUN, st['state'])}（结束后在 air runs 中显示为“已取消”）")


@app.command("label")
def label_run(run_id: str = typer.Argument(..., help="运行编号"),
              label: str = typer.Argument(..., help="trusted（可信）/ suspicious（可疑）/ invalid（无效）"),
              message: str = typer.Option("", "-m", "--message", help="原因"),
              pid: str = typer.Option(None, "-p", "--project", help="项目编号（省略时用最近创建的项目）")):
    """人工标注运行：invalid 的运行不计入汇总，suspicious 的计入但在表中标出。"""
    if label not in L.RUN_LABEL:
        fail(f"标注只能是 {' / '.join(L.RUN_LABEL)}")
    pid = resolve_pid(pid)
    api("POST", f"/api/projects/{pid}/runs/{run_id}/label",
        json={"label": label, "reason": message, "request_id": rid()})
    console.print(f"[green]✓ 已把 {run_id} 标注为「{L.RUN_LABEL[label]}」[/]。汇总结果会在下一次汇总时更新。")


@app.command()
def logs(args: list[str] = typer.Argument(..., help="[项目编号] 运行编号"),
         follow: bool = typer.Option(False, "-f", "--follow", help="持续输出直到运行结束"),
         stderr: bool = typer.Option(False, "--stderr", help="看 stderr")):
    """查看运行日志：air logs <run_id> [-f]。"""
    pid, run_id = (args[0], args[1]) if len(args) >= 2 else (None, args[0])
    pid = resolve_pid(pid)
    offset = 0
    while True:
        c = api("GET", f"/api/projects/{pid}/runs/{run_id}/logs",
                params={"stream": "stderr" if stderr else "stdout", "offset": offset})
        if c["text"]:
            console.out(c["text"], end="")
        offset = c["next_offset"]
        if not follow or c["eof"]:
            break
        time.sleep(1)


@app.command()
def export(pid: str = typer.Argument(None),
           what: str = typer.Option("paper", "--what", help="paper（论文交付包）或 archive（整个研究档案）"),
           output: Path = typer.Option(None, "-o", "--output", help="保存到哪里（默认当前目录下 <项目>-<what>.zip）")):
    """导出 zip：论文交付包（LaTeX、PDF、图表、论断、核验报告、ARCHIVE_INDEX.md）或整个研究档案。"""
    if what not in ("paper", "archive"):
        fail("--what 只能是 paper 或 archive")
    pid = resolve_pid(pid)
    try:
        r = client().get(f"/api/projects/{pid}/export", params={"what": what})
    except httpx.ConnectError:
        fail(f"连不上后台（{SERVER}）。请先在另一个终端运行：air serve")
    if r.status_code >= 400:
        fail(r.json().get("error", {}).get("message", r.text))
    output = output or Path(f"{pid}-{what}.zip")
    output.write_bytes(r.content)
    console.print(f"[green]✓ 已导出到 {output}[/]（{len(r.content) / 1024:.0f} KB）")


# ====================================================================== air template
@template_app.command("upload")
def template_upload(zip_path: Path = typer.Argument(..., help="模板 zip 文件"),
                    to: str = typer.Option(None, "--to", help="作为已有模板的新版本，如 tpl-01"),
                    pid: str = typer.Option(None, "-p", "--project", help="项目编号（省略时用最近创建的项目）")):
    """上传论文模板（zip），项目改用这个模板。"""
    if not zip_path.is_file():
        fail(f"找不到文件 {zip_path}")
    pid = resolve_pid(pid)
    data = {"request_id": rid(), **({"template_id": to} if to else {})}
    info = api("POST", f"/api/projects/{pid}/templates", data=data,
               files={"file": (zip_path.name, zip_path.read_bytes(), "application/zip")})
    console.print(f"[green]✓ 已上传为 {info['template_id']} v{info['version']}[/]（{len(info['files'])} 个文件，"
                  f"入口候选：{', '.join(info['tex_files']) or '无'}）。论文阶段会检查模板，有问题时会提问。")


@template_app.command("list")
def template_list(pid: str = typer.Option(None, "-p", "--project", help="项目编号（省略时用最近创建的项目）")):
    """列出已上传的模板版本和检查结果。"""
    pid = resolve_pid(pid)
    lst = api("GET", f"/api/projects/{pid}/templates")
    cur = lst["current"]
    console.print("当前使用：" + (f"{cur['id']} v{cur['version']}" if cur else "默认模板"))
    if not lst["templates"]:
        console.print("还没有上传过模板。上传：air template upload <zip 文件>")
        return
    t = Table("模板", "版本", "文件名", "入口", "缺失依赖", "最小编译", "使用中")
    for x in lst["templates"]:
        chk = x["check"] or {}
        missing = ", ".join(m.get("name", "?") for m in chk.get("missing", [])) if chk else ""
        ok = {True: "成功", False: "失败"}.get(chk.get("minimal_compile_ok"), "未检查") if chk else "未检查"
        t.add_row(x["template_id"], str(x["version"]), escape(x["filename"] or ""), chk.get("entry") or "",
                  escape(missing), ok, "✓" if x["in_use"] else "")
    console.print(t)


# ====================================================================== air models
def _key_mark(v: bool | None) -> str:
    return {True: "[green]✓ 已设置[/]", False: "[red]✗ 未设置[/]", None: "[dim]不需要[/]"}[v]


@models_app.callback()
def models_main(ctx: typer.Context, pid: str = typer.Option(None, "-p", "--project",
                                                             help="显示某个项目实际使用的模型（含项目覆盖）")):
    """列出已登记的模型、Key 是否已设置、每个阶段实际使用的模型。"""
    if ctx.invoked_subcommand is not None:
        return
    info = api("GET", "/api/models", params={"project_id": pid} if pid else None)
    t = Table("名字", "模型", "Key", "说明")
    for mdl in info["models"]:
        key = _key_mark(mdl["key_set"]) + (f" [dim]({mdl['key_env']})[/]" if mdl["key_env"] else "")
        t.add_row(mdl["alias"], mdl["model"], key, escape(mdl["description"] or (mdl["api_base"] or "")))
    console.print(t)
    tiers = sorted({tier for a in info["assignments"].values() for tier in a})
    t2 = Table("阶段", *tiers, title="每个阶段实际使用的模型" + (f"（项目 {pid}）" if pid else ""))
    for stage, row in info["assignments"].items():
        t2.add_row(stage, *[row[x]["alias"] or "[red]未配置[/]" for x in tiers])
    console.print(t2)
    if info["roles"]:
        console.print("按角色指定：" + "，".join(f"{k} → {v}" for k, v in info["roles"].items()))
    for w in info["warnings"]:
        console.print(f"[yellow]⚠ {escape(w)}[/]")
    files = info["config_files"]
    console.print(f"[dim]配置文件：{files['user']}（我的）· {files['repo']}（默认）· Key：{files['dotenv']}[/]")
    console.print("试调用：[bold cyan]air models test <名字>[/]   生成配置模板：[bold cyan]air models init[/]")


@models_app.command("test")
def models_test(alias: str = typer.Argument(..., help="模型名字，如 claude-fast"),
                pid: str = typer.Option(None, "-p", "--project")):
    """用一次极短的调用检查模型能否使用（会产生极少量费用）。"""
    console.print(f"正在调用 {alias} ……")
    r = api("POST", f"/api/models/{alias}/test", params={"project_id": pid} if pid else None,
            json={"request_id": rid()})
    if r["ok"]:
        console.print(f"[bold green]✓ {alias}（{r['model']}）可以使用[/]：回复 {escape(r['reply'])!r}，"
                      f"{r['seconds']} 秒，{r['input_tokens']}+{r['output_tokens']} tokens，约 ${r['usd']:.5f}")
    else:
        console.print(f"[bold red]✗ {alias} 调用失败[/]：{escape(r.get('error', ''))}")
        raise typer.Exit(1)


@models_app.command("init")
def models_init():
    """生成我的模型配置 ~/air/models.yaml 和 Key 文件 ~/air/.env 的模板（已存在则不覆盖）。"""
    from airesearcher.llm.models import DOTENV_TEMPLATE, USER_TEMPLATE, dotenv_path, user_config_path

    for path, text in ((user_config_path(), USER_TEMPLATE), (dotenv_path(), DOTENV_TEMPLATE)):
        if path.exists():
            console.print(f"[dim]已存在，未修改：{path}[/]")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        console.print(f"[green]✓ 已生成 {path}[/]")
    console.print("下一步：在 .env 里填上 Key（改完立即生效，不用重启后台）→ air models 查看 → air models test <名字>")


# ====================================================================== air dev
@eval_app.command("run")
def eval_run(config: Path = typer.Argument(..., help="批量配置，如 eval/configs/smoke_batch.yaml"),
             arm: list[str] = typer.Option(None, "--arm", help="只跑某几组（可写多次）"),
             out: Path = typer.Option(None, "--out", help="结果目录（默认用配置里的 results_dir）")):
    """批量运行评价轨迹，结果写到 <结果目录>/<名字>/summary.csv（详细设计 1 第 12.1 节）。中断后再运行会接着跑。"""
    from airesearcher.eval.batch import run_batch

    if not config.is_file():
        fail(f"找不到配置文件 {config}")
    try:
        summary = run_batch(config, only_arms=arm or None, results_dir=out,
                            log=lambda msg: console.print(escape(msg)))
    except ValueError as e:
        fail(str(e))
    import csv

    rows = list(csv.DictReader(summary.open(encoding="utf-8")))
    t = Table("组", "实例", "次", "结果", "最终状态", "LLM 费用", "运行（成功/失败）", "提问", "秒")
    for r in rows:
        t.add_row(r["arm"], r["instance"], r["rep"], r["outcome"], L.label(L.STATE, r["final_state"] or None),
                  f"${float(r['llm_usd'] or 0):.4f}", f"{r['runs_succeeded']}/{r['runs_failed']}",
                  r["questions_asked"], r["seconds"])
    console.print(t)


@skills_app.command("list")
def skills_list():
    """列出所有 skill：当前版本、已启用的版本。"""
    from airesearcher.skills.loader import SkillLoader

    loader = SkillLoader()
    reg = loader.registry()
    table = Table("名字", "当前版本", "已启用", "说明")
    for name in loader.available():
        cur = loader.current_version(name)
        en = reg.get(name)
        mark = f"[green]{en}[/]" if en == cur else f"[yellow]{en or '未登记'}（待验证）[/]"
        table.add_row(name, cur, mark, escape(str(loader.meta(name).get("description", ""))))
    console.print(table)
    console.print(f"[dim]启用记录：{loader.registry_path}[/]")


@skills_app.command("verify")
def skills_verify(
    name: str = typer.Argument(None, help="skill 名字；不写时配合 --all 验证全部"),
    all_: bool = typer.Option(False, "--all", help="验证所有 skill"),
    check: bool = typer.Option(False, "--check", help="只检查不修改 registry.yaml；版本未登记也算失败（CI 用）"),
):
    """用 skill 的 sample/ 小样例验证当前版本，通过后写进 skills/registry.yaml（详细设计 1 第 7.3 节）。"""
    from airesearcher.skills.loader import SkillLoader

    loader = SkillLoader()
    if not name and not all_:
        console.print("[red]请写 skill 名字，或用 --all[/]")
        raise typer.Exit(2)
    names = loader.available() if all_ else [name]
    failed = False
    for n in names:
        rep = loader.verify(n, update_registry=not check)
        console.print(f"[bold]{n}[/] {rep.version or ''}")
        for label, ok, detail in rep.steps:
            console.print(f"  {'[green]✓[/]' if ok else '[red]✗[/]'} {label}：{escape(detail)}")
        if rep.registry_changed:
            before, after = rep.registry_changed
            console.print(f"  [green]已启用 {after}[/]（之前：{before or '未登记'}），"
                          "请把 skills/registry.yaml 一起提交")
        failed |= not rep.ok
    if check:
        for prob in loader.problems():
            if all_ or prob.startswith(f"{name}："):
                console.print(f"[red]✗ {escape(prob)}[/]")
                failed = True
    if failed:
        raise typer.Exit(1)


@dev_app.command("new-workspace")
def dev_new_workspace(
    path: Path = typer.Argument(..., help="新工作区目录"),
    from_: Path = typer.Option(None, "--from", help="从已有项目目录复制（如 fixtures/sample_project）"),
    idea: str = typer.Option("比较基线、主方法和消融在冒烟任务上的得分", "--idea"),
    task: str = typer.Option("tasks/smoke", "--task"),
    impl: str = typer.Option(None, "--impl", help='如 "idea=example" 或 "fake"'),
    register: bool = typer.Option(False, "--register",
                                  help="登记到后台，air serve 和 GUI 中能看到（重启 air serve 生效）"),
):
    """创建一个可随便改的测试工作区（默认不登记到后台）。"""
    from airesearcher.core.project import Project
    from airesearcher.core.workspace import register_project

    if path.exists() and any(path.iterdir()):
        fail(f"{path} 已存在且不为空")
    if from_:
        shutil.copytree(from_, path, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".gitignore"))
        p = Project.open(path)
        console.print(f"[green]✓ 已从 {from_} 复制到 {path}（项目 {p.project_id}）[/]")
    else:
        dev = {"stage_impl": _parse_impl(impl)} if impl else {}
        p = Project.create(goal=idea, task=task, root=path, register=False, dev=dev)
        console.print(f"[green]✓ 已创建工作区 {p.root}（项目 {p.project_id}）[/]")
    if register:
        register_project(p.project_id, p.root)
        console.print("已登记到后台：重启 air serve 后，air list 和 GUI 中就能看到它")


@dev_app.command("check")
def dev_check(
    workspace: Path = typer.Argument(..., help="项目目录（如 ~/air/projects/p-xxx 或 fixtures/sample_project）"),
    owner: list[str] = typer.Option(None, "--owner", help="只检查某位提供方：主干 / 文献 / 实验 / 论文"),
):
    """契约检查：项目里的交接文件是否符合约定的格式（详细设计 1 第 11 节）。"""
    from airesearcher.testing.contracts import check_project

    report = check_project(workspace, owners=owner or None)
    console.print(escape(report.text()))
    if not report.ok:
        raise typer.Exit(1)


@dev_app.command("run-stage")
def dev_run_stage(
    stage: str = typer.Argument(..., help="阶段名：idea / plan / experiment / writing / example"),
    workspace: Path = typer.Option(..., "--workspace", "-w", help="工作区目录"),
    steps: int = typer.Option(5, "--steps", help="最多执行几步"),
    state: str = typer.Option(None, "--state", help="先把项目状态设为这个值（如 PlanDrafting）"),
    auto_approve: bool = typer.Option(False, "--auto-approve", help="遇到审批自动批准"),
    fake_llm: bool = typer.Option(False, "--fake-llm", help="用 FakeLLM（不需要 API Key；示例阶段可用）"),
):
    """不启动后台，直接在工作区上执行阶段的 step()，打印每步的 StepResult。"""
    from airesearcher.core.locks import ProjectLock
    from airesearcher.core.models.common import ProjectState
    from airesearcher.core.project import Project
    from airesearcher.engine.engine import ProjectEngine
    from airesearcher.engine.stage import Wait, describe
    from airesearcher.testing.fake_llm import fake_gateway

    project = Project.open(workspace)
    llm = fake_gateway(project, default={"title": "（FakeLLM）示例标题", "points": ["要点一", "要点二"]}) \
        if fake_llm else None
    with ProjectLock(project.root):
        eng = ProjectEngine(project, llm=llm, retry_delay=0)
        eng.stage_override = stage
        if state:
            eng.ck.state = ProjectState(state)
            eng._save()
        for i in range(steps):
            if auto_approve and eng.auto_approve_pending():
                console.print("[dim]  （已自动批准待审批）[/]")
            before = eng.state.value
            t = eng.tick()
            after = eng.state.value
            if t.result is not None:
                console.print(f"[bold]{i + 1}.[/] [{before}] {describe(t.result)}"
                              + (f"  → {after}" if after != before else ""))
                if isinstance(t.result, Wait):
                    time.sleep(min(t.result.seconds, 5))
            elif after != before:
                why = f"（{escape(eng.ck.reason)}）" if eng.ck.reason else ""
                console.print(f"[bold]{i + 1}.[/] {before} → {after}{why}")
            else:
                console.print(f"[yellow]项目处于等待状态 {after}"
                              + (f"（{eng.ck.reason}）" if eng.ck.reason else "")
                              + "，没有可执行的 step（遇到审批可加 --auto-approve）。[/]")
                break


if __name__ == "__main__":
    app()
