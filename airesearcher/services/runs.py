"""给其他模块的读取服务：运行与汇总结果（详细设计 3 第 11 节）。只读文件、不调模型。

主干的 API（/runs）和论文阶段（画图、核验）都通过这些函数读数据，不直接解析文件。

【实验】同学接手时：这里的 compute_aggregate() 是按文档 8.2 写的确定性汇总，
真实的 stages/experiment/aggregate.py 可以直接调用它，也可以替换为自己的实现（保持输出格式不变）。
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import airesearcher
from airesearcher.core.archive import Archive
from airesearcher.core.errors import NotFound
from airesearcher.core.events import Events
from airesearcher.core.fsutil import now, read_json, read_jsonl
from airesearcher.core.models.aggregate import Aggregate, AggregateRow, Contrast
from airesearcher.core.models.common import VersionRef
from airesearcher.core.models.plan import Comparison, ExperimentPlan, parse_plan_md
from airesearcher.core.models.run import (
    MetricRecord,
    Resources,
    RunLabel,
    RunRecord,
    RunState,
    RunStatus,
    RunView,
)

from . import stats

PLAN_ID = "plan/experiment_plan.md"
RUN_LABELS = "artifacts/run_labels.jsonl"
ACTIVE = {RunState.created, RunState.preparing, RunState.queued, RunState.running}


def _archive(root: Path) -> Archive:
    return Archive(root, Events(root))


def _read_status(d: Path, run_id: str) -> RunStatus:
    p = d / "status.json"
    try:
        return RunStatus.model_validate(read_json(p))
    except (FileNotFoundError, ValueError):
        return RunStatus(run_id=run_id, state=RunState.unknown, host="local")


def load_labels(root: Path) -> dict[str, RunLabel]:
    """人工标注：同一运行以最后一行为准。"""
    out: dict[str, RunLabel] = {}
    for row in read_jsonl(Path(root) / RUN_LABELS):
        try:
            lab = RunLabel.model_validate(row)
        except ValueError:
            continue
        out[lab.run_id] = lab
    return out


def read_metrics(root: Path, run_id: str) -> list[MetricRecord]:
    rows = read_jsonl(Path(root) / "runs" / run_id / "metrics.jsonl")
    return [MetricRecord.model_validate(r) for r in rows]


def load_run(root: Path, run_id: str, with_metrics: bool = True,
             labels: dict[str, RunLabel] | None = None) -> RunView:
    d = Path(root) / "runs" / run_id
    if not (d / "run.json").exists():
        raise NotFound(f"找不到运行 {run_id}")
    rec = RunRecord.model_validate(read_json(d / "run.json"))
    res = Resources.model_validate(read_json(d / "resources.json")) if (d / "resources.json").exists() else None
    env = read_json(d / "environment.json") if (d / "environment.json").exists() else None
    return RunView(
        record=rec, status=_read_status(d, run_id), metrics=read_metrics(root, run_id) if with_metrics else [],
        resources=res, environment=env, label=(labels if labels is not None else load_labels(root)).get(run_id),
    )


def list_runs(
    root: Path, experiment_id: str | None = None, state: str | None = None, kind: str | None = None
) -> list[RunView]:
    runs_dir = Path(root) / "runs"
    out = []
    if not runs_dir.exists():
        return out
    labels = load_labels(root)
    for d in sorted(runs_dir.iterdir()):
        if not (d / "run.json").exists():
            continue
        try:
            rv = load_run(root, d.name, with_metrics=False, labels=labels)
        except (ValueError, NotFound):
            continue
        if experiment_id and rv.record.experiment_id != experiment_id:
            continue
        if state and rv.status.state.value != state:
            continue
        if kind and rv.record.kind != kind:
            continue
        out.append(rv)
    return sorted(out, key=lambda r: (r.record.created_at, r.record.run_id))


def active_runs(root: Path) -> list[str]:
    """状态为 created/preparing/queued/running 的运行（引擎据此决定是否调用 background()）。"""
    return [r.record.run_id for r in list_runs(root) if r.status.state in ACTIVE]


def load_plan(root: Path, version: int | None = None) -> ExperimentPlan:
    root = Path(root)
    if version is None:
        p = root / PLAN_ID
        if not p.exists():
            raise NotFound("还没有实验计划")
        return parse_plan_md(p.read_text(encoding="utf-8"))
    arch = _archive(root)
    for v in arch.versions(PLAN_ID):
        if v.ref.version == version:
            return parse_plan_md(arch.read_text(v.ref))
    raise NotFound(f"找不到计划版本 v{version}")


def load_aggregate(root: Path, aggregate_id: str, version: int | None = None) -> Aggregate:
    root = Path(root)
    aid = f"artifacts/aggregates/{aggregate_id}.json"
    if version is None:
        p = root / aid
        if not p.exists():
            raise NotFound(f"找不到汇总结果 {aggregate_id}")
        return Aggregate.model_validate(read_json(p))
    arch = _archive(root)
    for v in arch.versions(aid):
        if v.ref.version == version:
            return Aggregate.model_validate_json(arch.get(v.ref))
    raise NotFound(f"找不到汇总结果 {aggregate_id} v{version}")


def _norm(d: dict) -> str:
    return json.dumps(d, sort_keys=True, ensure_ascii=False)


def resolve_value(
    root: Path, aggregate_id: str, group: dict, metric: str, stat: str, contrast: dict | None = None,
    version: int | None = None, aggregate: Aggregate | None = None,
) -> float:
    """按 (aggregate_id, group, metric, stat) 取一个数字。stat 为 diff_mean / p_value 时需给 contrast={a, b}。"""
    agg = aggregate or load_aggregate(root, aggregate_id, version)
    if stat in ("diff_mean", "p_value"):
        if not contrast:
            raise ValueError(f"stat={stat} 需要 contrast")
        for c in agg.contrasts:
            if c.metric == metric and _norm(c.a) == _norm(contrast["a"]) and _norm(c.b) == _norm(contrast["b"]):
                v = getattr(c, stat)
                if v is None:
                    raise ValueError(f"{aggregate_id} 的 {stat} 无法计算（样本不足）")
                return float(v)
        raise NotFound(f"{aggregate_id} 中找不到对比 {contrast}")
    for row in agg.rows:
        if row.metric == metric and _norm(row.group) == _norm(group):
            if stat == "ci95_low":
                return float(row.ci95[0]) if row.ci95 else float("nan")
            if stat == "ci95_high":
                return float(row.ci95[1]) if row.ci95 else float("nan")
            v = getattr(row, stat)
            return float("nan") if v is None else float(v)
    raise NotFound(f"{aggregate_id} 中找不到分组 {group} / {metric}")


def _run_value(root: Path, run_id: str, metric: str) -> float | None:
    vals = [m.value for m in read_metrics(root, run_id) if m.name == metric]
    return vals[-1] if vals else None


def _run_fields(root: Path, rv: RunView) -> dict[str, Any]:
    cfg_path = Path(root) / "runs" / rv.record.run_id / "config.yaml"
    cfg: dict[str, Any] = {}
    if cfg_path.exists():
        import yaml

        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    return {**cfg, "experiment_id": rv.record.experiment_id, "kind": rv.record.kind}


def compute_aggregate(
    root: Path, plan: ExperimentPlan, plan_ref: VersionRef, comparison: Comparison, eval_condition: str = "final",
) -> Aggregate:
    """从 runs/ 的原始 metrics.jsonl 确定性地计算一个 comparison 的汇总（8.1、8.2）。"""
    root = Path(root)
    groups: dict[str, dict] = {}
    excluded: list[dict] = []
    in_scope = [rv for rv in list_runs(root) if rv.record.experiment_id in comparison.experiments]
    for rv in in_scope:
        rec, st = rv.record, rv.status
        reason = None
        if st.state != RunState.succeeded:
            reason = f"{st.state.value}: {st.failure_reason or ''}".rstrip(": ")
        elif rec.kind in ("trial", "smoke"):
            reason = f"kind={rec.kind}"
        elif rec.plan_ref.sha256 != plan_ref.sha256 and rec.run_id not in plan.reuse_runs:
            reason = "属于旧计划版本"
        elif rec.eval_condition != eval_condition:
            reason = f"eval_condition={rec.eval_condition}"
        elif rv.label is not None and rv.label.label == "invalid":
            reason = f"人工标注为无效：{rv.label.reason}".rstrip("：")
        fields = _run_fields(root, rv)
        if reason is None and any(fields.get(k) != v for k, v in comparison.filter.items()):
            continue
        value = _run_value(root, rec.run_id, comparison.metric) if reason is None else None
        if reason is None and value is None:
            reason = "missing_metrics"
        if reason:
            excluded.append({"run_id": rec.run_id, "reason": reason})
            continue
        group = {k: fields.get(k) for k in comparison.group_by}
        g = groups.setdefault(_norm(group), {"group": group, "values": [], "run_ids": [], "suspicious": []})
        g["values"].append(float(value))
        g["run_ids"].append(rec.run_id)
        if rv.label is not None and rv.label.label == "suspicious":
            g["suspicious"].append(rec.run_id)

    rows = []
    for key in sorted(groups):
        g = groups[key]
        d = stats.describe(g["values"])
        rows.append(AggregateRow(
            group=g["group"], metric=comparison.metric, n=d["n"], mean=d["mean"], std=d["std"], sem=d["sem"],
            ci95=d["ci95"], min=d["min"], max=d["max"], values=g["values"], run_ids=g["run_ids"],
            outliers=d["outliers"], suspicious=g["suspicious"],
        ))
    contrasts = []
    for a in rows[1:]:
        b = rows[0]
        diff, t, p = stats.welch(a.values, b.values)
        contrasts.append(Contrast(a=a.group, b=b.group, metric=comparison.metric, diff_mean=diff, welch_t=t,
                                  p_value=p, n_a=a.n, n_b=b.n))
    failed = [rv for rv in in_scope if rv.status.state == RunState.failed]
    cpu = sum((rv.resources.cpu_seconds if rv.resources else 0.0) for rv in in_scope) / 3600
    gpu = sum((rv.resources.gpu_seconds if rv.resources else 0.0) for rv in in_scope) / 3600
    return Aggregate(
        aggregate_id=comparison.id, comparison=comparison.model_dump(mode="json"), plan_ref=plan_ref,
        script={"path": "airesearcher/services/runs.py", "git_blob": None,
                "package_version": airesearcher.__version__},
        eval_condition=eval_condition, rows=rows, contrasts=contrasts, excluded=excluded,
        cost={"runs_total": len(in_scope), "runs_failed": len(failed),
              "failure_rate": round(len(failed) / len(in_scope), 4) if in_scope else 0.0,
              "cpu_hours": round(cpu, 6), "gpu_hours": round(gpu, 6)},
        created_at=now(),
    )


def recompute_aggregate(root: Path, aggregate_id: str, version: int | None = None) -> Aggregate:
    """从原始 metrics.jsonl 重新计算（论文核验用），用的是存档中该汇总记录的计划版本。"""
    stored = load_aggregate(root, aggregate_id, version=version)
    if version is not None:
        # Historical checks recompute only the recorded supporting runs, never
        # discover new runs or substitute another plan's latest Aggregate.
        rows = []
        for row in stored.rows:
            values = [_run_value(root, rid, row.metric) for rid in row.run_ids]
            if not values or any(v is None for v in values):
                continue  # exact missing support is reported by evidence.check
            description = stats.describe(values)
            rows.append(row.model_copy(update={**description, "values": values}))
        contrasts = []
        for contrast in stored.contrasts:
            a = next((r for r in rows if r.group == contrast.a and r.metric == contrast.metric), None)
            b = next((r for r in rows if r.group == contrast.b and r.metric == contrast.metric), None)
            if a is not None and b is not None:
                diff, t, p = stats.welch(a.values, b.values)
                contrasts.append(contrast.model_copy(update={"diff_mean": diff, "welch_t": t, "p_value": p,
                                                            "n_a": a.n, "n_b": b.n}))
        return stored.model_copy(update={"rows": rows, "contrasts": contrasts})
    plan = parse_plan_md(_archive(Path(root)).read_text(stored.plan_ref))
    comp = Comparison.model_validate(stored.comparison)
    return compute_aggregate(root, plan, stored.plan_ref, comp, stored.eval_condition)


def aggregate_csv(agg: Aggregate) -> str:
    buf = io.StringIO()
    keys = sorted({k for r in agg.rows for k in r.group})
    w = csv.writer(buf)
    w.writerow([*keys, "metric", "n", "mean", "std", "ci95_low", "ci95_high", "min", "max"])
    for r in agg.rows:
        lo, hi = (r.ci95 or [None, None])
        w.writerow([*(r.group.get(k) for k in keys), r.metric, r.n, r.mean, r.std, lo, hi, r.min, r.max])
    return buf.getvalue()


def aggregate_markdown(agg: Aggregate) -> str:
    keys = sorted({k for r in agg.rows for k in r.group})
    lines = [f"### {agg.aggregate_id}：{agg.comparison.get('question', '')}", "",
             "| " + " | ".join([*keys, "n", "mean", "std", "95% CI"]) + " |",
             "|" + "---|" * (len(keys) + 4)]
    for r in agg.rows:
        ci = f"[{r.ci95[0]:.4f}, {r.ci95[1]:.4f}]" if r.ci95 else "—"
        std = f"{r.std:.4f}" if r.std is not None else "—"
        lines.append("| " + " | ".join([*(str(r.group.get(k)) for k in keys), str(r.n), f"{r.mean:.4f}", std, ci])
                     + " |")
    if agg.contrasts:
        lines += ["", "| 对比 | 均值差 | Welch t | p |", "|---|---|---|---|"]
        for c in agg.contrasts:
            lines.append(f"| {_norm(c.a)} vs {_norm(c.b)} | {c.diff_mean:+.4f} | {c.welch_t} | {c.p_value} |")
    suspicious = [rid for r in agg.rows for rid in r.suspicious]
    if suspicious:
        lines += ["", f"人工标注为可疑、但仍计入统计的运行：{', '.join(suspicious)}"]
    if agg.excluded:
        lines += ["", f"排除的运行：{len(agg.excluded)} 个（" +
                  "；".join(f"{e['run_id']}: {e['reason']}" for e in agg.excluded[:5]) + "）"]
    return "\n".join(lines) + "\n"
