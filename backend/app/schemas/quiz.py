"""习题生成模式。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class QuizGenerateRequest(BaseModel):
    course_id: str
    node_id: str | None = None
    count: int = Field(default=5, ge=1, le=20)
    difficulty: str = Field(default="medium", pattern="^(easy|medium|hard)$")


class QuizOut(BaseModel):
    id: str
    course_id: str
    node_id: str | None = None
    qtype: str
    difficulty: str
    question: str
    options: list[str]
    answer: str
    explanation: str
    # 任务 55：分层 / 预计用时 / 开放题评分要点（读，落库在 quiz_items.meta）
    level: str = ""
    minutes: int = 0
    rubric: str = ""
