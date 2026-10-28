"""Deterministic checks use exact manuscript and experimental versions."""

import json
import re

from airesearcher.core.fsutil import atomic_write_json
from airesearcher.services import evidence, runs


def codes(report):
    return {i.code for c in report.claims for i in c.issues} | {i.code for i in report.unbound_issues}


def test_exact_paper_ref_does_not_read_mutable_source(sample_project):
    p = sample_project
    ref = p.archive.put("paper", "paper", p.root / "paper", exclude=["build/**", "review/**"])
    before = evidence.check(p.root, paper_ref=ref)
    (p.root / "paper/claims.jsonl").write_text("", encoding="utf-8")
    (p.root / "paper/air_values.tex").write_text("corrupt", encoding="utf-8")
    (p.root / "paper/sections/results.tex").write_text("An unbound score 987.654.\\cite{invented}\n", encoding="utf-8")
    after = evidence.check(p.root, paper_ref=ref)
    assert after.claims == before.claims and after.unbound_issues == before.unbound_issues
    assert after.claims


def test_sample_project_has_no_unbound_numbers(sample_project):
    report = evidence.check(sample_project.root)
    assert not [i for i in report.unbound_issues if i.code == "NUM_UNBOUND"]


def test_macro_tampering_is_a_numeric_blocker(sample_project):
    path = sample_project.root / "paper/air_values.tex"
    path.write_text(path.read_text(encoding="utf-8").replace("{78.0}", "{12.3}"), encoding="utf-8")
    report = evidence.check(sample_project.root)
    first = next(c for c in report.claims if c.claim_id == "CL1")
    assert any(i.code == "NUM_MISMATCH" and i.severity == "blocker" for i in first.issues)


def test_wrong_plan_newer_aggregate_is_not_stale(sample_project):
    p = sample_project
    aggregate = runs.load_aggregate(p.root, "C1")
    aggregate.plan_ref = aggregate.plan_ref.model_copy(update={"version": 999, "sha256": "0" * 64})
    p.archive.put("artifacts/aggregates/C1.json", "aggregate", aggregate.model_dump_json())
    report = evidence.check(p.root)
    assert "STALE_EVIDENCE" not in codes(report)
    assert report.claims[0].recomputed


def test_unbound_scanner_excludes_identifiers_comments_and_literals(sample_project):
    p = sample_project
    text = (
        "A result 12.34.\n"
        + r"\airval{C1.score.123}\cite{devlin2019bert}\label{fig:99}\ref{fig:99}"
        + "\n"
        + "In 2024. % 54.32\n"
        + r"\verb|77.88%|"
        + "\n"
    )
    (p.root / "paper/sections/abstract.tex").write_text(text, encoding="utf-8")
    report = evidence.check(p.root)
    issues = [
        i for i in report.unbound_issues if i.code == "NUM_UNBOUND" and i.location["file"].endswith("abstract.tex")
    ]
    assert len(issues) == 1 and issues[0].severity == "major" and issues[0].location["line"] == 1


def test_failed_supporting_run_is_invalid(sample_project):
    p = sample_project
    rid = runs.load_aggregate(p.root, "C1", version=1).rows[0].run_ids[0]
    status = p.root / f"runs/{rid}/status.json"
    data = json.loads(status.read_text(encoding="utf-8"))
    data["state"] = "failed"
    atomic_write_json(status, data)
    assert "INVALID_RUN" in codes(evidence.check(p.root))


def rewrite_claims(project, edit):
    path = project.root / "paper/claims.jsonl"
    claims = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    edit(claims)
    path.write_text("".join(json.dumps(c) + "\n" for c in claims), encoding="utf-8")


def test_all_remaining_issue_codes(sample_project):
    p = sample_project

    def edit(claims):
        claims[0]["text"] = re.sub(r"\\airval\{[^}]*\.n\} seeds", "8 seeds", claims[0]["text"])
        claims[0]["evidence"][0]["selector"]["eval_condition"] = "explore"
        claims[2]["values"][0]["aggregate_version"] = 999
        claims[4]["evidence"][0]["selector"] = {"citation_scope": "experimental_detail"}
        claims[4]["evidence"].append({"type": "paper", "ref": "invented"})

    rewrite_claims(p, edit)
    report = evidence.check(p.root)
    assert {
        "N_MISMATCH",
        "CONDITION_MISMATCH",
        "STALE_EVIDENCE",
        "CITATION_ABSTRACT_ONLY",
        "CITATION_NOT_IN_ARCHIVE",
        "CONTRADICTED",
        "NUM_MISMATCH",
    } <= codes(report)
    assert report.claims[2].support == "unverifiable"
    assert report.claims[6].support == "contradicted"
    assert report.claims == evidence.check(p.root).claims


def test_non_significant_comparison_has_no_automatic_overclaim(sample_project, monkeypatch):
    p = sample_project
    original = runs.recompute_aggregate

    def overlapping(root, aid, version=None):
        agg = original(root, aid, version)
        for contrast in agg.contrasts:
            contrast.p_value = 0.5
        for row in agg.rows:
            row.ci95 = [0.0, 1.0]
        return agg

    monkeypatch.setattr(runs, "recompute_aggregate", overlapping)
    assert "OVERCLAIM_SIGNIFICANCE" not in codes(evidence.check(p.root))
    rewrite_claims(p, lambda cs: cs[1].update(text=r"\claim{CL2}{Main significantly outperforms baseline.}"))
    report = evidence.check(p.root)
    assert any(i.code == "OVERCLAIM_SIGNIFICANCE" and i.severity == "major" for i in report.claims[1].issues)
    rewrite_claims(p, lambda cs: cs[1].update(text=r"\claim{CL2}{The difference is not statistically significant.}"))
    assert "OVERCLAIM_SIGNIFICANCE" not in codes(evidence.check(p.root))


def test_dispatcher_override_missing_and_duplicate_macros(sample_project):
    p = sample_project
    path = p.root / "paper/air_values.tex"
    original = path.read_text(encoding="utf-8")
    for changed in (original + r"\renewcommand{\airval}[1]{999}", "", original + original):
        path.write_text(changed, encoding="utf-8")
        report = evidence.check(p.root)
        assert any(i.code == "NUM_MISMATCH" for i in report.claims[0].issues)


def test_invalid_kinds_labels_missing_and_suspicious_runs(sample_project):
    import shutil

    p = sample_project
    rid = runs.load_aggregate(p.root, "C1", version=1).rows[0].run_ids[0]
    path = p.root / f"runs/{rid}/run.json"
    original = json.loads(path.read_text(encoding="utf-8"))
    for kind in ("trial", "smoke"):
        atomic_write_json(path, {**original, "kind": kind})
        assert "INVALID_RUN" in codes(evidence.check(p.root))
    atomic_write_json(path, original)
    labels = p.root / "artifacts/run_labels.jsonl"
    for label, expected in (("invalid", True), ("suspicious", False)):
        labels.write_text(
            json.dumps({"run_id": rid, "label": label, "ts": "2026-01-01T00:00:00Z"}) + "\n", encoding="utf-8")
        assert ("INVALID_RUN" in codes(evidence.check(p.root))) is expected
    shutil.rmtree(path.parent)
    report = evidence.check(p.root)
    assert "INVALID_RUN" in codes(report) and report.claims[0].support == "unverifiable"


def test_trace_exact_versions_and_safe_runtime(sample_project):
    p = sample_project
    before = evidence.trace(p.root, "CL1")
    aggregate = runs.load_aggregate(p.root, "C1")
    aggregate.rows[0].mean = 999
    p.archive.put("artifacts/aggregates/C1.json", "aggregate", aggregate.model_dump_json())
    for brief in before.runs:
        path = p.root / f"runs/{brief['run_id']}/environment.json"
        atomic_write_json(
            path,
            {"python": "3.11", "api_key": "DO-NOT-LEAK", "cwd": "/private/home", "env_vars": {"TOKEN": "DO-NOT-LEAK"}},
        )
    after = evidence.trace(p.root, "CL1")
    assert after.aggregates == before.aggregates
    assert {a["version"] for a in after.aggregates} == {1}
    assert {r["run_id"] for r in after.runs} == {r["run_id"] for r in before.runs}
    assert "DO-NOT-LEAK" not in after.model_dump_json() and "/private/home" not in after.model_dump_json()
    assert after.check == evidence.check(p.root, paper_ref=p.archive.latest("paper")).claims[0]


def test_exact_compile_report_not_newer_mutable_report(sample_project):
    p = sample_project
    ref = p.archive.latest("paper")
    before = evidence.check(p.root, paper_ref=ref).compile_ok
    atomic_write_json(p.root / "paper/build/compile_report.json", {"ok": not before})
    assert evidence.check(p.root, paper_ref=ref).compile_ok == before


def test_unbound_severity_layout_and_matching_literal(sample_project):
    p = sample_project
    for section in ("abstract", "results", "discussion"):
        (p.root / f"paper/sections/{section}.tex").write_text(
            r"\section{2. Results}\setcounter{table}{3}\label{table:4}" + "\nA matching score 78.0.\n",
            encoding="utf-8",
        )
    report = evidence.check(p.root)
    for section, severity in (("abstract", "major"), ("results", "major"), ("discussion", "minor")):
        issues = [
            i
            for i in report.unbound_issues
            if i.code == "NUM_UNBOUND" and i.location["file"] == f"paper/sections/{section}.tex"
        ]
        assert len(issues) == 1 and issues[0].severity == severity and issues[0].location["line"] == 2
        assert "C1.main.score.mean" in issues[0].detail


def test_official_old_plan_and_missing_metric_invalidity(sample_project):
    p = sample_project
    row = runs.load_aggregate(p.root, "C1", version=1).rows[0]
    rid = row.run_ids[0]
    path = p.root / f"runs/{rid}/run.json"
    original = json.loads(path.read_text(encoding="utf-8"))
    changed = json.loads(path.read_text(encoding="utf-8"))
    changed["plan_ref"]["sha256"] = "0" * 64
    atomic_write_json(path, changed)
    assert "INVALID_RUN" in codes(evidence.check(p.root))
    atomic_write_json(path, original)
    (path.parent / "metrics.jsonl").write_text("", encoding="utf-8")
    report = evidence.check(p.root)
    assert "INVALID_RUN" in codes(report)
    assert report.claims[0].support == "unverifiable"
