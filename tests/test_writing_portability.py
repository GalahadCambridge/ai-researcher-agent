"""Exercise locale and filesystem boundaries without depending on the host OS."""

import json
import tempfile
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

from airesearcher.engine.stage import Error
from airesearcher.skills.loader import SkillLoader
from airesearcher.stages.writing import bundle, template
from airesearcher.stages.writing._latex import TemplateError, safe_tree
from airesearcher.stages.writing.claims import PROMPT_ID as CLAIM_PROMPT
from airesearcher.stages.writing.compile import compile_paper
from airesearcher.stages.writing.stage import _path
from scripts.writing.demo import prepare_sample, run

from .test_writing_stage import receipt, to_phase


def symlink(link, target):
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")


def legacy_locale(monkeypatch):
    read, write = Path.read_text, Path.write_text

    def cp1252_read(self, encoding=None, **kwargs):
        return read(self, encoding=encoding or "cp1252", **kwargs)

    def cp1252_write(self, data, encoding=None, **kwargs):
        return write(self, data, encoding=encoding or "cp1252", **kwargs)

    monkeypatch.setattr(Path, "read_text", cp1252_read)
    monkeypatch.setattr(Path, "write_text", cp1252_write)


@pytest.mark.parametrize("external_alias", [False, True])
def test_template_tree_under_external_ancestor(tmp_path, monkeypatch, external_alias):
    physical = tmp_path / "physical"
    physical.mkdir()
    parent = physical
    if external_alias:
        parent = tmp_path / "alias"
        symlink(parent, physical)
    project = parent / "project"
    original = project / "templates/default"
    original.mkdir(parents=True)
    text = "\\documentclass{article}\n\\begin{document}\n%% AIR:BODY\n\\end{document}\n"
    (original / "main.tex").write_text(text, encoding="utf-8")
    # No TeX lookup is needed to prove path/encoding behavior.
    monkeypatch.setattr(template.shutil, "which", lambda name: None)
    legacy_locale(monkeypatch)
    assert safe_tree(project) == (physical / "project").resolve()
    assert template.inspect_template(original).entry == "main.tex"
    body = "UTF-8: 论文、指标、é、μ."
    main = template.prepare_template(original, project / "paper", body)
    assert body in main.read_text(encoding="utf-8")
    assert (original / "main.tex").read_bytes() == text.encode("utf-8")
    report = compile_paper(project / "paper", enabled=False)
    assert not report.ok and report.engine == "disabled"


@pytest.mark.parametrize("kind", ["directory", "file", "inside", "dangling", "root"])
def test_controlled_tree_symlinks_still_rejected(tmp_path, kind):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "main.tex").write_text("outside", encoding="utf-8")
    target = outside
    link = root / "assets"
    if kind == "file":
        target, link = outside / "main.tex", root / "main.tex"
    elif kind == "inside":
        target = root / "real"
        target.mkdir()
    elif kind == "dangling":
        target = outside / "missing"
    elif kind == "root":
        root.rmdir()
        link = root
    symlink(link, target)
    with pytest.raises(TemplateError, match="Symlink|Unsafe"):
        safe_tree(root)
    with pytest.raises(TemplateError, match="Symlink|Unsafe"):
        _path(SimpleNamespace(root=root), link.relative_to(root).as_posix())
    assert (outside / "main.tex").read_text(encoding="utf-8") == "outside"


def test_project_path_rejects_parent_escape(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    with pytest.raises(TemplateError):
        _path(SimpleNamespace(root=root), "../outside")


def test_latex_skill_sample_with_external_temp_ancestor(tmp_path, monkeypatch):
    physical = tmp_path / "physical"
    physical.mkdir()
    alias = tmp_path / "alias"
    symlink(alias, physical)
    monkeypatch.setattr(tempfile, "tempdir", str(alias))
    result = SkillLoader().verify("latex-writing", update_registry=False)
    assert result.ok, result.steps


@pytest.mark.parametrize("phase", ["review", "revise"])
def test_semantic_artifacts_use_utf8_under_legacy_locale(tmp_path, monkeypatch, phase):
    project, engine = prepare_sample(tmp_path / "sample", compile_enabled=False, evidence_check=True)
    engine.llm.backend.semantic_revision = True
    original_call = engine.llm.backend.call

    def non_ascii_claims(request):
        response = original_call(request)
        if request.prompt_id == CLAIM_PROMPT:
            plan = json.loads(response.text)
            for claim in plan["claims"]:
                claim["text_template"] = "数据：" + claim["text_template"]
            response.text = json.dumps(plan, ensure_ascii=False)
        return response

    monkeypatch.setattr(engine.llm.backend, "call", non_ascii_claims)
    to_phase(engine, "verify" if phase == "review" else phase)
    old_claims = (project.root / "paper/claims.jsonl").read_bytes()
    assert "数据" in old_claims.decode("utf-8")
    if phase == "review":
        # Recovery scans previous receipts to reserve the next review number.
        (project.root / "paper/.writing/semantic-previous.json").write_text(
            json.dumps({"number": 9, "note": "论文数据"}, ensure_ascii=False), encoding="utf-8")
    legacy_locale(monkeypatch)
    probe = tmp_path / "utf8.json"
    probe.write_text('{"note":"论文数据"}', encoding="utf-8")
    with pytest.raises(UnicodeDecodeError):
        probe.read_text()  # Deliberate negative control for the simulated locale.
    assert json.loads(probe.read_text(encoding="utf-8"))["note"] == "论文数据"
    tick = engine.tick()
    assert not isinstance(tick.result, Error), tick.result
    approval = run(engine)
    assert approval.kind == "manuscript"
    assert (project.root / "paper/claims.jsonl").read_text(encoding="utf-8")
    assert receipt(project, "pending-revision")["old"]["paper/claims.jsonl"].encode("utf-8") == old_claims
    number = 2 if phase == "revise" else 10
    assert (project.root / f"paper/review/review_v{number}.json").exists()


@pytest.mark.parametrize("path", ["/private/gpu", "C:\\private\\gpu", "\\\\server\\share\\gpu"])
def test_runtime_paths_are_redacted_independently_of_host(monkeypatch, path):
    # Model Windows path semantics on Linux too; POSIX archived paths stay private.
    monkeypatch.setattr(bundle, "Path", PureWindowsPath)
    assert bundle._portable_input({"hardware": {"device_path": path}}) == {
        "hardware": {"device_path": "<external-path>"}}
