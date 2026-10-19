"""Always validate structure; require real TeX success when the tools are present."""

import json
import shutil

from airesearcher.core.fsutil import sha256_file
from airesearcher.stages.writing.compile import compile_paper
from airesearcher.stages.writing.template import check_template, prepare_template, resolve_template


def run(skill, input_dir, out_dir):
    source = skill.path("templates/default")
    hashes = {p.relative_to(source).as_posix(): sha256_file(p) for p in source.rglob("*") if p.is_file()}
    body = (input_dir / "body.tex").read_text(encoding="utf-8")
    paper = out_dir / "paper"
    prepare_template(source, paper, body)
    compiled = bool(shutil.which("latexmk") and shutil.which("kpsewhich"))
    if compiled:
        selection = resolve_template(out_dir, skill=skill)
        assert check_template(selection).minimal_compile_ok
        assert compile_paper(paper).ok
    summary = {"entry": "main.tex", "body_inserted": True, "original_unchanged": True}
    (out_dir / "summary.json").write_text(json.dumps(summary, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return {"paper": str(paper), "original": str(source), "body": body,
            "original_hashes": hashes, "compiled": compiled}
