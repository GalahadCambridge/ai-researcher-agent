"""用户上传的 LaTeX 模板（详细设计 1 第 9.2 节 /templates，详细设计 4 第 7.3 节）。

上传由主干完成：zip 解压到 templates/<tpl_id>/v<N>/original/（原始模板永远不修改），登记版本，
并把 project.yaml 的 template 改为 {id, version}。模板检查（入口、依赖、最小编译）由论文阶段做，
结果写在同一版本目录下的 check.json，这里只负责读出来给 GUI 显示。
"""

from __future__ import annotations

import hashlib
import io
import re
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from .errors import NotFound, ValidationFailed
from .fsutil import atomic_write_json, now, read_json
from .models.template import TemplateInfo, TemplateList

if TYPE_CHECKING:
    from .project import Project

MAX_ZIP_BYTES = 20 * 1024 * 1024  # 上传的 zip 最大 20 MB
MAX_UNPACKED_BYTES = 100 * 1024 * 1024  # 解压后最大 100 MB（防 zip 炸弹）
MAX_FILES = 500
SKIP = re.compile(r"(^|/)(__MACOSX/|\.DS_Store$|Thumbs\.db$)")
_TPL_ID = re.compile(r"^tpl-(\d+)$")


def _safe_members(zf: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, str]]:
    """检查 zip 中的每个文件：不允许绝对路径、..、符号链接；去掉统一的顶层目录。"""
    out: list[tuple[zipfile.ZipInfo, str]] = []
    total = 0
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if info.is_dir() or SKIP.search(name):
            continue
        parts = PurePosixPath(name).parts
        if name.startswith("/") or (parts and ":" in parts[0]) or ".." in parts:
            raise ValidationFailed(f"模板 zip 中有不安全的路径：{info.filename}")
        if stat.S_ISLNK(info.external_attr >> 16):
            raise ValidationFailed(f"模板 zip 中不能有符号链接：{info.filename}")
        total += info.file_size
        out.append((info, "/".join(parts)))
    if not out:
        raise ValidationFailed("模板 zip 是空的")
    if len(out) > MAX_FILES:
        raise ValidationFailed(f"模板 zip 中文件太多（{len(out)} 个，最多 {MAX_FILES} 个）")
    if total > MAX_UNPACKED_BYTES:
        raise ValidationFailed(f"模板解压后超过 {MAX_UNPACKED_BYTES // 1024 // 1024} MB")
    tops = {rel.split("/", 1)[0] for _, rel in out}
    if len(tops) == 1 and all("/" in rel for _, rel in out):  # 整个模板包在一个文件夹里：去掉这一层
        out = [(info, rel.split("/", 1)[1]) for info, rel in out]
    if not any(rel.lower().endswith(".tex") for _, rel in out):
        raise ValidationFailed("模板 zip 中没有 .tex 文件")
    return out


def _extract(zf: zipfile.ZipFile, members: list[tuple[zipfile.ZipInfo, str]], dest_dir: Path) -> None:
    budget = MAX_UNPACKED_BYTES  # 按实际解压出的字节数计（zip 里写的大小可以是假的）
    for info, name in members:
        dest = dest_dir / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(dest, "wb") as out:
            while chunk := src.read(1 << 16):
                budget -= len(chunk)
                if budget < 0:
                    raise ValidationFailed(f"模板解压后超过 {MAX_UNPACKED_BYTES // 1024 // 1024} MB")
                out.write(chunk)


def _dir(root: Path) -> Path:
    return Path(root) / "templates"


def _versions(root: Path, template_id: str) -> list[int]:
    d = _dir(root) / template_id
    return sorted(int(p.name[1:]) for p in d.glob("v*") if p.name[1:].isdigit()) if d.exists() else []


def import_zip(project: Project, data: bytes, filename: str = "template.zip", template_id: str | None = None,
               actor: str = "user") -> TemplateInfo:
    """解压并登记一个模板版本，然后让项目使用它。template_id 给出时作为该模板的新版本。"""
    if len(data) > MAX_ZIP_BYTES:
        raise ValidationFailed(f"模板 zip 超过 {MAX_ZIP_BYTES // 1024 // 1024} MB")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValidationFailed("上传的文件不是 zip") from None
    members = _safe_members(zf)
    root = project.root
    if template_id:
        if not _versions(root, template_id):
            raise NotFound(f"找不到模板 {template_id}")
    else:
        ids = [int(m.group(1)) for p in _dir(root).glob("tpl-*") if (m := _TPL_ID.match(p.name))] \
            if _dir(root).exists() else []
        template_id = f"tpl-{max(ids, default=0) + 1:02d}"
    version = max(_versions(root, template_id), default=0) + 1
    rel = f"templates/{template_id}/v{version}"
    project.permissions.guard("write", rel, actor=actor)
    vdir = root / rel
    orig = vdir / "original"
    try:
        _extract(zf, members, orig)
    except Exception:
        shutil.rmtree(vdir, ignore_errors=True)  # 不留下解压了一半的版本
        raise
    atomic_write_json(vdir / "upload.json", {
        "filename": filename, "sha256": hashlib.sha256(data).hexdigest(), "uploaded_at": now().isoformat(),
        "files": len(members), "by": actor,
    })
    ref = project.archive.put(rel, "template", vdir, producer=actor, note=f"上传模板 {filename}")
    cfg = project.config.model_copy(update={"template": {"id": template_id, "version": version}})
    project.save_config(cfg, summary=f"改用上传的模板 {template_id} v{version}", actor=actor)
    project.events.append("template.uploaded", f"上传模板 {filename} → {template_id} v{version}",
                          actor=actor, refs=[ref], data={"template_id": template_id, "version": version})
    return template_info(project, template_id, version)


def template_info(project: Project, template_id: str, version: int) -> TemplateInfo:
    vdir = _dir(project.root) / template_id / f"v{version}"
    meta: dict[str, Any] = read_json(vdir / "upload.json") if (vdir / "upload.json").exists() else {}
    check = read_json(vdir / "check.json") if (vdir / "check.json").exists() else None
    files = sorted(p.relative_to(vdir / "original").as_posix() for p in (vdir / "original").rglob("*")
                   if p.is_file())
    cur = project.config.template or {}
    return TemplateInfo(
        template_id=template_id, version=version, path=f"templates/{template_id}/v{version}/original",
        filename=meta.get("filename"), uploaded_at=meta.get("uploaded_at"), files=files,
        tex_files=[f for f in files if f.endswith(".tex") and "/" not in f],
        in_use=cur.get("id") == template_id and cur.get("version") == version, check=check,
    )


def list_templates(project: Project) -> TemplateList:
    root = project.root
    items = []
    if _dir(root).exists():
        for d in sorted(p for p in _dir(root).iterdir() if _TPL_ID.match(p.name)):
            items += [template_info(project, d.name, v) for v in _versions(root, d.name)]
    default_check = root / "paper" / ".template_check_default.json"
    return TemplateList(
        current=project.config.template, templates=items,
        default_check=read_json(default_check) if default_check.exists() else None,
    )
