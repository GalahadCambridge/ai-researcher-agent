"""Extract PDF text and select reading excerpts; no OCR or reconstruction of tables and equations."""

from __future__ import annotations

import io
import re

from pypdf import PdfReader

MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_PAGES = 150
READING_WORD_LIMIT = 12000
EXTRACTION_VERSION = 2
_TOP_HEADING = re.compile(r"^\s*(?:\d+|[A-H])[.)]?\s+([A-Za-z][^\n]{1,90})$")


def _category(title):
    # PDF small caps often appear as "I NTRODUCTION" or "O UR METHOD" in extracted text.
    compact = re.sub(r"[^a-z]", "", title.lower())
    for category, words in (
        ("abstract", ("abstract",)), ("introduction", ("introduction",)),
        ("method", ("method", "approach", "algorithm", "modelarchitecture")),
        ("experiment", ("experiment", "evaluation", "result")),
        ("limitation", ("limitation",)), ("conclusion", ("conclusion",)),
    ):
        if any(word in compact for word in words):
            return category
    return "other"


def _prefix_words(text: str, maximum: int) -> str:
    tokens = list(re.finditer(r"\S+", text))
    return text if len(tokens) <= maximum else text[:tokens[maximum - 1].end()]


def reading_excerpt(text: str, maximum: int = READING_WORD_LIMIT):
    if len(text.split()) <= maximum:
        return text, {"strategy": "all_extracted_text", "excerpted": False}
    sections, current, page = [], None, "[p.1]"
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if re.fullmatch(r"\[p\.\d+\]", stripped):
            page = stripped
        numbered = _TOP_HEADING.match(stripped)
        # Only split at top-level headings: child sections stay with their parent method/experiment section.
        bare = stripped.isupper() and _category(stripped) != "other" and len(stripped.split()) <= 6
        if numbered or bare:
            title = numbered.group(1) if numbered else stripped
            current = {"name": _category(title), "page": page, "lines": []}
            sections.append(current)
        if current is not None:
            current["lines"].append(line)
    remaining = {"abstract": 800, "introduction": 1000, "method": 4000,
                 "experiment": 4500, "limitation": 800, "conclusion": 600}
    chosen = []
    for section in sections:
        name = section["name"]
        if remaining.get(name, 0) <= 0:
            continue
        body = "".join(section["lines"])
        if name == "introduction":
            paragraphs = [p for p in re.split(r"\n\s*\n", body) if p.strip()]
            body = "\n\n".join(paragraphs[-2:])
        part = _prefix_words(body, remaining[name])
        remaining[name] -= len(part.split())
        chosen.append(section["page"] + "\n" + part)
    joined = "\n\n".join(chosen)
    if not chosen or len(joined.split()) < min(1000, maximum // 2) or not any(
            section["name"] in {"method", "experiment"} for section in sections):
        return _prefix_words(text, maximum), {
            "strategy": "leading_text_fallback", "excerpted": True,
            "note": (
                "Insufficient section detection; using a word-limited prefix of the extracted "
                "text. The complete PDF and extracted text are retained."
            ),
        }
    return _prefix_words(joined, maximum), {
        "strategy": "section_headings_heuristic", "excerpted": True,
        "note": (
            "Section detection is heuristic; the last two introduction paragraphs are "
            "selected. The original PDF and complete extracted text are retained for "
            "verification."
        ),
    }


def extract_pdf(data: bytes):
    if len(data) > MAX_PDF_BYTES or not data.lstrip().startswith(b"%PDF-"):
        raise ValueError("Invalid PDF or file size exceeds the limit")
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("The PDF is encrypted and cannot be extracted")
    if not 0 < len(reader.pages) <= MAX_PAGES:
        raise ValueError("The PDF has no pages or exceeds the page limit")
    chunks, empty_pages, total_chars = [], [], 0
    for number, page in enumerate(reader.pages, 1):
        content = page.get_contents()
        if content is not None and len(content.get_data()) > 10 * 1024 * 1024:
            raise ValueError("A PDF page content stream exceeds the size limit")
        text = page.extract_text() or ""
        if not text.strip():
            empty_pages.append(number)
        total_chars += len(text)
        if total_chars > 2_000_000:
            raise ValueError("Extracted PDF text exceeds the size limit")
        chunks.append(f"[p.{number}]\n{text}")
    raw = "\n\n".join(chunks)
    if sum(len(t.split()) for t in chunks) - len(chunks) < 30:
        raise ValueError("The PDF has no usable text and may be scanned; OCR is not supported in this version")
    excerpt, details = reading_excerpt(raw)
    return raw, excerpt, {**details, "pages": len(reader.pages), "empty_text_pages": empty_pages,
                          "raw_words": len(raw.split()), "reading_words": len(excerpt.split())}
