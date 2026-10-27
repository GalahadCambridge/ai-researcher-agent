"""Real Stage semantic loop, granular invalidation and transactional recovery (offline)."""

import json
from dataclasses import replace

import pytest

from airesearcher.core.models.claim import CheckReport
from airesearcher.engine.stage import Continue, Error, Stop
from airesearcher.services import evidence
from airesearcher.stages.writing import review as r
from airesearcher.stages.writing import stage as module
from scripts.writing.demo import SampleWritingLLM, context_of, prepare_sample, run

from .test_writing_stage import receipt, restart, to_phase, writing_calls
from .test_writing_true import claim_calls


@pytest.fixture
def sample(tmp_path):
    return prepare_sample(tmp_path / "semantic", compile_enabled=False, evidence_check=True)


def calls(e, prompt):
    return [c for c in e.llm.backend.calls if c.prompt_id == prompt]


def reports(p):
    return [
        CheckReport.model_validate_json(path.read_bytes())
        for path in sorted((p.root / "paper/review").glob("review_v*.json"))
    ]


def scenario(monkeypatch, *, minor=False, residual=False, unbound=False, invalid=None):
    original = SampleWritingLLM.call

    def scripted(self, request):
        response = original(self, request)
        context = context_of(request)
        data = json.loads(response.text)
        if unbound and request.prompt_id == "writing/section" and context["section"] == "abstract":
            data["latex"] += " Our method dominates every evaluated baseline."
        if request.prompt_id == r.REVIEW_PROMPT:
            code = None
            if context["claim_id"] == "CL1" and (minor or residual):
                code = "MIXED_INTERPRETATION" if minor else "OVERCLAIM_SCOPE"
            if unbound and context["sentence"] == "Our method dominates every evaluated baseline.":
                code = "NO_EVIDENCE"
            if code:
                data["issues"] = [
                    {"code": code, "detail": "Scope exceeds available evidence.", "suggestion": "Use cautious scope."}
                ]
            if invalid:
                if invalid == "code":
                    data["issues"] = [{"code": "FAKE", "detail": "bad", "suggestion": "bad"}]
                else:
                    data[invalid] = "invented"
        if unbound and request.prompt_id == r.REVISE_PROMPT:
            data = {
                "claim_id": None,
                "sentence": context["sentence"],
                "replacement": "Further evaluation is needed.",
                "hedge": "none",
            }
        response.text = json.dumps(data)
        return response

    monkeypatch.setattr(SampleWritingLLM, "call", scripted)


def test_clean_semantic_context_boundaries_and_control(sample, tmp_path):
    p, e = sample
    approval = run(e)
    review_calls = calls(e, r.REVIEW_PROMPT)
    assert review_calls and not calls(e, r.REVISE_PROMPT)
    contexts = [context_of(c) for c in review_calls]
    assert {c["claim_id"] for c in contexts if c["claim_id"]} == {"CL1", "CL2"}
    assert {c["section"] for c in contexts if c["claim_id"] is None} == {"abstract", "conclusion"}
    for context in contexts:
        assert len(context["nearby"]) <= 2 and len(json.dumps(context)) < 24000
        assert "runtime" not in context and "plan" not in context
        if context["claim_id"]:
            assert len(context["scope"]["aggregates"]) == 1
    assert "Semantic review rounds: 1; automatic revision rounds: 0" in approval.summary
    assert reports(p)[0].counts == {"blocker": 0, "major": 0, "minor": 0, "supported": 2}
    control, engine = prepare_sample(tmp_path / "control", compile_enabled=False)
    run(engine)
    assert not any(
        c.role in {"writing.claims", "writing.reviewer", "writing.reviser"} for c in engine.llm.backend.calls
    )
    assert not any((control.root / "paper" / name).exists() for name in ("claims.jsonl", "air_values.tex", "review"))


def test_major_revision_round_trip_exact_immutable_evidence_and_history(sample):
    p, e = sample
    e.llm.backend.semantic_revision = True
    to_phase(e, "revise")
    old = evidence.load_claims(p.root)
    values = (p.root / "paper/air_values.tex").read_bytes()
    first = (p.root / "paper/review/review_v1.json").read_bytes()
    old_paper = reports(p)[0].paper_ref
    approval = run(e)
    new = evidence.load_claims(p.root)
    assert len(reports(p)) == 2 and len(calls(e, r.REVISE_PROMPT)) == 1
    assert len(writing_calls(e)) == 8 and len(claim_calls(e)) == 1
    assert (p.root / "paper/air_values.tex").read_bytes() == values
    for a, b in zip(old, new, strict=True):
        assert a.model_dump(exclude={"text", "hedge", "location"}) == b.model_dump(
            exclude={"text", "hedge", "location"}
        )
    assert old[0].text != new[0].text and old[1].text == new[1].text
    assert reports(p)[0].counts["major"] == 1 and reports(p)[1].counts["major"] == 0
    assert reports(p)[1].paper_ref == approval.target != old_paper
    assert (p.root / "paper/review/review_v1.json").read_bytes() == first
    assert "universal superiority" in (p.archive.stored_path(old_paper) / "sections/results.tex").read_text(
        encoding="utf-8")
    assert evidence.check(p.root, paper_ref=old_paper).paper_ref == old_paper
    for index, report in enumerate(reports(p), 1):
        ref = p.archive.latest(f"paper/review/review_v{index}.json")
        parents = p.archive.version_info(ref).parents
        assert parents[0] == report.paper_ref and parents[1] in p.archive.version_info(report.paper_ref).parents
    assert "Semantic review rounds: 2; automatic revision rounds: 1" in approval.summary
    assert "Semantic LLM claim review was not run" not in (p.root / "paper/ai_use.tex").read_text(encoding="utf-8")


@pytest.mark.parametrize("limit", [1, 2])
def test_residual_major_round_cap_submits(sample, monkeypatch, limit):
    p, e = sample
    e.llm.backend.semantic_revision = True
    p.config.paper["max_review_rounds"] = limit
    p.save_config(p.config)
    scenario(monkeypatch, residual=True)
    approval = run(e)
    assert len(reports(p)) == limit
    assert len(calls(e, r.REVISE_PROMPT)) == limit - 1
    assert "RESIDUAL BLOCKER/MAJOR ISSUES" in approval.summary and "1 major" in approval.summary


def test_minor_only_never_revises(sample, monkeypatch):
    p, e = sample
    scenario(monkeypatch, minor=True)
    run(e)
    assert len(reports(p)) == 1 and reports(p)[0].counts["minor"] == 1
    assert not calls(e, r.REVISE_PROMPT)


@pytest.mark.parametrize("invalid", ["sentence", "claim_id", "code"])
def test_invalid_reviewer_output_is_untrusted_and_durable(sample, monkeypatch, invalid):
    p, e = sample
    scenario(monkeypatch, invalid=invalid)
    to_phase(e, "verify")
    from airesearcher.engine.stage import Continue, Stop
    assert isinstance(e.tick().result, Continue)
    assert not list((p.root / "paper/review").glob("review_v*.json"))
    reopened = restart(p, e)
    assert isinstance(reopened.tick().result, Continue)
    assert isinstance(reopened.tick().result, Stop)
    assert len(calls(e, r.REVIEW_PROMPT)) == 2
    basis = module.WritingStage()._review_basis(reopened.build_context(), reopened.ck.scratch["writing"])
    review_name = f"semantic-{basis}"
    assert r.model_context(receipt(p, review_name)["units"][0]) == context_of(calls(e, r.REVIEW_PROMPT)[0])
    # The first invalid unit stops review; select its identity, never directory order.
    directory = p.root / "paper/.writing"
    journal = directory / f"{review_name}-0-attempts.json"
    assert set(directory.glob("semantic-*-attempts.json")) == {journal}
    assert list(directory.glob("section-*-attempts.json"))
    assert len(list(directory.glob("*-attempts.json"))) > 1
    attempts = json.loads(journal.read_text(encoding="utf-8"))["attempts"]
    assert len(attempts) == 2 and all(a["status"] == "rejected" for a in attempts)
    n = len(calls(e, r.REVIEW_PROMPT))
    reopened = restart(p, reopened)
    reopened.tick()
    assert p.questions.pending() and len(calls(e, r.REVIEW_PROMPT)) == n
    assert json.loads(journal.read_text(encoding="utf-8"))["attempts"] == attempts


def test_no_evidence_targeted_surrounding_prose(sample, monkeypatch):
    p, e = sample
    scenario(monkeypatch, unbound=True)
    to_phase(e, "revise")
    old = evidence.load_claims(p.root)
    run(e)
    assert evidence.load_claims(p.root) == old
    assert reports(p)[0].unbound_issues[0].code == "NO_EVIDENCE"
    assert not reports(p)[1].unbound_issues
    text = (p.root / "paper/sections/abstract.tex").read_text(encoding="utf-8")
    assert "Further evaluation is needed." in text and "dominates every" not in text
    assert len(calls(e, r.REVISE_PROMPT)) == 1


@pytest.mark.parametrize("dependency", ["review_prompt", "review_model", "skill"])
def test_granular_semantic_invalidation(sample, monkeypatch, dependency):
    p, e = sample
    to_phase(e, "submit")
    old = e.ck.scratch["writing"]["semantic_basis"]
    n = len(calls(e, r.REVIEW_PROMPT))
    if dependency == "review_prompt":
        original = e.llm.prompts.load

        def load(pid, version=None):
            prompt = original(pid, version)
            return replace(prompt, version=prompt.version + 1) if pid == r.REVIEW_PROMPT else prompt

        monkeypatch.setattr(e.llm.prompts, "load", load)
    elif dependency == "review_model":
        p.config.models = {"roles": {"writing.reviewer": "fast"}}
        p.save_config(p.config)
    else:
        from airesearcher.skills.loader import SkillLoader

        original = SkillLoader.load

        def load(self, name, *args, **kwargs):
            skill = original(self, name, *args, **kwargs)
            return (
                replace(skill, instructions=skill.instructions + "\nClarified review rule.")
                if name == "claim-review"
                else skill
            )

        monkeypatch.setattr(SkillLoader, "load", load)
    run(e)
    assert e.ck.scratch["writing"]["semantic_basis"] != old
    assert len(calls(e, r.REVIEW_PROMPT)) == 2 * n
    assert len(writing_calls(e)) == 8 and len(claim_calls(e)) == 1
    assert len(calls(e, "writing/figure_spec")) == 2
    assert len(reports(p)) == 2


@pytest.mark.parametrize("boundary", ["response", "section", "claims", "bound_receipt"])
def test_revision_transaction_recovery_and_no_duplicate_calls(sample, monkeypatch, boundary):
    p, e = sample
    e.llm.backend.semantic_revision = True
    to_phase(e, "revise")
    original_write = module.atomic_write_bytes
    original_save = module._save
    original_finish = module.WritingStage._finish_revision
    if boundary == "response":

        def crash(self, ctx, pending):
            raise OSError("after accepted revision persisted")

        monkeypatch.setattr(module.WritingStage, "_finish_revision", crash)
    elif boundary == "bound_receipt":

        def crash(ctx, name, value):
            if name == "bound-claims":
                raise OSError("after target files installed")
            return original_save(ctx, name, value)

        monkeypatch.setattr(module, "_save", crash)
    else:

        def crash(path, data):
            if boundary == "section" and path.as_posix().endswith("paper/claims.jsonl"):
                raise OSError("section installed, claims pending")
            if boundary == "claims" and path.as_posix().endswith("sections/results.tex"):
                pending = receipt(p, "pending-revision")
                original_write(p.root / "paper/claims.jsonl", pending["new"]["paper/claims.jsonl"].encode())
                raise OSError("claims installed, section pending")
            return original_write(path, data)

        monkeypatch.setattr(module, "atomic_write_bytes", crash)
    assert isinstance(e.tick().result, Error)
    assert len(calls(e, r.REVISE_PROMPT)) == 1
    monkeypatch.setattr(module, "atomic_write_bytes", original_write)
    monkeypatch.setattr(module, "_save", original_save)
    monkeypatch.setattr(module.WritingStage, "_finish_revision", original_finish)
    e.request_action("resume")
    reopened = restart(p, e)
    run(reopened)
    assert len(calls(reopened, r.REVISE_PROMPT)) == 1
    assert len(reports(p)) == 2 and reports(p)[-1].counts["major"] == 0
    assert receipt(p, "pending-revision")["installed"] and receipt(p, "pending-revision")["assembled"]
    assert len(writing_calls(reopened)) == 8 and len(claim_calls(reopened)) == 1


@pytest.mark.parametrize("boundary", ["file", "archive"])
def test_review_response_recovery_no_duplicate_calls(sample, monkeypatch, boundary):
    p, e = sample
    to_phase(e, "verify")
    original_write = module.atomic_write_bytes
    archive_type = type(p.archive)
    original_put = archive_type.put
    if boundary == "file":

        def crash(path, data):
            if str(path).endswith("review_v1.json"):
                raise OSError("response saved before artifact")
            return original_write(path, data)

        monkeypatch.setattr(module, "atomic_write_bytes", crash)
    else:

        def crash(self, aid, *args, **kwargs):
            if aid.endswith("review_v1.json"):
                raise OSError("review file written before archive registration")
            return original_put(self, aid, *args, **kwargs)

        monkeypatch.setattr(archive_type, "put", crash)
    assert isinstance(e.tick().result, Error)
    n = len(calls(e, r.REVIEW_PROMPT))
    monkeypatch.setattr(module, "atomic_write_bytes", original_write)
    monkeypatch.setattr(archive_type, "put", original_put)
    e.request_action("resume")
    reopened = restart(p, e)
    run(reopened)
    assert len(calls(reopened, r.REVIEW_PROMPT)) == n and len(reports(p)) == 1


@pytest.mark.parametrize("target", ["paper/sections/results.tex", "paper/claims.jsonl", "paper/air_values.tex"])
def test_pending_revision_never_overwrites_intervening_human_edit(sample, monkeypatch, target):
    p, e = sample
    e.llm.backend.semantic_revision = True
    to_phase(e, "revise")
    original = module.WritingStage._finish_revision

    def crash(self, ctx, pending):
        raise OSError("interrupted before installation")

    monkeypatch.setattr(module.WritingStage, "_finish_revision", crash)
    assert isinstance(e.tick().result, Error)
    path = p.root / target
    changed = path.read_bytes() + b"\n% human edit\n"
    path.write_bytes(changed)
    monkeypatch.setattr(module.WritingStage, "_finish_revision", original)
    e.request_action("resume")
    reopened = restart(p, e)
    for _ in range(4):
        tick = reopened.tick()
        if isinstance(tick.result, Stop):
            break
    assert isinstance(tick.result, Stop) and "intervening" in tick.result.reason
    assert path.read_bytes() == changed and len(calls(reopened, r.REVISE_PROMPT)) == 1


def test_legacy_review_number_preserved(sample):
    p, e = sample
    to_phase(e, "verify")
    path = p.root / "paper/review/review_v7.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"legacy":true}\n')
    assert isinstance(e.tick().result, Continue)
    run(e)
    assert path.read_bytes() == b'{"legacy":true}\n'
    assert (path.parent / "review_v8.json").exists()


@pytest.mark.parametrize("dependency", ["prompt", "model"])
def test_reviser_receipt_invalidation_after_response_before_acceptance(sample, monkeypatch, dependency):
    p, e = sample
    e.llm.backend.semantic_revision = True
    to_phase(e, "revise")
    original = r.validate_revision

    def crash(*args, **kwargs):
        raise OSError("response saved, before validation/acceptance")

    monkeypatch.setattr(r, "validate_revision", crash)
    assert isinstance(e.tick().result, Error)
    assert len(calls(e, r.REVISE_PROMPT)) == 1
    monkeypatch.setattr(r, "validate_revision", original)
    if dependency == "prompt":
        load_original = e.llm.prompts.load

        def load(pid, version=None):
            prompt = load_original(pid, version)
            return replace(prompt, version=prompt.version + 1) if pid == r.REVISE_PROMPT else prompt

        monkeypatch.setattr(e.llm.prompts, "load", load)
    else:
        p.config.models = {"roles": {"writing.reviser": "fast"}}
        p.save_config(p.config)
    e.request_action("resume")
    reopened = restart(p, e)
    run(reopened)
    assert len(calls(reopened, r.REVISE_PROMPT)) == 2
    assert len(claim_calls(reopened)) == 1 and len(writing_calls(reopened)) == 8


def test_recovery_equivalent_to_uninterrupted_revision(sample, tmp_path, monkeypatch):
    import shutil

    from airesearcher.core.project import Project
    from airesearcher.engine.engine import ProjectEngine
    from airesearcher.llm.gateway import LLMGateway

    p, e = sample
    e.llm.backend.semantic_revision = True
    to_phase(e, "revise")
    clone_root = tmp_path / "clone"
    shutil.copytree(p.root, clone_root)
    clone = Project.open(clone_root)
    backend = SampleWritingLLM()
    backend.semantic_revision = True
    cloned_engine = ProjectEngine(clone, llm=LLMGateway(clone, backend=backend, backoff_base=0), retry_delay=0)
    uninterrupted = run(e)
    original = module.WritingStage._finish_revision

    def crash(self, ctx, pending):
        raise OSError("accepted but not installed")

    monkeypatch.setattr(module.WritingStage, "_finish_revision", crash)
    assert isinstance(cloned_engine.tick().result, Error)
    monkeypatch.setattr(module.WritingStage, "_finish_revision", original)
    cloned_engine.request_action("resume")
    recovered = run(restart(clone, cloned_engine))
    assert recovered.target == uninterrupted.target
    assert evidence.load_claims(clone.root) == evidence.load_claims(p.root)
    for a, b in zip(reports(p), reports(clone), strict=True):
        assert a.model_dump(exclude={"created_at"}) == b.model_dump(exclude={"created_at"})


def test_unbound_results_sentence_and_oversized_unit_fail_explicitly(sample):
    p, e = sample
    to_phase(e, "verify")
    path = p.root / "paper/sections/results.tex"
    path.write_text(path.read_text(encoding="utf-8") + "\nOur method beats all rivals.\n", encoding="utf-8")
    run(e)
    contexts = [context_of(call) for call in calls(e, r.REVIEW_PROMPT)]
    assert any(
        c["section"] == "results" and c["claim_id"] is None and c["sentence"] == "Our method beats all rivals."
        for c in contexts
    )
    # Exercise bounded extraction against a private copy, never mutate archived history.
    import shutil

    scratch = p.root / "unit-source"
    shutil.copytree(p.root / "paper", scratch)
    (scratch / "sections/abstract.tex").write_text("Oversized " + "word " * 600 + ".", encoding="utf-8")
    bundle = module.EvidenceBundle.model_validate_json((p.root / "paper/evidence_bundle.json").read_bytes())
    with pytest.raises(ValueError, match="bounded"):
        r.units(scratch, evidence.load_claims(p.root), bundle, reports(p)[0])


def test_source_changed_during_model_call_is_not_overwritten(sample, monkeypatch):
    p, e = sample
    e.llm.backend.semantic_revision = True
    to_phase(e, "revise")
    before = (p.root / "paper/claims.jsonl").read_bytes()
    original = SampleWritingLLM.call
    path = p.root / "paper/sections/discussion.tex"
    changed = path.read_text(encoding="utf-8") + "\n% human edit during revision call\n"

    def change(self, request):
        response = original(self, request)
        if request.prompt_id == r.REVISE_PROMPT:
            path.write_text(changed, encoding="utf-8")
        return response

    monkeypatch.setattr(SampleWritingLLM, "call", change)
    tick = e.tick()
    assert isinstance(tick.result, Error) and "source changed during" in tick.result.message
    assert path.read_text(encoding="utf-8") == changed and (p.root / "paper/claims.jsonl").read_bytes() == before
    assert not (p.root / "paper/.writing/pending-revision.json").exists()


def test_real_numeric_blocker_cannot_be_waived_or_revised(sample):
    from airesearcher.services import runs

    p, e = sample
    e.llm.backend.semantic_revision = True
    to_phase(e, "verify")
    run_id = runs.load_aggregate(p.root, "C1", version=1).rows[0].run_ids[0]
    path = p.root / f"runs/{run_id}/metrics.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        if row["name"] == "score":
            row["value"] += 0.25
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    approval = run(e)
    final = reports(p)[-1]
    assert any(i.code == "NUM_MISMATCH" and i.severity == "blocker" for i in r.all_issues(final))
    assert any(i.code == "OVERCLAIM_SCOPE" for i in r.all_issues(final))
    assert "RESIDUAL BLOCKER/MAJOR ISSUES" in approval.summary
    assert not calls(e, r.REVISE_PROMPT) and len(reports(p)) == 1


def test_exact_revision_preserves_crlf_and_surrounding_human_prose(sample):
    p, e = sample
    e.llm.backend.semantic_revision = True
    to_phase(e, "compile")
    path = p.root / "paper/sections/results.tex"
    path.write_bytes(("% preserved human header\n" + path.read_text(encoding="utf-8")).replace("\n", "\r\n").encode())
    run(e)
    data = path.read_bytes()
    assert data.startswith(b"% preserved human header\r\n")
    assert b"\n" not in data.replace(b"\r\n", b"")
    assert b"limited to the evaluated scope" in data
    assert receipt(p, "section-results")["human_edited"] is True
    assert "Automatic revision rounds completed: 1" in (p.root / "paper/review/review_v2.md").read_text(
        encoding="utf-8")
