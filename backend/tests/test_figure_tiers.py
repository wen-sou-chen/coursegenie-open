# -*- coding: utf-8 -*-
"""图生成侧口径（任务 120）：字号四档（φ 档距）、线宽/圆角取档、单图容量上限与超限拆图。

硬边界：只测图生成侧（excalidraw_layout / excalidraw_chart / diagram / figure_png 的渲染
度量），不碰 ppt.py 的判据/选档/交付，也不碰 validate_elements 这道落库闸门。
"""
from __future__ import annotations

import pytest

from app.db.session import SessionLocal
from app.models.course import Course
from app.services import diagram
from app.services import excalidraw_chart as chart_mod
from app.services import excalidraw_layout as layout
from app.services import figure_png as F
from tests.test_excalidraw_layout import LAYOUT_SCENES

TIERS = F.FONT_TIERS
#: 验收②要求的「四档 ↔ 等效磅数 / 源字号 / 线宽 / 圆角」对应表（源码级钉死）
TIER_TABLE = {
    "caption": (9.0, 7.64, 1.0, 4.0),
    "body": (14.56, 12.36, 1.6, 6.5),
    "subtitle": (23.56, 20.0, 2.6, 10.5),
    "title": (38.12, 32.36, 4.2, 16.9),
}
MINDMAP_SCENE = {
    "title": "面粉怎么选 · 思维导图",
    "root": {"emoji": "\U0001F331", "title": "面粉"},
    "branches": [
        {"emoji": "\u2B50", "title": "高筋面粉", "subs": ["蛋白质高", "面条饺子皮"]},
        {"emoji": "\U0001F4AA", "title": "中筋面粉", "subs": ["软硬刚好", "包子馒头"]},
        {"emoji": "\U0001F382", "title": "低筋面粉", "subs": ["组织松软", "蛋糕点心"]},
    ],
}
ALL_SCENES = {**LAYOUT_SCENES, "mindmap": MINDMAP_SCENE}
BAR_DATA = {"labels": ["一周", "两周"], "datasets": [{"label": "体积", "data": [3, 5]}]}
SHAPES = ("rectangle", "ellipse", "diamond")


def test_four_tier_table_matches_the_source():
    """四档 ↔ 磅数/源字号/线宽/圆角：档距黄金比 φ，最低档钉在既有护栏 MIN_TEXT_PT 上。"""
    for tier, row in TIER_TABLE.items():
        got = (F.tier_pt(tier), F.tier_units(tier), F.tier_stroke(tier), F.tier_radius(tier))
        assert got == pytest.approx(row), tier
    assert F.tier_pt("caption") == pytest.approx(F.MIN_TEXT_PT)
    assert F.tier_units("subtitle") == pytest.approx(F.TIER_BASE_UNITS)
    for ladder in ([F.tier_pt(t) for t in TIERS], [F.tier_units(t) for t in TIERS],
                   [F.tier_stroke(t) for t in TIERS], [F.tier_radius(t) for t in TIERS]):
        for lo, hi in zip(ladder, ladder[1:]):
            assert hi / lo == pytest.approx(F.GOLDEN_RATIO, rel=0.02)   # 档距 φ（取整容差）


def test_every_layout_element_uses_the_tier_ladder():
    """8 种版式里每个文字/图形的字号、线宽、圆角都落在四档上，且四档全都被用上。"""
    fonts = {F.tier_units(t) for t in TIERS}
    strokes = {F.tier_stroke(t) for t in TIERS}
    radii = {F.tier_radius(t) for t in TIERS}
    icons = {layout.ICON_ROOT, layout.ICON_HERO, layout.ICON_CARD,
             layout.ICON_TIP, layout.ICON_BADGE}
    seen_strokes, seen_radii = set(), set()
    for name, scene in ALL_SCENES.items():
        elements = layout.render(name, scene)
        issues = layout.validate_elements(elements)
        assert not issues["text_overlaps"], (name, issues)
        assert not issues["overflow"], (name, issues)
        for el in elements:
            if el["type"] == "text":
                assert el["fontSize"] in (fonts | icons), (name, el["text"][:8], el["fontSize"])
                continue
            if el["type"] not in SHAPES:
                continue
            stroke = round(float(el["strokeWidth"]), 1)
            assert stroke in strokes, (name, stroke)
            seen_strokes.add(stroke)
            if el["type"] == "rectangle":
                assert el["roundness"]["type"] == 2
                radius = round(float(el["roundness"]["value"]), 1)
                assert radius in radii, (name, radius)
                seen_radii.add(radius)
    assert seen_strokes == strokes   # 标题轴 / 卡片 / 内部线 / 标签虚线 四档都在用
    assert seen_radii == radii       # 小标签 / 面板 / 卡片 / 标题条 四档圆角都在用


def test_chart_text_and_shapes_use_the_tier_ladder():
    """图表（chart）同样只取档：图题=小标题档、表头=正文档、脚注=注释档（低于护栏时由 MIN_TEXT_UNITS 兜底）。"""
    fonts = {F.tier_units(t) for t in TIERS}
    doc = chart_mod.chart_to_excalidraw_doc("bar", BAR_DATA, "发酵体积")
    elements = doc["elements"]
    assert elements
    for el in elements:
        if el["type"] == "text":
            assert el["fontSize"] in fonts or el["fontSize"] == pytest.approx(
                chart_mod.MIN_TEXT_UNITS), el["fontSize"]
        elif el["type"] in SHAPES:
            assert round(float(el["strokeWidth"]), 1) in {F.tier_stroke(t) for t in TIERS}


def test_capacity_split_is_balanced():
    """拆图分组是均分的（确定、可测）：6→3+3、7→4+3、9→3+3+3。"""
    assert diagram._partition(list(range(6)), 2) == [[0, 1, 2], [3, 4, 5]]
    assert diagram._partition(list(range(7)), 2) == [[0, 1, 2, 3], [4, 5, 6]]
    assert diagram._partition(list(range(9)), 3) == [[0, 1, 2], [3, 4, 5], [6, 7, 8]]


def _timeline_data(steps: int) -> dict:
    return {"nodes": [{"title": f"第 {i + 1} 步", "subs": ["要点一", "要点二"]}
                      for i in range(steps)]}


def test_over_capacity_scene_splits_until_every_part_fits():
    """超限的图必须拆：每一张都不再超上限（把上限去掉 ⇒ 这条会红，反证据）。"""
    caption = "酵母发面六步"
    parts = diagram.split_scene_by_capacity("timeline", _timeline_data(6), caption)
    assert len(parts) >= 2
    assert [p["part"] for p in parts] == list(range(1, len(parts) + 1))
    for part in parts:
        elements = layout.render(part["layout"],
                                 diagram._build_scene(part["layout"], part["data"], caption))
        assert not F.over_capacity(elements), part["part"]
        assert part["note"]["kind"] == "media-split"
        assert caption in part["caption"]      # 图注文案不变（讲义里的图引用不受影响）
    small = diagram.split_scene_by_capacity("timeline", _timeline_data(2), caption)
    assert len(small) == 1 and small[0]["note"] is None


def test_capacity_off_keeps_one_figure(client, course_id):
    """反证据开关：``split=False``（等于把容量上限去掉）时只落一张图。"""
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        items = diagram.persist_diagrams_from_data(
            db, course, None, "timeline", "酵母发面六步", _timeline_data(6),
            title="课程 · 概念图", split=False)
        assert len(items) == 1
        assert not (items[0].meta or {}).get("layout_notes")
    finally:
        db.close()


def test_persist_diagrams_from_data_splits_and_writes_notes(client, course_id):
    """超限落库：拆成「图 1 / 图 2」，图注不变，拆图理由进 meta.layout_notes（kind=media-split）。"""
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        items = diagram.persist_diagrams_from_data(
            db, course, None, "timeline", "酵母发面六步", _timeline_data(6),
            title="课程 · 概念图")
        assert len(items) >= 2
        assert items[0].title.endswith(f"（图 1/{len(items)}）")
        assert items[1].title.endswith(f"（图 2/{len(items)}）")
        for i, item in enumerate(items, 1):
            notes = (item.meta or {}).get("layout_notes") or []
            assert notes and notes[0]["kind"] == "media-split"
            assert notes[0]["figure"] == f"图 {i}/{len(items)}"
            assert item.body["caption"] == "酵母发面六步"      # 图注（讲义引用文案）一字未改
            assert item.body["excalidraw_json"]["elements"]
    finally:
        db.close()
