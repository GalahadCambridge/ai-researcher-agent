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


def _inactive_spans(text: str) -> list[tuple[int, int, bool]]:
    """Read lexical spans; each tuple is (start, end, is_literal), excluding active TeX."""
    spans = []
    line_end = re.compile(r"[\r\n]")
    control_word = re.compile(r"\\([A-Za-z]+)")
    environment = re.compile(r"\\begin\s*\{(verbatim\*?|lstlisting|minted)\}")
    i = 0
    while i < len(text):
        if text[i] == "%":
            newline = line_end.search(text, i)
            end = newline.start() if newline else len(text)
            spans.append((i, end, False))
            i = end
        elif text[i] == "\\":
            word = control_word.match(text, i)
            if word is None:
                # Consume control symbols atomically: \%, \\, \{, etc.
                i += 2
                continue
            name = word[1]
            end = word.end()
            if name == "verb":
                if end < len(text) and text[end] == "*":
                    end += 1
                if end == len(text) or text[end].isspace():
                    raise TemplateError(f"Missing or unsupported delimiter for \\verb at offset {i}")
                close = text.find(text[end], end + 1)
                newline = line_end.search(text, end + 1)
                if close == -1 or (newline and newline.start() < close):
                    raise TemplateError(f"Unclosed \\verb literal at offset {i}")
                end = close + 1
                spans.append((i, end, True))
            elif name == "begin" and (literal := environment.match(text, i)):
                close = re.compile(r"\\end\s*\{" + re.escape(literal[1]) + r"\}").search(text, literal.end())
                if close is None:
                    raise TemplateError(f"Unclosed literal environment {literal[1]} at offset {i}")
                end = close.end()
                spans.append((i, end, True))
            i = end
        else:
            i += 1
    return spans


def active_text(text: str) -> str:
    """Blank comments/literals in lexical order, preserving offsets and CR/LF.

    Recognize control words/symbols, verb/verb*, and the existing literal environments.
    Inline literals must have an immediate non-whitespace delimiter and close on the
    same line. Malformed supported literals raise rather than expose their contents.
    Macro expansion and catcode changes remain outside this conservative scanner.
    """
    chars = list(text)
    for start, end, _ in _inactive_spans(text):
        chars[start:end] = [c if c in "\r\n" else " " for c in text[start:end]]
    return "".join(chars)


def _is_escaped(text: str, index: int) -> bool:
    before = index - 1
    while before >= 0 and text[before] == "\\":
        before -= 1
    return (index - 1 - before) % 2 == 1


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
            if clean[i] in "{}" and not _is_escaped(clean, i):
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
