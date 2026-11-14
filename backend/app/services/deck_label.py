"""课件展示标签的唯一口径（前端 frontend/src/lib/chapterLabel.ts 与之一字对齐）。

同一份大纲节点在不同界面曾有三种写法（DB 标题「1 发酵原理与微生物基础 · 课件」、
笔记库文件名「第1章-发酵原理与微生物基础课件.pptx」、界面行「1 发酵原理与微生物基础」），
用户看不出谁是谁。这里把「怎么显示」收敛到一个函数：

    章    ：第1章 发酵原理与微生物基础
    知识点：1.1 多菌共生体系与发酵定义
    整课  ：整课课件（笔记库文件名仍是 00-整课课件.pptx）

标题里若已自带「第N章」前缀（旧数据），先剥掉再拼，避免「第2章 第2章 …」。
"""
from __future__ import annotations

import re

SCOPE_COURSE = "course"
SCOPE_CHAPTER = "chapter"

# 整课课件的统一叫法（笔记库文件名仍是 00-整课课件.pptx，不受影响）
COURSE_DECK_LABEL = "整课课件"

_RE_CHAPTER_PREFIX = re.compile(r"^\s*第\s*[0-9一二三四五六七八九十百]+\s*章[\s:：、.．-]*")


def _text(value) -> str:
    return str(value if value is not None else "").strip()


def strip_chapter_prefix(title) -> str:
    """去掉标题里自带的「第N章 / 第N章:」前缀（章名本身不含章号才是常态）。"""
    return _RE_CHAPTER_PREFIX.sub("", _text(title)).strip()


def node_label(key, title, node_type: str = "") -> str:
    """大纲节点 → 统一展示标签：章 = 「第N章 名」，知识点 = 「N.N 名」。"""
    key_text = _text(key)
    is_chapter = str(node_type or "") in ("chapter", "section") or (
        bool(key_text) and "." not in key_text
    )
    if is_chapter:
        name = strip_chapter_prefix(title) or key_text
        return f"第{key_text}章 {name}" if key_text else name
    return f"{key_text} {_text(title)}".strip()


def chapter_label(chapter) -> str:
    """章节点对象 → 「第1章 发酵原理与微生物基础」（课件标题 / 页脚 / 界面共用）。"""
    return node_label(getattr(chapter, "node_key", ""), getattr(chapter, "title", ""),
                      getattr(chapter, "node_type", "") or "chapter")


def deck_title(scope: str, course_title: str, chapter=None, *, suffix: str = "") -> str:
    """课件 DB 标题：章课件 = 「第1章 名 · 课件」，整课 = 「课程名 · 整课课件」。"""
    if str(scope) == SCOPE_CHAPTER and chapter is not None:
        return f"{chapter_label(chapter)} · 课件{suffix}"
    return f"{_text(course_title)} · {COURSE_DECK_LABEL}{suffix}"
