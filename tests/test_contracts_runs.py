"""The writing owner can consume canonical services and source evidence."""

from airesearcher.core.models.common import ProjectState as S
from airesearcher.services import runs
from airesearcher.testing.contracts import check_project

from .helpers import drive
from .test_experiment_stage import prepared


def test_run_aggregate_log_contracts_and_source_parents(air_home):
    project, engine, _ = prepared(air_home)
    drive(engine, S.LogPending)
    assert check_project(project.root, owners=["实验"]).ok
    agg = runs.load_aggregate(project.root, "all")
    row = agg.rows[0]
    assert runs.resolve_value(project.root, "all", row.group, row.metric, "mean") == row.mean
    ref = project.archive.latest("artifacts/aggregates/all.json")
    assert any(p.artifact_id.startswith("runs/") for p in project.archive.version_info(ref).parents)
    log = (project.root / "logs/round_1.md").read_text(encoding="utf-8")
    assert [
        line
        for line in log.splitlines()
        if line.startswith("## ") and not line.startswith("## all") and not line.startswith("## C1")
    ][:1] == ["## Observations"]
    for section in (
        "Interpretations",
        "Anomalies and disposition",
        "Hypothesis status",
        "Decision and rationale",
        "Estimated cost",
        "Affected experiments",
    ):
        assert "## " + section in log
