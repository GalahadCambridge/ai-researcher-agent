"""端到端（引擎，进程内）：创建 → idea → 计划 → 运行 → 日志 → 论文 → Completed，全部用假实现。"""

from __future__ import annotations

import json

from airesearcher.core.models.claim import CheckReport, Claim
from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.models.idea import parse_selected_md
from airesearcher.core.models.literature import PaperRecord
from airesearcher.core.models.plan import PlanTask, PrecheckItem, parse_plan_md
from airesearcher.core.models.run import RunState
from airesearcher.core.project import Project
from airesearcher.engine.engine import ProjectEngine
from airesearcher.services import runs as run_service
from airesearcher.testing.contracts import check_project

from .helpers import approve_pending, drive

DEV = {"smoke_seconds": 0.3, "poll_seconds": 1, "compile": False}


def make_project(air_home, **dev):
    return Project.create(goal="比较基线、主方法和消融在冒烟任务上的得分", task="tasks/smoke", home=air_home,
                          dev={**DEV, **dev})


def test_full_flow_to_completed(air_home):
    p = make_project(air_home)
    eng = ProjectEngine(p, retry_delay=0)

    drive(eng, S.IdeaPending)
    idea = parse_selected_md((p.root / "idea/selected.md").read_text(encoding="utf-8"))
    assert idea.task["task_config"] == "tasks/smoke" and idea.primary_metric().name == "score"
    for line in (p.root / "idea/literature.jsonl").read_text(encoding="utf-8").splitlines():
        PaperRecord.model_validate_json(line)
    # 退回一次：生成新版本并写明意见
    approve_pending(p, "changes_requested", "请把研究问题写得更具体")
    drive(eng, S.IdeaPending)
    assert "请把研究问题写得更具体" in (p.root / "idea/selected.md").read_text(encoding="utf-8")
    assert p.archive.latest("idea/selected.md").version == 2
    approve_pending(p)

    drive(eng, S.PlanPending)
    plan = parse_plan_md((p.root / "plan/experiment_plan.md").read_text(encoding="utf-8"))
    assert [e.kind for e in plan.experiments] == ["baseline", "main", "ablation"]
    assert plan.idea_ref.version == 2
    tasks = [PlanTask.model_validate(t) for t in json.loads((p.root / "plan/tasks.json").read_text(encoding="utf-8"))]
    assert len(tasks) == 9
    [PrecheckItem.model_validate(c) for c in json.loads((p.root / "plan/precheck.json").read_text(encoding="utf-8"))]
    approve_pending(p)

    drive(eng, S.LogPending, timeout=90)
    runs = run_service.list_runs(p.root)
    assert len(runs) == 9 and all(r.status.state == RunState.succeeded for r in runs)
    for name in ("run.json", "status.json", "metrics.jsonl", "environment.json", "resources.json"):
        assert (p.root / "runs" / runs[0].record.run_id / name).exists()
    c1 = run_service.load_aggregate(p.root, "C1")
    assert {tuple(r.group.values()) for r in c1.rows} == {("baseline",), ("main",)}
    assert c1.rows[0].n == 3 and c1.rows[0].ci95 is not None
    assert run_service.recompute_aggregate(p.root, "C1").rows == c1.rows  # 可从原始指标重算
    assert (p.root / "logs/round_1.md").exists()
    approve_pending(p)

    drive(eng, S.ManuscriptPending)
    lines = (p.root / "paper/claims.jsonl").read_text(encoding="utf-8").splitlines()
    claims = [Claim.model_validate_json(x) for x in lines]
    assert claims and all(c.evidence for c in claims)
    assert "\\airval" in (p.root / "paper/sections/results.tex").read_text(encoding="utf-8")
    review = CheckReport.model_validate_json((p.root / "paper/review/review_v1.json").read_text(encoding="utf-8"))
    assert review.counts["blocker"] == 0
    assert not [i for i in review.unbound_issues if i.code == "NUM_UNBOUND"]  # n 与 p 值都经 \\airval 绑定
    assert {v.stat for c in claims for v in c.values} >= {"n", "p_value"}
    paper_v = p.archive.latest_version("paper")
    assert paper_v.exclude == ["build/**", "review/**", ".template_check_default.json"]
    assert p.archive.latest("paper/build/main.pdf") is not None
    [a] = p.approvals.pending()
    assert a.kind == "manuscript" and a.target.artifact_id == "paper"
    approve_pending(p)

    drive(eng, S.Completed)
    assert set(eng.ck.approved) == {"idea", "plan", "log", "manuscript"}
    report = check_project(p.root)  # 假实现写出的所有文件都符合契约
    assert report.ok, report.text()
    types = [e.type for e in p.events.all()]
    for t in ("project.created", "approval.requested", "approval.decided", "run.created", "run.status_changed",
              "state.changed", "artifact.created"):
        assert t in types


def test_failed_run_question_skip(air_home):
    p = make_project(air_home, smoke_fail={"E2-seed=1": "exit1"})
    eng = ProjectEngine(p, retry_delay=0)
    drive(eng, S.IdeaPending)
    approve_pending(p)
    drive(eng, S.PlanPending)
    approve_pending(p)
    drive(eng, S.Paused, timeout=90)
    q = p.questions.pending()
    assert q is not None and {o.id for o in q.options} == {"skip", "retry", "end"}
    assert "E2-seed=1" in q.text
    p.questions.answer(q.question_id, "skip", request_id="ans-skip")
    drive(eng, S.LogPending, timeout=90)
    c1 = run_service.load_aggregate(p.root, "C1")
    main = next(r for r in c1.rows if r.group == {"method": "main"})
    assert main.n == 2  # 跳过了一个种子
    assert any(e["reason"].startswith("failed") for e in c1.excluded)


def test_failed_run_question_end(air_home):
    p = make_project(air_home, smoke_fail={"E1-seed=0": "exit1"})
    eng = ProjectEngine(p, retry_delay=0)
    drive(eng, S.IdeaPending)
    approve_pending(p)
    drive(eng, S.PlanPending)
    approve_pending(p)
    drive(eng, S.Paused, timeout=90)
    q = p.questions.pending()
    p.questions.answer(q.question_id, "end", request_id="ans-end")
    drive(eng, S.Failed)
    assert eng.ck.reason == "用户选择结束"
