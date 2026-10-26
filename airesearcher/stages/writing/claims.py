"""Model intent -> exact frozen evidence. No Stage orchestration or semantic review."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from airesearcher.core.archive import Archive, dir_manifest, manifest_bytes
from airesearcher.core.errors import AirError
from airesearcher.core.fsutil import sha256_bytes
from airesearcher.core.models.claim import Claim, Comparison, EvidenceLink, Stat, ValueRef
from airesearcher.core.models.common import VersionRef
from airesearcher.core.models.figure import Figure
from airesearcher.services import evidence, runs

from ._latex import active_text, commands, safe_path
from .bundle import EvidenceBundle

PROMPT_ID = "writing/claims"
SECTIONS = ("abstract", "introduction", "related_work", "method", "setup", "results", "discussion", "conclusion")
TOKEN = re.compile(r"\[\[([A-Za-z][A-Za-z0-9_]*)\]\]")
MARKER = re.compile(r"\[\[CLAIM:(CL[1-9][0-9]*)\]\]")
SAFE_KEY = re.compile(r"[A-Za-z0-9_.-]+")
PREAMBLE = "\\input{air_values}\n\\newcommand{\\claim}[2]{#2}\n"


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlannedContrast(StrictModel):
    a: dict
    b: dict


class PlannedValue(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    aggregate_id: str
    group: dict
    metric: str
    stat: Stat
    contrast: PlannedContrast | None = None


class PlannedEvidence(StrictModel):
    type: Literal["aggregate", "figure", "run", "paper", "plan", "log"]
    ref: str
    selector: dict | None = None
    citation_scope: Literal["main_conclusion", "experimental_detail"] | None = None


class PlannedComparison(StrictModel):
    aggregate_id: str
    a: dict
    b: dict
    metric: str
    asserted: Literal["a>b", "a<b", "a≈b"]


class PlannedClaim(StrictModel):
    section: Literal[
        "abstract", "introduction", "related_work", "method", "setup", "results", "discussion", "conclusion"
    ]
    text_template: str
    type: Literal["numeric", "comparative", "qualitative", "citation", "method_fact"]
    statement_kind: Literal["observation", "interpretation"]
    hedge: Literal["none", "likely", "possible"]
    importance: Literal["major", "minor"]
    hypothesis: str | None = None
    values: list[PlannedValue] = []
    comparison: PlannedComparison | None = None
    evidence: list[PlannedEvidence]


class ClaimPlan(StrictModel):
    claims: list[PlannedClaim]


class FigureSource(StrictModel):
    ref: VersionRef
    data: Figure


def load_figures(root: Path, refs: dict[str, VersionRef]) -> dict[str, FigureSource]:
    archive = Archive(root, None)  # read-only Archive operations never emit events
    result = {}
    for fid, ref in sorted(refs.items()):
        if ref.artifact_id != f"artifacts/figures/{fid}":
            raise ValueError("Figure identity mismatch")
        manifest = archive.get(ref)
        stored = archive.stored_path(ref)
        if stored is None or manifest_bytes(dir_manifest(safe_path(stored, root=root), [])) != manifest:
            raise ValueError("Figure archive bytes differ from exact ref")
        figure = Figure.model_validate_json(safe_path(stored / "figure.json", root=root).read_bytes())
        if figure.fig_id != fid:
            raise ValueError("Figure metadata identity mismatch")
        result[fid] = FigureSource(ref=ref, data=figure)
    return result


def hypotheses(bundle):
    return {
        str(h.get("id") or f"H{i}") if isinstance(h, dict) else f"H{i}": h
        for i, h in enumerate((bundle.idea.data or {}).get("hypotheses", []), 1)
    }


def planning_context(bundle: EvidenceBundle, figures: dict[str, FigureSource]) -> dict:
    return {
        "idea": bundle.idea.model_dump(mode="json"),
        "plan": bundle.plan.model_dump(mode="json"),
        "hypotheses": hypotheses(bundle),
        "aggregates": [a.model_dump(mode="json") for a in bundle.aggregates],
        "figures": {fid: f.model_dump(mode="json") for fid, f in sorted(figures.items())},
        "approved_logs": [log.model_dump(mode="json") for log in bundle.logs],
        "literature": [p.model_dump(mode="json") for p in bundle.papers],
        "runtime": bundle.runtime.model_dump(mode="json") if bundle.runtime else None,
        "allowed": {
            "aggregates": [a.aggregate_id for a in bundle.aggregates],
            "figures": sorted(figures),
            "runs": sorted({rid for a in bundle.aggregates for row in a.data.rows for rid in row.run_ids}),
            "papers": [p.paper_id for p in bundle.papers],
            "plan": bundle.plan_ref.model_dump(),
            "logs": [log.ref.model_dump() for log in bundle.logs],
        },
    }


def value_key(selector: dict) -> str:
    """Readable identity plus full digest; collisions are independently checked on installation."""
    readable = ".".join(str(selector[k]) for k in ("aggregate_id", "metric", "stat"))
    readable = re.sub(r"[^A-Za-z0-9_.-]", "_", readable)[:80]
    return readable + "." + sha256_bytes(encoded(selector))


def _format(value: PlannedValue, source, figures):
    if value.stat == "n":
        return {"scale": 1, "decimals": 0, "suffix": ""}
    if value.stat == "p_value":
        return {"scale": 1, "decimals": 4, "suffix": ""}
    specs = [
        f.data.spec
        for f in figures.values()
        if f.data.aggregate_ref == source.ref and f.data.spec.metric == value.metric
    ]
    scales = {spec.scale for spec in specs if spec.ylabel.strip()}
    if len(scales) != 1 or not all(math.isfinite(s) and s > 0 for s in scales):
        raise ValueError("metric presentation scale/unit must come from an exact validated Figure")
    return {"scale": scales.pop(), "decimals": 4, "suffix": ""}


def _bind_value(root, planned, sources, figures):
    source = sources.get(planned.aggregate_id)
    if source is None:
        raise ValueError("value aggregate is outside the frozen bundle")
    # Verify the archived identity, but resolve selectors against the frozen object.
    actual = runs.load_aggregate(root, source.aggregate_id, version=source.ref.version)
    if actual != source.data:
        raise ValueError("frozen Aggregate does not match its archived version")
    contrast = planned.contrast.model_dump() if planned.contrast else None
    if (planned.stat in {"diff_mean", "p_value"}) != (contrast is not None):
        raise ValueError("contrast is required only for contrast statistics")
    if contrast is not None and planned.group:
        raise ValueError("contrast statistics require empty group and exact a/b selectors")
    number = runs.resolve_value(
        root, planned.aggregate_id, planned.group, planned.metric, planned.stat, contrast, aggregate=source.data
    )
    if not math.isfinite(number):
        raise ValueError("selected statistic is unavailable/non-finite")
    selector = planned.model_dump(exclude={"name"})
    fmt = _format(planned, source, figures)
    return ValueRef(
        key=value_key(selector),
        aggregate_id=source.aggregate_id,
        aggregate_version=source.ref.version,
        group=planned.group,
        metric=planned.metric,
        stat=planned.stat,
        contrast=contrast,
        fmt=fmt,
        rendered=evidence.render_value(number, fmt),
    )


def _link(planned, bundle, figures):
    sources = {a.aggregate_id: a for a in bundle.aggregates}
    selector = dict(planned.selector or {})
    if set(selector) - {"group", "metric", "eval_condition"}:
        raise ValueError("unsupported evidence selector fields")
    version = None
    if planned.type == "aggregate":
        source = sources.get(planned.ref)
        if source is None:
            raise ValueError("unknown aggregate evidence")
        version = source.ref.version
        if "eval_condition" in selector and selector["eval_condition"] != source.data.eval_condition:
            raise ValueError("evidence condition differs from Aggregate")
        if "group" in selector and not any(
            r.group == selector["group"] and r.metric == selector.get("metric", r.metric) for r in source.data.rows
        ):
            raise ValueError("evidence group is absent")
        if "metric" in selector and not any(r.metric == selector["metric"] for r in source.data.rows):
            raise ValueError("evidence metric is absent")
    elif planned.type == "figure":
        if planned.ref not in figures or selector:
            raise ValueError("unknown figure evidence or unsupported selector")
        figure = figures[planned.ref]
        if figure.data.aggregate_ref not in [a.ref for a in bundle.aggregates]:
            raise ValueError("figure is outside bundle Aggregate basis")
        version = figure.ref.version
    elif planned.type == "run":
        if (
            planned.ref not in {rid for a in bundle.aggregates for row in a.data.rows for rid in row.run_ids}
            or selector
        ):
            raise ValueError("run does not support selected evidence")
    elif planned.type == "paper":
        if planned.ref not in {p.paper_id for p in bundle.papers} or selector or not planned.citation_scope:
            raise ValueError("unknown paper or missing explicit citation_scope")
        selector = {"citation_scope": planned.citation_scope}
    elif planned.type == "plan":
        if planned.ref != bundle.plan_ref.artifact_id or selector:
            raise ValueError("plan is not the exact approved plan")
        version = bundle.plan_ref.version
    else:
        logs = [log for log in bundle.logs if log.ref.artifact_id == planned.ref]
        if len(logs) != 1 or selector:
            raise ValueError("log is not an approved bundled log")
        version = logs[0].ref.version
    if planned.type != "paper" and planned.citation_scope:
        raise ValueError("citation_scope is only valid for paper evidence")
    return EvidenceLink(type=planned.type, ref=planned.ref, version=version, selector=selector or None)


def _bind_claim(root, planned, claim_id, bundle, figures):
    if planned.section == "results" and planned.statement_kind != "observation":
        raise ValueError("Results claims must be observations")
    if planned.statement_kind == "interpretation" and planned.hedge == "none":
        raise ValueError("interpretation requires likely/possible hedge")
    if planned.hypothesis and planned.hypothesis not in hypotheses(bundle):
        raise ValueError("unknown hypothesis")
    sources = {a.aggregate_id: a for a in bundle.aggregates}
    names = [v.name for v in planned.values]
    template = planned.text_template
    if not template.strip() or len(names) != len(set(names)) or set(TOKEN.findall(template)) != set(names):
        raise ValueError("claim placeholders must match unique planned values")
    clean = active_text(TOKEN.sub("", template))
    if re.search(r"\\(?:claim|airval)\b", clean):
        raise ValueError("model cannot supply claim IDs or airval keys")
    # Exclude citation keys, not visible result values. IDs such as H1/C1 are words.
    numeric_text = re.sub(r"\\cite\{[^}]*\}", "", clean)
    if re.search(r"(?<![\w])[-+]?\d+(?:\.\d+)?", numeric_text):
        raise ValueError("literal claim numbers must be planned placeholders")
    values = [_bind_value(root, v, sources, figures) for v in planned.values]
    links = [_link(link, bundle, figures) for link in planned.evidence]
    if not links:
        raise ValueError("claim requires frozen evidence")
    for v in values:
        if not any(e.type == "aggregate" and e.ref == v.aggregate_id for e in links):
            links.append(EvidenceLink(type="aggregate", ref=v.aggregate_id, version=v.aggregate_version))
    paper_ids = {e.ref for e in links if e.type == "paper"}
    if planned.type == "citation" and not paper_ids:
        raise ValueError("citation claim requires paper evidence")
    allowed = {
        p.record.get("citation_key"): p.paper_id
        for p in bundle.papers
        if p.status in {"verified", "metadata_only"} and p.record.get("bibtex")
    }
    for command in commands(template):
        if command.name == "cite" and any(allowed.get(k.strip()) not in paper_ids for k in command.value.split(",")):
            raise ValueError("citation is not linked to allowed frozen paper evidence")
    comparison = None
    if planned.type == "comparative" and planned.comparison is None:
        raise ValueError("comparative claim needs exact comparison")
    if planned.comparison:
        comp = planned.comparison
        source = sources.get(comp.aggregate_id)
        if source is None or not values or {v.aggregate_id for v in values} != {comp.aggregate_id}:
            raise ValueError("comparison must match the claim's bound Aggregate")
        diff = runs.resolve_value(
            root, comp.aggregate_id, {}, comp.metric, "diff_mean", {"a": comp.a, "b": comp.b}, aggregate=source.data
        )
        if (comp.asserted == "a>b" and diff <= 0) or (comp.asserted == "a<b" and diff >= 0):
            raise ValueError("planned comparison contradicts frozen evidence")
        comparison = Comparison.model_validate(comp.model_dump(exclude={"aggregate_id"}))
    replacements = {name: "\\airval{" + value.key + "}" for name, value in zip(names, values, strict=True)}
    inner = TOKEN.sub(lambda m: replacements[m[1]], template)
    if "[[" in inner or "]]" in inner:
        raise ValueError("unknown claim placeholder syntax")
    # Bind content to its bundle even if newer evidence yields identical display values.
    links.append(
        EvidenceLink(
            type="plan",
            ref=bundle.plan_ref.artifact_id,
            version=bundle.plan_ref.version,
            note="Frozen Writing evidence SHA256: "
            + sha256_bytes(
                encoded(
                    {
                        "bundle_sha256": sha256_bytes(bundle.to_bytes()),
                        "figures": {fid: f.ref.model_dump() for fid, f in sorted(figures.items())},
                    }
                )
            ),
        )
    )
    return Claim(
        claim_id=claim_id,
        text=f"\\claim{{{claim_id}}}{{{inner}}}",
        values=values,
        evidence=links,
        comparison=comparison,
        **planned.model_dump(exclude={"text_template", "values", "evidence", "comparison"}),
    )


def coverage_errors(claims, bundle):
    errors = []
    results = [c for c in claims if c.section == "results" and c.statement_kind == "observation"]
    for hid in hypotheses(bundle):
        if not any(c.hypothesis == hid for c in results):
            errors.append(f"coverage: hypothesis {hid} has no Results observation")
    for source in bundle.aggregates:
        matching = [c for c in results if any(v.aggregate_id == source.aggregate_id for v in c.values)]
        if not matching:
            errors.append(f"coverage: comparison {source.aggregate_id} absent from Results")
        for contrast in source.data.contrasts:
            if not any(
                c.comparison
                and c.comparison.a == contrast.a
                and c.comparison.b == contrast.b
                and c.comparison.metric == contrast.metric
                for c in matching
            ):
                errors.append(f"coverage: contrast {source.aggregate_id} {contrast.a} vs {contrast.b} absent")
    return errors


def bind_plan(root: Path, plan: dict, bundle: EvidenceBundle, figures: dict[str, FigureSource]):
    """Collect per-slot errors, preserving CL<n> identities even when optional slots are dropped."""
    if bundle.diagnostics:
        raise ValueError("cannot bind claims to inconsistent evidence lineage")
    if set(plan) != {"claims"} or not isinstance(plan["claims"], list):
        raise ValueError("claim plan must contain only the claims list")
    claims, errors = [], []
    for i, raw in enumerate(plan["claims"], 1):
        try:
            claims.append(_bind_claim(root, PlannedClaim.model_validate(raw), f"CL{i}", bundle, figures))
        except (AirError, ValueError, KeyError, OSError) as exc:
            errors.append(f"CL{i}: {exc}")
    try:
        unique_values(claims)
    except ValueError as exc:
        errors.append(f"binding: {exc}")
    return claims, errors + coverage_errors(claims, bundle)


def unique_values(claims):
    values = {}
    for claim in claims:
        for value in claim.values:
            if not SAFE_KEY.fullmatch(value.key):
                raise ValueError("unsafe ValueRef key")
            if value.key in values and values[value.key] != value:
                raise ValueError(f"conflicting ValueRef key collision: {value.key}")
            values[value.key] = value
    return dict(sorted(values.items()))


def claims_bytes(claims):
    if len({c.claim_id for c in claims}) != len(claims):
        raise ValueError("duplicate claim IDs")
    unique_values(claims)
    return b"".join(encoded(c.model_dump(mode="json")) + b"\n" for c in claims)


def values_tex(claims):
    lines = [
        "% Code-generated evidence values; do not edit.",
        r"\makeatletter",
        r"\newcommand{\airval}[1]{\@ifundefined{airval@#1}{\textbf{??#1??}}{\@nameuse{airval@#1}}}",
        r"\makeatother",
    ]
    for key, value in unique_values(claims).items():
        if not re.fullmatch(r"-?\d+(?:\.\d+)?", value.rendered):
            raise ValueError("bound numeric macro must contain only deterministic numeric text")
        lines.append(r"\expandafter\def\csname airval@" + key + r"\endcsname{" + value.rendered + "}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def insert_markers(text, claims, section):
    expected = {c.claim_id: c for c in claims if c.section == section}
    active = active_text(text)
    if re.search(r"\\(?:claim|airval)\b", active):
        raise ValueError("model must use claim markers, not claim/airval commands")
    found = MARKER.findall(active)
    if len(found) != len(set(found)) or set(found) != set(expected):
        raise ValueError("missing, duplicate, unknown or wrong-section claim marker")
    # Only active markers count; comments/literals cannot satisfy required claims.
    spans = list(MARKER.finditer(active))
    for match in reversed(spans):
        text = text[: match.start()] + expected[match[1]].text + text[match.end() :]
    if "[[CLAIM:" in active_text(text):
        raise ValueError("malformed claim marker")
    return text


def validate_bound_section(text, claims, section):
    """Exact active wrappers, keys and multiplicity; surrounding prose remains editable."""
    expected = [c for c in claims if c.section == section]
    remainder = text
    for c in expected:
        starts = [m.start() for m in re.finditer(re.escape(c.text), text)]
        starts = [i for i in starts if active_text(text)[i : i + 6] == r"\claim"]
        if len(starts) != 1:
            raise ValueError(f"bound claim {c.claim_id} missing, duplicated or altered; reconcile explicitly")
        start = starts[0]
        remainder = remainder[:start] + " " * len(c.text) + remainder[start + len(c.text) :]
    if re.search(r"\\(?:claim|airval)\b|\[\[CLAIM:", active_text(remainder)):
        raise ValueError("unknown/wrong-section claim or unbound airval key")


def locate_claims(root, claims):
    located = []
    for section in SECTIONS:
        text = safe_path(Path(root) / f"paper/sections/{section}.tex", root=root).read_text(encoding="utf-8")
        validate_bound_section(text, claims, section)
        for claim in claims:
            if claim.section == section:
                located.append(
                    claim.model_copy(
                        update={
                            "location": {
                                "file": f"paper/sections/{section}.tex",
                                "line": text[
                                    : next(
                                        m.start()
                                        for m in re.finditer(re.escape(claim.text), text)
                                        if active_text(text)[m.start() : m.start() + 6] == r"\claim"
                                    )
                                ].count("\n")
                                + 1,
                            }
                        }
                    )
                )
    by_id = {c.claim_id: c for c in located}
    return [by_id[c.claim_id] for c in claims]


def validate_preamble(text):
    active = active_text(text)
    if active.count(PREAMBLE) != 1:
        raise ValueError("bound manuscript must retain the exact code-owned evidence preamble")
    remainder = active.replace(PREAMBLE, "", 1)
    if re.search(r"\\(?:claim|airval)\b|\b(?:airval@|air_values)\b|\\csname", remainder):
        raise ValueError("manuscript redefines/overrides reserved evidence macros")
