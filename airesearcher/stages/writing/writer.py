"""Independent Writer for the evidence-check-disabled arm; no Stage or claim machinery.

Facts come only from the supplied bundle. Local literature is read solely to check its
snapshot and call the official citation/BibTeX services. Provenance mismatches fail
closed before generation; contexts remain inspectable. Validation is structural,
not scientific review. See README.md for persistence and integration boundaries.
"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from airesearcher.core.errors import StaleVersion, ValidationFailed
from airesearcher.core.fsutil import atomic_write_bytes, atomic_write_text, sha256_bytes
from airesearcher.core.models.claim import CompileReport
from airesearcher.llm.gateway import LLMGateway
from airesearcher.services import literature
from airesearcher.skills.loader import SkillLoader

from ._latex import TemplateError, _inactive_spans, _is_escaped, active_text, commands, relative_name, safe_path
from .bundle import EvidenceBundle
from .compile import compile_paper
from .template import inspect_template, prepare_template, resolve_template

SectionName = Literal[
    "abstract", "introduction", "related_work", "method", "setup", "results", "discussion", "conclusion",
]
SECTIONS = ("abstract", "introduction", "related_work", "method", "setup", "results", "discussion", "conclusion")
GENERATION_ORDER = (*SECTIONS[1:], "abstract")
PROMPT_ID = "writing/section"
_KEY = re.compile(r"[A-Za-z0-9_.:+-]+")
_WORDS = set("""section subsection subsubsection paragraph subparagraph begin end item
textbf textit emph texttt textrm textsf textsc underline textsuperscript textsubscript
textbackslash textasciitilde textasciicircum footnote caption label ref pageref cite includegraphics
centering hline cline multicolumn small footnotesize scriptsize tiny normalsize large Large LARGE
quad qquad ldots cdots pm times leq geq approx alpha beta gamma delta mu sigma theta lambda epsilon
frac sqrt sum mathrm mathbf mathit mathsf mathcal left right hspace vspace rule newline linebreak
noindent par percent""".split())
_ENVIRONMENTS = {"itemize", "enumerate", "description", "figure", "figure*", "table", "table*", "center",
                 "tabular", "tabular*", "equation", "equation*", "displaymath", "math"}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)


def _section(name: str) -> None:
    if name not in SECTIONS:
        raise ValidationFailed(f"Unknown writing section: {name}")


def _figures(inventory: Mapping[str, str] | None) -> dict[str, str]:
    result = dict(sorted((inventory or {}).items()))
    for fig_id, path in result.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", fig_id) or path != f"figures/{fig_id}.pdf":
            raise ValidationFailed("Figure inventory requires fig_id -> figures/<fig_id>.pdf")
    return result


def _citation_table(bundle: EvidenceBundle, *, abstracts: bool) -> list[dict]:
    table, seen_ids, seen_keys = [], set(), set()
    for paper in sorted(bundle.papers, key=lambda p: p.paper_id):
        record, status = paper.record, paper.citation
        key = record.get("citation_key")
        if (paper.paper_id in seen_ids or not isinstance(key, str) or not _KEY.fullmatch(key)
                or key in seen_keys or record.get("paper_id") != paper.paper_id
                or status.get("paper_id") != paper.paper_id or status.get("citation_key") != key
                or status.get("bibtex") != record.get("bibtex") or status.get("status") != paper.status):
            raise ValidationFailed("Inconsistent or ambiguous bundled citation identity")
        seen_ids.add(paper.paper_id)
        seen_keys.add(key)
        row = {name: record.get(name) for name in
               ("title", "authors", "year", "venue", "access_level", "source", "url")}
        row.update(paper_id=paper.paper_id, citation_key=key, status=paper.status,
                   note=status.get("note", ""), verification=bundle.literature_source.verification,
                   allowed=paper.status in {"verified", "metadata_only"} and bool(record.get("bibtex", "").strip()))
        if abstracts:
            row["abstract"] = record.get("abstract")
        table.append(row)
    return table


def build_section_context(bundle: EvidenceBundle, section: str, *, language: str | None = None,
                          figures: Mapping[str, str] | None = None) -> dict:
    """Pure section projection; no current files, statistics recomputation or LLM summaries.

    setup and discussion (including limitations) follow the frozen section vocabulary.
    Full observation rows retain value/run alignment; summary sections omit raw seeds.
    """
    _section(section)
    idea = bundle.idea.data or {}
    context = {
        "section": section, "language": language or "Follow the language of the approved research question",
        "idea_ref": bundle.idea_ref.model_dump(), "plan_ref": bundle.plan_ref.model_dump(),
        "provenance_diagnostics": [d.model_dump() for d in
                                   sorted(bundle.diagnostics, key=lambda d: (d.code, d.artifact_id))],
        "provenance_policy": "Any diagnostic blocks generation; never relabel historical evidence as current",
        "literature_verification": bundle.literature_source.verification,
        "citation_table": _citation_table(bundle, abstracts=section in {"introduction", "related_work", "discussion"}),
        "figures": _figures(figures),
    }
    if section in {"introduction", "method"}:
        context["idea"] = {"ref": bundle.idea.ref.model_dump(), "text": bundle.idea.text, "data": idea}
    else:
        context["idea"] = {k: idea.get(k) for k in
                           ("title", "research_question", "hypotheses", "methods", "metrics", "expected_difficulties",
                            "open_questions")}
    if section in {"method", "setup"}:
        context["plan"] = {"ref": bundle.plan.ref.model_dump(), "text": bundle.plan.text, "data": bundle.plan.data}
        if bundle.runtime is not None:
            context["runtime"] = bundle.runtime.model_dump(mode="json")
    if section in {"method", "results", "discussion"}:
        context["approved_logs"] = [{"ref": log.ref.model_dump(), "text": log.text}
                                    for log in sorted(bundle.logs, key=lambda log: log.ref.artifact_id)]
    if section in {"abstract", "introduction", "setup", "results", "discussion", "conclusion"}:
        observations = []
        for item in sorted(bundle.aggregates, key=lambda a: (a.aggregate_id, a.ref.version)):
            data = item.data.model_dump(mode="json", exclude={"created_at", "cost", "script"})
            if section == "setup":
                data.pop("rows")
                data.pop("contrasts")
            elif section not in {"results", "discussion"}:
                data["rows"] = [{k: v for k, v in row.items() if k not in {"values", "run_ids"}}
                                for row in data["rows"]]
            observations.append({"ref": item.ref.model_dump(), "data": data})
        context["aggregates"] = observations
    # Return a detached JSON projection; sorting mappings does not reorder paired arrays.
    return json.loads(_json(context))


class SectionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section: SectionName
    latex: str

    @field_validator("latex")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("LaTeX fragment must not be empty")
        return value


@dataclass(frozen=True)
class GeneratedSection:
    output: SectionOutput
    citations: tuple[str, ...]
    context_sha256: str
    skill_ref: str
    call_id: str
    prompt_id: str
    prompt_version: int


@dataclass(frozen=True)
class DraftResult:
    sections: tuple[GeneratedSection, ...]
    main: Path
    references: Path
    compile_report: CompileReport


class Writer:
    """Use llm=ctx.llm later; no StageContext, archive, approvals or global checkpoint state.

    generate_section -> write_section is separately callable for targeted regeneration.
    read_section validates an existing file, not its factual freshness; the later Stage
    must compare saved input fingerprints before deciding whether to reuse it.
    """

    def __init__(self, root: Path, bundle: EvidenceBundle, *, llm: LLMGateway,
                 skills: SkillLoader | None = None, language: str | None = None,
                 figures: Mapping[str, str] | None = None, guidance: list[dict] | None = None):
        self.root = safe_path(root)
        self.bundle = EvidenceBundle.model_validate_json(bundle.to_bytes())
        self.llm = llm
        self.skill = (skills or SkillLoader()).load("latex-writing")
        self.language = language
        self.figures = _figures(figures)
        self.guidance = json.loads(_json(guidance or []))
        self._allowed = {row["citation_key"]: row["paper_id"] for row in _citation_table(self.bundle, abstracts=False)
                         if row["allowed"]}

    def _target(self, relative: str) -> Path:
        target = safe_path(self.root / relative)
        if self.root not in target.parents or (target.exists() and not target.is_file()):
            raise ValidationFailed(f"Writer target must be a project file: {relative}")
        return target

    def _provenance(self) -> None:
        if self.bundle.diagnostics:
            raise ValidationFailed("Bundle provenance mismatch; rebuild/reconcile sources before writing",
                                   {"diagnostics": [d.model_dump() for d in self.bundle.diagnostics]})

    def validate_section(self, output: SectionOutput, section: str) -> tuple[str, ...]:
        """Conservative fragment/citation checks; direct numeric prose remains allowed."""
        _section(section)
        output = SectionOutput.model_validate(output.model_dump())
        if output.section != section:
            raise ValidationFailed(f"Expected section {section}, received {output.section}")
        text = output.latex
        try:
            text.encode("utf-8")
            if any(ord(c) < 32 and c not in "\r\n\t" for c in text):
                raise ValidationFailed("Control characters in LaTeX fragment")
            clean = active_text(text)
            if "^^" in clean:
                raise ValidationFailed("TeX character-code escape syntax is not supported")
            for token in re.finditer(r"\\([A-Za-z]+|[^\r\n])", clean):
                word = token[1]
                if word.isalpha() and word not in _WORDS:
                    raise ValidationFailed(f"Unsupported or unsafe generated command: \\{word}")
            depth = 0
            for i, char in enumerate(clean):
                if char in "{}" and not _is_escaped(clean, i):
                    depth += 1 if char == "{" else -1
                    if depth < 0:
                        raise ValidationFailed("Unbalanced LaTeX braces")
            if depth:
                raise ValidationFailed("Unbalanced LaTeX braces")
            if not clean.strip() and not any(literal for _, _, literal in _inactive_spans(text)):
                raise ValidationFailed("LaTeX fragment contains only comments")
            stack, cites = [], set()
            parsed_commands = commands(text)
            braced = {command.start for command in parsed_commands}
            for token in re.finditer(r"\\([A-Za-z]+|[^\r\n])", clean):
                if (token[1] in {"begin", "end", "cite", "includegraphics", "ref", "pageref", "label"}
                        and token.start() not in braced):
                    raise ValidationFailed("Use a directly braced, literal command argument")
            for command in parsed_commands:
                if command.name in {"begin", "end"}:
                    environment = command.value.strip()
                    if environment not in _ENVIRONMENTS:
                        raise ValidationFailed(f"Unsupported generated environment: {environment}")
                    if command.name == "begin":
                        stack.append(environment)
                    elif not stack or stack.pop() != environment:
                        raise ValidationFailed("Unbalanced LaTeX environments")
                elif command.name == "cite":
                    for key in command.value.split(","):
                        key = key.strip()
                        if key not in self._allowed:
                            raise ValidationFailed(f"Unknown or unavailable citation: {key}")
                        cites.add(key)
                elif command.name == "includegraphics":
                    path = relative_name(command.value.strip())
                    if path not in self.figures.values():
                        raise ValidationFailed(f"Unknown figure path: {path}")
                elif command.name in {"ref", "pageref", "label"}:
                    if (not re.match(r"\\[A-Za-z]+\s*\{", clean[command.start:])
                            or not _KEY.fullmatch(command.value)):
                        raise ValidationFailed("References/labels require a directly braced, literal label")
                    if command.value.startswith("fig:") and command.value[4:] not in self.figures:
                        raise ValidationFailed(f"Unknown figure ID: {command.value[4:]}")
            if stack:
                raise ValidationFailed("Unbalanced LaTeX environments")
            for start, end, literal in _inactive_spans(text):
                environment = re.match(r"\\begin\s*\{([^}]+)\}", text[start:end]) if literal else None
                if environment and environment[1] not in {"verbatim", "verbatim*"}:
                    raise ValidationFailed(f"Unsupported generated literal environment: {environment[1]}")
            return tuple(sorted(cites))
        except (TemplateError, UnicodeError) as exc:
            raise ValidationFailed(f"Invalid LaTeX fragment: {exc}") from exc

    def section_context(self, section: str) -> dict:
        """Detached context shared by generation and pre-call freshness checks."""
        context = build_section_context(self.bundle, section, language=self.language, figures=self.figures)
        if self.guidance:
            context["user_writing_guidance"] = self.guidance
        return context

    def section_fingerprint(self, section: str, *, prompt_version: int | None = None) -> str:
        """Identity of facts, presentation, guidance, skill and actual versioned prompt."""
        pins = self.llm.project.config.prompt_pins
        prompt = self.llm.prompts.load(PROMPT_ID, prompt_version or pins.get(PROMPT_ID))
        return sha256_bytes(_json({"context": self.section_context(section),
                                   "bundle_sha256": sha256_bytes(self.bundle.to_bytes()),
                                   "skill": self.skill.ref, "instructions": self.skill.instructions,
                                   "prompt": [prompt.id, prompt.version, prompt.system, prompt.user]}).encode("utf-8"))

    def generate_section(self, section: str, *, prompt_version: int | None = None) -> GeneratedSection:
        self._provenance()
        context = self.section_context(section)
        encoded = _json(context)
        response = self.llm.complete(role="writing.writer", prompt_id=PROMPT_ID, tier="strong",
                                     prompt_version=prompt_version, response_schema=SectionOutput,
                                     variables={"section": section, "skill_rules": self.skill.instructions,
                                                "skill_ref": self.skill.ref,
                                                "evidence": LLMGateway.wrap_untrusted(encoded, f"bundle:{section}")})
        output = SectionOutput.model_validate(response.parsed)
        citations = self.validate_section(output, section)
        fingerprint = self.section_fingerprint(section, prompt_version=response.prompt_version)
        return GeneratedSection(output, citations, fingerprint, self.skill.ref, response.call_id,
                                response.prompt_id, response.prompt_version)

    def write_section(self, output: SectionOutput) -> Path:
        self._provenance()
        self.validate_section(output, output.section)
        target = self._target(f"paper/sections/{output.section}.tex")
        atomic_write_text(target, output.latex + ("" if output.latex.endswith("\n") else "\n"))
        return target

    def read_section(self, section: str) -> SectionOutput | None:
        _section(section)
        target = self._target(f"paper/sections/{section}.tex")
        if not target.exists():
            return None
        output = SectionOutput(section=section, latex=target.read_text(encoding="utf-8"))
        self.validate_section(output, section)
        return output

    def _literature_snapshot(self) -> bytes:
        source = self.bundle.literature_source
        path = safe_path(self.root / literature.LITERATURE)
        raw = path.read_bytes() if path.exists() else b""
        if (sha256_bytes(raw) != source.sha256 or (source.mode == "absent") != (not path.exists())
                or source.path != literature.LITERATURE):
            raise StaleVersion("Literature snapshot changed; rebuild EvidenceBundle before generating references")
        return raw

    def generate_references(self) -> Path:
        self._provenance()
        keys = set()
        for section in SECTIONS:
            output = self.read_section(section)
            if output is None:
                raise ValidationFailed(f"Missing required section: {section}")
            keys.update(self.validate_section(output, section))
        raw = self._literature_snapshot()
        # Freeze the checked bytes for service calls: even a transient change/restore
        # of the live JSONL cannot mix a different bibliography into frozen prose.
        with tempfile.TemporaryDirectory(prefix="air-writing-literature-") as tmp:
            snapshot = Path(tmp)
            atomic_write_bytes(snapshot / literature.LITERATURE, raw)
            ids = []
            for key in sorted(keys):
                paper_id = literature.resolve_citation_key(snapshot, key)
                if paper_id != self._allowed[key]:
                    raise StaleVersion(f"Citation cannot resolve to its bundled identity: {key}")
                ids.append(paper_id)
            text = literature.bibtex_for(snapshot, ids)
        self._literature_snapshot()
        entries = re.findall(r"(?mi)^\s*@(?!(?:comment|string|preamble)\b)[A-Za-z]+\s*[{(]\s*([^,\s]+)\s*,", text)
        if set(entries) != keys or len(entries) != len(keys):
            raise ValidationFailed("Official BibTeX output does not contain exactly the cited entry set")
        target = self._target("paper/references.bib")
        atomic_write_text(target, text)
        return target

    def assemble_draft(self, *, template: dict | None = None, entry: str | None = None,
                       title: str | None = None, ai_use: bool = False) -> Path:
        """Bridge prepare_template's new-directory API into an existing section tree.

        No automatic figure copying. Non-main template resource collisions fail before
        replacing main.tex. The official reference service owns references.bib.
        """
        self._provenance()
        # Validate existing sections before touching the assembled main document.
        for section in SECTIONS:
            output = self.read_section(section)
            if output is None:
                raise ValidationFailed(f"Missing required section: {section}")
            for command in commands(output.latex):
                if command.name == "includegraphics" and not self._target("paper/" + command.value.strip()).exists():
                    raise ValidationFailed("Known figure asset has not been provisioned in paper/")
        source = resolve_template(self.root, template, skill=self.skill)
        check = inspect_template(source.original, entry=entry)
        if not check.entry:
            raise ValidationFailed("Template entry selection required", {"candidates": check.entry_candidates})
        original = (source.original / check.entry).read_text(encoding="utf-8")
        declarations = commands(original)
        # These control words need no mandatory braced argument. Scan active tokens
        # as well as parsed declarations, before generating references or copying.
        tokens = {m[1] for m in re.finditer(r"\\([A-Za-z]+|[^\r\n])", active_text(original))}
        if (tokens & {"addbibresource", "printbibliography"}
                or any(c.name == "begin" and c.value.strip() == "thebibliography" for c in declarations)
                or any(c.name in {"usepackage", "RequirePackage"}
                       and "biblatex" in {p.strip() for p in c.value.split(",")} for c in declarations)):
            raise ValidationFailed("Writer requires a BibTeX template; adapt the template explicitly")
        if any(c.name == "bibliography" and c.value != "references" for c in declarations):
            raise ValidationFailed("Template bibliography must use the generated references database")
        body = "\\begin{abstract}\n\\input{sections/abstract}\n\\end{abstract}\n"
        body += "".join(f"\\input{{sections/{section}}}\n" for section in SECTIONS[1:])
        if ai_use:
            if not self._target("paper/ai_use.tex").is_file():
                raise ValidationFailed("Deterministic AI-use source is missing")
            body += "\\input{ai_use}\n"
        else:
            body += "% AIR:AI_USE reserved for deterministic runtime-history integration\n"
        references = self.generate_references()
        if references.stat().st_size:
            if not any(c.name == "bibliographystyle" for c in declarations):
                body += "\\bibliographystyle{plain}\n"
            if not any(c.name == "bibliography" for c in declarations):
                body += "\\bibliography{references}\n"
        with tempfile.TemporaryDirectory(prefix="air-writing-") as tmp:
            prepared = Path(tmp) / "paper"
            prepare_template(source.original, prepared, body, entry=check.entry,
                             title=title if title is not None else (self.bundle.idea.data or {}).get("title"),
                             author="Anonymous")
            copies = []
            for path in sorted(prepared.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(prepared).as_posix()
                if rel == "references.bib":
                    continue
                if rel.startswith(("sections/", "build/", ".writing/", "figures/")) or rel in {
                    "claims.jsonl", "air_values.tex", "ai_use.tex", "evidence_bundle.json",
                }:
                    raise ValidationFailed(f"Template resource conflicts with Writer output: {rel}")
                target = self._target("paper/" + rel)
                content = path.read_bytes()
                if rel != "main.tex" and target.exists() and target.read_bytes() != content:
                    raise ValidationFailed(f"Template resource would overwrite an existing file: {rel}")
                copies.append((target, content))
            for target, content in sorted(copies, key=lambda pair: pair[0].name == "main.tex"):
                atomic_write_bytes(target, content)
        return self._target("paper/main.tex")

    def generate_draft(self, *, template: dict | None = None, entry: str | None = None,
                       title: str | None = None, compile_enabled: bool = False) -> DraftResult:
        """Small standalone integration helper. Failures leave valid sections inspectable.

        Always generates every section; later Stage chooses reuse/regeneration using
        individual APIs and saved fingerprints. Compile failure is returned, never repaired.
        """
        generated = []
        for section in GENERATION_ORDER:
            result = self.generate_section(section)
            self.write_section(result.output)
            generated.append(result)
        main = self.assemble_draft(template=template, entry=entry, title=title)
        report = compile_paper(main.parent, enabled=compile_enabled)
        return DraftResult(tuple(generated), main, main.parent / "references.bib", report)
