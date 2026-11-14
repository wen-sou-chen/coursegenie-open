"""分享链接请求模式（M5a）。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ShareCreateRequest(BaseModel):
    label: str = Field(default="", max_length=255, description="给谁用 / 备注")
    expires_in_days: int | None = Field(
        default=None, ge=1, le=3650, description="有效期天数；留空 = 不过期")
    password: str = Field(default="", max_length=128, description="可选访问密码")
    permission: Literal["view", "comment"] = Field(default="view", description="view 只读（预留 comment）")
    base_url: str | None = Field(
        default=None, max_length=500,
        description="拼分享链接用的前端地址（前端传 window.location.origin）；留空用请求 Host")