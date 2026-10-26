"""Immutable template inspection, explicit selection and deterministic preparation."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from airesearcher.core.fsutil import atomic_write_bytes, atomic_write_text
from airesearcher.core.models.claim import CompileReport
from airesearcher.skills.loader import Skill, SkillLoader

from ._latex import (
    TemplateError,
    _inactive_spans,
    active_text,
    commands,
    document_region,
    relative_name,
    safe_path,
    safe_tree,
)
from .compile import compile_paper


class Dependency(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    name: str
    source: str
    resolution: Literal["local", "system", "missing", "unresolved"]


class TemplateCheck(BaseModel):
    """Writing-owned metadata compatible with backbone template list/check display."""

    model_config = ConfigDict(extra="forbid")
    entry_candidates: list[str]
    entry: str | None = None
    entry_chosen_by: Literal["auto", "user"] | None = None
    missing: list[dict[str, str]] = Field(default_factory=list)
    dependencies: list[Dependency] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    minimal_compile_ok: bool = False
    compile_report: str | None = None
    # Truthful timing belongs in the separate frozen report, not check JSON.
    report: CompileReport | None = Field(default=None, exclude=True)
    compile_log: str | None = Field(default=None, exclude=True)

    @property
    def selection_required(self) -> bool:
        return len(self.entry_candidates) > 1 and self.entry is None

    def to_bytes(self) -> bytes:
        return (json.dumps(self.model_dump(mode="json"), ensure_ascii=False,
                           sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


@dataclass(frozen=True)
class TemplateSource:
    original: Path
    check_path: Path
    report_path: Path
    root: Path

    @property
    def log_path(self) -> Path:
        return self.report_path.with_suffix(".log")


def resolve_template(root: Path, template: dict | None = None, *, skill: Skill | None = None) -> TemplateSource:
    """Resolve explicit ProjectConfig.template; never substitute for an invalid upload."""
    root = safe_tree(root)
    if template is None:
        skill = skill or SkillLoader().load("latex-writing")
        original = safe_tree(skill.root / "templates/default", trusted_root=skill.root)
        check = root / "paper/.template_check_default.json"
        report = root / "paper/.template_compile_default.json"
    else:
        tid, version = template.get("id"), template.get("version")
        if (not isinstance(tid, str) or not re.fullmatch(r"tpl-\d+", tid)
                or type(version) is not int or version < 1):
            raise TemplateError("Template selection must contain a valid id and positive integer version")
        vdir = root / "templates" / tid / f"v{version}"
        original = safe_tree(vdir / "original", trusted_root=root)
        check, report = vdir / "check.json", vdir / "compile_report.json"
    safe_path(check, root=root)
    safe_path(report, root=root)
    return TemplateSource(original, check, report, root)


def _dependency_check(root: Path, entry: str | None) -> list[Dependency]:
    found = {}
    kpsewhich = shutil.which("kpsewhich")
    kinds = {"documentclass": "cls", "usepackage": "sty", "RequirePackage": "sty",
             "LoadClass": "cls", "bibliographystyle": "bst", "bibliography": "bib",
             "addbibresource": "bib", "input": "tex", "include": "tex", "includegraphics": "graphic"}
    # Follow the selected entry's local dependency graph. Unselected examples must
    # not impose their own packages on an explicitly chosen working entry.
    pending = [root / entry] if entry else []
    visited = set()
    while pending:
        path = pending.pop(0)
        if path in visited or path.suffix not in {".tex", ".sty", ".cls"}:
            continue
        visited.add(path)
        for cmd in commands(path.read_text(encoding="utf-8", errors="replace")):
            if cmd.name not in kinds:
                continue
            kind = kinds[cmd.name]
            for value in cmd.value.split(","):
                name = relative_name(value.strip())
                suffix = Path(name).suffix
                choices = ([name] if suffix else [name + ext for ext in (".pdf", ".png", ".jpg", ".eps")]) \
                    if kind == "graphic" else [name if suffix else f"{name}.{kind}"]
                resolution = "missing"
                for candidate in choices:
                    # TeX resolves from the working directory; also recognize resources next to the referring file.
                    local = [root / candidate, path.parent / candidate]
                    provided = next((p for p in local if p.is_file()), None)
                    if provided is not None:
                        resolution = "local"
                        pending.append(provided)
                        break
                    if kpsewhich:
                        try:
                            result = subprocess.run([kpsewhich, candidate], cwd=root, capture_output=True,
                                                    text=True, encoding="utf-8", errors="replace", timeout=10,
                                                    shell=False, check=False)
                        except subprocess.TimeoutExpired:
                            resolution = "unresolved"
                            continue
                        if result.returncode == 0 and result.stdout.strip():
                            resolution = "system"
                            break
                    elif resolution != "local":
                        resolution = "unresolved"
                key = (kind, choices[0], path.relative_to(root).as_posix())
                found[key] = Dependency(kind=kind, name=choices[0], source=key[2], resolution=resolution)
    return [found[key] for key in sorted(found)]


def inspect_template(original: Path, *, entry: str | None = None) -> TemplateCheck:
    """Read only. Ambiguity is data for a later Stage question, never a first-file guess."""
    root = safe_tree(original)
    candidates = []
    for path in sorted(root.glob("*.tex")):
        if any(c.name == "documentclass" for c in commands(path.read_text(encoding="utf-8"))):
            candidates.append(path.name)
    check = TemplateCheck(entry_candidates=candidates)
    if entry is not None:
        if entry not in candidates:
            raise TemplateError("Selected entry is not a detected top-level candidate")
        check.entry, check.entry_chosen_by = entry, "user"
    elif len(candidates) == 1:
        check.entry, check.entry_chosen_by = candidates[0], "auto"
    else:
        check.diagnostics.append("User entry selection required" if candidates else "No top-level documentclass entry")
    check.dependencies = _dependency_check(root, check.entry)
    check.missing = [{"kind": d.kind, "name": d.name} for d in check.dependencies if d.resolution == "missing"]
    check.missing = [dict(t) for t in sorted({tuple(sorted(m.items())) for m in check.missing})]
    if any(d.resolution == "unresolved" for d in check.dependencies):
        check.diagnostics.append("Dependency lookup unresolved: kpsewhich unavailable or timed out")
    if check.entry:
        try:
            document_region((root / check.entry).read_text(encoding="utf-8"))
        except TemplateError as exc:
            check.diagnostics.append(str(exc))
    return check


def check_template(source: TemplateSource, *, entry: str | None = None, enabled: bool = True) -> TemplateCheck:
    """Compile the unchanged template in a disposable copy; does not persist metadata."""
    check = inspect_template(source.original, entry=entry)
    if not check.entry or check.missing or check.diagnostics:
        return check
    with tempfile.TemporaryDirectory(prefix="air-template-") as tmp:
        work = Path(tmp) / "paper"
        shutil.copytree(source.original, work)
        report = compile_paper(work, entry=check.entry, enabled=enabled)
        log = (work.parent / report.log_path).read_text(encoding="utf-8", errors="replace")
        stdout = (work / "build/latexmk.stdout.log").read_text(encoding="utf-8", errors="replace")
        check.compile_log = (log + "\n" + stdout).replace(str(work), ".").replace(work.as_posix(), ".")
    # Persist useful diagnostics beside the metadata rather than a dangling temporary log path.
    report = report.model_copy(update={"log_path": source.log_path.relative_to(source.root).as_posix()})
    check.report = report
    check.compile_report = source.report_path.relative_to(source.root).as_posix()
    check.minimal_compile_ok = report.ok
    return check


def write_check(source: TemplateSource, check: TemplateCheck) -> None:
    """Write only designated adjacent metadata; never the original or project.yaml."""
    # Revalidate the destination tree, including existing metadata symlinks.
    safe_tree(source.root)
    for path in (source.check_path, source.report_path, source.log_path):
        path = safe_path(path, root=source.root)
        if source.root not in path.parents or source.original == path or source.original in path.parents:
            raise TemplateError("Check metadata must be inside the project and outside the original")
    if check.report:
        if check.compile_log is None:
            raise TemplateError("Compile diagnostics are missing")
        atomic_write_text(source.log_path, check.compile_log)
        atomic_write_text(source.report_path, json.dumps(check.report.model_dump(mode="json"),
                                                       ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    atomic_write_bytes(source.check_path, check.to_bytes())


def _environment_stack(text: str, end: int) -> list[str]:
    stack = []
    for cmd in commands(text):
        if cmd.start >= end:
            break
        if cmd.name == "begin":
            stack.append(cmd.value.strip())
        elif cmd.name == "end":
            if not stack or stack.pop() != cmd.value.strip():
                raise TemplateError("Unbalanced document environments")
    return stack


def fill_template(text: str, body: str, *, preamble: str = "", title: str | None = None,
                  author: str | None = None) -> str:
    """Insert already supplied LaTeX, not scientific prose generation.

    Without a marker preserve through maketitle/titlepage and preserve the bibliography
    tail. Refuse boundaries nested in environments rather than corrupting the template.
    Optional preamble text is a structural hook; no claims/macros are generated here.
    """
    start, end = document_region(text)
    if any(c.name == "documentclass" or (c.name in {"begin", "end"} and c.value == "document")
           for c in commands(body + "\n" + preamble)):
        raise TemplateError("Inserted content must not introduce document boundaries")
    markers = list(re.finditer(r"(?m)^[ \t]*%% AIR:BODY[ \t]*\r?$", text))
    if len(markers) > 1:
        raise TemplateError("Multiple AIR:BODY markers")
    if markers:
        marker = markers[0]
        if not start <= marker.start() < marker.end() <= end:
            raise TemplateError("AIR:BODY must be inside the document")
        # Use lexical spans so commented/inline examples of environments stay inert.
        if any(is_literal and start <= marker.start() < end
               for start, end, is_literal in _inactive_spans(text)):
            raise TemplateError("AIR:BODY is inside literal content")
        text = text[:marker.start()] + body.rstrip() + "\n" + text[marker.end():]
    else:
        clean = active_text(text)
        body_start = start
        for m in re.finditer(r"\\maketitle\b(?:\s*\[[^\]]*\])?", clean[start:end]):
            body_start = start + m.end()
        for cmd in commands(text):
            if cmd.name == "end" and cmd.value == "titlepage" and start < cmd.start < end:
                body_start = max(body_start, cmd.end)
        bibliography = [c.start for c in commands(text) if body_start <= c.start < end and
                        (c.name in {"bibliographystyle", "bibliography", "printbibliography"}
                         or (c.name == "begin" and c.value == "thebibliography"))]
        bibliography += [m.start() for m in re.finditer(r"\\printbibliography\b", clean)
                         if body_start <= m.start() < end]
        body_end = min(bibliography, default=end)
        if _environment_stack(text, body_start) != ["document"] or \
                _environment_stack(text, body_end) != ["document"]:
            raise TemplateError("No safe body boundary; add an explicit AIR:BODY marker")
        text = text[:body_start] + "\n" + body.rstrip() + "\n" + text[body_end:]
    # Edits to preamble arguments use scanner offsets, never global string replacement.
    preamble_end = next(c.start for c in commands(text) if c.name == "begin" and c.value == "document")
    updates = []
    for name, value in (("title", title), ("author", author)):
        if value is None:
            continue
        escaped = "".join({"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
                           "^": r"\textasciicircum{}"}.get(c, "\\" + c if c in "_%&#$" else c) for c in value)
        declarations = [c for c in commands(text[:preamble_end]) if c.name == name]
        if len(declarations) > 1:
            raise TemplateError(f"Multiple {name} declarations; template requires explicit adaptation")
        if declarations:
            c = declarations[0]
            updates.append((c.value_start, c.value_end, escaped))
        else:
            preamble += f"\n\\{name}{{{escaped}}}\n"
    for first, last, value in sorted(updates, reverse=True):
        text = text[:first] + value + text[last:]
    if preamble.strip():
        at = next(c.start for c in commands(text) if c.name == "begin" and c.value == "document")
        text = text[:at] + preamble.rstrip() + "\n" + text[at:]
    document_region(text)
    return text


def prepare_template(original: Path, destination: Path, body: str, *, entry: str | None = None,
                     preamble: str = "", title: str | None = None, author: str | None = None) -> Path:
    """Copy all assets into a new directory and normalize the entry to main.tex.

    Destination must not exist (protects prior papers and source trees). Later Stage
    orchestration owns replacing a previous paper and preserving other paper artifacts.
    """
    root = safe_tree(original)
    # Standalone preparation creates a new child of the caller's output directory.
    # Project callers validate that directory against their project boundary first.
    dest = safe_path(destination, root=Path(destination).parent)
    if dest.exists() or dest == root or root in dest.parents or dest in root.parents:
        raise TemplateError("Preparation requires a new destination outside the template tree")
    check = inspect_template(root, entry=entry)
    if not check.entry:
        raise TemplateError("Template entry selection required" if check.selection_required else "No template entry")
    structural = [d for d in check.diagnostics if not d.startswith("Dependency lookup unresolved:")]
    if check.missing or structural:
        raise TemplateError("Template inspection failed: " + "; ".join(check.diagnostics +
                            [m["name"] for m in check.missing]))
    if check.entry != "main.tex" and (root / "main.tex").exists():
        raise TemplateError("Renaming entry would overwrite a supporting main.tex")
    filled = fill_template((root / check.entry).read_text(encoding="utf-8"), body,
                           preamble=preamble, title=title, author=author)
    if (root / "build").exists():
        raise TemplateError("Template build/ conflicts with reserved compilation output directory")
    shutil.copytree(root, dest)
    if check.entry != "main.tex":
        (dest / check.entry).unlink()
    atomic_write_text(dest / "main.tex", filled)
    return dest / "main.tex"
