# -*- coding: utf-8 -*-
"""任务 127 ①②：图内取色跟「这份课件实际用的主题」+ 存量图一次性配色迁移。

复测（``验收-121-122-独立复测``）：① 图内色跟的是 ``settings.PPT_THEME``，不跟
``generate_deck(theme=...)`` 传入的课件主题 ⇒ 同一份课件里两套色；② 任务 124 之前落库的
概念图（没有 ``body.data``）元素里仍是当年写死的 pastel ⇒ 13 册上页 89 张位图里 60 张含
非主题色。这里钉死两条修法：**图内色以本次课件主题为准**、**存量图给一次性重映射路径**，
并且两者都**不静默**（记 ``layout_notes``）。

硬边界：只测配色 —— 几何 / 文本 / 字号一个字节不许动；不动 ``MIN_TEXT_PT=9`` /
``MIN_MEDIA_WIDTH_IN=6.00`` / ``MAX_DECOR_PER_SLIDE=14``，也不动交付包结构与文件名口径。
"""
from __future__ import annotations

import json

import pytest

from app.db.session import SessionLocal
from app.models.course import Course
from app.services import chart as chart_svc
from app.services import figure_png as F
from app.services import figure_regen
from app.services import ppt

# 任务 144：主题从 3 套扩到 5 套（新增 modern / navy）—— 迁移口径对新主题同样成立
THEMES = ("clean", "professional", "handdrawn", "modern", "navy")
#: 任务 121 之前写死在生成侧的 pastel（真库存量图实测出现的色值）。
LEGACY_PASTEL = ("#0ea5e9", "#9a3412", "#9d174d", "#d1fae5", "#e0f2fe",
                 "#ec4899", "#155e75", "#fef3c7")
_COLOR_KEYS = tuple(F.RECOLOR_FIELDS)


def _legacy_elements() -> list[dict]:
    """一份「任务 121 之前」的元素列表：几何 / 文本原样，取色是写死的 pastel。"""
    return [
        {"id": "a", "type": "rectangle", "x": 10.0, "y": 20.0, "width": 120.0, "height": 60.0,
         "strokeColor": "#0ea5e9", "backgroundColor": "#e0f2fe", "strokeWidth": 2,
         "roundness": {"type": 3, "value": 6}, "isDeleted": False},
        {"id": "b", "type": "text", "x": 14.0, "y": 30.0, "width": 100.0, "height": 20.0,
         "strokeColor": "#9a3412", "backgroundColor": "transparent", "fontSize": 20,
         "text": "步骤一", "textAlign": "center"},
        {"id": "c", "type": "ellipse", "x": 200.0, "y": 20.0, "width": 80.0, "height": 60.0,
         "strokeColor": "#9d174d", "backgroundColor": "#d1fae5", "strokeWidth": 2},
        {"id": "d", "type": "line", "x": 130.0, "y": 50.0, "width": 70.0, "height": 0.0,
         "strokeColor": "#0ea5e9", "backgroundColor": "transparent", "strokeWidth": 2},
    ]


def _without_colors(elements: list[dict]) -> list[dict]:
    """去掉两个取色字段后的「几何 + 文本」快照（用来证明换色不碰别的字段）。"""
    return [{k: v for k, v in el.items() if k not in _COLOR_KEYS} for el in elements]


def _stale_item(db, course, title: str = "存量流程图"):
    """造一张「任务 124 之前的存量图」：有元素、没有 ``body.data``、取色是旧 pastel。"""
    data = {"steps": [{"title": f"阶段{i + 1}", "subs": ["要点"]} for i in range(3)]}
    item = chart_svc.persist_chart_from_data(db, course, None, "timeline", "流程", data,
                                             title=title)
    body = dict(item.body or {})
    doc = dict(body["excalidraw_json"])
    alive = [el for el in doc["elements"] if isinstance(el, dict) and not el.get("isDeleted")]
    replace = {el["id"]: LEGACY_PASTEL[i % len(LEGACY_PASTEL)] for i, el in enumerate(alive)}
    doc["elements"] = [{**el, "strokeColor": replace.get(el.get("id"), el.get("strokeColor"))}
                       for el in doc["elements"] if isinstance(el, dict)]
    body["excalidraw_json"] = doc
    body.pop("data", None)                     # 存量图没有原始 data ⇒ 重跑不了
    item.body = body
    db.commit()
    db.refresh(item)
    return item


# ---------------------------------------------------------------------------
# ① 重映射：幂等 + 落到本主题 + 只碰取色两个字段
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("theme", THEMES)
def test_recolor_color_keeps_already_themed_colors_as_is(theme):
    """幂等：本主题的色（主题色 / 兑白档 / 压暗档 / 中性灰 / 图底色）原样返回。"""
    palette = F.figure_palette(theme)
    keep = [*palette.colors, palette.bg, *F.NEUTRALS,
            palette.fill(0, 0), palette.fill(0, 1), palette.soft(0),
            palette.tip(0), palette.deep(0)]
    for value in keep:
        assert F.recolor_color(value, theme=theme) == value, (theme, value)


@pytest.mark.parametrize("theme", THEMES)
def test_legacy_pastel_is_remapped_onto_the_theme_and_never_left_unknown(theme):
    """旧 pastel ⇒ 重映射后每一个色值都能归因到本主题（``unknown`` 必须为空）。"""
    palette = F.figure_palette(theme)
    mapped = [F.recolor_color(value, theme=theme) for value in LEGACY_PASTEL]
    unknown = [v for v in mapped if F.color_origin(v, theme=palette.theme)[0] == "unknown"]
    assert not unknown, (theme, unknown)


def test_recolor_elements_touches_only_the_two_color_fields():
    """只碰 ``strokeColor`` / ``backgroundColor``：几何 / 文本 / 字号 / 顺序一字不动。"""
    elements = _legacy_elements()
    before = json.dumps(elements, ensure_ascii=False, sort_keys=True)
    out, changed = F.recolor_elements(elements, theme="handdrawn")
    assert changed > 0
    assert json.dumps(elements, ensure_ascii=False, sort_keys=True) == before, "入参被改了"
    assert _without_colors(out) == _without_colors(elements)
    assert [list(el) for el in out] == [list(el) for el in elements], "字段集合变了"
    assert all(F.color_origin(el.get("strokeColor") or el.get("backgroundColor"),
                              theme="handdrawn")[0] != "unknown" for el in out)


def test_recolor_elements_reports_zero_change_when_already_themed():
    """本来就是本主题的色 ⇒ 0 处改动（迁移的幂等性就靠这条）。"""
    palette = F.figure_palette("clean")
    elements = [{"id": "a", "type": "rectangle", "strokeColor": palette.colors[0],
                 "backgroundColor": palette.fill(0, 0)},
                {"id": "b", "type": "text", "strokeColor": F.NEUTRALS[0],
                 "backgroundColor": "transparent"}]
    out, changed = F.recolor_elements(elements, theme="clean")
    assert changed == 0
    assert out[0] is elements[0] and out[1] is elements[1], "没改动就不该拷贝"


# ---------------------------------------------------------------------------
# ① 课件侧：图内色跟「本次课件主题」，且不静默
# ---------------------------------------------------------------------------

def test_themed_figure_follows_the_deck_theme_and_notes_the_remap(client, course_id):
    """存量图给 ``ppt._themed_figure`` ⇒ 换成**本课主题**的色，并附一条可读的理由。"""
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        item = _stale_item(db, course)
        elements, note = ppt._themed_figure(item, "handdrawn")
        assert elements is not None and note is not None
        assert note["kind"] == "figure-kind"
        assert "手绘" in note["reason"] or "handdrawn" in note["reason"]
        audit = F.figure_color_audit(elements, theme="handdrawn")
        assert not audit["unknown"], audit["unknown"]
        assert item.body["excalidraw_json"]["elements"][0]["strokeColor"] in LEGACY_PASTEL, \
            "条目本身不许被就地改（只给渲染用的那一份）"
    finally:
        db.close()


def test_themed_figure_is_silent_when_the_figure_already_matches(client, course_id):
    """新生成的图（已经是本主题派生色）⇒ 不重映射、也不记理由（不制造噪音）。"""
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        data = {"steps": [{"title": f"阶段{i + 1}", "subs": ["要点"]} for i in range(3)]}
        item = chart_svc.persist_chart_from_data(db, course, None, "timeline", "流程", data,
                                                title="新流程图", theme="clean")
        assert ppt._themed_figure(item, "clean") == (None, None)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# ② 存量图一次性迁移路径：先报后写、写后为 0、再跑幂等
# ---------------------------------------------------------------------------

def test_migrate_figure_colors_dry_run_then_apply_then_idempotent(client, course_id):
    """``--migrate-colors`` 的三段式：dry-run 只报 / ``--apply`` 落到 0 / 复跑 0 处改动。"""
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        item = _stale_item(db, course)
        before = (item.body or {}).get("excalidraw_json")

        dry = figure_regen.migrate_figure_colors(db, course_id, theme="clean", apply=False)
        assert dry["stale"] == 1 and dry["migrated"] == 0
        assert dry["unknown_before"] > 0 and dry["unknown_after"] == 0
        db.refresh(item)
        assert (item.body or {}).get("excalidraw_json") == before, "dry-run 不许写库"

        applied = figure_regen.migrate_figure_colors(db, course_id, theme="clean", apply=True)
        assert applied["migrated"] == 1 and applied["unknown_after"] == 0
        db.refresh(item)
        notes = (item.meta or {}).get("layout_notes") or []
        assert notes and notes[0]["kind"] == "figure-palette"
        assert "任务 127" in notes[0]["reason"]
        alive = [el for el in item.body["excalidraw_json"]["elements"]
                 if isinstance(el, dict) and not el.get("isDeleted")]
        assert not F.figure_color_audit(alive, theme="clean")["unknown"]
        assert str(item.body.get("svg_preview") or "").endswith(".svg"), "SVG 预览要跟着重写"

        again = figure_regen.migrate_figure_colors(db, course_id, theme="clean", apply=True)
        assert again["stale"] == 0 and again["unknown_before"] == 0
    finally:
        db.close()
