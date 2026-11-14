"""通用内容操作与大纲/习题编辑模式。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ContentUpdateRequest(BaseModel):
    """通用内容项编辑：title / body 可选，body 做一层深度合并。"""
    title: str | None = None
    body: dict | None = None


class PlanNodeUpdate(BaseModel):
    title: str | None = None
    objective: str | None = None
    teaching_strategy: str | None = None
    assessment: str | None = None
    prerequisites: list[str] | None = None


class QuizUpdateRequest(BaseModel):
    qtype: str | None = None
    difficulty: str | None = None
    question: str | None = None
    options: list[str] | None = None
    answer: str | None = None
    explanation: str | None = None
    # 任务 55：分层 / 预计用时 / 开放题评分要点（落 quiz_items.meta）
    level: str | None = None
    minutes: int | None = None
    rubric: str | None = None
