"""FakeExecutor：submit 后立即写好 status.json、metrics.jsonl 等文件，不启动任何进程（详细设计 1 第 10.3 节）。

给实验阶段和论文阶段写测试用：不用等实验跑完，也能得到格式完全正确的运行目录。

    ex = FakeExecutor(project)                                   # 全部成功，指标按种子生成
    ex = FakeExecutor(project, outcomes={
        "E2-seed=1": FakeOutcome.fail("nonzero_exit", stderr="Traceback ... CUDA out of memory"),
        "E3": FakeOutcome(metrics={"score": 0.61}),             # 可以写 run_id、task_key、实验编号或 kind
        "trial": FakeOutcome.missing_metrics(),
    })
    engine = ProjectEngine(project, executor=ex)                # 或 ctx.executor = ex

outcomes 的值也可以是函数 f(record, config) -> FakeOutcome，用来按配置决定结果。
prepare、status、logs、collect、reconcile 都沿用 LocalExecutor，所以运行目录的布局、档案登记和记账与真实执行完全一致。
"""

from __future__ import annotations

import hashlib
import json
import platform
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from airesearcher.core.errors import InvalidState
from airesearcher.core.fsutil import atomic_write_json, now
from airesearcher.core.models.run import (
    TERMINAL_RUN_STATES,
    FailureReason,
    MetricRecord,
    Resources,
    RunRecord,
    RunState,
    RunStatus,
)
from airesearcher.core.project import Project
from airesearcher.runtime.executor import JobHandle, RunSpec
from airesearcher.runtime.local import LocalExecutor

# 默认指标的基准值：主方法 > 消融 > 基线，种子之间有小幅波动，画出来的图像真实结果
KIND_BASE = {"baseline": 0.70, "main": 0.76, "ablation": 0.73, "replication": 0.76, "trial": 0.5, "smoke": 0.5}


@dataclass
class FakeOutcome:
    state: str = "succeeded"  # succeeded / failed / cancelled
    metrics: dict[str, float] | None = None  # None：按 required_metrics 和种子自动生成
    failure_reason: FailureReason | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    wall_seconds: float = 1.0
    files: dict[str, str] = field(default_factory=dict)  # outputs/ 下的文件：{相对路径: 文本内容}
    metric_splits: dict[str, str | None] = field(default_factory=dict)  # 可按指标覆盖 config.eval_split

    @classmethod
    def fail(cls, reason: FailureReason = "nonzero_exit", stderr: str = "", **kw: Any) -> FakeOutcome:
        return cls(state="failed", failure_reason=reason, exit_code=kw.pop("exit_code", 1), stderr=stderr,
                   metrics=kw.pop("metrics", {}), **kw)

    @classmethod
    def missing_metrics(cls, **kw: Any) -> FakeOutcome:
        """进程正常退出，但没有上报必需指标（详细设计 3 第 5.4 节）。"""
        return cls(state="failed", failure_reason="missing_metrics", exit_code=0, metrics={}, **kw)

    @classmethod
    def cancelled(cls, **kw: Any) -> FakeOutcome:
        return cls(state="cancelled", failure_reason="cancelled", exit_code=None, metrics={}, **kw)


OutcomeSpec = FakeOutcome | Callable[[RunRecord, dict], FakeOutcome]


def default_value(record: RunRecord, metric: str) -> float:
    """同一个实验、同一个种子总是得到同一个值（测试结果可重复）。"""
    h = hashlib.sha256(f"{record.experiment_id}|{record.seed}|{metric}".encode()).digest()
    noise = (h[0] / 255 - 0.5) * 0.02  # ±0.01
    return round(KIND_BASE.get(record.kind, 0.7) + noise, 4)


class FakeExecutor(LocalExecutor):
    simulation = True

    def __init__(self, project: Project, outcomes: dict[str, OutcomeSpec] | OutcomeSpec | None = None,
                 python: str | None = None):
        super().__init__(project, python=python or project.config.experiment.get("python") or "python")
        self.outcomes = outcomes
        self.submitted: list[str] = []  # 按提交顺序记录 run_id，测试里可以断言

    def probe(self) -> dict[str, Any]:
        return {**super().probe(), "executor": "fake"}

    def outcome_for(self, record: RunRecord, config: dict) -> FakeOutcome:
        spec: OutcomeSpec | None = None
        if isinstance(self.outcomes, dict):
            for key in (record.run_id, record.task_key, record.experiment_id, record.kind, "*"):
                if key in self.outcomes:
                    spec = self.outcomes[key]
                    break
        elif self.outcomes is not None:
            spec = self.outcomes
        if spec is None:
            return FakeOutcome()
        return spec if isinstance(spec, FakeOutcome) else spec(record, config)

    def submit(self, spec: RunSpec) -> JobHandle:
        rec = spec.record
        self.project.permissions.guard("exec", "local", actor="executor")
        st = self.status(rec.run_id)
        if st.state != RunState.queued:
            raise InvalidState(f"运行 {rec.run_id} 的状态是 {st.state.value}，不能提交")
        d = self.run_dir(rec.run_id)
        out = self.outcome_for(rec, spec.config)
        self._write_files(d, rec, out, eval_split=spec.config.get("eval_split", "test"))
        self.submitted.append(rec.run_id)
        return JobHandle(run_id=rec.run_id, wrapper_pid=None)

    def cancel(self, run_id: str) -> None:
        st = self.status(run_id)
        if st.state in TERMINAL_RUN_STATES:
            return
        self._write_status(st.model_copy(update={"state": RunState.cancelled, "failure_reason": "cancelled",
                                                 "ended_at": now()}))

    def _write_files(self, d: Path, rec: RunRecord, out: FakeOutcome, eval_split: str = "test") -> None:
        start = now()
        end = start + timedelta(seconds=out.wall_seconds)
        metrics = out.metrics
        if metrics is None:
            metrics = {m: default_value(rec, m) for m in (rec.required_metrics or ["score"])}
        lines, stdout = [], []
        for seq, (name, value) in enumerate(metrics.items(), start=1):
            split = out.metric_splits.get(name, eval_split)
            m = MetricRecord(seq=seq, ts=end, name=name, value=float(value), split=split)
            lines.append(m.model_dump_json() + "\n")
            stdout.append("@@AIR_METRIC " + json.dumps({"name": name, "value": float(value), "split": split,
                                                        "step": None}) + "\n")
        (d / "metrics.jsonl").write_text("".join(lines), encoding="utf-8")
        (d / "stdout.log").write_text(out.stdout + "".join(stdout), encoding="utf-8")
        (d / "stderr.log").write_text(out.stderr, encoding="utf-8")
        (d / "wrapper.log").write_text("FakeExecutor：没有启动进程，结果是预置的\n", encoding="utf-8")
        for rel, text in out.files.items():
            p = d / "outputs" / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        atomic_write_json(d / "resources.json", Resources(wall_seconds=out.wall_seconds,
                                                          cpu_seconds=out.wall_seconds, max_rss_mb=50.0))
        exit_code = out.exit_code if out.exit_code is not None else (0 if out.state == "succeeded" else None)
        self._write_status(RunStatus(
            run_id=rec.run_id, state=RunState(out.state), host=platform.node(), started_at=start,
            heartbeat_at=end, ended_at=end, exit_code=exit_code, failure_reason=out.failure_reason,
        ))
