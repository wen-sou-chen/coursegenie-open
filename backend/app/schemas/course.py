"""课程相关模式。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CourseCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    subject: str = ""
    audience: str = ""
    duration_minutes: int = Field(default=60, ge=10, le=1440)
    meta: dict = {}


class CourseUpdate(BaseModel):
    title: str | None = None
    subject: str | None = None
    audience: str | None = None
    duration_minutes: int | None = None
    status: str | None = None
    meta: dict | None = Field(default=None, description=(
        "PATCH 语义：与课程已有 meta **合并**（不是整块覆盖），"
        "这样只带 K-12 字段的请求不会清掉 kb 绑定等既有键"))


class CourseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    subject: str
    audience: str
    duration_minutes: int
    status: str
    meta: dict
    # 任务 51：K-12 元数据归一化后**单独透出**（前端/集成方不必猜 meta 里是散写还是嵌套块）
    k12: dict = {}
    # 任务 85：使用场景（五选多 + 主场景）同样单透出来；**未选择 = 空 dict**（不补默认场景）
    scenario: dict = {}
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _fill_k12(self) -> "CourseOut":
        from app.services.k12 import k12_meta

        self.k12 = k12_meta(self.meta)
        return self

    @model_validator(mode="after")
    def _fill_scenario(self) -> "CourseOut":
        from app.services.scenario import scenario_meta

        self.scenario = scenario_meta(self.meta)
        return self


class CourseListItem(CourseOut):
    """课程**列表**专用投影（任务 48v）：meta 去掉 ``kb`` 导出清单。

    ``meta.kb`` 里装着整个知识库的 ``manifest``（逐文件 sha1）与 ``incr``（增量游标），
    实测占 ``GET /api/course/`` 响应体积的 99% 以上（单门课 568.6 KB），而列表页一个字都用不到
    —— 它只需要 ``id/title/subject/audience/duration_minutes/status/k12``。
    详情（``GET /api/course/{id}``）与 ``GET /api/kb/course/{id}`` 仍返回**完整** meta。
    """

    @model_validator(mode="after")
    def _strip_kb(self) -> "CourseListItem":
        # 整块去掉 kb（而不是只删 manifest/incr）：以后 kb 里再长出大子键也不会漏进列表
        if "kb" in self.meta:
            self.meta = {k: v for k, v in self.meta.items() if k != "kb"}
        return self
