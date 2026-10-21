"""Shared protocol for live retrieval sources and FakeLiterature."""

from typing import Protocol

from airesearcher.core.models.literature import PaperRecord, SearchSnapshot


class LiteratureSource(Protocol):
    name: str

    def search(self, query: str, *, year_from: int | None, limit: int) -> tuple[SearchSnapshot, list[PaperRecord]]: ...

    def get(self, paper_id: str) -> PaperRecord | None: ...
