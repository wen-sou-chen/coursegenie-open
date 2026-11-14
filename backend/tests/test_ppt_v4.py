# -*- coding: utf-8 -*-
"""M4-PPT v4 测试：五套主题（任务 144 起）、版式族、几何自检、图片等比、配置项、超长降级。"""
from __future__ import annotations

import io
import time
from pathlib import Path

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.course import ContentItem, OutlineNode
from app.services import ppt, ppt_theme
from app.services.ppt import FIGURES_SUBDIR, audit_pptx, deck_work_dir
from app.services.ppt_layout import MAX_POINT_CHARS, char_units

POINT_TITLE = "发酵原理与微生物基础"
# 任务 144：主题从 3 套扩到 5 套（新增 modern / navy）—— 事实变了，这里同步
THEME_IDS = ("clean", "professional", "handdrawn", "modern", "navy")
REQUIRED_LAYOUTS = {"cover", "toc", "chapter", "point", "review", "quiz", "end"}


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


def _grouped(client, course_id: str) -> dict:
    return client.get(f"/api/ppt/{course_id}").json()


def _chapter_deck(client, course_id: str) -> dict:
    decks = _grouped(client, course_id)["chapters"]
    assert decks, "应已生成章节课件"
    return decks[0]


def _course_deck(client, course_id: str) -> dict:
    decks = _grouped(client, course_id)["course"]
    assert decks, "应已生成整课课件"
    return decks[0]


# ---------------------------------------------------------------------------
# pptx 读取工具
# ---------------------------------------------------------------------------

def _slides(pptx_path: str):
    from pptx import Presentation

    return list(Presentation(pptx_path).slides)


def _texts(pptx_path: str) -> list[str]:
    out: list[str] = []
    for slide in _slides(pptx_path):
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
                out.append(shape.text_frame.text.strip())
    return out


def _shape_stats(pptx_path: str) -> dict:
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    stats = {"pictures": 0, "charts": [], "tables": 0, "pictures_ratio": []}
    for slide in _slides(pptx_path):
        for shape in slide.shapes:
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                stats["pictures"] += 1
                from PIL import Image

                with Image.open(io.BytesIO(shape.image.blob)) as image:
                    intrinsic = image.width / image.height
                stats["pictures_ratio"].append((shape.width / shape.height, intrinsic))
            elif getattr(shape, "has_chart", False):
                stats["charts"].append(shape.chart)
            elif getattr(shape, "has_table", False):
                stats["tables"] += 1
    return stats


def _title_runs(pptx_path: str) -> list[dict]:
    out: list[dict] = []
    for idx, slide in enumerate(_slides(pptx_path), 1):
        entry = {"index": idx, "title": "", "color": None, "size": None, "body_size": None}
        if slide.shapes.title is not None:
            entry["title"] = slide.shapes.title.text.strip()
            for para in slide.shapes.title.text_frame.paragraphs:
                for run in para.runs:
                    try:
                        entry["color"] = str(run.font.color.rgb)
                    except (AttributeError, ValueError):
                        entry["color"] = None
                    entry["size"] = run.font.size.pt if run.font.size else None
                    break
                break
        for shape in slide.shapes:
            if not getattr(shape, "has_text_frame", False) or not shape.text_frame.text.strip():
                continue
            if not getattr(shape, "is_placeholder", False):
                continue
            if shape.placeholder_format.idx != 1:
                continue
            for para in shape.text_frame.paragraphs:
                for run in para.runs:
                    entry["body_size"] = run.font.size.pt if run.font.size else None
                    break
                break
        out.append(entry)
    return out


def _caption_runs(pptx_path: str) -> list[dict]:
    """页上「图 N · …」图注条的字号 / 颜色（按形状取，_title_runs 只读标题占位符）。"""
    out: list[dict] = []
    for idx, slide in enumerate(_slides(pptx_path), 1):
        for shape in slide.shapes:
            if not getattr(shape, "has_text_frame", False):
                continue
            text = shape.text_frame.text.strip()
            if not text.startswith("图 "):
                continue
            for para in shape.text_frame.paragraphs:
                for run in para.runs:
                    out.append({
                        "index": idx,
                        "text": text,
                        "size": run.font.size.pt if run.font.size else None,
                        "color": (str(run.font.color.rgb)
                                  if run.font.color and run.font.color.type is not None else None),
                    })
                    break
                break
    return out


# ---------------------------------------------------------------------------
# ① 每套主题各生成整课 + 章课件，版式族齐全
# ---------------------------------------------------------------------------

def test_three_themes_generate_course_and_chapter_decks(client, course_id):
    _plan_and_approve(client, course_id, num_chapters=2)
    chapter = _chapter(client, course_id)
    point = _add_point(course_id, chapter["id"], markdown=(
        "## 概念引入\n- 微生物的两大类\n- 发酵的四个阶段\n"
        "## 小结\n- 发酵是微生物的代谢过程\n"))
    _add_diagram(course_id, point["id"], "概念图-主题")

    for theme in THEME_IDS:
        _gen(client, course_id, scope="course", theme=theme)
        _gen(client, course_id, scope="chapter", chapter_node_id=chapter["id"], theme=theme)
        grouped = _grouped(client, course_id)
        assert len(grouped["course"]) == 1 and len(grouped["chapters"]) == 1
        for deck in grouped["items"]:
            assert deck["theme"] == theme, deck["theme"]
            assert deck["theme_name"], deck["theme_name"]
            assert deck["layouts"], "应记录每页版式族"
            assert len(deck["layouts"]) == deck["slide_count"]
            assert Path(deck["body"]["pptx"]).exists()

    grouped = _grouped(client, course_id)
    layouts: set[str] = set()
    for deck in grouped["items"]:
        layouts |= set(deck["layouts"])
    assert REQUIRED_LAYOUTS <= layouts, (sorted(REQUIRED_LAYOUTS - layouts), sorted(layouts))


def test_theme_list_endpoint(client):
    resp = client.get("/api/ppt/themes")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [i["id"] for i in items] == list(THEME_IDS)
    assert all(i["name"] and i["colors"] for i in items)


# ---------------------------------------------------------------------------
# ② 每页几何自检通过（每套主题 × 整课 / 每章）
# ---------------------------------------------------------------------------

def test_geometry_audit_passes_for_every_theme(client, course_id):
    _plan_and_approve(client, course_id, num_chapters=2)
    chapter = _chapter(client, course_id)
    point = _add_point(course_id, chapter["id"], markdown=(
        "## 概念引入\n- 微生物的两大类\n- 发酵的四个阶段\n"
        "## 核心讲解\n- 温度影响酶活性\n- 无氧环境产乳酸与酒精\n"
        "## 小结\n- 发酵是微生物的代谢过程\n"))
    quiz = client.post("/api/quiz/generate",
                       json={"course_id": course_id, "node_id": chapter["id"], "count": 3})
    assert quiz.status_code == 200, quiz.text
    _add_item(course_id, point["id"], "chart_bar", "速率对比",
              {"data": {"labels": ["A", "B", "C"], "datasets": [{"label": "速率", "data": [3, 5, 4]}]}})
    _add_diagram(course_id, point["id"], "概念图-几何")

    for theme in THEME_IDS:
        _gen(client, course_id, scope="course", theme=theme)
        _gen(client, course_id, scope="chapter", chapter_node_id=chapter["id"], theme=theme)
        for deck in _grouped(client, course_id)["items"]:
            issues = audit_pptx(deck["body"]["pptx"])
            assert issues == [], (theme, deck["title"], issues)
            assert deck["meta"]["layouts"], deck["meta"].keys()


# ---------------------------------------------------------------------------
# ③ 图片宽高比保持（容差 0.5%）
# ---------------------------------------------------------------------------

def test_images_keep_aspect_ratio(client, course_id):
    _plan_and_approve(client, course_id, num_chapters=1)
    chapter = _chapter(client, course_id)
    point = _add_point(course_id, chapter["id"])
    _add_diagram(course_id, point["id"], "概念图-等比")

    _gen(client, course_id, scope="chapter", chapter_node_id=chapter["id"])
    deck = _chapter_deck(client, course_id)
    stats = _shape_stats(deck["body"]["pptx"])
    assert stats["pictures"] == 1 and stats["pictures_ratio"]
    for actual, intrinsic in stats["pictures_ratio"]:
        assert abs(actual - intrinsic) / intrinsic < 0.005, (actual, intrinsic)
    assert all("图 1 · " in text for text in _texts(deck["body"]["pptx"]) if text.startswith("图 1 · "))


# ---------------------------------------------------------------------------
# ④ 配色与字号来自所选主题（含概念图 PNG 与原生图表）
# ---------------------------------------------------------------------------

def test_theme_palette_and_sizes_come_from_selected_theme(client, course_id):
    from PIL import Image

    _plan_and_approve(client, course_id, num_chapters=1)
    chapter = _chapter(client, course_id)
    point = _add_point(course_id, chapter["id"], markdown=(
        "## 概念引入\n- 叶绿体吸收光能，把二氧化碳和水变成有机物，这是光合作用的第一步。\n"
        "## 关键步骤\n- 光反应产生氧气和能量，暗反应固定二氧化碳并生成葡萄糖。\n"))
    _add_diagram(course_id, point["id"], "概念图-配色")
    _add_item(course_id, point["id"], "chart_bar", "颜色对比",
              {"data": {"labels": ["A", "B"], "datasets": [{"label": "数值", "data": [1, 2]}]}})

    for theme in THEME_IDS:
        spec, _ = ppt_theme.resolve_theme(theme)
        _gen(client, course_id, scope="chapter", chapter_node_id=chapter["id"], theme=theme)
        deck = _chapter_deck(client, course_id)
        assert deck["theme"] == spec["id"] and deck["theme_name"] == spec["name"]

        runs = _title_runs(deck["body"]["pptx"])
        assert runs[0]["size"] == spec["sizes"]["cover_title"]
        assert runs[0]["color"] == spec["palette"]["cover_title"]
        # 任务 146：配图知识点拆页后，续页标题仍用主题 title 档；图注改由页上 caption 条承载。
        assert any(r["size"] == spec["sizes"]["title"] and r["color"] == spec["palette"]["title"]
                   for r in runs[1:])
        continuations = [r for r in runs[1:] if str(r["title"]).endswith(ppt.CONT_SUFFIX)]
        assert continuations, "配图知识点应拆出「（接上页）」页"
        assert all(r["size"] == spec["sizes"]["title"] and r["color"] == spec["palette"]["title"]
                   for r in continuations)
        captions = _caption_runs(deck["body"]["pptx"])
        assert captions, "配图页应把图注画在页上"
        assert any(c["index"] in {r["index"] for r in continuations} for c in captions)
        assert all(c["size"] == spec["sizes"]["caption"] and c["color"] == spec["palette"]["caption"]
                   for c in captions)
        assert all(r["body_size"] in (spec["sizes"]["body"], spec["sizes"]["body_dense"])
                   for r in runs if r["body_size"])

        colors = set()
        for chart in _shape_stats(deck["body"]["pptx"])["charts"]:
            colors.add(str(chart.plots[0].series[0].format.fill.fore_color.rgb))
        assert spec["palette"]["colors"][0] in colors, (theme, colors)

        figures_dir = deck_work_dir(course_id, "chapter", chapter["key"]) / FIGURES_SUBDIR
        files = sorted(figures_dir.glob("fig-*.png"))
        assert files, figures_dir
        with Image.open(files[0]) as image:
            expect = tuple(int(spec["figure"]["bg"].lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
            assert image.convert("RGB").getpixel((3, 3)) == expect, (theme, expect)


# ---------------------------------------------------------------------------
# ⑤ 配置项生效：水印 / 页脚 / 提问页 / 封面作者 / 默认主题
# ---------------------------------------------------------------------------

def test_config_switches_take_effect(client, course_id, monkeypatch):
    settings = get_settings()
    _plan_and_approve(client, course_id, num_chapters=1)
    chapter = _chapter(client, course_id)
    _add_point(course_id, chapter["id"])

    monkeypatch.setattr(settings, "ppt_watermark", "试看版·请勿外传")
    monkeypatch.setattr(settings, "ppt_cover_author", "王老师")
    monkeypatch.setattr(settings, "ppt_include_questions", True)
    _gen(client, course_id, scope="chapter", chapter_node_id=chapter["id"])
    deck = _chapter_deck(client, course_id)
    texts = _texts(deck["body"]["pptx"])
    assert any("试看版·请勿外传" in t for t in texts)
    assert any("主讲：王老师" in t for t in texts)
    assert any("课堂提问" in s["title"] for s in deck["slides"])
    footers = [t for t in texts if t.startswith("光合作用入门 · ")]
    assert footers and footers[0].endswith("· P1"), footers
    # 页脚章号口径 = 与标签同一份（deck_label）：第N章 章名（示范大纲的章名自带「第N章」，不重复拼）
    assert f"第{chapter['key']}章" in footers[0], footers[0]

    monkeypatch.setattr(settings, "ppt_include_questions", False)
    _gen(client, course_id, scope="chapter", chapter_node_id=chapter["id"])
    deck = _chapter_deck(client, course_id)
    assert not any("课堂提问" in s["title"] for s in deck["slides"])

    monkeypatch.setattr(settings, "ppt_show_footer", False)
    monkeypatch.setattr(settings, "ppt_watermark", "")
    _gen(client, course_id, scope="chapter", chapter_node_id=chapter["id"])
    deck = _chapter_deck(client, course_id)
    texts = _texts(deck["body"]["pptx"])
    assert not any(t.startswith("光合作用入门 · ") for t in texts), texts
    assert not any("试看版" in t for t in texts)

    monkeypatch.setattr(settings, "ppt_show_footer", True)
    monkeypatch.setattr(settings, "ppt_theme", "handdrawn")
    _gen(client, course_id, scope="chapter", chapter_node_id=chapter["id"])
    deck = _chapter_deck(client, course_id)
    assert deck["theme"] == "handdrawn"
    assert deck["theme_name"] == "手绘亲和"


def test_unknown_theme_falls_back_with_reason(client, course_id):
    _plan_and_approve(client, course_id, num_chapters=1)
    chapter = _chapter(client, course_id)
    _add_point(course_id, chapter["id"])

    _gen(client, course_id, scope="chapter", chapter_node_id=chapter["id"], theme="no-such-theme")
    deck = _chapter_deck(client, course_id)
    assert deck["theme"] == "clean"
    assert "no-such-theme" in (deck["theme_error"] or "")
    assert any("no-such-theme" in n["reason"] for n in deck["layout_notes"])


# ---------------------------------------------------------------------------
# ⑥ 超长要点 / 要点过多：自动精简且 layout_notes 有记录
# ---------------------------------------------------------------------------

def test_overlong_and_too_many_points_are_trimmed_and_recorded(client, course_id):
    long_bullet = "微生物在无氧条件下通过酶催化把糖类分解成酒精或乳酸" * 3
    markdown = "\n".join([
        "## 概念引入", f"- {long_bullet}", "- 正常要点一条",
        "## 核心讲解", *[f"- 第{i}条要点内容" for i in range(2, 9)],
        "## 小结", "- 发酵是微生物的代谢过程",
    ])
    _plan_and_approve(client, course_id, num_chapters=1)
    chapter = _chapter(client, course_id)
    _add_point(course_id, chapter["id"], markdown=markdown)

    _gen(client, course_id, scope="chapter", chapter_node_id=chapter["id"])
    deck = _chapter_deck(client, course_id)
    reasons = " ".join(n["reason"] for n in deck["layout_notes"])
    assert "要点超长" in reasons, deck["layout_notes"]
    assert "要点过多" in reasons, deck["layout_notes"]
    assert deck["figure_failures"] == []

    page = next(s for s in deck["slides"] if s["title"] == POINT_TITLE)
    assert page["bullets"], page
    for bullet in page["bullets"]:
        assert char_units(bullet) <= MAX_POINT_CHARS + 1, bullet
    assert audit_pptx(deck["body"]["pptx"]) == []


# ---------------------------------------------------------------------------
# 口播稿：每页备注 150~300 字，可以照着念
# ---------------------------------------------------------------------------

def test_every_slide_note_is_a_speakable_script(client, course_id):
    _plan_and_approve(client, course_id, num_chapters=2)
    chapter = _chapter(client, course_id)
    point = _add_point(course_id, chapter["id"], markdown=(
        "## 概念引入\n先从贴近生活的现象切入「发酵原理」，明确本节要解决的问题，"
        "再给出本节的学习路径与判断标准。\n"
        "## 核心讲解\n- 微生物两大类\n- 无氧与有氧的差别\n"))
    quiz = client.post("/api/quiz/generate",
                       json={"course_id": course_id, "node_id": chapter["id"], "count": 2})
    assert quiz.status_code == 200, quiz.text
    _add_diagram(course_id, point["id"], "概念图-讲稿")

    _gen(client, course_id, scope="course")
    _gen(client, course_id, scope="chapter", chapter_node_id=chapter["id"])
    for deck in _grouped(client, course_id)["items"]:
        for slide in deck["slides"]:
            notes = slide["notes_excerpt"]
            assert notes.strip(), f"{deck['title']} 的「{slide['title']}」没有备注"
            assert 150 <= len(notes) <= 300, (slide["title"], len(notes), notes[:40])
    quiz_page = next(s for s in _chapter_deck(client, course_id)["slides"]
                     if s["title"] == "随堂练习")
    assert "答案" in quiz_page["notes_excerpt"] and "解析" in quiz_page["notes_excerpt"]
    assert all("答案" not in b for b in quiz_page["bullets"])
