"""Merge retrieval records by external identifiers and title, preferring published versions."""

import re
import unicodedata

from pydantic import BaseModel

from airesearcher.core.models.literature import PaperRecord


class PaperIdentity(BaseModel):
    """Minimal checkpoint fields used for identifier and title/year deduplication."""

    paper_id: str
    ids: dict[str, str]
    title: str
    year: int | None


def _id(key: str, value: str) -> str:
    value = value.strip().lower()
    if key == "doi":
        return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)", "", value)
    if key == "arxiv":
        return re.sub(r"v\d+$", "", value.removeprefix("arxiv:").split("/abs/")[-1])
    return value


def _title(text: str) -> str:
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", text).lower()))


def same_paper(a: PaperRecord | PaperIdentity, b: PaperRecord | PaperIdentity) -> bool:
    if a.paper_id == b.paper_id:
        return True
    if any(k in a.ids and k in b.ids and _id(k, a.ids[k]) == _id(k, b.ids[k])
           for k in ("doi", "arxiv", "s2")):
        return True
    return bool(_title(a.title) and _title(a.title) == _title(b.title)
                and a.year is not None and b.year is not None and abs(a.year - b.year) <= 1)


def deduplicate(papers: list[PaperRecord]) -> list[PaperRecord]:
    # Connected components also merge records that bridge two previously separate identifier sets.
    groups: list[list[PaperRecord]] = []
    for paper in papers:
        matches = [group for group in groups if any(same_paper(paper, old) for old in group)]
        if not matches:
            groups.append([paper])
        else:
            target = matches[0]
            target.append(paper)
            for group in matches[1:]:
                target.extend(group)
                groups.remove(group)
    result = []
    for group in groups:
        primary = max(group, key=lambda p: (
            p.venue_type in ("conference", "journal", "workshop"), bool(p.abstract), len(p.ids),
        ))
        merged = primary.model_copy(deep=True)
        for paper in group:
            for key, value in paper.ids.items():
                merged.ids.setdefault(key, value)
            merged.queries = list(dict.fromkeys([*merged.queries, *paper.queries]))
            merged.same_as = list(dict.fromkeys([
                *merged.same_as, *paper.same_as, *([paper.paper_id] if paper.paper_id != primary.paper_id else []),
            ]))
            if not merged.abstract and paper.abstract:
                merged.abstract = paper.abstract
                merged.access_level = "abstract_only"
        merged.same_as = [pid for pid in merged.same_as if pid != merged.paper_id]
        result.append(merged)
    return result
