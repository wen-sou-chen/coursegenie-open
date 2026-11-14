"""任务 48x 契约测试：用量面板的形状 + 「数据分析」的内容条目口径。

两处缺陷都是「从来没工作过」，而当时的门禁拦不住，所以这里各钉一条硬断言：

① `GET /api/analytics/{id}/usage` 的 `aux_history` 一直是**汇总对象**
   （`{runs, llm_calls, total_tokens, cost_cny, by_purpose, estimated}`），
   前端 `types.ts` 却把它声明成数组、`UsagePanel` 直接 `.map()` ⇒ tsc / build 全绿，
   页签却一打开就 `data.aux_history.map is not a function`、整页白屏。
   旧测试（`test_usage_dedup.py`）只断言了存在与数值，**没断言形状** ⇒ 这里钉死形状。
② `analytics.course_stats()` 只统计 `kind == "text"`，而讲义管线写 `kind="lecture"`、
   `kind="text"` 已无调用方（`content.py` 的单篇生成器）⇒「数据分析」的内容条目 /
   节点覆盖度 / 总字数 从讲义时代起恒为 0，与同页「习题数 78」自相矛盾。
   这里用「1 节点 + 1 篇 lecture」的小课断言三项都不再为 0。
"""
from __future__ import annotations

import re

from app.db.session import SessionLocal
from app.models.course import ContentItem, Course, Outline, OutlineNode
from app.services.analytics import count_chars, count_words
from app.services import usage as usage_log

AUX_KEYS = {"runs", "llm_calls", "total_tokens", "cost_cny", "by_purpose", "estimated"}
AUX_ROW_KEYS = {"purpose", "kind", "title", "runs", "llm_calls", "total_tokens",
                "cost_cny", "first_at", "last_at"}
MARKDOWN = "## 光反应\n\n" + "叶绿体把光能转成化学能。" * 120


def _one_point_course(course_id: str) -> str:
    """直接建「1 个知识点」的大纲（不走 LLM），返回知识点 id。"""
    db = SessionLocal()
    try:
        outline = Outline(course_id=course_id, title="光合作用", goal="理解光合作用", content={})
        db.add(outline)
        db.commit()
        db.refresh(outline)
        point = OutlineNode(outline_id=outline.id, node_key="1.1", node_type="point",
                            title="光反应", objective="说出光反应的产物", order_index=0)
        db.add(point)
        db.commit()
        db.refresh(point)
        return point.id
    finally:
        db.close()


def _add_lecture(course_id: str, node_id: str, markdown: str = MARKDOWN) -> str:
    """插一条讲义内容条目（与讲义管线同形：kind="lecture" + body.markdown）。"""
    db = SessionLocal()
    try:
        row = ContentItem(course_id=course_id, node_id=node_id, kind="lecture", title="光反应",
                          body={"markdown": markdown, "summary": "摘要", "keywords": [],
                                "tone": "正式"}, meta={})
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    finally:
        db.close()


def test_aux_history_shape_is_summary_object(client, course_id):
    """aux_history 是汇总对象，by_purpose 才是数组（前端 .map 的前提）。"""
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        assert usage_log.record_aux(course, "outline", {
            "llm_calls": 1, "prompt_tokens": 1000, "completion_tokens": 500,
            "total_tokens": 1500, "cost_cny": 0.0035, "duration_ms": 1200,
            "updated_at": "2026-09-18T00:00:00+00:00",
        })
        db.commit()
    finally:
        db.close()

    usage = client.get(f"/api/analytics/{course_id}/usage").json()
    aux = usage["aux_history"]
    assert isinstance(aux, dict), f"aux_history 必须是汇总对象，实得 {type(aux).__name__}"
    assert AUX_KEYS <= set(aux), sorted(aux)
    assert isinstance(aux["by_purpose"], list), type(aux["by_purpose"]).__name__
    assert len(aux["by_purpose"]) == 1, aux["by_purpose"]
    row = aux["by_purpose"][0]
    assert AUX_ROW_KEYS <= set(row), sorted(row)
    assert row["purpose"] == "outline" and row["kind"] == "outline" and row["title"]
    assert aux["runs"] == 1 and aux["total_tokens"] == 1500 and aux["estimated"] is False
    # 页签上三个平铺的「历史累计」字段必须与对象同源（防两处口径漂移）
    assert usage["history_aux_runs"] == aux["runs"]
    assert usage["history_aux_tokens"] == aux["total_tokens"]
    assert usage["history_aux_cost_cny"] == aux["cost_cny"]


def test_course_stats_counts_lecture_content(client, course_id):
    """「数据分析」的内容条目 / 覆盖度 / 总字数不再恒为 0。"""
    node_id = _one_point_course(course_id)
    _add_lecture(course_id, node_id)

    stats = client.get(f"/api/analytics/{course_id}").json()
    assert stats["outline_nodes"] == 1, stats
    assert stats["content_items"] == 1, stats
    assert stats["node_coverage"] == 1.0, stats
    assert stats["words_total"] > 100, stats          # 基线（只认 text 时）恒为 0
    assert stats["quizzes"] == 0, stats

    # 再加一篇讲义 ⇒ 内容条目与字数一起增长（证明确实在遍历讲义正文，而不只是「非 0」）
    _add_lecture(course_id, node_id, MARKDOWN.replace("光反应", "暗反应"))
    after = client.get(f"/api/analytics/{course_id}").json()
    assert after["content_items"] == 2, after
    assert after["words_total"] > stats["words_total"], after


def test_course_stats_keeps_legacy_text_items(client, course_id):
    """老 kind="text" 条目仍照算（修 lecture 是「加上」而不是「换掉」）。"""
    node_id = _one_point_course(course_id)
    _add_lecture(course_id, node_id)
    db = SessionLocal()
    try:
        db.add(ContentItem(course_id=course_id, node_id=node_id, kind="text", title="老正文",
                           body={"markdown": "老正文" * 40}, meta={}))
        db.commit()
    finally:
        db.close()

    stats = client.get(f"/api/analytics/{course_id}").json()
    assert stats["content_items"] == 2, stats
    assert stats["node_coverage"] == 1.0, stats


def test_course_stats_ignores_other_kinds(client, course_id):
    """配图 / 课件 / 概念卡不算「内容条目」（口径不许放宽成「所有 kind」）。"""
    node_id = _one_point_course(course_id)
    db = SessionLocal()
    try:
        for kind in ("diagram", "ppt", "concept", "chart_bar", "chart_mindmap"):
            db.add(ContentItem(course_id=course_id, node_id=node_id, kind=kind, title=kind,
                               body={"markdown": "图" * 50}, meta={}))
        db.commit()
    finally:
        db.close()

    stats = client.get(f"/api/analytics/{course_id}").json()
    assert stats["content_items"] == 0, stats
    assert stats["words_total"] == 0, stats
    assert stats["node_coverage"] == 0, stats


def test_word_count_metric_matches_its_label():
    """任务 81 §A1：字数口径 = 汉字逐字 + 连续英文 / 数字串（标题必须与算法一致）。

    原先 `course_stats()` 用 `content.extract_words()`，而它是 `[\\u4e00-\\u9fff\\w]+` 的
    **正则词块数** —— 中文连成一片只算 1 个，面板却把结果叫「总字数」。
    """
    assert count_words("中文三字") == 4          # 汉字逐字算 1
    assert count_words("hello 世界 2026") == 4   # hello + 世界 + 2026
    assert count_words("") == 0 and count_words(None) == 0
    assert count_words("光合作用") == 4
    # 与旧口径的差别（这就是当初差 8 倍的来源）：中文连成一片，旧口径只算 1 个
    assert len(re.findall(r"[\u4e00-\u9fff\w]+", "光合作用")) == 1
    # 字符数：只去空白（含标点）
    assert count_chars("a b\nc 中") == 4
    assert count_chars("") == 0


def test_course_stats_words_and_chars_are_reconcilable(client, course_id):
    """面板两个数字可对账：字数 ≤ 字符数，且字符数 = 去空白后的正文长度（含标点）。"""
    node_id = _one_point_course(course_id)
    _add_lecture(course_id, node_id)

    stats = client.get(f"/api/analytics/{course_id}").json()
    assert 0 < stats["words_total"] <= stats["chars_total"], stats
    assert stats["chars_total"] == len(re.sub(r"\s+", "", MARKDOWN)), stats
