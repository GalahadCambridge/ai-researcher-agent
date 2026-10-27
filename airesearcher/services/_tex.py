"""Read-only conservative TeX lexical scanner shared by Writing and evidence services.

No macro expansion or catcode interpretation. Preserve original character offsets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class TemplateError(ValueError):
    """Unsupported or malformed TeX structure."""


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
    for m in re.finditer(r"\\([A-Za-z]+)\*?\s*(?:\[[^\]]*\]\s*)?\{", clean):
        # A control word may immediately follow one or more \\ line breaks.
        # Only an odd number of preceding slashes escapes this candidate slash.
        if _is_escaped(clean, m.start()):
            continue
        depth, i = 1, m.end()
        while i < len(clean) and depth:
            if clean[i] in "{}" and not _is_escaped(clean, i):
                depth += 1 if clean[i] == "{" else -1
            i += 1
        if depth:
            raise TemplateError(f"Unbalanced argument of \\{m.group(1)}")
        out.append(Command(m.group(1), clean[m.end():i - 1], m.start(), i, m.end(), i - 1))
    return out
