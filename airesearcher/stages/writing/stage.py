"""Resumable real Writing control arm. Shared engine owns checkpoints and approvals.

Writing-owned receipts close completed-call/file-write gaps. They are not manuscript
evidence and are excluded from its directory archive. No Phase 2B functionality.
"""

from __future__ import annotations

import json
from pathlib import PurePosixPath

from pydantic import ConfigDict

from airesearcher.core.archive import dir_manifest, manifest_bytes
from airesearcher.core.errors import AirError
from airesearcher.core.fsutil import atomic_write_bytes, atomic_write_json, atomic_write_text, sha256_bytes, sha256_file
from airesearcher.core.models.claim import CompileReport
from airesearcher.core.models.common import VersionRef
from airesearcher.core.models.figure import Figure, FigureSpec
from airesearcher.core.models.plan import parse_plan_md
from airesearcher.core.models.question import Question, QuestionOption
from airesearcher.engine.stage import Continue, Error, NeedsApproval, StageContext, StepResult, Stop
from airesearcher.llm.gateway import LLMOutputError
from airesearcher.services import literature, runs

from ._latex import TemplateError, safe_path
from .bundle import ApprovedSource, EvidenceBundle, _runtime_input, build_bundle, write_bundle
from .compile import compile_paper
from .figures import default_spec, generate_figure, prepare_data, validate_artifact
from .template import check_template, inspect_template, resolve_template, write_check
from .writer import GENERATION_ORDER, PROMPT_ID, SECTIONS, SectionOutput, Writer, _section_bytes

PAPER_EXCLUDE = [
    "build/**",
    "review/**",
    ".template_check_default.json",
    ".template_compile_default.json",
    ".template_compile_default.log",
    ".writing/**",
]
FIGURE_PROMPT = "writing/figure_spec"
REPAIR_PROMPT = "writing/compile_repair"
_PHASES = ("bundle", "figures", "template", "write", "compile", "submit")


class PlannedFigure(FigureSpec):
    model_config = ConfigDict(extra="forbid")


def _hash(value: object) -> str:
    return sha256_bytes(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def _ref(value: dict) -> VersionRef:
    return VersionRef.model_validate(value)


def _refs(values):
    unique = {(r.artifact_id, r.version, r.sha256): r for r in values}
    return [unique[key] for key in sorted(unique)]


def _path(ctx, relative):
    return safe_path(ctx.root / relative)


def _receipt(ctx, name):
    path = _path(ctx, f"paper/.writing/{name}.json")
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _save(ctx, name, value):
    atomic_write_json(_path(ctx, f"paper/.writing/{name}.json"), value)


def _tex(text):
    return "".join(
        {"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}.get(
            c, "\\" + c if c in "_%&#$" else c
        )
        for c in str(text)
    )


class WritingStage:
    name = "writing"

    def step(self, ctx: StageContext) -> StepResult:
        if ctx.project.evidence_check:
            return Error("Real Writing evidence_check=true is not implemented until Phase 2B.", retryable=False)
        try:
            return self._step(ctx)
        except (AirError, ValueError, OSError, KeyError, LLMOutputError) as exc:
            return Error(f"Writing stopped: {exc}", retryable=False)

    def _step(self, ctx):
        paper = _path(ctx, "paper")
        if (
            (paper / "claims.jsonl").exists()
            or (paper / "air_values.tex").exists()
            or list((paper / "review").glob("review_v*.json"))
        ):
            raise ValueError(
                "Control arm requires a clean paper tree without Phase 2B artifacts; preserve/move the old paper first"
            )
        s = ctx.scratch.setdefault("writing", {})
        meta = _receipt(ctx, "runtime")
        # Establish existing provenance before any invalidation can reprovision files.
        self._validate_provisioned_figures(ctx, s)
        selected, basis, runtime = self._inputs(ctx)
        figure_keys = self._figure_keys(ctx, selected, runtime)
        figure_basis = _hash(figure_keys)
        new_basis = s.get("basis") != basis
        if new_basis:
            s.update(basis=basis, phase="bundle", aggregates=[r.model_dump() for r in selected], figures={})
        guidance = meta.setdefault("guidance", [])
        known = {g["approval_id"] for g in guidance}
        for fb in ctx.feedback:
            if (
                fb.kind == "manuscript"
                and fb.status in {"changes_requested", "rejected"}
                and fb.approval_id not in known
            ):
                guidance.append(
                    {
                        "approval_id": fb.approval_id,
                        "status": fb.status,
                        "comment": fb.decision_comment or "",
                        "target": fb.target.model_dump(),
                    }
                )
                known.add(fb.approval_id)
        guidance.sort(key=lambda g: g["approval_id"])
        writing_config = _hash(
            {
                "guidance": guidance,
                "pins": ctx.project.prompt_pins,
                "models": ctx.project.models,
                "skill": ctx.skills.load("latex-writing").instructions,
                "prompt": sha256_file(ctx.llm.prompts.load(PROMPT_ID, ctx.project.prompt_pins.get(PROMPT_ID)).path),
            }
        )
        if s.get("writing_config") != writing_config:
            if not new_basis and s.get("phase") in {"compile", "compile_fix", "submit"}:
                s["phase"] = "write"
            s["writing_config"] = writing_config
        selection = ctx.project.template
        if meta.get("fallback_from") == selection and "fallback_from" in meta:
            selection = None
        template_key = _hash(
            {
                "template": selection,
                "compile": ctx.project.dev.compile,
                "title": ctx.project.title,
                "skill": ctx.skills.load("latex-writing").ref,
            }
        )
        if s.get("template_key") != template_key:
            s["template_key"] = template_key
            if s.get("phase") not in {"bundle", "figures"}:
                s["phase"] = "template"
        s["template"] = selection
        # Engine delivers answers once. The receipt also survives an interrupted step.
        if ctx.answer and ctx.answer.question_id not in meta.setdefault("answers", []):
            wait = meta.get("waiting", {})
            choice = ctx.answer.choice
            if wait.get("kind") == "template_entry":
                if choice not in wait["choices"]:
                    raise ValueError("template answer is not a candidate")
                meta.setdefault("entries", {})[wait["key"]] = choice
                s["phase"] = "template"
            elif wait.get("kind") == "template_failure":
                if choice == "use_default":
                    meta["fallback_from"] = ctx.project.template
                    s["template"] = None
                elif choice != "uploaded_fix":
                    raise ValueError("unexpected template answer")
                s["phase"] = "template"
                meta.pop("template_checked", None)
            elif wait.get("kind") == "compile_failure" and choice == "retry":
                meta["manual_retry"] = True
                meta.pop("compiled", None)
                s["phase"] = "write"
            else:
                raise ValueError("unexpected Writing answer")
            meta["answers"].append(ctx.answer.question_id)
            meta.pop("waiting", None)
        _save(ctx, "runtime", meta)
        if s.get("phase") not in {"bundle", "figures"} and s.get("figure_basis") != figure_basis:
            s["phase"] = "figures"
            return Continue("Figure dependencies changed; revalidate/regenerate figures before manuscript work")
        phase = s.setdefault("phase", "bundle")
        index = _PHASES.index(phase) if phase in _PHASES else 4
        labels = {
            "bundle": "collecting evidence",
            "figures": "generating figures",
            "template": "validating template",
            "write": "preparing sections",
            "compile": "compiling",
            "compile_fix": "repairing compilation",
            "submit": "preparing manuscript approval",
        }
        ctx.progress(f"Paper: {labels.get(phase, phase)}", index + 1, 6, "steps")
        if phase == "bundle":
            bundle = build_bundle(ctx.root, approved=ctx.approved, aggregate_refs=selected, include_runtime=True)
            if bundle.diagnostics:
                raise ValueError("approved evidence lineage is inconsistent")
            # This phase only runs on a new basis/recovery boundary. Preserve the
            # bundle writer's exact parent-closure check, including identical bytes.
            bundle_ref = write_bundle(ctx.root, bundle)
            s.update(bundle_ref=bundle_ref.model_dump(), phase="figures")
            return Continue("Frozen evidence bundle registered")
        bundle = EvidenceBundle.model_validate_json(ctx.archive.get(_ref(s["bundle_ref"])))
        if phase == "figures":
            return self._figures(ctx, s, meta, bundle, figure_keys, figure_basis)
        if phase == "template":
            return self._template(ctx, s, meta)
        writer = Writer(
            ctx.root,
            bundle,
            llm=ctx.llm,
            skills=ctx.skills,
            figures={fid: f"figures/{fid}.pdf" for fid in s["figures"]},
            guidance=guidance,
        )
        if phase == "write":
            return self._write(ctx, s, meta, writer)
        if phase == "compile":
            return self._compile(ctx, s, meta, writer)
        if phase == "compile_fix":
            return self._repair(ctx, s, meta, writer)
        if phase == "submit":
            return self._submit(ctx, s, meta, writer)
        raise ValueError(f"unknown Writing phase {phase}")

    def _inputs(self, ctx):
        for kind, aid in (("idea", "idea/selected.md"), ("plan", "plan/experiment_plan.md")):
            if kind not in ctx.approved or ctx.approved[kind].artifact_id != aid:
                raise ValueError(f"exact approved {kind} ref required")
            ctx.archive.get(ctx.approved[kind])
        plan_ref = ctx.approved["plan"]
        text = ctx.archive.read_text(plan_ref)
        plan = parse_plan_md(text)
        selected, sources = [], []
        from .bundle import AggregateSource

        for comparison in sorted(plan.comparisons, key=lambda c: c.id):
            aid = f"artifacts/aggregates/{comparison.id}.json"
            if PurePosixPath(aid).name != comparison.id + ".json":
                raise ValueError("noncanonical comparison ID")
            found = None
            for version in reversed(ctx.archive.versions(aid)):
                if version.kind != "aggregate":
                    continue
                ctx.archive.get(version.ref)
                agg = runs.load_aggregate(ctx.root, comparison.id, version=version.ref.version)
                if agg.plan_ref == plan_ref:
                    if (
                        agg.aggregate_id != comparison.id
                        or not agg.rows
                        or not any(r.metric == comparison.metric for r in agg.rows)
                    ):
                        raise ValueError(f"unusable current-plan comparison: {comparison.id}")
                    found = version.ref
                    sources.append(AggregateSource(aggregate_id=comparison.id, ref=found, data=agg))
                    break
            if found is None:
                raise ValueError(f"no usable current-plan aggregate for required comparison {comparison.id}")
            selected.append(found)
        if not selected or len({r.artifact_id for r in selected}) != len(selected):
            raise ValueError("approved plan requires distinct nonempty comparisons")
        runtime = _runtime_input(
            ctx.root,
            ApprovedSource(ref=plan_ref, approval_id="", text=text, data=plan.model_dump(mode="json")),
            sources,
        )
        lit = _path(ctx, literature.LITERATURE)
        basis = _hash(
            {
                "approved": {k: ctx.approved[k].model_dump() for k in ("idea", "plan")},
                "aggregates": [r.model_dump() for r in selected],
                "runtime": runtime.sha256,
                "logs": [(a.approval_id, a.target.model_dump()) for a in ctx.approvals.list("log", "approved")],
                "literature": sha256_file(lit) if lit.exists() else None,
            }
        )
        return selected, basis, runtime

    def _figure_keys(self, ctx, selected, runtime):
        """One dependency construction for global invalidation and per-figure reuse.

        Prompt/model dependencies remain conservative even with explicit task units.
        Template, prose, and user writing feedback are intentionally absent.
        """
        skill = ctx.skills.load("scientific-plotting")
        pin = ctx.project.prompt_pins.get(FIGURE_PROMPT)
        prompt = ctx.llm.prompts.load(FIGURE_PROMPT, pin)
        common = {
            "runtime": runtime.sha256,
            "task": runtime.task_sha256,
            "skill": skill.ref,
            "skill_instructions": skill.instructions,
            "script": sha256_file(skill.path("scripts/plot.py")),
            "prompt": {"id": prompt.id, "version": prompt.version, "pin": pin, "sha256": sha256_file(prompt.path)},
            "models": [
                {"alias": model.alias, "entry": model.entry.model_dump(mode="json")}
                for model in ctx.llm.resolve("writing.figure_spec", FIGURE_PROMPT, "strong", prompt.tier)
            ],
        }
        return {
            f"fig_{PurePosixPath(ref.artifact_id).stem.lower()}": _hash({**common, "aggregate": ref.model_dump()})
            for ref in selected
        }

    def _validate_provisioned_figures(self, ctx, s, *, complete=False):
        """Prove current paper bytes against exact archived refs, never adopt edits.

        Validate the *registered* implementation here. Freshness against the current
        skill is separate, so an authorized dependency change can regenerate safely.
        """
        figures = s.get("figures", {})
        if not figures and not complete:
            return
        try:
            bundle = EvidenceBundle.model_validate_json(ctx.archive.get(_ref(s["bundle_ref"])))
            expected = {f"fig_{a.aggregate_id.lower()}": a for a in bundle.aggregates}
            if complete and figures.keys() != expected.keys():
                raise ValueError("registered figure set differs from required comparisons")
            for fid, raw in figures.items():
                if fid not in expected:
                    raise ValueError(f"unexpected figure identity: {fid}")
                ref = _ref(raw)
                if ref.artifact_id != f"artifacts/figures/{fid}":
                    raise ValueError(f"incorrect Figure artifact identity: {fid}")
                archived_manifest = ctx.archive.get(ref)
                info = ctx.archive.version_info(ref)
                item = expected[fid]
                if info.kind != "figure" or not info.is_dir or info.parents != [item.ref]:
                    raise ValueError(f"incorrect Figure/Aggregate provenance: {fid}")
                archived = ctx.archive.stored_path(ref)
                directory = _path(ctx, ref.artifact_id)
                if (
                    archived is None
                    or manifest_bytes(dir_manifest(safe_path(archived), [])) != archived_manifest
                    or manifest_bytes(dir_manifest(directory, [])) != archived_manifest
                ):
                    raise ValueError(f"Figure artifact no longer matches archived version: {fid}")
                figure = Figure.model_validate_json((directory / "figure.json").read_bytes())
                if figure.fig_id != fid or figure.aggregate_ref != item.ref:
                    raise ValueError(f"incorrect Figure metadata identity: {fid}")
                problems = validate_artifact(
                    directory, item.data, item.ref,
                    script_path=archived / "plot.py", skill_ref=figure.skill_ref,
                )
                if problems:
                    raise ValueError(f"invalid registered Figure {fid}: {problems}")
                source = _path(ctx, f"{ref.artifact_id}/{fid}.pdf")
                provisioned = _path(ctx, f"paper/figures/{fid}.pdf")
                if provisioned.read_bytes() != source.read_bytes():
                    raise ValueError(f"provisioned PDF differs: {fid}")
        except (AirError, ValueError, OSError, KeyError) as exc:
            raise ValueError(
                "Manuscript figure no longer matches its registered scientific Figure provenance; "
                f"explicit reconciliation/regeneration required: {exc}"
            ) from exc

    def _figures(self, ctx, s, meta, bundle, figure_keys, figure_basis):
        skill = ctx.skills.load("scientific-plotting")
        for item in bundle.aggregates:
            fid = f"fig_{item.aggregate_id.lower()}"
            key = figure_keys[fid]
            receipt = _receipt(ctx, f"figure-{fid}")
            if receipt.get("key") != key:
                units = bundle.runtime.task.get("metric_units", {})
                metric = item.data.comparison.get("metric")
                if metric in units:
                    spec = default_spec(item.data, fig_id=fid, ylabel=f"{metric} ({units[metric]})")
                    receipt = {"key": key, "spec": spec.model_dump()}
                else:
                    response = ctx.llm.complete(
                        role="writing.figure_spec",
                        prompt_id=FIGURE_PROMPT,
                        tier="strong",
                        response_schema=PlannedFigure,
                        variables={
                            "schema": PlannedFigure.model_json_schema(),
                            "evidence": ctx.llm.wrap_untrusted(
                                json.dumps(
                                    {
                                        "fig_id": fid,
                                        "aggregate": item.data.model_dump(mode="json"),
                                        "task": bundle.runtime.task,
                                    },
                                    ensure_ascii=False,
                                ),
                                "figure-input",
                            ),
                        },
                    )
                    spec = PlannedFigure.model_validate(response.parsed)
                    receipt = {
                        "key": key,
                        "spec": spec.model_dump(),
                        "call_id": response.call_id,
                        "prompt_id": response.prompt_id,
                        "prompt_version": response.prompt_version,
                    }
                if spec.fig_id != fid or spec.aggregate_id != item.aggregate_id or spec.metric != metric:
                    raise ValueError("FigureSpec changed the selected comparison identity")
                prepare_data(item.data, spec)
                _save(ctx, f"figure-{fid}", receipt)
            spec = FigureSpec.model_validate(receipt["spec"])
            directory = _path(ctx, f"artifacts/figures/{fid}")
            info = ctx.archive.latest_version(f"artifacts/figures/{fid}")
            reuse = False
            if info and directory.exists() and (directory / "figure.json").exists():
                figure = Figure.model_validate_json((directory / "figure.json").read_bytes())
                reuse = (
                    figure.spec == spec
                    and item.ref in info.parents
                    and ctx.archive.current_sha(info.ref.artifact_id) == info.ref.sha256
                    and not validate_artifact(
                        directory, item.data, item.ref, script_path=skill.path("scripts/plot.py"), skill_ref=skill.ref
                    )
                )
            if not reuse:
                generate_figure(ctx.root, item.aggregate_id, spec, version=item.ref.version, skill=skill)
            ref = ctx.archive.latest(f"artifacts/figures/{fid}")
            source = directory / f"{fid}.pdf"
            destination = _path(ctx, f"paper/figures/{fid}.pdf")
            provisioned = destination.exists() and destination.read_bytes() == source.read_bytes()
            if not provisioned:
                atomic_write_bytes(destination, source.read_bytes())
            completed = s["figures"].get(fid) == ref.model_dump()
            s["figures"][fid] = ref.model_dump()
            if not completed or not reuse or not provisioned:
                return Continue(f"Scientific figure ready: {fid}")
        s.update(phase="template", figure_basis=figure_basis)
        return Continue("All comparison figures provisioned")

    def _ask(self, ctx, meta, kind, text, options, **details):
        meta["waiting"] = {"kind": kind, **details}
        _save(ctx, "runtime", meta)
        return Stop(text, Question(text=text, options=[QuestionOption(id=k, label=v) for k, v in options]))

    def _template(self, ctx, s, meta):
        key = _hash(s["template"])
        entry = meta.get("entries", {}).get(key)
        try:
            source = resolve_template(ctx.root, s["template"], skill=ctx.skills.load("latex-writing"))
            check = inspect_template(source.original, entry=entry)
            if check.selection_required:
                return self._ask(
                    ctx,
                    meta,
                    "template_entry",
                    "Select a manuscript template entry.",
                    [(c, c) for c in check.entry_candidates],
                    key=key,
                    choices=check.entry_candidates,
                )
            if (
                not check.entry
                or check.missing
                or any(not d.startswith("Dependency lookup unresolved:") for d in check.diagnostics)
            ):
                raise TemplateError(json.dumps(check.model_dump(), ensure_ascii=False))
            cache = _hash(
                [
                    key,
                    entry,
                    ctx.project.dev.compile,
                    [
                        (p.relative_to(source.original).as_posix(), sha256_file(p))
                        for p in sorted(source.original.rglob("*"))
                        if p.is_file()
                    ],
                ]
            )
            if meta.get("template_checked") != cache:
                if ctx.project.dev.compile:
                    check = check_template(source, entry=entry)
                    write_check(source, check)
                    if not check.minimal_compile_ok:
                        raise TemplateError(f"Template minimal compilation failed: {check.compile_report}")
                else:
                    write_check(source, check)
                    ctx.log("Template structural inspection only; dev.compile=false skips minimal compilation")
                meta["template_checked"] = cache
            s.update(entry=check.entry, phase="write")
            _save(ctx, "runtime", meta)
            return Continue("Template inspected; explicit selection retained")
        except (TemplateError, OSError) as exc:
            return self._ask(
                ctx,
                meta,
                "template_failure",
                f"Template requires attention: {exc}",
                [
                    ("uploaded_fix", "I uploaded a corrected template version"),
                    ("use_default", "Use the verified default template"),
                    ("end", "End project"),
                ],
            )

    def _write(self, ctx, s, meta, writer):
        for section in GENERATION_ORDER:
            fingerprint = _hash([writer.section_fingerprint(section), s["writing_config"], s["figures"]])
            receipt = _receipt(ctx, f"section-{section}")
            path = _path(ctx, f"paper/sections/{section}.tex")
            if not receipt and path.exists():
                raise ValueError(f"unreceipted existing section {section}; preserve/reconcile it before generation")
            if receipt.get("fingerprint") == fingerprint:
                if not receipt.get("installed", True) or not path.exists():
                    output = SectionOutput.model_validate(receipt["output"])
                    writer.validate_section(output, section)
                    expected = _section_bytes(output)
                    if path.exists():
                        if path.read_bytes() != expected:
                            raise ValueError(
                                f"pending generated section {section} conflicts with an existing different file; "
                                "explicit reconciliation required"
                            )
                        writer.read_section(section)
                    else:
                        writer.write_section(output)
                    if path.read_bytes() != expected:
                        raise ValueError(f"section installation verification failed: {section}")
                    receipt.update(installed=True, file_sha256=sha256_bytes(expected))
                    _save(ctx, f"section-{section}", receipt)
                output = writer.read_section(section)
                current = sha256_file(path)
                if receipt.get("file_sha256") != current:
                    receipt.update(output=output.model_dump(), file_sha256=current, human_edited=True)
                    _save(ctx, f"section-{section}", receipt)
                    ctx.log(f"Preserved human-edited section: {section}")
                continue
            if (
                receipt
                and path.exists()
                and (receipt.get("human_edited") or sha256_file(path) != receipt.get("file_sha256"))
            ):
                raise ValueError(
                    f"human-edited {section} has stale factual/writing inputs; reconcile the edit explicitly"
                )
            ctx.progress(f"Paper: writing {section.replace('_', ' ')}", 4, 6, "steps")
            generated = writer.generate_section(section)
            # Persist validated output before installing the file: a crash can replay this write for free.
            receipt = {
                "fingerprint": fingerprint,
                "output": generated.output.model_dump(),
                "context_sha256": generated.context_sha256,
                "skill_ref": generated.skill_ref,
                "call_id": generated.call_id,
                "prompt_id": generated.prompt_id,
                "prompt_version": generated.prompt_version,
                "file_sha256": sha256_bytes(_section_bytes(generated.output)),
                "installed": False,
            }
            _save(ctx, f"section-{section}", receipt)
            writer.write_section(generated.output)
            if path.read_bytes() != _section_bytes(generated.output):
                raise ValueError(f"section installation verification failed: {section}")
            receipt.update(installed=True, file_sha256=sha256_file(path))
            _save(ctx, f"section-{section}", receipt)
            return Continue(f"Section persisted: {section}")
        self._assemble(ctx, s, meta, writer)
        s["phase"] = "compile"
        return Continue("Sections, references and deterministic AI-use statement assembled")

    def _assemble(self, ctx, s, meta, writer):
        self._validate_provisioned_figures(ctx, s, complete=True)
        receipts = [_receipt(ctx, f"section-{name}") for name in SECTIONS]
        calls = sorted({f"{r['prompt_id']} v{r['prompt_version']}" for r in receipts if r.get("call_id")})
        repairs = meta.get("repairs", [])
        accepted = sum(r.get("status") == "accepted" for r in repairs)
        human = [name for name, r in zip(SECTIONS, receipts, strict=True) if r.get("human_edited")]
        lines = [
            r"\section*{Generative AI Use}",
            "An AI agent generated manuscript prose using " + _tex(", ".join(calls)) + ".",
            "Runtime model backend: " + _tex(type(ctx.llm.backend).__name__) + ".",
            f"Writing roles: writing.writer. Recorded automatic compile-repair attempts: {len(repairs)}; "
            f"accepted: {accepted}.",
            "Evidence checking is disabled (control arm, evidence\\_check=false). "
            "Compilation and local offline citation checks do not establish scientific correctness.",
            "Approved idea and plan versions were supplied. "
            f"Manuscript feedback items processed: {len(meta.get('guidance', []))}. "
            "This draft awaits manuscript approval; no publication authorization is implied.",
            f"Prior manuscript approval records: {len(ctx.approvals.list('manuscript', 'approved'))}.",
        ]
        if repairs:
            versions = sorted({str(r["prompt_version"]) for r in repairs if r.get("call_id")})
            lines.append(
                "Compile repair workflow: role writing.compile\\_repair; "
                "recorded completed prompt versions: " + _tex(", ".join(versions) or "none") + "."
            )
            if any(r["status"] == "started" for r in repairs):
                lines.append(
                    "An interrupted repair attempt has uncertain completion status and consumes its retry budget."
                )
        if human:
            lines.append("Human-edited sections preserved: " + _tex(", ".join(human)) + ".")
        if "fallback_from" in meta and s["template"] is None:
            lines.append("The user explicitly selected the default template after an uploaded-template problem.")
        figure_plans = [_receipt(ctx, f"figure-{fid}") for fid in s["figures"]]
        if any(r.get("call_id") for r in figure_plans):
            lines.append(
                "Figure presentation used writing.figure\\_spec (writing/figure\\_spec v1); "
                "values and graphics came from deterministic scripts."
            )
        atomic_write_text(_path(ctx, "paper/ai_use.tex"), "\n".join(lines) + "\n")
        # Remove only unchanged resources previously owned by template preparation.
        if meta.get("assembled_template") != s["template_key"]:
            for rel, sha in meta.get("template_assets", {}).items():
                path = _path(ctx, "paper/" + rel)
                if path.exists():
                    if sha256_file(path) != sha:
                        raise ValueError(f"human-edited template asset needs explicit adaptation: {rel}")
                    path.unlink()
        main = _path(ctx, "paper/main.tex")
        manual_main = main.exists() and meta.get("main_sha256") and sha256_file(main) != meta["main_sha256"]
        if manual_main:
            writer.generate_references()
            ctx.log("Preserving human-edited main.tex; recompiling current source")
        else:
            writer.assemble_draft(template=s["template"], entry=s["entry"], title=ctx.project.title, ai_use=True)
            meta["main_sha256"] = sha256_file(main)
        source = resolve_template(ctx.root, s["template"], skill=writer.skill)
        meta["template_assets"] = {
            p.relative_to(source.original).as_posix(): sha256_file(p)
            for p in source.original.rglob("*")
            if p.is_file() and p.name not in {s["entry"], "references.bib"}
        }
        meta["assembled_template"] = s["template_key"]
        # Bind PDF bytes to the complete source basis, including comment-only edits.
        # Archive.put deduplicates bytes: this invisible deterministic PDF metadata
        # prevents a new source version reusing a PDF parented to an older manuscript.
        # Identical rendered PDFs can have new Figure refs after a renderer change.
        # Include those refs so byte-deduplication cannot retain old paper parents.
        source_identity = _hash(
            [dir_manifest(_path(ctx, "paper"), [*PAPER_EXCLUDE, "ai_use.tex"]), lines, s["figures"]]
        )
        atomic_write_text(
            _path(ctx, "paper/ai_use.tex"),
            "\n".join(lines)
            + "\n"
            + r"\ifdefined\pdfinfo\pdfinfo{/AIRSourceSHA256 ("
            + source_identity
            + r")}\fi"
            + "\n",
        )
        meta["assembled_source_sha256"] = self._source_sha(ctx)
        _save(ctx, "runtime", meta)

    def _source_sha(self, ctx):
        return ctx.archive.current_sha("paper", exclude=PAPER_EXCLUDE)

    def _compile(self, ctx, s, meta, writer):
        self._validate_provisioned_figures(ctx, s, complete=True)
        if meta.get("assembled_source_sha256") != self._source_sha(ctx):
            s["phase"] = "write"
            return Continue("Source edited before compilation; validate and refresh references")
        for name in SECTIONS:
            if writer.read_section(name) is None:
                raise ValueError(f"missing section {name}")
        sha = self._source_sha(ctx)
        previous = meta.get("compiled", {})
        report_path = _path(ctx, "paper/build/compile_report.json")
        pdf = _path(ctx, "paper/build/main.pdf")
        if (
            previous.get("source_sha256") == sha
            and previous.get("enabled") == ctx.project.dev.compile
            and report_path.exists()
            and sha256_file(report_path) == previous.get("report_sha256")
            and (not previous.get("pdf_sha256") or (pdf.exists() and sha256_file(pdf) == previous["pdf_sha256"]))
        ):
            report = CompileReport.model_validate_json(report_path.read_bytes())
        else:
            report = compile_paper(_path(ctx, "paper"), enabled=ctx.project.dev.compile)
            # A report's log identity binds it to its exact source even if all other
            # fields (notably a disabled report) are identical to an earlier build.
            log = _path(ctx, report.log_path)
            if log.is_file():
                relative = f"paper/build/source-{sha}/main.log"
                atomic_write_bytes(_path(ctx, relative), log.read_bytes())
                report = report.model_copy(update={"log_path": relative})
            atomic_write_json(report_path, report)
            meta["compiled"] = {
                "source_sha256": sha,
                "enabled": ctx.project.dev.compile,
                "report_sha256": sha256_file(report_path),
                "pdf_sha256": sha256_file(pdf) if report.ok and pdf.exists() else None,
            }
            if report.errors or report.undefined_citations or report.undefined_refs or report.overfull_boxes:
                ctx.log("Paper compilation diagnostics: " + report.model_dump_json())
        if report.ok or (not ctx.project.dev.compile and report.engine == "disabled"):
            s["phase"] = "submit"
            meta.pop("manual_retry", None)
        elif meta.pop("manual_retry", False):
            _save(ctx, "runtime", meta)
            return self._compile_question(ctx, meta)
        else:
            s["phase"] = "compile_fix"
        _save(ctx, "runtime", meta)
        return Continue("Compilation recorded" if report.ok else f"Compilation state: {report.engine}")

    def _cycle(self, s):
        return _hash([s["basis"], s["writing_config"], s["template_key"]])

    def _compile_question(self, ctx, meta):
        return self._ask(
            ctx,
            meta,
            "compile_failure",
            "Compilation requires manual attention. "
            "See paper/build/compile_report.json. Retry validates and recompiles current source first.",
            [("retry", "I fixed the section source; recompile"), ("end", "End project")],
        )

    def _repair(self, ctx, s, meta, writer):
        attempts = meta.setdefault("repairs", [])
        cycle = self._cycle(s)
        current = [a for a in attempts if a["cycle"] == cycle]
        # Complete a persisted accepted repair before considering another model call.
        if current and current[-1].get("status") == "accepted" and not current[-1].get("installed"):
            attempt = current[-1]
            output = SectionOutput.model_validate(attempt["output"])
            actual = writer.read_section(output.section)
            if sha256_bytes(actual.latex.encode()) not in {
                attempt["source_sha256"],
                sha256_bytes(_section_bytes(output)),
            }:
                raise ValueError("section changed after repair receipt; refusing to overwrite human edits")
            writer.write_section(output)
            receipt = _receipt(ctx, f"section-{output.section}")
            receipt.update(
                output=output.model_dump(), file_sha256=sha256_file(_path(ctx, f"paper/sections/{output.section}.tex"))
            )
            _save(ctx, f"section-{output.section}", receipt)
            attempt["installed"] = True
            meta.pop("compiled", None)
            self._assemble(ctx, s, meta, writer)
            s["phase"] = "compile"
            _save(ctx, "runtime", meta)
            return Continue("Validated compile repair installed")
        if meta.get("compiled", {}).get("source_sha256") != self._source_sha(ctx):
            s["phase"] = "write"
            meta["manual_retry"] = True
            return Continue("Source changed after compile failure; validate manual edits before repair")
        maximum = ctx.project.paper.get("max_compile_fixes", 2)
        if type(maximum) is not int or maximum < 0:
            raise ValueError("paper.max_compile_fixes must be a nonnegative integer")
        if len(current) >= maximum:
            return self._compile_question(ctx, meta)
        report = CompileReport.model_validate_json(_path(ctx, "paper/build/compile_report.json").read_bytes())
        targets = {}
        for error in report.errors:
            name = str(error.get("file", "")).replace("\\", "/").removeprefix("./").removeprefix("paper/")
            for section in SECTIONS:
                receipt = _receipt(ctx, f"section-{section}")
                if name == f"sections/{section}.tex" and receipt.get("call_id") and not receipt.get("human_edited"):
                    targets.setdefault(section, []).append(error)
        if not targets:
            return self._compile_question(ctx, meta)
        section = sorted(targets)[0]
        original = writer.read_section(section)
        attempt = {
            "cycle": cycle,
            "section": section,
            "status": "started",
            "source_sha256": sha256_bytes(original.latex.encode()),
            "number": len(current) + 1,
        }
        attempts.append(attempt)
        _save(ctx, "runtime", meta)  # An ambiguous interrupted call consumes its bounded attempt.
        try:
            response = ctx.llm.complete(
                role="writing.compile_repair",
                prompt_id=REPAIR_PROMPT,
                tier="strong",
                response_schema=SectionOutput,
                max_retries=0,
                variables={
                    "section": section,
                    "skill_rules": writer.skill.instructions,
                    "evidence": ctx.llm.wrap_untrusted(
                        json.dumps(
                            {
                                "section": original.model_dump(),
                                "errors": targets[section],
                                "citation_keys": sorted(writer._allowed),
                                "figures": writer.figures,
                            }
                        ),
                        "compile-repair",
                    ),
                },
            )
            attempt.update(call_id=response.call_id, prompt_version=response.prompt_version)
            output = SectionOutput.model_validate(response.parsed)
            writer.validate_section(output, section)
            attempt.update(
                status="accepted",
                output=output.model_dump(),
                call_id=response.call_id,
                prompt_version=response.prompt_version,
                installed=False,
            )
        except (ValueError, AirError, LLMOutputError) as exc:
            attempt.update(status="rejected", reason=str(exc))
        _save(ctx, "runtime", meta)
        return Continue(f"Compile repair attempt {attempt['number']}: {attempt['status']}")

    def _submit(self, ctx, s, meta, writer):
        self._validate_provisioned_figures(ctx, s, complete=True)
        if meta.get("compiled", {}).get("source_sha256") != self._source_sha(ctx):
            s["phase"] = "write"
            return Continue("Source changed; preserve and validate edits before compiling")
        compiled = meta["compiled"]
        report_path = _path(ctx, "paper/build/compile_report.json")
        pdf = _path(ctx, "paper/build/main.pdf")
        if (
            not report_path.exists()
            or sha256_file(report_path) != compiled["report_sha256"]
            or (compiled.get("pdf_sha256") and (not pdf.exists() or sha256_file(pdf) != compiled["pdf_sha256"]))
        ):
            meta.pop("compiled", None)
            _save(ctx, "runtime", meta)
            s["phase"] = "compile"
            return Continue("Build artifacts changed; recompile before registration")
        parents = [
            _ref(s["bundle_ref"]),
            *[_ref(r) for r in s["aggregates"]],
            *[_ref(r) for r in s["figures"].values()],
        ]
        paper = ctx.archive.put(
            "paper",
            "paper",
            _path(ctx, "paper"),
            parents=_refs(parents),
            producer="agent:writing",
            exclude=PAPER_EXCLUDE,
        )
        registered_parents = ctx.archive.version_info(paper).parents
        if any(_ref(raw) not in registered_parents for raw in s["figures"].values()):
            raise ValueError(
                "archived manuscript Figure parents differ from validated provenance; reconcile explicitly"
            )
        report = CompileReport.model_validate_json(_path(ctx, "paper/build/compile_report.json").read_bytes())
        if not report.ok and not (not ctx.project.dev.compile and report.engine == "disabled"):
            raise ValueError("unsuccessful compilation cannot be submitted in real compile mode")
        extra = [
            ctx.archive.put(
                "paper/build/compile_report.json",
                "other",
                _path(ctx, "paper/build/compile_report.json"),
                parents=[paper],
                producer="agent:writing",
            )
        ]
        if report.ok:
            extra.append(
                ctx.archive.put(
                    "paper/build/main.pdf",
                    "paper",
                    _path(ctx, "paper/build/main.pdf"),
                    parents=[paper],
                    producer="agent:writing",
                )
            )
        s["paper_ref"] = paper.model_dump()
        s["extra_refs"] = [r.model_dump() for r in extra]
        ctx.progress("Paper: awaiting manuscript approval", 6, 6, "steps")
        status = f"compiled, {report.pages} pages" if report.ok else "compilation disabled; no PDF produced"
        return NeedsApproval(
            target=paper,
            kind="manuscript",
            summary=f"Manuscript v{paper.version}: {len(s['figures'])} figures; {status}; "
            "evidence checking disabled (control arm).",
            impact="Approval marks project completion; it does not authorize publication or submission.",
            extra_refs=extra,
        )


def create_stage() -> WritingStage:
    return WritingStage()
