"""仅使用 FakeLLM 的计划开发演示。停在 PlanPending，不批准计划、不执行训练。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from airesearcher.core.fsutil import dumps
from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.models.frontmatter import render_front_matter
from airesearcher.core.models.idea import parse_selected_md
from airesearcher.core.project import Project
from airesearcher.engine import checkpoint
from airesearcher.engine.engine import ProjectEngine
from airesearcher.engine.stage import NeedsApproval, describe
from airesearcher.stages.plan.stage import PROMPT, PlanStage
from airesearcher.testing.fake_executor import FakeExecutor
from airesearcher.testing.fake_llm import fake_gateway

from .fixtures import fixture_idea, leaking_draft, offline_models, preset_draft


class FixtureIdeaStage:
    """只提交一个明确标注的测试/导入选题；不执行文献检索，不调用模型。"""

    name = "idea"

    def __init__(self, ref):
        self.ref = ref

    def step(self, ctx):
        return NeedsApproval(
            self.ref,
            "idea",
            "Input idea for offline planning demonstration",
            "Only for an isolated development workspace",
        )


def imported_idea(path: Path):
    source = Project.open(path)
    ck = checkpoint.load(source.root)
    if ck is None or "idea" not in ck.approved:
        raise ValueError("源项目没有引擎记录的已批准选题，请先在原项目完成选题审批")
    ref = ck.approved["idea"]
    if not any(a.status == "approved" and a.target == ref for a in source.approvals.list(kind="idea")):
        raise ValueError("源项目缺少与已批准版本匹配的审批记录")
    text = source.archive.read_text(ref)
    idea = parse_selected_md(text)
    return idea, text, source.config.task, {"source_project": str(source.root), "source_ref": ref.model_dump()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True, help="必须为空；结果保存在此新工作区")
    parser.add_argument("--task", choices=["smoke", "text_cls_lowres"], help="默认 smoke")
    parser.add_argument("--scenario", choices=["happy", "repair", "fail", "bad-json", "revise"], default="happy")
    parser.add_argument("--from-project", type=Path, help="只读导入另一个项目已批准的选题；不改原项目")
    args = parser.parse_args()
    root = args.workspace.expanduser().resolve()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        parser.error("工作区必须为空；请换一个新目录名，已有结果不会覆盖")
    try:
        if args.from_project:
            if args.task:
                parser.error("--from-project 自动使用源项目任务，不要再指定 --task")
            idea, text, task_path, provenance = imported_idea(args.from_project)
        else:
            task_path = f"tasks/{args.task or 'smoke'}"
            idea = fixture_idea(task_path)
            text = render_front_matter(
                idea.model_dump(mode="json"), "# Test idea\n\nPreset input for offline plan-stage development only.\n"
            )
            provenance = {
                "mode": "synthetic_fixture",
                "note": "Synthetic fixture, not output from the literature agent",
            }
        good = preset_draft(idea, task_path)
        revised = preset_draft(idea, task_path, train_size=100)
        revised["experiments"][0]["purpose"] += " (preset revised version)"
    except (ValueError, OSError, KeyError) as exc:
        parser.error(str(exc))
    responses = {
        "happy": [good],
        "repair": [leaking_draft(good), good],
        "fail": [leaking_draft(good)],
        "bad-json": [good],
        "revise": [good, revised],
    }[args.scenario]
    project = Project.create(
        root=root,
        register=False,
        task=task_path,
        goal=idea.research_question,
        title="FakeLLM planning workspace",
        models=offline_models(),
        llm_cache="off",
        dev={"stage_impl": {"idea": "fake", "plan": "real", "experiment": "fake", "writing": "fake"}},
    )
    provenance_ref = project.archive.put("idea/import_source.json", "other", dumps(provenance), producer="system")
    ref = project.archive.put(
        "idea/selected.md",
        "idea",
        text,
        parents=[provenance_ref],
        producer="system",
        note="Offline input; see import_source.json for the approved version and origin",
    )
    gateway = fake_gateway(project, {PROMPT: responses}, bad_json_once=args.scenario == "bad-json")
    executor = FakeExecutor(project)
    engine = ProjectEngine(
        project,
        llm=gateway,
        executor=executor,
        retry_delay=0,
        stages={"idea": FixtureIdeaStage(ref), "plan": PlanStage()},
    )
    print("[FAKE ONLY] 模型回复来自本地预设；无真实 API、无联网、无训练。")
    print("仅在这个独立工作区自动确认测试/导入选题；不会自动批准实验计划。")
    revisions_requested = False
    for _ in range(40):
        tick = engine.tick()
        if tick.result is not None:
            print(describe(tick.result))
        if engine.state == S.IdeaPending:
            [a] = project.approvals.pending()
            project.approvals.decide(
                a.approval_id,
                "approved",
                a.target.sha256,
                comment="Test-only input approval; not a research-quality assessment",
                by="demo-fixture",
            )
            continue
        if engine.state == S.PlanPending:
            if args.scenario == "revise" and not revisions_requested:
                [a] = project.approvals.pending()
                project.approvals.decide(
                    a.approval_id,
                    "changes_requested",
                    a.target.sha256,
                    comment="Test revision request; use the next preset response",
                    by="demo-fixture",
                )
                revisions_requested = True
                continue
            break
        if engine.state in (S.Paused, S.Failed, S.BudgetExhausted):
            break
        if engine.state == S.Executing:
            raise RuntimeError("演示不允许进入执行阶段")
    else:
        raise RuntimeError(f"超过开发步骤上限，当前状态：{engine.state.value}")
    assert not executor.submitted and not list((root / "runs").glob("*/run.json"))
    used = project.budget.used()
    assert used.get("llm_usd", 0) == 0, "FakeLLM 费用应为 0"
    print(
        json.dumps(
            {
                "state": engine.state.value,
                "backend": type(gateway.backend).__name__,
                "fake_model_calls": len(gateway.backend.calls),
                "llm_usd": used.get("llm_usd", 0),
                "training_submissions": len(executor.submitted),
                "workspace": str(root),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    generated = [
        root / p for p in ("plan/experiment_plan.md", "plan/tasks.json", "plan/precheck.json") if (root / p).is_file()
    ]
    for path in generated:
        print(path)
    if not generated:
        print("尚未生成计划文件，请先处理上面的输入检查问题。")
    expected = S.Paused if args.scenario == "fail" else S.PlanPending
    if engine.state != expected:
        raise RuntimeError(f"没有到达预期的 {expected.value}：{engine.ck.reason}")
    if args.scenario == "fail":
        if len(gateway.backend.calls) != 3 or project.questions.pending() is None:
            raise RuntimeError(f"没有走完预期的两次模拟修订，请先处理输入问题：{engine.ck.reason}")
    elif any(c["status"] == "fail" for c in json.loads((root / "plan/precheck.json").read_text(encoding="utf-8"))):
        raise RuntimeError("计划仍有 fail，不能视为演示通过")


if __name__ == "__main__":
    main()
