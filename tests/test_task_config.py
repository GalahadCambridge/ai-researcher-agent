"""A non-NLP task changes methods/resources/trial sizes through task.yaml only."""

import json
import socket

import pytest
import yaml

from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.models.frontmatter import render_front_matter
from airesearcher.core.project import Project
from airesearcher.engine.engine import ProjectEngine
from airesearcher.llm.gateway import LiteLLMBackend
from airesearcher.runtime.local import LocalExecutor
from airesearcher.runtime.task import uses_gpu
from airesearcher.services.runs import list_runs
from airesearcher.stages.plan.expand import canonical_method, load_task
from airesearcher.testing.fake_executor import FakeExecutor
from airesearcher.testing.fake_llm import fake_gateway
from scripts.experiment.demo import analysis_reply
from scripts.plan.demo import FixtureIdeaStage
from scripts.plan.fixtures import fixture_idea, offline_models, preset_draft

from .helpers import approve_pending, drive


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Task portability acceptance must stay offline and simulated")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(LiteLLMBackend, "call", forbidden)
    monkeypatch.setattr(LocalExecutor, "submit", forbidden)


def test_queue_scheduling_task_uses_declared_methods_resources_and_trial_overrides(air_home, tmp_path):
    _, task, _ = load_task("tasks/smoke")
    directory = tmp_path / "queue_scheduling"
    directory.mkdir()
    definition = task.model_dump()
    definition.update(
        name="queue_scheduling", domain="systems", description="Queue scheduling offline fixture",
        methods_available=["fifo", "shortest_job", "random_order"],
        method_aliases={"baseline": "fifo", "main": "shortest_job", "ablation": "random_order"},
        method_resources={"fifo": {"gpu": False}, "shortest_job": {"gpu": False}, "random_order": {"gpu": False}},
        trial_overrides={"work_units": 2},
        config_schema=str(directory / "config_schema.json"),
    )
    (directory / "config_schema.json").write_text(json.dumps({
        "type": "object", "required": ["method", "seed", "work_units"], "additionalProperties": False,
        "properties": {
            "method": {"enum": ["fifo", "shortest_job", "random_order"]},
            "seed": {"type": "integer"}, "work_units": {"type": "integer", "minimum": 1},
            "seconds": {"type": "number"}, "eval_split": {"enum": ["test"]},
            "fail_mode": {"enum": ["none"]}, "device": {"enum": ["cuda"]},
        },
    }), encoding="utf-8")
    (directory / "task.yaml").write_text(yaml.safe_dump(definition), encoding="utf-8")
    _, custom, _ = load_task(str(directory))
    assert canonical_method("  MAIN  ", custom) == "shortest_job"
    assert not uses_gpu({"method": "fifo", "device": "cuda"}, custom)
    assert uses_gpu({"method": "new_gpu_method", "device": "cuda"}, custom)

    idea = fixture_idea("tasks/smoke")
    draft = preset_draft(idea, "tasks/smoke")
    idea = idea.model_copy(update={"task": {"domain": "systems", "task_config": str(directory)}})
    for experiment in draft["experiments"]:
        experiment["method"] = canonical_method(experiment["method"], custom)
        experiment["fixed"]["work_units"] = 100
        experiment["fixed"]["device"] = "cuda"  # CPU-only method metadata wins without name-based exceptions.
    project = Project.create(
        goal=idea.research_question, task=str(directory), home=air_home, register=False, models=offline_models(),
        dev={"stage_impl": {"plan": "real", "experiment": "real"}},
    )
    ref = project.archive.put(
        "idea/selected.md", "idea", render_front_matter(idea.model_dump(mode="json"), "# Queue fixture\n"),
        producer="system",
    )
    gateway = fake_gateway(project, {"plan/draft": [draft], "experiment/analyze": [analysis_reply()]})
    executor = FakeExecutor(project)
    engine = ProjectEngine(
        project, llm=gateway, executor=executor, retry_delay=0, stages={"idea": FixtureIdeaStage(ref)},
    )
    drive(engine, S.IdeaPending)
    approve_pending(project)
    drive(engine, S.PlanPending)
    approve_pending(project)
    drive(engine, S.LogPending)
    views = list_runs(project.root)
    assert len(views) == 12
    methods = set()
    for view in views:
        config = json.loads((project.root / "runs" / view.record.run_id / "config.yaml").read_text(encoding="utf-8"))
        methods.add(config["method"])
        assert config["work_units"] == (2 if view.record.kind == "trial" else 100)
        assert "train_size" not in config and "epochs" not in config
        environment = json.loads((project.root / "runs" / view.record.run_id / "environment.json").read_text(
            encoding="utf-8"
        ))
        assert environment["gpu_requested"] is False
    assert methods == {"fifo", "shortest_job", "random_order"}
    approve_pending(project)
    drive(engine, S.Writing)
    assert project.budget.used().get("llm_usd", 0) == 0
