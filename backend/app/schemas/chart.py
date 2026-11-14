"""教学图表请求/响应模式。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ChartType = Literal["bar", "line", "pie", "mindmap", "timeline", "table", "quadrant"]


class ChartGenerateRequest(BaseModel):
    course_id: str
    node_id: str | None = Field(default=None, description="绑定的章节/知识点节点")
    chart_type: ChartType = Field(description="图表类型：bar/line/pie/mindmap/timeline/table/quadrant")
    instruction: str = Field(default="", max_length=500, description="自然语言要求，如：近五年全球气温变化")
    item_id: str | None = Field(default=None, description="提供则原地重做该图表（类型须一致）")


class ChartUpdateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    data: dict | None = Field(default=None, description="替换图表数据（labels/datasets 或 headers/rows）")
