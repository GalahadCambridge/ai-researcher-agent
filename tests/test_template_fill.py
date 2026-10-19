"""Template source selection, filling, dependencies, immutability and real uploads."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import zipfile
from types import SimpleNamespace

import pytest

from airesearcher.core import templates
from airesearcher.core.fsutil import sha256_file
from airesearcher.skills.loader import SkillLoader
from airesearcher.stages.writing import template as tpl
from airesearcher.stages.writing._latex import TemplateError, commands
from airesearcher.stages.writing.compile import compile_paper

REAL_TEX = pytest.mark.skipif(not (shutil.which("latexmk") and shutil.which("kpsewhich")), reason="TeX not installed")
BASE = "\\documentclass{article}\n\\title{Sample}\n\\author{Author}\n\\begin{document}\n\\maketitle\n"
END = "\n\\end{document}\n"
BODY = "\\section{Check}\nDeterministic inserted body.\n"


def original_at(tmp_path, content=None):
    root = tmp_path / "original"
    root.mkdir()
    (root / "entry.tex").write_text(content or BASE + "%% AIR:BODY\n" + END, encoding="utf-8")
    return root


def hashes(root):
    return {p.relative_to(root).as_posix(): sha256_file(p) for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def installed_dependencies(monkeypatch):
    monkeypatch.setattr(tpl.shutil, "which", lambda name: "/tools/kpsewhich")
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="/texmf/" + args[-1])

    monkeypatch.setattr(tpl.subprocess, "run", run)
    return calls


def test_default_resolution_and_deterministic_inspection(tmp_path, installed_dependencies):
    skill = SkillLoader().load("latex-writing", allow_unverified=True)
    source = tpl.resolve_template(tmp_path, skill=skill)
    before = hashes(source.original)
    check = tpl.inspect_template(source.original)
    assert check.entry_candidates == ["main.tex"] and check.entry == "main.tex"
    assert check.entry_chosen_by == "auto" and not check.selection_required
    assert not check.missing and all(d.resolution == "system" for d in check.dependencies)
    assert check.to_bytes() == tpl.inspect_template(source.original).to_bytes()
    assert str(tmp_path).encode() not in check.to_bytes()
    assert source.check_path == tmp_path / "paper/.template_check_default.json"
    tpl.prepare_template(source.original, tmp_path / "paper", BODY)
    assert hashes(source.original) == before


def test_single_zero_multiple_and_explicit_entry(tmp_path, installed_dependencies):
    root = original_at(tmp_path)
    assert tpl.inspect_template(root).entry == "entry.tex"
    (root / "comment.tex").write_text("% \\documentclass{ignored}\n\\section{Part}\n")
    assert tpl.inspect_template(root).entry_candidates == ["entry.tex"]
    (root / "other.tex").write_text(BASE + END)
    check = tpl.inspect_template(root)
    assert check.selection_required and check.entry is None
    assert check.entry_candidates == ["entry.tex", "other.tex"]
    assert tpl.inspect_template(root, entry="other.tex").entry_chosen_by == "user"
    with pytest.raises(TemplateError, match="selection"):
        tpl.prepare_template(root, tmp_path / "paper", BODY)
    with pytest.raises(TemplateError, match="not a detected"):
        tpl.inspect_template(root, entry="comment.tex")
    (root / "entry.tex").unlink()
    (root / "other.tex").unlink()
    check = tpl.inspect_template(root)
    assert check.entry is None and "No top-level" in check.diagnostics[0]


def test_local_and_system_dependencies(tmp_path, installed_dependencies):
    root = original_at(tmp_path, BASE.replace("{article}", "{local}") +
                       "\\usepackage{localstyle,graphicx}\n\\bibliographystyle{custom}\n"
                       "\\input{parts/body}\n\\bibliography{refs}\n" + END)
    (root / "local.cls").write_text("\\LoadClass{article}\n")
    (root / "localstyle.sty").write_text("% local style\n")
    (root / "custom.bst").write_text("% controlled local style\n")
    (root / "refs.bib").write_text("% controlled bibliography\n")
    (root / "parts").mkdir()
    (root / "parts/body.tex").write_text("Text.\n")
    check = tpl.inspect_template(root)
    assert not check.missing
    resolved = {d.name: d.resolution for d in check.dependencies}
    local_names = ("local.cls", "localstyle.sty", "custom.bst", "refs.bib", "parts/body.tex")
    assert all(resolved[n] == "local" for n in local_names)
    assert resolved["article.cls"] == resolved["graphicx.sty"] == "system"
    assert all(kwargs["shell"] is False and kwargs["timeout"] == 10 for _, kwargs in installed_dependencies)


def test_missing_and_unavailable_dependency_lookup(tmp_path, monkeypatch):
    root = original_at(tmp_path, BASE + "\\usepackage{absent}\n\\bibliographystyle{absent}\n" + END)
    monkeypatch.setattr(tpl.shutil, "which", lambda name: "/tools/kpsewhich")
    monkeypatch.setattr(tpl.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))
    check = tpl.inspect_template(root)
    assert check.missing == [{"kind": "bst", "name": "absent.bst"}, {"kind": "cls", "name": "article.cls"},
                             {"kind": "sty", "name": "absent.sty"}]
    with pytest.raises(TemplateError, match="inspection failed"):
        tpl.prepare_template(root, tmp_path / "paper", BODY)
    monkeypatch.setattr(tpl.shutil, "which", lambda name: None)
    check = tpl.inspect_template(root)
    assert check.missing == [] and {d.resolution for d in check.dependencies} == {"unresolved"}
    assert "unresolved" in check.diagnostics[0]


def test_dependency_timeout(tmp_path, monkeypatch):
    root = original_at(tmp_path)
    monkeypatch.setattr(tpl.shutil, "which", lambda name: "kpsewhich")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("kpsewhich", 10)

    monkeypatch.setattr(tpl.subprocess, "run", timeout)
    assert tpl.inspect_template(root).dependencies[0].resolution == "unresolved"


def test_marker_preserves_template_and_structural_hooks():
    text = BASE + "Before marker.\n%% AIR:BODY\nAfter marker.\n\\bibliographystyle{plain}\n" + END
    filled = tpl.fill_template(text, BODY, preamble="\\newcommand{\\optionalhook}{Future}",
                               title="A & B_1", author="Anonymous")
    assert "Before marker." in filled and "After marker." in filled
    assert "%% AIR:BODY" not in filled and BODY.strip() in filled
    assert r"\title{A \& B\_1}" in filled and r"\author{Anonymous}" in filled
    assert filled.index(r"\newcommand") < filled.index(r"\begin{document}")
    assert tpl.fill_template(text, BODY) == tpl.fill_template(text, BODY)


@pytest.mark.parametrize("bibliography", [r"\bibliographystyle{plain}\bibliography{refs}",
                                          r"\printbibliography[title=References]",
                                          r"\begin{thebibliography}{9}\bibitem{x}Reference.\end{thebibliography}", ""])
def test_no_marker_preserves_preamble_maketitle_bibliography(bibliography):
    text = BASE + "% old sample comment\n\\section{Old}\nOLD SAMPLE\n" + bibliography + END
    filled = tpl.fill_template(text, BODY)
    assert BASE.rstrip() in filled and "OLD SAMPLE" not in filled
    assert bibliography in filled and BODY.strip() in filled and filled.endswith(END)


def test_titlepage_and_no_maketitle():
    text = BASE.replace("\\maketitle\n", "\\begin{titlepage}Custom cover.\\end{titlepage}\n") + "OLD" + END
    assert "Custom cover." in tpl.fill_template(text, BODY)
    assert "OLD" not in tpl.fill_template(text, BODY)
    text = "\\documentclass{article}\n\\begin{document}OLD" + END
    assert "OLD" not in tpl.fill_template(text, BODY)


@pytest.mark.parametrize("text", [
    BASE + "%% AIR:BODY\n%% AIR:BODY\n" + END,
    "%% AIR:BODY\n" + BASE + END,
    BASE + "\\begin{verbatim}\n%% AIR:BODY\n\\end{verbatim}\n" + END,
    BASE + "\\begin{center}\\bibliography{refs}\\end{center}" + END,
    BASE + "Missing end",
])
def test_unsafe_structures_are_rejected(text):
    with pytest.raises(TemplateError):
        tpl.fill_template(text, BODY)


def test_comments_are_not_commands():
    text = "% \\documentclass{wrong}\n" + BASE + "Text \\% literal. % \\bibliography{fake}\nOLD" + END
    assert [c.value for c in commands(text) if c.name == "documentclass"] == ["article"]
    assert "fake" not in tpl.fill_template(text, BODY)


@pytest.mark.parametrize("resource", ["../escape", "/outside", "C:/outside", "..\\outside", "\\variable", "-shell"])
def test_unsafe_resource_paths(tmp_path, resource):
    root = original_at(tmp_path, BASE + f"\\input{{{resource}}}" + END)
    with pytest.raises(TemplateError):
        tpl.inspect_template(root)


def test_prepare_preserves_local_assets_and_rejects_overwrite(tmp_path, installed_dependencies):
    root = original_at(tmp_path)
    (root / "style.sty").write_text("% local asset\n")
    (root / "image.png").write_bytes(b"controlled asset")
    before = hashes(root)
    dest = tmp_path / "paper"
    entry = tpl.prepare_template(root, dest, BODY)
    assert entry == dest / "main.tex" and not (dest / "entry.tex").exists()
    assert (dest / "style.sty").read_bytes() == (root / "style.sty").read_bytes()
    assert (dest / "image.png").read_bytes() == (root / "image.png").read_bytes()
    assert hashes(root) == before
    for target in (root, root / "inside", dest):
        with pytest.raises(TemplateError):
            tpl.prepare_template(root, target, BODY)


def test_symlinks_rejected_for_inspection_preparation_and_metadata(tmp_path, installed_dependencies):
    root = original_at(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    try:
        (root / "linked.sty").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(TemplateError):
        tpl.inspect_template(root)
    with pytest.raises(TemplateError):
        tpl.prepare_template(root, tmp_path / "paper", BODY)
    (root / "linked.sty").unlink()
    source = tpl.TemplateSource(root, tmp_path / "check.json", tmp_path / "report.json", tmp_path)
    (tmp_path / "check.json").symlink_to(outside)
    with pytest.raises(TemplateError):
        tpl.write_check(source, tpl.inspect_template(root))
    assert outside.read_text() == "untouched"


@pytest.mark.parametrize("selection", [{"id": "../tpl-01", "version": 1}, {"id": "tpl-01", "version": 0},
                                      {"id": "tpl-01", "version": True}, {"id": "tpl-01", "version": 999}])
def test_invalid_upload_never_falls_back(tmp_path, selection):
    with pytest.raises(TemplateError):
        tpl.resolve_template(tmp_path, selection)


@REAL_TEX
@pytest.mark.parametrize("marker", [True, False])
def test_real_uploaded_template_check_prepare_compile_and_metadata(project, marker):
    body = "%% AIR:BODY\n" if marker else "OLD SAMPLE\n"
    text = BASE.replace("{article}", "{local}") + body + END
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("user/entry.tex", text)
        archive.writestr("user/local.cls", "\\NeedsTeXFormat{LaTeX2e}\n\\ProvidesClass{local}\n\\LoadClass{article}\n")
        archive.writestr("user/local.sty", "% Supporting asset\n")
    info = templates.import_zip(project, data.getvalue(), "user-template.zip")
    source = tpl.resolve_template(project.root, project.config.template)
    before, config_before = hashes(source.original), (project.root / "project.yaml").read_bytes()
    check = tpl.check_template(source)
    assert check.minimal_compile_ok and check.report.ok, check
    assert check.report.log_path == source.log_path.relative_to(project.root).as_posix()
    assert check.report.pages == 1
    first_bytes = check.to_bytes()
    tpl.write_check(source, check)
    assert templates.template_info(project, info.template_id, info.version).check == json.loads(first_bytes)
    assert (project.root / check.compile_report).exists()
    assert source.log_path.is_file() and "air-template-" not in source.log_path.read_text()
    assert first_bytes == tpl.check_template(source).to_bytes()
    assert b"air-template-" not in first_bytes
    tpl.prepare_template(source.original, project.root / "prepared-paper", BODY)
    report = compile_paper(project.root / "prepared-paper")
    assert report.ok and report.pages == 1, report
    assert hashes(source.original) == before
    assert (project.root / "project.yaml").read_bytes() == config_before


@REAL_TEX
def test_real_minimal_compile_failure_and_disabled_are_honest(tmp_path):
    root = original_at(tmp_path, BASE + "\\notARealCommand\n" + END)
    source = tpl.TemplateSource(root, tmp_path / "check.json", tmp_path / "report.json", tmp_path)
    before = hashes(root)
    check = tpl.check_template(source)
    assert not check.minimal_compile_ok and check.report.errors
    assert hashes(root) == before
    disabled = tpl.check_template(source, enabled=False)
    assert not disabled.minimal_compile_ok and disabled.report.engine == "disabled"


def test_skill_can_verify_structure_without_tex(monkeypatch):
    monkeypatch.setattr(tpl.shutil, "which", lambda name: None)
    report = SkillLoader().verify("latex-writing", update_registry=False)
    assert report.ok, report.steps


def test_ambiguous_or_missing_template_does_not_compile(tmp_path, installed_dependencies, monkeypatch):
    root = original_at(tmp_path)
    (root / "other.tex").write_text(BASE + END)
    source = tpl.TemplateSource(root, tmp_path / "check.json", tmp_path / "report.json", tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("Ambiguous templates must not compile")

    monkeypatch.setattr(tpl, "compile_paper", forbidden)
    check = tpl.check_template(source)
    assert check.selection_required and not check.minimal_compile_ok and check.compile_report is None
    (root / "entry.tex").unlink()
    (root / "other.tex").unlink()
    check = tpl.check_template(source)
    assert not check.minimal_compile_ok and check.entry is None


def test_entry_rename_collision_and_reserved_build_are_rejected(tmp_path, installed_dependencies):
    root = original_at(tmp_path)
    (root / "main.tex").write_text("Supporting text only.\n")
    with pytest.raises(TemplateError, match="overwrite"):
        tpl.prepare_template(root, tmp_path / "paper", BODY)
    (root / "main.tex").unlink()
    (root / "build").mkdir()
    with pytest.raises(TemplateError, match="reserved"):
        tpl.prepare_template(root, tmp_path / "paper", BODY)


@pytest.mark.parametrize("body", [r"\end{document}", r"\documentclass{other}"])
def test_inserted_document_boundaries_rejected(body):
    with pytest.raises(TemplateError, match="boundaries"):
        tpl.fill_template(BASE + "%% AIR:BODY\n" + END, body)


def test_selected_entry_only_checks_its_reachable_dependencies(tmp_path, monkeypatch):
    root = original_at(tmp_path)
    (root / "unused.tex").write_text(BASE + "\\usepackage{unusedMissing}\n" + END)
    (root / "unused.sty").write_text("\\RequirePackage{unrelatedMissing}\n")
    monkeypatch.setattr(tpl.shutil, "which", lambda name: "kpsewhich")

    def lookup(args, **kwargs):
        present = args[-1] == "article.cls"
        return SimpleNamespace(returncode=0 if present else 1, stdout="/texmf/article.cls" if present else "")

    monkeypatch.setattr(tpl.subprocess, "run", lookup)
    assert tpl.inspect_template(root).selection_required
    chosen = tpl.inspect_template(root, entry="entry.tex")
    assert not chosen.missing and [d.name for d in chosen.dependencies] == ["article.cls"]
    assert tpl.inspect_template(root, entry="unused.tex").missing == [{"kind": "sty", "name": "unusedMissing.sty"}]


def test_recursive_dependency_cycle_is_finite(tmp_path, installed_dependencies):
    root = original_at(tmp_path, BASE + "\\input{part}\n" + END)
    (root / "part.tex").write_text("\\input{part}\n\\usepackage{graphicx}\n")
    check = tpl.inspect_template(root)
    assert [(d.name, d.resolution) for d in check.dependencies] == [
        ("article.cls", "system"), ("graphicx.sty", "system"), ("part.tex", "local"), ("part.tex", "local")]
