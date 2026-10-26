"""Document-prescribed model inputs, checked without an API or training."""

import json
import socket

import pytest

from airesearcher.core.models.common import ProjectState as S
from airesearcher.llm.gateway import LiteLLMBackend
from airesearcher.stages.experiment.coder import run_coder
from airesearcher.stages.experiment.stage import load_approved

from .helpers import drive
from .test_experiment_stage import prepared


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    def forbidden(*args, **kwargs):
        raise AssertionError("Context tests cannot access a network or a paid model")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(LiteLLMBackend, "call", forbidden)


def inputs(request, source):
    user = next(m["content"] for m in request.messages if m["role"] == "user")
    document = user.split(f'<untrusted_document source="{source}">\n', 1)[1]
    return json.loads(document.rsplit("\n</untrusted_document>", 1)[0])


def test_analysis_receives_the_canonical_markdown_tables(air_home):
    project, engine, executor = prepared(air_home)
    drive(engine, S.LogPending)
    [request] = [c for c in engine.llm.backend.calls if c.role == "exp.analyst"]
    context = inputs(request, "analysis_inputs")
    assert request.prompt_version >= 3
    assert len(context["aggregate_tables"]) == len(context["aggregates"])
    for table, aggregate in zip(context["aggregate_tables"], context["aggregates"], strict=True):
        assert table["artifact_id"] == f"artifacts/aggregates/{aggregate['aggregate_id']}.md"
        assert table["markdown"] == (project.root / table["artifact_id"]).read_text(encoding="utf-8")
        assert "| n | mean |" in table["markdown"]
    assert project.budget.used().get("llm_usd", 0) == 0 and executor.submitted


@pytest.mark.parametrize("separate_method", [False, True])
def test_coder_preloads_the_target_method_or_reports_no_module(air_home, separate_method):
    project, engine, _ = prepared(air_home)
    engine.tick()
    ctx = engine.build_context()
    approved = load_approved(ctx)
    experiment = approved.plan.experiments[0]
    path = project.root / "src" / "methods" / f"{experiment.method}.py"
    content = '"""Target method fixture."""\nVALUE = 1\n'
    if separate_method:
        path.parent.mkdir(exist_ok=True)
        path.write_text(content, encoding="utf-8")
    result = run_coder(ctx, approved, experiment, ctx.scratch["experiment"]["entries"],
                       lambda _: pytest.fail("No training required by this context test"))
    assert result["ok"]
    [request] = [c for c in engine.llm.backend.calls if c.role == "exp.coder"]
    context = inputs(request, "coder_inputs")
    assert request.prompt_version >= 3
    assert context["entrypoint_source"]
    expected = {"path": path.relative_to(project.root).as_posix(), "content": content.rstrip("\n")}
    assert context["method_source"] == (expected if separate_method else None)
    expected_keys = [key for key, entry in ctx.scratch["experiment"]["entries"].items()
                     if entry["experiment_id"] == experiment.id and entry["trial"]]
    assert context["trial_task_keys"] == expected_keys
    assert project.budget.used().get("llm_usd", 0) == 0


def test_method_preloading_does_not_follow_a_link_outside_the_workspace(air_home, tmp_path):
    project, engine, _ = prepared(air_home)
    engine.tick()
    ctx = engine.build_context()
    approved = load_approved(ctx)
    experiment = approved.plan.experiments[0]
    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 7\n", encoding="utf-8")
    target = project.root / "src/methods" / f"{experiment.method}.py"
    target.parent.mkdir(exist_ok=True)
    try:
        target.symlink_to(outside)
    except OSError:
        pytest.skip("This platform cannot create the required test symlink")
    with pytest.raises(ValueError, match="outside permitted"):
        run_coder(ctx, approved, experiment, ctx.scratch["experiment"]["entries"],
                  lambda _: pytest.fail("No training allowed"))
    assert not engine.llm.backend.calls


def test_installed_sdk_can_initialize_function_tools_without_network():
    litellm = pytest.importorskip("litellm")
    reply = litellm.completion(
        model="openrouter/google/gemini-2.5-pro",
        api_key="offline-placeholder",
        messages=[{"role": "user", "content": "Offline dependency check"}],
        tools=[{"type": "function", "function": {"name": "finish", "description": "Finish",
                "parameters": {"type": "object", "properties": {}}}}],
        mock_response="offline dependency check passed",
    )
    assert reply.choices[0].message.content == "offline dependency check passed"
