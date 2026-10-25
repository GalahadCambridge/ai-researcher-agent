"""Exploration must never leak into final evaluation."""

from copy import deepcopy

from airesearcher.core.models.common import ProjectState as S
from scripts.plan.fixtures import fixture_idea, preset_draft

from .test_plan_stage import checks, seeded, start_plan


def test_final_split_tuning_is_repaired_before_approval(air_home):
    good = preset_draft(fixture_idea("tasks/smoke"), "tasks/smoke")
    bad = deepcopy(good)
    bad["experiments"][0]["eval_condition"] = "explore"
    project, engine, _, _ = seeded(air_home, [bad, good])
    start_plan(engine, project)
    assert engine.state == S.PlanPending and checks(project)["leakage_split"]["status"] == "pass"
    assert len(engine.llm.backend.calls) == 2
