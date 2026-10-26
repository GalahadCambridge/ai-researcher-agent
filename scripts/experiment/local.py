"""FakeLLM + LocalExecutor：真实本地训练，不调用大模型 API。默认只执行 trial。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from airesearcher.core.fsutil import sha256_file
from airesearcher.core.locks import ProjectLock
from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.models.run import TERMINAL_RUN_STATES
from airesearcher.core.project import Project
from airesearcher.engine import checkpoint
from airesearcher.engine.engine import ProjectEngine
from airesearcher.engine.stage import describe
from airesearcher.runtime.local import LocalExecutor
from airesearcher.runtime.task import load_task_config
from airesearcher.services.runs import list_runs
from airesearcher.stages.experiment.stage import uses_gpu
from airesearcher.testing.contracts import check_project
from airesearcher.testing.fake_llm import fake_gateway
from scripts.experiment.demo import summary
from scripts.experiment.workspace import copy_pending_plan


def configure_local(project: Project, scope: str):
    experiment = {
        **project.config.experiment,
        "python": sys.executable,
        "allow_local_execution": True,
        "stop_after_trials": scope == "trial",
        "llm_backend": "fake",
    }
    project.save_config(
        project.config.model_copy(update={"experiment": experiment}),
        "Explicitly enable local execution; retain FakeLLM",
        actor="user",
    )


def probe_cuda(project: Project):
    tasks = json.loads((project.root / "plan/tasks.json").read_text(encoding="utf-8"))
    if not any(t["config"].get("device") == "cuda" for t in tasks):
        print("设备检查：当前计划仅包含 CPU 任务。", flush=True)
        return
    code = (
        "import json,torch; ok=torch.cuda.is_available(); "
        "print(json.dumps({'available':ok,'torch':torch.__version__,"
        "'gpu':torch.cuda.get_device_name(0) if ok else None})); "
        "assert ok, '计划要求 CUDA，禁止回退 CPU'; "
        "assert torch.ones(4,device='cuda').sum().item()==4"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise ValueError(f"CUDA 检查失败，未启动训练：{result.stdout.strip()} {result.stderr[-1200:]}")
    print("设备检查通过：" + result.stdout.strip(), flush=True)


def print_runs(project, seen):
    for view in list_runs(project.root):
        record, status = view.record, view.status
        if seen.get(record.run_id) == status.state:
            continue
        seen[record.run_id] = status.state
        d = project.root / "runs" / record.run_id
        config = json.loads((d / "config.yaml").read_text(encoding="utf-8"))
        device = "GPU/CUDA" if uses_gpu(config, load_task_config(project.config.task)) else "CPU"
        print(
            f"[{status.state.value}] {config['method']} seed={record.seed} "
            f"{'trial' if record.kind == 'trial' else 'formal'} | 计划设备={device} | {record.run_id}",
            flush=True,
        )
        result = d / "outputs/result.json"
        if status.state in TERMINAL_RUN_STATES and result.exists():
            data = json.loads(result.read_text(encoding="utf-8"))
            print(
                f"  实际设备={data['device']} GPU={data.get('gpu_name')} "
                f"train_seconds={data['train_seconds']:.3f} metrics={data['metrics']}",
                flush=True,
            )


def execute(project: Project, scope: str, *, max_seconds: float = 1800):
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    executor = LocalExecutor(project, python=sys.executable)
    gateway = fake_gateway(project)
    engine = ProjectEngine(project, executor=executor, llm=gateway, retry_delay=0)
    if engine.state in (S.Paused, S.Failed):
        if project.questions.pending():
            raise ValueError("存在待处理的失败问题，请先明确选择处理方式；本入口不自动重试或跳过")
        engine.request_action("resume")
    deadline, seen = time.monotonic() + max_seconds, {}
    last_reason = None
    try:
        while time.monotonic() < deadline:
            tick = engine.tick()
            if tick.result:
                reason = describe(tick.result)
                if reason != last_reason:
                    print(reason, flush=True)
                    last_reason = reason
            print_runs(project, seen)
            if engine.state in (S.Analyzing, S.Paused, S.Failed, S.BudgetExhausted):
                break
            time.sleep(min(0.5, max(0.1, tick.seconds)))
        else:
            raise TimeoutError("本地验收入口达到时间上限，取消其活动运行并保留记录")
    except BaseException:
        # CLI 中断时不留下不受监视的真实训练；已结束的记录不改写。
        for view in list_runs(project.root):
            if view.status.state not in TERMINAL_RUN_STATES:
                executor.cancel(view.record.run_id)
        engine.request_action("pause")
        engine.tick()
        raise
    report = check_project(project.root, owners=["实验"])
    print(report.text(), flush=True)
    if not report.ok:
        raise ValueError("运行目录协议检查未通过")
    execution = summary(project)
    result = {
        "state": engine.state.value,
        "scope": scope,
        "executor": "LocalExecutor",
        "simulation": False,
        "trials_complete": execution["trials_complete"],
        "complete": execution["complete"],
        "trial_attempts": execution["trial_attempts"],
        "formal_attempts": execution["formal_attempts"],
        "successful_formal_tasks": execution["successful_formal_tasks"],
        "execution_llm_calls": len(gateway.backend.calls),
        "llm_usd": project.budget.used().get("llm_usd", 0),
        "workspace": str(project.root),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    if gateway.backend.calls or result["llm_usd"] or execution["simulation"]:
        raise ValueError("FakeLLM/真实运行边界检查失败")
    if scope == "trial":
        if not execution["trials_complete"] or execution["formal_attempts"]:
            raise ValueError(f"trial 验收未通过：{engine.ck.reason}")
    elif not execution["complete"] or engine.state != S.Analyzing:
        raise ValueError(f"正式执行未完成：{engine.ck.reason}")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-workspace", type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--scope", choices=["trial", "all"], default="trial")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--approve-plan-for-local", action="store_true", help="批准独立副本用于真实本地训练，原计划审批保持不变"
    )
    args = parser.parse_args()
    if not args.approve_plan_for_local:
        parser.error("真实训练需要 --approve-plan-for-local；不会默认把模拟审批用于真实训练")
    if not args.resume and args.plan_workspace is None:
        parser.error("新执行工作区需要 --plan-workspace")
    if args.resume and args.plan_workspace is not None:
        parser.error("恢复使用 --workspace，不再复制 --plan-workspace")
    print("[LOCAL TRAINING + FAKE LLM] 真实训练；无大模型 API 调用。", flush=True)
    try:
        if args.resume:
            project = Project.open(args.workspace.resolve())
            marker = project.root / "artifacts/LOCAL_EXECUTION.json"
            if not marker.is_file() or project.config.experiment.get("allow_local_execution") is not True:
                raise ValueError("只能恢复此本地入口创建的真实运行工作区")
        else:
            project = copy_pending_plan(args.plan_workspace, args.workspace, simulation=False)
        with ProjectLock(project.root):
            configure_local(project, args.scope)
            probe_cuda(project)
            ck = checkpoint.load(project.root)
            if ck and ck.pending_approval:
                approval = project.approvals.get(ck.pending_approval)
                project.approvals.decide(
                    approval.approval_id,
                    "approved",
                    approval.target.sha256,
                    comment="User authorized real local execution in this isolated copy; retain FakeLLM",
                    by="user:local-cli",
                )
            execute(project, args.scope)
            approved = checkpoint.load(project.root).approved["plan"]
            if sha256_file(project.root / "plan/experiment_plan.md") != approved.sha256:
                raise ValueError("已批准的计划文件发生变化")
    except (ValueError, OSError, TimeoutError) as exc:
        parser.exit(1, f"本地执行停止：{exc}\n")


if __name__ == "__main__":
    main()
