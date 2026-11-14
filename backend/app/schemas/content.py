"""内容生成模式。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ContentGenerateRequest(BaseModel):
    course_id: str
    node_ids: list[str] = Field(default_factory=list, description="为空则生成全部知识点")
    tone: str = Field(default="正式", description="语气风格：正式/轻松/幽默")
    include_quiz: bool = True


class ContentItemOut(BaseModel):
    id: str
    course_id: str
    node_id: str | None = None
    kind: str
    title: str
    body: dict
    meta: dict
