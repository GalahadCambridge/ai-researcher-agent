"""Cartesian task expansion in the frozen PlanTask schema."""

from airesearcher.core.models.common import VersionRef
from airesearcher.stages.plan.expand import PlanDraft, expand_tasks, load_task, make_plan
from scripts.plan.fixtures import fixture_idea, preset_draft


def test_cartesian_variables_times_seeds_and_exact_fields():
    _, task, schema = load_task("tasks/smoke")
    draft = PlanDraft.model_validate(preset_draft(fixture_idea("tasks/smoke"), "tasks/smoke"))
    draft.experiments[0].variables = {"seconds": [0.01, 0.02]}
    draft.experiments[0].fixed.pop("seconds")
    ref = VersionRef(artifact_id="idea/selected.md", version=1, sha256="a" * 64)
    plan = make_plan(draft, ref, "tasks/smoke", task, 1)
    tasks = expand_tasks(plan, task, schema)
    assert len(tasks) == 12 and tasks[0].task_key == "E1-seconds=0.01-seed=0"
    assert set(tasks[0].model_dump()) == {
        "task_key",
        "experiment_id",
        "kind",
        "config",
        "eval_condition",
        "depends_on",
        "expected_metrics",
    }
    assert "replication_rule" not in plan.model_dump()
