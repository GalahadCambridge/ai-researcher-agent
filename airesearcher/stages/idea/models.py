"""选题阶段的内部输出结构，不修改已冻结的交接模型。"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

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
            raise ValueError("查询词不能为空或只有空白")
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
            raise ValueError("评分理由不能为空")
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
            raise ValueError("差距描述不能为空")
        return description.strip()


class GapAnalysis(BaseModel):
    gaps: list[ResearchGap]
