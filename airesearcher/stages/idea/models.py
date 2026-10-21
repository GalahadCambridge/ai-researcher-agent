"""Internal idea-stage output schemas; frozen handoff models remain unchanged."""

import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator

from airesearcher.core.models.idea import SelectedIdea
from airesearcher.core.models.literature import CardItem


class IdeaBrief(BaseModel):
    research_object: str = Field(min_length=1)
    task: str
    target_variables: list[str]
    data_or_workload: list[str]
    constraints: dict
    keywords: list[str] = Field(min_length=1)
    missing_info: list[str]


class QueryGroup(BaseModel):
    question: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    queries: list[str] = Field(min_length=1, max_length=2)

    @field_validator("queries")
    @classmethod
    def nonempty_queries(cls, queries: list[str]) -> list[str]:
        if any(not q.strip() for q in queries):
            raise ValueError("Queries must not be empty or whitespace-only")
        for query in queries:
            if (len(query.split()) > 6 or re.search(r'\b(?:AND|OR|NOT)\b|["():]', query)):
                raise ValueError("Use at most six plain keyword tokens, without Boolean operators or field syntax")
        return [q.strip() for q in queries]


class QueryPlan(BaseModel):
    subquestions: list[QueryGroup] = Field(min_length=3, max_length=5)

    def queries(self) -> list[str]:
        return list(dict.fromkeys(q.strip() for group in self.subquestions for q in group.queries if q.strip()))


class PaperRelevance(BaseModel):
    paper_id: str = Field(min_length=1)
    score: int = Field(ge=1, le=5, strict=True)
    reason: str = Field(min_length=1)

    @field_validator("reason")
    @classmethod
    def nonempty_reason(cls, reason: str) -> str:
        if not reason.strip():
            raise ValueError("The relevance rationale must not be empty")
        return reason.strip()


class ScreenBatch(BaseModel):
    papers: list[PaperRelevance]


class ReaderOutput(BaseModel):
    items: list[CardItem]
    relevance_to_question: str = Field(min_length=1)


class ResearchGap(BaseModel):
    gap_id: str = Field(pattern=r"^G[1-9][0-9]*$")
    description: str = Field(min_length=1)
    evidence: list[str]
    status: Literal["supported", "to_verify"]

    @field_validator("description")
    @classmethod
    def nonempty_description(cls, description: str) -> str:
        if not description.strip():
            raise ValueError("The gap description must not be empty")
        return description.strip()


class GapAnalysis(BaseModel):
    gaps: list[ResearchGap]


NonemptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class EvidencePoint(BaseModel):
    text: NonemptyText
    evidence: list[str] = Field(min_length=1)
    to_verify: bool = False


class ReadingTheme(BaseModel):
    theme: NonemptyText
    points: list[EvidencePoint] = Field(min_length=1)


class ConflictCandidate(BaseModel):
    field: NonemptyText
    evidence: list[str] = Field(min_length=2)
    note: NonemptyText


class CoordinatorOutput(BaseModel):
    themes: list[ReadingTheme]
    feasibility_notes: list[EvidencePoint]
    gaps: list[ResearchGap]
    conflicts: list[ConflictCandidate]


class ReadingConflict(BaseModel):
    conflict_id: str = Field(pattern=r"^K[1-9][0-9]*$")
    paper_id: str | None
    field: str
    positions: list[dict] = Field(min_length=2)
    detected_by: Literal["rule", "coordinator"]
    resolution: Literal["unresolved", "resolved_by_reread", "needs_human"] = "unresolved"
    note: str = ""
    accepted_evidence: list[str] = Field(default_factory=list)


class ReviewQuote(BaseModel):
    ref: str
    quote: NonemptyText


class Adjudication(BaseModel):
    resolution: Literal["resolved_by_reread", "needs_human"]
    accepted_evidence: list[str]
    quotes: list[ReviewQuote]
    explanation: NonemptyText


class ReadingSummary(BaseModel):
    themes: list[ReadingTheme]
    gaps: list[ResearchGap]
    feasibility_notes: list[EvidencePoint]
    abstract_only_papers: list[str]
    incomplete_reading: list[str] = Field(default_factory=list)
    unresolved_conflicts: list[str] = Field(default_factory=list)


class RefinementOutput(BaseModel):
    idea: SelectedIdea
    task_fit: Literal["supported", "mismatch", "unknown"]
    task_fit_reason: NonemptyText


class StrictDimension(BaseModel):
    score: int = Field(ge=1, le=5, strict=True)
    rationale: NonemptyText
    evidence: list[str]
    to_verify: bool


class ScoreOutput(BaseModel):
    novelty: StrictDimension
    feasibility: StrictDimension
    impact: StrictDimension
    testability: StrictDimension
    risk: StrictDimension


class GapCell(BaseModel):
    text: NonemptyText
    evidence: list[str]
    to_verify: bool = False


class GapRow(BaseModel):
    paper_id: NonemptyText
    cells: dict[str, GapCell]


class GapTableOutput(BaseModel):
    rows: list[GapRow]


class RevisionOutput(RefinementOutput):
    needs_more_literature: bool
    new_queries: list[NonemptyText] = Field(max_length=3)
