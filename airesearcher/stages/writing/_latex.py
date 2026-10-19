"""Small conservative LaTeX scanner and filesystem checks; not a TeX interpreter."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath


class TemplateError(ValueError):
    """Unsafe or structurally unsupported template; caller must request a fix."""


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


def active_text(text: str) -> str:
    """Blank comments and literal environments without changing character offsets."""
    chars = list(text)
    # A percent sign is escaped iff preceded by an odd number of backslashes.
    for m in re.finditer(r"%", text):
        i = m.start() - 1
        while i >= 0 and text[i] == "\\":
            i -= 1
        if (m.start() - 1 - i) % 2 == 0:
            end = text.find("\n", m.start())
            end = len(text) if end == -1 else end
            chars[m.start():end] = " " * (end - m.start())
    clean = "".join(chars)
    literal = re.compile(r"\\begin\{(verbatim\*?|lstlisting|minted)\}.*?\\end\{\1\}", re.S)
    for m in literal.finditer(clean):
        chars[m.start():m.end()] = ["\n" if c == "\n" else " " for c in m.group()]
    clean = "".join(chars)
    for m in re.finditer(r"\\verb\*?([^\w\s]).*?\1", clean):
        chars[m.start():m.end()] = " " * len(m.group())
    return "".join(chars)


@dataclass(frozen=True)
class Command:
    name: str
    value: str
    start: int
    end: int
    value_start: int
    value_end: int


def commands(text: str) -> list[Command]:
    clean = active_text(text)
    out = []
    for m in re.finditer(r"(?<!\\)\\([A-Za-z]+)\*?\s*(?:\[[^\]]*\]\s*)?\{", clean):
        depth, i = 1, m.end()
        while i < len(clean) and depth:
            if clean[i] in "{}" and (i == 0 or clean[i - 1] != "\\"):
                depth += 1 if clean[i] == "{" else -1
            i += 1
        if depth:
            raise TemplateError(f"Unbalanced argument of \\{m.group(1)}")
        out.append(Command(m.group(1), clean[m.end():i - 1], m.start(), i, m.end(), i - 1))
    return out


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
