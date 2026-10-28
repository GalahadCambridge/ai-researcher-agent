"""Resumable real Writing control arm. Shared engine owns checkpoints and approvals.

Writing-owned receipts close completed-call/file-write gaps. They are not manuscript
evidence and are excluded from its directory archive. Both paths share compilation
and approvals.
"""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from types import SimpleNamespace

from pydantic import ConfigDict

from airesearcher.core.archive import dir_manifest, manifest_bytes
from airesearcher.core.errors import AirError, ValidationFailed
from airesearcher.core.figure_errors import UnitLabelError
from airesearcher.core.fsutil import atomic_write_bytes, atomic_write_json, atomic_write_text, sha256_bytes, sha256_file
from airesearcher.core.models.claim import CheckReport, Claim, CompileReport, Issue
from airesearcher.core.models.common import VersionRef
from airesearcher.core.models.figure import Figure, FigureSpec
from airesearcher.core.models.plan import parse_plan_md
from airesearcher.core.models.question import Question, QuestionOption
from airesearcher.engine.stage import Continue, Error, NeedsApproval, StageContext, StepResult, Stop
from airesearcher.llm.gateway import LLMOutputError
from airesearcher.services import evidence, literature, runs

from . import attempts, figure_planning, recovery
from . import claims as binding
from . import review as semantic
from ._latex import TemplateError, safe_path
from .bundle import ApprovedSource, EvidenceBundle, _runtime_input, build_bundle, write_bundle
from .compile import compile_paper
from .figures import default_spec, generate_figure, prepare_data, presentation_error, validate_artifact, validate_source
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
    return safe_path(ctx.root / relative, root=ctx.root)


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
        try:
            pending = _receipt(ctx, "reconcile")
            if pending.get("done") and ctx.answer and pending.get("answer") == ctx.answer.question_id:
                return Continue("Reconciliation answer already applied")
            if pending and not pending.get("done"):
                return recovery.recover(ctx, None, _receipt, _save, _path, atomic_write_bytes)
            return self._step(ctx)
        except recovery.ReconcileRequired as exc:
            try:
                return recovery.recover(ctx, exc, _receipt, _save, _path, atomic_write_bytes)
            except (AirError, ValueError, OSError, KeyError) as error:
                return Error(f"Writing reconciliation stopped: {error}", retryable=False)
        except attempts.DecisionRequired as exc:
            return exc.result
        except (AirError, ValueError, OSError, KeyError, LLMOutputError) as exc:
            return Error(f"Writing stopped: {exc}", retryable=False)

    def _step(self, ctx):
        paper = _path(ctx, "paper")
        if not ctx.project.evidence_check and (
            (paper / "claims.jsonl").exists()
            or (paper / "air_values.tex").exists()
            or list((paper / "review").glob("review_v*.json"))
        ):
            raise ValueError(
                "Control arm requires a clean paper tree without Phase 2B artifacts; preserve/move the old paper first"
            )
        s = ctx.scratch.setdefault("writing", {})
        meta = _receipt(ctx, "runtime")
        if meta and meta.get("evidence_check", False) != ctx.project.evidence_check:
            raise ValueError("Writing mode changed in place; preserve history and use a fresh workspace")
        meta["evidence_check"] = ctx.project.evidence_check
        if ctx.project.evidence_check:
            semantic.round_limit(ctx.project.paper.get("max_review_rounds", 2))
            revision = _receipt(ctx, "pending-revision")
            if revision and not revision.get("assembled"):
                self._finish_revision(ctx, revision)
                # Complete the accepted transaction on its recorded basis before
                # examining new dependencies. The next tick performs normal invalidation.
                claims = self._guard_claim_files(ctx)
                bundle = EvidenceBundle.model_validate_json(ctx.archive.get(_ref(s["bundle_ref"])))
                writer = Writer(
                    ctx.root,
                    bundle,
                    llm=ctx.llm,
                    skills=ctx.skills,
                    figures={fid: f"figures/{fid}.pdf" for fid in s["figures"]},
                    guidance=meta.get("guidance", []),
                    claims=claims,
                )
                return self._revision_assemble(ctx, s, meta, writer)
            pending = _receipt(ctx, "pending-claims")
            if pending and not pending.get("installed"):
                self._finish_claim_install(ctx, pending)
            self._guard_claim_files(ctx)
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
                "models": [
                    {"alias": m.alias, "entry": m.entry.model_dump(mode="json")}
                    for m in ctx.llm.resolve("writing.writer", PROMPT_ID, "strong", None)
                ],
                "skill": ctx.skills.load("latex-writing").instructions,
                "prompt": sha256_file(ctx.llm.prompts.load(PROMPT_ID, ctx.project.prompt_pins.get(PROMPT_ID)).path),
            }
        )
        if s.get("writing_config") != writing_config:
            if not new_basis and s.get("phase") in {"compile", "compile_fix", "verify", "review", "revise", "submit"}:
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
            if ctx.questions.record(ctx.answer.question_id).question.text.startswith("Semantic unit "):
                pass  # The bounded-attempt journal validates and consumes this answer.
            elif wait.get("kind") == "figure_unit":
                self._answer_figure_unit(ctx, wait, figure_keys, selected)
                if not new_basis:
                    s["phase"] = "figures"
            elif wait.get("kind") == "template_entry":
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
        phases = (
            ("bundle", "figures", "template", "claims", "write", "compile", "verify", "review", "revise", "submit")
            if ctx.project.evidence_check
            else _PHASES
        )
        index = phases.index(phase) if phase in phases else phases.index("compile")
        labels = {
            "bundle": "collecting evidence",
            "figures": "generating figures",
            "template": "validating template",
            "claims": "binding experimental claims",
            "verify": "checking deterministic evidence",
            "write": "preparing sections",
            "compile": "compiling",
            "compile_fix": "repairing compilation",
            "submit": "preparing manuscript approval",
        }
        ctx.progress(f"Paper: {labels.get(phase, phase)}", index + 1, len(phases), "steps")
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
        claims = None
        if ctx.project.evidence_check:
            figures = binding.load_figures(ctx.root, {fid: _ref(raw) for fid, raw in s["figures"].items()})
            claim_basis = self._claim_basis(ctx, s, bundle, figures)
            if phase != "claims" and s.get("claim_basis") != claim_basis:
                s["phase"] = "claims"
                return Continue("Claim evidence/planning basis changed; bind claims before manuscript work")
            if phase == "claims":
                return self._claims(ctx, s, bundle, figures, claim_basis)
            claims = self._guard_claim_files(ctx)
            if claims is None:
                raise ValueError("true Writing requires an installed bound claim set")
        writer = Writer(
            ctx.root,
            bundle,
            llm=ctx.llm,
            skills=ctx.skills,
            figures={fid: f"figures/{fid}.pdf" for fid in s["figures"]},
            guidance=guidance,
            claims=claims,
        )
        if phase == "write":
            return self._write(ctx, s, meta, writer)
        if phase == "compile":
            return self._compile(ctx, s, meta, writer)
        if phase == "compile_fix":
            return self._repair(ctx, s, meta, writer)
        if phase == "verify":
            return self._verify(ctx, s, meta, writer)
        if phase == "review":
            return self._review(ctx, s, meta, writer)
        if phase == "revise":
            return self._revise(ctx, s, meta, writer)
        if phase == "revision_assemble":
            return self._revision_assemble(ctx, s, meta, writer)
        if phase == "submit":
            return self._submit(ctx, s, meta, writer)
        raise ValueError(f"unknown Writing phase {phase}")

    def _claim_basis(self, ctx, s, bundle, figures):
        prompt = ctx.llm.prompts.load(binding.PROMPT_ID, ctx.project.prompt_pins.get(binding.PROMPT_ID))
        skill = ctx.skills.load("latex-writing")
        return _hash(
            {
                "bundle_ref": s["bundle_ref"],
                "bundle_sha": sha256_bytes(bundle.to_bytes()),
                "figures": {fid: f.model_dump(mode="json") for fid, f in figures.items()},
                "idea": bundle.idea_ref.model_dump(),
                "plan": bundle.plan_ref.model_dump(),
                "prompt": [prompt.id, prompt.version, sha256_file(prompt.path)],
                "models": [
                    {"alias": m.alias, "entry": m.entry.model_dump(mode="json")}
                    for m in ctx.llm.resolve("writing.claims", binding.PROMPT_ID, "strong", prompt.tier)
                ],
                "skill": [skill.ref, skill.instructions],
                "binding_rules": sha256_file(binding.__file__),
            }
        )

    def _guard_claim_files(self, ctx):
        receipt = _receipt(ctx, "bound-claims")
        paths = [_path(ctx, "paper/claims.jsonl"), _path(ctx, "paper/air_values.tex")]
        if not receipt:
            if any(p.exists() for p in paths):
                raise recovery.ReconcileRequired("unreceipted claim/value files",
                                                {p.relative_to(ctx.root).as_posix(): None for p in paths})
            return None
        claims = [Claim.model_validate(raw) for raw in receipt["claims"]]
        for path, expected in zip(paths, (binding.claims_bytes(claims), binding.values_tex(claims)), strict=True):
            if path.exists() and path.read_bytes() != expected:
                raise recovery.ReconcileRequired(f"code-owned {path.name} changed",
                                                {path.relative_to(ctx.root).as_posix(): expected})
            if not path.exists():
                atomic_write_bytes(path, expected)
        return claims

    def _install_claims(self, ctx, basis, claims):
        # Compare old generated files before replacing them for an authorized new basis.
        self._guard_claim_files(ctx)
        previous = _receipt(ctx, "bound-claims")
        expected = {"claims.jsonl": binding.claims_bytes(claims), "air_values.tex": binding.values_tex(claims)}
        pending = {
            "basis": basis,
            "claims": [c.model_dump(mode="json") for c in claims],
            "previous": previous,
            "installed": False,
        }
        _save(ctx, "pending-claims", pending)
        self._finish_claim_install(ctx, pending, expected)

    def _finish_claim_install(self, ctx, pending, expected=None):
        claims = [Claim.model_validate(raw) for raw in pending["claims"]]
        expected = expected or {
            "claims.jsonl": binding.claims_bytes(claims),
            "air_values.tex": binding.values_tex(claims),
        }
        old = [Claim.model_validate(raw) for raw in pending.get("previous", {}).get("claims", [])]
        old_bytes = {"claims.jsonl": binding.claims_bytes(old), "air_values.tex": binding.values_tex(old)}
        for name, content in expected.items():
            path = _path(ctx, "paper/" + name)
            allowed = [content] + ([old_bytes[name]] if pending.get("previous") else [])
            if path.exists() and path.read_bytes() not in allowed:
                raise recovery.ReconcileRequired(f"pending {name} conflicts with an intervening edit",
                                                {"paper/" + name: content})
            if not path.exists() or path.read_bytes() != content:
                atomic_write_bytes(path, content)
        _save(ctx, "bound-claims", {"basis": pending["basis"], "claims": pending["claims"]})
        pending["installed"] = True
        _save(ctx, "pending-claims", pending)

    def _claims(self, ctx, s, bundle, figures, basis):
        installed = _receipt(ctx, "bound-claims")
        if installed.get("basis") == basis:
            s.update(claim_basis=basis, phase="write")
            return Continue("Reusing exact bound claims")
        name = "claim-plan-" + basis
        receipt = _receipt(ctx, name)
        responses = receipt.setdefault("responses", [])
        claims, errors = binding.bind_plan(ctx.root, responses[-1]["plan"], bundle, figures) if responses else ([], [])
        if not responses or (errors and len(responses) == 1):
            response = ctx.llm.complete(
                role="writing.claims",
                prompt_id=binding.PROMPT_ID,
                tier="strong",
                max_retries=0,
                response_schema=binding.ClaimPlan,
                variables={
                    "schema": binding.ClaimPlan.model_json_schema(),
                    "evidence": ctx.llm.wrap_untrusted(
                        json.dumps(binding.planning_context(bundle, figures)), "claim-evidence"
                    ),
                    "correction": ctx.llm.wrap_untrusted(
                        json.dumps({"previous": responses[-1]["plan"], "errors": errors}), "claim-correction"
                    )
                    if responses
                    else "null",
                },
            )
            plan = binding.ClaimPlan.model_validate(response.parsed).model_dump(mode="json")
            responses.append({"plan": plan, "call_id": response.call_id, "prompt_version": response.prompt_version})
            receipt["errors"] = errors
            _save(ctx, name, receipt)  # durable response before any binding/file installation
            return Continue(
                "Claim planning response persisted" if len(responses) == 1 else "One correction response persisted"
            )
        if len(responses) == 2 and len(responses[0]["plan"]["claims"]) != len(responses[1]["plan"]["claims"]):
            raise ValueError("claim correction must preserve ordered claim slots/IDs")
        receipt["dropped"] = errors
        _save(ctx, name, receipt)
        if any(e.startswith(("coverage:", "binding:")) for e in errors):
            raise ValueError("claim coverage/binding failed after bounded correction: " + "; ".join(errors))
        self._install_claims(ctx, basis, claims)
        s.update(claim_basis=basis, phase="write")
        return Continue("Claims bound to exact evidence; code-generated values installed")

    def _finalize_claims(self, ctx, s, writer):
        current = self._guard_claim_files(ctx)
        located = binding.locate_claims(ctx.root, current)
        if located != current:
            self._install_claims(ctx, s["claim_basis"], located)
        writer.claims = located
        parents = self._paper_parents(s, include_claims=False)
        ref = ctx.archive.put(
            "paper/claims.jsonl", "claims", _path(ctx, "paper/claims.jsonl"), parents=parents, producer="agent:writing"
        )
        if ctx.archive.version_info(ref).parents != parents:
            raise ValueError("identical claims retain stale evidence parents; explicit reconciliation required")
        s["claims_ref"] = ref.model_dump()

    def _paper_parents(self, s, *, include_claims=True):
        return _refs(
            [
                _ref(s["bundle_ref"]),
                *[_ref(r) for r in s["aggregates"]],
                *[_ref(r) for r in s["figures"].values()],
                *([_ref(s["claims_ref"])] if include_claims and s.get("claims_ref") else []),
            ]
        )

    def _register_paper(self, ctx, s):
        self._validate_provisioned_figures(ctx, s, complete=True)
        if ctx.project.evidence_check:
            self._guard_claim_files(ctx)
            if not s.get("claims_ref"):
                raise ValueError("bound claims must be registered before manuscript")
        parents = self._paper_parents(s)
        ref = ctx.archive.put(
            "paper", "paper", _path(ctx, "paper"), parents=parents, producer="agent:writing", exclude=PAPER_EXCLUDE
        )
        if ctx.archive.version_info(ref).parents != parents:
            raise ValueError("archived manuscript parents differ from validated provenance; reconcile explicitly")
        return ref

    def _register_build(self, ctx, paper):
        report_path = _path(ctx, "paper/build/compile_report.json")
        report = CompileReport.model_validate_json(report_path.read_bytes())
        if not report.ok and not (not ctx.project.dev.compile and report.engine == "disabled"):
            raise ValueError("unsuccessful compilation cannot be submitted")
        paths = [("paper/build/compile_report.json", "other")]
        if report.ok:
            paths.append(("paper/build/main.pdf", "paper"))
        refs = []
        for path, kind in paths:
            ref = ctx.archive.put(path, kind, _path(ctx, path), parents=[paper], producer="agent:writing")
            if ctx.archive.version_info(ref).parents != [paper]:
                raise ValueError("build artifact retains stale manuscript parent")
            refs.append(ref)
        return refs

    def _verification_basis(self, ctx, s, paper):
        bundle = EvidenceBundle.model_validate_json(ctx.archive.get(_ref(s["bundle_ref"])))
        ids = sorted({rid for a in bundle.aggregates for row in a.data.rows for rid in row.run_ids})
        views = []
        for rid in ids:
            try:
                view = runs.load_run(ctx.root, rid)
                views.append(
                    {
                        "id": rid,
                        "metrics": [m.model_dump(mode="json") for m in view.metrics],
                        "status": view.status.state.value,
                        "kind": view.record.kind,
                        "condition": view.record.eval_condition,
                        "plan": view.record.plan_ref.model_dump(),
                        "label": view.label.model_dump(mode="json") if view.label else None,
                    }
                )
            except (AirError, ValueError, OSError):
                views.append({"id": rid, "missing": True})
        return _hash([paper.model_dump(), views, sha256_file(evidence.__file__), sha256_file(runs.__file__)])

    def _verify(self, ctx, s, meta, writer):
        self._validate_provisioned_figures(ctx, s, complete=True)
        self._guard_claim_files(ctx)
        if meta.get("compiled", {}).get("source_sha256") != self._source_sha(ctx):
            s["phase"] = "write"
            return Continue("Source changed before deterministic verification; refresh locations and compile")
        paper = self._register_paper(ctx, s)
        self._register_build(ctx, paper)
        basis = self._verification_basis(ctx, s, paper)
        name = "check-" + basis
        receipt = _receipt(ctx, name)
        if not receipt:
            report = evidence.check(ctx.root, paper_ref=paper, mode="full")
            receipt = {"report": report.model_dump(mode="json")}
            _save(ctx, name, receipt)
        report = CheckReport.model_validate(receipt["report"])
        if report.paper_ref != paper:
            raise ValueError("persisted check report has the wrong manuscript basis")
        s.update(deterministic_basis=basis, deterministic_report=receipt["report"], phase="review")
        return self._review(ctx, s, meta, writer)

    def _incomplete(self, unit):
        return Issue(code="SEMANTIC_INCOMPLETE", severity="major", claim_id=unit["claim_id"],
                     location=unit["location"], sentence=unit["sentence"],
                     detail="Responses exhausted / user skipped. Semantic review INCOMPLETE; human approval required.")

    def _semantic_route(self, ctx, prompt_id, role):
        prompt = ctx.llm.prompts.load(prompt_id, ctx.project.prompt_pins.get(prompt_id))
        skill = ctx.skills.load("claim-review")
        return {
            "prompt": [prompt.id, prompt.version, sha256_file(prompt.path)],
            "models": [
                {"alias": m.alias, "model": m.entry.model, "config_sha256": _hash(m.entry.model_dump(mode="json"))}
                for m in ctx.llm.resolve(role, prompt_id, "strong", prompt.tier)
            ],
            "skill_ref": skill.ref,
            "skill_sha256": _hash(skill.instructions),
            "policy": sha256_file(semantic.__file__),
        }

    def _review_basis(self, ctx, s):
        return _hash(
            [
                s["claims_ref"],
                s["deterministic_report"],
                self._semantic_route(ctx, semantic.REVIEW_PROMPT, "writing.reviewer"),
            ]
        )

    def _semantic_call(self, ctx, prompt, role, schema, context):
        skill = ctx.skills.load("claim-review")
        return ctx.llm.complete(
            role=role,
            prompt_id=prompt,
            tier="strong",
            max_retries=0,
            response_schema=schema,
            variables={
                "schema": schema.model_json_schema(),
                "skill_ref": skill.ref,
                "skill_rules": skill.instructions,
                "context": ctx.llm.wrap_untrusted(json.dumps(context), "exact-sentence"),
            },
        )

    def _review(self, ctx, s, meta, writer):
        report = CheckReport.model_validate(s["deterministic_report"])
        paper = report.paper_ref
        if meta.get("compiled", {}).get("source_sha256") != self._source_sha(ctx) or s[
            "deterministic_basis"
        ] != self._verification_basis(ctx, s, paper):
            s["phase"] = "verify"
            return Continue("Manuscript/evidence changed before semantic review; recheck")
        basis = self._review_basis(ctx, s)
        name = "semantic-" + basis
        receipt = _receipt(ctx, name)
        if not receipt:
            source = ctx.archive.stored_path(paper)
            units = semantic.units(source, writer.claims, writer.bundle, report)
            numbers = [int(p.stem.removeprefix("review_v")) for p in _path(ctx, "paper/review").glob("review_v*.json")]
            # Reserve a number before calls; a crash cannot overwrite an older review.
            for path in _path(ctx, "paper/.writing").glob("semantic-*.json"):
                previous = json.loads(path.read_text(encoding="utf-8"))
                if "number" in previous:
                    numbers.append(previous["number"])
            cycle = self._cycle(s)
            history = meta.setdefault("semantic_rounds", {}).setdefault(cycle, [])
            receipt = {
                "number": max(numbers, default=0) + 1,
                "round": len(history) + 1,
                "revision_rounds": len(meta.get("semantic_revisions", [])),
                "revision_receipts": list(meta.get("semantic_revisions", [])),
                "cycle": cycle,
                "basis": basis,
                "claims_ref": s["claims_ref"],
                "route": self._semantic_route(ctx, semantic.REVIEW_PROMPT, "writing.reviewer"),
                "units": units,
                "responses": [],
                "report": None,
            }
            _save(ctx, name, receipt)
        issues = []
        for index, unit in enumerate(receipt["units"]):
            if index < len(receipt["responses"]) and "rejected" not in receipt["responses"][index]:
                saved = receipt["responses"][index]
                if saved.get("skipped"):
                    issues.append(self._incomplete(unit))
                    continue
                try:
                    issues.extend(semantic.normalize(unit, saved["response"]))
                except ValueError:
                    pass  # Legacy response was persisted before failed normalization.
                else:
                    continue
            saved = attempts.complete(
                ctx, f"{name}-{index}-attempts", _receipt, _save,
                lambda unit=unit: self._semantic_call(ctx, semantic.REVIEW_PROMPT, "writing.reviewer",
                                            semantic.ReviewResponse, semantic.model_context(unit)),
                lambda value, unit=unit: semantic.normalize(unit, value),
            )
            result = saved or {"skipped": True}
            if index == len(receipt["responses"]):
                receipt["responses"].append(result)
            else:
                receipt.setdefault("legacy_rejected", []).append(receipt["responses"][index])
                receipt["responses"][index] = result
            _save(ctx, name, receipt)
            issues.extend(semantic.normalize(unit, saved["response"]) if saved else [self._incomplete(unit)])
        issues.extend(self._incomplete(x["unit"]) for x in meta.get("semantic_incomplete", [])
                      if x["cycle"] == self._cycle(s))
        combined = semantic.merge(report, issues)
        receipt["report"] = combined.model_dump(mode="json")
        _save(ctx, name, receipt)
        content = {
            "json": binding.encoded(receipt["report"]) + b"\n",
            "md": semantic.markdown(combined, receipt).encode("utf-8"),
        }
        refs = {}
        for suffix, data in content.items():
            path = f"paper/review/review_v{receipt['number']}.{suffix}"
            target = _path(ctx, path)
            if target.exists() and target.read_bytes() != data:
                raise ValueError("historical review report changed; explicit reconciliation required")
            atomic_write_bytes(target, data)
            ref = ctx.archive.put(
                path,
                "review_report" if suffix == "json" else "other",
                target,
                parents=[paper, _ref(s["claims_ref"])],
                producer="agent:writing/review",
            )
            if ctx.archive.version_info(ref).parents != [paper, _ref(s["claims_ref"])]:
                raise ValueError("review report retains stale manuscript parents")
            refs[suffix] = ref
        history = meta.setdefault("semantic_rounds", {}).setdefault(receipt["cycle"], [])
        if basis not in history:
            history.append(basis)
        _save(ctx, "runtime", meta)
        targets = [(u, items) for u, items in semantic.revision_targets(combined, receipt["units"])
                   if not any(i.code == "SEMANTIC_INCOMPLETE" for i in items)]
        more = len(history) < semantic.round_limit(ctx.project.paper.get("max_review_rounds", 2))
        s.update(
            review_ref=refs["json"].model_dump(),
            review_basis=s["deterministic_basis"],
            semantic_basis=basis,
            phase="revise" if targets and more else "submit",
        )
        return Continue("Combined deterministic and semantic review persisted")

    def _revise(self, ctx, s, meta, writer):
        if s["semantic_basis"] != self._review_basis(ctx, s):
            s["phase"] = "review"
            return Continue("Semantic dependencies changed before revision; rereview")
        if meta["compiled"]["source_sha256"] != self._source_sha(ctx):
            s["phase"] = "write"
            return Continue("Human source edit before revision; preserve and recheck")
        review = _receipt(ctx, "semantic-" + s["semantic_basis"])
        report = CheckReport.model_validate(review["report"])
        targets = semantic.revision_targets(report, review["units"])
        targets = [
            (u, issues)
            for u, issues in targets
            if semantic.comparison_revisable(u, next((c for c in writer.claims if c.claim_id == u["claim_id"]), None))
        ]
        route = self._semantic_route(ctx, semantic.REVISE_PROMPT, "writing.reviser")
        basis = _hash(
            [
                s["semantic_basis"],
                route,
                [c.model_dump() for c in writer.claims],
                [(u, [i.model_dump() for i in issues]) for u, issues in targets],
            ]
        )
        # Issue models are serialized explicitly for the receipt identity.
        name = "revision-" + basis
        receipt = _receipt(ctx, name)
        if not receipt:
            receipt = {"basis": basis, "review_basis": s["semantic_basis"], "route": route, "responses": []}
            _save(ctx, name, receipt)
        claims = [c.model_copy(deep=True) for c in writer.claims]
        sections = {u["section"] for u, _ in targets}
        old = {
            f"paper/sections/{section}.tex": _path(ctx, f"paper/sections/{section}.tex").read_bytes().decode("utf-8")
            for section in sections
        }
        new = dict(old)
        # Descending offsets allow multiple exact targets in one section without relocating anchors.
        targets = sorted(targets, key=lambda item: (item[0]["section"], -item[0]["location"]["start"]))
        for index, (unit, issues) in enumerate(targets):
            claim = next((c for c in claims if c.claim_id == unit["claim_id"]), None)
            context, placeholders = semantic.revision_context(unit, issues, claim)
            if index < len(receipt["responses"]) and not receipt["responses"][index].get("skipped"):
                previous = receipt["responses"][index]
                try:
                    if "rejected" in previous:
                        raise ValueError(previous["rejected"])
                    semantic.validate_revision(context, placeholders, previous["response"], claim)
                except ValueError:
                    if len(receipt["responses"]) != index + 1:
                        raise ValueError("Ambiguous legacy revision response order; reconcile explicitly") from None
                    receipt.setdefault("legacy_rejected", []).append(receipt["responses"].pop())
                    _save(ctx, name, receipt)
            if index == len(receipt["responses"]):
                history = meta.get("semantic_rounds", {}).get(self._cycle(s), [])
                if len(history) >= semantic.round_limit(ctx.project.paper.get("max_review_rounds", 2)):
                    s["phase"] = "submit"
                    return Continue("Current review-round budget exhausted; retain residual issues for human review")
                saved = attempts.complete(
                    ctx, f"{name}-{index}-attempts", _receipt, _save,
                    lambda context=context: self._semantic_call(ctx, semantic.REVISE_PROMPT, "writing.reviser",
                                                semantic.RevisionResponse, context),
                    lambda value, context=context, placeholders=placeholders, claim=claim:
                        semantic.validate_revision(context, placeholders, value, claim),
                )
                receipt["responses"].append(saved or {"skipped": True})
                _save(ctx, name, receipt)
            saved = receipt["responses"][index]
            if saved.get("skipped"):
                note = {"unit": unit, "reason": "user skipped revision", "cycle": self._cycle(s)}
                if note not in meta.setdefault("semantic_incomplete", []):
                    meta["semantic_incomplete"].append(note)
                _save(ctx, "runtime", meta)
                continue
            if "rejected" in saved:
                # Migrate old rejected receipts into the bounded recovery path.
                receipt.setdefault("legacy_rejected", []).append(receipt["responses"].pop())
                _save(ctx, name, receipt)
                return Continue("Legacy rejected revision retained; bounded recovery pending")
            replacement, revised = semantic.validate_revision(context, placeholders, saved["response"], claim)
            relative = unit["location"]["file"]
            a, b = unit["location"]["start"], unit["location"]["end"]
            if new[relative][a:b] != unit["source"]:
                raise ValueError("revision exact source anchor changed")
            new[relative] = new[relative][:a] + replacement + new[relative][b:]
            if revised:
                claims = [revised if c.claim_id == revised.claim_id else c for c in claims]
        if new == old:
            s["phase"] = "submit"
            return Continue("Revision made no change; preserve remaining issues for human inspection")
        # Compute final locations before the transaction, so assembly does not create a second claims write.
        for claim in claims:
            relative = f"paper/sections/{claim.section}.tex"
            text = new.get(relative, _path(ctx, relative).read_bytes().decode("utf-8"))
            start = next(
                m.start()
                for m in re.finditer(re.escape(claim.text), text)
                if binding.active_text(text)[m.start() : m.start() + 6] == r"\claim"
            )
            claim.location = {"file": relative, "line": text[:start].count("\n") + 1}
        writer.claims = claims
        for section in sections:
            writer.validate_section(SectionOutput(section=section, latex=new[f"paper/sections/{section}.tex"]), section)
        if binding.values_tex(claims) != _path(ctx, "paper/air_values.tex").read_bytes():
            raise ValueError("revision changed code-owned numeric values")
        if meta["compiled"]["source_sha256"] != self._source_sha(ctx):
            raise ValueError("source changed during semantic revision; preserve human edits and recheck")
        old["paper/claims.jsonl"] = _path(ctx, "paper/claims.jsonl").read_text(encoding="utf-8")
        new["paper/claims.jsonl"] = binding.claims_bytes(claims).decode()
        old["paper/air_values.tex"] = _path(ctx, "paper/air_values.tex").read_text(encoding="utf-8")
        new["paper/air_values.tex"] = binding.values_tex(claims).decode("utf-8")
        transaction = {
            "basis": basis,
            "review_basis": s["semantic_basis"],
            "claim_basis": s["claim_basis"],
            "claims": [c.model_dump(mode="json") for c in claims],
            "old": old,
            "new": new,
            "source_hashes": dir_manifest(_path(ctx, "paper"), PAPER_EXCLUDE),
            "affected_claims": [u["claim_id"] for u, _ in targets],
            "responses": receipt["responses"],
            "installed": False,
            "assembled": False,
        }
        _save(ctx, "pending-revision", transaction)
        self._finish_revision(ctx, transaction)
        s["phase"] = "revision_assemble"
        return Continue("Accepted targeted revision installed; reassembly and compilation required")

    def _finish_revision(self, ctx, pending):
        if pending.get("installed"):
            for relative, new in pending["new"].items():
                path = _path(ctx, relative)
                if not path.exists() or path.read_bytes() != new.encode():
                    raise recovery.ReconcileRequired(
                        "pending semantic revision conflicts with an intervening human edit: "
                                                     + relative, {relative: new.encode()})
            return
        # Validate ALL targets before writing ANY: half-installed states allow only exact old/new bytes.
        for relative, new in pending["new"].items():
            path = _path(ctx, relative)
            if not path.exists() or path.read_bytes() not in {pending["old"][relative].encode(), new.encode()}:
                raise recovery.ReconcileRequired(
                        "pending semantic revision conflicts with an intervening human edit: "
                                                     + relative, {relative: new.encode()})
        current = dir_manifest(_path(ctx, "paper"), PAPER_EXCLUDE)
        excluded = {p.removeprefix("paper/") for p in pending["new"]}
        if {k: v for k, v in current if k not in excluded} != {
            k: v for k, v in pending["source_hashes"] if k not in excluded
        }:
            raise ValueError("pending semantic revision conflicts with an intervening source edit")
        for relative, new in pending["new"].items():
            atomic_write_bytes(_path(ctx, relative), new.encode())
        _save(ctx, "bound-claims", {"basis": pending["claim_basis"], "claims": pending["claims"]})
        pending["installed"] = True
        _save(ctx, "pending-revision", pending)

    def _revision_assemble(self, ctx, s, meta, writer):
        pending = _receipt(ctx, "pending-revision")
        for section in SECTIONS:
            path = _path(ctx, f"paper/sections/{section}.tex")
            receipt = _receipt(ctx, f"section-{section}")
            output = writer.read_section(section)
            relative = f"paper/sections/{section}.tex"
            expected = pending["new"].get(relative)
            authorized_sha = sha256_bytes(expected.encode()) if expected is not None else receipt.get("file_sha256")
            if sha256_file(path) != authorized_sha:
                receipt["human_edited"] = True
            receipt.update(
                fingerprint=_hash([writer.section_fingerprint(section), s["writing_config"], None]),
                output=output.model_dump(),
                file_sha256=sha256_file(path),
                installed=True,
            )
            _save(ctx, f"section-{section}", receipt)
        self._assemble(ctx, s, meta, writer)
        revisions = meta.setdefault("semantic_revisions", [])
        if pending["basis"] not in revisions:
            revisions.append(pending["basis"])
        _save(ctx, "runtime", meta)
        pending["assembled"] = True
        _save(ctx, "pending-revision", pending)
        s["phase"] = "compile"
        return Continue("Revised manuscript assembled; compile, deterministic recheck and semantic rereview required")

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
                    or manifest_bytes(dir_manifest(safe_path(archived, root=ctx.root), [])) != archived_manifest
                    or manifest_bytes(dir_manifest(directory, [])) != archived_manifest
                ):
                    raise ValueError(f"Figure artifact no longer matches archived version: {fid}")
                figure = Figure.model_validate_json((directory / "figure.json").read_bytes())
                if figure.fig_id != fid or figure.aggregate_ref != item.ref:
                    raise ValueError(f"incorrect Figure metadata identity: {fid}")
                problems = validate_artifact(
                    directory,
                    item.data,
                    item.ref,
                    script_path=archived / "plot.py",
                    skill_ref=figure.skill_ref,
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

    def _answer_figure_unit(self, ctx, wait, figure_keys, selected):
        fid = wait["fig_id"]
        receipt = _receipt(ctx, f"figure-{fid}")
        record = ctx.questions.record(ctx.answer.question_id)
        latest = ctx.questions.records()[-1].question.question_id
        if (
            figure_keys.get(fid) != wait["key"]
            or _ref(wait["aggregate_ref"]) not in selected
            or receipt.get("key") != wait["key"]
            or _hash(receipt.get("original_proposal")) != wait["proposal_hash"]
            or receipt.get("spec", {}).get("metric") != wait["metric"]
            or _hash({k: v for k, v in receipt.get("spec", {}).items() if k != "ylabel"}) != wait["spec_basis"]
            or record.question.stage != "writing"
            or record.question.text != wait["question_text"]
            or record.question.refs != [_ref(wait["aggregate_ref"])]
            or record.question.status != "answered"
            or record.answer != ctx.answer
            or latest != ctx.answer.question_id
            or ctx.answer.question_id == wait["previous_question_id"]
        ):
            raise ValueError("stale Figure unit answer; dependencies/proposal/question changed; reconcile or resume")
        if ctx.answer.answered_by != "user" or not ctx.answer.text.strip():
            raise ValueError("Figure unit requires nonempty explicit user clarification")
        clarifications = receipt.setdefault("clarifications", [])
        if any(c["answer"]["question_id"] == ctx.answer.question_id for c in clarifications):
            return  # Receipt persisted before the engine consumed this answer.
        if len(clarifications) >= 3:
            raise ValueError("Figure unit clarification exhausted after 3 attempts")
        clarifications.append(
            {"question": record.question.model_dump(mode="json"), "answer": ctx.answer.model_dump(mode="json")}
        )
        receipt["spec"]["ylabel"] = ctx.answer.text.strip()
        receipt["ylabel_source"] = {
            "kind": "user_clarification",
            "question_id": ctx.answer.question_id,
            "fig_id": fid,
            "key": receipt["key"],
            "aggregate_ref": wait["aggregate_ref"],
        }
        _save(ctx, f"figure-{fid}", receipt)

    def _ask_figure_unit(self, ctx, meta, item, receipt, reason=""):
        if len(receipt.get("clarifications", [])) >= 3:
            raise ValueError("Figure unit clarification exhausted after 3 attempts: " + reason)
        spec = receipt["spec"]
        text = (
            f'Provide the y-axis label for metric "{spec["metric"]}", including its unit '
            'or an explicit dimensionless/unitless designation (for example, "Latency (ms)" '
            'or "score (dimensionless)").'
        )
        if reason:
            text += " Previous clarification was invalid: " + reason
        records = ctx.questions.records()
        result = self._ask(
            ctx,
            meta,
            "figure_unit",
            text,
            [],
            fig_id=spec["fig_id"],
            key=receipt["key"],
            aggregate_ref=item.ref.model_dump(),
            metric=spec["metric"],
            proposal_hash=_hash(receipt["original_proposal"]),
            spec_basis=_hash({k: v for k, v in spec.items() if k != "ylabel"}),
            question_text=text,
            previous_question_id=records[-1].question.question_id if records else None,
        )
        result.question.refs = [item.ref]
        return result

    def _figure_unit_key(self, bundle, spec):
        # A label describes displayed values. Scope reuse to the same approved plan,
        # task definition, exact metric and display scale; never infer compatibility.
        return _hash([bundle.plan_ref.model_dump(), bundle.runtime.task_sha256, spec.metric, spec.scale])

    def _figures(self, ctx, s, meta, bundle, figure_keys, figure_basis):
        skill = ctx.skills.load("scientific-plotting")
        for item in bundle.aggregates:
            validate_source(item.data)
            fid = f"fig_{item.aggregate_id.lower()}"
            key = figure_keys[fid]
            receipt = _receipt(ctx, f"figure-{fid}")
            superseded = None
            if receipt and receipt.get("key") != key:
                # Changed renderer/model dependencies must not erase an old raw
                # proposal, including invalid legacy caches from a previous release.
                path = f"paper/.writing/figure-{fid}.json"
                superseded = ctx.archive.put(path, "other", _path(ctx, path),
                                             producer="agent:writing/figure-planning").model_dump()
                if receipt.get("status") != "rejected":
                    try:
                        figure_planning.validate(item, fid, receipt["spec"])
                    except ValueError as exc:
                        # A release can change the renderer key without changing
                        # evidence. Count the recorded legacy call conservatively;
                        # upgrading must not turn it into another free first try.
                        figure_planning.reject(ctx, item, fid, key, receipt, exc, _receipt, _save)
                        return Continue("Rejected legacy Figure proposal retained; bounded repair pending")
            if receipt.get("key") != key or receipt.get("status") == "rejected":
                units = bundle.runtime.task.get("metric_units", {})
                metric = item.data.comparison.get("metric")
                spec = None
                default_reason = ""
                if metric in units and receipt.get("status") != "rejected":
                    try:
                        spec = default_spec(item.data, fig_id=fid, ylabel=f"{metric} ({units[metric]})")
                    except ValueError as exc:
                        if not presentation_error(exc):
                            raise ValidationFailed(f"Invalid Aggregate evidence: {exc}") from exc
                        default_reason = str(exc)
                if spec is not None:
                    receipt = {"key": key, "spec": spec.model_dump()}
                else:
                    def request(reason, fid=fid, item=item, default_reason=default_reason):
                        return ctx.llm.complete(
                            role="writing.figure_spec", prompt_id=FIGURE_PROMPT, tier="strong", max_retries=0,
                            response_schema=PlannedFigure,
                            variables={
                                "schema": PlannedFigure.model_json_schema(),
                                "evidence": ctx.llm.wrap_untrusted(json.dumps({
                                    "fig_id": fid, "aggregate": item.data.model_dump(mode="json"),
                                    "task": bundle.runtime.task, "rejection_reason": reason or default_reason,
                                    "presentation": "Represent all varying dimensions with x, hue and optional facets; "
                                                    "at most 16 panels. Never pool configurations.",
                                }, ensure_ascii=False), "figure-input"),
                            },
                        )
                    receipt = figure_planning.propose(ctx, item, fid, key, _receipt, _save, request)
                if superseded:
                    receipt["supersedes"] = superseded
                # The attempt journal already retained the reply before validation.
                _save(ctx, f"figure-{fid}", receipt)
            try:
                figure_planning.validate(item, fid, receipt["spec"])
            except ValueError as exc:
                if receipt.get("ylabel_source") and isinstance(exc, UnitLabelError):
                    if receipt.get("clarifications"):
                        receipt["clarifications"][-1]["validation_error"] = str(exc)
                        _save(ctx, f"figure-{fid}", receipt)
                    return self._ask_figure_unit(ctx, meta, item, receipt, str(exc))
                figure_planning.reject(ctx, item, fid, key, receipt, exc, _receipt, _save)
                return Continue("Rejected Figure proposal retained; bounded repair pending")
            spec = FigureSpec.model_validate(receipt["spec"])
            if not spec.xlabel.strip():
                rows = [r for r in item.data.rows if r.metric == spec.metric]
                if not rows or not all(spec.x in r.group for r in rows):
                    raise ValueError("unknown grouping field in x; cannot normalize xlabel")
                spec.xlabel = spec.x
                receipt["spec"] = spec.model_dump()
                _save(ctx, f"figure-{fid}", receipt)
            unit_key = self._figure_unit_key(bundle, spec)
            known = meta.get("figure_units", {}).get(unit_key)
            if not spec.ylabel.strip() and known:
                spec.ylabel = known["ylabel"]
                receipt.update(spec=spec.model_dump(), ylabel_source=known["source"])
                _save(ctx, f"figure-{fid}", receipt)
            if not spec.ylabel.strip() and "original_proposal" in receipt:
                return self._ask_figure_unit(ctx, meta, item, receipt)
            try:
                prepare_data(item.data, spec)
            except ValueError as exc:
                # Only a rejected user-supplied unit returns to clarification.
                # Presentation failures repair within a persisted budget. Source
                # integrity errors escape as ValidationFailed and still fail closed.
                if receipt.get("ylabel_source") and isinstance(exc, UnitLabelError):
                    if receipt.get("clarifications"):
                        receipt["clarifications"][-1]["validation_error"] = str(exc)
                        _save(ctx, f"figure-{fid}", receipt)
                    return self._ask_figure_unit(ctx, meta, item, receipt, str(exc))
                if presentation_error(exc):
                    figure_planning.reject(ctx, item, fid, key, receipt, exc, _receipt, _save)
                    return Continue("Rejected Figure proposal retained; bounded repair pending")
                raise
            if receipt.get("status") == "pending_unit":
                receipt["status"] = "accepted"
                _save(ctx, f"figure-{fid}", receipt)
            if receipt.get("ylabel_source"):
                meta.setdefault("figure_units", {})[unit_key] = {
                    "ylabel": spec.ylabel,
                    "source": receipt["ylabel_source"],
                }
                _save(ctx, "runtime", meta)
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
            s.update(entry=check.entry, phase="claims" if ctx.project.evidence_check else "write")
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
            fingerprint = _hash(
                [
                    writer.section_fingerprint(section),
                    s["writing_config"],
                    s["figures"] if writer.claims is None else None,
                ]
            )
            receipt = _receipt(ctx, f"section-{section}")
            path = _path(ctx, f"paper/sections/{section}.tex")
            if not receipt and path.exists():
                raise recovery.ReconcileRequired(f"unreceipted existing section {section}",
                                                {f"paper/sections/{section}.tex": None})
            if receipt.get("fingerprint") == fingerprint:
                if not receipt.get("installed", True) or not path.exists():
                    output = SectionOutput.model_validate(receipt["output"])
                    writer.validate_section(output, section)
                    expected = _section_bytes(output)
                    if path.exists():
                        if path.read_bytes() != expected:
                            raise recovery.ReconcileRequired(
                                f"pending generated section {section} conflicts with local edit",
                                                            {f"paper/sections/{section}.tex": expected})
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
                raise recovery.ReconcileRequired(f"human-edited {section} has stale factual/writing inputs",
                                                {f"paper/sections/{section}.tex": None,
                                                 f"paper/.writing/section-{section}.json": None})
            ctx.progress(
                f"Paper: writing {section.replace('_', ' ')}",
                5 if ctx.project.evidence_check else 4,
                8 if ctx.project.evidence_check else 6,
                "steps",
            )
            if writer.claims is None:
                generated = writer.generate_section(section)
            else:
                name = "section-" + section + "-" + fingerprint + "-attempts"
                def response_for(saved):
                    return SimpleNamespace(parsed=saved["response"], call_id=saved["call_id"],
                                           prompt_id=saved["prompt_id"], prompt_version=saved["prompt_version"])
                def request(section=section, name=name):
                    journal = _receipt(ctx, name)
                    history = journal.get("attempts", [])
                    reason = next((a.get("reason", "") for a in reversed(history) if a.get("reason")), "")
                    return writer.request_section(section, rejection_reason=reason)
                def validate_payload(payload, section=section):
                    try:
                        writer.validate_generated(section, payload)
                    except ValidationFailed as exc:
                        raise ValueError(str(exc)) from exc
                saved = attempts.complete(ctx, name, _receipt, _save, request, validate_payload, allow_skip=False)
                generated = writer.finish_section(section, response_for(saved))
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
        if writer.claims is not None:
            self._finalize_claims(ctx, s, writer)
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
            (
                "Evidence checking is enabled (evidence\\_check=true). Claim planning used writing.claims; "
                "experimental values are resolved deterministically by code. Deterministic evidence verification "
                "is followed by semantic review using writing.reviewer. Bounded targeted revision may use "
                "writing.reviser. Completed review and revision details are recorded in review artifacts."
                if ctx.project.evidence_check
                else "Evidence checking is disabled (control arm, evidence\\_check=false). "
                "Compilation and local offline citation checks do not establish scientific correctness."
            ),
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
            if writer.claims is not None:
                binding.validate_preamble(main.read_text(encoding="utf-8"))
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
            [
                dir_manifest(_path(ctx, "paper"), [*PAPER_EXCLUDE, "ai_use.tex"]),
                lines,
                s["figures"],
                s.get("claims_ref"),
            ]
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
            s["phase"] = "verify" if ctx.project.evidence_check else "submit"
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
        paper = self._register_paper(ctx, s)
        if ctx.project.evidence_check:
            if s.get("review_basis") != self._verification_basis(ctx, s, paper):
                s["phase"] = "verify"
                return Continue("Exact manuscript/check basis changed; deterministic verification required")
            if s.get("semantic_basis") != self._review_basis(ctx, s):
                s["phase"] = "review"
                return Continue("Reviewer prompt/model/skill changed; semantic rereview required")
            ref = _ref(s["review_ref"])
            report = CheckReport.model_validate_json(ctx.archive.get(ref))
            if report.paper_ref != paper or paper not in ctx.archive.version_info(ref).parents:
                raise ValueError("review report does not correspond to the exact manuscript")
            incomplete = [x for x in meta.get("semantic_incomplete", []) if x["cycle"] == self._cycle(s)]
            if incomplete:
                note_name = "semantic-incomplete-" + _hash([ref.model_dump(), incomplete])
                note = _receipt(ctx, note_name)
                if not note:
                    report = semantic.merge(report, [self._incomplete(x["unit"]) for x in incomplete
                        if not any(i.code == "SEMANTIC_INCOMPLETE" and i.sentence == x["unit"]["sentence"]
                                   for i in semantic.all_issues(report))])
                    numbers = [int(p.stem.removeprefix("review_v"))
                               for p in _path(ctx, "paper/review").glob("review_v*.json")]
                    for p in _path(ctx, "paper/.writing").glob("semantic-*.json"):
                        numbers.append(json.loads(p.read_text(encoding="utf-8")).get("number", 0))
                    note = {"number": max(numbers, default=0) + 1, "report": report.model_dump(mode="json")}
                    _save(ctx, note_name, note)
                report = CheckReport.model_validate(note["report"])
                if report.paper_ref != paper:
                    raise ValueError("Incomplete review report belongs to a different manuscript")
                path = f"paper/review/review_v{note['number']}.json"
                data = binding.encoded(note["report"]) + b"\n"
                target = _path(ctx, path)
                if target.exists() and target.read_bytes() != data:
                    raise ValueError("Incomplete review report changed; reconcile explicitly")
                atomic_write_bytes(target, data)
                ref = ctx.archive.put(path, "review_report", target,
                                      parents=[paper, _ref(s["claims_ref"])], producer="agent:writing/review")
            extra = [ref, _ref(s["claims_ref"]), *self._register_build(ctx, paper)]
            s["paper_ref"] = paper.model_dump()
            s["extra_refs"] = [r.model_dump() for r in extra]
            counts = report.counts
            return NeedsApproval(
                target=paper,
                kind="manuscript",
                extra_refs=extra,
                summary=("SEMANTIC INCOMPLETE; HUMAN REVIEW REQUIRED. " if incomplete or
                         any(i.code == "SEMANTIC_INCOMPLETE" for i in semantic.all_issues(report)) else "")
                + f"{'RESIDUAL BLOCKER/MAJOR ISSUES: ' if counts.get('blocker') or counts.get('major') else ''}"
                f"Manuscript v{paper.version}: {len(report.claims)} claims; combined review: "
                f"{counts.get('supported', 0)} supported, {counts.get('partially_supported', 0)} partially supported, "
                f"{counts.get('unsupported', 0)} unsupported, {counts.get('contradicted', 0)} contradicted, "
                f"{counts.get('unverifiable', 0)} unverifiable; "
                f"{counts.get('blocker', 0)} blockers, {counts.get('major', 0)} major issues; "
                f"{'compiled' if report.compile_ok else 'compilation disabled'}. "
                f"Semantic review rounds: {len(meta.get('semantic_rounds', {}).get(self._cycle(s), []))}; "
                f"automatic revision rounds: {len(meta.get('semantic_revisions', []))}.",
                impact=(
                    "Human inspection is required; review residual issues. "
                    "Approval does not authorize publication/submission."
                ),
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
