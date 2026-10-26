"""Each Claim owns its complete exact experimental evidence closure, even without values."""

import json
import shutil

import pytest

from airesearcher.core.fsutil import atomic_write_json
from airesearcher.core.models.claim import EvidenceLink
from airesearcher.services import evidence, runs


def qualitative(p, kind="aggregate", selector=None):
    c = evidence.load_claims(p.root)[0]
    c.values = []
    c.comparison = None
    c.type = "qualitative"
    c.text = r"\claim{CL1}{The evaluated groups show distinct score distributions.}"
    c.evidence = [EvidenceLink(type=kind, ref="C1" if kind == "aggregate" else "fig_c1", version=1, selector=selector)]
    save(p, c)
    return c


def save(p, c):
    (p.root / "paper/claims.jsonl").write_text(c.model_dump_json() + "\n", encoding="utf-8")


def checked(p):
    return evidence.check(p.root).claims[0]


def archived_trace(p):
    p.archive.put("paper", "paper", p.root / "paper", exclude=evidence.PAPER_EXCLUDE)
    return evidence.trace(p.root, "CL1")


@pytest.mark.parametrize("kind", ["aggregate", "figure"])
@pytest.mark.parametrize(
    "damage", [None, "failed", "trial", "smoke", "invalid", "missing", "condition", "plan", "metric"]
)
def test_valueless_experimental_runs_checked_per_claim(sample_project, kind, damage):
    p = sample_project
    qualitative(p, kind)
    agg = runs.load_aggregate(p.root, "C1", version=1)
    rid = agg.rows[0].run_ids[0]
    directory = p.root / f"runs/{rid}"
    if damage == "failed":
        data = json.loads((directory / "status.json").read_text(encoding="utf-8"))
        atomic_write_json(directory / "status.json", {**data, "state": "failed"})
    elif damage in {"trial", "smoke", "condition", "plan"}:
        data = json.loads((directory / "run.json").read_text(encoding="utf-8"))
        if damage in {"trial", "smoke"}:
            data["kind"] = damage
        elif damage == "condition":
            data["eval_condition"] = "explore"
        else:
            data["plan_ref"]["sha256"] = "0" * 64
        atomic_write_json(directory / "run.json", data)
    elif damage == "invalid":
        (p.root / "artifacts/run_labels.jsonl").write_text(
            json.dumps({"run_id": rid, "label": "invalid", "ts": "2026-01-01T00:00:00Z"}) + "\n",
            encoding="utf-8",
        )
    elif damage == "missing":
        shutil.rmtree(directory)
    elif damage == "metric":
        (directory / "metrics.jsonl").write_text("", encoding="utf-8")
    result = checked(p)
    if damage is None:
        assert result.support == "supported" and not result.issues
    else:
        code = "CONDITION_MISMATCH" if damage == "condition" else "INVALID_RUN"
        assert code in {i.code for i in result.issues}
        assert result.support != "supported"
    traced = archived_trace(p)
    assert {a["ref"]["version"] for a in traced.aggregates} == {1}
    assert {a["ref"]["artifact_id"] for a in traced.aggregates} == {"artifacts/aggregates/C1.json"}
    assert {r["run_id"] for r in traced.runs} == {rid for row in agg.rows for rid in row.run_ids}
    assert len(traced.figures) == (kind == "figure")
    assert traced.check == result


def test_closure_deduplicates_exact_refs_rows_runs_and_issues(sample_project):
    p = sample_project
    c = evidence.load_claims(p.root)[0]
    c.evidence[1].version = 1
    save(p, c)
    agg = runs.load_aggregate(p.root, "C1", version=1)
    rid = agg.rows[0].run_ids[0]
    path = p.root / f"runs/{rid}/status.json"
    atomic_write_json(path, {**json.loads(path.read_text(encoding="utf-8")), "state": "failed"})
    report = checked(p)
    invalid = [i for i in report.issues if i.code == "INVALID_RUN"]
    assert len(invalid) == 1
    traced = archived_trace(p)
    assert len(traced.runs) == len({rid for row in agg.rows for rid in row.run_ids})
    # Distinct numeric slots retain the established per-ValueRef trace shape;
    # Aggregate/Figure links do not add duplicates of these briefs.
    assert len(traced.aggregates) == len(c.values)
    assert {a["key"] for a in traced.aggregates} == {v.key for v in c.values}
    exact = p.archive.versions("artifacts/aggregates/C1.json")[0].ref
    assert all(a["ref"] == exact.model_dump() for a in traced.aggregates)
    source = p.archive.stored_path(p.archive.latest("paper"))
    closure, _, diagnostics, _ = evidence._claim_evidence_closure(
        p.root, p.archive, evidence._bundle(source), c, p.archive.latest("paper")
    )
    assert len(closure) == 1 and closure[0]["ref"] == exact
    assert len(closure[0]["rows"]) == len(agg.rows)
    assert len([i for i in diagnostics if i[0] == "INVALID_RUN"]) == 1
    assert len(traced.figures) == 1


def test_aggregate_selector_limits_support_and_trace_uses_frozen_version(sample_project):
    p = sample_project
    agg = runs.load_aggregate(p.root, "C1", version=1)
    selected, excluded = agg.rows[:2]
    qualitative(p, selector={"group": selected.group, "metric": selected.metric, "eval_condition": agg.eval_condition})
    rid = excluded.run_ids[0]
    path = p.root / f"runs/{rid}/status.json"
    atomic_write_json(path, {**json.loads(path.read_text(encoding="utf-8")), "state": "failed"})
    assert checked(p).support == "supported"
    agg.rows = [excluded]
    p.archive.put("artifacts/aggregates/C1.json", "aggregate", agg.model_dump_json())
    traced = archived_trace(p)
    assert {r["run_id"] for r in traced.runs} == set(selected.run_ids)
    assert {a["version"] for a in traced.aggregates} == {1}


def test_literature_and_method_fact_do_not_require_runs(sample_project):
    p = sample_project
    candidates = [c for c in evidence.load_claims(p.root) if c.type in {"citation", "method_fact"}]
    assert candidates
    shutil.rmtree(p.root / "runs")
    for c in candidates:
        save(p, c)
        assert not any(i.code == "INVALID_RUN" for i in checked(p).issues)


@pytest.mark.parametrize("damage", ["live", "newer", "archive", "outside_bundle", "wrong_hash"])
def test_figure_uses_exact_archived_graph(sample_project, damage):
    p = sample_project
    qualitative(p, "figure")
    original = archived_trace(p)
    path = p.root / "artifacts/figures/fig_c1/figure.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if damage == "archive":
        stored = p.archive.stored_path(p.archive.versions("artifacts/figures/fig_c1")[0].ref)
        (stored / "figure.json").write_text("{}", encoding="utf-8")
    elif damage == "outside_bundle":
        bundle_path = p.root / "paper/evidence_bundle.json"
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        bundle["aggregates"] = []
        atomic_write_json(bundle_path, bundle)
    elif damage == "wrong_hash":
        data["aggregate_ref"]["sha256"] = "0" * 64
        atomic_write_json(path, data)
        info = p.archive.put("artifacts/figures/fig_c1", "figure", path.parent)
        c = evidence.load_claims(p.root)[0]
        c.evidence[0].version = info.version
        save(p, c)
    else:
        data["spec"]["metric"] = "invented"
        atomic_write_json(path, data)
        if damage == "newer":
            p.archive.put("artifacts/figures/fig_c1", "figure", path.parent)
    if damage in {"live", "newer"}:
        assert checked(p).support == "supported"
        assert archived_trace(p).figures == original.figures
        assert archived_trace(p).aggregates == original.aggregates
    else:
        assert checked(p).support == "unverifiable"
        assert any(i.code == "STALE_EVIDENCE" for i in checked(p).issues)


@pytest.mark.parametrize(
    "selector", [{"metric": "missing"}, {"group": {"method": "missing"}}, {"eval_condition": "explore"}]
)
def test_invalid_aggregate_selector_cannot_pass(sample_project, selector):
    qualitative(sample_project, selector=selector)
    result = checked(sample_project)
    assert result.support != "supported"
    code = "CONDITION_MISMATCH" if "eval_condition" in selector else "STALE_EVIDENCE"
    assert code in {i.code for i in result.issues}


@pytest.mark.parametrize("case", ["unsafe", "reused"])
def test_closure_run_identity_and_explicit_plan_reuse(sample_project, case):
    from airesearcher.core.models.frontmatter import render_front_matter
    from airesearcher.core.models.plan import parse_plan_md

    p = sample_project
    agg = runs.load_aggregate(p.root, "C1", version=1)
    c = qualitative(p)
    if case == "unsafe":
        agg.rows[0].run_ids[0] = "../outside"
    else:
        plan = parse_plan_md(p.archive.read_text(agg.plan_ref))
        plan.reuse_runs = [rid for row in agg.rows for rid in row.run_ids]
        agg.plan_ref = p.archive.put(
            "plan/experiment_plan.md", "plan", render_front_matter(plan.model_dump(mode="json"), "Approved reuse")
        )
    ref = p.archive.put("artifacts/aggregates/C1.json", "aggregate", agg.model_dump_json())
    c.evidence[0].version = ref.version
    save(p, c)
    path = p.root / "paper/evidence_bundle.json"
    bundle = json.loads(path.read_text(encoding="utf-8"))
    bundle["aggregates"][0]["ref"] = ref.model_dump()
    atomic_write_json(path, bundle)
    result = checked(p)
    if case == "unsafe":
        assert result.support == "unverifiable"
        assert any(i.code == "INVALID_RUN" and "../outside" in i.detail for i in result.issues)
    else:
        assert result.support == "supported" and not result.issues
