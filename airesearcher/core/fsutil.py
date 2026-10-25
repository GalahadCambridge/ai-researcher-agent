"""文件读写小工具：原子写、哈希、时间。所有模块都用这里的函数写文件，保证崩溃时不会留下半个文件。"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def now() -> datetime:
    """带本地时区的当前时间（文档约定：ISO 8601 带时区）。"""
    return datetime.now().astimezone()


def rand_hex(n: int = 4) -> str:
    return secrets.token_hex((n + 1) // 2)[:n]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _replace(src: str, dst: Path) -> None:
    """os.replace，Windows 上目标文件正被别的进程打开时会短暂失败，重试几次。"""
    for i in range(50):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == 49:
                raise
            time.sleep(0.02)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """先写临时文件再 rename：读者要么看到旧内容，要么看到新内容。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        _replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def to_jsonable(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    return obj


def dumps(obj: Any, indent: int | None = 2) -> str:
    return json.dumps(to_jsonable(obj), ensure_ascii=False, indent=indent, default=str)


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, dumps(obj) + "\n")


def read_json(path: Path) -> Any:
    for i in range(50):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except PermissionError:  # Windows：文件正被原子替换
            if i == 49:
                raise
            time.sleep(0.02)


def append_jsonl(path: Path, obj: Any) -> None:
    """只追加日志：一行一个 JSON。调用方负责加锁。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(to_jsonable(obj), ensure_ascii=False, default=str)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()


def read_jsonl(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # 崩溃时最后一行可能写了一半：跳过，不影响其他记录
                continue
    return out


def iter_jsonl_reverse(path: Path, chunk_size: int = 1 << 16) -> Iterator[dict]:
    """从文件末尾往前逐行读 JSONL（取最新几条时不用读全量）。坏行同 read_jsonl 一样跳过。"""
    path = Path(path)
    if not path.exists():
        return
    with open(path, "rb") as f:
        pos = f.seek(0, os.SEEK_END)
        rest = b""
        while pos > 0:
            n = min(chunk_size, pos)
            pos -= n
            f.seek(pos)
            lines = (f.read(n) + rest).split(b"\n")
            rest = lines[0]  # 可能是半行，留到下一块拼上
            for line in reversed(lines[1:]):
                obj = _parse_jsonl_line(line)
                if obj is not None:
                    yield obj
        obj = _parse_jsonl_line(rest)
        if obj is not None:
            yield obj


def _parse_jsonl_line(line: bytes) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
