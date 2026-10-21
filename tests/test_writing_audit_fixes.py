"""Final audit: transaction ordering, immutable comparison facts and dynamic budget."""

import json

import pytest

from airesearcher.core.models.claim import Comparison
from airesearcher.engine.stage import Error
from airesearcher.stages.writing import review as r
from airesearcher.stages.writing import stage as module
from scripts.writing.demo import SampleWritingLLM, prepare_sample, run

from .test_writing_review import claim, unit
from .test_writing_semantic_stage import calls, reports
from .test_writing_stage import receipt, restart, to_phase


@pytest.mark.parametrize("human", [False, True])
@pytest.mark.parametrize("installed,regenerate", [(False, False), (True, False), (False, True)])
def test_pending_revision_precedes_figure_invalidation(tmp_path, monkeypatch, human, installed, regenerate):
    p, e = prepare_sample(tmp_path / "recovery", compile_enabled=False, evidence_check=True)
    e.llm.backend.semantic_revision = True
    to_phase(e, "revise")
    old_basis = e.ck.scratch["writing"]["figure_basis"]
    original = module.WritingStage._finish_revision
    old_claim_basis = e.ck.scratch["writing"]["claim_basis"]
    old_figures = dict(e.ck.scratch["writing"]["figures"])

    def crash(self, ctx, pending):
        if installed:
            original(self, ctx, pending)
        raise OSError("accepted transaction before installation")

    monkeypatch.setattr(module.WritingStage, "_finish_revision", crash)
    assert isinstance(e.tick().result, Error)
    accepted = receipt(p, "pending-revision")["basis"]
    p.config.models = {"roles": {"writing.figure_spec": "fast"}}
    p.save_config(p.config)
    if regenerate:
        original_call = SampleWritingLLM.call

        def new_figure(self, request):
            response = original_call(self, request)
            if request.prompt_id == "writing/figure_spec":
                data = json.loads(response.text)
                data["title"] += " (updated presentation)"
                response.text = json.dumps(data)
            return response

        monkeypatch.setattr(SampleWritingLLM, "call", new_figure)
        e.llm.backend.semantic_revision = False
    path = p.root / "paper/sections/results.tex"
    if human:
        changed = path.read_bytes() + b"\n% human edit\n"
        path.write_bytes(changed)
    monkeypatch.setattr(module.WritingStage, "_finish_revision", original)
    e.request_action("resume")
    reopened = restart(p, e)
    # Regeneration may legitimately need a new review cycle; the accepted response
    # must be consumed once before any dependency work.
    if human:
        for _ in range(5):
            result = reopened.tick().result
            if isinstance(result, Error):
                break
        assert isinstance(result, Error) and "intervening" in result.message
        assert path.read_bytes() == changed
    else:
        for _ in range(8):
            result = reopened.tick().result
            assert not isinstance(result, Error)
            if receipt(p, "pending-revision").get("assembled"):
                break
        assert receipt(p, "pending-revision")["assembled"]
        assert len(calls(reopened, r.REVISE_PROMPT)) == 1
        assert accepted in receipt(p, "runtime")["semantic_revisions"]
        approval = run(reopened)  # bounded to 150 ticks by production demo helper
        assert approval.target == reports(p)[-1].paper_ref
        assert reopened.ck.scratch["writing"]["figure_basis"] != old_basis
        assert len(calls(reopened, "writing/figure_spec")) == 4
        assert reports(p)[-1].counts["major"] == 0
        if regenerate:
            assert reopened.ck.scratch["writing"]["figures"] != old_figures
            assert reopened.ck.scratch["writing"]["claim_basis"] != old_claim_basis
            assert len(calls(reopened, "writing/claims")) == 2
            assert len(calls(reopened, "writing/section")) > 8
    assert len(calls(reopened, r.REVISE_PROMPT)) == 1


@pytest.mark.parametrize("limit", [1, 3])
def test_round_limit_rechecked_after_entering_revise(tmp_path, limit):
    p, e = prepare_sample(tmp_path / "budget", compile_enabled=False, evidence_check=True)
    e.llm.backend.semantic_revision = True
    p.config.paper["max_review_rounds"] = 2
    p.save_config(p.config)
    to_phase(e, "revise")
    first = (p.root / "paper/review/review_v1.json").read_bytes()
    p.config.paper["max_review_rounds"] = limit
    p.save_config(p.config)
    reopened = restart(p, e)
    approval = run(reopened)
    assert len(calls(reopened, r.REVISE_PROMPT)) == (0 if limit == 1 else 1)
    assert len(reports(p)) == (1 if limit == 1 else 2)
    assert (p.root / "paper/review/review_v1.json").read_bytes() == first
    if limit == 1:
        assert "RESIDUAL BLOCKER/MAJOR ISSUES" in approval.summary
        assert reports(p)[-1].counts["major"] == 1
    else:
        assert "Semantic review rounds: 2; automatic revision rounds: 1" in approval.summary


@pytest.mark.parametrize(
    "original,changed",
    [
        ("A was higher than B", "B was higher than A"),
        ("A [[VALUE_1]] exceeded B [[VALUE_2]]", "A [[VALUE_2]] exceeded B [[VALUE_1]]"),
        ("A, not B, improved", "B, not A, improved"),
        ("A had a positive relation and B a negative relation", "B had a positive relation and A a negative relation"),
        ("A was non-significant; B was significant", "B was non-significant; A was significant"),
        ("A scored [[VALUE_1]] versus B [[VALUE_2]]", "B scored [[VALUE_1]] versus A [[VALUE_2]]"),
    ],
)
def test_comparison_factual_core_cannot_be_rewritten(original, changed):
    c = claim()
    c.type = "comparative"
    c.comparison = Comparison(a={"method": "A"}, b={"method": "B"}, metric="score", asserted="a>b")
    c.values = []
    # Literal slot strings suffice to test the validator's existing counter boundary.
    c.text = "\\claim{CL1}{" + original + ". This establishes universal superiority.}"
    u = unit()
    u["sentence"] = r.inner(c)
    context, placeholders = r.revision_context(u, [], c)
    raw = {
        "sentence": context["sentence"],
        "claim_id": "CL1",
        "hedge": "possible",
        "replacement": changed + ". This observation is limited to the evaluated setting.",
    }
    with pytest.raises(ValueError):
        r.validate_revision(context, placeholders, raw, c)


def test_comparison_safe_scope_suffix_and_no_safe_span():
    c = claim()
    c.type = "comparative"
    core = "A had a negative association relative to B. "
    c.text = "\\claim{CL1}{" + core + "This establishes universal superiority.}"
    u = unit()
    u["sentence"] = r.inner(c)
    context, placeholders = r.revision_context(u, [], c)
    raw = {
        "sentence": context["sentence"],
        "claim_id": "CL1",
        "hedge": "possible",
        "replacement": core + "This observation is limited to the evaluated setting.",
    }
    text, revised = r.validate_revision(context, placeholders, raw, c)
    assert r.inner(revised).startswith(core) and text == revised.text
    c.text = "\\claim{CL1}{A was higher than B.}"
    u["sentence"] = r.inner(c)
    context, placeholders = r.revision_context(u, [], c)
    raw.update(sentence=context["sentence"], replacement="B was higher than A.")
    with pytest.raises(ValueError):
        r.validate_revision(context, placeholders, raw, c)


def test_comparison_without_recognized_shell_goes_to_human(tmp_path, monkeypatch):
    from .test_writing_semantic_stage import scenario

    p, e = prepare_sample(tmp_path / "no-shell", compile_enabled=False, evidence_check=True)
    scenario(monkeypatch, residual=True)
    approval = run(e)
    assert not calls(e, r.REVISE_PROMPT)
    assert len(reports(p)) == 1 and reports(p)[0].counts["major"] == 1
    assert "RESIDUAL BLOCKER/MAJOR ISSUES" in approval.summary


@pytest.mark.parametrize(
    "suffix",
    [
        "This establishes universal superiority.",
        "These results generalize to all settings.",
        "This establishes a causal mechanism.",
    ],
)
def test_comparison_shell_never_accepts_new_facts(suffix):
    c = claim()
    c.type = "comparative"
    c.text = "\\claim{CL1}{A exceeded B. " + suffix + "}"
    u = unit()
    u["sentence"] = r.inner(c)
    context, placeholders = r.revision_context(u, [], c)
    for replacement in ["A exceeded B. B exceeded A.", "A exceeded B. This observation is limited to B exceeding A."]:
        raw = {"sentence": context["sentence"], "claim_id": "CL1", "hedge": "possible", "replacement": replacement}
        with pytest.raises(ValueError, match="immutable comparison"):
            r.validate_revision(context, placeholders, raw, c)
    raw["replacement"] = "A exceeded B. These observations do not establish causality."
    assert r.inner(r.validate_revision(context, placeholders, raw, c)[1]) == raw["replacement"]
