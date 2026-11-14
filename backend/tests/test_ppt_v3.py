# -*- coding: utf-8 -*-
"""M4-PPT v3 测试：概念图入页、原生数据图表、版式与页数可配、失败显式、幂等。"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.course import ContentItem, OutlineNode
from app.services import figure_png, ppt
from app.services.ppt import FIGURES_SUBDIR, deck_work_dir

POINT_TITLE = "发酵原理与微生物基础"


# ---------------------------------------------------------------------------
# 夹具
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
               title: str = POINT_TITLE, markdown: str = "") -> dict:
    """直接落一个知识点（mock 规划器只产章节点），并可选塞一篇讲义。"""
    db = SessionLocal()
    try:
        chapter = db.get(OutlineNode, chapter_id)
        node = OutlineNode(outline_id=chapter.outline_id, node_key=key, node_type="point",
                           title=title, objective=f"理解{title}", order_index=1)
        db.add(node)
        db.commit()
        db.refresh(node)
        if markdown:
            db.add(ContentItem(course_id=course_id, node_id=node.id, kind="lecture", title=title,
                               body={"markdown": markdown, "tone": "正式"}, meta={}))
            db.commit()
        return {"id": node.id, "key": node.node_key}
    finally:
        db.close()


def _add_item(course_id: str, node_id: str, kind: str, title: str, body: dict,
              meta: dict | None = None) -> str:
    db = SessionLocal()
    try:
        row = ContentItem(course_id=course_id, node_id=node_id, kind=kind, title=title,
                          body=body, meta=meta or {})
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    finally:
        db.close()


def _diagram_elements(text: str = "发酵原理") -> list[dict]:
    return [
        {"type": "rectangle", "x": 20, "y": 40, "width": 220, "height": 110,
         "backgroundColor": "#ffe4e6", "strokeColor": "#f43f5e", "strokeWidth": 2.5},
        {"type": "text", "x": 40, "y": 80, "width": 180, "height": 40, "text": text,
         "fontSize": 20, "strokeColor": "#374151", "textAlign": "center"},
        {"type": "ellipse", "x": 300, "y": 40, "width": 200, "height": 110,
         "backgroundColor": "#e0f2fe", "strokeColor": "#0ea5e9", "strokeWidth": 2.5},
        {"type": "arrow", "x": 240, "y": 95, "width": 60, "height": 0,
         "points": [[0, 0], [60, 0]], "strokeColor": "#94a3b8", "endArrowhead": "arrow"},
    ]


def _add_diagram(course_id: str, node_id: str, title: str) -> str:
    return _add_item(course_id, node_id, "diagram", title, {
        "excalidraw_json": {"type": "excalidraw", "elements": _diagram_elements(title[:6])},
        "caption": title,
    }, {"role": "concept"})


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


def _slide_shapes(pptx_path: str, title: str) -> dict:
    """按标题取某一页的形状统计：图片数 / 原生图表类型 / 原生表格数。"""
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    prs = Presentation(pptx_path)
    for slide in prs.slides:
        name = (slide.shapes.title.text if slide.shapes.title is not None else "").strip()
        if name != title:
            continue
        out = {"pictures": 0, "charts": [], "tables": 0}
        for shape in slide.shapes:
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                out["pictures"] += 1
            elif getattr(shape, "has_chart", False):
                out["charts"].append(str(shape.chart.chart_type))
            elif getattr(shape, "has_table", False):
                out["tables"] += 1
        return out
    raise AssertionError(f"课件里没有标题为「{title}」的页")


# ---------------------------------------------------------------------------
# ① 每页图数与该节点图数一致（超限取前 N 并在备注列出其余）
# ---------------------------------------------------------------------------

def test_point_page_media_count_matches_and_overflow_listed_in_notes(client, course_id, monkeypatch):
    plan = _plan_and_approve(client, course_id, num_chapters=1)
    chapter = _chapter(client, course_id)
    point = _add_point(course_id, chapter["id"])
    titles = [f"概念图-{i}" for i in range(1, 6)]          # 5 张图；位图一页最多 1 张
    for title in titles:
        _add_diagram(course_id, point["id"], title)
    monkeypatch.setattr(get_settings(), "ppt_slides_per_point", 1)

    _gen(client, course_id, scope="chapter", chapter_node_id=chapter["id"])
    deck = _chapter_deck(client, course_id)

    assert deck["meta"]["figure_count"] == 2, deck["meta"]["figure_failures"]
    assert deck["figure_failures"] == []
    shapes = _slide_shapes(deck["body"]["pptx"], POINT_TITLE)
    # 任务 146：位图一页一张（两张同页时每张只剩 < 6 英寸），本页只放第 1 张。
    assert shapes["pictures"] == 1, shapes
    page = next(s for s in deck["slides"] if s["title"] == POINT_TITLE)
    # 任务 146b A4：丢图原因必须是真话（limit 已排满 + 第几张 + 共几张），
    # 不再写「因每页图上限 2 张」。
    assert "页数上限：limit=1 已排满" in page["notes_excerpt"]
    assert "第 2 张" in page["notes_excerpt"]
    assert "共 5 张图" in page["notes_excerpt"]
    # notes_excerpt 只有 300 字，逐图原因会超出预览截断；逐张可核的原文在 layout_notes。
    dropped = [n for n in deck["meta"]["layout_notes"] if n.get("kind") == "figure-dropped"]
    blob = "\n".join(str(n.get("reason") or "") for n in dropped)
    for title in titles[1:]:
        assert f"[图:{title}]" in blob, blob
    assert plan["nodes"], "大纲应存在"


# ---------------------------------------------------------------------------
# ② PNG 能被 Pillow 打开且非空白
# ---------------------------------------------------------------------------

def test_rendered_figure_png_is_readable_and_not_blank(client, course_id):
    from PIL import Image

    _plan_and_approve(client, course_id, num_chapters=1)
    chapter = _chapter(client, course_id)
    point = _add_point(course_id, chapter["id"])
    _add_diagram(course_id, point["id"], "概念图-空白检查")

    _gen(client, course_id, scope="chapter", chapter_node_id=chapter["id"])
    deck = _chapter_deck(client, course_id)
    assert _slide_shapes(deck["body"]["pptx"], POINT_TITLE)["pictures"] == 1

    figures_dir = deck_work_dir(course_id, "chapter", chapter["key"]) / FIGURES_SUBDIR
    files = sorted(figures_dir.glob("fig-*.png"))
    assert len(files) == 1, files
    with Image.open(files[0]) as image:
        assert image.width >= figure_png.MIN_WIDTH, image.size
        colors = image.getcolors(maxcolors=100000) or []
    assert len(colors) > 1, "渲染出的图不应是空白画布"
    assert figure_png.find_font_path(), "本机应探测到中文字体"


# ---------------------------------------------------------------------------
# ③ bar/pie/line 是原生图表，table 是原生表格
# ---------------------------------------------------------------------------

def test_data_charts_are_native_pptx_objects(client, course_id):
    _plan_and_approve(client, course_id, num_chapters=1)
    chapter = _chapter(client, course_id)
    # 任务 146：4 张图分 2 页，每页至少 1 条要点（否则第 2 页就是无内容纯图页）。
    markdown = ("## 速率对比\n"
                "- 三种图表分别展示柱状、折线与饼图口径，便于对照趋势与占比。\n"
                "- 表格用于并排核对同一组数据，并保留原始数值方便课后逐项复核。")
    point = _add_point(course_id, chapter["id"], markdown=markdown)
    series = {"labels": ["A", "B", "C"], "datasets": [{"label": "速率", "data": [3, 5, 4]}]}
    for kind in ("chart_bar", "chart_line", "chart_pie"):
        _add_item(course_id, point["id"], kind, f"{kind}-速率对比", {"data": series})
    _add_item(course_id, point["id"], "chart_table", "表格-对比",
              {"data": {"headers": ["项", "值"], "rows": [["温度", "30"]]}})

    _gen(client, course_id, scope="chapter", chapter_node_id=chapter["id"])
    deck = _chapter_deck(client, course_id)          # 4 张图 → 每页 2 张，共 2 页
    charts: list[str] = []
    tables = 0
    for title in (POINT_TITLE, ppt.continue_title(POINT_TITLE)):
        shapes = _slide_shapes(deck["body"]["pptx"], title)
        charts += shapes["charts"]
        tables += shapes["tables"]
    assert len(charts) == 3, charts
    assert any("COLUMN_CLUSTERED" in c for c in charts), charts
    assert any("LINE" in c for c in charts), charts
    assert any("PIE" in c for c in charts), charts
    assert tables == 1
    assert deck["meta"]["figure_failures"] == []
    assert deck["figure_count"] == 4


# ---------------------------------------------------------------------------
# ④ ppt_slides_per_point 生效
# ---------------------------------------------------------------------------

def test_slides_per_point_config_takes_effect(client, course_id, monkeypatch):
    _plan_and_approve(client, course_id, num_chapters=1)
    chapter = _chapter(client, course_id)
    markdown = "\n".join(["## 概念引入", *[f"- 要点 {i}" for i in range(1, 7)],
                          "## 关键点", *[f"- 要点 {i}" for i in range(7, 13)],
                          "## 小结", *[f"- 要点 {i}" for i in range(13, 19)]])
    _add_point(course_id, chapter["id"], markdown=markdown)

    for expected in (1, 2, 3):
        monkeypatch.setattr(get_settings(), "ppt_slides_per_point", expected)
        _gen(client, course_id, scope="chapter", chapter_node_id=chapter["id"])
        deck = _chapter_deck(client, course_id)
        pages = [s for s in deck["slides"]
                 if s["title"] in (POINT_TITLE, ppt.continue_title(POINT_TITLE))]
        assert len(pages) == expected, (expected, [s["title"] for s in deck["slides"]])
        assert deck["slides_per_point"] == expected


# ---------------------------------------------------------------------------
# ⑤ 字体缺失 / 渲染失败：不报错、退回文字版、figure_failures 有原因
# ---------------------------------------------------------------------------

def test_figure_failure_is_explicit_without_breaking_deck(client, course_id, monkeypatch):
    _plan_and_approve(client, course_id, num_chapters=1)
    chapter = _chapter(client, course_id)
    point = _add_point(course_id, chapter["id"])
    _add_diagram(course_id, point["id"], "概念图-字体缺失")
    _add_item(course_id, point["id"], "flowchart", "流程图-暂不支持",
              {"mermaid": "flowchart TD\n a-->b"})
    monkeypatch.setattr(figure_png, "find_font_path", lambda: None)

    _gen(client, course_id, scope="chapter", chapter_node_id=chapter["id"])  # 不应失败
    deck = _chapter_deck(client, course_id)
    failures = deck["figure_failures"]
    assert len(failures) == 2, failures
    reasons = " ".join(f["reason"] for f in failures)
    assert "字体" in reasons, failures
    assert all(f["node_key"] == "1.1" and f["figure"] for f in failures)
    assert deck["meta"]["figure_count"] == 0
    shapes = _slide_shapes(deck["body"]["pptx"], POINT_TITLE)
    assert shapes["pictures"] == 0 and shapes["charts"] == []   # 退回文字版
    assert "未能嵌入的图" in next(
        s for s in deck["slides"] if s["title"] == POINT_TITLE)["notes_excerpt"]


def test_plain_text_deck_when_no_figures(client, course_id):
    """完全没有图时保持纯文字、不报错。"""
    _plan_and_approve(client, course_id, num_chapters=1)
    chapter = _chapter(client, course_id)
    _add_point(course_id, chapter["id"])

    _gen(client, course_id, scope="chapter", chapter_node_id=chapter["id"])
    deck = _chapter_deck(client, course_id)
    assert deck["figure_failures"] == []
    assert deck["meta"]["figure_count"] == 0
    shapes = _slide_shapes(deck["body"]["pptx"], POINT_TITLE)
    assert shapes["pictures"] == 0 and shapes["charts"] == [] and shapes["tables"] == 0


# ---------------------------------------------------------------------------
# ⑥ 同内容不重复渲染 PNG（幂等）
# ---------------------------------------------------------------------------

def test_same_content_reuses_rendered_png(client, course_id, monkeypatch):
    _plan_and_approve(client, course_id, num_chapters=1)
    chapter = _chapter(client, course_id)
    point = _add_point(course_id, chapter["id"])
    _add_diagram(course_id, point["id"], "概念图-幂等")

    calls = {"n": 0}
    real_render = figure_png.render_png

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return real_render(*args, **kwargs)

    monkeypatch.setattr(figure_png, "render_png", _counting)

    _gen(client, course_id, scope="chapter", chapter_node_id=chapter["id"])
    deck = _chapter_deck(client, course_id)
    figures_dir = deck_work_dir(course_id, "chapter", chapter["key"]) / FIGURES_SUBDIR
    files = sorted(figures_dir.glob("fig-*.png"))
    assert len(files) == 1 and calls["n"] == 1
    before = {p.name: (p.stat().st_mtime_ns, hashlib.sha1(p.read_bytes()).hexdigest())
              for p in files}

    _gen(client, course_id, scope="chapter", chapter_node_id=chapter["id"])
    again = _chapter_deck(client, course_id)
    assert again["meta"]["figure_count"] == deck["meta"]["figure_count"] == 1
    assert calls["n"] == 1, "同内容不应重复渲染"
    after = {p.name: (p.stat().st_mtime_ns, hashlib.sha1(p.read_bytes()).hexdigest())
             for p in sorted(figures_dir.glob("fig-*.png"))}
    assert after == before, "同内容的 PNG 应原样复用"
    assert Path(again["body"]["pptx"]).exists()
