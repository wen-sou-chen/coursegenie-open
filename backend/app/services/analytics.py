"""学习数据分析：基于现有数据的基础统计（P0 占位；P3 接 LRS 事件流）。"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.course import ContentItem, Course, QuizItem
from app.services import usage as usage_log
from app.services.planner import list_nodes


# 任务 81 §A1：字数口径 —— 汉字逐字算 1，连续英文 / 数字串算 1 个词（不含标点与空白）。
# 原先这里用 content.extract_words()，而它是 r"[\u4e00-\u9fff\w]+" 的**正则词块数**：
# 中文连成一片只算 1 个 ⇒ 标签写「总字数」却比真实字数小一个量级
# （AI 课实测 6,659 vs 去空白后 49,858 字符）。
_WORD_RE = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9]+(?:['\-][A-Za-z0-9]+)*")


def count_words(markdown: str) -> int:
    """正文字数：汉字逐字算 1，连续英文 / 数字串算 1 个词；不含标点与空白。"""
    return len(_WORD_RE.findall(markdown or ""))


def count_chars(markdown: str) -> int:
    """字符数：去掉空白后的全部字符（含标点）—— 与「字数」并列返回，便于对账口径。"""
    return len(re.sub(r"\s+", "", markdown or ""))


def course_stats(db: Session, course_id: str) -> dict:
    course = db.get(Course, course_id)
    if course is None:
        raise ValueError(f"课程不存在：{course_id}")

    nodes = list_nodes(db, course_id)
    # 任务 48x：内容条目要同时认讲义与老文本 —— 讲义管线写 kind="lecture"，
    # kind="text" 是 MVP 遗留（content.py 的单篇生成器已无调用方）。此前只认 text，
    # 导致「数据分析」的内容条目 / 节点覆盖度 / 总字数从讲义时代起恒为 0。
    texts = db.query(ContentItem).filter(
        ContentItem.course_id == course_id, ContentItem.kind.in_(("lecture", "text"))
    ).all()
    quizzes = db.query(QuizItem).filter(QuizItem.course_id == course_id).all()

    # 每章节覆盖度：有文本内容的节点占比
    covered = {t.node_id for t in texts}
    node_ids = [n.id for n in nodes]

    quiz_by_difficulty: dict[str, int] = {}
    for q in quizzes:
        quiz_by_difficulty[q.difficulty] = quiz_by_difficulty.get(q.difficulty, 0) + 1

    words_total = sum(count_words(t.body.get("markdown", "")) for t in texts)
    chars_total = sum(count_chars(t.body.get("markdown", "")) for t in texts)

    return {
        "course_id": course_id,
        "status": course.status,
        "outline_nodes": len(nodes),
        "content_items": len(texts),
        "node_coverage": round(len(covered & set(node_ids)) / len(node_ids), 2) if node_ids else 0,
        "words_total": words_total,
        "chars_total": chars_total,
        "quizzes": len(quizzes),
        "quiz_by_difficulty": quiz_by_difficulty,
        "low_score_alert": "P3 接入 LRS 后提供真实答题正确率",
        "high_exit_alert": "P3 接入事件流后提供章节跳出率",
    }


# 课程级辅助用量（不属于任何内容条目）：purpose → (页签里的 kind, 标题)
AUX_ROWS = {
    "outline": ("outline", "大纲生成（整课）"),
    "concept": ("concept_extract", "概念抽取（整课）"),
}


def _aux_rows(course: Course) -> list[dict]:
    """课程级辅助用量行（大纲 / 概念抽取），字段形状与 `_usage_row` 一致。

    这两段没有对应的内容条目，由 `usage.record_aux` 写在 `course.meta['aux_usage']`；
    不并进页签就会少报它们的 tokens 与费用（P3-4 / P6-2 / P9-3，实测少报 18.5%）。
    item_id 用 `aux:<purpose>` 占位，保证前端 rowKey 稳定唯一。
    """
    rows: list[dict] = []
    for purpose, summary in sorted(((course.meta or {}).get("aux_usage") or {}).items()):
        if not summary:
            continue
        kind, title = AUX_ROWS.get(purpose, (purpose, purpose))
        rows.append({
            "item_id": f"aux:{purpose}",
            "node_id": None,
            "node_key": "",
            "kind": kind,
            "title": title,
            "locked": False,
            "llm_calls": int(summary.get("llm_calls") or 0),
            "prompt_tokens": int(summary.get("prompt_tokens") or 0),
            "completion_tokens": int(summary.get("completion_tokens") or 0),
            "total_tokens": int(summary.get("total_tokens") or 0),
            "cost_cny": float(summary.get("cost_cny") or 0.0),
            "duration_ms": int(summary.get("duration_ms") or 0),
            "by_purpose": summary.get("by_purpose") or {},
            "unpriced_models": list(summary.get("unpriced_models") or []),
            "updated_at": summary.get("updated_at"),
        })
    return rows


def _aux_history(course: Course) -> dict:
    """辅助用量的历史累计：purpose → 多次跑的累计（含被重跑覆盖的版本）。

    ``aux_usage`` 是覆盖写，页签上的「累计」其实只是当前版本；历史账本由
    ``usage.record_aux`` 在 ``course.meta['aux_usage_history']`` 里另记一份（P9-4）。
    本页签上线前的老课程没有这份账本，此时按「当前版本 = 至少 1 次」给下限并标
    ``estimated``，不假装知道真实次数。
    """
    hist = usage_log.aux_history(course)
    rows: list[dict] = []
    for purpose, row in sorted(hist.items()):
        kind, title = AUX_ROWS.get(purpose, (purpose, purpose))
        rows.append({
            "purpose": purpose,
            "kind": kind,
            "title": title,
            "runs": int(row.get("runs") or 0),
            "llm_calls": int(row.get("llm_calls") or 0),
            "total_tokens": int(row.get("total_tokens") or 0),
            "cost_cny": float(row.get("cost_cny") or 0.0),
            "first_at": row.get("first_at"),
            "last_at": row.get("last_at"),
        })
    estimated = False
    if not rows:
        for purpose, summary in sorted(((course.meta or {}).get("aux_usage") or {}).items()):
            if not summary:
                continue
            kind, title = AUX_ROWS.get(purpose, (purpose, purpose))
            rows.append({
                "purpose": purpose,
                "kind": kind,
                "title": title,
                "runs": 1,
                "llm_calls": int(summary.get("llm_calls") or 0),
                "total_tokens": int(summary.get("total_tokens") or 0),
                "cost_cny": float(summary.get("cost_cny") or 0.0),
                "first_at": summary.get("updated_at"),
                "last_at": summary.get("updated_at"),
            })
        estimated = bool(rows)
    return {
        "runs": sum(row["runs"] for row in rows),
        "llm_calls": sum(row["llm_calls"] for row in rows),
        "total_tokens": sum(row["total_tokens"] for row in rows),
        "cost_cny": round(sum(row["cost_cny"] for row in rows), 6),
        "by_purpose": rows,
        "estimated": estimated,
    }


def _usage_row(item: ContentItem) -> dict | None:
    """把一个内容条目的 meta.usage 折成课程级报告的一行（没有用量则返回 None）。"""
    usage = (item.meta or {}).get("usage") or {}
    if not usage:
        return None
    meta = item.meta or {}
    return {
        "item_id": item.id,
        "node_id": item.node_id,
        "node_key": str(meta.get("node_key") or ""),
        "kind": item.kind,
        "title": item.title,
        "locked": bool(meta.get("locked")),
        "llm_calls": int(usage.get("llm_calls") or 0),
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "cost_cny": float(usage.get("cost_cny") or 0.0),
        "duration_ms": int(usage.get("duration_ms") or 0),
        "by_purpose": usage.get("by_purpose") or {},
        "unpriced_models": list(usage.get("unpriced_models") or []),
        "updated_at": usage.get("updated_at"),
    }


def course_usage(db: Session, course_id: str, *, top_n: int = 5) -> dict:
    """课程级用量与成本：累计 tokens/费用、平均单篇耗时、最贵 N 篇、按 kind 分布。

    口径：以「内容条目 meta.usage」为准逐条累加（单篇 = 一次生成的完整用量，
    含该篇的配图/卡片/习题调用），外加课程级辅助用量（大纲 / 概念抽取，见
    :func:`_aux_rows`）；未落用量的老条目不计入，也不做估算。
    """
    course = db.get(Course, course_id)
    if course is None:
        raise ValueError(f"课程不存在：{course_id}")

    items = (db.query(ContentItem)
             .filter(ContentItem.course_id == course_id)
             .order_by(ContentItem.created_at).all())
    rows = [row for row in (_usage_row(item) for item in items) if row]
    rows += _aux_rows(course)

    total_tokens = sum(row["total_tokens"] for row in rows)
    total_cost = round(sum(row["cost_cny"] for row in rows), 6)
    llm_calls = sum(row["llm_calls"] for row in rows)
    duration = sum(row["duration_ms"] for row in rows)

    by_kind: dict[str, dict] = {}
    for row in rows:
        bucket = by_kind.setdefault(row["kind"], {
            "kind": row["kind"], "items": 0, "llm_calls": 0,
            "prompt_tokens": 0, "completion_tokens": 0,
            "total_tokens": 0, "cost_cny": 0.0, "duration_ms": 0,
        })
        bucket["items"] += 1
        bucket["llm_calls"] += row["llm_calls"]
        bucket["prompt_tokens"] += row["prompt_tokens"]
        bucket["completion_tokens"] += row["completion_tokens"]
        bucket["total_tokens"] += row["total_tokens"]
        bucket["cost_cny"] = round(bucket["cost_cny"] + row["cost_cny"], 6)
        bucket["duration_ms"] += row["duration_ms"]
    by_kind_rows = sorted(by_kind.values(), key=lambda b: (-b["cost_cny"], b["kind"]))

    unpriced = sorted({model for row in rows for model in row["unpriced_models"]})
    aux_hist = _aux_history(course)
    top_items = sorted(rows, key=lambda r: (-r["cost_cny"], -r["total_tokens"]))[:top_n]
    lecture_rows = [row for row in rows if row["kind"] == "lecture"]
    lectures_duration = sum(row["duration_ms"] for row in lecture_rows)
    report = {
        "course_id": course.id,
        "course_title": course.title,
        "items_measured": len(rows),
        "lectures": len(lecture_rows),
        "llm_calls": llm_calls,
        "prompt_tokens": sum(row["prompt_tokens"] for row in rows),
        "completion_tokens": sum(row["completion_tokens"] for row in rows),
        "total_tokens": total_tokens,
        "cost_cny": total_cost,
        "avg_cost_cny": round(total_cost / len(rows), 6) if rows else 0.0,
        "duration_ms": duration,
        "avg_duration_ms": int(duration / len(rows)) if rows else 0,
        "avg_lecture_duration_ms": int(lectures_duration / len(lecture_rows)) if lecture_rows else 0,
        "priced": not unpriced,
        "unpriced_models": unpriced,
        "by_kind": by_kind_rows,
        "top_items": top_items,
        "items": rows,
        # P9-4：上面的 cost_cny/各行都是「当前版本」（重跑覆盖）；
        # 历史累计单独给一组字段，避免把两个口径混为一谈。
        "aux_history": aux_hist,
        "history_aux_runs": aux_hist["runs"],
        "history_aux_cost_cny": aux_hist["cost_cny"],
        "history_aux_tokens": aux_hist["total_tokens"],
        "history_aux_estimated": aux_hist["estimated"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    if unpriced:
        report["note"] = (f"以下模型{usage_log.UNPRICED_NOTE}，费用计 0："
                          f"{'、'.join(unpriced)}；可用 MODEL_PRICES 配置单价")
    return report
