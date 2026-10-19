"""Final-log facts, compiler failure states and actual TeX runtime checks."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from airesearcher.stages.writing import compile as compiler
from airesearcher.stages.writing._latex import TemplateError

TEX = r"\documentclass{article}\begin{document}Hello.\end{document}"
REAL_TEX = pytest.mark.skipif(not shutil.which("latexmk"), reason="latexmk not installed")


def paper_at(tmp_path, text=TEX):
    paper = tmp_path / "paper"
    paper.mkdir()
    (paper / "main.tex").write_text(text, encoding="utf-8")
    return paper


def fake_process(monkeypatch, *, log="", output="", code=0, pdf=True, timeout=False):
    seen = {}

    class Process:
        pid = 123456

        def __init__(self, args, **kwargs):
            seen.update(args=args, **kwargs)
            build = kwargs["cwd"] / "build"
            if log:
                (build / "main.log").write_text(log, encoding="utf-8")
            if pdf:
                (build / "main.pdf").write_bytes(b"%PDF-1.5\ncontrolled fake process output\n%%EOF\n")
            kwargs["stdout"].write(output)
            kwargs["stdout"].flush()

        def wait(self, timeout=None):
            if timeout is not None and seen.get("timeout_raised") is None and timeout_case:
                seen["timeout_raised"] = True
                raise subprocess.TimeoutExpired("latexmk", timeout)
            return code

    timeout_case = timeout
    monkeypatch.setattr(compiler.shutil, "which", lambda name: "/tools/latexmk")
    monkeypatch.setattr(compiler.subprocess, "Popen", Process)
    return seen


def test_final_log_all_fields():
    facts = compiler.parse_log("""! LaTeX Error: File `missing.sty' not found.
l.14 \\usepackage{missing}
LaTeX Warning: Citation `unknown' on page 1 undefined on input line 10.
LaTeX Warning: Citation `unknown' on page 1 undefined on input line 10.
LaTeX Warning: Reference `sec:missing' on page 1 undefined on input line 11.
Overfull \\hbox (10.001pt too wide) in paragraph at lines 2--3
Overfull \\hbox (10.0pt too wide) in paragraph at lines 3--4
Overfull \\vbox (12.0pt too high) detected at line 5
Output written on build/main.pdf (3 pages, 12345 bytes).
""")
    assert facts["errors"][0]["line"] == 14
    assert "missing.sty" in facts["errors"][0]["message"]
    assert facts["undefined_citations"] == ["unknown"]
    assert facts["undefined_refs"] == ["sec:missing"]
    assert facts["missing_files"] == ["missing.sty"]
    assert facts["overfull_boxes"] == 2
    assert facts["pages"] == 3


@pytest.mark.parametrize(("value", "count"), [(0.5, 0), (9.99, 0), (10, 0), (10.0001, 1), (20, 1)])
def test_overfull_threshold(value, count):
    assert compiler.parse_log(f"Overfull \\hbox ({value}pt too wide)")["overfull_boxes"] == count


def test_wrapped_warnings_and_file_line_errors(tmp_path):
    facts = compiler.parse_log(f"""{tmp_path}/sections/results.tex:42: Undefined control sequence.
LaTeX Warning: Reference `some:label' on page 1 undefined on input
line 42.
Package natbib Warning: Citation `key' on page 1 undefined
(natbib)                on input line 5.
! I can't find file `./data.tex'.
I couldn't open database file refs.bib
I couldn't open style file absent.bst
! LaTeX Error: File `/unrelated/temp/picture.png' not found.
""", work=tmp_path)
    assert facts["errors"][0] == {"file": "sections/results.tex", "line": 42,
                                  "message": "Undefined control sequence."}
    assert facts["undefined_refs"] == ["some:label"]
    assert facts["undefined_citations"] == ["key"]
    assert facts["missing_files"] == ["absent.bst", "data.tex", "picture.png", "refs.bib"]
    assert compiler.parse_log("")["pages"] is None


def test_command_environment_final_log_and_persisted_report(tmp_path, monkeypatch):
    paper = paper_at(tmp_path)
    seen = fake_process(monkeypatch, log="Output written on build/main.pdf (1 page, 100 bytes).",
                        output="LaTeX Warning: Reference `resolved' on page 1 undefined.")
    monkeypatch.setenv("TEXINPUTS", "/unsafe//:")
    report = compiler.compile_paper(paper)
    assert report.ok and report.pages == 1
    assert report.undefined_refs == []  # first-pass stdout does not pollute final facts
    assert seen["args"] == ["/tools/latexmk", "-norc", "-g", "-pdf", "-interaction=nonstopmode",
                            "-halt-on-error", "-no-shell-escape", "-file-line-error", "-outdir=build", "main.tex"]
    assert seen["shell"] is False and seen["cwd"] == paper
    assert seen["env"]["openin_any"] == seen["env"]["openout_any"] == "p"
    assert seen["env"]["shell_escape"] == "f" and "TEXINPUTS" not in seen["env"]
    assert seen["env"]["SOURCE_DATE_EPOCH"] == "946684800"
    assert report.log_path == "paper/build/main.log"
    assert json.loads((paper / "build/compile_report.json").read_text()) == report.model_dump(mode="json")
    assert sorted(p.name for p in paper.iterdir()) == ["build", "main.tex"]


@pytest.mark.parametrize("state", ["disabled", "missing", "timeout", "bad_exit", "no_pdf", "bad_pdf"])
def test_failed_attempts_never_keep_a_stale_or_placeholder_pdf(tmp_path, monkeypatch, state):
    paper = paper_at(tmp_path)
    build = paper / "build"
    build.mkdir()
    (build / "main.pdf").write_bytes(b"%PDF-1.5 old success")
    (build / "main.log").write_text("Output written on build/main.pdf (9 pages, 100 bytes).")
    seen = fake_process(monkeypatch, code=1 if state == "bad_exit" else 0,
                        pdf=state not in {"no_pdf", "bad_pdf"}, timeout=state == "timeout")
    if state == "missing":
        monkeypatch.setattr(compiler.shutil, "which", lambda name: None)
    if state == "bad_pdf":
        original = compiler.subprocess.Popen

        def invalid_pdf(*args, **kwargs):
            process = original(*args, **kwargs)
            (kwargs["cwd"] / "build/main.pdf").write_bytes(b"not a PDF but more than 10 bytes")
            return process

        monkeypatch.setattr(compiler.subprocess, "Popen", invalid_pdf)
    terminated = []
    monkeypatch.setattr(compiler, "_terminate", lambda process: terminated.append(process))
    report = compiler.compile_paper(paper, enabled=state != "disabled", timeout=0.1)
    assert not report.ok and report.errors and report.pages is None
    assert not (build / "main.pdf").exists()
    if state in {"disabled", "missing"}:
        assert seen == {}  # no process invoked
    if state == "timeout":
        assert terminated and "timed out" in report.errors[-1]["message"]
    if state == "disabled":
        assert report.engine == "disabled"
    if state == "missing":
        assert "unavailable" in report.errors[-1]["message"]


def test_nonzero_exit_and_bibtex_missing_file(tmp_path, monkeypatch):
    paper = paper_at(tmp_path)
    fake_process(monkeypatch, log="ordinary final TeX log", code=12,
                 output="I couldn't open style file missing.bst\n")
    report = compiler.compile_paper(paper)
    assert not report.ok and report.missing_files == ["missing.bst"]


@pytest.mark.parametrize("entry", ["../main.tex", "/main.tex", "C:/main.tex", "-main.tex", "a/main.tex"])
def test_compile_entry_safety(tmp_path, entry):
    with pytest.raises(TemplateError):
        compiler.compile_paper(paper_at(tmp_path), entry=entry)


def test_build_symlink_rejected(tmp_path):
    paper = paper_at(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (paper / "build").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(TemplateError):
        compiler.compile_paper(paper)
    assert list(outside.iterdir()) == []


@REAL_TEX
def test_real_success_and_resolved_multi_pass_reference(tmp_path):
    paper = paper_at(tmp_path, r"""\documentclass{article}
\begin{document}See Section~\ref{sec:later}.
\section{Later}\label{sec:later}Hello.
\end{document}
""")
    report = compiler.compile_paper(paper)
    assert report.ok, report
    assert report.pages == 1 and report.errors == report.undefined_refs == []
    pdf = paper / "build/main.pdf"
    assert pdf.stat().st_size > 1000 and pdf.read_bytes().startswith(b"%PDF-")
    assert (paper / "build/main.log").is_file()


@REAL_TEX
@pytest.mark.parametrize(("body", "expected", "ok"), [
    (r"\notARealCommand", "error", False),
    (r"\input{absent-file}", "missing", False),
    (r"\cite{unknown}", "citation", True),
    (r"\pageref{unknown}", "reference", True),
])
def test_real_compile_diagnostics(tmp_path, body, expected, ok):
    paper = paper_at(tmp_path, "\\documentclass{article}\n\\begin{document}\n" + body + "\n\\end{document}\n")
    report = compiler.compile_paper(paper)
    assert report.ok is ok, report
    if expected == "error":
        assert any(e.get("line") == 3 for e in report.errors)
    elif expected == "missing":
        assert "absent-file" in " ".join(report.missing_files)
    elif expected == "citation":
        assert report.undefined_citations == ["unknown"]
    else:
        assert report.undefined_refs == ["unknown"]
    assert (paper / "build/main.pdf").exists() is ok


@REAL_TEX
def test_real_rc_file_not_executed(tmp_path):
    paper = paper_at(tmp_path)
    (paper / ".latexmkrc").write_text('die "UNTRUSTED RC EXECUTED";\n', encoding="utf-8")
    assert compiler.compile_paper(paper).ok


@REAL_TEX
def test_real_shell_escape_and_parent_output_are_blocked(tmp_path):
    paper = paper_at(tmp_path, "\\documentclass{article}\n\\begin{document}\n"
                     "\\immediate\\write18{touch EXECUTED}\nHello.\\end{document}\n")
    assert compiler.compile_paper(paper).ok
    assert not (paper / "EXECUTED").exists()
    (paper / "main.tex").write_text("\\documentclass{article}\n\\begin{document}\n"
                                    "\\immediate\\openout0=../escaped.txt\n\\end{document}\n")
    report = compiler.compile_paper(paper)
    assert not report.ok and not (tmp_path / "escaped.txt").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group runtime check")
def test_actual_process_timeout_terminates_subprocess(tmp_path, monkeypatch):
    paper = paper_at(tmp_path)
    popen = compiler.subprocess.Popen
    processes = []

    def looping_process(args, **kwargs):
        process = popen([sys.executable, "-c", "while True: pass"], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(compiler.shutil, "which", lambda name: sys.executable)
    monkeypatch.setattr(compiler.subprocess, "Popen", looping_process)
    report = compiler.compile_paper(paper, timeout=0.1)
    assert not report.ok and "timed out" in report.errors[-1]["message"]
    assert processes[0].poll() is not None


def test_reserved_build_file_is_explicit_error(tmp_path):
    paper = paper_at(tmp_path)
    (paper / "build").write_text("not a directory")
    with pytest.raises(TemplateError, match="not a directory"):
        compiler.compile_paper(paper)


def test_fatal_without_bang_prefix_is_reported():
    errors = compiler.parse_log("Fatal error occurred, no output PDF file produced!\n")["errors"]
    assert len(errors) == 1 and "Fatal error" in errors[0]["message"]
