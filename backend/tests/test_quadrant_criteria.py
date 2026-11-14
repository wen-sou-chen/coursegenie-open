# -*- coding: utf-8 -*-
"""任务 127 ③：象限图判据落到代码里（并写进 ``meta.layout_notes``）。

复测（``验收-121-122-独立复测``）：象限图在本课**命中 0 页** —— 判据当时只是
``CARDS_SYSTEM`` / ``CHART_SYSTEM`` 里的人话，代码里没有任何内容规则；``meta.layout_notes``
里也**没有一条**记录「为什么是象限图」。这里钉死三件事：

1. 判据**确定、可测、无 LLM**（``lecture.quadrant_criteria``）：命中取舍类词，或出现
   ≥3 个不同的两维词且合计 ≥5 次；其余一律 ``None``（没命中就照旧不选）；
2. 命中后**真的出象限图**（生产链路 ``regenerate_cards_for_lecture``），并且理由
   （含命中的词）逐条写进 ``meta.layout_notes``；
3. 反证据：把判据打桩成永远 ``None`` ⇒ 象限图回 0。

硬边界：只测「选不选象限图」这一层；不动 ``MIN_TEXT_PT=9`` / ``MIN_MEDIA_WIDTH_IN=6.00`` /
``MAX_DECOR_PER_SLIDE=14``，也不动交付包结构与文件名口径。
"""
from __future__ import annotations

from app.db.session import SessionLocal
from app.models.course import ContentItem, Course
from app.models.course import OutlineNode
from app.services import lecture as lecture_svc
from app.services import ppt
from app.services.planner import list_nodes

TRADE_MD = """## 风险等级评估框架

### 概念引入
这一节要讲清风险等级评估框架，核心是**取舍**与优先级怎么定。

### 关键对照
- 高风险高收益：值得投入
- 低成本低风险：先做
"""

DIMS_MD = """## 风险等级评估框架

### 概念引入
风险要先分级，风险高的先处理，风险低的排队。

### 成本与产出
成本要算清楚，投入与产出要一起看，风险与成本不能分开谈。
"""

PLAIN_MD = """## 光合作用入门

### 概念引入
叶绿体吸收光能，把二氧化碳和水变成有机物。

### 关键步骤
光反应产氧气，暗反应固定二氧化碳，最后生成葡萄糖。
"""


def _lecture_item(db, course, node, markdown: str) -> ContentItem:
    """直接把讲义正文写进库里（不走 LLM：判据只看正文，不需要模型参与）。"""
    item = ContentItem(course_id=course.id, node_id=node.id, kind="lecture",
                       title=str(node.title or "讲义"), body={"markdown": markdown},
                       meta={"tone": "正式"})
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _plan_approve(client, course_id: str) -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "初中生物光合作用", "num_chapters": 2,
    })
    assert resp.status_code == 200, resp.text
    assert client.post("/api/plan/approve", json={
        "course_id": course_id, "approved": True}).status_code == 200
    return resp.json()


def _leaf(plan: dict) -> dict:
    return next((n for n in plan["nodes"] if "." in n["key"]), None) or plan["nodes"][0]


def _quadrant_items(db, course) -> list[ContentItem]:
    return db.query(ContentItem).filter(
        ContentItem.course_id == course.id,
        ContentItem.kind == "chart_quadrant").all()


# ---------------------------------------------------------------------------
# ① 判据本身：确定、可测、无 LLM
# ---------------------------------------------------------------------------

def test_trade_words_hit_the_criteria_and_name_themselves():
    """取舍类词命中 ⇒ 返回理由，且理由里**写出命中的词**（人工可核对）。"""
    crit = lecture_svc.quadrant_criteria(TRADE_MD)
    assert crit is not None
    assert "取舍" in crit["hits"]
    assert "取舍" in crit["reason"] and "象限图" in crit["reason"]


def test_several_distinct_dimension_words_hit_the_criteria():
    """≥3 个不同的两维词、合计 ≥5 次 ⇒ 命中，并把每个词的出现次数写进理由。"""
    crit = lecture_svc.quadrant_criteria(DIMS_MD)
    assert crit is not None
    assert len(crit["dims"]) >= lecture_svc.QUADRANT_MIN_DIMS
    assert sum(crit["dims"].values()) >= lecture_svc.QUADRANT_MIN_HITS
    for word in crit["dims"]:
        assert word in crit["reason"]


def test_plain_lecture_does_not_hit_the_criteria():
    """没命中就照旧不选（判据不是「凡讲义都要出象限图」）。"""
    assert lecture_svc.quadrant_criteria(PLAIN_MD) is None
    assert lecture_svc.quadrant_criteria("") is None


def test_repeating_one_dimension_word_is_not_a_trade_off():
    """同一个词反复出现不算「两维」：只 1 个维度词 ⇒ 不选（阈值就是为这条设的）。"""
    assert lecture_svc.quadrant_criteria("风险。" * 40) is None
    two = "风险与成本" * 10                     # 2 个不同维度词 < QUADRANT_MIN_DIMS
    assert lecture_svc.quadrant_criteria(two) is None


def test_citations_do_not_count_toward_the_criteria():
    """出处标注（「（来源:《…》第 N 段）」）里的词不算内容命中。"""
    body = "叶绿体吸收光能。\n> （来源:《风险与成本与价值手册》第 3 段）\n" * 6
    assert lecture_svc.quadrant_criteria(body) is None


# ---------------------------------------------------------------------------
# ② 生产链路：命中 ⇒ 出象限图 + 记 layout_notes + 上页
# ---------------------------------------------------------------------------

def test_criteria_turns_the_data_card_into_a_quadrant_with_a_note(client, course_id):
    """命中判据 ⇒ 数据卡改型为象限图，理由（含命中的词）进 ``meta.layout_notes``。"""
    plan = _plan_approve(client, course_id)
    node = _leaf(plan)
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        item = _lecture_item(db, course, db.get(OutlineNode, node["id"]), TRADE_MD)
        assert lecture_svc.regenerate_cards_for_lecture(db, item) >= 1
        quadrants = _quadrant_items(db, course)
        assert quadrants, "判据命中却没有象限图"
        card = quadrants[0]
        notes = (card.meta or {}).get("layout_notes") or []
        assert notes and notes[0]["kind"] == "figure-kind"
        assert "取舍" in notes[0]["reason"]
        assert len(card.body["data"]["quadrants"]) == 4
    finally:
        db.close()


def test_quadrant_figure_lands_on_a_slide_page(client, course_id):
    """出课件：象限图**真的上页**（改前命中 0 页，这里要求 ≥1 页）。"""
    plan = _plan_approve(client, course_id)
    node = _leaf(plan)
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        leaf = db.get(OutlineNode, node["id"])
        item = _lecture_item(db, course, leaf, TRADE_MD)
        lecture_svc.regenerate_cards_for_lecture(db, item)
        card = _quadrant_items(db, course)[0]
        label = ppt._figure_label(card)

        nodes = list_nodes(db, course.id)
        chapter = next(n for n in nodes if "." not in str(n.node_key or ""))
        slides, _meta = ppt.chapter_slides(db, course, chapter, nodes, theme="clean")
        hit = [s for s in slides
               if any(str(m.get("caption") or "") == label for m in (s.get("media") or []))]
        assert hit, f"象限图没上页（{label}）"
    finally:
        db.close()


def test_removing_the_criteria_puts_quadrant_back_to_zero(client, course_id, monkeypatch):
    """反证据：判据打桩成永远 ``None`` ⇒ 象限图回 0（说明命中确实来自这条判据）。"""
    plan = _plan_approve(client, course_id)
    node = _leaf(plan)
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        leaf = db.get(OutlineNode, node["id"])
        monkeypatch.setattr(lecture_svc, "quadrant_criteria", lambda text: None)
        item = _lecture_item(db, course, leaf, TRADE_MD)
        lecture_svc.regenerate_cards_for_lecture(db, item)
        assert _quadrant_items(db, course) == []
    finally:
        db.close()
