# -*- coding: utf-8 -*-
"""Excalidraw 概念图排版器（excalidraw_layout）测试。

覆盖 P1 的 7 种布局：文件结构合法、text∩text=0、文字不越界、id 唯一、ZWJ 估宽。
"""
import json
from xml.etree import ElementTree as ET

import pytest

from app.services.diagram import diagram_to_svg
from app.services.excalidraw_layout import (
    build_excalidraw_json,
    estimate_text_width,
    layout_to_excalidraw_text,
    render,
    validate_elements,
)

CHAIN_SCENE = {
    "title": "为什么面团放一会儿会自己变大？",
    "nodes": [
        {"emoji": "\U0001F963", "text": "面粉+水\n+酵母"},
        {"emoji": "\U0001F60B", "text": "酵母吃掉\n糖分"},
        {"emoji": "\U0001F4A8", "text": "排出 CO₂\n+酒精/酸"},
        {"emoji": "\U0001F578\ufe0f", "text": "面筋网\n兜住气体"},
        {"emoji": "\U0001F95F", "text": "面团膨胀\n体积≈2倍"},
    ],
    "edge_labels": ["无氧", "排出", "被兜住", "鼓起"],
    "tip": "小提示：28~35℃ 酵母最活跃；闻到明显酸味、按下去不回弹 → 发过头了，抓紧整形（可加碱救场）",
    "tip_emoji": "\U0001F634",
}

COMIC_SCENE = {
    "title": "酵母菌的作用 · 四格小漫画",
    "subtitle": "酵母是面团里的“打工小能手”，四步看懂它干了啥",
    "panels": [
        {"emoji": "\U0001F9A0", "lines": ["我是酵母菌——", "单细胞的微生物，", "最爱吃麦芽糖 \U0001F36C"], "caption": "① 我是谁？"},
        {"emoji": "\U0001F9A0", "lines": ["我钻进面团里，", "把糖分解，", "悄悄排出 CO₂ \U0001F4A8"], "caption": "② 我做什么？"},
        {"emoji": "\U0001F468\u200d\U0001F373", "lines": ["无数小气泡被面筋网", "兜住，面团", "就鼓起来了 \U0001F388"], "caption": "③ 为什么变胖？"},
        {"emoji": "\U0001F60B", "lines": ["蒸熟后气泡定型，", "包子松软、", "气孔均匀 \U0001F60B"], "caption": "④ 结果：松软好吃"},
    ],
}

RADIAL_SCENE = {
    "title": "面团发酵三要素 · 把酵母伺候舒服",
    "center": {"emoji": "\U0001F95F", "text": "面团发酵"},
    "cards": [
        {"emoji": "\U0001F321\ufe0f", "title": "温度 28~35℃",
         "subs": ["太冷：酵母睡觉 \U0001F634", "太热：酵母烫伤 \U0001F975"]},
        {"emoji": "\U0001F4A7", "title": "湿度 盖湿布保湿", "subs": ["防止表面干皮开裂"]},
        {"emoji": "\u23F0", "title": "时间 观察体积",
         "subs": ["约 2 倍大时", "整形最合适"]},
    ],
    "banner": {"emoji": "\u2705",
               "text": "三要素都到位 → 面团白白胖胖、气孔均匀 \U0001F44D"},
}

RING_SCENE = {
    "title": "做包子，面粉怎么选？",
    "center": {"emoji": "\U0001F95F", "text": "中筋 · 包子首选"},
    "cards": [
        {"emoji": "\U0001F4AA", "title": "高筋面粉",
         "subs": ["蛋白质高 · 有嚼劲", "适合面条 / 饺子皮"]},
        {"emoji": "\u2B50", "title": "中筋面粉",
         "subs": ["蛋白质适中 软硬刚好", "包子馒头首选它"]},
        {"emoji": "\U0001F382", "title": "低筋面粉",
         "subs": ["蛋白质低 组织松软", "做蛋糕点心，包子易塌"]},
        {"emoji": "\U0001F33E", "title": "全麦面粉",
         "subs": ["带麸皮 · 麦香浓", "发酵慢，掺中筋 1:2"]},
    ],
}

TIMELINE_SCENE = {
    "title": "酵母发面 · 六步时间轴",
    "subtitle": "照顺序做，白胖包子不翻车",
    "steps": [
        {"emoji": "\U0001F963", "title": "和面揉光",
         "subs": ["面粉+水+酵母", "揉到盆光手光"]},
        {"emoji": "\U0001F634", "title": "一次发酵",
         "subs": ["盖湿布 28~35℃", "发到约 2 倍大"]},
        {"emoji": "\U0001F590\ufe0f", "title": "排气整形",
         "subs": ["揉匀排掉大气泡", "分剂子、包馅"]},
        {"emoji": "\u23F0", "title": "二次醒发",
         "subs": ["再醒 15~20 分钟", "体积变 1.5 倍"]},
        {"emoji": "\u2668\ufe0f", "title": "上锅蒸制",
         "subs": ["水开上锅 中火", "蒸 12~15 分钟"]},
        {"emoji": "\U0001F389", "title": "焖3分钟出锅",
         "subs": ["关火焖 3 分钟", "白白胖胖开盖"]},
    ],
    "tip": "二次醒发别偷懒，包子才够松软；关火焖 3 分钟，不塌不皱 \U0001F44D",
    "tip_emoji": "\U0001F95F",
}

COMPARE_SCENE = {
    "title": "老面 vs 干酵母 · 谁更省心？",
    "subtitle": "都是发面引子，性格大不同",
    "columns": [
        {
            "emoji": "\U0001F475", "name": "老面 · 天然菌种",
            "bubble": "我是上次留下的老面，\n住着酵母 + 乳酸菌",
            "fill": "#fff7ed", "stroke": "#ea580c", "soft": "#fdba74",
            "tcol": "#9a3412", "chipfill": "#ffedd5",
            "rows": [
                {"label": "菌种", "value": "酵母 + 乳酸菌等，多菌共舞"},
                {"label": "风味", "value": "酸香有层次，回甘明显"},
                {"label": "速度", "value": "较慢，看室温约 3~6 小时"},
                {"label": "保存", "value": "要“续养”，久了易发酸"},
                {"label": "适合", "value": "老面馒头、有嚼劲面食"},
            ],
        },
        {
            "emoji": "\U0001F9EA", "name": "干酵母 · 单一菌种",
            "bubble": "我是工厂提纯的酵母，\n一小袋就够发一盆",
            "fill": "#f0f9ff", "stroke": "#0284c7", "soft": "#7dd3fc",
            "tcol": "#075985", "chipfill": "#e0f2fe",
            "rows": [
                {"label": "菌种", "value": "单一纯酵母，工厂提纯"},
                {"label": "风味", "value": "干净麦香，几乎不酸"},
                {"label": "速度", "value": "快且稳，约 1~2 小时"},
                {"label": "保存", "value": "小袋密封，保质期长"},
                {"label": "适合", "value": "包子日常省时，首选"},
            ],
        },
    ],
}

DECISION_SCENE = {
    "title": "发酵状态自检 · 决策小流程",
    "start": "和面完毕，盖湿布静置发酵",
    "checks": [
        {"q": "体积到 2 倍了吗？", "no": "还没到 2 倍？\n别急，再等 20~30 分钟"},
        {"q": "按下去能回弹吗？\n闻着微微酒香？"},
    ],
    "good": "\u2705 发好啦！\n回弹快 · 微微酒香\n排气整形，二次醒发",
    "bad": "\u26A0\ufe0f 发过头了\n酸味重 · 按下去凹陷\n加碱水揉匀救场",
    "tip": "口诀：体积 2 倍 · 一按回弹 · 闻着微酸 → 刚刚好 \U0001F44D",
}

LAYOUT_SCENES = {
    "chain": CHAIN_SCENE,
    "comic": COMIC_SCENE,
    "radial": RADIAL_SCENE,
    "ring": RING_SCENE,
    "timeline": TIMELINE_SCENE,
    "compare": COMPARE_SCENE,
    "decision": DECISION_SCENE,
}

BOX_TYPES = ("rectangle", "ellipse", "diamond")


def _assert_clean(layout: str, scene: dict) -> None:
    elements = render(layout, scene)
    assert elements, f"{layout} 未生成元素"
    # 元素 id 唯一
    ids = [el["id"] for el in elements]
    assert len(ids) == len(set(ids)), f"{layout} 存在重复 id"
    # .excalidraw 文件结构合法
    doc = build_excalidraw_json(elements)
    assert doc["type"] == "excalidraw"
    assert doc["version"] == 2
    assert doc["elements"] == elements
    assert isinstance(doc["appState"], dict)
    assert json.loads(json.dumps(doc, ensure_ascii=False)) == doc
    for el in elements:
        for key in ("id", "type", "x", "y", "width", "height",
                    "strokeColor", "backgroundColor", "roughness", "seed",
                    "version", "isDeleted", "boundElements"):
            assert key in el, f"{layout} 元素 {el['id']} 缺少 {key}"
    # 绑定文字必须在容器内（容器 id 需真实存在）
    idset = set(ids)
    for el in elements:
        cid = el.get("containerId")
        assert cid is None or cid in idset, f"{layout} 绑定文字指向不存在的容器 {cid}"
    # text∩text=0 且文字不越界
    issues = validate_elements(elements)
    assert not issues["text_overlaps"], f"{layout} text∩text 重叠：{issues['text_overlaps']}"
    assert not issues["overflow"], f"{layout} 文字越界：{issues['overflow']}"


def _assert_svg_is_valid_xml(layout: str, elements: list) -> str:
    """生成的 SVG 必须是合法 XML。

    回归任务 9：polyline 曾同时带字面 fill="none" 与本函数输出的 fill，
    同一属性出现两次 → SVG 非法 → 浏览器按 XML 解析直接失败（网页概念图不显示）。
    """
    svg = diagram_to_svg(elements)
    assert svg, f"{layout} 未生成 SVG"
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as exc:  # 重复属性等非法结构会在这里抛错
        raise AssertionError(f"{layout} 生成的 SVG 不是合法 XML：{exc}") from exc
    assert root.tag.endswith("svg"), f"{layout} 根元素不是 svg"
    assert "viewBox" in svg, f"{layout} SVG 缺少 viewBox"
    # 属性唯一性（双保险：不依赖解析器的宽容度）
    for tag in ("polyline", "rect", "ellipse", "polygon", "text"):
        for chunk in svg.split(f"<{tag}")[1:]:
            head = chunk.split(">", 1)[0]
            attrs = [seg.split("=", 1)[0].strip() for seg in head.split('" ')[:-1] if "=" in seg]
            assert len(attrs) == len(set(attrs)), f"{layout} 的 <{tag}> 有重复属性：{head[:120]}"
    return svg


@pytest.mark.parametrize("layout", sorted(LAYOUT_SCENES))
def test_layout_clean_and_legal(layout):
    _assert_clean(layout, LAYOUT_SCENES[layout])


@pytest.mark.parametrize("layout", sorted(LAYOUT_SCENES))
def test_layout_svg_is_valid_xml(layout):
    """任务 9 回归：7 种布局生成的 SVG 都必须是合法 XML（含箭头/连线）。"""
    elements = render(layout, LAYOUT_SCENES[layout])
    svg = _assert_svg_is_valid_xml(layout, elements)
    # 含线/箭头的布局必须仍有 polyline，且每个 polyline 只带一个 fill
    if any(el.get("type") in ("line", "arrow") for el in elements):
        assert "<polyline" in svg, f"{layout} 丢失 polyline"
        none_count = svg.count('fill="none"')
        poly_count = svg.count("<polyline")
        assert none_count == poly_count, (
            f"{layout} polyline 的 fill 数量异常:fill={none_count} polyline={poly_count}"
        )


def test_zwj_emoji_width_is_single_glyph():
    """👨‍🍳 应只按 2 个 emoji 字形宽估算，而不是按 3 个“字符”累加。"""
    w = estimate_text_width("\U0001F468\u200d\U0001F373", 54)
    assert 100 <= w <= 130, f"ZWJ 估宽异常：{w}"


def test_render_unknown_layout_raises():
    with pytest.raises(ValueError):
        render("not-a-layout", {})


def test_layout_to_excalidraw_text_roundtrip():
    text = layout_to_excalidraw_text("chain", CHAIN_SCENE)
    data = json.loads(text)
    assert data["type"] == "excalidraw"
    assert data["elements"]
