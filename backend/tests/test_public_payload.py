"""任务 89：公开侧载荷收窄（`meta` 白名单投影 + 学员版不带历史版本）—— **0 LLM**。

背景（DSH 走查第 7 步实测）：分享页把内部字段原样吐给客户/学生 —— 真库一门课
`cost_cny` 27 行 / `usage` 27 行 / `history` 27 行（每篇 5 个历史版本）/ `input_fingerprint`
27 行 / `*_ids` 各 27 行 / `qc` 27 行；`?view=student` 只剥了正文，历史版本里的
`**答案**` / `**解析**` / `✓` 原样发出（真库两门课合计 303 / 346 / 102 处）。

四条口径（只收窄公开侧）：

1. **白名单而不是黑名单**：`share.public_meta` 是唯一出处；`meta` 以后长出新内部键
   （本文件用 `future_internal_key` 模拟）时**默认不外泄**；
2. **学员版不带泄题内容**：历史版本整块不进载荷；引用表里被当成「来源」记下来的
   随堂练习解析行也整条去掉（真库实测 49 处）；
3. **内部侧一个字段都不许少**：`/api/content/{id}` 里 `usage` / `input_fingerprint` /
   `history` 照旧（公开侧收窄**不许**连带砍内部侧）；
4. **课件公开视图不退化**：`public_deck` 的 `scope` / `chapter_key` / `engine` /
   `slide_count` / `generated_at` 都还在（它们也走同一个白名单）。
"""
from __future__ import annotations

import json

from app.db.session import SessionLocal
from app.models.course import ContentItem, Outline, OutlineNode, QuizItem
from app.services.share import PUBLIC_META_KEYS, public_meta

#: 真库里真实存在的内部键（值取自任务 89 的扫描原文）。
INTERNAL_META = {
    "usage": {"llm_calls": 3, "prompt_tokens": 13801, "completion_tokens": 1648,
              "total_tokens": 15449, "cost_cny": 0.032546,
              "models": ["deepseek/deepseek-chat"]},
    "input_fingerprint": "1e0069315f7b55ad",
    "input_fingerprint_at": "2026-09-17T06:44:02.326752+00:00",
    "history": [{"body": {"markdown": "**答案**：酵母菌\n**解析**：无氧呼吸\n✓ 乙\n"}}],
    "locked": True,
    "locked_reason": "样板节定稿",
    "quiz_ids": ["q1"],
    "diagram_ids": ["d1"],
    "chart_ids": ["c1"],
    "image_ids": ["i1"],
    "structured_block": {"points": []},
    "content_issues": ["表头缺单位"],
    "diagram_failures": ["节点重叠"],
    "style_anchor_ref": "1.1",
    "quiz_issues": ["答案与选项长度不匹配"],
    "qc": {"score": 88, "risks": ["坏链 1 条"]},
    "llm_used": True,
    # 以后新长出来的内部键（白名单必须挡住，这就是「白名单而非黑名单」的判据）
    "future_internal_key": "2027 年新加的统计",
}

#: 允许出门的内容属性（§A1 的清单）+ 课件版面属性。
ALLOWED_META = {
    "node_key": "1.1",
    "sources": [{"section": "核心讲解", "quote": "发酵靠的是微生物", "paragraph": 33}],
    "caption": "图 1 发酵曲线",
    "role": "body",
    "from_lecture": True,
    "generated_at": "2026-09-17T06:44:02.326752+00:00",
    "minutes": 6,
    "layout": "two-column",
    "chart_type": "bar",
    "concept_id": "c-1",
    "aliases": ["发酵"],
    "chapters": [1],
    "prompt_version": "lecture-v14",
}

#: 公开载荷里**一个字符都不许出现**的字样（探针 P1 的 BAD_TEXT 同一套）。
FORBIDDEN_TEXT = ("cost_cny", "llm_calls", "prompt_tokens", "completion_tokens",
                  "total_tokens", "input_fingerprint", "deepseek/")

LEAK_MARKS = ("**答案**", "**解析**", "**评分要点**", "✓")

TEACHER_MARKDOWN = (
    "# 一、发酵\n\n发酵靠的是微生物。\n\n"
    "## 随堂练习\n\n#### 第 1 题 · 选择题\n\n- 甲\n- ✓ 乙\n\n**答案**：乙\n\n**解析**：酵母菌无氧呼吸。\n"
)


def _make_course_with_internal_meta(client) -> str:
    """建课 + 一章一点 + 一篇带满内部键的讲义 + 一道习题 + 一个课件（都在 meta 里塞内部键）。"""
    course_id = client.post("/api/course/", json={
        "title": "公开侧载荷测试课", "subject": "生物", "audience": "初中生",
        "duration_minutes": 90}).json()["id"]
    plan = client.post("/api/plan/generate", json={
        "course_id": course_id, "audience": "初中生", "goal": "理解发酵",
        "num_chapters": 1, "style": "标准"})
    assert plan.status_code == 200, plan.text
    assert client.post("/api/plan/approve", json={
        "course_id": course_id, "approved": True}).status_code == 200
    db = SessionLocal()
    try:
        outline = db.query(Outline).filter(Outline.course_id == course_id).first()
        node = OutlineNode(outline_id=outline.id, node_key="1.1", node_type="point",
                           title="发酵原理", objective="理解发酵", order_index=1)
        db.add(node)
        db.commit()
        db.refresh(node)
        db.add(ContentItem(
            course_id=course_id, node_id=node.id, kind="lecture", title="发酵原理",
            body={"markdown": TEACHER_MARKDOWN, "tone": "正式"},
            meta={**ALLOWED_META, **INTERNAL_META}))
        db.add(QuizItem(course_id=course_id, node_id=node.id, qtype="choice",
                        difficulty="easy", question="发酵的主角是谁？",
                        options=["乳酸菌", "酵母菌"], answer="1",
                        explanation="无氧呼吸。"))
        db.add(ContentItem(
            course_id=course_id, node_id=None, kind="ppt", title="整课课件",
            body={"pptx": "x.pptx", "engine": "python-pptx"},
            meta={"scope": "course", "chapter_key": None, "chapter_node_id": None,
                  "engine": "python-pptx", "slide_count": 7,
                  "generated_at": "2026-09-17T07:20:00+00:00",
                  "slides": [{"index": 1, "title": "封面", "bullets": ["a"]}],
                  **INTERNAL_META}))
        db.commit()
    finally:
        db.close()
    return course_id


def _token(client, course_id: str) -> str:
    resp = client.post(f"/api/share/course/{course_id}",
                       json={"label": "载荷测试", "base_url": "http://localhost:5173"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


# --------------------------------------------------- A. 纯函数：白名单是唯一出处

def test_public_meta_keeps_allowed_keys_and_drops_everything_else():
    assert public_meta({**ALLOWED_META, **INTERNAL_META}) == ALLOWED_META


def test_public_meta_drops_unknown_keys_by_default():
    """白名单的判据：没列进去的键（含以后新长的）一律不外泄。"""
    assert public_meta({"future_internal_key": 1, "brand_new_stats": 2}) == {}


def test_public_meta_tolerates_dirty_input():
    """脏数据（None / 非 dict）宁可不发，绝不原样透传。"""
    for bad in (None, [], "", 3, object()):
        assert public_meta(bad) == {}


def test_public_meta_whitelist_has_no_internal_key():
    """白名单本身不许混进内部键（`lock` 的反面：防止有人往清单里塞 `usage`）。"""
    assert not (set(PUBLIC_META_KEYS) & set(INTERNAL_META))
    for key in PUBLIC_META_KEYS:
        for needle in ("usage", "cost", "token", "fingerprint", "history", "locked",
                       "structured_block", "content_issues", "qc", "llm_used"):
            assert needle not in key, f"白名单里混进了内部键 {key}"


def test_public_meta_passes_values_through_unchanged():
    """只做投影、不改值（引用表 / 时间戳原样过去）。"""
    meta = {"sources": ALLOWED_META["sources"], "minutes": 6}
    assert public_meta(meta)["sources"] is meta["sources"]
    assert public_meta(meta)["minutes"] == 6


# --------------------------------------------------- B. 公开侧 /content（教师版）

def test_public_content_meta_is_whitelisted(client):
    course_id = _make_course_with_internal_meta(client)
    token = _token(client, course_id)
    rows = client.get(f"/api/public/{token}/content").json()
    assert rows, "公开侧应当能读到内容"
    assert any(row["kind"] == "lecture" for row in rows)
    for row in rows:
        extra = set(row.get("meta") or {}) - set(PUBLIC_META_KEYS)
        assert not extra, f"{row['kind']} 行 meta 里有白名单外的键：{sorted(extra)}"
    blob = json.dumps(rows, ensure_ascii=False)
    for needle in FORBIDDEN_TEXT:
        assert needle not in blob, f"公开载荷里还有 {needle}"
    for key in ("usage", "input_fingerprint", "history", "locked", "locked_reason",
                "quiz_ids", "diagram_ids", "chart_ids", "image_ids", "structured_block",
                "content_issues", "diagram_failures", "style_anchor_ref", "quiz_issues", "qc"):
        assert f'"{key}"' not in blob, f"公开载荷里还有内部键 {key}"


def test_public_content_keeps_content_attributes(client):
    """白名单不等于砍光：内容属性（本节标记 / 引用 / 生成时间 / 口径标记）照旧在。"""
    course_id = _make_course_with_internal_meta(client)
    token = _token(client, course_id)
    rows = client.get(f"/api/public/{token}/content").json()
    lecture = next(row for row in rows if row["kind"] == "lecture")
    assert lecture["meta"]["node_key"] == "1.1"
    assert lecture["meta"]["sources"] == ALLOWED_META["sources"]
    assert lecture["meta"]["prompt_version"] == "lecture-v14"
    assert lecture["meta"]["minutes"] == 6
    assert lecture["title"] == "发酵原理"
    assert lecture["body"]["tone"] == "正式"


def test_public_overview_and_plan_stay_clean(client):
    """§A3：四个公开接口同一口径 —— overview / plan / ppt 同样不带敏感字样。"""
    course_id = _make_course_with_internal_meta(client)
    token = _token(client, course_id)
    for path in (f"/api/public/{token}", f"/api/public/{token}/plan", f"/api/public/{token}/ppt"):
        resp = client.get(path)
        assert resp.status_code == 200, resp.text
        blob = json.dumps(resp.json(), ensure_ascii=False)
        for needle in FORBIDDEN_TEXT + ('"usage"', '"history"', '"input_fingerprint"'):
            assert needle not in blob, f"{path} 里还有 {needle}"


def test_public_deck_keeps_its_layout_fields(client):
    """`public_deck` 的 meta 也走白名单：版面字段还在（不退化），`usage` 没了。"""
    course_id = _make_course_with_internal_meta(client)
    token = _token(client, course_id)
    payload = client.get(f"/api/public/{token}/ppt").json()
    deck = payload["course"][0]
    assert deck["scope"] == "course"
    assert deck["engine"] == "python-pptx"
    assert deck["slide_count"] == 7
    assert deck["generated_at"] == "2026-09-17T07:20:00+00:00"
    assert deck["slides"] and deck["slides"][0]["title"] == "封面"


# --------------------------------------------------- C. 学员版（不许带答案）

def test_teacher_view_is_untouched(client):
    """默认 = 现状：教师版仍带答案与解析（否则下面的「学员版 0 命中」是空的）。"""
    course_id = _make_course_with_internal_meta(client)
    token = _token(client, course_id)
    blob = json.dumps(client.get(f"/api/public/{token}/content").json(), ensure_ascii=False)
    assert "**答案**" in blob and "**解析**" in blob and "✓" in blob


def test_student_view_has_no_history_and_no_leak_marks(client):
    course_id = _make_course_with_internal_meta(client)
    token = _token(client, course_id)
    rows = client.get(f"/api/public/{token}/content?view=student").json()
    blob = json.dumps(rows, ensure_ascii=False)
    for mark in LEAK_MARKS:
        assert mark not in blob, f"学员版载荷里还有 {mark}"
    for row in rows:
        assert "history" not in (row.get("meta") or {})
    # 题面与选项保留（学生要能答题）
    assert "发酵的主角是谁？" in blob
    assert "酵母菌" in blob


def test_student_view_drops_leaky_source_quotes_only(client):
    """引用表：整行是「答案 / 解析」的条目整条去掉，正常引文原样保留。"""
    course_id = _make_course_with_internal_meta(client)
    db = SessionLocal()
    try:
        item = (db.query(ContentItem)
                .filter(ContentItem.course_id == course_id, ContentItem.kind == "lecture")
                .first())
        meta = dict(item.meta)
        meta["sources"] = [
            {"section": "核心讲解", "quote": "发酵靠的是微生物", "paragraph": 33},
            {"section": "第 1 题 · 选择题", "quote": "**解析**：酵母菌无氧呼吸", "paragraph": 40},
        ]
        item.meta = meta
        db.commit()
    finally:
        db.close()
    token = _token(client, course_id)
    student = client.get(f"/api/public/{token}/content?view=student").json()
    lecture = next(row for row in student if row["kind"] == "lecture")
    assert lecture["meta"]["sources"] == [
        {"section": "核心讲解", "quote": "发酵靠的是微生物", "paragraph": 33}]
    teacher = client.get(f"/api/public/{token}/content").json()
    assert len(next(r for r in teacher if r["kind"] == "lecture")["meta"]["sources"]) == 2


# --------------------------------------------------- D. 内部侧不许被砍

def test_internal_content_keeps_every_internal_field(client):
    """公开侧收窄**不许**连带砍内部侧：内部接口里这些字段照旧存在。"""
    course_id = _make_course_with_internal_meta(client)
    resp = client.get(f"/api/content/{course_id}")
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    lecture = next(row for row in rows if row["kind"] == "lecture")
    meta = lecture["meta"]
    for key in ("usage", "input_fingerprint", "input_fingerprint_at", "history", "locked",
                "locked_reason", "quiz_ids", "diagram_ids", "chart_ids", "image_ids",
                "structured_block", "content_issues", "diagram_failures",
                "style_anchor_ref", "quiz_issues", "qc", "llm_used", "future_internal_key"):
        assert key in meta, f"内部侧少了 {key}"
    assert meta["usage"]["cost_cny"] == 0.032546
    assert meta["history"]


def test_internal_course_detail_and_share_admin_still_work(client):
    """内部侧的两个入口照旧（收窄只发生在 `public_*` 这一组）。"""
    course_id = _make_course_with_internal_meta(client)
    detail = client.get(f"/api/course/{course_id}")
    assert detail.status_code == 200, detail.text
    listing = client.get(f"/api/share/course/{course_id}")
    assert listing.status_code == 200, listing.text
