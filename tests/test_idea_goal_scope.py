"""Regression checks for silent study substitution and repair limits; no paid model calls."""

import pytest

from airesearcher.engine.stage import Stop
from airesearcher.stages.idea.goal_scope import comparison_targets, validate_goal_scope
from airesearcher.stages.idea.selection import normalize_idea, refine_step, task_config

from .test_idea_selection import draft, ready


@pytest.mark.parametrize("goal,expected", [
    ("Compare LoRA and full fine-tuning for low-resource text classification", ["LoRA", "full fine-tuning"]),
    ("Compare BM25 with dense retrieval on scientific papers", ["BM25", "dense retrieval"]),
    ("Adam versus SGD for image classification", ["Adam", "SGD"]),
    ("Explore parameter-efficient adaptation", []),
])
def test_explicit_comparison_extraction(goal, expected):
    assert comparison_targets(goal) == expected


def test_optimizer_substitution_is_rejected_even_if_goal_is_in_open_questions(air_home):
    project, _, ctx, _ = ready(air_home)
    project.config.goal = "Compare LoRA and full fine-tuning for low-resource text classification"
    idea = draft(ctx)
    idea.research_question = "Does LoRA-RITE outperform Adam?"
    idea.open_questions.append(project.config.goal)
    idea.baselines[0].name, idea.methods[0].name = "LoRA with Adam", "LoRA-RITE"
    with pytest.raises(ValueError, match="GOAL_SCOPE_MISMATCH"):
        normalize_idea(ctx, idea, task_config(ctx))


def test_registered_task_does_not_override_requested_methods(air_home):
    project, _, ctx, _ = ready(air_home)
    project.config.goal = "Compare LoRA and full fine-tuning for text classification"
    idea = draft(ctx)
    idea.methods[0].name = "low-rank adaptation"
    idea.baselines[0].name = "Full parameter finetuning"
    normalized = normalize_idea(ctx, idea, task_config(ctx))
    assert validate_goal_scope(project.config.goal, normalized)["status"] == "passed"
    assert any("Task extension required" in q for q in normalized.open_questions)


def test_question_only_alignment_cannot_hide_changed_hypotheses(air_home):
    project, _, ctx, _ = ready(air_home)
    project.config.goal = "Compare BM25 and dense retrieval on scientific papers"
    idea = draft(ctx)
    idea.hypotheses[0].statement = "A different optimizer improves accuracy."
    with pytest.raises(ValueError, match="GOAL_SCOPE_MISMATCH"):
        validate_goal_scope(project.config.goal, idea)


def test_two_bad_refinement_replies_stop_without_publishing(air_home):
    project, engine, ctx, _ = ready(air_home)
    project.config.goal = "Compare LoRA and full fine-tuning for text classification"
    bad = draft(ctx).model_dump(mode="json")
    bad["research_question"] = "Does LoRA-RITE outperform Adam?"
    reply = {"idea": bad, "task_fit": "unknown", "task_fit_reason": "Workflow fixture."}
    engine.llm.backend.script["idea/refine"] = [reply]
    engine.llm.backend._fixed_prompts.add("idea/refine")
    result = refine_step(ctx, engine.ck.scratch["idea"])
    assert isinstance(result, Stop) and result.question is not None
    assert sum(r.prompt_id == "idea/refine" for r in engine.llm.backend.calls) == 2
    assert not project.archive.latest("idea/selected.json")
