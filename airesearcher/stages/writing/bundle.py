"""Writing-owned evidence bundle schema v1 (no shared/core contract changes).

API: build_bundle(root, approved=None, aggregate_refs=None) -> EvidenceBundle;
write_bundle(root, bundle) -> VersionRef. Building is read-only. Persistence validates
the captured selection again before Archive.put performs an atomic file replacement.

Policy:
* idea/plan are required canonical Markdown file artifacts. Explicit ctx.approved refs
  must have matching approved records; otherwise choose the greatest approved version.
* Important logs are exactly approvals.list('log', 'approved'), grouped by artifact ID.
  Choose greatest version, then lexically greatest approval_id for duplicates. File
  content comes from Archive, never mutable working files or directory manifests.
* Default aggregates include every registered kind=aggregate JSON at the canonical
  aggregate path (including 'all'); CSV/Markdown exports are not separate evidence.
  An explicit ref list is an exact selection, including [] for no aggregates.
* Each aggregate is loaded through runs.load_aggregate(version=...), checked against
  archived bytes, and retained in full. Mismatched plan/idea lineage is diagnosed,
  not relabelled or dropped. Referenced plan/idea bytes must also verify.
* Literature uses the official current offline services. Record full metadata and
  fingerprints. Attach a VersionRef only when the current JSONL bytes match a verified
  archive version; otherwise label it current_snapshot (or absent). No source is
  registered by the builder. Fulltext paths inside the workspace become relative;
  external absolute paths become opaque external-path:sha256 identities. Raw source
  SHA-256 and per-record fingerprints retain identity without absolute machine paths.
* CitationStatus.checked_at is omitted: this is an offline snapshot, not a new online
  verification. All other status fields and source timestamps are preserved.
* Empty aggregates/logs/literature are allowed. Required approvals/content/hash errors,
  non-finite JSON, or sources changing during capture fail explicitly. No wall clock
  or absolute workspace location is added. Values and run IDs keep their alignment.

The fake's idea_ref/plan_ref/aggregates/logs/papers keys remain; entries now carry
substantive content. No existing code consumes the fake bundle. Schema v1 is local
to Writing, not a new framework. Only paper/evidence_bundle.json is registered, as
kind='other', producer='agent:writing/bundle'. There is no whole-paper registration.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from airesearcher.core.approvals import Approvals
from airesearcher.core.archive import Archive
from airesearcher.core.errors import AirError
from airesearcher.core.events import Events
from airesearcher.core.fsutil import sha256_bytes
from airesearcher.core.models.aggregate import Aggregate
from airesearcher.core.models.approval import Approval
from airesearcher.core.models.common import VersionRef
from airesearcher.core.models.idea import parse_selected_md
from airesearcher.core.models.plan import parse_plan_md
from airesearcher.services import literature, runs

BUNDLE_ID = "paper/evidence_bundle.json"
REQUIRED = {"idea": "idea/selected.md", "plan": "plan/experiment_plan.md"}


class BundleError(ValueError):
    """A selected source is missing, unapproved, inconsistent or unverifiable."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApprovedSource(_Model):
    ref: VersionRef
    approval_id: str
    text: str
    data: dict[str, Any] | None = None


class AggregateSource(_Model):
    aggregate_id: str
    ref: VersionRef
    data: Aggregate


class LiteratureSource(_Model):
    path: str = literature.LITERATURE
    mode: Literal["archived", "current_snapshot", "absent"]
    sha256: str
    ref: VersionRef | None = None
    verification: Literal["local_offline"] = "local_offline"


class PaperSource(_Model):
    paper_id: str
    status: str  # Compatibility with the fake index; full service result is below.
    record: dict[str, Any]
    record_sha256: str
    citation: dict[str, Any]  # CitationStatus, intentionally excluding checked_at.


class Diagnostic(_Model):
    code: Literal["AGGREGATE_PLAN_MISMATCH", "PLAN_IDEA_MISMATCH"]
    artifact_id: str
    actual_ref: VersionRef
    approved_ref: VersionRef


class EvidenceBundle(_Model):
    schema_version: Literal[1] = 1
    idea_ref: VersionRef
    plan_ref: VersionRef
    idea: ApprovedSource
    plan: ApprovedSource
    aggregates: list[AggregateSource]
    logs: list[ApprovedSource]
    papers: list[PaperSource]
    literature_source: LiteratureSource
    diagnostics: list[Diagnostic]

    def to_bytes(self) -> bytes:
        """Stable UTF-8 JSON; reject NaN/Infinity instead of emitting invalid evidence."""
        return _json_bytes(self.model_dump(mode="json"))


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _archive(root: Path) -> Archive:
    return Archive(root, Events(root))


def _read(archive: Archive, ref: VersionRef) -> bytes:
    if ref.version < 1 or not re.fullmatch(r"[0-9a-f]{64}", ref.sha256):
        raise BundleError(f"invalid VersionRef: {ref.artifact_id}")
    archive.path_of(ref.artifact_id)
    info = archive.version_info(ref)
    if info.is_dir:
        raise BundleError(f"expected a source file, not a directory manifest: {ref.short()}")
    return archive.get(ref)


def _choose(records: Sequence[Approval], ref: VersionRef | None = None) -> Approval:
    choices = [a for a in records if ref is None or a.target == ref]
    if not choices:
        raise BundleError("missing required approval or supplied ref is not approved")
    return max(choices, key=lambda a: (a.target.version, a.approval_id))


def _approved_source(archive: Archive, approval: Approval, kind: str) -> ApprovedSource:
    text = _read(archive, approval.target).decode("utf-8")
    data = None
    if kind == "idea":
        data = parse_selected_md(text).model_dump(mode="json")
    elif kind == "plan":
        data = parse_plan_md(text).model_dump(mode="json")
    return ApprovedSource(ref=approval.target, approval_id=approval.approval_id, text=text, data=data)


def _plan_idea(archive: Archive, plan_ref: VersionRef) -> VersionRef:
    """Verify exact archived Plan -> Idea closure, without requiring current approval."""
    plan = parse_plan_md(_read(archive, plan_ref).decode("utf-8"))
    if archive.version_info(plan_ref).kind != "plan":
        raise BundleError(f"not a Plan artifact: {plan_ref.short()}")
    idea_ref = plan.idea_ref
    parse_selected_md(_read(archive, idea_ref).decode("utf-8"))
    if archive.version_info(idea_ref).kind != "idea":
        raise BundleError(f"not an Idea artifact: {idea_ref.short()}")
    return idea_ref


def _aggregate_sources(root: Path, archive: Archive, refs: Sequence[VersionRef] | None) -> list[AggregateSource]:
    selected = list(refs) if refs is not None else [
        v.ref for v in archive.list_latest("aggregate")
        if PurePosixPath(v.ref.artifact_id).parent == PurePosixPath("artifacts/aggregates")
        and PurePosixPath(v.ref.artifact_id).suffix == ".json"
    ]
    result, seen = [], set()
    for ref in sorted(selected, key=lambda r: r.artifact_id):
        path = PurePosixPath(ref.artifact_id)
        if (path.parent != PurePosixPath("artifacts/aggregates") or path.suffix != ".json"
                or path.as_posix() != ref.artifact_id or not path.stem or ref.artifact_id in seen):
            raise BundleError("aggregate selection must contain one canonical JSON ref per aggregate")
        seen.add(ref.artifact_id)
        if archive.version_info(ref).kind != "aggregate":
            raise BundleError(f"not an Aggregate artifact: {ref.short()}")
        raw = _read(archive, ref)
        source = runs.load_aggregate(root, path.stem, version=ref.version)
        if source.aggregate_id != path.stem or source != Aggregate.model_validate_json(raw):
            raise BundleError(f"service/source Aggregate mismatch: {ref.short()}")
        _plan_idea(archive, source.plan_ref)
        result.append(AggregateSource(aggregate_id=source.aggregate_id, ref=ref, data=source))
    return result


def _portable_fulltext(record: dict[str, Any], root: Path) -> dict[str, Any]:
    record = dict(record)
    location = record.get("fulltext_path")
    if location and (Path(location).is_absolute() or PureWindowsPath(location).is_absolute()):
        try:
            record["fulltext_path"] = Path(location).relative_to(root).as_posix()
        except ValueError:
            record["fulltext_path"] = "external-path:sha256:" + sha256_bytes(location.encode("utf-8"))
    return record


def _literature(root: Path, archive: Archive) -> tuple[LiteratureSource, list[PaperSource]]:
    path = archive.path_of(literature.LITERATURE)
    before = path.read_bytes() if path.exists() else None
    papers = literature.load_papers(root)
    sources = []
    for pid, paper in sorted(papers.items()):
        status = literature.verify_citation(root, pid, online=False)
        if (status.paper_id != pid or status.citation_key != paper.citation_key
                or status.bibtex != paper.bibtex or status.title != paper.title or status.url != paper.url):
            raise BundleError(f"literature changed during citation lookup: {pid}")
        record = paper.model_dump(mode="json")
        sources.append(PaperSource(paper_id=pid, status=status.status,
                                   record=_portable_fulltext(record, root),
                                   record_sha256=sha256_bytes(_json_bytes(record)),
                                   citation=status.model_dump(mode="json", exclude={"checked_at"})))
    after = path.read_bytes() if path.exists() else None
    if before != after:
        raise BundleError("literature changed during capture; retry from a stable local snapshot")
    fingerprint = sha256_bytes(before or b"")
    ref = None
    for version in reversed(archive.versions(literature.LITERATURE)):
        if before is not None and version.ref.sha256 == fingerprint:
            if _read(archive, version.ref) != before:
                raise BundleError("literature archive does not match selected local snapshot")
            ref = version.ref
            break
    mode = "archived" if ref else ("current_snapshot" if before is not None else "absent")
    return LiteratureSource(mode=mode, sha256=fingerprint, ref=ref), sources


def build_bundle(root: Path, *, approved: Mapping[str, VersionRef] | None = None,
                 aggregate_refs: Sequence[VersionRef] | None = None) -> EvidenceBundle:
    """Collect an inspectable, read-only snapshot. Explicit ctx.approved is supported.

    Default selection includes all latest registered Aggregate JSONs, even historical
    plan results and exploratory results; diagnostics expose lineage mismatches.
    No current aggregate/idea/plan/log bytes are silently adopted or registered.
    """
    root = Path(root).resolve()
    archive = _archive(root)
    approvals = Approvals(root, archive.events, archive)
    try:
        selected = {}
        for kind, artifact_id in REQUIRED.items():
            candidates = [a for a in approvals.list(kind, "approved") if a.target.artifact_id == artifact_id]
            if approved is not None and kind not in approved:
                raise BundleError(f"missing required approved {kind} ref")
            if approved is not None and not isinstance(approved[kind], VersionRef):
                raise BundleError(f"invalid required approved {kind} ref")
            try:
                approval = _choose(candidates, approved[kind] if approved is not None else None)
            except BundleError as exc:
                raise BundleError(f"{kind}: {exc}") from exc
            selected[kind] = _approved_source(archive, approval, kind)
        log_groups: dict[str, list[Approval]] = {}
        for approval in approvals.list("log", "approved"):
            log_groups.setdefault(approval.target.artifact_id, []).append(approval)
        logs = [_approved_source(archive, _choose(log_groups[aid]), "log") for aid in sorted(log_groups)]
        aggregates = _aggregate_sources(root, archive, aggregate_refs)
        plan_idea = _plan_idea(archive, selected["plan"].ref)
        diagnostics = []
        if plan_idea != selected["idea"].ref:
            diagnostics.append(Diagnostic(code="PLAN_IDEA_MISMATCH", artifact_id=selected["plan"].ref.artifact_id,
                                          actual_ref=plan_idea, approved_ref=selected["idea"].ref))
        for item in aggregates:
            if item.data.plan_ref != selected["plan"].ref:
                diagnostics.append(Diagnostic(code="AGGREGATE_PLAN_MISMATCH", artifact_id=item.ref.artifact_id,
                                              actual_ref=item.data.plan_ref, approved_ref=selected["plan"].ref))
        literature_source, papers = _literature(root, archive)
        bundle = EvidenceBundle(idea_ref=selected["idea"].ref, plan_ref=selected["plan"].ref,
                                idea=selected["idea"], plan=selected["plan"], logs=logs, aggregates=aggregates,
                                papers=papers, literature_source=literature_source, diagnostics=diagnostics)
        bundle.to_bytes()  # Validate serialization before any caller can persist it.
        return bundle
    except (AirError, OSError, ValueError, KeyError) as exc:
        if isinstance(exc, BundleError):
            raise
        raise BundleError(f"cannot build evidence bundle: {exc}") from exc


def write_bundle(root: Path, bundle: EvidenceBundle) -> VersionRef:
    """Revalidate the captured selection before atomically persisting/registering it.

    New latest aggregate versions do not invalidate an explicit captured selection.
    Changed approvals/log selection or literature fail before replacing an old bundle.
    Archive writes are atomic for the bundle file, not a multi-file crash transaction.
    Archive.put deduplicates by payload, not parents. An identical legacy payload
    with incomplete parents requires explicit lineage migration; never rewrite its
    historical metadata or silently claim that missing parents were registered.
    """
    root = Path(root).resolve()
    target = root / BUNDLE_ID
    if target.is_symlink() or target.parent.is_symlink():
        raise BundleError("bundle destination must not alias another file through a symlink")
    if target.exists() and not target.is_file():
        raise BundleError("bundle destination must be a file")
    bundle = EvidenceBundle.model_validate(bundle.model_dump(mode="json"))
    payload = bundle.to_bytes()
    current = build_bundle(root, approved={"idea": bundle.idea_ref, "plan": bundle.plan_ref},
                           aggregate_refs=[a.ref for a in bundle.aggregates])
    if current.to_bytes() != payload:
        raise BundleError("captured bundle or selected sources changed; rebuild before writing")
    refs = [bundle.idea_ref, bundle.plan_ref, *[x.ref for x in bundle.logs],
            *[x.ref for x in bundle.aggregates], *[x.data.plan_ref for x in bundle.aggregates],
            *[x.actual_ref for x in bundle.diagnostics]]
    if bundle.literature_source.ref:
        refs.append(bundle.literature_source.ref)
    archive = _archive(root)
    plan_refs = {(r.artifact_id, r.version, r.sha256): r
                 for r in [bundle.plan_ref, *[x.data.plan_ref for x in bundle.aggregates]]}
    refs.extend(_plan_idea(archive, plan_refs[key]) for key in sorted(plan_refs))
    unique = {(r.artifact_id, r.version, r.sha256): r for r in refs}
    parents = [unique[key] for key in sorted(unique)]
    latest = archive.latest_version(BUNDLE_ID)
    if latest and latest.ref.sha256 == sha256_bytes(payload):
        recorded = {(r.artifact_id, r.version, r.sha256) for r in latest.parents}
        if not unique.keys() <= recorded:
            raise BundleError("identical bundle is missing required provenance parents; "
                              "explicit lineage migration required")
    return archive.put(BUNDLE_ID, "other", payload, parents=parents, producer="agent:writing/bundle")
