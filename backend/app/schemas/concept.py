"""概念库（原子笔记）请求体。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ConceptGenerateRequest(BaseModel):
    node_id: str | None = Field(
        default=None, description="预留：只抽取某一章的概念（v1 为整课统一抽取，忽略该字段）"
    )


class ConceptSupplementRequest(BaseModel):
    names: list[str] = Field(
        default_factory=list,
        description="需要补抽的概念名（库外关系目标 / 孤点）；"
                    "留空时自动取图谱里「指向库外的关系目标」。",
    )


class ConceptUpdateRequest(BaseModel):
    definition: str | None = Field(default=None, description="一句话定义")
    points: list[str] | None = Field(default=None, description="要点 3~5 条")
    pitfalls: list[str] | None = Field(default=None, description="常见误区")
    aliases: list[str] | None = Field(default=None, description="别名（归一与合并用）")
    relations: dict[str, list[str]] | None = Field(
        default=None,
        description="关系（broader/narrower/related → 概念名列表）；指向库外的名字不画边、"
                    "会在图谱里显式列出，不静默丢弃",
    )
