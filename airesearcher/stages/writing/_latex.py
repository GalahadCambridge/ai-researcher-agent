"""Small conservative LaTeX scanner and filesystem checks; not a TeX interpreter."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath

from airesearcher.services._tex import (  # retain Writing's existing private imports
    TemplateError as TemplateError,
)
from airesearcher.services._tex import (
    _inactive_spans as _inactive_spans,
)
from airesearcher.services._tex import (
    _is_escaped as _is_escaped,
)
from airesearcher.services._tex import (
    active_text as active_text,
)
from airesearcher.services._tex import (
    commands as commands,
)


def safe_path(path: Path) -> Path:
    path = Path(path).absolute()
    for part in [*reversed(path.parents), path]:
        if part.is_symlink():
            raise TemplateError(f"Symlink is not allowed: {part.name}")
    return path.resolve()


def safe_tree(root: Path) -> Path:
    root = safe_path(root)
    if not root.is_dir():
        raise TemplateError("Template/work directory does not exist")
    for path in root.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise TemplateError(f"Unsafe file: {path.relative_to(root).as_posix()}")
    return root


def relative_name(name: str) -> str:
    """TeX resource paths: portable, literal, relative, and never command options."""
    if (not name or name.startswith(("-", "/", "\\")) or "\\" in name
            or PureWindowsPath(name).drive or ".." in PurePosixPath(name).parts
            or not re.fullmatch(r"[\w ./+@-]+", name, re.ASCII)):
        raise TemplateError(f"Unsafe or nonliteral resource path: {name!r}")
    return PurePosixPath(name).as_posix()


def document_region(text: str) -> tuple[int, int]:
    cmds = commands(text)
    begins = [c for c in cmds if c.name == "begin" and c.value.strip() == "document"]
    ends = [c for c in cmds if c.name == "end" and c.value.strip() == "document"]
    classes = [c for c in cmds if c.name == "documentclass"]
    if len(begins) != 1 or len(ends) != 1 or len(classes) != 1:
        raise TemplateError("Expected one documentclass and one begin/end document pair")
    if not classes[0].end <= begins[0].start < begins[0].end <= ends[0].start:
        raise TemplateError("Invalid document structure")
    return begins[0].end, ends[0].start
