"""Validate actual prepared files, not a claimed success flag."""

import json
from pathlib import Path

from airesearcher.core.fsutil import sha256_file
from airesearcher.core.models.claim import CompileReport
from airesearcher.stages.writing._latex import TemplateError, document_region, safe_tree


def check_output(output):
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except json.JSONDecodeError:
            return ["Expected an artifact envelope"]
    if not isinstance(output, dict) or "paper" not in output or "original" not in output:
        return ["Prepared paper and immutable original are required"]
    try:
        paper, original = safe_tree(Path(output["paper"])), safe_tree(Path(output["original"]))
        text = (paper / "main.tex").read_text(encoding="utf-8")
        document_region(text)
        body = output.get("body", "")
        if not body or body not in text or "%% AIR:BODY" in text:
            return ["Supplied body was not inserted"]
        hashes = {p.relative_to(original).as_posix(): sha256_file(p)
                  for p in sorted(original.rglob("*")) if p.is_file()}
        if hashes != output.get("original_hashes"):
            return ["Original template changed"]
        if output.get("compiled"):
            report = CompileReport.model_validate_json(
                (paper / "build/compile_report.json").read_text(encoding="utf-8"))
            pdf = paper / "build/main.pdf"
            if not report.ok or not pdf.is_file() or not pdf.read_bytes().startswith(b"%PDF-"):
                return ["Real compilation did not succeed"]
    except (TemplateError, OSError, ValueError, TypeError) as exc:
        return [f"Invalid template artifact: {exc}"]
    return []
