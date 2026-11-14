# -*- coding: utf-8 -*-
"""任务 77：概念图节点「图标」口径收敛（白名单 + 兜底轮换 + 提示词约束）。

起因（用户 2026-09-22 看 HTML 样板时发现）：概念图上出现 👨‍🍳 / 🥟 / 🌡️ / 🌀 这类与主题
不搭的 emoji。根因在我们自己的 `app/services/diagram.py`——提示词只写「单个字符或省略」，
`_emoji_of()` 又是「模型给什么就用什么」（兜底轮换只解决"有没有"，没有白名单）。

本文件走**纯函数路径**（不调 LLM）：真知识点 + **造假 emoji** 跑
`_build_scene → render → diagram_to_svg`，断言输出图里的图标全部落在白名单内、黑名单命中 0。
另有：兜底轮换顺序稳定（跑两遍一致）、提示词版本与约束、`meta.prompt_version` 落库、
`figure_png` 仍能把白名单 emoji 画进 PNG、只换图标不影响图的尺寸（文字行宽估算不变）。
"""
from __future__ import annotations

import re

import pytest
from PIL import Image

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.course import Course, OutlineNode
from app.services import figure_png
from app.services.diagram import (
    DIAGRAM_EMOJI_WHITELIST,
    DIAGRAM_PROMPT_VERSION,
    DIAGRAM_SYSTEM,
    LAYOUTS,
    _build_scene,
    _DIAGRAM_CENTER_EMOJI,
    _DIAGRAM_HEAD_EMOJI,
    _EMOJI_WHEEL,
    _emoji_of,
    _resolve_layout,
    diagram_to_svg,
    generate_diagram,
    render,
)
from app.services.excalidraw_layout import validate_elements

# 真库只读抄来的真实知识点（课程「AI 幻觉治理实战：从成因到可信输出」27 个知识点里挑 12 个）
REAL_NODES = (
    ("1.1", "AI 也会一本正经地胡说", "掌握幻觉的定义和典型特征"),
    ("1.2", "为什么幻觉是 AI 应用的头号顽疾", "理解幻觉对应用落地的严重影响"),
    ("2.1", "概率机器的本质（逐词预测只顾顺不顾真）", "掌握概率生成的基本原理及其缺陷"),
    ("2.4", "创造性采样策略的双刃剑", "理解温度参数等采样策略的影响"),
    ("3.2", "置信度自评机制", "掌握置信度评级的实现方法"),
    ("4.1", "出处标注标准格式", "掌握标准化引用格式"),
    ("5.1", "上下文窗口的科学利用", "掌握上下文的高效组织方法"),
    ("6.1", "确定性输出与创意输出的平衡", "理解温度参数的适用场景"),
    ("7.1", "模型差异性分析", "了解主流模型的幻觉特点"),
    ("9.2", "速查决策工具", "掌握快速决策方法"),
    ("11.2", "医疗问答治理方案", "掌握医疗场景的特殊要求"),
    ("12.2", "持续改进飞轮", "掌握迭代优化机制"),
)

# 覆盖 §C1 点名的五种布局（radial / chain / ring / timeline / compare）+ decision / comic
LAYOUT_CYCLE = ("radial", "chain", "ring", "timeline", "compare", "decision", "comic")

# 造假 emoji：真机上出现过的那批 + 旧轮换 + 各"闹笑话"家族各一个（**全部**在白名单外）
HOSTILE_EMOJI = ("👨‍🍳", "🥟", "🍕", "🧠", "🌀", "🌡️", "🇨🇳", "💰", "🙂", "👍",
                 "📊", "⭐", "🥖", "🍶", "🧑‍🔬", "📖")

# 「闹笑话」家族（§B1：食物 / 厨具 / 人物 / 表情 / 手势 / 国旗 / 货币；另加动物）
FORBIDDEN_RANGES = (
    (0x1F1E6, 0x1F1FF),  # 国旗（区域指示符）
    (0x1F32D, 0x1F32F),  # 🌭🌮🥙
    (0x1F345, 0x1F35F),  # 🍅🍆🍇…
    (0x1F362, 0x1F37F),  # 🍢🍣🍳🍺🍽…
    (0x1F400, 0x1F43F),  # 动物
    (0x1F466, 0x1F478),  # 👦👧👨👩👴…（人物）
    (0x1F4B0, 0x1F4B8),  # 💰💵💶💷💸（货币）
    (0x1F590, 0x1F596),  # 手势
    (0x1F600, 0x1F64F),  # 😀😂😍🙂🙏（表情）
    (0x1F918, 0x1F91F),  # 手势
    (0x1F950, 0x1F96F),  # 🥐🥟🥡🥢（食物；🥟 = U+1F95F 就是真机上那枚）
    (0x1F9C0, 0x1F9C2),  # 🧀🧁🧂
    (0x1F9D0, 0x1F9E0),  # 🧐🧑🧠（人物 / 身体）
    (0x270A, 0x270D),    # ✊✋✌✍（手势）
)
_SKIP = ("\u200d", "\ufe0f")  # ZWJ / 变体选择符：不算独立图标
_WHITELIST_CHARS = {ch for icon in DIAGRAM_EMOJI_WHITELIST for ch in icon if ch not in _SKIP}


def _forbidden(ch: str) -> bool:
    cp = ord(ch)
    return any(low <= cp <= high for low, high in FORBIDDEN_RANGES)


def _icons_in(text: str) -> set[str]:
    """按 figure_png 的口径取出字符串里出现的 emoji 字符。"""
    return {ch for ch in str(text) if ch not in _SKIP and figure_png.is_emoji(ch)}


def _emoji_fields(obj) -> list[str]:
    """递归取出 scene / data 里所有 `"emoji"` 字段的取值。"""
    found: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in ("emoji", "center_emoji", "tip_emoji") and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_emoji_fields(value))
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            found.extend(_emoji_fields(value))
    return found


def _hostile_data(layout: str, caption: str) -> dict:
    """造假 DIAGRAM_JSON：节点 / 中心 / 对比栏全塞**白名单外**的 emoji。"""

    def pick(i: int) -> str:
        return HOSTILE_EMOJI[i % len(HOSTILE_EMOJI)]

    if layout == "compare":
        nodes = [
            {"side": "left", "emoji": pick(0), "title": "方案 A", "subs": ["常规做法"],
             "rows": [{"label": "优点", "value": "稳妥"}, {"label": "局限", "value": "步骤偏多"}]},
            {"side": "right", "emoji": pick(1), "title": "方案 B", "subs": ["进阶做法"],
             "rows": [{"label": "优点", "value": "高效"}, {"label": "局限", "value": "需要经验"}]},
        ]
        edges: list[dict] = []
    elif layout == "decision":
        nodes = [
            {"kind": "start", "emoji": pick(0), "title": "开始：条件是否满足？"},
            {"kind": "check", "emoji": pick(1), "title": "前置条件齐了吗？", "subs": ["否：先补齐前置条件"]},
            {"kind": "good", "emoji": pick(2), "title": "满足条件", "subs": ["继续推进"]},
            {"kind": "bad", "emoji": pick(3), "title": "不满足条件", "subs": ["调整后重试"]},
        ]
        edges = []
    elif layout == "timeline":
        nodes = [{"emoji": pick(i), "title": f"阶段 {i + 1}", "subs": ["一句话说明"]} for i in range(5)]
        edges = []
    else:  # radial / ring / chain / comic
        nodes = [{"emoji": pick(i), "title": f"要点 {i + 1}", "subs": ["一句话说明"]} for i in range(4)]
        edges = [{"from": i, "to": i + 1, "label": "接着"} for i in range(3)]
    return {"layout": layout, "caption": caption, "center": "中心一句话",
            "center_emoji": pick(5), "nodes": nodes, "edges": edges}


def test_real_knowledge_points_only_whitelist_icons():
    """§C1：真知识点 × 各布局跑纯函数路径 ⇒ 图标全在白名单、黑名单命中 0。"""
    cases: list[tuple[str, str, str]] = []
    for idx, (key, title, objective) in enumerate(REAL_NODES):
        cases.append((key, title, _resolve_layout(None, objective, title)))       # 真生成路径的推断
        cases.append((key, title, LAYOUT_CYCLE[idx % len(LAYOUT_CYCLE)]))         # 点名布局，保证全覆盖
    problems: list[str] = []
    for key, title, layout in cases:
        caption = f"{title} · 一图看懂"
        data = _hostile_data(layout, caption)
        scene = _build_scene(layout, data, caption)
        elements = render(layout, scene)
        issues = validate_elements(elements)
        svg = diagram_to_svg(elements)

        icons = _emoji_fields(scene)
        outside = sorted(set(icons) - set(DIAGRAM_EMOJI_WHITELIST))
        flagged = sorted({ch for ch in svg if _forbidden(ch)})
        stray = sorted(_icons_in(svg) - _WHITELIST_CHARS)
        if issues["text_overlaps"] or issues["overflow"]:
            problems.append(f"{key} {layout} 排版自检未过：{issues}")
        if outside:
            problems.append(f"{key} {layout} scene 里有白名单外图标：{outside}")
        if flagged:
            problems.append(f"{key} {layout} SVG 命中黑名单：{flagged}")
        if stray:
            problems.append(f"{key} {layout} SVG 里有白名单外图标：{stray}")
        print(f"[{layout:8s}] {key} {title} → 图标 {sorted(set(icons))}；黑名单命中 0")

    assert {layout for _, _, layout in cases} >= {"radial", "chain", "ring", "timeline", "compare"}
    assert len({key for key, _, _ in cases}) >= 10, "至少要 10 个真实知识点"
    assert not problems, "图标口径被破：\n" + "\n".join(problems)


def test_fallback_wheel_is_stable_and_reproducible():
    """§C2：白名单外 / 空 / 拼两个图标 ⇒ 一律走兜底轮换，跑两遍顺序一致。"""
    nodes = ([{"emoji": e} for e in HOSTILE_EMOJI]
             + [{}, {"emoji": ""}, {"emoji": None}, {"emoji": 123},
                {"emoji": f"{DIAGRAM_EMOJI_WHITELIST[0]}{DIAGRAM_EMOJI_WHITELIST[1]}"}])
    first = [_emoji_of(node, i) for i, node in enumerate(nodes)]
    second = [_emoji_of(node, i) for i, node in enumerate(nodes)]
    print("第一遍：" + "".join(icon for icon in first))
    print("第二遍：" + "".join(icon for icon in second))
    assert first == second, "兜底轮换不可复现（两遍结果不同）"
    assert set(first) <= set(_EMOJI_WHEEL)
    assert first[: len(_EMOJI_WHEEL)] == _EMOJI_WHEEL, "兜底轮换没有从轮子头部按序取"
    assert len(_EMOJI_WHEEL) == len(set(_EMOJI_WHEEL)), "兜底轮换里有重复项"

    # 白名单内的图标（模型选对了）必须原样保留
    keep = [_emoji_of({"emoji": icon}, i) for i, icon in enumerate(DIAGRAM_EMOJI_WHITELIST)]
    assert keep == DIAGRAM_EMOJI_WHITELIST


def test_whitelist_shape_families_and_png_coverage():
    """§B1：10~16 个、无重复、不含闹笑话家族、能被 figure_png 认作 emoji。"""
    icons = DIAGRAM_EMOJI_WHITELIST
    assert 10 <= len(icons) <= 16, f"白名单应 10~16 个，实际 {len(icons)}"
    assert len(set(icons)) == len(icons), "白名单里有重复项"
    bad = sorted({ch for icon in icons for ch in icon if _forbidden(ch)})
    assert not bad, f"白名单里混进了闹笑话家族：{bad}"
    stripped = [ch for icon in icons for ch in icon if ch not in _SKIP and not figure_png.is_emoji(ch)]
    assert not stripped, f"白名单里有 figure_png 不认的字符（PNG 里会被抹掉）：{stripped}"
    assert set(_EMOJI_WHEEL) <= set(icons), "兜底轮换不是白名单子集"
    assert _DIAGRAM_HEAD_EMOJI in icons and _DIAGRAM_CENTER_EMOJI in icons


def test_prompt_constrains_icons_and_version_bumped():
    """§B2：提示词点名「只能从图标清单里选一个」+ 版本号升级。"""
    assert DIAGRAM_PROMPT_VERSION == "diagram-v2"
    assert "单个字符或省略" not in DIAGRAM_SYSTEM, "提示词还在写「单个字符或省略」（等于没约束）"
    assert re.search(r"emoji 只能从下面这 \d+ 个图标里\*\*原样选一个\*\*", DIAGRAM_SYSTEM), DIAGRAM_SYSTEM[-400:]
    for word in ("食物", "厨具", "人物", "表情", "国旗", "货币"):
        assert word in DIAGRAM_SYSTEM, f"提示词没有点名排除「{word}」"
    listed = int(re.search(r"这 (\d+) 个图标", DIAGRAM_SYSTEM).group(1))
    assert listed == len(DIAGRAM_EMOJI_WHITELIST)
    for icon in DIAGRAM_EMOJI_WHITELIST:
        assert icon in DIAGRAM_SYSTEM, f"提示词清单里少了 {icon}"
    assert "从规则 6 的图标清单里原样选一个" in DIAGRAM_SYSTEM


def test_generated_item_meta_and_svg_are_clean(client, course_id):
    """§B2 + §C1：mock LLM 真落库路径 —— meta 记版本，写入的 SVG 无黑名单图标。"""
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "初中生物光合作用", "num_chapters": 2,
    })
    assert resp.status_code == 200, resp.text
    assert client.post("/api/plan/approve", json={
        "course_id": course_id, "approved": True,
    }).status_code == 200
    node = next((n for n in resp.json()["nodes"] if "." in n["key"]), resp.json()["nodes"][0])

    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        node_row = db.get(OutlineNode, node["id"])
        for layout in sorted(LAYOUTS):
            item = generate_diagram(db, course, node_row, diagram_type=layout,
                                    instruction=f"围绕{node_row.title}梳理{layout}布局内容")
            assert (item.meta or {}).get("prompt_version") == DIAGRAM_PROMPT_VERSION
            svg = (get_settings().storage_dir / item.body["svg_preview"]).read_text(encoding="utf-8")
            assert not sorted({ch for ch in svg if _forbidden(ch)}), f"{layout} 的 SVG 命中黑名单"
            assert _icons_in(svg) <= _WHITELIST_CHARS, f"{layout} 的 SVG 有白名单外图标"
    finally:
        db.close()


def _geometry(elements) -> list[tuple]:
    """元素几何快照：**跳过图标元素本身**（图标字形宽会变，文字行宽不该变）。"""
    out = []
    for el in elements:
        text = str(el.get("text") or "")
        if any(figure_png.is_emoji(ch) for ch in text):
            continue
        out.append((el["type"], round(float(el["x"]), 3), round(float(el["y"]), 3),
                    round(float(el.get("width") or 0), 3), round(float(el.get("height") or 0), 3),
                    text))
    return out


def test_icon_swap_keeps_text_geometry():
    """§C4：图标独立顶置、不参与文字行宽估算 ⇒ 只换图标时**文字/卡片/箭头几何一字不差**。"""
    caption = "尺寸不变"
    data = {"layout": "radial", "caption": caption, "center": "中心一句话",
            "center_emoji": "🍕", "nodes": [
                {"emoji": "🥟", "title": "要点一", "subs": ["说明一句"]},
                {"emoji": "🧠", "title": "要点二", "subs": ["说明一句"]},
                {"emoji": "🌀", "title": "要点三", "subs": ["说明一句"]},
            ], "edges": []}
    scene_new = _build_scene("radial", data, caption)
    scene_old = {**scene_new, "emoji": "\U0001F468\u200d\U0001F373"}  # 改回渲染器旧缺省头像 👨‍🍳
    els_new = render("radial", scene_new)
    els_old = render("radial", scene_old)
    assert _geometry(els_new) == _geometry(els_old), "换图标把文字/卡片/箭头的几何改了"
    svg_new = diagram_to_svg(els_new)
    svg_old = diagram_to_svg(els_old)
    box = lambda svg: re.search(r'viewBox="([^"]+)" width="([^"]+)" height="([^"]+)"', svg).groups()
    print(f"同一张图、只换标题条图标：旧 {box(svg_old)} → 新 {box(svg_new)}（画布只随图标字形宽变化）")
    assert box(svg_new)[2] == box(svg_old)[2], "高度不该变"
    assert "\U0001F468" in svg_old and "\U0001F468" not in svg_new
    assert _DIAGRAM_HEAD_EMOJI in svg_new
    assert not sorted({ch for ch in svg_new if _forbidden(ch)})


def test_figure_png_still_draws_whitelist_emoji(tmp_path):
    """§C4 不回归：figure_png 仍能把白名单 emoji 画进 PNG（像素断言）。"""
    emoji_font = figure_png.find_emoji_font_path()
    if emoji_font is None:
        pytest.skip("本机没有彩色 emoji 字体（figure_png 会退化为去掉 emoji）")
    icon = DIAGRAM_EMOJI_WHITELIST[0]
    out = figure_png.render_png(
        [{"id": "t1", "type": "text", "x": 40, "y": 40, "width": 160, "height": 120,
          "text": icon, "fontSize": 64, "strokeColor": "#111827", "textAlign": "center"}],
        tmp_path / "emoji.png", min_width=1200,
    )
    img = Image.open(out).convert("RGB")
    bg = (255, 251, 242)
    drawn = [(x, y) for y in range(img.height) for x in range(img.width)
             if img.getpixel((x, y)) != bg]
    colored = [p for p in drawn if max(img.getpixel(p)) - min(img.getpixel(p)) > 24]
    print(f"figure_png: {out} 画布 {img.size}、非背景像素 {len(drawn)}（其中彩色 {len(colored)}）；"
          f"emoji 字体 {emoji_font}")
    assert len(drawn) > 200, f"emoji 没画进 PNG（非背景像素只有 {len(drawn)}）"
