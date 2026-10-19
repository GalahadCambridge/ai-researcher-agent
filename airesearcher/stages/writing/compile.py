"""Restricted latexmk invocation and factual reports, independent of WritingStage."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

from airesearcher.core.fsutil import atomic_write_text
from airesearcher.core.models.claim import CompileReport

from ._latex import TemplateError, relative_name, safe_tree

COMPILE_TIMEOUT = 180


def _diagnostic_path(value: str, work: Path | None) -> str:
    value = value.strip(" `\"'").replace("\\", "/")
    if work is not None:
        value = value.replace(work.as_posix() + "/", "")
    # External paths in diagnostics are not useful provenance.
    if value.startswith("/") or re.match(r"^[A-Za-z]:/", value):
        value = value.rsplit("/", 1)[-1]
    return value.removeprefix("./")


def parse_log(log: str, *, entry: str = "main.tex", work: Path | None = None) -> dict:
    """Parse the final TeX log (not accumulated first-pass latexmk warnings)."""
    if work is not None:
        log = log.replace(str(work), ".").replace(work.as_posix(), ".")
    errors = []
    lines = log.splitlines()
    for i, line in enumerate(lines):
        located = re.match(r"(.+?\.(?:tex|sty|cls)):(\d+):\s*(.+)", line)
        if located:
            file, number, message = located.groups()
            errors.append({"file": _diagnostic_path(file, work), "line": int(number), "message": message})
        elif line.startswith("!"):
            context = "\n".join(lines[i + 1:i + 8])
            number = re.search(r"(?m)^l\.(\d+)\s", context)
            errors.append({"file": entry, "line": int(number[1]) if number else None,
                           "message": "\n".join([line, *lines[i + 1:i + 3]]).strip()})
        elif re.search(r"Fatal error occurred|Emergency stop|^Latexmk:.*(?:failed|error)", line, re.I):
            errors.append({"file": entry, "line": None, "message": line.strip()})
    # LaTeX wraps long warnings. Restrict joining to warning paragraphs.
    warnings = re.sub(r"\n(?:\([^\n]*\)\s*)?", " ", log)
    citations = re.findall(r"Citation\s+[`']([^'`]+)['`]\s+.*?undefined", warnings)
    refs = re.findall(r"Reference\s+[`']([^'`]+)['`]\s+.*?undefined", warnings)
    missing = []
    for pattern in (
        r"(?:File|file)\s+[`'\"]([^`'\"\n]+)[`'\"]\s+(?:not found|does not exist)",
        r"I can't find file\s+[`']([^`'\n]+)['`]",
        r"I couldn't open (?:database|style) file\s+([^\s\n]+)",
        r"No file\s+([^\s\n]+\.(?:tex|sty|cls|bst|bib|pdf|png|jpg|eps))\.",
    ):
        missing.extend(re.findall(pattern, log))
    pages = re.findall(r"Output written on .*?\((\d+) pages?[,)]", log)
    return {
        "errors": errors,
        "undefined_citations": sorted(set(citations)),
        "undefined_refs": sorted(set(refs)),
        "missing_files": sorted({_diagnostic_path(p, work) for p in missing}),
        "overfull_boxes": sum(float(x) > 10 for x in re.findall(r"Overfull \\\w+box \(([\d.]+)pt too", log)),
        "pages": int(pages[-1]) if pages else None,
    }


def _terminate(process: subprocess.Popen) -> None:
    """Kill the latexmk process tree, including a looping TeX subprocess."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif taskkill := shutil.which("taskkill"):
        subprocess.run([taskkill, "/PID", str(process.pid), "/T", "/F"],
                       capture_output=True, timeout=10, check=False)
    process.kill()
    process.wait()


def compile_paper(paper: Path, *, entry: str = "main.tex", enabled: bool = True,
                  timeout: float = COMPILE_TIMEOUT) -> CompileReport:
    """Compile a working tree into its reserved build/ directory and write a report.

    Invalid paths raise TemplateError before invocation. Disabled/unavailable/failed
    compilation returns ok=False; never creates a placeholder PDF. No archive writes.
    """
    work = safe_tree(paper)
    entry = relative_name(entry)
    if "/" in entry or not entry.endswith(".tex") or not (work / entry).is_file():
        raise TemplateError("Compile entry must be an existing top-level .tex file")
    if timeout <= 0:
        raise ValueError("Compile timeout must be positive")
    build = work / "build"
    if build.exists() and not build.is_dir():
        raise TemplateError("Reserved build/ path is not a directory")
    build.mkdir(exist_ok=True)
    stem = Path(entry).stem
    pdf, tex_log = build / f"{stem}.pdf", build / f"{stem}.log"
    stdout_log = build / "latexmk.stdout.log"
    # Force a new run and prevent an old PDF/log from masquerading as this result.
    for path in (pdf, tex_log, stdout_log):
        if path.exists() and not path.is_file():
            raise TemplateError(f"Reserved output is not a file: build/{path.name}")
        path.unlink(missing_ok=True)
    started = time.monotonic()
    failure = None
    engine = "latexmk -pdf"
    result_code = None
    latexmk = shutil.which("latexmk") if enabled else None
    if not enabled:
        engine, failure = "disabled", "Compilation disabled (dev.compile=false)"
    elif latexmk is None:
        failure = "latexmk unavailable; install a supported TeX environment"
    else:
        env = os.environ.copy()
        for name in ("TEXINPUTS", "BIBINPUTS", "BSTINPUTS", "TEXMFOUTPUT", "TEXMFHOME"):
            env.pop(name, None)
        env.update(openin_any="p", openout_any="p", shell_escape="f",
                   SOURCE_DATE_EPOCH="946684800", FORCE_SOURCE_DATE="1", TZ="UTC")
        args = [latexmk, "-norc", "-g", "-pdf", "-interaction=nonstopmode", "-halt-on-error",
                "-no-shell-escape", "-file-line-error", "-outdir=build", entry]
        kwargs = {"start_new_session": True} if os.name == "posix" else {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
        }
        try:
            with stdout_log.open("w", encoding="utf-8") as output:
                process = subprocess.Popen(args, cwd=work, env=env, stdin=subprocess.DEVNULL,
                                           stdout=output, stderr=subprocess.STDOUT, shell=False, **kwargs)
                try:
                    result_code = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    _terminate(process)
                    failure = f"Compilation timed out after {timeout:g} seconds; process tree terminated"
        except OSError as exc:
            failure = f"latexmk could not start: {exc.strerror}"
    log = tex_log.read_text(encoding="utf-8", errors="replace") if tex_log.exists() else ""
    output = stdout_log.read_text(encoding="utf-8", errors="replace") if stdout_log.exists() else ""
    facts = parse_log(log or output, entry=entry, work=work)
    if result_code not in (None, 0):
        # BibTeX/latexmk errors may not appear in the final TeX log.
        extra = parse_log(output, entry=entry, work=work)
        facts["missing_files"] = sorted(set(facts["missing_files"] + extra["missing_files"]))
        failure = failure or f"latexmk exited with status {result_code}; see build/latexmk.stdout.log"
    data = pdf.read_bytes() if pdf.is_file() else b""
    valid_pdf = data.startswith(b"%PDF-") and data.rstrip().endswith(b"%%EOF")
    ok = result_code == 0 and not failure and not facts["errors"] and valid_pdf
    if not ok:
        pdf.unlink(missing_ok=True)
        failure = failure or "Compilation did not produce a valid PDF"
        facts["errors"].append({"file": entry, "line": None, "message": failure})
    if not tex_log.exists():
        atomic_write_text(stdout_log, output + ("\n" + failure if failure else ""))
    report = CompileReport(ok=ok, engine=engine, entry=entry, **facts,
                           log_path=f"{work.name}/build/{tex_log.name if tex_log.exists() else stdout_log.name}",
                           seconds=round(time.monotonic() - started, 3))
    atomic_write_text(build / "compile_report.json",
                      json.dumps(report.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return report
