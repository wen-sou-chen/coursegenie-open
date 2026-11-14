# -*- coding: utf-8 -*-
"""任务 128：课件**逐页**记「为什么选了这个版式」（``kind="layout-choice"``）。

起因（``验收-127-独立复测``）：任务 127 的 ③ 只把「为什么是象限图」写在**图条目**的
``meta.layout_notes`` 上，**课件页**这一层查不到（复测口径：课件页 note 里「象限」出现 0 次）⇒
读者打开课件看不到「这页为什么是象限图 / 为什么是这两栏对照」。本文件钉住四件事：

1. 判据命中的页（象限图）在**课件页 note** 里出现「象限」+ 命中的词 + 规则名；
2. 没命中的页记「默认 + 原因」（默认知识点页也要说明为什么是默认）；
3. **每一页恰好一条** ``layout-choice``（逐页覆盖，按页可检索）；
4. 既有 note（point-variant / media-tier / figure-kind / quiz-pack / media-drop…）一条不少、
   不被覆盖 —— 选型理由只**追加**，职责不混。

反证据（本文件即证据）：把 ``_render_pptx`` 里那段 ``layout-choice`` 循环删掉 ⇒ 用例 1 / 3 / 5 立刻红；
把 ``slide_choice_of`` 换成不写「象限」的桩 ⇒ 用例 1 / 6 红（实测记录见
``backend/docs/task128-deck-layout-choice.md``）。

硬边界：**只记录已经做出的选择** —— 不选版式、不改判据、不动几何；不动 ``MIN_TEXT_PT=9`` /
``MIN_MEDIA_WIDTH_IN=6.00`` / ``MAX_DECOR_PER_SLIDE=14``。
"""
from __future__ import annotations

import time

from app.db.session import SessionLocal
from app.models.course import ContentItem, OutlineNode
from app.services import lecture as lecture_svc
from app.services import ppt

#: 命中象限图判据的讲义正文（取舍类词 + 两个 ``###`` 锚点 ⇒ mock 也会给一张数据卡）。
QUADRANT_MD = """## 风险等级评估框架

### 概念引入
这一节要讲清风险等级评估框架，核心是**取舍**与优先级怎么定。

### 关键对照
- 高风险高收益：值得投入
- 低成本低风险：先做
"""

#: 命中「步骤页」判据（≥2 条要点以序号开头），但**不**命中象限图判据。
STEPS_MD = """## 治理流程

### 概念引入
先说清治理流程要解决的问题。

### 三个环节
- 1. 先核对来源
- 2. 再补引用
"""

#: 两条判据都不命中 ⇒ 默认知识点页。
PLAIN_MD = """## 光合作用

### 概念引入
叶绿体吸收光能，把二氧化碳和水变成有机物。

### 关键步骤
光反应产氧气，暗反应固定二氧化碳。
"""


def _plan_and_approve(client, course_id: str, num_chapters: int = 1) -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "让初中生理解光合作用", "num_chapters": num_chapters,
    })
    assert resp.status_code == 200, resp.text
    assert client.post("/api/plan/approve",
                       json={"course_id": course_id, "approved": True}).status_code == 200
    return client.get(f"/api/plan/{course_id}").json()


def _chapter(client, course_id: str) -> dict:
    nodes = client.get(f"/api/plan/{course_id}").json()["nodes"]
    return ([n for n in nodes if n["type"] in ("chapter", "section")] or nodes)[0]


def _add_point(course_id: str, chapter_id: str, markdown: str, *,
               key: str = "1.1", title: str = "知识点") -> str:
    """落一个知识点 + 一篇讲义（不调 LLM），返回节点 id。"""
    db = SessionLocal()
    try:
        chapter = db.get(OutlineNode, chapter_id)
        node = OutlineNode(outline_id=chapter.outline_id, node_key=key, node_type="point",
                           title=title, objective=f"理解{title}", order_index=1)
        db.add(node)
        db.commit()
        db.refresh(node)
        db.add(ContentItem(course_id=course_id, node_id=node.id, kind="lecture", title=title,
                           body={"markdown": markdown, "tone": "正式"}, meta={}))
        db.commit()
        return node.id
    finally:
        db.close()


def _rebuild_cards(node_id: str) -> None:
    """走生产链路重建概念卡（判据命中 ⇒ 数据卡改型为象限图）。"""
    db = SessionLocal()
    try:
        item = (db.query(ContentItem)
                .filter(ContentItem.node_id == node_id, ContentItem.kind == "lecture").one())
        lecture_svc.regenerate_cards_for_lecture(db, item)
    finally:
        db.close()


def _wait(client, job_id: str, timeout: float = 60.0) -> dict:
    state: dict | None = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = client.get(f"/api/ppt/jobs/{job_id}").json()
        if state["status"] in ("success", "failed"):
            return state
        time.sleep(0.1)
    raise AssertionError(f"任务超时未结束：{state}")


def _gen(client, course_id: str, **payload) -> dict:
    resp = client.post("/api/ppt/generate", json={"course_id": course_id, **payload})
    assert resp.status_code == 200, resp.text
    state = _wait(client, resp.json()["job_id"])
    assert state["status"] == "success", state
    return state


def _chapter_deck(client, course_id: str) -> dict:
    decks = client.get(f"/api/ppt/{course_id}").json()["chapters"]
    assert decks, "应已生成章节课件"
    return decks[0]


def _notes(deck: dict) -> list[dict]:
    return [n for n in (deck["meta"].get("layout_notes") or []) if isinstance(n, dict)]


def _choices(deck: dict) -> list[dict]:
    return [n for n in _notes(deck) if n.get("kind") == "layout-choice"]


def _deck(client, course_id: str, markdown: str, *, title: str = "知识点") -> dict:
    """落讲义 → 重建概念卡 → 出章节课件（一条链走完，返回课件行）。"""
    plan = _plan_and_approve(client, course_id)
    chapter = _chapter(client, course_id)
    node_id = _add_point(course_id, chapter["id"], markdown, key="1.1", title=title)
    _rebuild_cards(node_id)
    _gen(client, course_id, scope="chapter", chapter_node_id=chapter["id"])
    return _chapter_deck(client, course_id)


# ---------------------------------------------------------------------------
# ① 命中：课件页 note 里要能查到「象限图 + 命中的词 + 规则名」
# ---------------------------------------------------------------------------

def test_quadrant_page_note_names_the_rule_and_the_hit_words(client, course_id):
    """命中判据的那一页：课件页 note 里有「象限」、规则名与命中的词（复测口径）。"""
    deck = _deck(client, course_id, QUADRANT_MD, title="风险等级评估框架")
    hits = [n for n in _choices(deck) if n.get("rule") == "quadrant"]
    assert hits, [n for n in _choices(deck)][:3]
    reason = str(hits[0]["reason"])
    assert "象限" in reason, reason                      # 复测就是按这个口径判的
    assert "取舍" in reason, reason                       # 命中的词
    assert reason.startswith(ppt.LAYOUT_CHOICE_RULES["quadrant"]), reason   # 规则名
    # 课件页 note 与图条目 note 各司其职：图条目那条判据原文仍在（没被搬走 / 覆盖）
    db = SessionLocal()
    try:
        card = (db.query(ContentItem)
                .filter(ContentItem.course_id == course_id, ContentItem.kind == "chart_quadrant")
                .one())
        assert (card.meta or {})["layout_notes"][0]["kind"] == "figure-kind"
    finally:
        db.close()


def test_non_hit_page_records_the_default_and_the_reason(client, course_id):
    """没命中判据的页记「默认 + 原因」，不是只写一句「默认版式」。"""
    deck = _deck(client, course_id, PLAIN_MD, title="光合作用")
    defaults = [n for n in _choices(deck) if n.get("rule") == "point-default"]
    assert defaults, _choices(deck)
    reason = str(defaults[0]["reason"])
    assert reason.startswith(ppt.LAYOUT_CHOICE_RULES["point-default"]), reason
    assert "默认知识点页" in reason, reason
    assert "条要点" in reason or "百分比" in reason, reason      # 说清为什么是默认
    assert "象限" not in reason, reason                          # 没命中就不许乱说


def test_point_variant_choice_reuses_the_criteria_text(client, course_id):
    """判据选中的变体页：选型理由与既有 kind="point-variant" 的文案同源（不另编一套口径）。"""
    deck = _deck(client, course_id, STEPS_MD, title="治理流程")
    variants = [n for n in _notes(deck) if n.get("kind") == "point-variant"]
    assert variants, _notes(deck)
    chosen = {n["slide"] for n in _choices(deck) if n.get("rule") == "point-variant"}
    assert variants[0]["slide"] in chosen, (variants[0], chosen)
    same_page = next(n for n in _choices(deck) if n["slide"] == variants[0]["slide"])
    assert str(variants[0]["reason"]) in str(same_page["reason"])


# ---------------------------------------------------------------------------
# ② 逐页覆盖 + 既有 note 不被覆盖
# ---------------------------------------------------------------------------

def test_every_page_gets_exactly_one_layout_choice_note(client, course_id):
    """**逐页**一条：layout-choice 条数 == 课件页数（按页可检索，复测就是按页查的）。"""
    deck = _deck(client, course_id, QUADRANT_MD, title="风险等级评估框架")
    pages = len(deck["meta"]["layouts"])
    choices = _choices(deck)
    assert pages > 1
    assert len(choices) == pages, (len(choices), pages)
    assert sorted(n["slide"] for n in choices) == list(range(1, pages + 1))
    assert all(str(n.get("reason") or "").strip() for n in choices)     # 一句空话都不许有
    assert all(str(n.get("rule") or "").strip() for n in choices)


def test_existing_notes_are_not_overwritten_by_the_choice_note(client, course_id):
    """选型理由只**追加**：既有各 kind 的 note 一条不少，页数与文案口径也没被改。"""
    deck = _deck(client, course_id, STEPS_MD, title="治理流程")
    kinds = {str(n.get("kind")) for n in _notes(deck)}
    assert "layout-choice" in kinds
    assert {"point-variant", "media-split", "media-tier"} & kinds, kinds
    assert ppt.LAYOUT_CHOICE_KIND not in {"media-split", "media-tier", "point-variant",
                                          "figure-kind", "quiz-pack", "figure-dropped"}
    # 同一页上既有 note 与选型 note 并存（不互相顶替）
    per_page: dict[int, set[str]] = {}
    for note in _notes(deck):
        per_page.setdefault(int(note.get("slide") or 0), set()).add(str(note.get("kind")))
    assert any(len(ks) > 1 for ks in per_page.values()), per_page


# ---------------------------------------------------------------------------
# ③ 反证据：不写选型理由 ⇒ 课件页就查不到「象限」
# ---------------------------------------------------------------------------

def test_removing_the_choice_writer_drops_the_quadrant_reason(client, course_id, monkeypatch):
    """把选型理由改成不写（桩掉 ``slide_choice_of``）⇒ 课件页 note 里「象限」回 0。"""
    plan = _plan_and_approve(client, course_id)
    chapter = _chapter(client, course_id)
    node_id = _add_point(course_id, chapter["id"], QUADRANT_MD, key="1.1", title="风险等级评估框架")
    _rebuild_cards(node_id)
    monkeypatch.setattr(ppt, "slide_choice_of",
                        lambda spec: {"rule": "off", "reason": "（本次不写选型理由）"})
    _gen(client, course_id, scope="chapter", chapter_node_id=chapter["id"])
    deck = _chapter_deck(client, course_id)
    assert [n for n in _choices(deck) if "象限" in str(n.get("reason") or "")] == []
    assert _choices(deck), "页面还在记 note，只是不再写象限选型理由"
