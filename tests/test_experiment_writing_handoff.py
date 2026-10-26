"""Real Plan/Experiment -> Writing contracts; all model/executor responses are synthetic."""

import io
import json
import shutil
import socket
import zipfile

from fastapi.testclient import TestClient

from airesearcher.core.models.claim import CheckReport, CompileReport
from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.models.figure import Figure
from airesearcher.core.project import Project
from airesearcher.engine.engine import ProjectEngine
from airesearcher.engine.stage import Error, Stop
from airesearcher.llm.gateway import LiteLLMBackend, LLMGateway
from airesearcher.runtime.local import LocalExecutor
from airesearcher.server.app import create_app
from airesearcher.services import evidence, runs
from airesearcher.stages.writing.bundle import EvidenceBundle
from airesearcher.stages.writing.figures import validate_artifact
from airesearcher.stages.writing.stage import FIGURE_PROMPT
from airesearcher.testing.contracts import check_project
from airesearcher.testing.fake_executor import FakeOutcome
from airesearcher.testing.fake_llm import FakeLLM
from scripts.plan.fixtures import fixture_idea, preset_draft
from scripts.writing.demo import SampleWritingLLM, context_of

from .helpers import approve_pending, drive
from .test_experiment_stage import prepared
from .test_writing_stage import receipt


class HandoffWritingLLM(SampleWritingLLM):
    """Use generated evidence, with no sample-project papers or C1/C2 assumptions."""

    semantic_revision = True

    def call(self, request):
        context = context_of(request)
        if request.prompt_id == "writing/section":
            section = context["section"]
            prose = "This is an offline software integration exercise using simulated observations."
            if section != "abstract":
                prose = rf"\section{{{section.replace('_', ' ').title()}}}" + "\n" + prose
            if section == "discussion":
                prose += "\n" + r"\subsection{Limitations}" + "\nNo research conclusion follows from preset values."
            if section == "results":
                for fid, path in context["figures"].items():
                    prose += (
                        "\n" + r"\begin{figure}[ht]\centering" + "\n"
                        + rf"\includegraphics[width=4in]{{{path}}}" + "\n"
                        + r"\caption{Simulated observations with confidence intervals.}"
                        + rf"\label{{fig:{fid}}}\end{{figure}}" + "\n"
                    )
            self.default = {"section": section, "latex": prose + "\n" + "\n".join(context["claim_markers"])}
            return FakeLLM.call(self, request)
        response = super().call(request)
        if request.prompt_id == FIGURE_PROMPT:
            spec = json.loads(response.text)
            cost = context["aggregate"]["cost"]
            total, rate = cost["runs_total"], cost["failure_rate"]
            spec["caption"] += f" Failure rate was {rate} across {total} runs."
            spec["caption"] += f" {total} 次运行中的失败率为 {rate}。"
            spec["ylabel"] = ""  # Human must declare the unit through production Questions.
            response.text = json.dumps(spec)
        return response


def outcome(record, config):
    if record.kind != "trial" and config["method"] == "main" and config["seed"] == 0 and not record.retry_of:
        return FakeOutcome.fail(stderr="RuntimeError: synthetic retry to test excluded provenance")
    score = {"baseline": 0.3, "main": 0.5, "ablation": 0.4}[config["method"]] + config["seed"] * 0.01
    return FakeOutcome(metrics={"score": score}, wall_seconds=0.01, stdout="SIMULATED; no training.\n")


def test_real_experiment_to_manuscript_pending(air_home, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Integration test forbids external network, paid models and local training")

    original_connect = socket.socket.connect

    def offline_connect(sock, address):
        # Windows implements asyncio's socketpair via loopback TCP. Permit that
        # internal transport for TestClient; provider calls remain forbidden.
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return original_connect(sock, address)
        return forbidden()

    monkeypatch.setattr(socket.socket, "connect", offline_connect)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(LiteLLMBackend, "call", forbidden)
    monkeypatch.setattr(LocalExecutor, "submit", forbidden)
    draft = preset_draft(fixture_idea("tasks/smoke"), "tasks/smoke")
    draft["comparisons"][0]["id"] = "MethodSummary"
    project, engine, executor = prepared(air_home, draft=draft, outcomes=outcome)
    project.config.dev.stage_impl["writing"] = "real"
    project.config.evidence_check = True
    has_tex = bool(shutil.which("latexmk") and shutil.which("kpsewhich"))
    project.config.dev.compile = has_tex
    project.save_config(project.config)
    drive(engine, S.Analyzing, timeout=90)
    drive(engine, S.LogPending)
    assert engine.ck.scratch["experiment"]["analysis_decision"]["decision"] == "stop_and_write"
    [log_approval] = project.approvals.pending()
    assert log_approval.kind == "log" and not (project.root / "paper/main.tex").exists()
    aggregate_ref = project.archive.latest("artifacts/aggregates/MethodSummary.json")
    aggregate = runs.load_aggregate(project.root, "MethodSummary", version=aggregate_ref.version)
    assert aggregate.script["path"] == "airesearcher/stages/experiment/aggregate.py"
    assert aggregate.plan_ref == engine.ck.approved["plan"] and aggregate.eval_condition == "final"
    assert aggregate.comparison["group_by"] == ["method"] and aggregate.comparison["metric"] == "score"
    assert all(row.n == 3 and row.std > 0 and row.ci95 and row.metric == "score" for row in aggregate.rows)
    records = runs.list_runs(project.root)
    assert all(view.record.executor == "fake" for view in records)
    assert aggregate.cost["runs_failed"] == 1 and aggregate.cost["runs_total"] == len(records)
    included = {rid for row in aggregate.rows for rid in row.run_ids}
    excluded = {item["run_id"] for item in aggregate.excluded}
    assert len(included) == 9 and included.isdisjoint(excluded)
    assert included | excluded == {view.record.run_id for view in records}
    parents = project.archive.version_info(aggregate_ref).parents
    assert aggregate.plan_ref in parents
    assert {f"runs/{rid}" for rid in included | excluded} <= {ref.artifact_id for ref in parents}
    approve_pending(project)
    drive(engine, S.Writing)
    ctx = engine.build_context()
    assert ctx.entry.refs == [log_approval.target]
    assert ctx.approved["plan"] == aggregate.plan_ref and ctx.feedback == []
    assert not project.approvals.pending() and not project.questions.pending()

    backend = HandoffWritingLLM()
    engine = ProjectEngine(project, llm=LLMGateway(project, backend=backend, backoff_base=0),
                           executor=executor, retry_delay=0)
    drive(engine, S.Paused)
    question = project.questions.pending()
    assert question and question.stage == "writing" and question.question_id
    assert question.allow_text and question.options == []
    original = receipt(project, "figure-fig_methodsummary")
    assert original["original_proposal"]["ylabel"] == ""
    project.questions.answer(question.question_id, None, text="score (dimensionless)", request_id=question.question_id)
    engine = ProjectEngine(Project.open(project.root), llm=engine.llm, executor=executor, retry_delay=0)
    project = engine.project
    phases = set()
    for _ in range(150):
        phases.add(engine.ck.scratch.get("writing", {}).get("phase"))
        tick = engine.tick()
        assert not isinstance(tick.result, Error | Stop), tick.result
        if engine.state == S.ManuscriptPending:
            break
    assert engine.state == S.ManuscriptPending
    assert {"claims", "write", "compile", "verify", "revise", "submit"} <= phases
    assert len([c for c in backend.calls if c.prompt_id == FIGURE_PROMPT]) == 1
    saved = receipt(project, "figure-fig_methodsummary")
    assert saved["original_proposal"] == original["original_proposal"]
    assert saved["call_id"] == original["call_id"] and saved["spec"]["ylabel"] == "score (dimensionless)"
    bundle = EvidenceBundle.model_validate_json((project.root / "paper/evidence_bundle.json").read_bytes())
    assert [source.ref for source in bundle.aggregates] == [aggregate_ref]  # excludes the auxiliary 'all' summary
    assert bundle.plan_ref == aggregate.plan_ref and [log.ref for log in bundle.logs] == [log_approval.target]
    directory = project.root / "artifacts/figures/fig_methodsummary"
    figure = Figure.model_validate_json((directory / "figure.json").read_bytes())
    assert figure.aggregate_ref == aggregate_ref and set(figure.run_ids) == included
    assert validate_artifact(directory, aggregate, aggregate_ref) == []
    claims = evidence.load_claims(project.root)
    assert claims and all(v.aggregate_id == "MethodSummary" for c in claims for v in c.values)
    [approval] = project.approvals.pending()
    assert approval.kind == "manuscript" and not project.approvals.list("log", "pending")
    reports = [CheckReport.model_validate_json(p.read_bytes())
               for p in sorted((project.root / "paper/review").glob("review_v*.json"))]
    assert len(reports) == 2 and reports[0].counts["major"] == 1
    assert reports[-1].paper_ref == approval.target and reports[-1].counts["major"] == 0
    assert all(c.support == "supported" for c in evidence.check(project.root).claims)
    assert evidence.trace(project.root, claims[0].claim_id).runs
    if has_tex:
        compiled = CompileReport.model_validate_json((project.root / "paper/build/compile_report.json").read_bytes())
        assert compiled.ok and reports[-1].compile_ok
        assert (project.root / "paper/build/main.pdf").read_bytes().startswith(b"%PDF")
    contract = check_project(project.root, owners=["实验", "论文"])
    assert contract.ok, contract.text()
    assert all(section.checked and not section.skipped for section in contract.sections)
    assert project.budget.used().get("llm_usd", 0) == 0

    # Existing production HTTP routes must serve the newly generated evidence.
    with TestClient(create_app(home=air_home, start_engines=False)) as client:
        manager = client.app.state.manager
        manager.projects[project.project_id] = project
        manager.engines[project.project_id] = engine
        base = f"/api/projects/{project.project_id}"
        assert client.get(base).json()["state"] == "ManuscriptPending"
        assert client.get(base + "/figures").json()[0]["aggregate_ref"] == aggregate_ref.model_dump()
        assert len(client.get(base + "/claims").json()["claims"]) == len(claims)
        assert client.get(base + f"/claims/{claims[0].claim_id}/trace").json()["runs"]
        assert client.get(base + f"/approvals/{approval.approval_id}").status_code == 200
        assert client.get(base + "/paper").json()["review_summary"]["counts"]["major"] == 0
        export = client.get(base + "/export", params={"what": "paper"})
        assert export.status_code == 200
        assert any(name.endswith("main.tex") for name in zipfile.ZipFile(io.BytesIO(export.content)).namelist())
        if has_tex:
            assert client.get(base + "/paper/pdf").content.startswith(b"%PDF")
    print(f"INTEGRATED_WORKSPACE={project.root}; real_compile={has_tex}")
