"""Production Figure-unit Questions using the real engine and controlled offline LLM."""

import json

import pytest

from airesearcher.core.models.common import ProjectState as S
from airesearcher.engine.stage import Error, Stop
from airesearcher.stages.writing import stage as module
from scripts.writing.demo import SampleWritingLLM, prepare_sample

from .helpers import drive
from .test_writing_stage import receipt, restart, to_phase


@pytest.fixture
def sample(tmp_path, monkeypatch):
    original = SampleWritingLLM.call

    def missing(self, request):
        response = original(self, request)
        if request.prompt_id == module.FIGURE_PROMPT:
            data = json.loads(response.text)
            data.update(xlabel=" \t", ylabel=" \t")
            response.text = json.dumps(data)
        return response

    monkeypatch.setattr(SampleWritingLLM, "call", missing)
    p, e = prepare_sample(tmp_path / "units", compile_enabled=False, evidence_check=True)
    to_phase(e, "figures")
    return p, e


def calls(e):
    return [c for c in e.llm.backend.calls if c.prompt_id == module.FIGURE_PROMPT]


def ask(p, e):
    result = e.tick().result
    assert isinstance(result, Stop), result
    q = p.questions.pending()
    assert q and q.allow_text and not q.options
    assert not (p.root / "artifacts/figures/fig_c1").exists()
    return q


def answer(p, e, text):
    q = p.questions.pending()
    p.questions.answer(q.question_id, None, text=text, request_id=q.question_id)
    return restart(p, e)


def test_blank_axes_persist_original_and_stop_before_prepare(sample, monkeypatch):
    p, e = sample

    def forbidden(*args, **kwargs):
        pytest.fail("blank ylabel reached plotting validation")

    monkeypatch.setattr(module, "prepare_data", forbidden)
    q = ask(p, e)
    saved = receipt(p, "figure-fig_c1")
    assert saved["original_proposal"]["ylabel"] == " \t"
    assert saved["original_proposal"]["xlabel"] == " \t"
    assert saved["spec"]["xlabel"] == "method" and saved["spec"]["ylabel"] == " \t"
    assert saved["call_id"] and saved["prompt_id"] == module.FIGURE_PROMPT and saved["prompt_version"] == 1
    assert len(calls(e)) == 1
    assert q.refs[0].artifact_id == "artifacts/aggregates/C1.json"


def test_valid_answer_restart_and_same_metric_reuse(sample):
    p, e = sample
    q = ask(p, e)
    old = receipt(p, "figure-fig_c1")
    e = restart(p, e)
    for _ in range(2):
        e.tick()
        assert e.state == S.Paused and p.questions.pending().question_id == q.question_id
        assert len(calls(e)) == 1
    e = answer(p, e, "score (dimensionless)")
    to_phase(e, "template")
    first, second = receipt(p, "figure-fig_c1"), receipt(p, "figure-fig_c2")
    assert first["original_proposal"] == old["original_proposal"]
    assert first["call_id"] == old["call_id"]
    assert first["spec"]["ylabel"] == second["spec"]["ylabel"] == "score (dimensionless)"
    assert first["clarifications"][0]["answer"]["text"] == "score (dimensionless)"
    assert second["ylabel_source"]["question_id"] == first["ylabel_source"]["question_id"]
    assert len(calls(e)) == 2
    assert len([r for r in p.questions.records() if r.question.stage == "writing"]) == 1
    for fid in ("fig_c1", "fig_c2"):
        assert (p.root / f"artifacts/figures/{fid}/{fid}.pdf").read_bytes().startswith(b"%PDF")


def test_invalid_answer_reasks_and_limit_is_finite(sample):
    p, e = sample
    ask(p, e)
    for attempt in range(1, 4):
        e = answer(p, e, "score")
        drive(e, {S.Paused, S.Failed})
        assert len(calls(e)) == 1
        assert not (p.root / "artifacts/figures/fig_c1").exists()
        assert len(receipt(p, "figure-fig_c1")["clarifications"]) == attempt
        if attempt < 3:
            assert e.state == S.Paused and "unit" in p.questions.pending().text
        else:
            assert e.state == S.Failed and "3" in e.ck.reason


def test_changed_dependency_rejects_stale_answer(sample):
    p, e = sample
    ask(p, e)
    old = receipt(p, "figure-fig_c1")
    p.config.models = {"roles": {"writing.figure_spec": "fast"}}
    p.save_config(p.config)
    e = answer(p, e, "score (dimensionless)")
    drive(e, S.Failed)
    assert "stale" in e.ck.reason.lower()
    assert receipt(p, "figure-fig_c1") == old
    assert len(calls(e)) == 1


@pytest.mark.parametrize("boundary", ["proposal", "answer"])
def test_receipt_crash_does_not_repeat_model_or_answer(sample, monkeypatch, boundary):
    p, e = sample
    original_save = module._save
    if boundary == "answer":
        ask(p, e)
        e = answer(p, e, "score (dimensionless)")
    fired = False

    def crash(ctx, name, value):
        nonlocal fired
        original_save(ctx, name, value)
        if not fired and name == "figure-fig_c1" and (boundary == "proposal" or value.get("clarifications")):
            fired = True
            raise OSError("after durable Figure receipt")

    monkeypatch.setattr(module, "_save", crash)
    assert isinstance(e.tick().result, Error)
    old = receipt(p, "figure-fig_c1")
    monkeypatch.setattr(module, "_save", original_save)
    e.request_action("resume")
    e = restart(p, e)
    if boundary == "proposal":
        drive(e, S.Paused)
        assert len(calls(e)) == 1
        e = answer(p, e, "score (dimensionless)")
    to_phase(e, "template")
    saved = receipt(p, "figure-fig_c1")
    assert saved["original_proposal"] == old["original_proposal"] and saved["call_id"] == old["call_id"]
    assert len(saved["clarifications"]) == 1 and len(calls(e)) == 2


@pytest.mark.parametrize("field,value", [("metric", "latency"), ("scale", 100)])
def test_clarification_cache_never_crosses_metric_or_scale(sample, field, value):
    from airesearcher.core.fsutil import atomic_write_json
    from airesearcher.core.models.figure import FigureSpec
    from airesearcher.stages.writing.bundle import EvidenceBundle

    p, e = sample
    ask(p, e)
    e = answer(p, e, "score (dimensionless)")
    e.tick()  # accept the unit and render C1
    meta = receipt(p, "runtime")
    assert len(meta["figure_units"]) == 1
    known = next(iter(meta["figure_units"].values()))
    bundle = EvidenceBundle.model_validate_json((p.root / "paper/evidence_bundle.json").read_bytes())
    spec = FigureSpec.model_validate(receipt(p, "figure-fig_c1")["spec"])
    setattr(spec, field, value)
    different = module.WritingStage()._figure_unit_key(bundle, spec)
    assert different not in meta["figure_units"]
    meta["figure_units"] = {different: known}
    atomic_write_json(p.root / "paper/.writing/runtime.json", meta)
    # Isolate C2 so the already validated C1 cannot repopulate its own valid cache.
    bundle.aggregates = [a for a in bundle.aggregates if a.aggregate_id == "C2"]
    ctx = e.build_context()
    state = ctx.scratch["writing"]
    result = module.WritingStage()._figures(
        ctx, state, meta, bundle, {"fig_c2": "isolated-C2-proposal"}, "isolated-basis"
    )
    assert isinstance(result, Stop) and receipt(p, "runtime")["waiting"]["fig_id"] == "fig_c2"
    assert not (p.root / "artifacts/figures/fig_c2").exists()
    assert len(calls(e)) == 2


def test_already_valid_spec_does_not_ask_or_change(tmp_path):
    p, e = prepare_sample(tmp_path / "valid", compile_enabled=False, evidence_check=True)
    to_phase(e, "template")
    assert len(calls(e)) == 2
    assert not [r for r in p.questions.records() if r.question.stage == "writing"]
    for fid in ("fig_c1", "fig_c2"):
        saved = receipt(p, "figure-" + fid)
        assert saved["original_proposal"] == saved["spec"]
        assert json.loads(saved["original_response"]) == saved["original_proposal"]
        assert not saved.get("clarifications")


def test_blank_xlabel_with_unknown_group_is_not_invented(sample):
    from airesearcher.engine.stage import Continue
    p, e = sample
    from airesearcher.services import runs
    from airesearcher.stages.writing.figures import default_spec

    spec = default_spec(runs.load_aggregate(p.root, "C1"), ylabel="score (dimensionless)").model_dump()
    spec.update(xlabel="", x="invented", ylabel="")
    e.llm.backend.script[module.FIGURE_PROMPT] = [spec]
    result = e.tick().result
    assert isinstance(result, Continue)
    paths = list((p.root / "paper/.writing").glob("figure-fig_c1-*-attempts.json"))
    assert len(paths) == 1
    saved = json.loads(paths[0].read_bytes())["attempts"][0]
    assert saved["status"] == "rejected" and "grouping field" in saved["reason"]
    assert saved["response"] == json.loads(saved["raw"])
    assert saved["response"]["x"] == "invented" and not saved["response"]["xlabel"].strip()
    assert len(calls(e)) == 1 and not (p.root / "artifacts/figures/fig_c1").exists()
    assert not p.questions.pending()


def test_unit_answer_does_not_mask_other_scientific_failures(sample):
    from airesearcher.engine.stage import Continue
    p, e = sample
    from airesearcher.services import runs
    from airesearcher.stages.writing.figures import default_spec

    spec = default_spec(runs.load_aggregate(p.root, "C1"), ylabel="score (dimensionless)").model_dump()
    spec.update(ylabel="", error="none")
    e.llm.backend.script[module.FIGURE_PROMPT] = [spec]
    ask(p, e)
    e = answer(p, e, "score (dimensionless)")
    result = e.tick().result
    assert isinstance(result, Continue)
    saved = receipt(p, "figure-fig_c1")
    assert saved["status"] == "rejected" and "uncertainty" in saved["rejection_reason"]
    journal = receipt(p, saved["attempt_journal"])
    assert len(journal["attempts"]) == 1 and journal["attempts"][0]["status"] == "rejected"
    assert not (p.root / "artifacts/figures/fig_c1").exists()
    assert len(calls(e)) == 1


def test_empty_answer_rejected_by_production_questions(sample):
    from airesearcher.core.errors import ValidationFailed

    p, e = sample
    q = ask(p, e)
    with pytest.raises(ValidationFailed, match="文字"):
        p.questions.answer(q.question_id, None, text="  ")
    assert p.questions.pending().question_id == q.question_id
    assert len(calls(e)) == 1


@pytest.mark.parametrize("valid_on", [2, 3])
def test_valid_clarification_after_rejected_attempts(sample, valid_on):
    p, e = sample
    ask(p, e)
    for _ in range(valid_on - 1):
        e = answer(p, e, "score")
        drive(e, S.Paused)
    e = answer(p, e, "score (dimensionless)")
    to_phase(e, "template")
    saved = receipt(p, "figure-fig_c1")
    assert len(saved["clarifications"]) == valid_on and len(calls(e)) == 2
    assert len([c for c in saved["clarifications"] if "validation_error" in c]) == valid_on - 1
    assert saved["original_proposal"]["ylabel"] == " \t"


def test_effective_proposal_change_rejects_pending_answer(sample):
    from airesearcher.core.fsutil import atomic_write_json

    p, e = sample
    ask(p, e)
    saved = receipt(p, "figure-fig_c1")
    saved["spec"]["scale"] = 100
    atomic_write_json(p.root / "paper/.writing/figure-fig_c1.json", saved)
    e = answer(p, e, "score (dimensionless)")
    drive(e, S.Failed)
    assert "stale" in e.ck.reason.lower()
    assert receipt(p, "figure-fig_c1") == saved
    assert len(calls(e)) == 1


@pytest.mark.parametrize("boundary", ["proposal", "prepare"])
def test_unit_recovery_uses_type_not_diagnostic_text(sample, monkeypatch, boundary):
    from airesearcher.core.figure_errors import UnitLabelError
    from airesearcher.stages.writing import figures

    p, e = sample
    ask(p, e)
    e = answer(p, e, "score (dimensionless)")

    def revised_diagnostic(*args):
        raise UnitLabelError("Declare an explicit measurement unit")

    monkeypatch.setattr(figures if boundary == "proposal" else module, "prepare_data", revised_diagnostic)
    result = e.tick().result
    assert isinstance(result, Stop) and p.questions.pending().allow_text
    saved = receipt(p, "figure-fig_c1")
    assert saved["clarifications"][-1]["validation_error"] == "Declare an explicit measurement unit"
    assert len(calls(e)) == 1 and not (p.root / "artifacts/figures/fig_c1").exists()
