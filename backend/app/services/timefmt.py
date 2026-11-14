"""时间口径统一（任务 83）：库里的 naive datetime 一律按 UTC 解释，接口输出恒带时区。

背景（任务 81 的 A2 遗留）：``course_versions.created_at`` / ``share_links.expires_at`` 这类列是
``DateTime(timezone=True)`` 配 ``utcnow()``，但 **SQLite 不存时区** ⇒ 读回来是 naive，``isoformat()``
出来是 ``2026-09-21T06:24:46.708384``（没有 ``Z`` / ``+00:00``）；前端 ``new Date(...)`` 按 ES 规范
把没有时区标记的串当**本地时间**解释 ⇒ 界面上比真实本地时间少 8 小时（日期都可能变）。

本模块是**唯一出处**：``as_aware`` 负责比较（原有函数，从 ``share.py`` 提升而来），
``as_utc_iso`` 负责序列化 —— 让前端不必猜（前端侧对应 ``frontend/src/lib/datetime.ts``）。
"""
from __future__ import annotations

from datetime import datetime, timezone


def as_aware(value: datetime | None) -> datetime | None:
    """SQLite 读回来的 DateTime 是 naive：统一按 UTC 处理，避免比较时炸掉。"""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def as_utc_iso(value: datetime | None) -> str | None:
    """序列化给前端：naive 补 ``+00:00``，带时区的原样输出，``None`` 仍是 ``None``。"""
    aware = as_aware(value)
    return None if aware is None else aware.isoformat()
