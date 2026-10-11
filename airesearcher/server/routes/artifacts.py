"""产物与文件（详细设计 1 第 9.2、9.3 节）。"""

from __future__ import annotations

import fnmatch
import mimetypes

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from airesearcher.core.errors import NotFound, PermissionDenied, StaleVersion
from airesearcher.core.fsutil import atomic_write_text, sha256_file
from airesearcher.core.models.artifact import ArtifactVersion, LineageNode
from airesearcher.core.models.common import VersionRef

from ..deps import manager
from ..manager import ProjectManager
from ..schemas import EditRequest

router = APIRouter(prefix="/api/projects/{pid}", tags=["artifacts"])

# 9.3：本期只允许编辑文本型产物
EDITABLE = ["idea/selected.md", "plan/experiment_plan.md", "src/**", "configs/**", "paper/sections/*.tex"]


def _version_ref(m: ProjectManager, pid: str, artifact_id: str, version: int | None) -> VersionRef:
    p = m.get(pid)
    if version is None:
        ref = p.archive.latest(artifact_id)
        if ref is None:
            raise NotFound(f"找不到产物 {artifact_id}")
        return ref
    for v in p.archive.versions(artifact_id):
        if v.ref.version == version:
            return v.ref
    raise NotFound(f"找不到产物 {artifact_id} v{version}")


@router.get("/artifacts", response_model=list[ArtifactVersion])
def list_artifacts(pid: str, kind: str | None = None, m: ProjectManager = Depends(manager)) -> list[ArtifactVersion]:
    return m.get(pid).archive.list_latest(kind)


@router.get("/artifacts/versions", response_model=list[ArtifactVersion])
def artifact_versions(pid: str, artifact_id: str, m: ProjectManager = Depends(manager)) -> list[ArtifactVersion]:
    return m.get(pid).archive.versions(artifact_id)


@router.get("/artifacts/content")
def artifact_content(pid: str, artifact_id: str, version: int | None = None,
                     m: ProjectManager = Depends(manager)):
    p = m.get(pid)
    if not artifact_id.startswith(("runs/", "templates/")):
        p.archive.sync([artifact_id])  # 读取前做人工编辑检测（3.4）
    ref = _version_ref(m, pid, artifact_id, version)
    info = p.archive.version_info(ref)
    data = p.archive.get(ref)
    if not info.is_dir:
        try:
            return {"artifact_id": artifact_id, "version": ref.version, "sha256": ref.sha256,
                    "content": data.decode("utf-8")}
        except UnicodeDecodeError:
            path = p.archive.stored_path(ref)
            mt = mimetypes.guess_type(artifact_id)[0] or "application/octet-stream"
            return FileResponse(path, media_type=mt)
    return {"artifact_id": artifact_id, "version": ref.version, "sha256": ref.sha256,
            "content": data.decode("utf-8"), "is_dir": True}


@router.get("/artifacts/lineage", response_model=LineageNode)
def artifact_lineage(pid: str, artifact_id: str, version: int | None = None,
                     m: ProjectManager = Depends(manager)) -> LineageNode:
    return m.get(pid).archive.lineage(_version_ref(m, pid, artifact_id, version))


@router.put("/artifacts/content")
def edit_artifact(pid: str, body: EditRequest, m: ProjectManager = Depends(manager)) -> dict:
    p = m.get(pid)
    done = p.requests.get(body.request_id)
    if done is not None:  # 同一次编辑重复提交：返回第一次的结果，不再报“文件已被修改”
        return done
    out = _edit(p, body)
    p.requests.record(body.request_id, "edit_artifact", out)
    m.engine(pid).wake()
    return out


def _edit(p, body: EditRequest) -> dict:
    aid = body.artifact_id.strip("/")
    if not any(fnmatch.fnmatchcase(aid, pat) for pat in EDITABLE):
        raise PermissionDenied(f"{aid} 不允许在线编辑（可编辑：{', '.join(EDITABLE)}）")
    path = p.archive.path_of(aid)
    current = sha256_file(path) if path.exists() else ""
    if body.base_sha256 != current:
        raise StaleVersion("文件已被修改，请刷新后再编辑", {"current_sha256": current})
    if aid in ("idea/selected.md", "plan/experiment_plan.md"):
        latest = p.archive.latest_version(aid)
        ref = p.archive.put(aid, latest.kind if latest else "other", body.content,
                            parents=[latest.ref] if latest else [], producer="human-edited", note="用户在线编辑")
        return {"artifact_id": aid, "version": ref.version, "sha256": ref.sha256}
    atomic_write_text(path, body.content)
    if aid.startswith(("src/", "configs/")):
        p.workspace.commit(f"用户编辑 {aid}", author="human-edited")
    else:
        p.archive.sync(["paper"])
    return {"artifact_id": aid, "sha256": sha256_file(path)}


@router.get("/files")
def read_file(pid: str, path: str, m: ProjectManager = Depends(manager)):
    p = m.get(pid)
    target = p.archive.path_of(path)  # 越出工作区 → PERMISSION_DENIED
    if not target.is_file():
        raise NotFound(f"找不到文件 {path}")
    try:
        return {"path": path, "content": target.read_text(encoding="utf-8")}
    except UnicodeDecodeError:
        return FileResponse(target, media_type=mimetypes.guess_type(path)[0] or "application/octet-stream")
