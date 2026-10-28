"""Real control-arm Stage: gateway, durable recovery and engine approval lifecycle."""

import ast
import json
import shutil
from pathlib import Path

import pytest
import yaml

from airesearcher.core.fsutil import atomic_write_json, sha256_file
from airesearcher.core.models.claim import CompileReport
from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.models.frontmatter import render_front_matter
from airesearcher.core.models.plan import parse_plan_md
from airesearcher.core.project import Project
from airesearcher.engine.engine import ProjectEngine
from airesearcher.engine.stage import Continue, Error, Stop
from airesearcher.llm.gateway import LLMGateway
from airesearcher.services import runs
from airesearcher.stages import load_stage
from airesearcher.stages.writing import stage as module
from airesearcher.stages.writing.bundle import EvidenceBundle, build_bundle, write_bundle
from airesearcher.stages.writing.figures import default_spec
from airesearcher.stages.writing.writer import GENERATION_ORDER, PROMPT_ID, SECTIONS, Writer
from airesearcher.testing.fake_llm import fake_gateway
from scripts.writing.demo import SampleWritingLLM, context_of, prepare_sample, run

from .helpers import approve_pending, drive


@pytest.fixture
def writing(tmp_path):
    return prepare_sample(tmp_path / "writing", compile_enabled=False)


def to_phase(engine, phase):
    for _ in range(100):
        if engine.ck.scratch.get("writing", {}).get("phase") == phase:
            return
        tick = engine.tick()
        assert isinstance(tick.result, Continue), tick.result
    pytest.fail(f"did not reach {phase}")


def restart(project, engine):
    # Reload the real persisted engine checkpoint, with a fresh Stage object.
    reopened = Project.open(project.root)
    return ProjectEngine(reopened, llm=engine.llm, retry_delay=0)


def receipt(project, name):
    return json.loads((project.root / f"paper/.writing/{name}.json").read_text(encoding="utf-8"))


def set_template(project, files, version=1):
    directory = project.root / f"templates/tpl-01/v{version}/original"
    directory.mkdir(parents=True)
    for name, text in files.items():
        (directory / name).write_text(text, encoding="utf-8")
    project.config.template = {"id": "tpl-01", "version": version}
    project.save_config(project.config)
    return directory


TEMPLATE = (
    r"\documentclass{article}\usepackage{graphicx}" + "\n" + r"\begin{document}" + "\n%% AIR:BODY\n" + r"\end{document}"
)


def writing_calls(engine):
    return [c for c in engine.llm.backend.calls if c.prompt_id == PROMPT_ID]


def test_real_stage_registry(project):
    project.config.dev.stage_impl["writing"] = "real"
    stage = load_stage("writing", project.config)
    assert stage.name == "writing"
    assert type(stage).__name__ == "WritingStage"


def test_true_arm_requires_approved_inputs_before_output(project):
    project.config.dev.stage_impl["writing"] = "real"
    stage = load_stage("writing", project.config)
    llm = fake_gateway(project)
    ctx = ProjectEngine(project, llm=llm).build_context()
    result = stage.step(ctx)
    assert isinstance(result, Error) and not result.retryable
    assert "approved idea" in result.message
    assert not llm.backend.calls
    assert not (project.root / "paper/evidence_bundle.json").exists()


def test_control_arm_engine_artifacts_and_exact_sources(writing):
    p, e = writing
    approval = run(e)
    assert e.state == S.ManuscriptPending and approval.target.artifact_id == "paper"
    s = e.ck.scratch["writing"]
    bundle = EvidenceBundle.model_validate_json((p.root / "paper/evidence_bundle.json").read_bytes())
    assert bundle.schema_version == 2
    assert bundle.idea_ref == e.ck.approved["idea"] and bundle.plan_ref == e.ck.approved["plan"]
    assert [a.aggregate_id for a in bundle.aggregates] == ["C1", "C2"]
    assert s["bundle_ref"] == p.archive.latest("paper/evidence_bundle.json").model_dump()
    assert len(writing_calls(e)) == 8 and len(e.llm.backend.calls) == 10
    assert [context_of(c)["section"] for c in writing_calls(e)] == list(GENERATION_ORDER)
    for call in writing_calls(e):
        assert call.schema_name == "SectionOutput" and call.role == "writing.writer" and call.prompt_version == 3
        assert context_of(call)["figures"] == {fid: f"figures/{fid}.pdf" for fid in ("fig_c1", "fig_c2")}
    for a in bundle.aggregates:
        fid = f"fig_{a.aggregate_id.lower()}"
        figure = p.archive.latest_version(f"artifacts/figures/{fid}")
        assert figure.parents == [a.ref]
        assert (p.root / f"paper/figures/{fid}.pdf").read_bytes() == (
            p.root / f"artifacts/figures/{fid}/{fid}.pdf"
        ).read_bytes()
    assert "disabled" in approval.summary and "control arm" in approval.summary
    assert "supported" not in approval.summary and "blocker" not in approval.summary
    assert {r.artifact_id for r in approval.extra_refs} == {"paper/build/compile_report.json"}
    paper = p.root / "paper"
    for relative in ("main.tex", "references.bib", "ai_use.tex", "build/compile_report.json"):
        assert (paper / relative).exists()
    assert "hu2022lora" in (paper / "references.bib").read_text(encoding="utf-8")
    assert r"\input{ai_use}" in (paper / "main.tex").read_text(encoding="utf-8")
    assert "evidence\\_check=false" in (paper / "ai_use.tex").read_text(encoding="utf-8")
    assert not (paper / "build/main.pdf").exists()
    assert not (paper / "claims.jsonl").exists() and not (paper / "air_values.tex").exists()
    assert not list((paper / "review").glob("review_v*.json"))
    stored = p.archive.stored_path(approval.target)
    assert not (stored / ".writing").exists() and not (stored / "build").exists()
    assert not list(stored.glob(".template*"))
    parents = p.archive.version_info(approval.target).parents
    assert p.archive.latest("paper/evidence_bundle.json") in parents
    assert all(a.ref in parents for a in bundle.aggregates)


@pytest.mark.parametrize(
    "phase,section_count", [("figures", 0), ("template", 0), ("write", 3), ("write", 8), ("compile", 8), ("submit", 8)]
)
def test_restart_boundaries_reuse_completed_calls(writing, phase, section_count):
    p, e = writing
    to_phase(e, phase)
    if phase == "write":
        for _ in range(section_count):
            e.tick()
    before = len(e.llm.backend.calls)
    resumed = restart(p, e)
    run(resumed)
    assert len(resumed.llm.backend.calls) == 10
    assert len(resumed.llm.backend.calls) >= before
    assert len(writing_calls(resumed)) == 8
    bundle_versions = p.archive.versions("paper/evidence_bundle.json")
    assert len(bundle_versions) == 1  # all restarts retain the one registered Stage bundle


def test_crash_after_section_receipt_before_install_does_not_call_again(writing, monkeypatch):
    p, e = writing
    to_phase(e, "write")
    original = Writer.write_section
    monkeypatch.setattr(Writer, "write_section", lambda *args: (_ for _ in ()).throw(OSError("simulated crash")))
    tick = e.tick()
    assert isinstance(tick.result, Error)
    assert receipt(p, "section-introduction")["installed"] is False
    calls = len(writing_calls(e))
    monkeypatch.setattr(Writer, "write_section", original)
    e.request_action("resume")
    resumed = restart(p, e)
    run(resumed)
    assert calls == 1 and len(writing_calls(resumed)) == 8


def test_restart_after_first_figure_reuses_scripted_artifact(writing, monkeypatch):
    p, e = writing
    to_phase(e, "figures")
    e.tick()
    before = sha256_file(p.root / "artifacts/figures/fig_c1/figure.json")
    original = module.generate_figure
    generated = []

    def observe(*args, **kwargs):
        generated.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "generate_figure", observe)
    resumed = restart(p, e)
    run(resumed)
    assert generated == ["C2"]
    assert sha256_file(p.root / "artifacts/figures/fig_c1/figure.json") == before


def test_current_plan_selection_ignores_new_wrong_plan_version(writing):
    p, e = writing
    plan_ref = e.ck.approved["plan"]
    plan = parse_plan_md(p.archive.read_text(plan_ref))
    plan.plan_id += "-historical"
    other = p.archive.put(plan_ref.artifact_id, "plan", render_front_matter(plan.model_dump(mode="json"), "Other"))
    aggregate = runs.load_aggregate(p.root, "C1")
    original = p.archive.latest("artifacts/aggregates/C1.json")
    aggregate.plan_ref = other
    p.archive.put(original.artifact_id, "aggregate", aggregate.model_dump_json())
    e.tick()
    selected = e.ck.scratch["writing"]["aggregates"]
    assert selected[0] == original.model_dump()
    bundle = EvidenceBundle.model_validate_json((p.root / "paper/evidence_bundle.json").read_bytes())
    assert bundle.plan_ref == plan_ref and not bundle.diagnostics


def test_missing_current_comparison_fails_without_fabricating(writing):
    p, e = writing
    plan_ref = e.ck.approved["plan"]
    plan = parse_plan_md(p.archive.read_text(plan_ref))
    plan.comparisons[0].id = "Missing"
    changed = p.archive.put(plan_ref.artifact_id, "plan", render_front_matter(plan.model_dump(mode="json"), "Changed"))
    aid = p.approvals.request(changed, "plan", "test", "test")
    p.approvals.decide(aid, "approved", changed.sha256)
    e.ck.approved["plan"] = changed
    result = e.tick().result
    assert isinstance(result, Error) and "required comparison" in result.message
    assert not e.llm.backend.calls


def test_runtime_context_uses_service_filters_secrets_and_freezes(writing, monkeypatch):
    p, e = writing
    original = runs.load_run
    called = []

    def observe(root, rid, **kwargs):
        assert kwargs["with_metrics"] is False
        called.append(rid)
        view = original(root, rid, **kwargs)
        view.environment.update(workdir="/private/machine", env_vars={"API_KEY": "never-prompt", "PYTHONHASHSEED": "0"})
        view.environment["hardware"]["credential"] = "also-never"
        view.environment["hardware"]["device_path"] = "/private/gpu"
        return view

    monkeypatch.setattr(runs, "load_run", observe)
    to_phase(e, "compile")
    bundle = EvidenceBundle.model_validate_json((p.root / "paper/evidence_bundle.json").read_bytes())
    ids = {rid for a in bundle.aggregates for row in a.data.rows for rid in row.run_ids}
    assert set(called) == ids == {r["run_id"] for r in bundle.runtime.runs}
    encoded = bundle.to_bytes().decode()
    assert "never-prompt" not in encoded and "also-never" not in encoded and "/private/" not in encoded
    assert "<external-path>" in encoded
    for call in writing_calls(e):
        context = context_of(call)
        if context["section"] in {"method", "setup"}:
            assert context["runtime"] == bundle.runtime.model_dump(mode="json")
            assert context["runtime"]["task"]["name"] == "smoke"
    before = bundle.to_bytes()
    # Later live data never leaks into an already constructed Writer context.
    writer = Writer(p.root, bundle, llm=e.llm)
    monkeypatch.setattr(runs, "load_run", lambda *a, **kw: pytest.fail("Writer must use the frozen input"))
    assert writer.section_context("setup")["runtime"]["sha256"] == bundle.runtime.sha256
    assert bundle.to_bytes() == before


def test_bundle_v1_bytes_remain_v1_and_v2_is_explicit(writing):
    p, e = writing
    old = build_bundle(p.root)
    assert old.schema_version == 1 and "runtime" not in json.loads(old.to_bytes())
    assert EvidenceBundle.model_validate_json(old.to_bytes()).to_bytes() == old.to_bytes()
    with pytest.raises(ValueError, match="schema v2"):
        EvidenceBundle.model_validate({**old.model_dump(), "schema_version": 2})
    e.tick()
    bundle = EvidenceBundle.model_validate_json((p.root / "paper/evidence_bundle.json").read_bytes())
    assert bundle.schema_version == 2
    assert write_bundle(p.root, bundle) == p.archive.latest("paper/evidence_bundle.json")


def test_stage_does_not_bypass_bundle_parent_closure_on_identical_bytes(writing):
    p, e = writing
    bundle = build_bundle(
        p.root,
        approved=e.ck.approved,
        include_runtime=True,
        aggregate_refs=[p.archive.latest(f"artifacts/aggregates/{cid}.json") for cid in ("C1", "C2")],
    )
    p.archive.put("paper/evidence_bundle.json", "other", bundle.to_bytes(), parents=[])
    result = e.tick().result
    assert isinstance(result, Error) and "lineage migration" in result.message
    assert not e.llm.backend.calls


def test_changed_run_environment_invalidates_frozen_basis(writing):
    p, e = writing
    run(e)
    old = p.archive.latest("paper")
    bundle = EvidenceBundle.model_validate_json((p.root / "paper/evidence_bundle.json").read_bytes())
    envpath = p.root / "runs" / bundle.runtime.runs[0]["run_id"] / "environment.json"
    data = json.loads(envpath.read_text(encoding="utf-8"))
    data["python"] = "3.99.test"
    atomic_write_json(envpath, data)
    e.ck.state = S.Writing
    e.ck.pending_approval = None
    e.ck.stale = ["paper"]
    run(e)
    assert len(writing_calls(e)) == 16
    assert p.archive.latest("paper").version > old.version
    latest = EvidenceBundle.model_validate_json((p.root / "paper/evidence_bundle.json").read_bytes())
    assert latest.runtime.sha256 != bundle.runtime.sha256


@pytest.mark.parametrize("decision", ["changes_requested", "rejected"])
def test_real_approval_feedback_consumed_once_and_new_version(writing, decision):
    p, e = writing
    old = run(e).target
    old_bytes = p.archive.get(old)
    approve_pending(p, decision, "Use a concise explanation; do not invent additional observations.")
    if decision == "rejected":
        drive(e, S.Paused)
        q = p.questions.pending()
        p.questions.answer(q.question_id, "redo", request_id="redo")
    drive(e, S.ManuscriptPending, timeout=90)
    assert p.archive.latest("paper").version > old.version and p.archive.get(old) == old_bytes
    assert len(writing_calls(e)) == 16
    context = context_of(writing_calls(e)[-1])
    assert len(context["user_writing_guidance"]) == 1
    assert "do not invent" in context["user_writing_guidance"][0]["comment"]
    resumed = restart(p, e)
    resumed.tick()
    assert len(writing_calls(e)) == 16
    assert len(receipt(p, "runtime")["guidance"]) == 1


def test_human_section_edit_preserved_and_reference_service_used(writing, monkeypatch):
    p, e = writing
    old = run(e).target
    path = p.root / "paper/sections/discussion.tex"
    path.write_text(
        path.read_text(encoding="utf-8") + "\nHuman clarification.\\cite{zhang2021revisiting}\n", encoding="utf-8")
    from airesearcher.services import literature

    original = literature.bibtex_for
    called = []

    def observe(*args, **kwargs):
        called.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(literature, "bibtex_for", observe)
    drive(e, S.ManuscriptPending, timeout=90)
    assert "Human clarification" in path.read_text(encoding="utf-8")
    assert len(writing_calls(e)) == 8 and called
    assert "zhang2021revisiting" in (p.root / "paper/references.bib").read_text(encoding="utf-8")
    assert p.archive.latest("paper").version > old.version
    assert receipt(p, "section-discussion")["human_edited"]
    assert "Human-edited sections" in (p.root / "paper/ai_use.tex").read_text(encoding="utf-8")


def test_compile_only_rerun_does_not_version_source(writing):
    p, e = writing
    first = run(e).target
    meta = receipt(p, "runtime")
    meta.pop("compiled")
    atomic_write_json(p.root / "paper/.writing/runtime.json", meta)
    e.ck.state = S.Writing
    e.ck.pending_approval = None
    e.ck.scratch["writing"]["phase"] = "compile"
    assert run(e).target == first
    assert len(writing_calls(e)) == 8


def test_multi_entry_question_answer_survives_restart(writing):
    p, e = writing
    original = set_template(p, {"a.tex": TEMPLATE, "b.tex": TEMPLATE + "\n% B"})
    before = {f.name: f.read_bytes() for f in original.iterdir()}
    drive(e, S.Paused)
    q = p.questions.pending()
    assert [o.id for o in q.options] == ["a.tex", "b.tex"] and not any(o.default for o in q.options)
    p.questions.answer(q.question_id, "b.tex", request_id="choose-b")
    resumed = restart(p, e)
    run(resumed)
    assert resumed.ck.scratch["writing"]["entry"] == "b.tex"
    assert len([r for r in p.questions.records() if r.question.stage == "writing"]) == 1
    assert before == {f.name: f.read_bytes() for f in original.iterdir()}


@pytest.mark.parametrize("choice", ["use_default", "uploaded_fix", "end"])
def test_broken_template_explicit_choices(writing, choice):
    p, e = writing
    set_template(p, {"broken.tex": TEMPLATE.replace(r"\end{document}", "")})
    drive(e, S.Paused)
    q = p.questions.pending()
    assert [o.id for o in q.options] == ["uploaded_fix", "use_default", "end"]
    assert not writing_calls(e)
    if choice == "uploaded_fix":
        set_template(p, {"fixed.tex": TEMPLATE}, version=2)
    p.questions.answer(q.question_id, choice, request_id=choice)
    resumed = restart(p, e)
    if choice == "end":
        drive(resumed, S.Failed)
        assert not writing_calls(resumed)
    else:
        run(resumed)
        assert len([r for r in p.questions.records() if r.question.stage == "writing"]) == 1
        if choice == "use_default":
            assert receipt(p, "runtime")["fallback_from"] == {"id": "tpl-01", "version": 1}
            assert "explicitly selected" in (p.root / "paper/ai_use.tex").read_text(encoding="utf-8")
        else:
            assert resumed.ck.scratch["writing"]["template"]["version"] == 2


def test_template_only_change_reassembles_without_prose_calls(writing):
    p, e = writing
    run(e)
    old_sections = {name: (p.root / f"paper/sections/{name}.tex").read_bytes() for name in SECTIONS}
    set_template(p, {"custom.tex": TEMPLATE.replace(r"\documentclass{article}", r"\documentclass[11pt]{article}")})
    e.ck.state = S.Writing
    e.ck.pending_approval = None
    run(e)
    assert len(writing_calls(e)) == 8
    assert "[11pt]" in (p.root / "paper/main.tex").read_text(encoding="utf-8")
    assert old_sections == {name: (p.root / f"paper/sections/{name}.tex").read_bytes() for name in SECTIONS}


def test_no_direct_cross_stage_import_or_private_run_reads():
    folder = Path(module.__file__).parent
    for path in folder.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("airesearcher.stages.idea")
        if path.name != "bundle.py":
            assert '"environment.json"' not in path.read_text(encoding="utf-8")
        assert '"run.json"' not in path.read_text(encoding="utf-8")
        assert '"config.yaml"' not in path.read_text(encoding="utf-8")


def test_figure_planner_rejects_extra_values_and_code(writing):
    p, e = writing
    to_phase(e, "figures")
    agg = runs.load_aggregate(p.root, "C1")
    spec = default_spec(agg, ylabel="Score (dimensionless)").model_dump()
    e.llm.backend.script[module.FIGURE_PROMPT] = [{**spec, "values": [999], "code": "malicious"}]
    result = e.tick().result
    assert isinstance(result, module.Continue)
    assert isinstance(e.tick().result, module.Continue)
    assert isinstance(e.tick().result, module.Stop)
    journals = list((p.root / "paper/.writing").glob("figure-fig_c1-*-attempts.json"))
    assert len(journals) == 1
    attempts = json.loads(journals[0].read_bytes())["attempts"]
    assert len(attempts) == 2 and all(a["status"] == "rejected" and a["reason"] for a in attempts)
    assert all(json.loads(a["raw"])["values"] == [999] and json.loads(a["raw"])["code"] == "malicious"
               for a in attempts)
    assert len([c for c in e.llm.backend.calls if c.prompt_id == module.FIGURE_PROMPT]) == 2
    assert {option.id for option in p.questions.pending().options} == {"retry", "end"}
    assert not (p.root / "artifacts/figures/fig_c1").exists()


def test_unknown_units_are_not_invented_by_deterministic_stage(writing):
    p, e = writing
    to_phase(e, "figures")
    spec = default_spec(runs.load_aggregate(p.root, "C1"), ylabel="Score (dimensionless)").model_dump()
    spec["ylabel"] = ""
    e.llm.backend.script[module.FIGURE_PROMPT] = [spec]
    result = e.tick().result
    assert isinstance(result, module.Stop)
    assert p.questions.pending().allow_text
    assert not (p.root / "artifacts/figures/fig_c1").exists()
    assert receipt(p, "figure-fig_c1")["original_proposal"] == spec


def test_explicit_task_units_avoid_figure_planner(writing):
    p, e = writing
    task = Path(module.__file__).resolve().parents[3] / "tasks/smoke/task.yaml"
    data = yaml.safe_load(task.read_text(encoding="utf-8"))
    data["metrics"][0]["unit"] = "dimensionless"
    target = p.root / "tasks/smoke/task.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(yaml.safe_dump(data), encoding="utf-8")
    run(e)
    assert len(e.llm.backend.calls) == 8


def test_compile_disabled_has_truthful_frozen_report(writing):
    p, e = writing
    run(e)
    report = CompileReport.model_validate_json((p.root / "paper/build/compile_report.json").read_bytes())
    assert not report.ok and report.engine == "disabled" and report.pages is None
    assert not (p.root / "paper/build/main.pdf").exists()


def test_no_tex_development_mode_uses_no_placeholder(writing, monkeypatch):
    p, e = writing
    original = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda name: None if name in {"latexmk", "kpsewhich", "pdflatex"} else original(name)
    )
    run(e)
    assert e.state == S.ManuscriptPending
    report = CompileReport.model_validate_json((p.root / "paper/build/compile_report.json").read_bytes())
    assert report.engine == "disabled" and not report.ok and not (p.root / "paper/build/main.pdf").exists()


@pytest.mark.skipif(not shutil.which("kpsewhich"), reason="real TeX dependency lookup unavailable")
def test_missing_template_dependency_asks_before_prose(writing):
    p, e = writing
    set_template(p, {"main.tex": TEMPLATE.replace(r"\usepackage{graphicx}", r"\usepackage{nonexistentairpackage}")})
    drive(e, S.Paused)
    assert "nonexistentairpackage" in p.questions.pending().text
    assert not writing_calls(e)


def simulated_compile(writing, monkeypatch, errors=None):
    """Only repair-control tests use simulated reports; no simulated PDF is produced."""
    p, e = writing
    to_phase(e, "compile")
    failed = CompileReport(
        ok=False,
        engine="test-simulated",
        entry="main.tex",
        seconds=0,
        errors=errors or [{"file": "sections/method.tex", "line": 2, "message": "Extra alignment tab"}],
        log_path="paper/build/main.log",
    )
    monkeypatch.setattr(module, "compile_paper", lambda *a, **kw: failed)
    e.tick()
    assert e.ck.scratch["writing"]["phase"] == "compile_fix"
    return p, e


def test_compile_repair_validated_and_restart_does_not_repeat(writing, monkeypatch):
    p, e = simulated_compile(writing, monkeypatch)
    e.llm.backend.script[module.REPAIR_PROMPT] = [{"section": "method", "latex": r"\section{Method} Repaired syntax."}]
    e.tick()
    assert receipt(p, "runtime")["repairs"][-1]["status"] == "accepted"
    before = len(e.llm.backend.calls)
    resumed = restart(p, e)
    resumed.tick()  # install the durable response, no call
    assert len(e.llm.backend.calls) == before
    assert "Repaired syntax" in (p.root / "paper/sections/method.tex").read_text(encoding="utf-8")
    assert resumed.ck.scratch["writing"]["phase"] == "compile"
    request = e.llm.backend.calls[-1]
    assert request.role == "writing.compile_repair" and request.prompt_version == 1
    assert "approved_logs" not in context_of(request)


@pytest.mark.parametrize(
    "bad",
    [
        {"section": "results", "latex": "wrong section"},
        {"section": "method", "latex": r"\input{/etc/passwd}"},
        {"section": "method", "latex": r"\cite{invented}"},
        {"section": "method", "latex": "changed", "path": "../../template.tex"},
    ],
)
def test_invalid_repair_preserves_prior_section(writing, monkeypatch, bad):
    p, e = simulated_compile(writing, monkeypatch)
    path = p.root / "paper/sections/method.tex"
    original = path.read_bytes()
    e.llm.backend.script[module.REPAIR_PROMPT] = [bad]
    e.tick()
    assert path.read_bytes() == original
    assert receipt(p, "runtime")["repairs"][-1]["status"] == "rejected"


@pytest.mark.parametrize("choice", ["retry", "end"])
def test_exhausted_repairs_question_and_manual_retry_without_model(writing, monkeypatch, choice):
    p, e = simulated_compile(writing, monkeypatch)
    e.llm.backend.script[module.REPAIR_PROMPT] = [{"section": "method", "latex": r"\cite{invented}"}]
    drive(e, S.Paused)
    assert sum(c.prompt_id == module.REPAIR_PROMPT for c in e.llm.backend.calls) == 2
    q = p.questions.pending()
    assert [o.id for o in q.options] == ["retry", "end"] and "compile_report.json" in q.text
    calls = len(e.llm.backend.calls)
    p.questions.answer(q.question_id, choice, request_id=choice)
    if choice == "end":
        drive(e, S.Failed)
    else:
        path = p.root / "paper/sections/method.tex"
        path.write_text(path.read_text(encoding="utf-8") + "\nHuman fix.\n", encoding="utf-8")
        # Actual disabled compile implementation: proves no placeholder and no extra repair call.
        from airesearcher.stages.writing.compile import compile_paper

        monkeypatch.setattr(module, "compile_paper", compile_paper)
        drive(e, S.ManuscriptPending, timeout=90)
        assert "Human fix" in path.read_text(encoding="utf-8")
    assert len(e.llm.backend.calls) == calls


def test_template_error_cannot_target_arbitrary_section(writing, monkeypatch):
    p, e = simulated_compile(writing, monkeypatch, errors=[{"file": "main.tex", "line": 1, "message": "bad template"}])
    drive(e, S.Paused)
    assert not any(c.prompt_id == module.REPAIR_PROMPT for c in e.llm.backend.calls)


def test_zero_compile_fix_budget_makes_no_repair_call(writing, monkeypatch):
    p, e = simulated_compile(writing, monkeypatch)
    p.config.paper["max_compile_fixes"] = 0
    p.save_config(p.config)
    drive(e, S.Paused)
    assert not any(c.prompt_id == module.REPAIR_PROMPT for c in e.llm.backend.calls)


def test_exact_approved_bytes_win_over_unapproved_working_files(writing):
    p, e = writing
    for kind in ("idea", "plan"):
        (p.root / e.ck.approved[kind].artifact_id).write_text("UNAPPROVED CURRENT FILE", encoding="utf-8")
    to_phase(e, "compile")
    assert all("UNAPPROVED CURRENT FILE" not in json.dumps(context_of(c)) for c in writing_calls(e))


def test_exploratory_and_negative_results_remain_visible(writing):
    p, e = writing
    aggregate = runs.load_aggregate(p.root, "C1")
    aggregate.eval_condition = "explore"
    aggregate.contrasts[0].diff_mean = -0.1
    aggregate.contrasts[0].p_value = 0.8
    p.archive.put("artifacts/aggregates/C1.json", "aggregate", aggregate.model_dump_json())
    to_phase(e, "compile")
    context = next(context_of(c) for c in writing_calls(e) if context_of(c)["section"] == "results")
    data = context["aggregates"][0]["data"]
    assert data["eval_condition"] == "explore" and data["contrasts"][0]["diff_mean"] == -0.1
    assert data["contrasts"][0]["p_value"] == 0.8
    spec = json.loads((p.root / "artifacts/figures/fig_c1/spec.json").read_text(encoding="utf-8"))
    assert "Exploratory" in spec["title"] and "Exploratory" in spec["caption"]


def test_task_snapshot_mutation_rebuilds_bundle(writing):
    p, e = writing
    to_phase(e, "figures")
    first = p.archive.latest("paper/evidence_bundle.json")
    task = Path(module.__file__).resolve().parents[3] / "tasks/smoke/task.yaml"
    data = yaml.safe_load(task.read_text(encoding="utf-8"))
    data["resources"]["gpu"] = "different recorded hardware constraint"
    target = p.root / "tasks/smoke/task.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(yaml.safe_dump(data), encoding="utf-8")
    e.tick()
    assert p.archive.latest("paper/evidence_bundle.json").version == first.version + 1


def test_stale_after_actual_manuscript_approval_and_reopen(writing):
    p, e = writing
    old = run(e).target
    approve_pending(p)
    drive(e, S.Completed)
    aggregate = runs.load_aggregate(p.root, "C1")
    aggregate.excluded.append({"run_id": "documented-exclusion", "reason": "new recorded exclusion note"})
    p.archive.put("artifacts/aggregates/C1.json", "aggregate", aggregate.model_dump_json())
    e.request_action("reopen", request_id="reopen-evidence")
    new = run(e).target
    assert new.version > old.version and e.ck.approved["manuscript"] == old
    assert len(writing_calls(e)) == 16
    assert p.archive.stored_path(old).exists()


def test_changed_prompt_fingerprint_regenerates_completed_sections(writing):
    p, e = writing
    to_phase(e, "write")
    e.tick()
    first = receipt(p, "section-introduction")["fingerprint"]
    p.config.prompt_pins[PROMPT_ID] = 1
    p.save_config(p.config)
    run(e)
    assert len(writing_calls(e)) == 9
    assert receipt(p, "section-introduction")["fingerprint"] != first
    assert receipt(p, "section-introduction")["prompt_version"] == 1


def test_writer_pre_call_fingerprint_matches_generated_receipt(writing):
    p, e = writing
    e.tick()
    bundle = EvidenceBundle.model_validate_json((p.root / "paper/evidence_bundle.json").read_bytes())
    writer = Writer(p.root, bundle, llm=e.llm)
    before = writer.section_fingerprint("method")
    generated = writer.generate_section("method")
    assert generated.context_sha256 == before
    guided = Writer(p.root, bundle, llm=e.llm, guidance=[{"comment": "Concise style"}])
    assert guided.section_fingerprint("method") != before


def test_minimal_compile_failure_is_a_template_question(writing, monkeypatch):
    p, e = writing
    p.config.dev.compile = True
    p.save_config(p.config)

    def failed(source, **kwargs):
        check = module.inspect_template(source.original, entry=kwargs.get("entry"))
        check.minimal_compile_ok = False
        return check

    monkeypatch.setattr(module, "check_template", failed)
    drive(e, S.Paused)
    assert [o.id for o in p.questions.pending().options] == ["uploaded_fix", "use_default", "end"]
    assert not writing_calls(e)


def test_unknown_existing_section_is_not_overwritten(writing):
    p, e = writing
    to_phase(e, "write")
    path = p.root / "paper/sections/introduction.tex"
    path.parent.mkdir(parents=True)
    path.write_text("Human-authored fragment without a model receipt.", encoding="utf-8")
    result = e.tick().result
    assert isinstance(result, Stop) and "unreceipted" in result.reason
    assert {o.id for o in result.question.options} == {"restore", "end"}
    assert path.read_text(encoding="utf-8") == "Human-authored fragment without a model receipt."
    assert not writing_calls(e)


def test_human_edit_during_compile_failure_is_recompiled_first(writing, monkeypatch):
    p, e = simulated_compile(writing, monkeypatch)
    path = p.root / "paper/sections/method.tex"
    path.write_text(path.read_text(encoding="utf-8") + "\nHuman fix before automatic repair.\n", encoding="utf-8")
    result = e.tick().result
    assert isinstance(result, Continue) and e.ck.scratch["writing"]["phase"] == "write"
    assert not any(c.prompt_id == module.REPAIR_PROMPT for c in e.llm.backend.calls)


def test_interrupted_repair_consumes_budget_and_does_not_repeat_completed_attempt(writing, monkeypatch):
    p, e = simulated_compile(writing, monkeypatch)
    e.llm.backend.script[module.REPAIR_PROMPT] = [{"section": "method", "latex": "Safe fragment."}]
    meta = receipt(p, "runtime")
    meta["repairs"] = [
        {
            "cycle": module.WritingStage()._cycle(e.ck.scratch["writing"]),
            "section": "method",
            "status": "started",
            "number": 1,
        }
    ]
    atomic_write_json(p.root / "paper/.writing/runtime.json", meta)
    resumed = restart(p, e)
    resumed.tick()
    assert len(receipt(p, "runtime")["repairs"]) == 2
    assert sum(c.prompt_id == module.REPAIR_PROMPT for c in e.llm.backend.calls) == 1


@pytest.mark.skipif(not shutil.which("latexmk"), reason="real latexmk unavailable")
def test_comment_only_edit_pdf_and_report_parent_current_source(tmp_path):
    p, e = prepare_sample(tmp_path / "comment-edit", compile_enabled=True)
    old = run(e)
    old_pdf = next(r for r in old.extra_refs if r.artifact_id.endswith(".pdf"))
    # Mark the first human edit, then change only comments on the next revision.
    path = p.root / "paper/sections/method.tex"
    for n in range(2):
        path.write_text(path.read_text(encoding="utf-8") + f"\n% Human comment {n}\n", encoding="utf-8")
        drive(e, S.ManuscriptPending, timeout=90)
    current = p.approvals.get(e.ck.pending_approval)
    assert current.target != old.target
    for ref in current.extra_refs:
        assert p.archive.version_info(ref).parents == [current.target]
    assert next(r for r in current.extra_refs if r.artifact_id.endswith(".pdf")).sha256 != old_pdf.sha256
    assert len(writing_calls(e)) == 8


@pytest.mark.skipif(not shutil.which("latexmk"), reason="real latexmk unavailable")
def test_real_tex_stage_reaches_approval_and_pdf_registered(tmp_path):
    p, e = prepare_sample(tmp_path / "real", compile_enabled=True)
    approval = run(e)
    report = CompileReport.model_validate_json((p.root / "paper/build/compile_report.json").read_bytes())
    assert report.ok and report.pages >= 1
    assert (
        not report.errors and not report.undefined_citations and not report.undefined_refs and not report.missing_files
    )
    pdf = p.root / "paper/build/main.pdf"
    assert pdf.stat().st_size > 10000 and pdf.read_bytes().startswith(b"%PDF-")
    pdf_ref = next(r for r in approval.extra_refs if r.artifact_id.endswith(".pdf"))
    assert p.archive.version_info(pdf_ref).parents == [approval.target]
    assert p.archive.get(pdf_ref) == pdf.read_bytes()
    assert len(e.llm.backend.calls) == 10


@pytest.mark.skipif(not shutil.which("latexmk"), reason="real latexmk unavailable")
def test_real_tex_first_repair_succeeds(tmp_path):
    p, e = prepare_sample(tmp_path / "repair-real", compile_enabled=True)

    class MalformedSample(SampleWritingLLM):
        def call(self, request):
            response = super().call(request)
            if request.prompt_id == PROMPT_ID and context_of(request)["section"] == "method":
                response.text = json.dumps(
                    {"section": "method", "latex": r"\section{Method}\begin{tabular}{c}One & Two\\\end{tabular}"}
                )
            return response

    backend = MalformedSample(
        {
            module.REPAIR_PROMPT: [
                {"section": "method", "latex": r"\section{Method} The approved smoke comparison is retained."}
            ]
        }
    )
    e._llm = LLMGateway(p, backend=backend, backoff_base=0)
    approval = run(e)
    assert e.state == S.ManuscriptPending and approval.target.artifact_id == "paper"
    assert sum(c.prompt_id == module.REPAIR_PROMPT for c in backend.calls) == 1
    assert receipt(p, "runtime")["repairs"][0]["installed"] is True
    report = CompileReport.model_validate_json((p.root / "paper/build/compile_report.json").read_bytes())
    assert report.ok and not report.errors


@pytest.mark.parametrize("existing", ["absent", "matching", "human"])
def test_pending_section_compare_before_install(writing, monkeypatch, existing):
    p, e = writing
    to_phase(e, "write")
    original = Writer.write_section

    def crash(writer, output):
        if existing == "matching":
            original(writer, output)
        raise OSError("crash before installed receipt update")

    monkeypatch.setattr(Writer, "write_section", crash)
    assert isinstance(e.tick().result, Error)
    path = p.root / "paper/sections/introduction.tex"
    assert not receipt(p, "section-introduction")["installed"]
    if existing == "human":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\\section{Introduction}\nHuman-authored replacement.\n", encoding="utf-8")
    before = sha256_file(path) if path.exists() else None
    calls = len(e.llm.backend.calls)
    writes = []

    def observe(writer, output):
        writes.append(output.section)
        return original(writer, output)

    monkeypatch.setattr(Writer, "write_section", observe)
    e.request_action("resume")
    resumed = restart(p, e)
    # Stop at the next section boundary so a legitimate subsequent call is not
    # confused with replaying introduction's already completed model call.
    monkeypatch.setattr(module, "GENERATION_ORDER", ("introduction",))
    monkeypatch.setattr(module.WritingStage, "_assemble", lambda *args: None)
    result = resumed.tick().result
    assert len(e.llm.backend.calls) == calls
    if existing == "human":
        assert sha256_file(path) == before
        assert isinstance(result, Stop) and "reconciliation" in result.reason
        assert {o.id for o in result.question.options} == {"restore", "end"}
        assert not writes and not receipt(p, "section-introduction")["installed"]
    else:
        assert isinstance(result, Continue)
        assert receipt(p, "section-introduction")["installed"]
        assert receipt(p, "section-introduction")["file_sha256"] == sha256_file(path)
        assert writes == (["introduction"] if existing == "absent" else [])
        if existing == "matching":
            assert sha256_file(path) == before


def replace_paper_figure(p):
    path = p.root / "paper/figures/fig_c1.pdf"
    before = sha256_file(path)
    # Another real renderer-produced PDF, not a fake signature/placeholder.
    path.write_bytes((p.root / "paper/figures/fig_c2.pdf").read_bytes())
    assert path.read_bytes().startswith(b"%PDF-") and sha256_file(path) != before
    return path, before


@pytest.mark.parametrize("phase", ["write", "compile", "submit"])
def test_figure_replacement_after_figures_fails_closed(writing, phase):
    p, e = writing
    to_phase(e, phase)
    path, _ = replace_paper_figure(p)
    changed = path.read_bytes()
    approvals = p.approvals.list("manuscript")
    calls = len(e.llm.backend.calls)
    result = e.tick().result
    assert isinstance(result, Error) and "figure" in result.message.lower() and "provenance" in result.message
    assert path.read_bytes() == changed
    assert p.approvals.list("manuscript") == approvals
    assert len(e.llm.backend.calls) == calls


def test_pending_manuscript_figure_replacement_engine_reconciliation(writing):
    p, e = writing
    approval = run(e)
    s = e.ck.scratch["writing"]
    figures = {fid: module._ref(ref) for fid, ref in s["figures"].items()}
    old_paper = p.archive.get(approval.target)
    old_figures = {fid: p.archive.get(ref) for fid, ref in figures.items()}
    path, _ = replace_paper_figure(p)
    changed = path.read_bytes()
    approvals = len(p.approvals.list("manuscript"))
    result = e.tick().result
    assert p.approvals.get(approval.approval_id).status == "superseded"
    for _ in range(20):
        if isinstance(result, Error):
            break
        assert e.state != S.ManuscriptPending
        result = e.tick().result
    assert isinstance(result, Error) and "provenance" in result.message
    assert path.read_bytes() == changed
    assert len(p.approvals.list("manuscript")) == approvals
    assert p.archive.get(approval.target) == old_paper
    for fid, ref in figures.items():
        assert p.archive.get(ref) == old_figures[fid]
        assert p.archive.latest(ref.artifact_id) == ref


def test_manuscript_figure_parents_represent_included_bytes(writing):
    p, e = writing
    approval = run(e)
    paper = p.archive.stored_path(approval.target)
    parents = p.archive.version_info(approval.target).parents
    for fid, raw in e.ck.scratch["writing"]["figures"].items():
        ref = module._ref(raw)
        assert ref in parents
        p.archive.get(ref)
        archived = p.archive.stored_path(ref)
        assert (paper / f"figures/{fid}.pdf").read_bytes() == (archived / f"{fid}.pdf").read_bytes()
        assert (p.root / f"artifacts/figures/{fid}/{fid}.pdf").read_bytes() == (archived / f"{fid}.pdf").read_bytes()


def change_figure_dependency(p, e, monkeypatch, tmp_path, dependency):
    from dataclasses import replace

    if dependency == "prompt":
        original = e.llm.prompts.load(module.FIGURE_PROMPT)
        copied = tmp_path / "prompts/writing/prompts"
        copied.mkdir(parents=True)
        (copied / "figure_spec.v2.md").write_text(
            original.path.read_text(encoding="utf-8").replace("version: 1", "version: 2"), encoding="utf-8")
        e.llm.prompts.roots = [tmp_path / "prompts", *e.llm.prompts.roots]
        p.config.prompt_pins[module.FIGURE_PROMPT] = 2
        p.save_config(p.config)
    else:
        loader = e.build_context().skills
        original_load = type(loader).load
        skill = loader.load("scientific-plotting")
        copied = tmp_path / "plotting-skill"
        shutil.copytree(skill.root, copied)
        script = copied / "scripts/plot.py"
        script.write_text(script.read_text(encoding="utf-8") + "\n# Stabilization dependency test\n", encoding="utf-8")
        changed = replace(skill, root=copied, version="0.1.1" if dependency == "skill" else skill.version)
        def load(self, name, *args, **kwargs):
            return changed if name == "scientific-plotting" else original_load(self, name, *args, **kwargs)

        monkeypatch.setattr(type(loader), "load", load)


@pytest.mark.parametrize("phase", ["write", "compile", "submit"])
@pytest.mark.parametrize("dependency", ["prompt", "script", "skill"])
def test_figure_dependency_change_returns_to_figures(writing, monkeypatch, tmp_path, phase, dependency):
    p, e = writing
    to_phase(e, phase)
    s = e.ck.scratch["writing"]
    old_basis = s.get("figure_basis")
    old_key = receipt(p, "figure-fig_c1")["key"]
    old_refs = dict(s["figures"])
    before = len(e.llm.backend.calls)
    change_figure_dependency(p, e, monkeypatch, tmp_path, dependency)
    result = e.tick().result
    assert isinstance(result, Continue)
    assert e.ck.scratch["writing"]["phase"] == "figures"
    assert len(e.llm.backend.calls) == before  # no later phase work before invalidation
    to_phase(e, "template")
    assert s["figure_basis"] != old_basis
    assert receipt(p, "figure-fig_c1")["key"] != old_key
    assert len([c for c in e.llm.backend.calls if c.prompt_id == module.FIGURE_PROMPT]) == 4
    if dependency != "prompt":
        assert s["figures"] != old_refs
    for raw in old_refs.values():
        p.archive.get(module._ref(raw))  # historical versions survive regeneration
    if phase == "compile" and dependency == "script":
        previous = receipt(p, "section-introduction")["fingerprint"]
        run(e)
        assert receipt(p, "section-introduction")["fingerprint"] != previous
        assert len(writing_calls(e)) == 16


def test_dependency_change_does_not_overwrite_replaced_paper_figure(writing, monkeypatch, tmp_path):
    p, e = writing
    to_phase(e, "compile")
    path, _ = replace_paper_figure(p)
    changed = path.read_bytes()
    change_figure_dependency(p, e, monkeypatch, tmp_path, "script")
    result = e.tick().result
    assert isinstance(result, Error) and "provenance" in result.message
    assert path.read_bytes() == changed


@pytest.mark.parametrize("boundary", ["_assemble", "_compile", "_submit"])
def test_each_manuscript_boundary_has_its_own_figure_gate(writing, boundary):
    p, e = writing
    to_phase(e, "submit")
    ctx = e.build_context()
    s = ctx.scratch["writing"]
    stage = module.WritingStage()
    stage._validate_provisioned_figures(ctx, s, complete=True)
    path, _ = replace_paper_figure(p)  # edit after an earlier successful check
    before = path.read_bytes()
    with pytest.raises(ValueError, match="provenance"):
        # No step-level gate here: the boundary must independently reject the edit.
        getattr(stage, boundary)(ctx, s, receipt(p, "runtime"), None)
    assert path.read_bytes() == before


@pytest.mark.parametrize("damage", ["artifact", "archive", "missing", "wrong_ref", "wrong_parent", "metadata"])
def test_figure_gate_checks_full_registered_artifact(writing, damage):
    p, e = writing
    to_phase(e, "submit")
    s = e.ck.scratch["writing"]
    ref = module._ref(s["figures"]["fig_c1"])
    directory = p.root / ref.artifact_id
    if damage == "artifact":
        (directory / "fig_c1.pdf").write_bytes((directory.parent / "fig_c2/fig_c2.pdf").read_bytes())
    elif damage == "archive":
        (p.archive.stored_path(ref) / "fig_c1.pdf").write_bytes(b"corrupt archived PDF")
    elif damage == "missing":
        (p.root / "paper/figures/fig_c1.pdf").unlink()
    elif damage == "wrong_ref":
        s["figures"]["fig_c1"] = s["figures"]["fig_c2"]
    else:
        figure = json.loads((directory / "figure.json").read_text(encoding="utf-8"))
        parents = p.archive.version_info(ref).parents
        if damage == "metadata":
            figure["fig_id"] = "fig_wrong"
        else:
            figure["created_at"] = "2026-10-21T00:00:00Z"
            parents = []
        atomic_write_json(directory / "figure.json", figure)
        changed = p.archive.put(ref.artifact_id, "figure", directory, parents=parents)
        s["figures"]["fig_c1"] = changed.model_dump()
    with pytest.raises(ValueError, match="provenance"):
        module.WritingStage()._submit(e.build_context(), s, receipt(p, "runtime"), None)


def test_compile_fix_dependency_change_precedes_repair(writing, monkeypatch, tmp_path):
    p, e = simulated_compile(writing, monkeypatch)
    before = len(e.llm.backend.calls)
    change_figure_dependency(p, e, monkeypatch, tmp_path, "script")
    result = e.tick().result
    assert isinstance(result, Continue) and e.ck.scratch["writing"]["phase"] == "figures"
    assert len(e.llm.backend.calls) == before


def test_figure_basis_uses_effective_planner_configuration_only(writing):
    p, e = writing
    to_phase(e, "write")
    stage = module.WritingStage()
    selected, _, runtime = stage._inputs(e.build_context())
    original = stage._figure_keys(e.build_context(), selected, runtime)
    p.config.models = {"roles": {"writing.writer": "openai/test-prose"}}
    p.config.prompt_pins[PROMPT_ID] = 1
    p.config.title = "An unrelated prose/template change"
    p.save_config(p.config)
    assert stage._figure_keys(e.build_context(), selected, runtime) == original
    p.config.models["roles"]["writing.figure_spec"] = "openai/test-figure-planner"
    p.save_config(p.config)
    result = e.tick().result
    assert isinstance(result, Continue) and e.ck.scratch["writing"]["phase"] == "figures"
    assert stage._figure_keys(e.build_context(), selected, runtime) != original


def test_changed_figure_refs_after_registered_submit_recovery_have_exact_parents(writing, monkeypatch, tmp_path):
    p, e = writing
    to_phase(e, "submit")
    # Registration completed, but Engine has not consumed NeedsApproval yet.
    first = module.WritingStage().step(e.build_context())
    assert isinstance(first, module.NeedsApproval)
    old_refs = dict(e.ck.scratch["writing"]["figures"])
    old_bytes = (p.root / "paper/figures/fig_c1.pdf").read_bytes()
    change_figure_dependency(p, e, monkeypatch, tmp_path, "script")
    approval = run(e)
    new_refs = e.ck.scratch["writing"]["figures"]
    assert new_refs != old_refs
    assert (p.root / "paper/figures/fig_c1.pdf").read_bytes() == old_bytes
    parents = p.archive.version_info(approval.target).parents
    assert all(module._ref(raw) in parents for raw in new_refs.values())
    assert approval.target != first.target
