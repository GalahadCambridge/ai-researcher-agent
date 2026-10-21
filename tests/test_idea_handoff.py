"""Exercise the real literature-stage handoff through shared API and CLI contracts with fake models."""

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from airesearcher.cli import main as cli
from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.models.plan import parse_plan_md
from airesearcher.server.app import create_app
from airesearcher.services.literature import bibtex_for, verify_citation
from airesearcher.stages.idea.stage import IdeaStage
from airesearcher.stages.idea.storage import append_papers
from scripts.literature.demo import demo_gateway

from .helpers import drive
from .test_idea_publication import pending
from .test_idea_readers import seed


def app_for(project, air_home):
    return create_app(home=air_home, start_engines=False,
                      engine_kwargs={"llm": demo_gateway(project), "retry_delay": 0,
                                     "stages": {"idea": IdeaStage(fulltext_download=False)}})


def test_real_idea_api_revision_cli_approval_and_plan_version_handoff(air_home, monkeypatch):
    project, _, _ = pending(air_home)
    app = app_for(project, air_home)
    runner = CliRunner()
    with TestClient(app) as client:
        engine = app.state.manager.engine(project.project_id)
        prefix = f"/api/projects/{project.project_id}"
        [approval] = client.get(prefix + "/approvals", params={"status": "pending"}).json()
        detail = client.get(prefix + f"/approvals/{approval['approval_id']}").json()
        assert detail["target_content"] and detail["extra"]
        papers = client.get(prefix + "/literature").json()
        assert papers
        arxiv = next(p for p in papers if p["paper_id"].startswith("arxiv:"))
        paper = client.get(prefix + "/literature/" + quote(arxiv["paper_id"], safe="")).json()
        assert paper["reading_cards"] and all(c["based_on"] == "abstract_only" for c in paper["reading_cards"])
        for p in papers:
            assert verify_citation(project.root, p["paper_id"]).status == "verified"
        assert bibtex_for(project.root, [p["paper_id"] for p in papers]).strip()
        response = client.post(prefix + f"/approvals/{approval['approval_id']}/decision", json={
            "decision": "changes_requested", "expected_sha256": approval["target"]["sha256"],
            "request_id": "handoff-revise", "comment": "Keep the scope narrow and retain uncertainty."})
        assert response.status_code == 200
        drive(engine, S.IdeaPending)
        [current] = project.approvals.pending()
        assert current.target.version == 2
        stale = client.post(prefix + f"/approvals/{approval['approval_id']}/decision", json={
            "decision": "approved", "expected_sha256": approval["target"]["sha256"],
            "request_id": "handoff-stale"})
        assert stale.status_code == 409
        monkeypatch.setattr(cli, "_client", client)
        result = runner.invoke(cli.app, ["approve", current.approval_id, "--project", project.project_id])
        assert result.exit_code == 0, result.output
        drive(engine, S.PlanPending)
        plan = parse_plan_md((project.root / "plan/experiment_plan.md").read_text(encoding="utf-8"))
        assert plan.idea_ref == current.target and engine.ck.approved["idea"] == current.target
        assert not list((project.root / "runs").glob("*/run.json"))


@pytest.mark.parametrize("paper_id", ["s2:test0", "doi:10.5555/literature-fixture"])
def test_shared_api_exposes_reading_cards_for_non_arxiv_ids(air_home, paper_id):
    project, engine, papers = seed(air_home)
    original = papers[0]
    if paper_id != original.paper_id:
        original.selected_for_reading = False
        target = original.model_copy(deep=True, update={"paper_id": paper_id, "selected_for_reading": True,
                                                      "ids": {"doi": "10.5555/literature-fixture"}})
        append_papers(engine.build_context(), [original, target])
    drive(engine, S.Paused)
    with TestClient(app_for(project, air_home)) as client:
        response = client.get(f"/api/projects/{project.project_id}/literature/" + quote(paper_id, safe=""))
        assert response.status_code == 200
        assert len(response.json()["reading_cards"]) == 2
        assert all(card["paper_id"] == paper_id for card in response.json()["reading_cards"])
        missing = client.get(f"/api/projects/{project.project_id}/literature/doi:10.5555/not-registered")
        assert missing.status_code == 404
