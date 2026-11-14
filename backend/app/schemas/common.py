"""通用响应模式。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    ok: bool = True
    data: T | None = None
    message: str = ""


class JobResult(BaseModel):
    job_id: str
    status: str
    result: dict[str, Any] = {}
