"""M3 样板与风格锚：把「一节满意的样板讲义」固化成后续生成的风格参数。

样板锚存在 ``course.meta["style_anchor"]``：
``{source_item_id, source_node_id, source_node_key, source_title, tone, depth,
figure_style, quiz_difficulty, terminology[], created_at}``。

生成讲义时由 :func:`style_anchor_prompt` 注入提示词，保证按章批量跨章风格一致；
锚缺失时批量入口要求用户显式选择「忽略样板」，而不是静默换一套风格。
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.course import ContentItem, Course, OutlineNode, QuizItem

STYLE_ANCHOR_KEY = "style_anchor"
SOURCE_ITEM_KEY = "style_anchor_item"

DEPTH_SHORT = "精简"
DEPTH_MEDIUM = "中"
DEPTH_DEEP = "深入"

# 正文长度（字符）分档：样板讲义的「深度」由此推断
DEPTH_MEDIUM_CHARS = 1200
DEPTH_DEEP_CHARS = 2600

FIG_STYLE_CONCEPT = "Excalidraw 概念卡"
FIG_STYLE_CHART = "Excalidraw 数据图"
FIG_STYLE_BOTH = "Excalidraw 概念卡 + 数据图"
FIG_STYLE_NONE = "以文字与表格为主"

DIFFICULTY_LABEL = {"easy": "基础", "medium": "中等", "hard": "进阶"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_style_anchor(course: Course) -> dict | None:
    """读取课程样板锚；没有或结构不合法时返回 None。"""
    anchor = (course.meta or {}).get(STYLE_ANCHOR_KEY)
    if isinstance(anchor, dict) and anchor.get("source_node_key"):
        return dict(anchor)
    return None


def save_style_anchor(db: Session, course: Course, anchor: dict) -> dict:
    """写入课程样板锚（同时保留最近一次样板讲义条目 id）。"""
    meta = dict(course.meta or {})
    meta[STYLE_ANCHOR_KEY] = dict(anchor)
    course.meta = meta
    db.commit()
    db.refresh(course)
    return dict(anchor)


def clear_style_anchor(db: Session, course: Course) -> None:
    """清除样板锚（回到「先生成一节样板」状态）。"""
    meta = dict(course.meta or {})
    meta.pop(STYLE_ANCHOR_KEY, None)
    course.meta = meta
    db.commit()
    db.refresh(course)


def _depth_of(markdown: str) -> str:
    chars = len(markdown or "")
    if chars >= DEPTH_DEEP_CHARS:
        return DEPTH_DEEP
    if chars >= DEPTH_MEDIUM_CHARS:
        return DEPTH_MEDIUM
    return DEPTH_SHORT


def _figure_style_of(item: ContentItem) -> str:
    meta = dict(item.meta or {})
    concepts = [x for x in (meta.get("diagram_ids") or []) if x]
    charts = [x for x in (meta.get("chart_ids") or []) if x]
    if concepts and charts:
        return FIG_STYLE_BOTH
    if concepts:
        return FIG_STYLE_CONCEPT
    if charts:
        return FIG_STYLE_CHART
    return FIG_STYLE_NONE


def _quiz_difficulty_of(db: Session, item: ContentItem) -> str:
    ids = [q for q in ((item.meta or {}).get("quiz_ids") or []) if q]
    if not ids:
        return DIFFICULTY_LABEL["medium"]
    rows = db.query(QuizItem).filter(QuizItem.id.in_(ids)).all()
    counter = Counter(str(row.difficulty or "medium") for row in rows)
    if not counter:
        return DIFFICULTY_LABEL["medium"]
    return DIFFICULTY_LABEL.get(counter.most_common(1)[0][0], DIFFICULTY_LABEL["medium"])


def derive_anchor(db: Session, course: Course, item: ContentItem,
                  node: OutlineNode | None = None) -> dict:
    """从样板讲义里抽取风格锚（规则抽取，确定性、可测；用户可在 UI 上改）。"""
    body = dict(item.body or {})
    node = node or (db.get(OutlineNode, item.node_id) if item.node_id else None)
    keywords = [str(k) for k in (body.get("keywords") or []) if str(k).strip()]
    return {
        "source_item_id": item.id,
        "source_node_id": item.node_id,
        "source_node_key": (node.node_key if node else str((item.meta or {}).get("node_key") or "")),
        "source_title": (node.title if node else item.title),
        "tone": str(body.get("tone") or "正式"),
        "depth": _depth_of(str(body.get("markdown") or "")),
        "figure_style": _figure_style_of(item),
        "quiz_difficulty": _quiz_difficulty_of(db, item),
        "terminology": keywords[:8],
        "created_at": _now_iso(),
    }


def anchor_summary(anchor: dict) -> str:
    """样板横幅上的一行摘要。"""
    parts = [
        f"{anchor.get('source_node_key') or ''}《{anchor.get('source_title') or ''}》".strip(),
        f"讲法:{anchor.get('tone') or '-'}",
        f"深度:{anchor.get('depth') or '-'}",
        f"图:{anchor.get('figure_style') or '-'}",
    ]
    return " · ".join(parts)


ANCHOR_LOCK_REASON = "样板节：批量生成不会覆盖"


def anchor_source_item(db: Session, course: Course):
    """样板锚指向的那篇讲义（被删掉时返回 None）。"""
    anchor = get_style_anchor(course)
    if anchor is None:
        return None
    item_id = str(anchor.get("source_item_id") or "")
    if not item_id:
        return None
    item = db.get(ContentItem, item_id)
    if item is None or item.course_id != course.id or item.kind != "lecture":
        return None
    return item


def anchor_lock_state(db: Session, course: Course) -> dict:
    """样板节当前的锁定状态：界面横幅据此提示「批量不会覆盖」。

    P4-1 的现象：样板锚写进 ``course.meta`` 后该节点的输入指纹随之变化，批量时
    它不再算「输入未变」→ 老师认可的那一版被悄悄重做、进了 ``meta.history``。
    所以「以这篇为样板」时自动加锁（locked 语义本来就有），这里把状态读出来给界面。
    """
    anchor = get_style_anchor(course)
    if anchor is None:
        return {"has_anchor": False, "exists": False, "locked": False,
                "source_item_id": None, "source_node_key": "", "notice": ""}
    item = anchor_source_item(db, course)
    exists = item is not None
    locked = bool(exists and (item.meta or {}).get("locked"))
    if not exists:
        notice = "样板讲义已不在（可能被删了）：请重新生成一节样板并点「以这篇为样板」"
    elif locked:
        notice = "样板节已锁定：按章 / 整课批量都会跳过它，不会覆盖这一版。"
    else:
        notice = ("样板节当前**没有**加锁：批量会把它一起重做，定稿的那一版会被覆盖。"
                  "想保留就点「加锁样板节」。")
    return {
        "has_anchor": True, "exists": exists, "locked": locked,
        "source_item_id": item.id if exists else anchor.get("source_item_id"),
        "source_node_id": anchor.get("source_node_id"),
        "source_node_key": anchor.get("source_node_key") or "",
        "notice": notice,
    }


def anchor_overwrite_risk(db: Session, course: Course,
                          nodes: list[OutlineNode]) -> dict | None:
    """这次批量会不会动到样板节：已锁定 → 会被跳过（安全）；没锁 → 要显式确认。

    返回 None 表示这次批量碰不到样板节（或在范围内但已锁定、会被 locked_skipped 接住）。
    """
    anchor = get_style_anchor(course)
    if anchor is None:
        return None
    item = anchor_source_item(db, course)
    if item is None or item.node_id is None:
        return None
    hit = next((node for node in nodes if node.id == item.node_id), None)
    if hit is None:
        return None
    locked = bool((item.meta or {}).get("locked"))
    return {
        "node_id": hit.id,
        "node_key": hit.node_key,
        "title": hit.title,
        "source_item_id": item.id,
        "locked": locked,
        "message": (
            f"样板节 {hit.node_key} {hit.title} 在本次范围内：已加锁，会被跳过（locked_skipped），不会覆盖。"
            if locked else
            f"样板节 {hit.node_key} {hit.title} 在本次范围内且**没有加锁**：批量会覆盖这一版。"
            "要保留就先点「加锁样板节」；确实要覆盖，请显式确认后重试。"
        ),
    }


def style_anchor_prompt(anchor: dict | None) -> str:
    """把样板锚转成提示词片段；没有样板时返回空串（保持旧行为）。"""
    if not anchor or not anchor.get("source_node_key"):
        return ""
    lines = [
        "",
        "【风格锚】本课程已用下面这篇样板讲义定下风格，本讲必须与它保持一致：",
        f"- 样板出处：{anchor.get('source_node_key')}《{anchor.get('source_title')}》",
        f"- 讲法：{anchor.get('tone') or '正式'}",
        f"- 深度：{anchor.get('depth') or DEPTH_MEDIUM}",
        f"- 图示风格：{anchor.get('figure_style') or FIG_STYLE_CONCEPT}",
        f"- 习题难度：{anchor.get('quiz_difficulty') or DIFFICULTY_LABEL['medium']}",
    ]
    terms = [str(t) for t in (anchor.get("terminology") or []) if str(t).strip()]
    if terms:
        lines.append(f"- 术语表（全文统一用词）：{'、'.join(terms)}")
    lines.append("只对齐讲法/深度/用词/难度，不要照抄样板的内容。")
    return "\n".join(lines)