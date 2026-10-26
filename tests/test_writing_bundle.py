"""Evidence bundle source selection and persistence against real Archive/services."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta

import pytest

from airesearcher.core.approvals import Approvals
from airesearcher.core.archive import dir_manifest
from airesearcher.core.fsutil import append_jsonl, atomic_write_json, sha256_file
from airesearcher.core.models.common import VersionRef
from airesearcher.core.models.frontmatter import render_front_matter
from airesearcher.core.models.plan import parse_plan_md
from airesearcher.services import literature, runs
from airesearcher.stages.writing.bundle import BUNDLE_ID, BundleError, build_bundle, write_bundle
from airesearcher.testing.sample import SAMPLE_PROJECT


def approve(project, ref, kind):
    aid = project.approvals.request(ref, kind, "synthetic test approval", "test only")
    return project.approvals.decide(aid, "approved", ref.sha256, "test", request_id=aid)


def put_aggregate(project, aggregate):
    return project.archive.put(f"artifacts/aggregates/{aggregate.aggregate_id}.json", "aggregate",
                               aggregate.model_dump_json(), producer="agent:experiment/aggregate")


def seed(project):
    """Use real frozen source formats in a new synthetic, disposable workspace."""
    idea_text = (SAMPLE_PROJECT / "idea/selected.md").read_text(encoding="utf-8")
    idea = project.archive.put("idea/selected.md", "idea", idea_text)
    approve(project, idea, "idea")
    plan = parse_plan_md((SAMPLE_PROJECT / "plan/experiment_plan.md").read_text(encoding="utf-8"))
    plan.idea_ref = idea
    text = render_front_matter(plan.model_dump(mode="json"), "# Synthetic plan\n")
    plan_ref = project.archive.put("plan/experiment_plan.md", "plan", text, parents=[idea])
    approve(project, plan_ref, "plan")
    return idea, plan_ref


def test_real_sample_content_provenance_and_allowed_effects(sample_project):
    p = sample_project
    before = dir_manifest(p.root, [])
    bundle = build_bundle(p.root)
    assert dir_manifest(p.root, []) == before  # Building has no filesystem effects.
    assert bundle.idea.ref == p.approvals.latest_approved("idea").target
    assert bundle.plan.ref == p.approvals.latest_approved("plan").target
    assert bundle.idea.text == p.archive.read_text(bundle.idea_ref)
    assert bundle.idea.data["hypotheses"] and bundle.plan.data["experiments"]
    assert [a.aggregate_id for a in bundle.aggregates] == ["C1", "C2", "all"]
    for item in bundle.aggregates:
        assert item.data == runs.load_aggregate(p.root, item.aggregate_id, version=item.ref.version)
        assert item.data.rows and item.data.rows[0].values and item.data.rows[0].run_ids
        p.archive.get(item.ref)
    assert bundle.logs[0].text == p.archive.read_text(bundle.logs[0].ref)
    assert bundle.logs[0].approval_id == "ap-0004"
    assert bundle.literature_source.ref == p.archive.latest(literature.LITERATURE)
    assert bundle.literature_source.mode == "archived"
    assert len(bundle.papers) == 3 and all(x.record["abstract"] for x in bundle.papers)
    assert bundle.diagnostics == []
    first = write_bundle(p.root, bundle)
    assert (p.root / BUNDLE_ID).read_bytes() == bundle.to_bytes()
    second = write_bundle(p.root, build_bundle(p.root))
    assert first == second
    assert len(p.archive.versions(BUNDLE_ID)) == 1
    info = p.archive.version_info(first)
    assert info.kind == "other" and info.producer == "agent:writing/bundle"
    assert len(info.parents) == 7  # idea, plan, three aggregates, log, literature
    for ref in info.parents:
        p.archive.get(ref)
    after = dir_manifest(p.root, [])
    changed = {name for name, sha in after if dict(before).get(name) != sha}
    assert all(name == BUNDLE_ID or name == "research_log.jsonl"
               or name.startswith(".archive/paper%2Fevidence_bundle.json/") for name in changed)
    assert str(p.root) not in bundle.to_bytes().decode("utf-8")


def test_approved_v1_beats_newer_unapproved_files(project):
    idea, plan = seed(project)
    assert idea.version == plan.version == 1
    old = build_bundle(project.root)
    project.archive.put(idea.artifact_id, "idea", old.idea.text + "\nUNAPPROVED IDEA v2")
    project.archive.put(plan.artifact_id, "plan", old.plan.text + "\nUNAPPROVED PLAN v2")
    # Raw unregistered edits also must not be used or automatically archived.
    (project.root / idea.artifact_id).write_text("unregistered text", encoding="utf-8")
    before = dir_manifest(project.root, [])
    bundle = build_bundle(project.root, approved={"idea": idea, "plan": plan})
    assert bundle.to_bytes() == old.to_bytes()
    assert dir_manifest(project.root, []) == before
    assert "UNAPPROVED" not in bundle.idea.text + bundle.plan.text


def test_explicit_approved_selection_and_rejection(project):
    idea, plan = seed(project)
    original = build_bundle(project.root)
    newer = project.archive.put(idea.artifact_id, "idea", original.idea.text + "\nApproved v2")
    approve(project, newer, "idea")
    assert build_bundle(project.root).idea_ref == newer
    assert build_bundle(project.root, approved={"idea": idea, "plan": plan}).idea_ref == idea
    assert build_bundle(project.root).diagnostics[0].code == "PLAN_IDEA_MISMATCH"
    unapproved = project.archive.put(idea.artifact_id, "idea", original.idea.text + "\nPending v3")
    with pytest.raises(BundleError, match="idea.*not approved"):
        build_bundle(project.root, approved={"idea": unapproved, "plan": plan})


@pytest.mark.parametrize("missing", ["idea", "plan"])
def test_missing_context_approvals(sample_project, missing):
    refs = {kind: sample_project.approvals.latest_approved(kind).target for kind in ("idea", "plan")}
    del refs[missing]
    with pytest.raises(BundleError, match=f"missing required approved {missing}"):
        build_bundle(sample_project.root, approved=refs)


def test_missing_approval_records(project):
    with pytest.raises(BundleError, match="idea.*missing required approval"):
        build_bundle(project.root)


def test_log_version_selection_not_record_order(sample_project, monkeypatch):
    p = sample_project
    log_id = "logs/round_1.md"
    v2 = p.archive.put(log_id, "process_log", "# Approved second log\n", producer="agent:experiment")
    approved = approve(p, v2, "log")
    duplicate = approve(p, v2, "log")
    assert approved.approval_id < duplicate.approval_id
    v3 = p.archive.put(log_id, "process_log", "# Rejected third log\n")
    aid = p.approvals.request(v3, "log", "test", "test")
    p.approvals.decide(aid, "rejected", v3.sha256, "reject", request_id=aid)
    v4 = p.archive.put(log_id, "process_log", "# Pending fourth log\n")
    p.approvals.request(v4, "log", "test", "test")
    other = p.archive.put("notes/important.md", "process_log", "# Important by approval, not filename\n")
    approve(p, other, "log")
    # Re-approving an older version later is a valid duplicate approval; version wins.
    v1 = p.archive.versions(log_id)[0].ref
    # An imported historical Approval record emulates a later approval of old v1.
    historical = duplicate.model_copy(deep=True, update={"approval_id": "ap-9999", "target": v1})
    atomic_write_json(p.root / "approvals/ap-9999.json", historical)
    original = Approvals.list
    monkeypatch.setattr(Approvals, "list", lambda self, *a, **kw: list(reversed(original(self, *a, **kw))))
    bundle = build_bundle(p.root)
    assert [x.ref.artifact_id for x in bundle.logs] == [log_id, "notes/important.md"]
    assert bundle.logs[0].ref == v2 and bundle.logs[0].approval_id == duplicate.approval_id
    assert "Rejected" not in bundle.logs[0].text and "Pending" not in bundle.logs[0].text


def test_aggregate_exact_versions_preserve_fields(sample_project, monkeypatch):
    p = sample_project
    original_ref = p.archive.latest("artifacts/aggregates/C1.json")
    aggregate = runs.load_aggregate(p.root, "C1")
    aggregate.eval_condition = "explore"
    aggregate.rows[0].suspicious = [aggregate.rows[0].run_ids[0]]
    aggregate.excluded.append({"run_id": "synthetic-excluded", "reason": "explicit test warning"})
    latest_ref = put_aggregate(p, aggregate)
    (p.root / latest_ref.artifact_id).write_text('{"unregistered": true}', encoding="utf-8")
    service = runs.load_aggregate
    calls = []

    def tracked(root, aggregate_id, version=None):
        calls.append((aggregate_id, version))
        return service(root, aggregate_id, version=version)

    monkeypatch.setattr(runs, "load_aggregate", tracked)
    before = dir_manifest(p.root, [])
    bundle = build_bundle(p.root)
    chosen = next(x for x in bundle.aggregates if x.aggregate_id == "C1")
    assert chosen.ref == latest_ref and chosen.data == aggregate
    assert chosen.data.eval_condition == "explore" and chosen.data.excluded[-1]["reason"]
    assert chosen.data.rows[0].suspicious == aggregate.rows[0].suspicious
    assert chosen.data.rows[0].values == aggregate.rows[0].values
    assert ("C1", latest_ref.version) in calls and all(version is not None for _, version in calls)
    assert dir_manifest(p.root, []) == before
    historical = build_bundle(p.root, aggregate_refs=[original_ref])
    assert historical.aggregates[0].ref == original_ref
    assert historical.aggregates[0].data.eval_condition == "final"


def test_plan_provenance_mismatch_is_diagnostic(sample_project):
    p = sample_project
    old = build_bundle(p.root)
    new_plan = p.archive.put(old.plan_ref.artifact_id, "plan", old.plan.text + "\nApproved revision")
    approve(p, new_plan, "plan")
    bundle = build_bundle(p.root)
    assert bundle.plan_ref == new_plan
    assert {d.code for d in bundle.diagnostics} == {"AGGREGATE_PLAN_MISMATCH"}
    assert len(bundle.diagnostics) == 3
    assert all(a.data.plan_ref == old.plan_ref for a in bundle.aggregates)
    ref = write_bundle(p.root, bundle)
    assert old.plan_ref in p.archive.version_info(ref).parents


@pytest.mark.parametrize("source", ["idea", "plan", "aggregate", "log", "literature"])
@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_bad_archive_preserves_existing_bundle(sample_project, source, damage):
    p = sample_project
    good = build_bundle(p.root)
    ref = write_bundle(p.root, good)
    before = (p.root / BUNDLE_ID).read_bytes()
    selected = {"idea": good.idea_ref, "plan": good.plan_ref, "aggregate": good.aggregates[0].ref,
                "log": good.logs[0].ref, "literature": good.literature_source.ref}[source]
    path = p.archive.stored_path(selected)
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"corrupted source")
    with pytest.raises(BundleError, match="cannot build evidence bundle"):
        build_bundle(p.root)
    with pytest.raises(BundleError):
        write_bundle(p.root, good)
    assert (p.root / BUNDLE_ID).read_bytes() == before
    assert p.archive.latest(BUNDLE_ID) == ref


def test_literature_offline_status_and_volatile_time(sample_project, monkeypatch):
    p = sample_project
    real = literature.verify_citation
    calls = []

    def offline(root, paper_id, online=False):
        assert online is False
        calls.append(paper_id)
        status = real(root, paper_id, online=online)
        status.checked_at = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=len(calls))
        return status

    monkeypatch.setattr(literature, "verify_citation", offline)
    first = build_bundle(p.root)
    second = build_bundle(p.root)
    assert first.to_bytes() == second.to_bytes() and len(calls) == 6
    assert all("checked_at" not in x.citation for x in first.papers)
    assert all(x.citation["status"] == real(p.root, x.paper_id).status for x in first.papers)
    assert first.papers[0].record["access_level"] == "abstract_only"
    assert first.literature_source.verification == "local_offline"


def test_unregistered_literature_change_has_honest_snapshot(sample_project):
    p = sample_project
    original = build_bundle(p.root)
    record = next(iter(literature.load_papers(p.root).values()))
    record.abstract = "Changed local abstract"
    record.access_level = "metadata_only"
    append_jsonl(p.root / literature.LITERATURE, record)
    bundle = build_bundle(p.root)
    assert bundle.literature_source.mode == "current_snapshot" and bundle.literature_source.ref is None
    assert bundle.literature_source.sha256 == sha256_file(p.root / literature.LITERATURE)
    changed = next(x for x in bundle.papers if x.paper_id == record.paper_id)
    previous = next(x for x in original.papers if x.paper_id == record.paper_id)
    assert changed.record["abstract"] == "Changed local abstract"
    assert changed.status == changed.citation["status"] == "metadata_only"
    assert changed.record_sha256 != previous.record_sha256
    assert bundle.to_bytes() != original.to_bytes()
    assert p.archive.latest(literature.LITERATURE) == original.literature_source.ref


def test_empty_optional_collections(project):
    seed(project)
    bundle = build_bundle(project.root)
    assert bundle.aggregates == bundle.logs == bundle.papers == []
    assert bundle.literature_source.mode == "absent" and bundle.literature_source.ref is None
    assert write_bundle(project.root, bundle)


def test_collection_order_and_workspace_independence(sample_project, tmp_path, monkeypatch):
    p = sample_project
    first = build_bundle(p.root)
    refs = [x.ref for x in reversed(first.aggregates)]
    assert build_bundle(p.root, aggregate_refs=refs).to_bytes() == first.to_bytes()
    other = tmp_path / "relocated"
    shutil.copytree(p.root, other)
    assert build_bundle(other).to_bytes() == first.to_bytes()
    load = literature.load_papers
    monkeypatch.setattr(literature, "load_papers", lambda root: dict(reversed(list(load(root).items()))))
    assert build_bundle(p.root).to_bytes() == first.to_bytes()


def test_portable_literature_paths(sample_project):
    p = sample_project
    paper = next(iter(literature.load_papers(p.root).values()))
    paper.fulltext_path = str(p.root / "idea/fulltext/paper.txt")
    append_jsonl(p.root / literature.LITERATURE, paper)
    bundle = build_bundle(p.root)
    assert next(x for x in bundle.papers if x.paper_id == paper.paper_id).record["fulltext_path"] == (
        "idea/fulltext/paper.txt"
    )
    assert str(p.root) not in bundle.to_bytes().decode("utf-8")
    paper.fulltext_path = "C:\\private\\paper.txt"
    append_jsonl(p.root / literature.LITERATURE, paper)
    item = next(x for x in build_bundle(p.root).papers if x.paper_id == paper.paper_id)
    assert item.record["fulltext_path"].startswith("external-path:sha256:")


def test_tampered_bundle_and_changed_snapshot_do_not_replace(sample_project):
    p = sample_project
    good = build_bundle(p.root)
    first = write_bundle(p.root, good)
    bad = good.model_copy(deep=True)
    bad.idea.text = "invented approved content"
    with pytest.raises(BundleError, match="captured bundle"):
        write_bundle(p.root, bad)
    paper = next(iter(literature.load_papers(p.root).values()))
    paper.title = "Changed after build"
    append_jsonl(p.root / literature.LITERATURE, paper)
    with pytest.raises(BundleError, match="captured bundle"):
        write_bundle(p.root, good)
    assert (p.root / BUNDLE_ID).read_bytes() == good.to_bytes()
    assert p.archive.latest(BUNDLE_ID) == first


def test_explicit_empty_and_invalid_aggregate_selection(sample_project):
    p = sample_project
    assert build_bundle(p.root, aggregate_refs=[]).aggregates == []
    ref = p.archive.latest("artifacts/aggregates/C1.json")
    for refs in ([ref, ref], [p.archive.latest("idea/selected.md")],
                 [ref.model_copy(update={"sha256": "f" * 64})]):
        with pytest.raises(BundleError):
            build_bundle(p.root, aggregate_refs=refs)


def test_finite_serialization_required(sample_project):
    p = sample_project
    old = build_bundle(p.root)
    write_bundle(p.root, old)
    agg = runs.load_aggregate(p.root, "C1")
    # Archive raw invalid JSON numbers to exercise strict bundle serialization.
    data = agg.model_dump(mode="json")
    data["rows"][0]["mean"] = float("nan")
    p.archive.put("artifacts/aggregates/C1.json", "aggregate", json.dumps(data))
    with pytest.raises(BundleError):
        build_bundle(p.root)
    assert (p.root / BUNDLE_ID).read_bytes() == old.to_bytes()


def test_missing_approval_after_build_preserves_bundle(sample_project):
    p = sample_project
    bundle = build_bundle(p.root)
    saved = write_bundle(p.root, bundle)
    (p.root / "approvals" / f"{bundle.idea.approval_id}.json").unlink()
    with pytest.raises(BundleError, match="idea.*approval"):
        write_bundle(p.root, bundle)
    assert (p.root / BUNDLE_ID).read_bytes() == bundle.to_bytes()
    assert p.archive.latest(BUNDLE_ID) == saved


def test_directory_source_is_not_read_as_manifest_text(project):
    idea, plan = seed(project)
    archive_file = project.root / idea.artifact_id
    archive_file.unlink()
    archive_file.mkdir()
    (archive_file / "selected.md").write_text("not a canonical source file", encoding="utf-8")
    directory_ref = project.archive.put(idea.artifact_id, "idea", archive_file)
    approve(project, directory_ref, "idea")
    with pytest.raises(BundleError, match="directory manifest"):
        build_bundle(project.root, approved={"idea": directory_ref, "plan": plan})


def test_write_rejects_symlink_alias(sample_project):
    p = sample_project
    bundle = build_bundle(p.root)
    (p.root / BUNDLE_ID).unlink()
    target = p.root / "idea/selected.md"
    original = target.read_bytes()
    try:
        (p.root / BUNDLE_ID).symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable on this platform: {exc}")
    with pytest.raises(BundleError, match="symlink"):
        write_bundle(p.root, bundle)
    assert target.read_bytes() == original


def test_citation_uncertainty_not_upgraded(sample_project, monkeypatch):
    real = literature.verify_citation

    def uncertain(root, paper_id, online=False):
        result = real(root, paper_id, online=online)
        result.status = "source_unreachable"
        result.note = "offline service uncertainty retained"
        return result

    monkeypatch.setattr(literature, "verify_citation", uncertain)
    bundle = build_bundle(sample_project.root)
    assert all(p.status == p.citation["status"] == "source_unreachable" for p in bundle.papers)


def test_literature_change_during_capture_fails(sample_project, monkeypatch):
    p = sample_project
    real = literature.verify_citation
    changed = False

    def change_during_lookup(root, paper_id, online=False):
        nonlocal changed
        result = real(root, paper_id, online=online)
        if not changed:
            record = literature.load_papers(root)[paper_id]
            record.abstract = "concurrent change during capture"
            append_jsonl(root / literature.LITERATURE, record)
            changed = True
        return result

    monkeypatch.setattr(literature, "verify_citation", change_during_lookup)
    with pytest.raises(BundleError, match="literature changed during capture"):
        build_bundle(p.root)


def historical_lineage(project):
    """Old v1 lineage, newer approved v2 sources; only the aggregate uses v1."""
    idea1, plan1 = seed(project)
    aggregate = runs.load_aggregate(SAMPLE_PROJECT, "C1").model_copy(update={"plan_ref": plan1})
    aggregate_ref = put_aggregate(project, aggregate)
    idea2 = project.archive.put(idea1.artifact_id, "idea", project.archive.read_text(idea1) + "\nNew idea")
    approve(project, idea2, "idea")
    plan = parse_plan_md(project.archive.read_text(plan1))
    plan.idea_ref = idea2
    plan2 = project.archive.put(plan1.artifact_id, "plan", render_front_matter(plan.model_dump(mode="json"),
                               "# New approved plan"), parents=[idea2])
    approve(project, plan2, "plan")
    return idea1, plan1, aggregate_ref, idea2, plan2


def test_old_valid_lineage_is_preserved_with_complete_parents_and_idempotency(project):
    idea1, plan1, aggregate_ref, idea2, plan2 = historical_lineage(project)
    before = dir_manifest(project.root, [])
    bundle = build_bundle(project.root)
    assert dir_manifest(project.root, []) == before
    assert bundle.schema_version == 1 and bundle.idea_ref == idea2 and bundle.plan_ref == plan2
    assert bundle.aggregates[0].ref == aggregate_ref and bundle.aggregates[0].data.plan_ref == plan1
    assert parse_plan_md(project.archive.read_text(plan1)).idea_ref == idea1
    assert [(d.code, d.actual_ref, d.approved_ref) for d in bundle.diagnostics] == [
        ("AGGREGATE_PLAN_MISMATCH", plan1, plan2)]
    first = write_bundle(project.root, bundle)
    parents = project.archive.version_info(first).parents
    assert set((r.artifact_id, r.version, r.sha256) for r in parents) == set(
        (r.artifact_id, r.version, r.sha256) for r in [idea1, idea2, plan1, plan2, aggregate_ref])
    assert len(parents) == 5 and parents == sorted(parents, key=lambda r: (r.artifact_id, r.version, r.sha256))
    for parent in parents:
        project.archive.get(parent)
    second_bundle = build_bundle(project.root)
    assert second_bundle.to_bytes() == bundle.to_bytes()
    assert write_bundle(project.root, second_bundle) == first
    assert len(project.archive.versions(BUNDLE_ID)) == 1


@pytest.mark.parametrize("damage", ["missing_plan", "tampered_plan", "missing_idea", "tampered_idea",
                                   "bad_idea_hash", "bad_idea_version", "invalid_idea_ref", "unparseable_idea",
                                   "directory_idea", "wrong_idea_kind", "wrong_plan_kind"])
def test_broken_historical_closure_blocks_build_and_preserves_bundle(project, damage):
    idea1, plan1, aggregate_ref, _, _ = historical_lineage(project)
    good = build_bundle(project.root)
    saved = write_bundle(project.root, good)
    before = (project.root / BUNDLE_ID).read_bytes()
    if damage in {"missing_plan", "tampered_plan", "missing_idea", "tampered_idea"}:
        path = project.archive.stored_path(plan1 if damage.endswith("plan") else idea1)
        if damage.startswith("missing"):
            path.unlink()
        else:
            path.write_bytes(b"tampered archived bytes")
    else:
        plan = parse_plan_md(project.archive.read_text(plan1))
        if damage == "bad_idea_hash":
            plan.idea_ref = idea1.model_copy(update={"sha256": "f" * 64})
        elif damage == "bad_idea_version":
            plan.idea_ref = idea1.model_copy(update={"version": 999})
        elif damage == "invalid_idea_ref":
            plan.idea_ref = VersionRef(artifact_id=idea1.artifact_id, version=0, sha256="invalid")
        elif damage == "unparseable_idea":
            plan.idea_ref = project.archive.put("idea/historical.md", "idea", "not valid idea front matter")
        elif damage == "directory_idea":
            path = project.root / "idea/history"
            path.mkdir()
            (path / "note.md").write_text("not an Idea", encoding="utf-8")
            plan.idea_ref = project.archive.put("idea/history", "idea", path)
        elif damage == "wrong_idea_kind":
            plan.idea_ref = project.archive.put("idea/history.md", "other", project.archive.read_text(idea1))
        historical = project.archive.put("plan/history.md", "other" if damage == "wrong_plan_kind" else "plan",
                                        render_front_matter(plan.model_dump(mode="json"), "# Historical test plan"))
        aggregate = runs.load_aggregate(project.root, "C1", version=aggregate_ref.version)
        aggregate.plan_ref = historical
        put_aggregate(project, aggregate)
        # Check the new captured selection during write too, independent of payload drift.
        good.aggregates[0].data = aggregate
        good.aggregates[0].ref = project.archive.latest(aggregate_ref.artifact_id)
    with pytest.raises(BundleError):
        build_bundle(project.root)
    with pytest.raises(BundleError):
        write_bundle(project.root, good)
    assert (project.root / BUNDLE_ID).read_bytes() == before
    assert project.archive.latest(BUNDLE_ID) == saved


def test_identical_legacy_bundle_with_incomplete_parents_is_not_silently_reused(project):
    idea1, plan1, aggregate_ref, idea2, plan2 = historical_lineage(project)
    bundle = build_bundle(project.root)
    # Simulate a pre-patch archive entry: same payload, missing the historical Idea.
    legacy = project.archive.put(BUNDLE_ID, "other", bundle.to_bytes(),
                                 parents=[idea2, plan2, plan1, aggregate_ref])
    before = dir_manifest(project.root, [])
    with pytest.raises(BundleError, match="identical bundle.*parents.*migration"):
        write_bundle(project.root, bundle)
    assert dir_manifest(project.root, []) == before
    assert project.archive.latest(BUNDLE_ID) == legacy
    assert idea1 not in project.archive.version_info(legacy).parents
