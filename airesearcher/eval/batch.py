"""批量运行评价轨迹（详细设计 1 第 12.1 节）。

    air eval run eval/configs/smoke_batch.yaml

每条轨迹 = 一个独立的项目工作区 <results>/<name>/<arm>/<instance>/rep<k>/，在本进程里用引擎直接跑（不需要 air serve）。
- 审批策略 auto_approve：审批提交 approval_delay_s 秒后自动批准（decided_by="auto-approve(eval)"）；stop：遇到审批就停；
- 问题策略 auto_default：有默认选项的问题 question_delay_s 秒后自动选它（answered_by="auto-default(eval)"），
  没有默认选项的问题让轨迹停下，记为 needs_intervention；stop：任何问题都让轨迹停下；
- 结束后在 <results>/<name>/summary.csv 写一行；轨迹目录里的 .eval/result.json 是同样的内容。
  批量中途中断后再运行同一命令：已完成的轨迹跳过，没完成的从检查点继续。
"""

from __future__ import annotations

import copy
import csv
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from airesearcher.core.fsutil import atomic_write_json, now, read_json
from airesearcher.core.locks import ProjectLock
from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.project import Project
from airesearcher.core.workspace import repo_root
from airesearcher.engine.engine import ProjectEngine
from airesearcher.services import runs as run_service

AUTO_APPROVER = "auto-approve(eval)"
AUTO_ANSWERER = "auto-default(eval)"
TERMINAL = {S.Completed: "completed", S.Failed: "failed", S.BudgetExhausted: "budget_exhausted"}
SUMMARY_COLUMNS = [
    "arm", "instance", "rep", "outcome", "final_state", "reason", "llm_usd", "llm_tokens", "llm_calls",
    "wall_hours", "cpu_hours", "gpu_hours", "storage_gb", "runs_succeeded", "runs_failed", "approvals_auto",
    "questions_asked", "questions_auto", "interventions", "seconds", "workspace",
]


class Instance(BaseModel):
    """一个研究问题实例：eval/instances/*.yaml。"""

    id: str
    idea_text: str
    task: str = "tasks/smoke"
    title: str | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)
    literature_snapshots: str | None = None  # 检索快照目录（相对实例文件）；设置后文献阶段回放，不联网
    project: dict[str, Any] = Field(default_factory=dict)  # 其他 project.yaml 字段


class BatchConfig(BaseModel):
    name: str
    instances: list[str]
    repeats: int = 1
    arms: dict[str, dict[str, Any]]  # 每组覆盖的 project.yaml 字段，如 {evidence_check: false}
    fixed: dict[str, Any] = Field(default_factory=dict)  # 所有组共用的 project.yaml 字段（models、budget、dev……）
    prompt_pins: str | None = None  # 固定提示版本的文件（{prompt_id: version}），写进每个项目的 prompt_pins
    approval_policy: Literal["auto_approve", "stop"] = "auto_approve"
    question_policy: Literal["auto_default", "stop"] = "auto_default"
    approval_delay_s: float = 30.0
    question_delay_s: float = 30.0
    max_parallel: int = 1
    timeout_hours: float = 4.0  # 单条轨迹的墙钟上限，超过记为 timeout
    results_dir: str = "eval/results"  # 相对路径相对于运行命令时的当前目录


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def _resolve(path: str, base: Path) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p
    for b in (base, repo_root()):
        if (b / p).exists():
            return (b / p).resolve()
    return (base / p).resolve()


def load_config(path: Path) -> tuple[BatchConfig, list[Instance], Path]:
    path = Path(path).resolve()
    cfg = BatchConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    instances = []
    for ip in cfg.instances:
        f = _resolve(ip, path.parent)
        inst = Instance.model_validate(yaml.safe_load(f.read_text(encoding="utf-8")))
        if inst.literature_snapshots:
            inst.literature_snapshots = str(_resolve(inst.literature_snapshots, f.parent))
        instances.append(inst)
    ids = [i.id for i in instances]
    if len(set(ids)) != len(ids):
        raise ValueError(f"实例 id 重复：{ids}")
    return cfg, instances, path


class Trajectory:
    def __init__(self, cfg: BatchConfig, arm: str, inst: Instance, rep: int, root: Path, pins: dict[str, int],
                 log: Any = None):
        self.cfg, self.arm, self.inst, self.rep, self.root, self.pins = cfg, arm, inst, rep, root, pins
        self.log = log or (lambda msg: None)
        self.counts = {"approvals_auto": 0, "questions_auto": 0}

    @property
    def result_path(self) -> Path:
        return self.root / ".eval" / "result.json"

    def _project(self) -> Project:
        if (self.root / "project.yaml").exists():  # 上次中断：从检查点继续
            return Project.open(self.root)
        fields = _merge(_merge(self.cfg.fixed, self.cfg.arms[self.arm]), self.inst.project)
        if self.inst.literature_snapshots:
            fields = _merge(fields, {"literature": {"replay_snapshots": self.inst.literature_snapshots}})
        if self.pins:
            fields["prompt_pins"] = self.pins
        fields.setdefault("constraints", self.inst.constraints)
        return Project.create(goal=self.inst.idea_text, task=self.inst.task, title=self.inst.title,
                              root=self.root, register=False, **fields)

    def run(self) -> dict:
        if self.result_path.exists():
            return read_json(self.result_path)
        t0 = time.monotonic()
        p = self._project()
        eng = ProjectEngine(p, retry_delay=5.0)
        deadline = t0 + self.cfg.timeout_hours * 3600
        outcome = None
        lock = ProjectLock(p.root)
        lock.acquire()  # 与 air serve 一样占住项目锁，防止两个进程同时跑同一条轨迹
        try:
            while outcome is None:
                tick = eng.tick()
                outcome = self._handle_waiting(p, eng)
                if outcome is None and time.monotonic() > deadline:
                    outcome = "timeout"
                if outcome is None and tick.kind != "transition" and (tick.kind != "step" or tick.seconds):
                    time.sleep(min(max(tick.seconds, 0.05), 1.0))
        finally:
            eng.flush_accounting()
            lock.release()
        row = self._row(p, eng, outcome, time.monotonic() - t0)
        atomic_write_json(self.result_path, row)
        self.log(f"{self.arm}/{self.inst.id}/rep{self.rep}：{outcome}（{row['seconds']} 秒，${row['llm_usd']}）")
        return row

    def _handle_waiting(self, p: Project, eng: ProjectEngine) -> str | None:
        st = eng.state
        if st in TERMINAL:
            return TERMINAL[st]
        t = now()
        for a in p.approvals.pending():
            if self.cfg.approval_policy == "stop":
                return "needs_intervention"
            if (t - a.created_at).total_seconds() >= self.cfg.approval_delay_s:
                p.approvals.decide(a.approval_id, "approved", a.target.sha256, comment="评价轨迹自动批准",
                                   request_id=f"{AUTO_APPROVER}-{a.approval_id}", by=AUTO_APPROVER)
                self.counts["approvals_auto"] += 1
        q = p.questions.pending()
        if q is not None:
            default = next((o for o in q.options if o.default), None)
            if self.cfg.question_policy == "stop" or default is None:
                return "needs_intervention"
            if q.created_at is None or (t - q.created_at).total_seconds() >= self.cfg.question_delay_s:
                p.questions.answer(q.question_id, default.id, request_id=f"{AUTO_ANSWERER}-{q.question_id}",
                                   by=AUTO_ANSWERER)
                self.counts["questions_auto"] += 1
        elif st == S.Paused and not p.approvals.pending():
            return "paused"  # 暂停了又没有问题可答（如阶段出错次数过多）：轨迹到此为止
        return None

    def _row(self, p: Project, eng: ProjectEngine, outcome: str, seconds: float) -> dict:
        used = p.budget.used()
        runs = run_service.list_runs(p.root)
        asked = sum(1 for e in p.events.all() if e.type == "question.asked")
        row = {
            "arm": self.arm, "instance": self.inst.id, "rep": self.rep, "outcome": outcome,
            "final_state": eng.state.value, "reason": eng.ck.reason,
            **{k: round(used.get(k, 0.0), 6) for k in ("llm_usd", "llm_tokens", "llm_calls", "wall_hours",
                                                        "cpu_hours", "gpu_hours", "storage_gb")},
            "runs_succeeded": sum(1 for r in runs if r.status.state.value == "succeeded"),
            "runs_failed": sum(1 for r in runs if r.status.state.value == "failed"),
            **self.counts, "questions_asked": asked, "interventions": asked,  # 人工干预数 = 提问次数（12.1）
            "seconds": round(seconds, 1), "workspace": str(p.root),
        }
        return {k: row[k] for k in SUMMARY_COLUMNS}


def run_batch(config_path: Path, only_arms: list[str] | None = None, results_dir: Path | None = None,
              log: Any = print) -> Path:
    """跑完配置中的所有轨迹，返回 summary.csv 的路径。"""
    cfg, instances, path = load_config(config_path)
    out_dir = (Path(results_dir or cfg.results_dir) / cfg.name).resolve()
    unknown = set(only_arms or []) - set(cfg.arms)
    if unknown:
        raise ValueError(f"配置里没有这些组：{', '.join(sorted(unknown))}（有：{', '.join(cfg.arms)}）")
    pins: dict[str, int] = {}
    if cfg.prompt_pins:
        pins = {str(k): int(v) for k, v in (yaml.safe_load(
            _resolve(cfg.prompt_pins, path.parent).read_text(encoding="utf-8")) or {}).items()}
    arms = [a for a in cfg.arms if not only_arms or a in only_arms]
    trajs = [Trajectory(cfg, arm, inst, rep, out_dir / arm / inst.id / f"rep{rep}", pins, log)
             for arm in arms for inst in instances for rep in range(1, cfg.repeats + 1)]
    log(f"批量 {cfg.name}：{len(trajs)} 条轨迹（{len(arms)} 组 × {len(instances)} 个实例 × {cfg.repeats} 次），"
        f"结果放在 {out_dir}")
    lock = threading.Lock()
    rows: list[dict] = []

    def one(t: Trajectory) -> None:
        try:
            row = t.run()
        except Exception as e:  # 一条轨迹出错不影响其他轨迹；照常计入分母
            row = {c: "" for c in SUMMARY_COLUMNS} | {"arm": t.arm, "instance": t.inst.id, "rep": t.rep,
                                                       "outcome": "error", "reason": f"{type(e).__name__}: {e}",
                                                       "workspace": str(t.root)}
            log(f"{t.arm}/{t.inst.id}/rep{t.rep}：出错 {e}")
        with lock:
            rows.append(row)

    with ThreadPoolExecutor(max_workers=max(1, cfg.max_parallel)) as pool:
        list(pool.map(one, trajs))
    rows.sort(key=lambda r: (str(r["arm"]), str(r["instance"]), int(r["rep"])))
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = out_dir / "summary.csv"
    with open(summary, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SUMMARY_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    (out_dir / "config.json").write_text(json.dumps(cfg.model_dump(mode="json"), ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    done = sum(1 for r in rows if r["outcome"] == "completed")
    log(f"完成：{done}/{len(rows)} 条轨迹走到 Completed。汇总：{summary}")
    return summary
