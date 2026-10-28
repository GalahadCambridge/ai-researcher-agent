"""Multidimensional presentation and bounded proposal recovery, no providers."""

import json
import subprocess
import sys

import pytest

from airesearcher.core.models.aggregate import Aggregate
from airesearcher.core.models.figure import FigureSpec
from airesearcher.core.workspace import repo_root
from airesearcher.stages.writing import figures
from airesearcher.stages.writing import stage as module
from airesearcher.testing.fake_llm import FakeLLM
from scripts.writing.demo import SampleWritingLLM, context_of, prepare_sample

from .test_writing_stage import receipt, restart, to_phase


@pytest.fixture
def multidim():
    source = repo_root() / "skills/scientific-plotting/sample/input/aggregate.json"
    aggregate = Aggregate.model_validate_json(source.read_bytes())
    aggregate.comparison.update(metric="accuracy", group_by=["method", "learning_rate", "lora_rank"])
    aggregate.rows = [aggregate.rows[0].model_copy(update={
        "group": {"method": "lora", "learning_rate": rate, "lora_rank": rank},
        "metric": "accuracy", "n": 1, "values": [value], "mean": value,
        "min": value, "max": value, "std": None, "sem": None, "ci95": None,
        "run_ids": [f"synthetic-{i}"],
    }) for i, (rate, rank, value) in enumerate([
        (3e-5, 8, .842), (3e-5, 16, .839), (5e-5, 8, .842), (5e-5, 16, .846),
    ])]
    return aggregate


def proposal(aggregate, **changes):
    return FigureSpec(fig_id="fig_c1", aggregate_id=aggregate.aggregate_id, kind="bar",
                      x="method", hue="learning_rate", metric=aggregate.comparison["metric"],
                      error="none", title="Synthetic observations", xlabel="method",
                      ylabel=f"{aggregate.comparison['metric']} (dimensionless)",
                      caption="n=1 per group.").model_copy(update=changes)


def test_hidden_dimension_rejected_before_render(multidim):
    with pytest.raises(ValueError, match="lora_rank.*varies.*not represented"):
        figures.validate_proposal(multidim, proposal(multidim))


def test_original_plotting_safety_unchanged(multidim):
    with pytest.raises(ValueError, match="duplicate plotted group; refusing to pool hidden dimensions"):
        figures.load_renderer().select_rows(multidim, proposal(multidim))


def test_two_varying_dimensions_need_no_facet(multidim):
    spec = proposal(multidim, x="learning_rate", hue="lora_rank", xlabel="learning_rate")
    figures.validate_proposal(multidim, spec)
    assert len(figures.load_renderer().select_rows(multidim, spec)) == 4
    assert not figures.default_spec(multidim, ylabel="accuracy (dimensionless)").facets


@pytest.mark.parametrize("kind", ["bar", "line", "point", "box"])
def test_facets_render_all_groups_and_replay(tmp_path, multidim, kind):
    from airesearcher.core.archive import Archive
    from airesearcher.core.events import Events

    multidim.rows += [r.model_copy(update={"group": {**r.group, "method": "baseline"},
                                         "run_ids": ["baseline-" + r.run_ids[0]]}) for r in multidim.rows]
    spec = proposal(multidim, kind=kind, x="learning_rate", hue="method", facets=["lora_rank"],
                    xlabel="learning_rate")
    source = Archive(tmp_path, Events(tmp_path)).put(
        f"artifacts/aggregates/{multidim.aggregate_id}.json", "aggregate", multidim.model_dump_json())
    figure = figures.generate_figure(tmp_path, multidim.aggregate_id, spec)
    directory = tmp_path / "artifacts/figures/fig_c1"
    assert len(figure.run_ids) == len(set(figure.run_ids)) == len(figure.n_per_group) == 8
    assert set(figure.n_per_group.values()) == {1}
    assert figures.validate_artifact(directory, multidim, source) == []
    renderer = figures.load_renderer()
    drawn = renderer.draw(spec, renderer.select_rows(multidim, spec))
    try:
        assert len(drawn.axes) == 2
        assert all("lora_rank" in axis.get_title() for axis in drawn.axes)
        if kind == "bar":
            assert sum(len(axis.patches) for axis in drawn.axes) == 8
    finally:
        renderer.plt.close(drawn)
    if kind == "bar":
        subprocess.run([sys.executable, str(directory / "plot.py"), "--spec", str(directory / "spec.json"),
                        "--data", str(directory / "data.csv"), "--out", str(tmp_path / "replay")], check=True)
        assert (tmp_path / "replay/fig_c1.png").read_bytes() == (directory / "fig_c1.png").read_bytes()


@pytest.fixture
def project(tmp_path, multidim, monkeypatch):
    from airesearcher.services import runs

    p, e = prepare_sample(tmp_path / "project", compile_enabled=False, evidence_check=True)
    old = runs.load_aggregate(p.root, "C1")
    ids = [rid for row in old.rows for rid in row.run_ids]
    old.comparison["group_by"] = multidim.comparison["group_by"]
    old.rows = [r.model_copy(update={"metric": old.comparison["metric"], "run_ids": [ids[i]]})
                for i, r in enumerate(multidim.rows)]
    old.contrasts = []
    info = p.archive.latest_version("artifacts/aggregates/C1.json")
    p.archive.put(info.ref.artifact_id, "aggregate", old.model_dump_json(), parents=info.parents)
    to_phase(e, "figures")
    original = SampleWritingLLM.call
    seen, replies = [], []

    def scripted(self, request):
        if request.prompt_id == module.FIGURE_PROMPT and context_of(request)["fig_id"] == "fig_c1":
            context = context_of(request)
            seen.append(context)
            spec = proposal(Aggregate.model_validate(context["aggregate"]))
            spec.fig_id = context["fig_id"]
            update = replies[min(len(seen) - 1, len(replies) - 1)] if replies else {}
            self.default = update if isinstance(update, str) else spec.model_copy(update=update).model_dump()
            return FakeLLM.call(self, request)
        return original(self, request)

    monkeypatch.setattr(SampleWritingLLM, "call", scripted)
    return p, e, seen, replies


def journals(p):
    paths = list((p.root / "paper/.writing").glob("figure-fig_c1-*-attempts.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text(encoding="utf-8"))["attempts"]


def test_invalid_then_valid_is_durable_and_bounded(project):
    from airesearcher.engine.stage import Continue

    p, e, seen, replies = project
    replies.extend([{}, {"x": "learning_rate", "hue": "lora_rank", "xlabel": "learning_rate"}])
    assert isinstance(e.tick().result, Continue)
    assert not (p.root / "artifacts/figures/fig_c1").exists()
    assert journals(p)[0]["status"] == "rejected"
    e = restart(p, e)
    to_phase(e, "template")
    records = journals(p)
    assert [a["status"] for a in records] == ["rejected", "accepted"]
    assert len(seen) == 2 and "lora_rank" in seen[1]["rejection_reason"]
    for attempt in records:
        assert attempt["raw"] and attempt["response"] and attempt["call_id"]
        assert attempt["prompt_id"] == module.FIGURE_PROMPT and attempt["prompt_version"] == 1
        assert attempt["aggregate_ref"]["artifact_id"] == "artifacts/aggregates/C1.json"
    assert len(json.loads((p.root / "artifacts/figures/fig_c1/figure.json").read_bytes())["run_ids"]) == 4


def test_exhaustion_and_explicit_retry_survive_restart(project):
    from airesearcher.core.models.common import ProjectState as S
    from airesearcher.engine.stage import Continue, Stop

    p, e, seen, _ = project
    assert isinstance(e.tick().result, Continue)
    e = restart(p, e)
    assert isinstance(e.tick().result, Continue)
    assert isinstance(e.tick().result, Stop)
    q = p.questions.pending()
    assert {o.id for o in q.options} == {"retry", "end"}
    assert len(seen) == 2 and e.state == S.Paused
    e = restart(p, e)
    e.tick()
    assert len(seen) == 2 and len(journals(p)) == 2
    p.questions.answer(q.question_id, "retry", request_id=q.question_id)
    e = restart(p, e)
    assert isinstance(e.tick().result, Continue)
    assert isinstance(e.tick().result, Stop)
    assert len(seen) == 3 and len(journals(p)) == 3 and e.state == S.Paused


def test_malformed_reply_is_retained_without_hidden_schema_retries(project):
    from airesearcher.engine.stage import Continue, Stop

    p, e, seen, replies = project
    replies.append("not-json {")
    assert isinstance(e.tick().result, Continue)
    e = restart(p, e)
    assert isinstance(e.tick().result, Continue)
    assert isinstance(e.tick().result, Stop)
    assert len(seen) == 2
    for record in journals(p):
        assert record["status"] == "rejected" and record["raw"] == "not-json {"
        assert record["prompt_id"] == module.FIGURE_PROMPT and record["prompt_version"] == 1
        assert record["call_id"] and record["aggregate_ref"]["artifact_id"] == "artifacts/aggregates/C1.json"


def install_legacy_cache(p, e, *, previous_renderer=False):
    from airesearcher.core.fsutil import atomic_write_json
    from airesearcher.stages.writing.bundle import EvidenceBundle

    ctx = e.build_context()
    bundle = EvidenceBundle.model_validate_json(ctx.archive.get(module._ref(ctx.scratch["writing"]["bundle_ref"])))
    selected, _, runtime = module.WritingStage()._inputs(ctx)
    key = module.WritingStage()._figure_keys(ctx, selected, runtime)["fig_c1"]
    if previous_renderer:
        key = "previous-renderer-key"
    spec = proposal(bundle.aggregates[0].data).model_dump()
    raw = json.dumps(spec)
    atomic_write_json(p.root / "paper/.writing/figure-fig_c1.json", {
        "key": key, "spec": spec, "original_proposal": spec, "original_response": raw,
        "call_id": "legacy-paid-call", "prompt_id": module.FIGURE_PROMPT, "prompt_version": 1,
    })
    return raw, spec


@pytest.mark.parametrize("previous_renderer", [False, True])
def test_legacy_invalid_cache_is_a_consumed_attempt(project, previous_renderer):
    from airesearcher.engine.stage import Continue

    p, e, seen, replies = project
    replies.append({"x": "learning_rate", "hue": "lora_rank", "xlabel": "learning_rate"})
    raw, spec = install_legacy_cache(p, e, previous_renderer=previous_renderer)
    e = restart(p, e)
    assert isinstance(e.tick().result, Continue)
    assert not seen and receipt(p, "figure-fig_c1")["status"] == "rejected"
    e = restart(p, e)
    to_phase(e, "template")
    assert len(seen) == 1
    records = journals(p)
    assert [a["status"] for a in records] == ["rejected", "accepted"]
    assert records[0]["raw"] == raw and records[0]["call_id"] == "legacy-paid-call"
    assert records[0]["legacy_receipt"]["original_proposal"] == spec


def test_returned_response_crash_does_not_buy_another_attempt(project, monkeypatch):
    from airesearcher.engine.stage import Continue, Error

    p, e, seen, replies = project
    replies.extend([{}, {"x": "learning_rate", "hue": "lora_rank", "xlabel": "learning_rate"}])
    save = module._save
    fired = False

    def crash(ctx, name, value):
        nonlocal fired
        save(ctx, name, value)
        if not fired and name.endswith("-attempts") and value["attempts"][-1]["status"] == "returned":
            fired = True
            raise OSError("after durable response")

    monkeypatch.setattr(module, "_save", crash)
    assert isinstance(e.tick().result, Error)
    monkeypatch.setattr(module, "_save", save)
    e.request_action("resume")
    e = restart(p, e)
    assert isinstance(e.tick().result, Continue)
    # Validation of the durable first reply is free; the one new request is the
    # distinct correction, not a replay of that paid call.
    assert len(seen) == 2 and [a["status"] for a in journals(p)] == ["rejected", "accepted"]
    assert "lora_rank" in seen[1]["rejection_reason"]
    to_phase(e, "template")
    assert len(seen) == 2


def test_corrupt_source_fails_closed_before_proposal(project):
    from airesearcher.engine.stage import Error
    from airesearcher.services import runs

    p, e, seen, _ = project
    source = runs.load_aggregate(p.root, "C1")
    source.rows[0].n = 2  # Provenance is genuinely inconsistent, not a bad projection.
    info = p.archive.latest_version("artifacts/aggregates/C1.json")
    p.archive.put(info.ref.artifact_id, "aggregate", source.model_dump_json(), parents=info.parents)
    for _ in range(3):
        result = e.tick().result
        if isinstance(result, Error):
            break
    assert isinstance(result, Error) and "provenance" in result.message
    assert not seen and not p.questions.pending()


def test_facet_bounds_fail_before_render(multidim):
    multidim.rows = [multidim.rows[0].model_copy(update={
        "group": {"method": "lora", "learning_rate": 3e-5, "lora_rank": rank},
        "run_ids": [f"synthetic-{rank}"],
    }) for rank in range(17)]
    with pytest.raises(ValueError, match="16 panels"):
        figures.validate_proposal(multidim, proposal(multidim, facets=["lora_rank"]))


def test_repaired_proposal_unit_clarification_has_no_extra_model_call(project):
    from airesearcher.engine.stage import Continue, Stop

    p, e, seen, replies = project
    replies.extend([{}, {"x": "learning_rate", "hue": "lora_rank", "xlabel": "learning_rate", "ylabel": ""}])
    assert isinstance(e.tick().result, Continue)
    assert isinstance(e.tick().result, Stop)
    q = p.questions.pending()
    assert q.allow_text and not q.options and len(seen) == 2
    p.questions.answer(q.question_id, None, text="score (dimensionless)", request_id=q.question_id)
    e = restart(p, e)
    to_phase(e, "template")
    assert len(seen) == 2
    assert [a["status"] for a in journals(p)] == ["rejected", "accepted"]
    assert receipt(p, "figure-fig_c1")["status"] == "accepted"


@pytest.mark.parametrize("recoverable", [True, False, "misleading_text"])
def test_late_prepare_error_classification(project, monkeypatch, recoverable):
    from airesearcher.core.figure_errors import PresentationError
    from airesearcher.engine.stage import Continue, Error

    p, e, seen, replies = project
    replies.append({"x": "learning_rate", "hue": "lora_rank", "xlabel": "learning_rate"})
    original = module.prepare_data
    fired = False

    def late_error(*args):
        nonlocal fired
        if not fired:
            fired = True
            if recoverable is True:
                raise PresentationError("Updated presentation diagnostic, independent of wording")
            raise ValueError("duplicate plotted group; refusing to pool hidden dimensions"
                             if recoverable == "misleading_text" else "source integrity check failed")
        return original(*args)

    monkeypatch.setattr(module, "prepare_data", late_error)
    result = e.tick().result
    if recoverable is True:
        assert isinstance(result, Continue) and journals(p)[0]["status"] == "rejected"
        e = restart(p, e)
        to_phase(e, "template")
        assert len(seen) == 2 and [a["status"] for a in journals(p)] == ["rejected", "accepted"]
    else:
        assert isinstance(result, Error)
        assert ("duplicate plotted group" if recoverable == "misleading_text" else "source integrity") in result.message
        assert len(seen) == 1 and not p.questions.pending()


def test_legacy_rejection_crash_is_not_a_new_attempt(project, monkeypatch):
    from airesearcher.engine.stage import Continue, Error
    from airesearcher.stages.writing import figure_planning

    p, e, seen, replies = project
    replies.append({"x": "learning_rate", "hue": "lora_rank", "xlabel": "learning_rate"})
    install_legacy_cache(p, e)
    original = figure_planning.save_attempts
    fired = False

    def crash(*args):
        nonlocal fired
        original(*args)
        if not fired:
            fired = True
            raise OSError("after rejection persisted")

    monkeypatch.setattr(figure_planning, "save_attempts", crash)
    assert isinstance(e.tick().result, Error)
    monkeypatch.setattr(figure_planning, "save_attempts", original)
    e.request_action("resume")
    e = restart(p, e)
    assert isinstance(e.tick().result, Continue)
    assert not seen and len(journals(p)) == 1
    to_phase(e, "template")
    assert len(seen) == 1 and [a["status"] for a in journals(p)] == ["rejected", "accepted"]


def test_error_type_survives_copied_renderer(tmp_path, multidim):
    from airesearcher.core.figure_errors import PresentationError, UnitLabelError

    source = repo_root() / "skills/scientific-plotting/scripts/plot.py"
    copied = tmp_path / "plot.py"
    copied.write_bytes(source.read_bytes())
    renderer = figures.load_renderer(copied)
    assert renderer.PresentationError is figures.load_renderer().PresentationError is PresentationError
    assert figures.presentation_error(renderer.PresentationError("Different presentation wording"))
    assert not figures.presentation_error(ValueError("caption must define standard deviation error bars"))
    with pytest.raises(PresentationError, match="duplicate plotted group"):
        renderer.select_rows(multidim, proposal(multidim))
    with pytest.raises(UnitLabelError):
        renderer.select_rows(multidim, proposal(multidim, ylabel="unknown"))
    bad_source = multidim.model_copy(deep=True)
    bad_source.rows[0].n = 2
    with pytest.raises(ValueError) as failure:
        renderer.select_rows(bad_source, proposal(bad_source, x="learning_rate", hue="lora_rank"))
    assert not isinstance(failure.value, PresentationError)
