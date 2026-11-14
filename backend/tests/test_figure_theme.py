# -*- coding: utf-8 -*-
"""任务 121：图内配色一律从 PPT 主题派生（唯一出处 = ``figure_png.FigurePalette``）。

观测口径（验收①，研究报告「验收数字」）：同一份场景在 3 个主题下各渲染一次 ——

1. 图内每个色值都能归因到「该主题的 6 色 / 同色相兑白档 / 同色相压暗档 / 中性灰 / 该主题图底色」，
   ``figure_color_audit(...)["unknown"]`` 必须为空；
2. 单图非灰彩色相 <= 3（三色法上限 ``MAX_HUES``）；
3. 换主题整套色跟着换，而**几何指纹逐字不变**（坐标 / 尺寸 / 字号 / 线宽 / 圆角 / 文本）
   => 换肤不可能让磅数或显示跨度回归。

存量场景**自带的**颜色（如 ``COMPARE_SCENE`` 里的 ``fill/stroke``）按设计优先于派生
=> 存量图观感不变；所以「派生」只用**不带场景色**的场景来验（``_without_scene_colors``）。

反证据（见 ``docs/task121-figure-theme.md``）：把 ``excalidraw_layout.render`` 里的
``figure_palette(theme)`` 写死成 ``figure_palette("clean")`` => 本文件点名用例转红；还原后全绿。
"""
import copy
import json

import pytest

from app.services import chart, excalidraw_chart, excalidraw_flow
from app.services.excalidraw_layout import render
from app.services.figure_png import figure_color_audit, figure_palette
from app.services.ppt_theme import resolve_theme
from tests.test_excalidraw_layout import LAYOUT_SCENES

# 任务 144：主题从 3 套扩到 5 套（新增 modern / navy）—— 图色随主题的回归要覆盖新主题
THEMES = ("clean", "professional", "handdrawn", "modern", "navy")
#: 三色法上限：一张图最多 3 种非灰彩色相（研究报告「验收数字」）。
MAX_HUES = 3

MINDMAP_SCENE = {
    "title": "面粉怎么选 · 思维导图",
    "root": {"emoji": "\U0001F331", "title": "面粉"},
    "branches": [
        {"emoji": "\u2B50", "title": "高筋面粉", "subs": ["蛋白质高", "面条饺子皮"]},
        {"emoji": "\U0001F4AA", "title": "中筋面粉", "subs": ["软硬刚好", "包子馒头"]},
        {"emoji": "\U0001F382", "title": "低筋面粉", "subs": ["组织松软", "蛋糕点心"]},
    ],
}

SCENES = dict(LAYOUT_SCENES, mindmap=MINDMAP_SCENE)

CHART_DATA = {
    "bar": {"labels": ["一周", "两周", "三周"],
            "datasets": [{"label": "体积", "data": [3, 5, 7]}]},
    "line": {"labels": ["1", "2", "3"],
             "datasets": [{"label": "A", "data": [1, 3, 2]}, {"label": "B", "data": [2, 1, 4]}]},
    "pie": {"labels": ["甲", "乙", "丙", "丁", "戊"],
            "datasets": [{"label": "占比", "data": [4, 3, 2, 1, 1]}]},
    "table": {"headers": ["项目", "数值"], "rows": [["温度", "28 度"], ["时间", "2 小时"]]},
    "mindmap": {"root": {"title": "面粉"}, "branches": [
        {"title": "高筋", "subs": ["蛋白质高"]},
        {"title": "中筋", "subs": ["软硬刚好"]},
        {"title": "低筋", "subs": ["组织松软"]}]},
    "timeline": {"steps": [{"title": "第 1 步", "subs": ["起点"]},
                           {"title": "第 2 步", "subs": ["推进"]},
                           {"title": "第 3 步", "subs": ["收尾"]}]},
}

FLOW_SOURCE = """
flowchart TD
    A[叶绿体吸收光能] --> B[光反应阶段]
    B --> C{产物去向}
    C -->|产生氧气| D[氧气释放]
"""

#: 几何指纹 = 「颜色以外的一切」：坐标 / 尺寸 / 字号 / 线宽 / 圆角 / 文本 / 对齐。
#: 不带 ``id`` / ``containerId`` / ``seed`` 等每次渲染都变的字段。
GEOM_KEYS = ("type", "x", "y", "width", "height", "fontSize", "strokeWidth",
             "roundness", "text", "textAlign")


def _without_scene_colors(scene: dict) -> dict:
    """去掉场景自带的五个颜色键（``fill/stroke/soft/tcol/chipfill``）=> 全部交给主题派生。"""
    scene = copy.deepcopy(scene)
    for col in scene.get("columns") or []:
        for key in ("fill", "stroke", "soft", "tcol", "chipfill"):
            col.pop(key, None)
    return scene


CLEAN_SCENES = dict(SCENES, compare=_without_scene_colors(SCENES["compare"]))


def _elements(name: str, theme: str) -> list[dict]:
    if name.startswith("layout:"):
        return render(name.split(":", 1)[1], CLEAN_SCENES[name.split(":", 1)[1]], theme=theme)
    if name.startswith("chart:"):
        key = name.split(":", 1)[1]
        return excalidraw_chart.chart_to_excalidraw_doc(
            key, CHART_DATA[key], "示例图", theme=theme)["elements"]
    return excalidraw_flow.mermaid_to_excalidraw_json(FLOW_SOURCE, theme=theme)["elements"]


def _geometry(elements: list[dict]) -> str:
    return json.dumps([{key: el.get(key) for key in GEOM_KEYS} for el in elements],
                      sort_keys=True, ensure_ascii=False)


def _audit(name: str, theme: str) -> dict:
    return figure_color_audit(_elements(name, theme), theme=theme)


NAMES = (["layout:" + key for key in sorted(CLEAN_SCENES)]
         + ["chart:" + key for key in sorted(CHART_DATA)] + ["flow"])


def test_palette_is_the_theme_palette_and_nothing_else():
    """图内取色只有主题这一个出处：6 色与图底色逐字等于 ``ppt_theme`` 的定义。"""
    for theme in THEMES:
        spec, error = resolve_theme(theme)
        palette = figure_palette(theme)
        assert error is None
        assert palette.theme == theme
        assert palette.colors == tuple("#" + c.lower() for c in spec["palette"]["colors"])
        assert palette.bg == spec["figure"]["bg"].lower()


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("name", NAMES)
def test_every_color_is_derived_from_the_theme(name, theme):
    """验收①：颜色 100% 出自主题（unknown 为空），且单图非灰彩色相 <= 3。"""
    audit = _audit(name, theme)
    assert audit["colors"], f"{name}/{theme} 没有取到任何颜色"
    assert audit["unknown"] == [], (
        f"{name}/{theme} 出现非主题派生的颜色：{audit['unknown']}"
        f"（可见色值 {sorted(audit['colors'])}）")
    assert audit["hues"], f"{name}/{theme} 一个主题色相都没用到"
    assert len(audit["hues"]) <= MAX_HUES, (
        f"{name}/{theme} 用了 {len(audit['hues'])} 种非灰彩色相：{audit['hues']}")


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("name", ["chart:mindmap", "chart:timeline", "layout:mindmap", "flow"])
def test_mindmap_and_timeline_paths_share_the_same_source(name, theme):
    """思维导图（版式 / chart 两条路径）与流程图同样只吃主题色。"""
    audit = _audit(name, theme)
    assert audit["unknown"] == []
    assert 0 < len(audit["hues"]) <= MAX_HUES


@pytest.mark.parametrize("theme", THEMES)
def test_chart_mindmap_and_layout_mindmap_use_the_same_slot(theme):
    """同一张思维导图，走 chart 路径与走版式路径取到的是同一套色（同源，不是第二套板子）。"""
    chart_colors = set(_audit("chart:mindmap", theme)["colors"])
    layout_colors = set(_audit("layout:mindmap", theme)["colors"])
    assert chart_colors == layout_colors


@pytest.mark.parametrize("name", NAMES)
def test_theme_swap_recolors_everything_but_moves_no_geometry(name):
    """验收①/②：各主题渲染同一张图 => 色集合两两不同，几何指纹逐字相同。"""
    color_sets, geometries = [], set()
    for theme in THEMES:
        elements = _elements(name, theme)
        color_sets.append(frozenset(_audit(name, theme)["colors"]))
        geometries.add(_geometry(elements))
    assert len(geometries) == 1, f"{name} 换主题动了几何（磅数 / 跨度会回归）"
    assert len(set(color_sets)) == len(THEMES), (
        f"{name} 换主题没有整套换色：{[sorted(s) for s in color_sets]}")


@pytest.mark.parametrize("theme", THEMES)
def test_scene_supplied_colors_win_over_derived_ones(theme):
    """存量观感不回归：场景自带的颜色（存量 compare 图）跨主题一字不变，仍优先于派生色。"""
    legacy = LAYOUT_SCENES["compare"]
    hardcoded = {col[key] for col in legacy["columns"]
                 for key in ("fill", "stroke", "soft", "tcol", "chipfill")}
    used = set(figure_color_audit(render("compare", legacy, theme=theme), theme=theme)["colors"])
    assert hardcoded <= used, f"{theme} 下存量场景色被改掉了：{sorted(hardcoded - used)}"


@pytest.mark.parametrize("theme", THEMES)
def test_chart_js_data_uses_the_same_theme_palette(theme):
    """chart.js 侧（落库的 ``borderColor``/``backgroundColor``）与图内同源、同色板。

    回归：把主题色板喂给 ``chart_data.normalize_chart_data`` 时，它按 ``f"#{color}"`` 拼裸
    hex ⇒ 必须去掉 ``#``（否则出 ``##4c8df6``），且保持它既有的**大写**书写。
    """
    palette = figure_palette(theme)
    body, _title, _issues = chart._chart_payload(
        "bar", "示例图", "指令", dict(CHART_DATA["bar"]), "来源", theme=theme)
    want = {"#" + color.lstrip("#").upper() for color in palette.colors}
    for dataset in body["data"]["datasets"]:
        assert dataset["borderColor"] in want, dataset
        assert dataset["backgroundColor"] in want, dataset
        assert "##" not in dataset["borderColor"] + dataset["backgroundColor"]


@pytest.mark.parametrize("theme", THEMES)
def test_derived_colors_do_not_leak_another_theme(theme):
    """派生色的**值**必须属于本主题：别的主题的独占色出现在本主题的图里即判红。"""
    mine = set(figure_palette(theme).colors)
    for other in THEMES:
        if other == theme:
            continue
        foreign = set(figure_palette(other).colors) - mine
        assert not (foreign & mine)
