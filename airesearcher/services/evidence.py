"""Deterministic manuscript/evidence verification and exact provenance tracing.

No Stage, gateway, online citation lookup or semantic review. audit() is deferred.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path, PureWindowsPath

from airesearcher.core.archive import Archive, dir_manifest, manifest_bytes
from airesearcher.core.errors import AirError, NotFound
from airesearcher.core.events import Events
from airesearcher.core.fsutil import now, read_json, read_jsonl
from airesearcher.core.models.aggregate import Aggregate
from airesearcher.core.models.claim import CheckReport, Claim, ClaimCheck, EvidenceTrace, Issue
from airesearcher.core.models.common import VersionRef
from airesearcher.core.models.figure import Figure
from airesearcher.core.models.plan import parse_plan_md

from . import literature, runs
from ._tex import active_text, commands

CITE_RE = re.compile(r"\\cite[pt]?\*?(?:\[[^\]]*\])?\{([^}]*)\}")
NUMBER = re.compile(r"(?<![\w.])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?(?!\w|\.\d)")
MACRO = re.compile(r"\\expandafter\s*\\def\s*\\csname\s+airval@([^\s{}]+)\s*\\endcsname\s*\{([^{}]*)\}")
PAPER_EXCLUDE = [
    "build/**",
    "review/**",
    ".writing/**",
    ".template_check_default.json",
    ".template_compile_default.json",
    ".template_compile_default.log",
]


def render_value(value: float, fmt: dict) -> str:
    """One numeric formatting authority for binding and independent recomputation."""
    if value is None or not math.isfinite(float(value)):
        return "??"
    v = value * float(fmt.get("scale", 1))
    return f"{v:.{int(fmt.get('decimals', 1))}f}{fmt.get('suffix', '')}"


def load_claims(root: Path) -> list[Claim]:
    return [Claim.model_validate(r) for r in read_jsonl(Path(root) / "paper" / "claims.jsonl")]


def _source(root, paper_ref):
    archive = Archive(root, None)
    if paper_ref is not None:
        if paper_ref.artifact_id != "paper":
            raise ValueError("paper_ref must identify the manuscript directory")
        manifest = archive.get(paper_ref)
        info = archive.version_info(paper_ref)
        source = archive.stored_path(paper_ref)
        if not info.is_dir or source is None or manifest_bytes(dir_manifest(source, info.exclude)) != manifest:
            raise ValueError("archived manuscript bytes do not match paper_ref")
    else:
        source = Path(root) / "paper"
        latest = archive.latest_version("paper")
        sha = archive.current_sha("paper", latest.exclude if latest else PAPER_EXCLUDE) or ""
        paper_ref = (
            latest.ref
            if latest and latest.ref.sha256 == sha
            else VersionRef(artifact_id="paper", version=0, sha256=sha)
        )
    return archive, source, paper_ref


def _bundle(source):
    path = source / "evidence_bundle.json"
    return read_json(path) if path.exists() else {}


def _papers(root, source, bundle):
    frozen = bundle.get("papers", [])
    if "schema_version" in bundle:
        return {
            p["paper_id"]: p["record"]
            for p in frozen
            if "record" in p and p.get("status") in {"verified", "metadata_only"}
        }
    # Legacy fake bundles contain only IDs, without historical PaperRecords.
    # Restrict fallback identities to those IDs and the manuscript's own BibTeX.
    bib = (source / "references.bib").read_text(encoding="utf-8") if (source / "references.bib").exists() else ""
    keys = set(re.findall(r"(?mi)^\s*@\w+\s*\{\s*([^,]+),", bib))
    ids = {p["paper_id"] for p in frozen}
    return {
        pid: p.model_dump(mode="json")
        for pid, p in literature.load_papers(root).items()
        if (not frozen or pid in ids) and p.citation_key in keys
    }


def _support(issues, unverifiable):
    if any(i.code == "CONTRADICTED" for i in issues):
        return "contradicted"
    if unverifiable:
        return "unverifiable"
    if any(i.severity == "blocker" for i in issues):
        return "unsupported"
    if any(i.severity == "major" for i in issues):
        return "partially_supported"
    return "supported"


def _rows(aggregate, value):
    if value.stat in {"diff_mean", "p_value"}:
        groups = [value.contrast["a"], value.contrast["b"]] if value.contrast else []
    else:
        groups = [value.group]
    rows = [r for r in aggregate.rows if r.metric == value.metric and r.group in groups]
    if len(rows) != len(groups) or not rows:
        raise ValueError("exact supporting Aggregate rows are unavailable")
    return rows


def _numeric_surface(text):
    clean = list(active_text(text))
    for command in commands(text):
        # claim IDs are hidden, but the second argument (actual claim prose) stays visible.
        if command.name in {
            "airval",
            "claim",
            "cite",
            "citep",
            "citet",
            "ref",
            "pageref",
            "label",
            "includegraphics",
            "setcounter",
            "addtocounter",
            "hspace",
            "vspace",
            "rule",
        }:
            clean[command.start : command.end] = [c if c in "\r\n" else " " for c in text[command.start : command.end]]
    surface = "".join(clean)
    # Counter arguments and explicit heading numbering are layout, not measurements.
    for match in re.finditer(r"\\(?:setcounter|addtocounter)\s*\{[^{}]*\}\s*\{[+-]?\d+\}", active_text(text)):
        clean[match.start() : match.end()] = [c if c in "\r\n" else " " for c in text[match.start() : match.end()]]
    for match in re.finditer(r"\\(?:section|subsection|subsubsection)\*?\s*\{\s*(\d+(?:\.\d+)*[.)]?)(?=\s)", surface):
        clean[match.start(1) : match.end(1)] = " " * len(match[1])
    return "".join(clean)


def _compile_ok(root, archive, paper_ref, explicit):
    if paper_ref.version:
        for info in reversed(archive.versions("paper/build/compile_report.json")):
            if paper_ref in info.parents:
                try:
                    return bool(json.loads(archive.get(info.ref))["ok"])
                except (AirError, ValueError, KeyError, OSError):
                    return False
    if not explicit:
        path = Path(root) / "paper/build/compile_report.json"
        return bool(read_json(path).get("ok")) if path.exists() else False
    return False


def check(root: Path, paper_ref: VersionRef | None = None, mode: str = "full") -> CheckReport:
    root = Path(root)
    explicit = paper_ref is not None
    archive, source, paper_ref = _source(root, paper_ref)
    bundle = _bundle(source)
    papers = _papers(root, source, bundle)
    allowed_cites = {p.get("citation_key") for p in papers.values()}
    frozen = {a["aggregate_id"]: VersionRef.model_validate(a["ref"]) for a in bundle.get("aggregates", [])}
    claims = [Claim.model_validate(r) for r in read_jsonl(source / "claims.jsonl")]
    macro_path = source / "air_values.tex"
    macro_text = macro_path.read_text(encoding="utf-8") if macro_path.exists() else ""
    macros = {}
    for match in MACRO.finditer(active_text(macro_text)):
        macros.setdefault(match[1], []).append(match[2])
    dispatcher = r"\newcommand{\airval}[1]{\@ifundefined{airval@#1}{\textbf{??#1??}}{\@nameuse{airval@#1}}}"
    residual = MACRO.sub("", active_text(macro_text))
    macro_shape_ok = residual.count(dispatcher) == 1
    residual = residual.replace(dispatcher, "").replace(r"\makeatletter", "").replace(r"\makeatother", "")
    macro_shape_ok = macro_shape_ok and not residual.strip()
    cache = {}

    def aggregate(aid, version):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", aid) or version < 1:
            raise ValueError("invalid Aggregate identity/version")
        key = (aid, version)
        if key not in cache:
            stored = runs.load_aggregate(root, aid, version=version)
            cache[key] = stored, runs.recompute_aggregate(root, aid, version=version)
        return cache[key]

    checks = []
    for claim in claims:
        issues, recomputed, selected_rows = [], {}, []
        unavailable = False

        def issue(code, severity, detail, claim=claim, issues=issues):
            item = Issue(
                code=code,
                severity=severity,
                claim_id=claim.claim_id,
                location=claim.location or {},
                sentence=claim.text,
                detail=detail,
            )
            if item not in issues:
                issues.append(item)

        for value in claim.values:
            if not macro_shape_ok or macros.get(value.key) != [value.rendered]:
                issue("NUM_MISMATCH", "blocker", f"Missing, duplicate or altered air_values macro: {value.key}")
            expected = frozen.get(value.aggregate_id)
            if frozen and (expected is None or expected.version != value.aggregate_version):
                issue(
                    "STALE_EVIDENCE",
                    "major",
                    f"{value.aggregate_id} v{value.aggregate_version} differs from frozen bundle",
                )
            try:
                stored, recalculated = aggregate(value.aggregate_id, value.aggregate_version)
                if expected and expected.version == value.aggregate_version:
                    archive.get(expected)
                plan = parse_plan_md(archive.read_text(stored.plan_ref))
                rows = _rows(stored, value)
                selected_rows.extend(rows)
                for rid in sorted({rid for row in rows for rid in row.run_ids}):
                    try:
                        if not re.fullmatch(r"[A-Za-z0-9_-]+", rid):
                            raise ValueError("unsafe run identity")
                        view = runs.load_run(root, rid)
                        metrics = [m.value for m in view.metrics if m.name == value.metric]
                        wrong_plan = (
                            view.record.plan_ref.sha256 != stored.plan_ref.sha256 and rid not in plan.reuse_runs
                        )
                        missing_metric = not metrics or not math.isfinite(metrics[-1])
                        unavailable = unavailable or missing_metric
                        if (
                            view.status.state.value != "succeeded"
                            or view.record.kind in {"trial", "smoke"}
                            or (view.label and view.label.label == "invalid")
                            or wrong_plan
                            or missing_metric
                        ):
                            issue("INVALID_RUN", "blocker", f"Invalid supporting run: {rid}")
                        if view.record.eval_condition != stored.eval_condition:
                            issue("CONDITION_MISMATCH", "major", f"Run/Aggregate condition mismatch: {rid}")
                    except (AirError, ValueError, OSError):
                        unavailable = True
                        issue("INVALID_RUN", "blocker", f"Missing/unreadable supporting run: {rid}")
                number = runs.resolve_value(
                    root,
                    value.aggregate_id,
                    value.group,
                    value.metric,
                    value.stat,
                    value.contrast,
                    aggregate=recalculated,
                )
                rendered = render_value(number, value.fmt)
                recomputed[value.key] = rendered
                if rendered != value.rendered:
                    issue("NUM_MISMATCH", "blocker", f"{value.key}: claimed {value.rendered}, recomputed {rendered}")
                if (
                    stored.eval_condition == "explore"
                    and claim.importance == "major"
                    and not re.search(r"explor\w*|探索", active_text(claim.text), re.I)
                ):
                    issue("CONDITION_MISMATCH", "major", "Major exploratory claim lacks explicit exploratory wording")
            except (AirError, ValueError, OSError, KeyError, AttributeError) as exc:
                unavailable = True
                issue("NUM_MISMATCH", "blocker", f"Required exact numerical evidence unavailable: {value.key}: {exc}")
        counts = {row.n for row in selected_rows}
        surface = _numeric_surface(claim.text)
        for m in re.finditer(r"\bn\s*=\s*(\d+)\b|\b(\d+)\s+(?:seeds?|runs?|samples?)\b", surface, re.I):
            stated = int(m[1] or m[2])
            if counts and counts != {stated}:
                issue("N_MISMATCH", "major", f"Explicit n={stated} differs from supporting row counts {sorted(counts)}")
        if claim.comparison:
            try:
                if not claim.values or len({(v.aggregate_id, v.aggregate_version) for v in claim.values}) != 1:
                    raise ValueError("comparison has no unique bound Aggregate")
                value = claim.values[0]
                _, agg = aggregate(value.aggregate_id, value.aggregate_version)
                comp = claim.comparison
                contrast = {"a": comp.a, "b": comp.b}
                diff = runs.resolve_value(
                    root, value.aggregate_id, {}, comp.metric, "diff_mean", contrast, aggregate=agg
                )
                if (comp.asserted == "a>b" and diff <= 0) or (comp.asserted == "a<b" and diff >= 0):
                    issue("CONTRADICTED", "blocker", f"Assertion {comp.asserted} contradicts exact difference {diff}")
                try:
                    p = runs.resolve_value(
                        root, value.aggregate_id, {}, comp.metric, "p_value", contrast, aggregate=agg
                    )
                except (AirError, ValueError):
                    p = None
                wording = active_text(claim.text)
                significant = re.search(r"\b(?:statistically significant|significantly)\b|显著", wording, re.I)
                negated = re.search(r"\b(?:not|non)[ -]?(?:statistically )?significant\b|不显著", wording, re.I)
                rows = [r for r in agg.rows if r.metric == comp.metric and r.group in [comp.a, comp.b]]
                overlap = (
                    len(rows) == 2
                    and all(r.ci95 for r in rows)
                    and max(r.ci95[0] for r in rows) <= min(r.ci95[1] for r in rows)
                )
                strong = re.search(r"\b(?:outperforms?|superior|better than)\b|优于", wording, re.I)
                if (significant and not negated and (p is None or p >= 0.05)) or (
                    strong and overlap and claim.hedge == "none"
                ):
                    issue(
                        "OVERCLAIM_SIGNIFICANCE",
                        "major",
                        "Explicit significance/superiority exceeds p-value/CI support",
                    )
            except (AirError, ValueError, OSError, KeyError):
                unavailable = True
                issue("NUM_MISMATCH", "blocker", "Exact comparison evidence is unavailable")
        for link in claim.evidence:
            if link.type == "paper":
                record = papers.get(link.ref)
                if record is None:
                    unavailable = True
                    issue("CITATION_NOT_IN_ARCHIVE", "blocker", f"Paper outside frozen literature: {link.ref}")
                elif (link.selector or {}).get("citation_scope") == "experimental_detail" and record.get(
                    "access_level"
                ) != "full_text":
                    issue("CITATION_ABSTRACT_ONLY", "minor", f"Experimental detail uses abstract/metadata: {link.ref}")
            elif link.type == "run":
                try:
                    view = runs.load_run(root, link.ref, with_metrics=False)
                    if (
                        view.status.state.value != "succeeded"
                        or view.record.kind in {"trial", "smoke"}
                        or (view.label and view.label.label == "invalid")
                    ):
                        issue("INVALID_RUN", "blocker", f"Invalid linked run: {link.ref}")
                except (AirError, ValueError, OSError):
                    unavailable = True
                    issue("INVALID_RUN", "blocker", f"Missing linked run: {link.ref}")
            elif link.type in {"aggregate", "figure", "plan", "log"}:
                aid = (
                    f"artifacts/aggregates/{link.ref}.json"
                    if link.type == "aggregate"
                    else f"artifacts/figures/{link.ref}"
                    if link.type == "figure"
                    else link.ref
                )
                try:
                    infos = archive.versions(aid)
                    info = next((i for i in infos if i.ref.version == link.version), None)
                    if info is None and link.version is None and "schema_version" not in bundle:
                        info = infos[-1] if infos else None  # legacy fixture links omitted versions
                    if info is None:
                        raise ValueError("missing exact linked version")
                    content = archive.get(info.ref)
                    if info.is_dir:
                        path = archive.stored_path(info.ref)
                        if path is None or manifest_bytes(dir_manifest(path, info.exclude)) != content:
                            raise ValueError("linked archive bytes differ")
                    if link.type == "aggregate":
                        expected = frozen.get(link.ref)
                        if expected and expected != info.ref:
                            issue("STALE_EVIDENCE", "major", "Aggregate link differs from frozen bundle")
                        stored = Aggregate.model_validate_json(content)
                        condition = (link.selector or {}).get("eval_condition")
                        if condition and condition != stored.eval_condition:
                            issue("CONDITION_MISMATCH", "major", "Evidence condition differs from exact Aggregate")
                        if (
                            stored.eval_condition == "explore"
                            and claim.importance == "major"
                            and not re.search(r"explor\w*|探索", active_text(claim.text), re.I)
                        ):
                            issue("CONDITION_MISMATCH", "major", "Major exploratory claim lacks exploratory wording")
                except (AirError, ValueError, OSError, TypeError):
                    unavailable = True
                    issue("STALE_EVIDENCE", "major", f"Required linked evidence unavailable: {aid}")
        checks.append(
            ClaimCheck(
                claim_id=claim.claim_id,
                support=_support(issues, unavailable),
                issues=sorted(issues, key=lambda i: (i.code, i.detail)),
                recomputed=recomputed,
            )
        )

    unbound = []
    for tex in sorted((source / "sections").glob("*.tex")):
        text = tex.read_text(encoding="utf-8")
        active = active_text(text)
        surface = _numeric_surface(text)
        location_file = "paper/sections/" + tex.name
        for match in NUMBER.finditer(surface):
            literal = match[0]
            if re.fullmatch(r"(?:18|19|20|21)\d{2}", literal):
                continue
            line = text.count("\n", 0, match.start()) + 1
            matches = []
            tolerance = 0.5 * 10 ** (-len(literal.partition(".")[2])) if "." in literal else 0.5
            for claim in claims:
                for value in claim.values:
                    try:
                        if abs(float(literal) - float(value.rendered)) <= tolerance:
                            matches.append(value.key)
                    except ValueError:
                        pass
            unbound.append(
                Issue(
                    code="NUM_UNBOUND",
                    severity="major" if tex.stem in {"abstract", "results"} else "minor",
                    location={"file": location_file, "line": line},
                    sentence=text.splitlines()[line - 1].strip(),
                    detail=f"Literal {literal} is not bound through airval; possible keys: {sorted(set(matches))}",
                )
            )
        for match in CITE_RE.finditer(active):
            for key in sorted(set(k.strip() for k in match[1].split(",") if k.strip())):
                if key not in allowed_cites:
                    line = text.count("\n", 0, match.start()) + 1
                    unbound.append(
                        Issue(
                            code="CITATION_NOT_IN_ARCHIVE",
                            severity="blocker",
                            location={"file": location_file, "line": line},
                            sentence=text.splitlines()[line - 1].strip(),
                            detail=f"Citation key outside frozen literature: {key}",
                        )
                    )
    counts = {"blocker": 0, "major": 0, "minor": 0}
    for issue in [*(i for c in checks for i in c.issues), *unbound]:
        counts[issue.severity] += 1
    for claim in checks:
        counts[claim.support] = counts.get(claim.support, 0) + 1
    return CheckReport(
        paper_ref=paper_ref,
        mode=mode if mode in {"full", "audit"} else "full",
        claims=checks,
        unbound_issues=sorted(unbound, key=lambda i: (i.location["file"], i.location["line"], i.code, i.detail)),
        counts=counts,
        compile_ok=_compile_ok(root, archive, paper_ref, explicit),
        created_at=now(),
    )


def _safe_runtime(value):
    if isinstance(value, dict):
        return {
            k: _safe_runtime(v)
            for k, v in value.items()
            if not re.search(r"secret|token|password|credential|api.?key|authorization", k, re.I)
        }
    if isinstance(value, list):
        return [_safe_runtime(v) for v in value]
    if isinstance(value, str) and (Path(value).is_absolute() or PureWindowsPath(value).is_absolute()):
        return "<external-path>"
    return value


def trace(root: Path, claim_id: str) -> EvidenceTrace:
    root = Path(root)
    archive = Archive(root, None)
    ref = archive.latest("paper")
    archive, source, paper_ref = _source(root, ref)
    claims = {c.claim_id: c for c in (Claim.model_validate(r) for r in read_jsonl(source / "claims.jsonl"))}
    if claim_id not in claims:
        raise NotFound(f"Claim not found in current manuscript: {claim_id}")
    claim = claims[claim_id]
    bundle = _bundle(source)
    frozen_runtime = {r["run_id"]: r for r in (bundle.get("runtime") or {}).get("runs", [])}
    aggregates, ids, figures = [], set(), []
    for value in claim.values:
        stored = runs.load_aggregate(root, value.aggregate_id, version=value.aggregate_version)
        rec = runs.recompute_aggregate(root, value.aggregate_id, version=value.aggregate_version)
        number = runs.resolve_value(
            root, value.aggregate_id, value.group, value.metric, value.stat, value.contrast, aggregate=rec
        )
        aggregates.append(
            {**value.model_dump(), "version": value.aggregate_version, "recomputed": render_value(number, value.fmt)}
        )
        ids.update(rid for row in _rows(stored, value) for rid in row.run_ids)
    papers = _papers(root, source, bundle)
    cited = []
    for link in claim.evidence:
        if link.type == "figure":
            aid = f"artifacts/figures/{link.ref}"
            info = next((v for v in archive.versions(aid) if v.ref.version == link.version), None)
            if info is None and link.version is None:  # legacy fake claims did not bind a version
                paper_info = archive.version_info(paper_ref) if paper_ref.version else None
                candidates = [r for r in paper_info.parents if r.artifact_id == aid] if paper_info else []
                info = archive.version_info(candidates[0]) if len(candidates) == 1 else archive.latest_version(aid)
            if info:
                manifest = archive.get(info.ref)
                path = archive.stored_path(info.ref)
                if path is None or manifest_bytes(dir_manifest(path, info.exclude)) != manifest:
                    raise ValueError("Figure archive differs from exact linked version")
                fig = Figure.model_validate(read_json(path / "figure.json"))
                figures.append({**fig.model_dump(mode="json"), "ref": info.ref.model_dump()})
        elif link.type == "paper" and link.ref in papers:
            record = papers[link.ref]
            cited.append(
                {
                    "paper_id": link.ref,
                    "citation_key": record.get("citation_key"),
                    "access_level": record.get("access_level"),
                    "verification": "local_offline",
                    "title": record.get("title"),
                }
            )
    run_briefs = []
    for rid in sorted(ids):
        if rid in frozen_runtime:
            raw = frozen_runtime[rid]
            brief = {k: raw.get(k) for k in ("run_id", "status", "code_commit", "config_sha256", "environment")}
        else:
            view = runs.load_run(root, rid, with_metrics=False)
            env = view.environment or {}
            brief = {
                "run_id": rid,
                "state": view.status.state.value,
                "code_commit": view.record.code_commit,
                "config_sha256": view.record.config_sha256,
                "environment": {k: env[k] for k in ("os", "python", "hardware", "frameworks", "unknown") if k in env},
            }
        env = brief.get("environment") or {}
        brief["environment"] = {k: env[k] for k in ("os", "python", "hardware", "frameworks", "unknown") if k in env}
        run_briefs.append(_safe_runtime(brief))
    report = check(root, paper_ref=paper_ref if paper_ref.version else None)
    result = next((c for c in report.claims if c.claim_id == claim_id), None)
    return EvidenceTrace(
        claim=claim, aggregates=aggregates, figures=figures, runs=run_briefs, papers=cited, check=result
    )


def audit(root: Path, llm: object) -> CheckReport:
    """Semantic extraction/evaluation remains deferred beyond Phase 2B-1."""
    raise NotImplementedError("evidence.audit() remains deferred to evaluation")


# ---------------------------------------------------------------- ARCHIVE_INDEX.md（详细设计 4 第 10.2 节）
_CLAIM_WRAP = re.compile(r"^\\claim\{[^}]*\}\{(.*)\}$", re.S)
_AIRVAL = re.compile(r"\\airval\{([^}]*)\}")


def _cell(text: str, limit: int = 90) -> str:
    text = " ".join(str(text).split()).replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _latest_review(root: Path) -> CheckReport | None:
    reviews = sorted((Path(root) / "paper" / "review").glob("review_v*.json"),
                     key=lambda p: int(re.sub(r"\D", "", p.stem) or 0))
    if not reviews:
        return None
    try:
        return CheckReport.model_validate(read_json(reviews[-1]))
    except ValueError:
        return None


def archive_index(root: Path, project_id: str = "", title: str = "") -> str:
    """导出时附带的对照表：图表 → 汇总 → 运行编号、论断 → 证据、运行清单。只读文件。"""
    root = Path(root)
    arch = Archive(root, Events(root))
    lines = [f"# 研究档案索引：{title or project_id}", "",
             f"项目 `{project_id}`，生成于 {now().isoformat(timespec='seconds')}。", ""]
    refs = []
    for aid, name in (("idea/selected.md", "研究问题"), ("plan/experiment_plan.md", "实验计划"), ("paper", "论文")):
        ref = arch.latest(aid)
        if ref is not None:
            refs.append(f"- {name}：`{aid}` v{ref.version}（sha256 `{ref.sha256[:12]}`）")
    lines += refs + ([""] if refs else [])

    labels = runs.load_labels(root)
    lines += ["## 1. 图表 → 汇总结果 → 运行", "",
              "| 图 | 标题 | 汇总结果 | 每组样本数 | 运行 |", "|---|---|---|---|---|"]
    fig_dir = root / "artifacts" / "figures"
    figs = sorted(fig_dir.glob("*/figure.json")) if fig_dir.exists() else []
    for p in figs:
        try:
            fig = Figure.model_validate(read_json(p))
        except ValueError:
            continue
        n = ", ".join(f"{k}={v}" for k, v in fig.n_per_group.items())
        lines.append(f"| `{fig.fig_id}` | {_cell(fig.spec.title, 50)} | `{fig.spec.aggregate_id}` "
                     f"v{fig.aggregate_ref.version} | {n} | {', '.join(f'`{r}`' for r in fig.run_ids)} |")
    if not figs:
        lines.append("| （还没有图表） | | | | |")

    lines += ["", "## 2. 汇总结果 → 运行", "",
              "| 汇总 | 组 | 指标 | n | 均值 | 95% 区间 | 运行 |", "|---|---|---|---|---|---|---|"]
    agg_dir = root / "artifacts" / "aggregates"
    aggs = sorted(agg_dir.glob("*.json")) if agg_dir.exists() else []
    excluded: list[str] = []
    for p in aggs:
        try:
            agg = Aggregate.model_validate(read_json(p))
        except ValueError:
            continue
        for r in agg.rows:
            ci = f"[{r.ci95[0]:.4f}, {r.ci95[1]:.4f}]" if r.ci95 else "—"
            group = ", ".join(f"{k}={v}" for k, v in r.group.items())
            run_cells = ", ".join(f"`{x}`" + ("（可疑）" if x in r.suspicious else "") for x in r.run_ids)
            lines.append(f"| `{agg.aggregate_id}` | {group} | {r.metric} | {r.n} | {r.mean:.4f} | {ci} | {run_cells} |")
        excluded += [f"- `{agg.aggregate_id}` 排除 `{e['run_id']}`：{e['reason']}" for e in agg.excluded]
    if not aggs:
        lines.append("| （还没有汇总结果） | | | | | | |")
    if excluded:
        lines += ["", "排除的运行：", "", *excluded]

    lines += ["", "## 3. 论断 → 证据", "", "| 论断 | 章节 | 内容 | 证据 | 核验结论 |", "|---|---|---|---|---|"]
    review = _latest_review(root)
    verdict = {c.claim_id: c.support for c in review.claims} if review else {}
    try:
        claims = load_claims(root)
    except (ValueError, FileNotFoundError):
        claims = []
    for c in claims:
        m = _CLAIM_WRAP.match(c.text.strip())
        text = _AIRVAL.sub(lambda x: f"[{x.group(1)}]", m.group(1) if m else c.text).replace("\\%", "%")
        ev = ", ".join(f"{e.type}:`{e.ref}`" + (f" v{e.version}" if e.version else "") for e in c.evidence)
        lines.append(f"| `{c.claim_id}` | {c.section} | {_cell(text)} | {ev} | {verdict.get(c.claim_id, '未核验')} |")
    if not claims:
        lines.append("| （还没有论断） | | | | |")
    if review:
        lines += ["", f"核验报告：`paper/review/` 最新一版，问题数 {review.counts}。"]

    lines += ["", "## 4. 运行清单", "",
              "| 运行 | 任务 | 类型 | 状态 | 人工标注 | 代码提交 | 配置 sha256 | 重试自 |",
              "|---|---|---|---|---|---|---|---|"]
    for rv in runs.list_runs(root):
        rec, st = rv.record, rv.status
        lab = labels.get(rec.run_id)
        state = st.state.value + (f"（{st.failure_reason}）" if st.failure_reason else "")
        lines.append(f"| `{rec.run_id}` | {rec.task_key} | {rec.kind} | {state} | "
                     f"{(lab.label + '：' + _cell(lab.reason, 40)) if lab else ''} | `{rec.code_commit[:10]}` | "
                     f"`{rec.config_sha256[:12]}` | {rec.retry_of or ''} |")

    cited = sorted({pid for c in claims for e in c.evidence if e.type == "paper" for pid in [e.ref]})
    papers = literature.load_papers(root)
    if papers:
        lines += ["", "## 5. 文献", "", "| 文献 | 引用键 | 标题 | 链接 | 论文中引用 |", "|---|---|---|---|---|"]
        for pid, rec in sorted(papers.items()):
            lines.append(f"| `{pid}` | `{rec.citation_key}` | {_cell(rec.title, 60)} | {rec.url} | "
                         f"{'✓' if pid in cited else ''} |")
    return "\n".join(lines) + "\n"
