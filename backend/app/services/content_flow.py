"""「流程图」计数唯一口径（前端 `frontend/src/lib/contentCounts.ts` 与之一字对齐）。

真实情况（任务 27 复核）：内容条目里 ``kind='flowchart'`` 是 0 条 —— 流程图**内嵌在讲义
正文的 ```mermaid 代码块里**（真实库共 4 段：1.2 / 2.1 / 2.2 / 3.2 各 1 段）。所以任何
“本课/本节有几张流程图”的展示都必须数讲义正文里的 mermaid 代码块，不能按 ``kind`` 计数，
否则界面永远显示 0，和顶部统计（mermaid 口径）自相矛盾。

本模块只做一件事：从 Markdown 文本里数 ```mermaid 段。讲义行在
``app.services.content.build_content_rows`` 里用 :func:`count_mermaid` 把段数写进
``meta.mermaid_blocks``，前端优先读它、旧数据再回退到解析正文，保证前后端同一口径。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

#: ```mermaid 代码块起始标记（与前端 /```mermaid/g 同口径；大小写不敏感）。
_FENCE_RE = re.compile(r"```\s*mermaid", re.IGNORECASE)

#: 讲义行 meta 里缓存的流程图段数（前端优先读它，读不到再解析正文）。
MERMAID_BLOCKS_KEY = "mermaid_blocks"


def count_mermaid(markdown: Any) -> int:
    """讲义正文里的 ```mermaid 段数（不看 kind，正文里有多少段就是多少）。"""
    return len(_FENCE_RE.findall(str(markdown or "")))


def flowchart_count(rows: Iterable[Any]) -> int:
    """一批内容行（dict 或 ORM 行）的流程图总数。

    口径：凡是会在界面上按 Markdown 渲染的条目（讲义 ``lecture`` / 正文 ``text``）都算，
    数的是它们正文里的 ```mermaid 段；没有正文的条目（含 ``kind='flowchart'`` 的独立图
    条目）贡献 0 —— 所以「按 kind 计数永远是 0」的假 0 不会出现。
    优先用 :func:`count_mermaid` 现算，保证与正文实际渲染一致；``meta.mermaid_blocks``
    只作为没有正文时的兜底。
    """
    total = 0
    for row in rows:
        body = getattr(row, "body", None)
        meta = getattr(row, "meta", None)
        if body is None and isinstance(row, dict):
            body, meta = row.get("body"), row.get("meta")
        markdown = (body or {}).get("markdown") if isinstance(body, dict) else None
        if markdown:
            total += count_mermaid(markdown)
            continue
        cached = (meta or {}).get("mermaid_blocks") if isinstance(meta, dict) else None
        if isinstance(cached, int):
            total += cached
    return total
