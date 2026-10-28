"""生成样例项目 fixtures/sample_project/（详细设计 1 第 10.2 节）。

用法（在仓库根目录）：python fixtures/build_sample.py

做法：用四个阶段的假实现在进程内把一个项目真实地跑一遍（运行编号、哈希、版本记录都是真实计算的），
中途演示：idea 退回修改一次、一个运行失败后选择“重试”、一次试运行（trial），
最后在论文里**故意加入 2 条错误论断**（一个数字对不上、一个结论与数据相反），停在“等待审批最终手稿”。

只有在契约（数据格式）变化时才需要重新生成；每次生成的运行编号、时间都会不同。
"""

from __future__ import annotations

import platform
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "fixtures" / "sample_project"
sys.path.insert(0, str(REPO))

from airesearcher.core.fsutil import now, rand_hex  # noqa: E402
from airesearcher.core.models.claim import Claim  # noqa: E402
from airesearcher.core.models.claim import Comparison as ClaimComparison  # noqa: E402
from airesearcher.core.models.common import ProjectState as S  # noqa: E402
from airesearcher.core.models.run import TERMINAL_RUN_STATES, RunRecord  # noqa: E402
from airesearcher.core.project import Project  # noqa: E402
from airesearcher.engine.engine import ProjectEngine  # noqa: E402
from airesearcher.runtime.executor import RunSpec  # noqa: E402
from airesearcher.runtime.local import config_sha256, expand_entrypoint  # noqa: E402
from airesearcher.services import evidence  # noqa: E402
from airesearcher.stages.fakes.common import load_task  # noqa: E402
from airesearcher.stages.fakes.writing import PAPER_EXCLUDE, compile_paper  # noqa: E402
from airesearcher.testing.contracts import check_project  # noqa: E402

GOAL = "在每类只有几百条训练样本时，比较基线方法、主方法和一个消融版本在分类得分上的差异，并检查结果是否稳定。"
FAIL_TASK = "E2-seed=2"


def drive(eng: ProjectEngine, until: S, timeout: float = 120) -> None:
    t0 = time.time()
    first = True
    while first or eng.state != until:
        first = False
        t = eng.tick()
        if t.kind == "step" and t.seconds:
            time.sleep(min(t.seconds, 0.5))
        if time.time() - t0 > timeout:
            raise RuntimeError(f"卡在 {eng.state}：{eng.ck.reason}")


def decide(p: Project, decision: str = "approved", comment: str = "") -> None:
    [a] = p.approvals.pending()
    p.approvals.decide(a.approval_id, decision, a.target.sha256, comment, request_id=f"sample-{a.approval_id}")
    print(f"  {a.approval_id} {a.kind}: {decision}")


def trial_run(p: Project, eng: ProjectEngine) -> None:
    """演示一次试运行（kind=trial）：汇总时会被排除。"""
    ex = eng.executor
    s = eng.ck.scratch["experiment"]
    task = load_task(eng.build_context())
    rid = f"r-{now():%Y%m%d-%H%M%S}-{rand_hex(4)}"
    cfg = {"method": "baseline", "seed": 0, "seconds": 0.5, "fail_mode": "none"}
    d = ex.run_dir(rid)
    rec = RunRecord(run_id=rid, task_key="E1-seed=0-trial", experiment_id="E1", kind="trial",
                    plan_ref=eng.ck.approved["plan"], idea_ref=eng.ck.approved["idea"], code_commit=s["commit"],
                    config_sha256=config_sha256(cfg), eval_condition="final", seed=0,
                    command=expand_entrypoint(task, d, ex.python, trial=True), timeout_s=120,
                    required_metrics=["score"], created_at=now())
    spec = RunSpec(record=rec, run_dir=d, config=cfg, trial=True)
    ex.prepare(spec)
    ex.submit(spec)
    while ex.status(rid).state not in TERMINAL_RUN_STATES:
        time.sleep(0.2)
    ex.collect(rid)
    p.events.append("run.created", f"试运行 {rid}", actor="agent:experiment", run_id=rid)


def inject_wrong_claims(p: Project, eng: ProjectEngine) -> None:
    """在论文里加 2 条错误论断，重新登记论文、重新核验、重新提交手稿审批。"""
    paper = p.root / "paper"
    claims = [Claim.model_validate_json(x) for x in (paper / "claims.jsonl").read_text(encoding="utf-8").splitlines()]
    numeric = next(c for c in claims if c.type == "numeric" and c.values[0].aggregate_id == "C1")
    comparative = next(c for c in claims if c.type == "comparative" and c.values[0].aggregate_id == "C1")
    n = len(claims)
    # 错误 1：数字对不上（NUM_MISMATCH）——文中写的值与重算值不同
    v = numeric.values[0].model_copy(update={"key": numeric.values[0].key.replace(".mean", ".max"),
                                             "stat": "max", "rendered": "95.0"})
    wrong1 = Claim(claim_id=f"CL{n + 1}", section="results", type="numeric", statement_kind="observation",
                   hedge="none", importance="major", values=[v], evidence=numeric.evidence,
                   text=f"\\claim{{CL{n + 1}}}{{The best single run of {v.group['method']} reaches "
                        f"\\airval{{{v.key}}}\\%.}}")
    # 错误 2：结论与数据相反（CONTRADICTED）——数据显示 a 高于 b，却写成 a 低于 b
    cmp = comparative.comparison
    wrong2 = Claim(claim_id=f"CL{n + 2}", section="discussion", type="comparative", statement_kind="observation",
                   hedge="none", importance="major", values=comparative.values, evidence=comparative.evidence,
                   comparison=ClaimComparison(a=cmp.a, b=cmp.b, metric=cmp.metric, asserted="a<b"),
                   text=f"\\claim{{CL{n + 2}}}{{Overall, {cmp.a['method']} performs worse than {cmp.b['method']}.}}")
    with open(paper / "claims.jsonl", "a", encoding="utf-8") as f:
        f.write(wrong1.model_dump_json() + "\n" + wrong2.model_dump_json() + "\n")
    with open(paper / "air_values.tex", "a", encoding="utf-8") as f:
        f.write(f"\\expandafter\\def\\csname airval@{v.key}\\endcsname{{{v.rendered}}}\n")
    with open(paper / "sections" / "results.tex", "a", encoding="utf-8") as f:
        f.write(f"\n{wrong1.text}\n\n{wrong2.text}\n")

    report = compile_paper(paper, enabled=True)
    (paper / "build" / "compile_report.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    old = p.archive.latest("paper")
    paper_ref = p.archive.put("paper", "paper", paper, exclude=PAPER_EXCLUDE, parents=[old],
                              producer="system", note=f"样例项目：故意加入 2 条错误论断（CL{n + 1}、CL{n + 2}）")
    claims_ref = p.archive.put("paper/claims.jsonl", "claims", paper / "claims.jsonl", parents=[paper_ref])
    pdf_ref = p.archive.put("paper/build/main.pdf", "paper", paper / "build" / "main.pdf", parents=[paper_ref])
    compile_ref = p.archive.put("paper/build/compile_report.json", "other", paper / "build" / "compile_report.json",
                                parents=[paper_ref])
    check = evidence.check(p.root, paper_ref)
    k = len(list((paper / "review").glob("review_v*.json"))) + 1
    rp = paper / "review" / f"review_v{k}.json"
    rp.write_text(check.model_dump_json(indent=2), encoding="utf-8")
    md = [f"# 核验报告 v{k}", "", f"- 论文版本：paper v{paper_ref.version}", f"- 统计：{check.counts}", ""]
    for c in check.claims:
        md.append(f"- {c.claim_id}：{c.support}" + "".join(f"；{i.code}（{i.detail}）" for i in c.issues))
    (paper / "review" / f"review_v{k}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    review_ref = p.archive.put(f"paper/review/review_v{k}.json", "review_report", rp, parents=[paper_ref])
    blockers = check.counts.get("blocker", 0)
    aid = p.approvals.request(
        paper_ref, "manuscript",
        summary=f"论文草稿 v{paper_ref.version}：{len(check.claims)} 条论断，{blockers} 个 blocker"
                "（样例：故意加入的错误论断）",
        impact="批准后项目标记为完成；有 blocker 时批准必须填写意见",
        extra_refs=[review_ref, compile_ref, claims_ref, pdf_ref])
    eng.ck.pending_approval = aid  # 旧的手稿审批已被自动标为 superseded
    w = eng.ck.scratch.setdefault("writing", {})
    w["paper_ref"] = paper_ref.model_dump(mode="json")
    w["refs"] = {"review": review_ref.model_dump(mode="json"), "compile": compile_ref.model_dump(mode="json"),
                 "claims": claims_ref.model_dump(mode="json"), "pdf": pdf_ref.model_dump(mode="json"),
                 "review_path": rp.relative_to(p.root).as_posix()}
    eng._save()
    print(f"  注入错误论断后核验：{check.counts}；新的手稿审批 {aid}")


def sample_capture(root: Path, python: str, *, guard) -> dict:
    """样例项目用的环境记录：不执行 pip freeze，也不写解释器的绝对路径（它们是生成者本机的信息）。"""
    return {"python": platform.python_version(), "executable": python, "frameworks": {}, "lock_path": None,
            "lock_sha256": None, "gpu": None,
            "unknown": ["样例项目不记录包清单和本机解释器路径（避免带入生成者本机的信息）"]}


# 样例里不应出现的本机绝对路径（POSIX 家目录、临时目录、Windows 盘符）
ABS_PATH = re.compile(r"/Users/|/home/|/private/|/var/folders/|/tmp/|[A-Za-z]:\\")


def find_abs_paths(root: Path) -> list[str]:
    """返回样例目录里含本机绝对路径的文件（文本文件）。"""
    bad = []
    for f in sorted(root.rglob("*")):
        if f.is_file():
            try:
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if ABS_PATH.search(text):
                bad.append(f.relative_to(root).as_posix())
    return bad


def build(out: Path = OUT) -> Path:
    import airesearcher.runtime.local as local_runtime

    local_runtime.capture = sample_capture  # 只影响本脚本进程，不改变真实项目的行为
    tmp = Path(tempfile.mkdtemp(prefix="air-sample-"))
    try:
        p = Project.create(goal=GOAL, task="tasks/smoke", title="小样本分类：基线、主方法与消融的比较（样例项目）",
                           home=tmp / "home", register=False,
                           # 用 PATH 上的 python：run.json / environment.json 里不出现生成者本机的绝对路径
                           experiment={"trial_timeout_s": 300, "python": "python", "prepare_timeout_s": 1800},
                           dev={"smoke_seconds": 1.0, "poll_seconds": 1, "compile": True,
                                "smoke_fail": {FAIL_TASK: "exit1"}})
        eng = ProjectEngine(p, retry_delay=0)
        print(f"生成样例项目 {p.project_id}")
        drive(eng, S.IdeaPending)
        decide(p, "changes_requested", "研究问题请写明每类样本数和主要指标")
        drive(eng, S.IdeaPending)
        decide(p)
        drive(eng, S.PlanPending)
        decide(p)
        drive(eng, S.Executing)  # 第一步：准备代码与配置（之后才有 commit 可用）
        trial_run(p, eng)  # 试运行在正式运行之前
        drive(eng, S.Paused)  # FAIL_TASK 失败 → 提问
        q = p.questions.pending()
        p.questions.answer(q.question_id, "retry", "已检查代码，重试一次", request_id="sample-retry")
        print(f"  {q.question_id} 回答 retry")
        drive(eng, S.LogPending)
        decide(p)
        drive(eng, S.ManuscriptPending)
        inject_wrong_claims(p, eng)

        report = check_project(p.root)
        print(report.text())
        if not report.ok:
            raise SystemExit("样例项目没有通过契约检查")
        if out.exists():
            shutil.rmtree(out)
        # .fls / .fdb_latexmk 是 LaTeX 的文件清单（含本机绝对路径），不属于论文内容
        shutil.copytree(p.root, out, ignore=shutil.ignore_patterns(".git", "lock", "__pycache__", "*.fls",
                                                                  "*.fdb_latexmk"))
        # 覆盖仓库根目录 .gitignore 中的 *.log、build/ 等规则：样例里的所有文件都要提交
        (out / ".gitignore").write_text("!*\n!*/\n", encoding="utf-8")
        bad = find_abs_paths(out)
        if bad:
            raise SystemExit(f"样例项目含本机绝对路径：{bad}")
        print(f"已写入 {out}")
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    build()
