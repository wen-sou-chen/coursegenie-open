# -*- coding: utf-8 -*-
"""任务 35 测试：讲义里的图引用（协议链接）不再以原文落到 PPT 页面上。

覆盖三块：

1. 解析 / 过滤纯函数 ``sanitize_text`` / ``protocol_refs``（含被 ``_clip`` 截断的引用）；
2. 取要点行 ``_point_lines`` / ``_lecture_digest`` 不会漏出协议原文；
3. 端到端：整章生成后页面文字里裸引用 = 0、超上限的图进备注与 ``meta.layout_notes``
   （``kind="figure-dropped"``）、能解析到图的真的嵌进页面、封面 / 回顾等页型也干净。
"""
from __future__ import annotations

import time

import pytest

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.course import ContentItem, OutlineNode
from app.services.ppt import (_figure_drop_notes, _lecture_digest, _order_by_refs,
                              _point_lines, _point_slides, _sanitize_specs, protocol_refs,
                              sanitize_text)

DIG = "![概念卡：幻觉定义](diagram://%s)"
CHT = "![数据卡：幻觉比例](chart://%s)"
PROTOCOL_MARKERS = ("diagram://", "chart://", "gen://")


# ---------------------------------------------------------------------------
# 夹具（沿用 test_ppt_v4 的口径）
# ---------------------------------------------------------------------------

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


def _add_point(course_id: str, chapter_id: str, *, key: str = "1.1",
               title: str = "幻觉与人工复核") -> dict:
    db = SessionLocal()
    try:
        chapter = db.get(OutlineNode, chapter_id)
        node = OutlineNode(outline_id=chapter.outline_id, node_key=key, node_type="point",
                           title=title, objective=f"理解{title}", order_index=1)
        db.add(node)
        db.commit()
        db.refresh(node)
        return {"id": node.id, "key": node.node_key}
    finally:
        db.close()


def _add_lecture(course_id: str, node_id: str, markdown: str, title: str = "幻觉与人工复核") -> str:
    return _add_item(course_id, node_id, "lecture", title, {"markdown": markdown, "tone": "正式"})


def _add_item(course_id: str, node_id: str, kind: str, title: str, body: dict) -> str:
    db = SessionLocal()
    try:
        row = ContentItem(course_id=course_id, node_id=node_id, kind=kind, title=title,
                          body=body, meta={})
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    finally:
        db.close()


def _diagram_body(text: str) -> dict:
    return {"excalidraw_json": {"type": "excalidraw", "elements": [
        {"type": "rectangle", "x": 20, "y": 40, "width": 220, "height": 110,
         "backgroundColor": "#ffe4e6", "strokeColor": "#f43f5e", "strokeWidth": 2.5},
        {"type": "text", "x": 40, "y": 80, "width": 180, "height": 40, "text": text,
         "fontSize": 20, "strokeColor": "#374151", "textAlign": "center"},
    ]}, "caption": text}


def _add_diagram(course_id: str, node_id: str, title: str) -> str:
    return _add_item(course_id, node_id, "diagram", title, _diagram_body(title[:6]))


def _add_chart(course_id: str, node_id: str, title: str) -> str:
    return _add_item(course_id, node_id, "chart_bar", title, {"data": {
        "labels": ["A", "B", "C"], "datasets": [{"label": "幻觉率", "data": [3, 5, 4]}]}})


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


def _notes_of(slides: list[dict], title: str) -> str:
    page = next((s for s in slides if s["title"] == title), None)
    assert page is not None, [s["title"] for s in slides]
    return str(page.get("notes_excerpt") or "")


def _pictures_of(pptx_path: str, title: str) -> int:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    for slide in Presentation(pptx_path).slides:
        name = (slide.shapes.title.text if slide.shapes.title is not None else "").strip()
        if name != title:
            continue
        return sum(1 for shape in slide.shapes if shape.shape_type == MSO_SHAPE_TYPE.PICTURE)
    raise AssertionError(f"课件里没有标题为「{title}」的页")


# ---------------------------------------------------------------------------
# ① 纯函数：协议引用必须从文字里消失，markdown 残渣顺带清掉
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (DIG % "6d689a1a2b3c4d5e", ""),
    ("要点回顾：" + (CHT % "0d0f87698062475f"), ""),
    ("![数据卡：水合率调整](chart://c9b93710f", ""),          # 被 _clip 截断、没有右括号
    ("见 gen://abc123 占位", "见 占位"),
    ("**加粗**要点：![图](diagram://deadbeef)其余文字", "**加粗**要点： 其余文字"),
    ("普通图片 ![示意图](figures/a.png) 后面", "普通图片 示意图 后面"),
    ("外链 [官方文档](https://example.com/x) 后面", "外链 官方文档 后面"),
    ("| 模型 | 参数 |", "模型 | 参数"),
    ("这是一条没有任何引用的正常要点。", "这是一条没有任何引用的正常要点。"),
    ("", ""),
])
def test_sanitize_text_removes_protocol_and_markdown_residue(raw, expected):
    assert sanitize_text(raw) == expected
    assert not any(marker in sanitize_text(raw) for marker in PROTOCOL_MARKERS)


def test_sanitize_text_keeps_bold_marks_for_ppt_text_layer():
    assert sanitize_text("**重点**：发酵三阶段") == "**重点**：发酵三阶段"


def test_protocol_refs_order_scheme_and_dedup():
    markdown = "\n".join([
        "# 标题",
        CHT % "bbbb2222",
        DIG % "aaaa1111",
        DIG % "aaaa1111",              # 重复引用只算一次
        "裸引用 gen://cccc3333",
    ])
    refs = protocol_refs(markdown)
    assert [r["ref"] for r in refs] == ["bbbb2222", "aaaa1111", "cccc3333"]
    assert [r["scheme"] for r in refs] == ["chart", "diagram", "gen"]
    assert refs[1]["alt"] == "概念卡：幻觉定义"


def test_protocol_refs_empty_for_plain_markdown():
    assert protocol_refs("## 小结\n- 纯文字要点\n") == []


# ---------------------------------------------------------------------------
# ② 取要点行：协议原文不进 bullets / 备注
# ---------------------------------------------------------------------------

SAMPLE_MARKDOWN = "\n".join([
    "# 1.1 AI 也会一本正经地胡说",
    "## 核心讲解",
    "- 幻觉指模型生成的看似合理实则错误的内容",
    "- " + (DIG % "aaaa1111"),
    "- " + (CHT % "bbbb2222"),
    "",
    "| 模型 | 幻觉率 |",
    "| --- | --- |",
    "",
    "## 小结",
    "- 要点回顾：" + (CHT % "dddd4444"),
    "- 人工复核仍是最后一道关",
])


def test_point_lines_and_digest_never_keep_protocol_refs():
    lines = _point_lines(SAMPLE_MARKDOWN)
    assert lines, lines
    assert all(not any(marker in line for marker in PROTOCOL_MARKERS) for line in lines), lines
    assert any("幻觉指模型生成" in line for line in lines), lines
    assert not any("要点回顾" in line for line in lines), lines  # 整行只有引用 → 丢弃

    heads, bullets, paragraphs = _lecture_digest(SAMPLE_MARKDOWN)
    text = "\n".join(heads + bullets + [paragraphs])
    assert not any(marker in text for marker in PROTOCOL_MARKERS), text
    assert all("|" not in head for head in heads), heads  # 表格行不进文字


# ---------------------------------------------------------------------------
# ③ 引用的图优先嵌入；超上限 / 找不到条目的写进备注与 layout_notes
# ---------------------------------------------------------------------------

def test_order_by_refs_puts_referenced_figures_first():
    db = SessionLocal()
    try:
        first = ContentItem(id="fig-1", kind="diagram", title="概念图A")
        second = ContentItem(id="fig-2", kind="chart_bar", title="数据图B")
        ordered, missing = _order_by_refs(
            db, "no-such-course", [first, second],
            "![概念卡：B](chart://fig-2) 然后 ![概念卡：A](diagram://fig-1)")
    finally:
        db.close()
    assert [f.id for f in ordered] == ["fig-2", "fig-1"]
    assert missing == []


def test_order_by_refs_reports_missing_entries_with_alt_text():
    db = SessionLocal()
    try:
        ordered, missing = _order_by_refs(db, "no-such-course", [],
                                          "![概念卡：被删掉的图](diagram://zzz999)")
    finally:
        db.close()
    assert ordered == []
    assert missing == ["概念卡：被删掉的图"]


def test_point_slides_moves_over_limit_figures_into_notes():
    node = OutlineNode(node_key="1.1", node_type="point", title="幻觉与人工复核")
    markdown = "## 核心讲解\n- 幻觉的定义\n- " + (DIG % "aaa") + "\n- " + (CHT % "bbb")
    items = [ContentItem(kind="lecture", title="幻觉", body={"markdown": markdown})]

    slides = _point_slides(node, items, media=[], slides_per_point=1,
                           overflow=["概念图-超出上限"], missing_refs=["概念卡：没有条目"])

    assert len(slides) == 1
    page = slides[0]
    assert all(not any(marker in b for marker in PROTOCOL_MARKERS) for b in page["bullets"])
    assert page["bullets"] == ["【核心讲解】幻觉的定义"], page["bullets"]
    assert "页数上限：limit=1 已排满" in page["notes"]
    assert "[图:概念图-超出上限]" in page["notes"]
    assert "概念卡：没有条目" in page["notes"]
    dropped = page["figure_dropped"]
    assert dropped["overflow"] == ["概念图-超出上限"]
    assert dropped["missing"] == ["概念卡：没有条目"]
    assert len(dropped["reasons"]) == 1, dropped
    assert "页数上限：limit=1" in dropped["reasons"][0]
    assert "共 1 张图" in dropped["reasons"][0]


def test_point_slides_without_drops_has_no_notes_noise():
    node = OutlineNode(node_key="1.2", node_type="point", title="复核")
    items = [ContentItem(kind="lecture", title="复核", body={"markdown": "## 核心讲解\n- 人工复核"})]
    slides = _point_slides(node, items, media=[], slides_per_point=1)
    assert slides[0]["figure_dropped"] == {}
    assert "原计划配图" not in slides[0]["notes"]


def test_figure_drop_notes_shape_matches_layout_notes_convention():
    reason = ("页数上限：limit=1 已排满（第 1.1 个知识点「幻觉与人工复核」共 2 张图，"
              "第 2 张 [图:概念图-3] 未上页），已挪到备注")
    slides = [
        {"layout": "point", "figure_dropped": {"overflow": ["概念图-3"], "missing": [],
                                               "reasons": [reason]}},
        {"layout": "cover", "figure_dropped": {}},
    ]
    notes = _figure_drop_notes(slides)
    assert len(notes) == 1, notes
    assert notes[0]["slide"] == 1 and notes[0]["layout"] == "point"
    assert notes[0]["kind"] == "figure-dropped"
    assert notes[0]["reason"] == reason
    assert "每页图上限" not in notes[0]["reason"]


def test_sanitize_specs_catches_refs_on_cover_and_review_layouts():
    specs = [
        {"layout": "cover", "title": "第 1 章 覆盖", "bullets": [DIG % "aaa", "正常一条"]},
        {"layout": "review", "title": "本章回顾", "bullets": [],
         "notes": "口播稿。" + (CHT % "bbb")},
    ]
    notes: list[dict] = []
    _sanitize_specs(specs, notes)

    assert specs[0]["bullets"] == ["正常一条"]
    assert not any(marker in specs[1]["notes"] for marker in PROTOCOL_MARKERS)
    assert [n["kind"] for n in notes] == ["figure-dropped", "figure-dropped"]
    assert [n["slide"] for n in notes] == [1, 2]


# ---------------------------------------------------------------------------
# ④ 端到端：整章生成 → 页面裸引用 0、超限图进备注、图真的嵌进页面
# ---------------------------------------------------------------------------

def test_chapter_deck_pages_have_no_protocol_refs_anywhere(client, course_id, monkeypatch):
    _plan_and_approve(client, course_id, num_chapters=1)
    chapter = _chapter(client, course_id)
    point = _add_point(course_id, chapter["id"])
    fig_a = _add_diagram(course_id, point["id"], "概念图-幻觉")
    fig_b = _add_diagram(course_id, point["id"], "概念图-复核")
    fig_c = _add_chart(course_id, point["id"], "数据图-幻觉比例")
    _add_lecture(course_id, point["id"], "\n".join([
        "# 幻觉与人工复核",
        "## 核心讲解",
        "- 幻觉指模型编造出看似合理的内容",
        f"- ![概念卡：幻觉](diagram://{fig_a})",
        f"- ![概念卡：复核](diagram://{fig_b})",
        f"- ![数据卡：幻觉比例](chart://{fig_c})",
        "## 小结",
        f"- 要点回顾：![概念卡：幻觉](diagram://{fig_a})",
        "- 人工复核是最后一道关",
    ]))
    monkeypatch.setattr(get_settings(), "ppt_slides_per_point", 1)   # 每页 2 张 → 第 3 张超限

    _gen(client, course_id, scope="chapter", chapter_node_id=chapter["id"])
    deck = _chapter_deck(client, course_id)

    # 页面文字（以磁盘 pptx 读回为准）里绝不允许再出现协议原文
    assert deck["meta"]["slides"], "应读到页级大纲"
    for page in deck["meta"]["slides"]:
        blob = "\n".join([str(page.get("title") or ""), *(page.get("bullets") or []),
                          str(page.get("notes_excerpt") or "")])
        assert not any(marker in blob for marker in PROTOCOL_MARKERS), page

    # 超上限的图：进备注 + layout_notes
    assert deck["meta"]["figure_count"] == 2, deck["meta"]["figure_failures"]
    point_notes = _notes_of(deck["meta"]["slides"], point_title(deck))
    assert "页数上限" in point_notes and "数据图-幻觉比例" in point_notes, point_notes
    dropped = [n for n in deck["meta"]["layout_notes"] if n.get("kind") == "figure-dropped"]
    assert dropped, deck["meta"]["layout_notes"]
    assert any("数据图-幻觉比例" in n["reason"] for n in dropped), dropped

    # 能解析到的图真的嵌进页面（引用被消化成图片，而不是黑字）
    assert _pictures_of(deck["body"]["pptx"], point_title(deck)) >= 1


def point_title(deck: dict) -> str:
    """该章知识点页标题（第 1 页之外的第一个非「续」页）。"""
    for page in deck["meta"]["slides"]:
        title = str(page.get("title") or "")
        if title and "续" not in title and title not in ("谢谢观看",):
            skip = ("本章回顾", "随堂练习", "课堂提问", "课程目标", "课程目录")
            if not any(word in title for word in skip) and page["index"] > 1:
                return title
    raise AssertionError([p["title"] for p in deck["meta"]["slides"]])