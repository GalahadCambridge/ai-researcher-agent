"""Evidence-aware Stage vertical slice, recovery, ownership and exact provenance."""

import json

import pytest

from airesearcher.core.models.claim import CheckReport
from airesearcher.engine.stage import Error
from airesearcher.services import evidence
from airesearcher.stages.writing import claims as binding
from scripts.writing.demo import prepare_sample, run

from .test_writing_stage import TEMPLATE, restart, set_template, to_phase, writing_calls


def claim_calls(engine):
    return [c for c in engine.llm.backend.calls if c.prompt_id == binding.PROMPT_ID]


@pytest.fixture
def writing_true(tmp_path):
    return prepare_sample(tmp_path / "true", compile_enabled=False, evidence_check=True)


def test_true_vertical_slice_archive_and_trace(writing_true):
    p, e = writing_true
    approval = run(e)
    assert len(claim_calls(e)) == 1 and len(writing_calls(e)) == 8
    s = e.ck.scratch["writing"]
    claims = evidence.load_claims(p.root)
    assert [c.claim_id for c in claims] == ["CL1", "CL2"]
    assert all(c.location["line"] > 0 for c in claims)
    report = CheckReport.model_validate_json((p.root / "paper/review/review_v1.json").read_bytes())
    assert report.paper_ref == approval.target
    assert report.counts == {"blocker": 0, "major": 0, "minor": 0, "supported": 2}
    assert "Semantic review rounds: 1; automatic revision rounds: 0" in approval.summary
    parents = p.archive.version_info(approval.target).parents
    claims_ref = p.archive.latest("paper/claims.jsonl")
    assert claims_ref in parents
    assert not any(r.artifact_id == "paper" for r in p.archive.version_info(claims_ref).parents)
    assert set(r.artifact_id for r in parents) == {
        "paper/claims.jsonl",
        "paper/evidence_bundle.json",
        "artifacts/aggregates/C1.json",
        "artifacts/aggregates/C2.json",
        "artifacts/figures/fig_c1",
        "artifacts/figures/fig_c2",
    }
    for r in approval.extra_refs:
        if r != claims_ref:
            assert approval.target in p.archive.version_info(r).parents
    trace = evidence.trace(p.root, "CL1")
    assert trace.check == report.claims[0]
    assert trace.figures[0]["ref"] == s["figures"]["fig_c1"]
    for call in writing_calls(e):
        from scripts.writing.demo import context_of

        context = context_of(call)
        assert "aggregates" not in context and context["evidence_check"] is True
    assert len(list((p.root / "paper/review").glob("review_v*.json"))) == 1


@pytest.mark.parametrize("phase", ["claims", "write", "compile", "verify", "submit"])
def test_restart_true_boundaries(writing_true, phase):
    p, e = writing_true
    to_phase(e, phase)
    if phase == "claims":
        e.tick()  # response durably saved, binding not installed
        assert len(claim_calls(e)) == 1
    resumed = restart(p, e)
    run(resumed)
    assert len(claim_calls(resumed)) == 1 and len(writing_calls(resumed)) == 8


def test_one_correction_and_coverage_failure(writing_true, monkeypatch):
    from scripts.writing.demo import SampleWritingLLM

    p, e = writing_true
    original = SampleWritingLLM.call

    def wrong(self, request):
        response = original(self, request)
        if request.prompt_id == binding.PROMPT_ID:
            # Rewrite valid structured model output without touching production binding.
            data = json.loads(response.text)
            data["claims"][0]["hypothesis"] = None
            response.text = json.dumps(data)
        return response

    monkeypatch.setattr(SampleWritingLLM, "call", wrong)
    for _ in range(30):
        tick = e.tick()
        if isinstance(tick.result, Error):
            break
    assert isinstance(tick.result, Error) and "coverage" in tick.result.message
    assert len(claim_calls(e)) == 2 and not writing_calls(e)
    assert not (p.root / "paper/claims.jsonl").exists()


@pytest.mark.parametrize("relative", ["claims.jsonl", "air_values.tex", "sections/results.tex"])
def test_code_owned_edits_fail_closed(writing_true, relative):
    p, e = writing_true
    to_phase(e, "compile")
    path = p.root / "paper" / relative
    text = path.read_text()
    changed = text.replace("observed mean", "invented mean") if relative.endswith("results.tex") else text + "% edit\n"
    path.write_text(changed)
    calls = len(e.llm.backend.calls)
    for _ in range(4):
        tick = e.tick()
        if isinstance(tick.result, Error):
            break
    assert isinstance(tick.result, Error)
    assert path.read_text() == changed and len(e.llm.backend.calls) == calls


def test_surrounding_prose_updates_locations_without_replanning(writing_true):
    p, e = writing_true
    to_phase(e, "compile")
    before = evidence.load_claims(p.root)
    path = p.root / "paper/sections/results.tex"
    path.write_text("% preserved human context\n\n" + path.read_text())
    approval = run(e)
    after = evidence.load_claims(p.root)
    assert len(claim_calls(e)) == 1 and len(writing_calls(e)) == 8
    assert all(a.location["line"] == b.location["line"] + 2 for a, b in zip(after, before, strict=True))
    assert all(
        a.model_dump(exclude={"location"}) == b.model_dump(exclude={"location"})
        for a, b in zip(after, before, strict=True)
    )
    assert approval.target == p.archive.latest("paper")


@pytest.mark.parametrize("change", ["template", "prose_prompt", "claims_prompt"])
def test_claim_basis_separates_template_and_prose(writing_true, change, monkeypatch):
    p, e = writing_true
    to_phase(e, "compile")
    before = e.ck.scratch["writing"]["claim_basis"]
    if change == "template":
        set_template(p, {"main.tex": TEMPLATE})
    else:
        from dataclasses import replace

        original = e.llm.prompts.load
        target = "writing/section" if change == "prose_prompt" else "writing/claims"
        # A supported pin of existing immutable prose prompt changes writing only.
        if change == "prose_prompt":
            p.config.prompt_pins[target] = 2
            p.save_config(p.config)
        else:

            def changed(prompt_id, version=None):
                prompt = original(prompt_id, version)
                if prompt_id == target:
                    return replace(prompt, version=prompt.version + 1)
                return prompt

            monkeypatch.setattr(e.llm.prompts, "load", changed)
    run(e)
    assert len(claim_calls(e)) == (2 if change == "claims_prompt" else 1)
    assert (e.ck.scratch["writing"]["claim_basis"] != before) is (change == "claims_prompt")


def test_review_receipt_recovery_does_not_recheck(writing_true, monkeypatch):
    from airesearcher.stages.writing import stage

    p, e = writing_true
    to_phase(e, "verify")
    original = stage.atomic_write_bytes

    def crash(path, content):
        if str(path).endswith("review_v1.json"):
            raise OSError("crash after check receipt")
        return original(path, content)

    monkeypatch.setattr(stage, "atomic_write_bytes", crash)
    assert isinstance(e.tick().result, Error)
    assert len(list((p.root / "paper/.writing").glob("check-*.json"))) == 1
    monkeypatch.setattr(stage, "atomic_write_bytes", original)
    monkeypatch.setattr(evidence, "check", lambda *a, **k: pytest.fail("completed check repeated"))
    e.request_action("resume")
    run(restart(p, e))
    assert len(list((p.root / "paper/review").glob("review_v*.json"))) == 1


def test_figure_regeneration_rebinds_claims_without_stale_parents(writing_true, monkeypatch, tmp_path):
    from .test_writing_stage import change_figure_dependency

    p, e = writing_true
    to_phase(e, "compile")
    before = p.archive.latest("paper/claims.jsonl")
    old_figures = dict(e.ck.scratch["writing"]["figures"])
    change_figure_dependency(p, e, monkeypatch, tmp_path, "script")
    approval = run(e)
    assert len(claim_calls(e)) == 2
    after = p.archive.latest("paper/claims.jsonl")
    assert after != before and old_figures != e.ck.scratch["writing"]["figures"]
    parents = p.archive.version_info(after).parents
    assert all(binding.VersionRef.model_validate(ref) in parents for ref in e.ck.scratch["writing"]["figures"].values())
    assert after in p.archive.version_info(approval.target).parents
    p.archive.get(before)


def test_compile_repair_cannot_change_bound_claim(writing_true, monkeypatch):
    from airesearcher.stages.writing import stage

    from .test_writing_stage import receipt, simulated_compile

    p, e = simulated_compile(
        writing_true,
        monkeypatch,
        errors=[{"file": "sections/results.tex", "line": 1, "message": "synthetic syntax failure"}],
    )
    path = p.root / "paper/sections/results.tex"
    original = path.read_text()
    e.llm.backend.script[stage.REPAIR_PROMPT] = [
        {"section": "results", "latex": original.replace("observed", "invented")}
    ]
    e.tick()
    attempts = receipt(p, "runtime")["repairs"]
    assert attempts[-1]["status"] == "rejected" and "bound claim" in attempts[-1]["reason"]
    assert path.read_text() == original and len(claim_calls(e)) == 1


def test_missing_link_is_unverifiable_not_supported(writing_true):
    p, e = writing_true
    run(e)
    # Check a new unarchived candidate independently; Stage itself rejects such edits.
    path = p.root / "paper/claims.jsonl"
    data = [json.loads(line) for line in path.read_text().splitlines()]
    data[0]["evidence"].append({"type": "figure", "ref": "absent", "version": 1})
    path.write_text("".join(json.dumps(c) + "\n" for c in data))
    report = evidence.check(p.root)
    assert report.claims[0].support == "unverifiable"


def test_trace_ignores_newer_figure_and_uses_frozen_runtime(writing_true):
    from airesearcher.core.fsutil import atomic_write_json

    p, e = writing_true
    approval = run(e)
    before = evidence.trace(p.root, "CL1")
    old_ref = before.figures[0]["ref"]
    newer = p.root / old_ref["artifact_id"]
    metadata = json.loads((newer / "figure.json").read_text())
    metadata["spec"]["caption"] += " New unrelated rendering."
    atomic_write_json(newer / "figure.json", metadata)
    new_ref = p.archive.put(old_ref["artifact_id"], "figure", newer)
    assert new_ref.version > old_ref["version"]
    for row in before.runs:
        atomic_write_json(
            p.root / f"runs/{row['run_id']}/environment.json",
            {"python": "CHANGED", "api_key": "NEVER-EXPOSE", "env_vars": {"PASSWORD": "secret"}},
        )
    after = evidence.trace(p.root, "CL1")
    assert after.figures == before.figures and after.runs == before.runs
    assert "NEVER-EXPOSE" not in after.model_dump_json()
    assert after.check == evidence.check(p.root, paper_ref=approval.target).claims[0]
