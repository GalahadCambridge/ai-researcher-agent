"""计划阶段离线测试：网络/真实模型调用一律失败，不运行训练。"""

from __future__ import annotations

import json
import os
import socket
from copy import deepcopy

import pytest

from airesearcher.core.fsutil import dumps, sha256_file
from airesearcher.core.models.common import ProjectState as S
from airesearcher.core.models.frontmatter import render_front_matter
from airesearcher.core.models.plan import ExperimentPlan, PlanTask, parse_plan_md
from airesearcher.core.project import Project
from airesearcher.engine.engine import ProjectEngine
from airesearcher.engine.stage import Continue, Error, NeedsApproval
from airesearcher.llm.gateway import LiteLLMBackend, LLMGateway
from airesearcher.stages.plan.expand import PlanDraft, canonical_method, expand_tasks, load_task, make_plan
from airesearcher.stages.plan.precheck import inspect_inputs, item, planning_budget_context
from airesearcher.stages.plan.stage import PLAN, PRECHECK, PROMPT, TASKS, PlanStage
from airesearcher.testing.fake_executor import FakeExecutor
from airesearcher.testing.fake_llm import fake_gateway
from scripts.plan.demo import FixtureIdeaStage, imported_idea
from scripts.plan.fixtures import fixture_idea, leaking_draft, offline_models, preset_draft

from .helpers import approve_pending, drive


@pytest.fixture(autouse=True)
def no_network_or_real_llm(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("计划测试不允许网络或真实 LLM 后端调用")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(LiteLLMBackend, "call", forbidden)


def seeded(air_home, script=None, *, bad_json_once=False, budget=None):
    task = "tasks/smoke"
    idea = fixture_idea(task)
    project = Project.create(
        goal=idea.research_question,
        task=task,
        home=air_home,
        register=False,
        models=offline_models(),
        budget=budget,
        dev={"stage_impl": {"plan": "real"}},
    )
    ref = project.archive.put(
        "idea/selected.md",
        "idea",
        render_front_matter(idea.model_dump(mode="json"), "# 本地测试选题\n"),
        producer="system",
    )
    gateway = fake_gateway(project, {PROMPT: script or [preset_draft(idea, task)]}, bad_json_once=bad_json_once)
    executor = FakeExecutor(project)
    engine = ProjectEngine(
        project, llm=gateway, executor=executor, retry_delay=0, stages={"idea": FixtureIdeaStage(ref)}
    )
    return project, engine, idea, ref


def start_plan(engine, project):
    drive(engine, S.IdeaPending)
    approve_pending(project)
    drive(engine, S.PlanPending)


def checks(project):
    return {c["check"]: c for c in json.loads((project.root / PRECHECK).read_text(encoding="utf-8"))}


def test_factory_handoff_and_zero_cost(air_home):
    project, engine, _, idea_ref = seeded(air_home)
    start_plan(engine, project)
    plan = parse_plan_md((project.root / PLAN).read_text(encoding="utf-8"))
    tasks = [PlanTask.model_validate(t) for t in json.loads((project.root / TASKS).read_text(encoding="utf-8"))]
    assert plan.idea_ref == idea_ref == engine.ck.approved["idea"]
    assert len(tasks) == 9 and len({t.task_key for t in tasks}) == 9
    assert not any(c["status"] == "fail" for c in checks(project).values())
    [approval] = project.approvals.pending()
    assert approval.kind == "plan" and len(approval.extra_refs) == 2
    assert isinstance(engine._stage("plan"), PlanStage)
    assert len(engine.llm.backend.calls) == 1
    assert project.budget.used().get("llm_usd", 0) == 0
    assert engine.executor.submitted == [] and not list((project.root / "runs").glob("*/run.json"))


def test_real_backend_is_rejected_before_any_call(air_home, monkeypatch):
    project, engine, _, _ = seeded(air_home)
    ctx = engine.build_context()
    ctx.llm = LLMGateway(project)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-placeholder-never-use")
    result = PlanStage().step(ctx)
    assert isinstance(result, Error) and not result.retryable
    assert "FakeLLM" in result.message and not (project.root / ".llm/calls.jsonl").exists()


def test_bad_json_retries_only_fake_backend(air_home):
    project, engine, _, _ = seeded(air_home, bad_json_once=True)
    start_plan(engine, project)
    assert len(engine.llm.backend.calls) == 2
    assert project.budget.used().get("llm_usd", 0) == 0


def test_failed_precheck_is_repaired_with_second_preset(air_home):
    idea = fixture_idea("tasks/smoke")
    good = preset_draft(idea, "tasks/smoke")
    project, engine, _, _ = seeded(air_home, [leaking_draft(good), good])
    start_plan(engine, project)
    assert len(engine.llm.backend.calls) == 2
    assert checks(project)["leakage_split"]["status"] == "pass"
    assert len(project.archive.versions(PLAN)) == 2  # 失败草稿也保留


def test_failed_precheck_stops_after_two_revisions(air_home):
    idea = fixture_idea("tasks/smoke")
    bad = leaking_draft(preset_draft(idea, "tasks/smoke"))
    project, engine, _, _ = seeded(air_home, [bad])
    drive(engine, S.IdeaPending)
    approve_pending(project)
    drive(engine, S.Paused)
    assert len(engine.llm.backend.calls) == 3
    assert not project.approvals.pending()
    question = project.questions.pending()
    assert question and not any(o.default for o in question.options)
    assert {o.id for o in question.options} == {"submit_anyway", "end"}
    assert checks(project)["leakage_split"]["status"] == "fail"
    assert not engine.executor.submitted


def test_explicit_risk_acceptance_only_submits_approval(air_home):
    bad = leaking_draft(preset_draft(fixture_idea("tasks/smoke"), "tasks/smoke"))
    project, engine, _, _ = seeded(air_home, [bad])
    drive(engine, S.IdeaPending)
    approve_pending(project)
    drive(engine, S.Paused)
    q = project.questions.pending()
    project.questions.answer(q.question_id, "submit_anyway", "", request_id="accept-risk")
    drive(engine, S.PlanPending)
    assert "fail" in project.approvals.pending()[0].summary
    assert checks(project)["leakage_split"]["status"] == "fail"
    assert not engine.executor.submitted and len(engine.llm.backend.calls) == 3


def test_plan_changes_requested_creates_revision(air_home):
    idea = fixture_idea("tasks/smoke")
    first = preset_draft(idea, "tasks/smoke")
    second = deepcopy(first)
    second["experiments"][0]["purpose"] = "预设修订：更明确的比较说明"
    project, engine, _, _ = seeded(air_home, [first, second])
    start_plan(engine, project)
    previous = project.archive.latest(PLAN)
    approve_pending(project, "changes_requested", "请明确比较说明")
    drive(engine, S.PlanPending)
    current = project.archive.latest(PLAN)
    assert current.version == previous.version + 1
    assert "预设修订" in project.archive.read_text(current)
    assert previous in project.archive.latest_version(PLAN).parents
    assert len(engine.llm.backend.calls) == 2


def test_resume_between_draft_and_check_does_not_regenerate(air_home):
    project, engine, _, _ = seeded(air_home)
    drive(engine, S.IdeaPending)
    approve_pending(project)
    engine.tick()
    assert engine.ck.state == S.PlanDrafting and engine.ck.scratch["plan"]["phase"] == "check"
    gateway = fake_gateway(project)  # 没有预设回复；如果意外重新起草就会失败
    resumed = ProjectEngine(project, llm=gateway, executor=FakeExecutor(project), retry_delay=0)
    drive(resumed, S.PlanPending)
    assert not gateway.backend.calls


def test_import_uses_approved_version_not_newer_unapproved_file(air_home):
    project, engine, idea, approved = seeded(air_home)
    start_plan(engine, project)
    newer = idea.model_copy(update={"title": "尚未批准的新草稿"})
    project.archive.put("idea/selected.md", "idea", render_front_matter(newer.model_dump(mode="json"), "# v2\n"))
    imported, text, task, provenance = imported_idea(project.root)
    assert imported.title == idea.title and "尚未批准的新草稿" not in text
    assert provenance["source_ref"] == approved.model_dump() and task == project.config.task


def test_unapproved_source_is_rejected(air_home):
    project, _, _, _ = seeded(air_home)
    with pytest.raises(ValueError, match="已批准"):
        imported_idea(project.root)


def test_changed_approved_idea_regenerates_and_preserves_lineage(air_home):
    project, engine, idea, _ = seeded(air_home)
    start_plan(engine, project)
    previous = project.archive.latest(PLAN)
    updated = idea.model_copy(update={"title": "新的批准选题版本"})
    ref = project.archive.put(
        "idea/selected.md", "idea", render_front_matter(updated.model_dump(mode="json"), "# v2\n")
    )
    ctx = engine.build_context()
    ctx.approved["idea"] = ref  # 模拟主干交付的新批准指针
    result = None
    for _ in range(4):
        result = PlanStage().step(ctx)
        if isinstance(result, NeedsApproval):
            break
        assert isinstance(result, Continue)
    assert isinstance(result, NeedsApproval)
    plan = parse_plan_md(project.archive.read_text(result.target))
    assert plan.idea_ref == ref and result.target.version > previous.version
    assert previous in project.archive.latest_version(PLAN).parents


@pytest.mark.parametrize(
    "case,check_name", [("metric", "metric"), ("fairness", "fairness"), ("coverage", "coverage"), ("budget", "budget")]
)
def test_prechecks_catch_invalid_plans(air_home, case, check_name):
    idea = fixture_idea("tasks/smoke")
    draft = preset_draft(idea, "tasks/smoke")
    if case == "metric":
        draft["comparisons"][0]["metric"] = "invented_metric"
    elif case == "fairness":
        draft["experiments"][0]["variables"] = {"seconds": [1, 2]}
        draft["experiments"][0]["fixed"].pop("seconds")
    elif case == "coverage":
        for e in draft["experiments"]:
            e["hypotheses"] = []
    elif case == "budget":
        for e in draft["experiments"]:
            e["est_minutes_per_run"] = 10000
    project, engine, _, _ = seeded(air_home, [draft])
    drive(engine, S.IdeaPending)
    approve_pending(project)
    drive(engine, S.Paused)
    assert checks(project)[check_name]["status"] == "fail"
    assert len(engine.llm.backend.calls) == 3


def test_method_aliases_are_explicit_and_unknown_methods_fail():
    _, task, _ = load_task("tasks/text_cls_lowres")
    assert canonical_method("Full fine-tuning", task) == "full_ft"
    assert canonical_method("TF-IDF + LR", task) == "tfidf_lr"
    assert canonical_method("LoRA", task) == "lora"
    assert canonical_method("Low-Rank Adaptation (LoRA)", task) == "lora"
    assert canonical_method("LoRA (Low-Rank Adaptation)", task) == "lora"
    with pytest.raises(ValueError, match="unsupported by this template"):
        canonical_method("LoRA-RITE", task)


@pytest.mark.parametrize("case", ["empty_axis", "duplicate_seed", "too_many", "reserved_key"])
def test_invalid_or_oversized_matrix_is_rejected(air_home, case):
    project, _, idea, ref = seeded(air_home)
    _, task, schema = load_task(project.config.task)
    draft = PlanDraft.model_validate(preset_draft(idea, project.config.task))
    e = draft.experiments[0]
    if case == "empty_axis":
        e.variables = {"size": []}
    elif case == "duplicate_seed":
        e.seeds = [0, 0]
    elif case == "too_many":
        e.variables = {"size": list(range(300))}
    else:
        e.fixed["seed"] = 0
    plan = make_plan(draft, ref, project.config.task, task, 1)
    with pytest.raises(ValueError):
        expand_tasks(plan, task, schema)


def local_data(tmp_path, monkeypatch):
    """小型数据/模型哈希夹具，绝不加载其中的占位权重或执行训练。"""
    directory = tmp_path / "data"
    directory.mkdir()
    files = {}
    for split, count in (("train", 8), ("dev", 4), ("test", 2)):
        path = directory / f"{split}.jsonl"
        rows = [{"id": f"{split}-{i}", "text": f"unique {split} sentence {i}", "label": i % 2} for i in range(count)]
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        files[path.name] = {"sha256": sha256_file(path), "rows": count}
    (directory / "manifest.json").write_text(
        dumps({"dataset": "stanfordnlp/sst2", "resolved_revision": "fixture", "files": files}), encoding="utf-8"
    )
    model_dir = directory / "model"
    model_dir.mkdir()
    weights = model_dir / "placeholder.bin"
    weights.write_bytes(b"FAKE TEST FIXTURE - NEVER LOAD")
    (directory / "model_manifest.json").write_text(
        dumps(
            {
                "model_name": "distilbert/distilbert-base-uncased",
                "local_dir": str(model_dir),
                "resolved_revision": "fixture",
                "files": {weights.name: sha256_file(weights)},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AIR_DATA_DIR", str(directory))
    monkeypatch.delenv("AIR_MODEL_DIR", raising=False)
    return directory


def test_local_data_hash_tampering_is_detected(tmp_path, monkeypatch):
    directory = local_data(tmp_path, monkeypatch)
    _, task, _ = load_task("tasks/text_cls_lowres")
    original, _ = inspect_inputs(task)
    assert all(c.status == "pass" for c in original)
    (directory / "dev.jsonl").write_text("tampered", encoding="utf-8")
    changed, _ = inspect_inputs(task)
    assert any(c.check == "leakage_data" and c.status == "fail" for c in changed)


@pytest.mark.parametrize("shared_blobs", [False, True])
def test_model_cache_symlinks_are_allowed_but_hashes_still_checked(tmp_path, monkeypatch, shared_blobs):
    directory = local_data(tmp_path, monkeypatch)
    hub = tmp_path / "hf_cache" / "hub"
    model_repo = hub / "models--fixture"
    snapshot = model_repo / "snapshots" / "fixture-revision"
    snapshot.mkdir(parents=True)
    blob = (hub / "blobs" / "ab" / "content") if shared_blobs else (model_repo / "blobs" / "content")
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"FAKE CACHED WEIGHTS - NEVER LOAD")
    link = snapshot / "placeholder.bin"
    try:
        link.symlink_to(os.path.relpath(blob, snapshot))
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows 当前用户没有创建软链接的权限")
        raise
    manifest_path = directory / "model_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(local_dir=str(snapshot), files={link.name: sha256_file(blob)})
    manifest_path.write_text(dumps(manifest), encoding="utf-8")
    _, task, _ = load_task("tasks/text_cls_lowres")
    original, _ = inspect_inputs(task)
    assert all(c.status == "pass" for c in original)
    blob.write_bytes(b"CORRUPTED CACHED CONTENT")
    changed, _ = inspect_inputs(task)
    assert any(c.check == "model" and c.status == "fail" and "SHA256 mismatch" in c.detail for c in changed)


@pytest.mark.parametrize("path_kind", ["parent", "absolute"])
def test_model_manifest_rejects_path_escape_even_when_hash_matches(tmp_path, monkeypatch, path_kind):
    directory = local_data(tmp_path, monkeypatch)
    outside = directory / "outside.bin"
    outside.write_bytes(b"MATCHING HASH MUST NOT ALLOW A PATH ESCAPE")
    name = "../outside.bin" if path_kind == "parent" else str(outside)
    manifest_path = directory / "model_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"] = {name: sha256_file(outside)}
    manifest_path.write_text(dumps(manifest), encoding="utf-8")
    _, task, _ = load_task("tasks/text_cls_lowres")
    result, _ = inspect_inputs(task)
    assert any(c.check == "model" and c.status == "fail" and "relative paths" in c.detail for c in result)


def test_cross_split_duplicate_is_detected_even_with_updated_hash(tmp_path, monkeypatch):
    directory = local_data(tmp_path, monkeypatch)
    train = [json.loads(line) for line in (directory / "train.jsonl").read_text(encoding="utf-8").splitlines()]
    dev_path = directory / "dev.jsonl"
    dev = [json.loads(line) for line in dev_path.read_text(encoding="utf-8").splitlines()]
    dev[0]["text"] = "  " + train[0]["text"].upper() + "  "
    dev_path.write_text("".join(json.dumps(row) + "\n" for row in dev), encoding="utf-8")
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["dev.jsonl"]["sha256"] = sha256_file(dev_path)
    manifest_path.write_text(dumps(manifest), encoding="utf-8")
    _, task, _ = load_task("tasks/text_cls_lowres")
    result, _ = inspect_inputs(task)
    assert any(
        c.check == "leakage_data" and c.status == "fail" and "task leakage/data check failed" in c.detail
        for c in result
    )


@pytest.mark.parametrize("invalid_kind", ["unknown_parameter", "missing_required"])
def test_text_cls_defaults_and_config_schema(tmp_path, monkeypatch, air_home, invalid_kind):
    local_data(tmp_path, monkeypatch)
    task_path = "tasks/text_cls_lowres"
    directory, _, schema = load_task(task_path)
    schema_path = directory / "config_schema.json"
    schema_hash = sha256_file(schema_path)
    idea = fixture_idea(task_path)
    good = preset_draft(idea, task_path, train_size=4)
    bad = deepcopy(good)
    if invalid_kind == "unknown_parameter":
        bad["experiments"][0]["fixed"]["made_up_parameter"] = 1
    else:
        bad["experiments"][0]["fixed"].pop("eval_split")
    project = Project.create(
        goal=idea.research_question,
        task=task_path,
        home=air_home,
        register=False,
        models=offline_models(),
        dev={"stage_impl": {"plan": "real"}},
    )
    ref = project.archive.put("idea/selected.md", "idea", render_front_matter(idea.model_dump(mode="json"), "# Test\n"))
    gateway = fake_gateway(project, {PROMPT: [bad, good]})
    engine = ProjectEngine(
        project, llm=gateway, executor=FakeExecutor(project), retry_delay=0, stages={"idea": FixtureIdeaStage(ref)}
    )
    start_plan(engine, project)
    plan = ExperimentPlan.model_validate(parse_plan_md((project.root / PLAN).read_text(encoding="utf-8")))
    tasks = json.loads((project.root / TASKS).read_text(encoding="utf-8"))
    assert checks(project)["config_schema"]["status"] == "pass" and len(gateway.backend.calls) == 2
    assert checks(project)["license"]["status"] == "warn"
    assert len(tasks) == 9 and all(t["config"]["train_size"] == 4 for t in tasks)
    assert all(t["config"]["eval_split"] == "dev" and t["config"]["batch_size"] == 8 for t in tasks)
    assert plan.budget_estimate["runs"] == 9
    accepted = json.loads((project.root / "environments/data_checksums.json").read_text(encoding="utf-8"))
    assert accepted and all(len(entry["sha256"]) == 64 for entry in accepted)
    assert not getattr(plan, "planning_inputs", None)
    assert sha256_file(schema_path) == schema_hash
    inputs = []
    for request in gateway.backend.calls:
        assert request.prompt_version >= 5
        user = next(message["content"] for message in request.messages if message["role"] == "user")
        document = user.split('<untrusted_document source="plan_inputs">\n', 1)[1]
        context = json.loads(document.rsplit("\n</untrusted_document>", 1)[0])
        assert context["config_schema"] == schema
        assert context["task"]["splits"]["dev"]["role"] == "explore"
        assert context["task"]["splits"]["test"]["role"] == "final"
        inputs.append(context)
    assert not inputs[0]["precheck"]
    assert any(c["check"] == "config_schema" and c["status"] == "fail" for c in inputs[1]["precheck"])
    if invalid_kind == "missing_required":
        assert any("'eval_split' is a required property" in c["detail"] for c in inputs[1]["precheck"])


def plan_input(request):
    user = next(message["content"] for message in request.messages if message["role"] == "user")
    document = user.split('<untrusted_document source="plan_inputs">\n', 1)[1]
    return json.loads(document.rsplit("\n</untrusted_document>", 1)[0])


def test_invalid_experiment_id_gets_specific_format_feedback_and_fake_repair(air_home):
    good = preset_draft(fixture_idea("tasks/smoke"), "tasks/smoke")
    bad = deepcopy(good)
    old = bad["experiments"][0]["id"]
    bad["experiments"][0]["id"] = "full-ft-tuning"
    bad["comparisons"][0]["experiments"][0] = "full-ft-tuning"
    project, engine, _, _ = seeded(air_home, [bad, good])
    start_plan(engine, project)
    [first, repair] = engine.llm.backend.calls
    assert first.prompt_version >= 6
    assert "^[A-Za-z][A-Za-z0-9_]*$" in first.messages[1]["content"]
    assert any("String should match pattern" in message["content"] for message in repair.messages)
    plan = parse_plan_md(project.archive.read_text(project.archive.latest(PLAN)))
    assert plan.experiments[0].id == old
    assert not engine.executor.submitted and project.budget.used().get("llm_usd", 0) == 0


def test_legacy_candidate_id_error_keeps_the_actionable_english_reason(air_home):
    project, _, idea, ref = seeded(air_home)
    _, task, schema = load_task(project.config.task)
    draft = PlanDraft.model_validate(preset_draft(idea, project.config.task))
    plan = make_plan(draft, ref, project.config.task, task, 1)
    plan.experiments[0].id = "full-ft-tuning"
    with pytest.raises(ValueError) as error:
        expand_tasks(plan, task, schema)
    feedback = item("structure", [str(error.value)], "")
    assert "full-ft-tuning" in feedback.detail and "no hyphens or spaces" in feedback.detail
    assert "^[A-Za-z][A-Za-z0-9_]*$" in feedback.detail


def test_exhausted_identifier_retries_keep_the_rule_for_explicit_human_retry(air_home):
    bad = preset_draft(fixture_idea("tasks/smoke"), "tasks/smoke")
    bad["experiments"][0]["id"] = "full-ft-tuning"
    project, engine, _, _ = seeded(air_home, [bad])
    drive(engine, S.IdeaPending)
    approve_pending(project)
    drive(engine, S.Paused)
    assert len(engine.llm.backend.calls) == 6
    question = project.questions.pending()
    assert "experiments.0.id" in question.text and "^[A-Za-z][A-Za-z0-9_]*$" in question.text
    assert "Associated fields/files" not in question.text
    assert not engine.executor.submitted and project.budget.used().get("llm_usd", 0) == 0


@pytest.mark.parametrize("bad_filter", [{"eval_condition": "final"}, {"unknown_key": None}, {"eval_split": "dev"}])
def test_invalid_comparison_filters_receive_actionable_feedback_and_fake_repair(air_home, bad_filter):
    good = preset_draft(fixture_idea("tasks/smoke"), "tasks/smoke")
    good["comparisons"][0]["filter"] = {"eval_split": "test"}
    bad = deepcopy(good)
    bad["comparisons"][0]["filter"] = bad_filter
    project, engine, _, _ = seeded(air_home, [bad, good])
    start_plan(engine, project)
    [_, repair] = engine.llm.backend.calls
    if "eval_condition" in bad_filter:
        message = "\n".join(m["content"] for m in repair.messages if m["role"] == "user")
        assert "experiment metadata" in message and "eval_split" in message
    else:
        details = "\n".join(c["detail"] for c in plan_input(repair)["precheck"])
        expected = "unknown configuration filter keys" if "unknown_key" in bad_filter else "matches no tasks"
        assert expected in details
    assert checks(project)["fairness"]["status"] == "pass"
    plan = parse_plan_md(project.archive.read_text(project.archive.latest(PLAN)))
    assert plan.comparisons[0].filter == {"eval_split": "test"}
    assert not engine.executor.submitted and project.budget.used().get("llm_usd", 0) == 0


def test_remaining_budget_feedback_includes_spent_resources_and_reserved_seeds(air_home):
    good = preset_draft(fixture_idea("tasks/smoke"), "tasks/smoke")
    for experiment in good["experiments"]:
        experiment["est_minutes_per_run"] = 1
    bad = deepcopy(good)
    bad["replication_policy"] = {"max_extra_seeds_per_group": 3, "trigger": "std > 0.03"}
    for experiment in bad["experiments"]:
        experiment["est_minutes_per_run"] = 5
    budget = {"llm_usd": {"hard": 2}, "wall_hours": {"hard": 0.5}, "gpu_hours": {"hard": 0.4}}
    project, engine, idea, _ = seeded(air_home, [bad, good], budget=budget)
    project.budget.charge("wall_hours", 0.1, "prior-test-usage")
    project.budget.charge("gpu_hours", 0.05, "prior-test-usage")
    start_plan(engine, project)
    [initial, repair] = engine.llm.backend.calls
    first = plan_input(initial)["budget_context"]
    assert first["categories"]["wall_hours"]["remaining_before_stop_line"] == pytest.approx(0.35, abs=0.001)
    assert first["categories"]["gpu_hours"]["remaining_before_stop_line"] == pytest.approx(0.31)
    assert "Cartesian product" in first["run_cost_rule"] and "Reserved extra runs" in first["run_cost_rule"]
    details = "\n".join(c["detail"] for c in plan_input(repair)["precheck"] if c["check"] == "budget")
    assert "formal runs=9" in details and "reserved extra runs=9" in details
    assert "Reduce parameter-grid" in details and "Do not increase budget limits" in details
    assert checks(project)["budget"]["status"] == "pass"
    assert project.config.budget["llm_usd"].hard == 2 and project.config.budget["gpu_hours"].hard == 0.4
    smaller_idea = idea.model_copy(update={"budget": {"wall_hours": 0.25, "gpu_hours": 0.2}})
    smaller = planning_budget_context(project.config, smaller_idea, {"wall_hours": 0.1}, 0.9)
    assert smaller["categories"]["wall_hours"]["remaining_before_stop_line"] == pytest.approx(0.125)
    assert smaller["categories"]["gpu_hours"]["hard_limit"] == 0.2
    assert not engine.executor.submitted and project.budget.used().get("llm_usd", 0) == 0


def test_approved_literature_lora_name_reaches_pending_plan_without_modifying_input(
    tmp_path, monkeypatch, air_home
):
    local_data(tmp_path, monkeypatch)
    task_path = "tasks/text_cls_lowres"
    idea = fixture_idea(task_path)
    idea.baselines = [idea.baselines[1]]
    idea.methods[0].name = "Low-Rank Adaptation (LoRA)"
    project = Project.create(
        goal=idea.research_question, task=task_path, home=air_home, register=False,
        models=offline_models(), dev={"stage_impl": {"plan": "real"}},
    )
    text = render_front_matter(idea.model_dump(mode="json"), "# Approved test input\n")
    ref = project.archive.put("idea/selected.md", "idea", text)
    draft = preset_draft(idea, task_path, train_size=4)
    gateway = fake_gateway(project, {PROMPT: [draft]})
    engine = ProjectEngine(
        project, llm=gateway, executor=FakeExecutor(project), retry_delay=0, stages={"idea": FixtureIdeaStage(ref)}
    )
    schema_path = load_task(task_path)[0] / "config_schema.json"
    schema_hash = sha256_file(schema_path)
    start_plan(engine, project)
    assert checks(project)["scope"]["status"] == "pass"
    assert project.archive.read_text(ref) == text and project.archive.latest("idea/selected.md") == ref
    assert sha256_file(schema_path) == schema_hash
    assert len(gateway.backend.calls) == 1 and not engine.executor.submitted


def test_short_idea_repairs_then_hands_off_full_fake_experiment_flow(tmp_path, monkeypatch, air_home):
    from airesearcher.runtime.local import LocalExecutor
    from airesearcher.services.runs import list_runs
    from airesearcher.stages.writing.bundle import EvidenceBundle, build_bundle
    from airesearcher.testing.contracts import check_project
    from scripts.experiment.demo import analysis_reply, simulated_outcome

    def forbidden(*args, **kwargs):
        raise AssertionError("This regression must not submit local training")

    monkeypatch.setattr(LocalExecutor, "submit", forbidden)
    local_data(tmp_path, monkeypatch)
    task_path = "tasks/text_cls_lowres"
    idea = fixture_idea(task_path)
    idea.research_question = "Can LoRA reduce training cost while maintaining accuracy on low-resource SST-2?"
    idea.baselines = [idea.baselines[1]]
    idea.methods[0].name = "Low-Rank Adaptation (LoRA)"
    good = preset_draft(idea, task_path, train_size=4)
    for experiment in good["experiments"]:
        experiment["fixed"].update(epochs=1, eval_split="test")
        experiment["eval_condition"] = "final"
        experiment["est_minutes_per_run"] = 1
    good["comparisons"][0]["filter"] = {"eval_split": "test"}
    bad_id = deepcopy(good)
    bad_id["experiments"][0]["id"] = "full-ft-tuning"
    bad_id["comparisons"][0]["experiments"][0] = "full-ft-tuning"
    bad_filter = deepcopy(good)
    bad_filter["comparisons"][0]["filter"] = {"eval_condition": "final"}
    expensive = deepcopy(good)
    expensive["replication_policy"] = {"max_extra_seeds_per_group": 3, "trigger": "std > 0.03"}
    for experiment in expensive["experiments"]:
        experiment["est_minutes_per_run"] = 10
    project = Project.create(
        goal=idea.research_question, task=task_path, home=air_home, register=False, models=offline_models(),
        budget={"llm_usd": {"hard": 2}, "wall_hours": {"hard": 0.5}, "gpu_hours": {"hard": 0.5}},
        dev={"stage_impl": {"plan": "real", "experiment": "real", "writing": "fake"}},
    )
    ref = project.archive.put("idea/selected.md", "idea", render_front_matter(idea.model_dump(mode="json"), "# Test\n"))
    analysis = analysis_reply()
    analysis["hypothesis_status"] = {"H1": "inconclusive"}
    gateway = fake_gateway(project, {PROMPT: [bad_id, bad_filter, expensive, good], "experiment/analyze": [analysis]})
    executor = FakeExecutor(project, outcomes=simulated_outcome)
    engine = ProjectEngine(
        project, llm=gateway, executor=executor, retry_delay=0, stages={"idea": FixtureIdeaStage(ref)}
    )
    start_plan(engine, project)
    assert len(gateway.backend.calls) == 4 and not executor.submitted
    assert all(c["status"] != "fail" for c in checks(project).values())
    assert project.approvals.pending()[0].kind == "plan"
    approve_pending(project)
    drive(engine, S.LogPending)
    runs = list_runs(project.root)
    assert len(runs) == 8 and all(r.status.state.value == "succeeded" for r in runs)
    assert all(r.record.executor == "fake" for r in runs)
    assert sum(r.record.kind == "trial" for r in runs) == 2
    assert check_project(project.root, owners=["实验"]).ok
    assert project.approvals.pending()[0].kind == "log"
    approve_pending(project)
    drive(engine, S.Writing)
    assert project.budget.used().get("llm_usd", 0) == 0
    assert len(gateway.backend.calls) == 5
    # Exercise the writing owner's real, read-only consumer at the handoff
    # boundary; do not run or alter the downstream writing implementation.
    bundle = build_bundle(project.root, approved=engine.ck.approved, include_runtime=True)
    captured = EvidenceBundle.model_validate_json(bundle.to_bytes())
    assert captured.idea_ref == ref and captured.plan_ref == engine.ck.approved["plan"]
    assert not captured.diagnostics and len(captured.logs) == 1
    assert captured.logs[0].ref == project.approvals.latest_approved("log").target
    assert {a.aggregate_id for a in captured.aggregates} == {"C1", "all"}
    assert all(a.data.plan_ref == captured.plan_ref and all(row.n == 3 for row in a.data.rows)
               for a in captured.aggregates)
    formal = {view.record.run_id for view in runs if view.record.kind != "trial"}
    assert {run["run_id"] for run in captured.runtime.runs} == formal
    assert all(run["plan_ref"] == captured.plan_ref.model_dump(mode="json") for run in captured.runtime.runs)
    assert not (project.root / "paper/evidence_bundle.json").exists()
    assert not (project.root / "paper/main.tex").exists()
