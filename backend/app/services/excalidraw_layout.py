# -*- coding: utf-8 -*-
"""Excalidraw 概念图排版器（P2 阶段一）。

把 P1 样图（7 张已验收）的排版逻辑从临时脚本整理为正式服务模块：
手绘链条 layout_chain、四格漫画 layout_comic、中心辐射 layout_radial、
环形布局 layout_ring、时间轴 layout_timeline、对比分栏 layout_compare、
决策流程 layout_decision。

约定
- 每个布局函数输入统一的结构化场景 dict，输出 Excalidraw 元素列表（elements）。
- 共享风格参数：roughness≈2、**图内色从 PPT 主题派生**（单色阶 + 强调色 + 中性灰）、
  浅底、节点间距留白；换主题时整套图跟着换（任务 121）。
- emoji 独立顶置，不参与文字行宽计算；ZWJ emoji（如 👨‍🍳）按“单字符”估宽。
- 卡片加高、文字独占中下区，保证 text∩text=0 且文字不越界（P1 已验证方案）。
"""
from __future__ import annotations

import json
import random
import time
from itertools import count
from typing import Any

from app.services.excalidraw_format import normalize_document
from app.services.figure_png import (FONT_TIERS, FigurePalette, figure_palette,
                                     reflow_text_elements, source_font_units_for_pt,
                                     tier_radius, tier_stroke, tier_units)

TS = int(time.time() * 1000)
RND = random.Random()

INK = "#374151"
INK_DARK = "#1f2937"
MUTED = "#64748b"
EDGE = "#94a3b8"
BG = "#fffbf2"

# 与已验收 P1 样图一致（font 1 = Excalidraw Virgil 手写，需要手写体时可一处切换）
FONT_FAMILY = 2
ROUGH = 2.0

# —— 字号四档（任务 120）——
# 口径正本在 figure_png（图渲染侧）：四档 = 标题 / 小标题 / 正文 / 注释，档距黄金比 φ = 1.618，
# 最低档钉在可读性下限 MIN_TEXT_PT 上；**同一档位同时决定线宽与圆角**。
# 本模块只取档位值，不再散落取数。
FONT_TITLE = tier_units("title")          # 32.36 源单位
FONT_SUBTITLE = tier_units("subtitle")    # 20.00
FONT_BODY = tier_units("body")            # 12.36
FONT_CAPTION = tier_units("caption")      # 7.64
SW = tier_stroke("subtitle")              # 2.6：默认线宽 = 小标题档（原 2.5，取整到档位）
# 图标（emoji）是图形不是文字档，但同样不许散落取数：统一 =「小标题档 × 整数倍」，
# 五个倍率正好复现已验收样图的观感（54 / 42 / 40 / 30 / 84），图标尺寸因此一字未变。
ICON_ROOT = round(FONT_SUBTITLE * 4.2)    # 84：思维导图根节点 / 漫画角色
ICON_HERO = round(FONT_SUBTITLE * 2.7)    # 54：标题条左上角 / 中心大图标
ICON_CARD = round(FONT_SUBTITLE * 2.1)    # 42：卡片顶置图标
ICON_TIP = round(FONT_SUBTITLE * 2.0)     # 40：提示条 / 时间轴卡片图标
ICON_BADGE = round(FONT_SUBTITLE * 1.5)   # 30：思维导图分支徽标

# -*- coding: utf-8 -*-
# —— 布局 → 主题色槽位（任务 121）——
# 图内**不再自带色板**（原 PASTEL 7 组轮换 / ACCENTS 8 套 / TIP_ACCENT / _MINDMAP_GREENS 已删）：
# 颜色一律从 PPT 主题的 6 色派生（口径正本 = figure_png.FigurePalette），换主题整套图跟着换。
# 8 个布局映射到 6 个色槽，沿用「每个布局一套主色、别清一色模板」的意图；6 色不够 8 个布局
# ⇒ 两对按语义复用（radial·decision 同属「判断 / 提醒」的暖色；compare·comic 同属
# 「对比 / 叙事」的红系）。**具体色相由主题决定**，槽位只定角色。
LAYOUT_SLOTS = {
    "chain": 5, "timeline": 0, "radial": 1, "decision": 1,
    "ring": 3, "compare": 4, "comic": 4, "mindmap": 2,
}
#: 一张图的色相数：主色槽 + 强调色槽（+1）+ 第三色槽（+3，只给决策图的「不通过」出口用）。
#: 三色法上限（单图非灰彩色 ≤3 种）见 docs/task121-figure-theme.md。
ACCENT_OFFSET = 1
THIRD_OFFSET = 3


def slot_of(layout: str) -> int:
    """该布局的主色槽位（未收录的布局回落 0，不抛异常）。"""
    return LAYOUT_SLOTS.get(str(layout), 0)
#: 标题条图标的**渲染器缺省值**：场景没有 `emoji` 字段时就用它。
#: 任务 92：原先这里是 👨‍🍳（U+1F468 + ZWJ + U+1F373）—— 存量场景渲染出来左上角顶着个厨师，
#: 而任务 77 只收敛了「生成时写进场景的图标」，没动渲染器 ⇒ 缺字段的老图永远长厨师。
#: 现在换成 📘（= `diagram._DIAGRAM_HEAD_EMOJI`，白名单内的「知识 / 教材」）；存量场景里
#: 已经写死的那枚 👨‍🍳 由 `scripts/normalize_diagram_icons.py` 就地归一。
DEFAULT_HEAD = "\U0001F4D8"
CIRCLED = "\u2460\u2461\u2462\u2463\u2464\u2465\u2466\u2467\u2468\u2469"

_IDS = count(1)

# 任务 107（可读性口径）：本模块的卡片/节点尺寸都**由文字度量推导**（见
# estimate_text_width / estimate_text_height），因此「字号 ÷ 画布跨度」是个不变量 ——
# 单独调大 fontSize 只会等比放大画布，上页后的等效磅数（figure_png.pt_equivalent）不变。
# 任务 120 已按这条路兑现：① 字号收成四档（FONT_TITLE / FONT_SUBTITLE / FONT_BODY /
# FONT_CAPTION，档距 φ，且同一档位决定线宽与圆角）；② 渲染侧白边/卡片内留白收到半档
# （figure_png.PAD / CARD_PAD_UNITS / TEXT_GAP_UNITS），文字相对画布变大；③ 「一屏放几个
# 节点」由 figure_png.MAX_FIGURE_TEXT_ELEMENTS 定上限、diagram.split_scene_by_capacity
# 超限拆图。渲染侧 figure_png.min_text_px() 仍是最后一道护栏（按显示档位抬字号）。


def _nid(prefix: str) -> str:
    return f"{prefix}{next(_IDS)}"


def _defaults() -> dict[str, Any]:
    return {
        "version": 1,
        "versionNonce": RND.randint(0, 2**31),
        "seed": RND.randint(0, 2**31),
        "updated": TS,
        "isDeleted": False,
        "groupIds": [],
        "frameId": None,
        "link": None,
        "locked": False,
        "boundElements": [],
    }


def _glyph_width(ch: str, size: float) -> float:
    """单字符估宽：ZWJ/变体选择符/组合附加符按 0，emoji/CJK 按整字宽。"""
    o = ord(ch)
    if ch in "\u200d\ufe0f" or 0x0300 <= o <= 0x036F:
        return 0.0
    if o > 0x2E80 or 0x1F000 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF or 0x2B00 <= o <= 0x2BFF:
        return size
    if ch == " ":
        return size * 0.45
    if ch.isascii():
        return size * 0.55
    return size


def estimate_text_width(text: str, size: float) -> float:
    """估算多行文本的最大行宽（含 8px 余量）。"""
    lines = text.split("\n") or [""]
    return max(sum(_glyph_width(c, size) for c in ln) for ln in lines) + 8


def estimate_text_height(text: str, size: float) -> float:
    """估算多行文本高度（行高 1.35）。"""
    return text.count("\n") * size * 1.35 + size * 1.1 + 6


def _box(shape: str, x: float, y: float, w: float, h: float,
         fill: str, stroke: str, rough: float = ROUGH, sw: float | None = None,
         dash: bool = False, *, tier: str = "subtitle") -> dict[str, Any]:
    """图形元素。``tier``（四档之一）同时决定**线宽与圆角**（任务 120）。"""
    etype = {"ellipse": "ellipse", "diamond": "diamond"}.get(shape, "rectangle")
    width = tier_stroke(tier) if sw is None else float(sw)
    roundness = None if etype != "rectangle" else {"type": 2, "value": tier_radius(tier)}
    el = {
        "id": _nid("b"),
        "type": etype,
        "x": round(x, 1),
        "y": round(y, 1),
        "width": round(w, 1),
        "height": round(h, 1),
        "angle": 0,
        "strokeColor": stroke,
        "backgroundColor": fill,
        "fillStyle": "solid",
        "strokeWidth": width,
        "strokeStyle": "dashed" if dash else "solid",
        "roughness": rough,
        "opacity": 100,
        "roundness": roundness,
    }
    el.update(_defaults())
    return el


def _text_el(content: str, x: float, y: float, size: float, color: str = INK,
             container: str | None = None, align: str = "center") -> dict[str, Any]:
    el = {
        "id": _nid("t"),
        "type": "text",
        "x": round(x, 1),
        "y": round(y, 1),
        "width": round(estimate_text_width(content, size), 1),
        "height": round(estimate_text_height(content, size), 1),
        "angle": 0,
        "strokeColor": color,
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": 1,
        "strokeStyle": "solid",
        "roughness": 1,
        "opacity": 100,
        "roundness": None,
        "text": content,
        "rawText": content,
        "originalText": content,
        "fontSize": size,
        "fontFamily": FONT_FAMILY,
        "textAlign": align,
        "verticalAlign": "middle",
        "containerId": container,
        "autoResize": True,
        "lineHeight": 1.35,
    }
    el.update(_defaults())
    return el


def _free_center(cx: float, cy: float, content: str, size: float,
                 color: str = INK) -> dict[str, Any]:
    w = estimate_text_width(content, size)
    h = estimate_text_height(content, size)
    return _text_el(content, cx - w / 2, cy - h / 2, size, color)


def _free_left(x: float, y: float, content: str, size: float,
               color: str = INK) -> dict[str, Any]:
    return _text_el(content, x, y, size, color, align="left")


def _emoji_el(content: str, cx: float, cy: float, size: float) -> dict[str, Any]:
    """emoji 顶置元素：独立摆放，不参与容器文字行宽计算。"""
    return _free_center(cx, cy, content, size)


def _bound(box_el: dict[str, Any], content: str, size: float,
           color: str = INK, dy: float = 0.0) -> dict[str, Any]:
    """把文本绑定进容器并居中；容器加高让文字落在“中下区”。"""
    w = estimate_text_width(content, size)
    h = estimate_text_height(content, size)
    x = box_el["x"] + (box_el["width"] - w) / 2
    y = box_el["y"] + (box_el["height"] - h) / 2 + dy
    te = _text_el(content, x, y, size, color, container=box_el["id"])
    box_el["boundElements"].append({"id": te["id"], "type": "text"})
    return te


def _arrow(x1: float, y1: float, x2: float, y2: float, color: str = EDGE,
           sw: float | None = None, rough: float = 1.8, end: bool = True,
           dash: bool = False, *, tier: str = "subtitle") -> dict[str, Any]:
    """箭头：线宽取档（默认真文档 = 小标题档）。"""
    width = tier_stroke(tier) if sw is None else float(sw)
    x = min(x1, x2)
    y = min(y1, y2)
    el = {
        "id": _nid("a"),
        "type": "arrow",
        "x": round(x, 1),
        "y": round(y, 1),
        "width": round(abs(x2 - x1), 1),
        "height": round(abs(y2 - y1), 1),
        "angle": 0,
        "strokeColor": color,
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": width,
        "strokeStyle": "dashed" if dash else "solid",
        "roughness": rough,
        "opacity": 100,
        "roundness": {"type": 2},
        "points": [
            [round(x1 - x, 1), round(y1 - y, 1)],
            [round(x2 - x, 1), round(y2 - y, 1)],
        ],
        "startArrowhead": None,
        "endArrowhead": "arrow" if end else None,
        "elbowed": False,
    }
    el.update(_defaults())
    return el


def _line(x1: float, y1: float, x2: float, y2: float, color: str = EDGE,
          sw: float | None = None, rough: float = 1.4, dash: bool = False,
          *, tier: str = "body") -> dict[str, Any]:
    """直线/虚线：线宽取档（默认正文档；装饰性虚线用注释档）。"""
    width = tier_stroke(tier) if sw is None else float(sw)
    x = min(x1, x2)
    y = min(y1, y2)
    el = {
        "id": _nid("l"),
        "type": "line",
        "x": round(x, 1),
        "y": round(y, 1),
        "width": round(abs(x2 - x1), 1),
        "height": round(abs(y2 - y1), 1),
        "angle": 0,
        "strokeColor": color,
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": width,
        "strokeStyle": "dashed" if dash else "solid",
        "roughness": rough,
        "opacity": 100,
        "roundness": None,
        "points": [
            [round(x1 - x, 1), round(y1 - y, 1)],
            [round(x2 - x, 1), round(y2 - y, 1)],
        ],
        "startArrowhead": None,
        "endArrowhead": None,
        "elbowed": False,
    }
    el.update(_defaults())
    return el

def _finalize(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for i, el in enumerate(elements):
        el["index"] = f"a{i}"
    return elements


def _card_paint(pal: FigurePalette, slot: int, i: int) -> tuple[str, str]:
    """卡片填充 / 描边：**同一个色相**的两个浅档交替，描边 = 主题色原样（不再彩虹轮换）。"""
    return pal.fill(slot, i), pal.base(slot)


def _text_lines(text: str) -> list[str]:
    return [ln for ln in text.split("\n") if ln != ""]


def _header(scene: dict, pal: FigurePalette, slot: int,
            emoji: str = DEFAULT_HEAD) -> list[dict[str, Any]]:
    """顶置标题条：左侧标题图标（缺省 📘）+ 标题卡片（主题主色家族）。"""
    title = scene.get("title") or ""
    if not title:
        return []
    fam = pal.family(slot)
    fill, stroke, tcol = fam["fill"], fam["stroke"], fam["tcol"]
    # 标题档字号变大（24 → FONT_TITLE），标题条的**外框宽度保持原样**（内边距 200 → 110 抵消），
    # 这样画布跨度不因标题变大而变宽（跨度一变，全图等效磅数就跟着掉）。
    w = max(420.0, estimate_text_width(title, FONT_TITLE) + 110.0)
    b = _box("rectangle", 176, 40, w, 90, fill, stroke, tier="title")
    els = [_emoji_el(scene.get("emoji") or emoji, 64, 78, ICON_HERO), b]
    els.append(_bound(b, title, FONT_TITLE, tcol))
    return els


def _tip_bar(tip: str, tip_emoji: str, x: float, y: float,
             total_w: float | None = None, *,
             pal: FigurePalette, slot: int) -> list[dict[str, Any]]:
    """底部小提示条：左 emoji + 居中文字（主题**强调色**的最浅一档底色）。"""
    fill, stroke, tcol = pal.tip(slot), pal.base(slot), pal.deep(slot)
    tw = estimate_text_width(tip, FONT_BODY)
    w = max(900.0, tw + 320.0)
    if total_w is not None:
        x = max(20.0, (total_w - w) / 2)
    h = max(100.0, estimate_text_height(tip, FONT_BODY) + 70)
    b = _box("rectangle", x, y, w, h, fill, stroke, tier="body")
    els = [b]
    if tip_emoji:
        els.append(_emoji_el(tip_emoji, x + 76, y + h / 2, ICON_TIP))
    els.append(_bound(b, tip, FONT_BODY, tcol))
    return els


# —— 图例条（任务 122）——
# 研究结论（素材 C5 好图五要素 / C7 图例与约定先行）：**图例最常被忽略** —— 用到多色 /
# 多形状的版式要在边上补一行图例，说明每个颜色 / 形状代表什么。两条硬约束都是**既有口径**
# （不是本任务新加的规矩），图例的形态必须绕开它们：
#   ① 色块**不能**用 line / arrow 画：``tests/test_excalidraw_format.py::test_layout_arrows_are_bound``
#      要求 chain / radial / decision 里的全部 linear 元素都双向绑定（拖节点时箭头要跟着走），
#      装饰性线段会直接把它打红；
#   ② 色块**也不能**用矩形 / 椭圆（占单图图形上限 ``MAX_FIGURE_SHAPES``），或额外再加一个
#      text（6 节点 chain 的文本数正好 19，再加一个就 > ``MAX_FIGURE_TEXT_ELEMENTS``=20
#      ⇒ 触发超限拆图、页数上涨）。
# ⇒ 做法：**一个图例条目 = 一个 text 元素**，色块是行首的几何字符（■ / ▶），整行用它所代表的
#   颜色书写（色就在字上）—— 多色图的图例条本身就是一张配色说明，元素账只 +1 text / 条。
#   ③ 字符**必须是渲染字体真的有的字形**：色块字符原先取 U+25AC（▬），本机渲染字体
#      （NotoSansSC-VF）没有它，出图是一个「打叉方框」的豆腐块（实测 PNG 截图）⇒ 改用 U+25A0。
TIER_UNITS = tuple(tier_units(t) for t in FONT_TIERS)   # 四档源字号（由小到大；图例只许取档）
LEGEND_ROW_H = 62.0          # 图例条占的高度（版式据此留空档）
LEGEND_GLYPHS = {"line": "\u25a0", "arrow": "\u25b6", "none": ""}
LEGEND_GAP = 30.0            # 条目之间的间距


def _legend_font_units(ambient: list[dict[str, Any]] | None) -> float:
    """图例的字号：夹在小标题档 ~ 标题档之间，并抬到**全图被几何重排后的最小字号**。

    图例是自由文本，和全图别的自由文本一样会被 ``figure_png.reflow_text_elements`` 按几何
    抬字号；它自己贴着画布边沿排、又抬不动（放大后的外框会越出画布边沿，见 ``_free_scale``
    的 ±2 单位留边）⇒ 若沿用正文档，它就会成为**全图最小字号**（实测 mock 概念卡 chain
    六节点：9.00 磅被拉到 6.18 磅，掉出 9 磅护栏）⇒ ``ppt.media_tier_of`` 换档、要点进备注
    —— 那是**改既有版式的下游行为**，本任务明令不许。

    口径 = 「渲染时全图最小字号」（``figure_png.plan_render`` 的 ``text_px[0]``）的两半取大：
    ① 重排后的最小源字号（自由文本按几何抬到边界后的最小值）；② ``source_font_units_for_pt``
    按本图跨度反算的 9 磅下限（画布够宽时最小字号会被这一条托住）。下限小标题档。这样图例
    与既有文字**同字号**（并列最小、不会更小）⇒ 既有版式的最小等效磅数一个数字都不变（实测
    矩阵见 ``tests/test_figure_legend_quadrant.py``）。
    """
    els = list(ambient or [])
    reflowed = reflow_text_elements(els)
    sizes = [float(el.get("fontSize") or 0.0) for el in reflowed
             if str(el.get("type") or "") == "text" and str(el.get("text") or "").strip()]
    need = min(sizes) if sizes else FONT_SUBTITLE
    if els:
        x0 = min(float(el.get("x") or 0.0) for el in els)
        x1 = max(float(el.get("x") or 0.0) + float(el.get("width") or 0.0) for el in els)
        need = max(need, source_font_units_for_pt(max(1.0, x1 - x0)))
    # 取**档**：图内的字号只许落在四档上（``tests/test_figure_tiers.py`` 钉的口径）⇒ 要往上
    # 取到「不小于 need 的最小档」，全都装不下（need 比标题档还大）就取标题档 —— 图例宁可与
    # 标题同大，也不许比既有文字小（小了就是全图最小字号 ⇒ 拉低最小等效磅数 ⇒ 换档）。
    for units in TIER_UNITS:
        if units >= need:
            return units
    return TIER_UNITS[-1]


def _legend(entries: list[dict], x: float, y: float, *, pal: FigurePalette,
            slot: int, ambient: list[dict[str, Any]] | None = None,
            size: float | None = None) -> list[dict[str, Any]]:
    """图例条：一行「色块 + 说明」，左起排列（任务 122）。

    每项：``{"label": str}`` 必填；可选 ``color``（hex，缺省 = 主题主色）、``shape``
    （``"line"`` 缺省 / ``"arrow"`` / ``"none"`` = 只写文字）。**一个条目就是一个 text 元素**
    （色块 = 行首的几何字符 ■ / ▶，整行按 ``color`` 上色，色就在字上）—— 为什么不用真正的
    线段 / 矩形当色块，见上方两条硬约束。返回元素列表供调用方 extend。

    ``ambient`` = 本图**已有**的元素（不含图例）；字号缺省由 :func:`_legend_font_units` 按
    它反算（``size`` 只在测试/对照时显式给）。调用方请把图例留到**最后**再 extend，别漏掉
    提示条那类后加的文本。
    """
    if size is None:
        size = _legend_font_units(ambient)
    els: list[dict[str, Any]] = []
    cx = float(x)
    for entry in entries:
        label = str(entry.get("label") or "").strip()
        shape = str(entry.get("shape") or "line").strip() or "line"
        color = str(entry.get("color") or pal.base(slot))
        glyph = LEGEND_GLYPHS.get(shape, LEGEND_GLYPHS["line"])
        text_line = f"{glyph} {label}".strip() if glyph else label
        if not text_line:
            continue
        el = _free_left(cx, y + 20.0 - estimate_text_height(text_line, size) / 2,
                        text_line, size, color)
        els.append(el)
        cx += el["width"] + LEGEND_GAP
    return els


def _extent(els: list[dict[str, Any]]) -> tuple[float, float, float, float]:
    """元素集合的包围盒（只算 x / y / width / height，与 ``validate_elements`` 同一把尺子）。"""
    x0 = min(float(e.get("x") or 0.0) for e in els)
    y0 = min(float(e.get("y") or 0.0) for e in els)
    x1 = max(float(e.get("x") or 0.0) + float(e.get("width") or 0.0) for e in els)
    y1 = max(float(e.get("y") or 0.0) + float(e.get("height") or 0.0) for e in els)
    return round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)


def _legend_in_place(entries: list[dict], x: float, y: float, *, pal: FigurePalette,
                     slot: int, ambient: list[dict[str, Any]] | None = None
                     ) -> list[dict[str, Any]]:
    """只在**既有空档**里画图例；放不下就**不画**（任务 122 的硬规则：只新增、不改既有行为）。

    两条判据（缺一条就不画）：① 落点不撑大既有包围盒 —— 画布一变，``fit_display_width_in``
    出来的等效磅数就跟着变 ⇒ ``ppt.media_tier_of`` 可能换档、要点进备注；② 不新增
    ``validate_elements`` 的 text∩text / 越界。放不下时**宁可少画这一条图例**，也不许挪动
    既有元素 / 撑大画布。

    真实生成链路不会走到「不画」这一支：图注 ``caption`` 永远有值（``_figure_caption`` 兜底
    「图」），图例都落在标题条下方那条空档里；无标题 / 带副标题的场景只可能来自存量 / 手写
    数据（提示词里没有 subtitle 字段）。边界矩阵见 ``tests/test_figure_legend_quadrant.py``。
    """
    els = _legend(entries, x, y, pal=pal, slot=slot, ambient=ambient)
    plain = list(ambient or [])
    if not els or not plain:
        return els
    if _extent(plain + els) != _extent(plain):
        return []
    before = validate_elements(plain)
    after = validate_elements(plain + els)
    if (len(after["text_overlaps"]) > len(before["text_overlaps"])
            or len(after["overflow"]) > len(before["overflow"])):
        return []
    return els


def layout_chain(scene: dict, pal: FigurePalette) -> list[dict[str, Any]]:
    """手绘链条 · 一图看懂：从左到右步骤卡，emoji 顶置 + 箭头 + 小提示。"""
    nodes = scene.get("nodes") or []
    if not nodes:
        raise ValueError("chain 布局至少需要 1 个节点")
    main = slot_of("chain")
    els = _header(scene, pal, main)
    y = 250 if scene.get("title") else 60
    width_slots: list[tuple[float, float]] = []
    x = 20.0
    for node in nodes:
        text = node.get("text") or ""
        w = max(200.0, estimate_text_width(text, FONT_SUBTITLE) + 44)
        width_slots.append((x, w))
        x += w + 76
    edge_labels = scene.get("edge_labels") or []
    for i, node in enumerate(nodes):
        px, w = width_slots[i]
        fill, stroke = _card_paint(pal, main, i)
        b = _box("rectangle", px, y, w, 240, fill, stroke)
        els.append(b)
        emoji = node.get("emoji")
        if emoji:
            els.append(_emoji_el(str(emoji), px + w / 2, y + 50, ICON_CARD))
        els.append(_bound(b, node.get("text") or "", FONT_SUBTITLE, INK_DARK))
    ay = y + 120
    for i in range(len(width_slots) - 1):
        x1, w1 = width_slots[i]
        x2, _ = width_slots[i + 1]
        els.append(_arrow(x1 + w1, ay, x2, ay))
        if i < len(edge_labels) and edge_labels[i]:
            els.append(_free_center((x1 + w1 + x2) / 2, ay - 34,
                                    str(edge_labels[i]), FONT_BODY, MUTED))
    tip = scene.get("tip")
    if tip:
        els.extend(_tip_bar(str(tip), scene.get("tip_emoji") or "\U0001F4A1",
                            20, y + 240 + 60,
                            pal=pal, slot=main + ACCENT_OFFSET))
    # 图例条（任务 122）：有标题时落在标题条下方的空档（y=140，**在既有包围盒之内** ⇒ 画布
    # 宽高一个单位都不涨）；**无标题时内容从 y=60 起**、只有上沿 0~60 是空档 ⇒ 图例放 y=5
    # （探针实测：无标题仍放 y=140 会与第一行卡片文字 text∩text 交叠 343.9 平方单位）。
    # 放在提示条**之后**：字号要按全图（含提示条）重排后的最小字号算。
    els.extend(_legend_in_place(
        [{"shape": "arrow", "color": EDGE, "label": "箭头 = 步骤先后顺序"}],
        width_slots[0][0], 140.0 if scene.get("title") else 5.0,
        pal=pal, slot=main, ambient=els))
    return _finalize(els)


def _comic_panel(panel: dict, px: float, py: float, pw: float, bubble_w: float,
                 pal: FigurePalette, slot: int) -> list[dict[str, Any]]:
    lines = panel.get("lines") or _text_lines(panel.get("text") or "")
    if not lines:
        raise ValueError("comic 每格需要 lines 或 text")
    els: list[dict[str, Any]] = []
    b = _box("rectangle", px, py, pw, 350, "#ffffff", pal.soft(slot),
             rough=1.4, tier="body")
    els.append(b)
    cap = panel.get("caption")
    if cap:
        els.append(_free_left(px + 30, py + 28, str(cap), FONT_BODY, MUTED))
    emoji = panel.get("emoji")
    if emoji:
        els.append(_emoji_el(str(emoji), px + 150, py + 195, ICON_ROOT))
    bubble = _box("rectangle", px + 258, py + 78, bubble_w, 228,
                  "#f8fafc", EDGE, rough=1.8)
    els.append(bubble)
    els.append(_bound(bubble, "\n".join(lines), FONT_SUBTITLE, INK_DARK))
    if emoji:
        els.append(_line(px + 204, py + 215, px + 256, py + 192,
                         EDGE, rough=1.8))
    return els


def layout_comic(scene: dict, pal: FigurePalette) -> list[dict[str, Any]]:
    """四格漫画：2×2 分镜，左侧角色 emoji + 右侧对白气泡。"""
    main = slot_of("comic")
    panels = scene.get("panels") or scene.get("nodes") or []
    if not panels or len(panels) > 4:
        raise ValueError("comic 布局需要 1~4 格分镜")
    bubble_w = 300.0
    for panel in panels:
        lines = panel.get("lines") or _text_lines(panel.get("text") or "")
        joined = "\n".join(lines)
        bubble_w = max(bubble_w, estimate_text_width(joined, FONT_SUBTITLE) + 44)
    pw = max(580.0, 258 + bubble_w + 40)
    gap = 20.0
    mx = 40.0
    total_w = mx * 2 + pw * 2 + gap
    title = scene.get("title")
    subtitle = scene.get("subtitle") or scene.get("caption") or ""
    y0 = 150.0 if (title or subtitle) else 60.0
    els: list[dict[str, Any]] = []
    if title:
        els.append(_free_center(total_w / 2, 52, str(title), FONT_TITLE, INK_DARK))
    if subtitle:
        els.append(_free_center(total_w / 2, 108, str(subtitle), FONT_BODY, MUTED))
    for i, panel in enumerate(panels):
        col, row = i % 2, i // 2
        px = mx + col * (pw + gap)
        py = y0 + row * (350 + gap)
        els.extend(_comic_panel(panel, px, py, pw, bubble_w, pal, main))
    return _finalize(els)

def _card_body(els: list[dict[str, Any]], card: dict, x: float, y: float,
               w: float, h: float, i: int, *, pal: FigurePalette, slot: int,
               subs_top: float = 156.0,
               emoji_size: float = ICON_CARD, title_size: float = FONT_SUBTITLE,
               subs_size: float = FONT_BODY, title_color: str = INK_DARK,
               line_step: float | None = None) -> None:
    """卡片通用排版：emoji 顶置，标题居中，副行自由居中在卡片中下部。"""
    fill, stroke = _card_paint(pal, slot, i)
    if card.get("fill") and card.get("stroke"):
        fill, stroke = str(card["fill"]), str(card["stroke"])
    b = _box("rectangle", x, y, w, h, fill, stroke)
    els.append(b)
    emoji = card.get("emoji")
    if emoji:
        els.append(_emoji_el(str(emoji), x + w / 2, y + 52, emoji_size))
    title = card.get("title") or card.get("text") or ""
    subs = card.get("subs") or []
    if title:
        els.append(_free_center(x + w / 2, y + subs_top - 38, str(title),
                                title_size, title_color))
    step = line_step or subs_size * 1.75
    cy = y + subs_top
    for ln in subs:
        els.append(_free_center(x + w / 2, cy, str(ln), subs_size, MUTED))
        cy += step


def _card_need(card: dict, *, title_size: float = FONT_SUBTITLE,
               subs_size: float = FONT_BODY, min_w: float = 320.0) -> tuple[float, float]:
    title = card.get("title") or card.get("text") or ""
    subs = card.get("subs") or []
    w = max(min_w,
            estimate_text_width(str(title), title_size) + 48,
            *(estimate_text_width(str(s), subs_size) + 48 for s in subs))
    h = max(215.0, 150.0 + len(subs) * 32.0)
    return w, h


def layout_radial(scene: dict, pal: FigurePalette) -> list[dict[str, Any]]:
    """中心辐射 · 三要素：中心椭圆 + 四周要素卡 + 连线箭头。"""
    center = scene.get("center") or {}
    cards = scene.get("cards") or []
    if not cards:
        raise ValueError("radial 布局至少需要 1 张要素卡")
    main = slot_of("radial")
    els = _header(scene, pal, main)
    k = len(cards)
    card_w = max(_card_need(c)[0] for c in cards)
    card_h = max(_card_need(c)[1] for c in cards)
    pitch = card_w + 70
    mx = 60.0
    total_w = mx * 2 + (k - 1) * pitch + card_w
    cx0 = total_w / 2
    cy0 = 340.0 if scene.get("title") else 260.0
    card_top = cy0 + 280
    core = _box("ellipse", cx0 - 155, cy0 - 125, 310, 250,
                center.get("fill") or pal.fill(main, 1),
                center.get("stroke") or pal.base(main))
    els.append(core)
    cemo = center.get("emoji")
    ctext = center.get("text") or ""
    if ctext and estimate_text_width(ctext, FONT_SUBTITLE) > 250:
        raise ValueError("radial 中心文字过长（≤14 字）")
    if cemo:
        els.append(_emoji_el(str(cemo), cx0, cy0 - 40, ICON_HERO))
    if ctext:
        els.append(_free_center(cx0, cy0 + (40 if cemo else 0), ctext,
                                FONT_SUBTITLE, pal.deep(main)))
    for i, card in enumerate(cards):
        px = mx + i * pitch
        _card_body(els, card, px, card_top, card_w, card_h, i, pal=pal, slot=main)
        els.append(_arrow(cx0, cy0 + 135, px + card_w / 2, card_top - 10,
                          rough=1.8))
    banner = scene.get("banner")
    if banner:
        text = banner.get("text") or ""
        top = card_top + card_h + 60
        w = max(900.0, estimate_text_width(text, FONT_SUBTITLE) + 320)
        x = max(20.0, (total_w - w) / 2)
        bfam = pal.family(main + ACCENT_OFFSET)
        b = _box("rectangle", x, top, w, 90, bfam["fill"], bfam["stroke"])
        els.append(b)
        if banner.get("emoji"):
            els.append(_emoji_el(str(banner["emoji"]), x + 76, top + 45, ICON_TIP))
        els.append(_bound(b, text, FONT_SUBTITLE, bfam["tcol"]))
    return _finalize(els)


def layout_ring(scene: dict, pal: FigurePalette) -> list[dict[str, Any]]:
    """环形布局 · 面粉家族：中心总结 + 四角成员卡 + 虚线连接。"""
    center = scene.get("center") or {}
    cards = scene.get("cards") or scene.get("nodes") or []
    if not cards or len(cards) > 4:
        raise ValueError("ring 布局需要 1~4 张成员卡")
    main = slot_of("ring")
    els = _header(scene, pal, main)
    pw, ph = 320.0, 240.0
    max_subs = max(len(c.get("subs") or []) for c in cards)
    if max_subs > 2:
        ph = max(240.0, 172 + (max_subs - 1) * 28 + 60)
    for card in cards:
        title = card.get("title") or card.get("text") or ""
        pw = max(pw, estimate_text_width(str(title), FONT_SUBTITLE) + 48)
        for s in card.get("subs") or []:
            pw = max(pw, estimate_text_width(str(s), FONT_BODY) + 48)
    mx = 40.0
    right_x = max(1040.0, mx + pw + 560.0)
    cx0 = (mx + pw + right_x) / 2
    row1_y = 150.0
    cy0 = row1_y + ph + 140
    row2_y = cy0 + 230
    slots = [(mx, row1_y), (right_x, row1_y), (mx, row2_y), (right_x, row2_y)]
    if center:
        ctext = center.get("text") or ""
        if ctext and estimate_text_width(ctext, FONT_SUBTITLE) > 250:
            raise ValueError("ring 中心文字过长（≤16 字）")
        core = _box("ellipse", cx0 - 140, cy0 - 110, 280, 220,
                    center.get("fill") or pal.fill(main, 1),
                    center.get("stroke") or pal.base(main))
        els.append(core)
        if center.get("emoji"):
            els.append(_emoji_el(str(center["emoji"]), cx0, cy0 - 44, ICON_HERO))
        if ctext:
            els.append(_free_center(cx0, cy0 + (56 if center.get("emoji") else 0),
                                    ctext, FONT_SUBTITLE, pal.deep(main)))
    corners = [(cx0 - 140, cy0 - 110), (cx0 + 140, cy0 - 110),
               (cx0 - 140, cy0 + 110), (cx0 + 140, cy0 + 110)]
    for i, card in enumerate(cards):
        x, y = slots[i]
        _card_body(els, card, x, y, pw, ph, i, pal=pal, slot=main, subs_top=172,
                   emoji_size=ICON_CARD, subs_size=FONT_BODY)
        sx, sy = corners[i]
        ex = x + pw / 2
        ey = y + ph if i < 2 else y
        els.append(_line(sx, sy, ex, ey, "#cbd5e1", rough=1.6, dash=True,
                         tier="caption"))
    return _finalize(els)


def layout_timeline(scene: dict, pal: FigurePalette) -> list[dict[str, Any]]:
    """时间轴：水平轴线 + 编号圆点 + 步骤卡 + 底部小提示。"""
    steps = scene.get("steps") or scene.get("nodes") or []
    n = len(steps)
    if not n or n > 6:
        raise ValueError("timeline 布局需要 1~6 步")
    main = slot_of("timeline")
    els = _header(scene, pal, main)
    title = scene.get("title")
    axis_y = 280.0 if title else 160.0
    card_w = _timeline_width(steps)
    subtitle = scene.get("subtitle")
    if subtitle:
        total_known = 40 * 2 + (n - 1) * (card_w + 24) + card_w
        els.append(_free_center(total_known / 2, 158, str(subtitle), FONT_BODY, MUTED))
    max_subs = max(len(s.get("subs") or []) for s in steps)
    card_h = max(230.0, 145 + max_subs * 29)
    spacing = card_w + 24
    mx = 40.0
    centers = [mx + card_w / 2 + i * spacing for i in range(n)]
    total_w = mx * 2 + (n - 1) * spacing + card_w
    card_top = axis_y + 60
    els.append(_line(mx - 20, axis_y, total_w - mx + 20, axis_y,
                     "#e2e8f0", rough=0.6, tier="title"))
    for i, step in enumerate(steps):
        cx = centers[i]
        scol = pal.base(main)
        if step.get("stroke"):
            scol = str(step["stroke"])
        b = _box("rectangle", cx - card_w / 2, card_top, card_w, card_h,
                 "#ffffff", scol)
        els.append(b)
        if step.get("emoji"):
            els.append(_emoji_el(str(step["emoji"]), cx, card_top + 52, ICON_TIP))
        title_text = step.get("title") or step.get("text") or ""
        if title_text:
            els.append(_free_center(cx, card_top + 118, title_text, FONT_SUBTITLE, INK_DARK))
        subs = step.get("subs") or []
        sub_top = card_h - (len(subs) - 1) * 27 - 40
        cy = card_top + sub_top
        for ln in subs:
            els.append(_free_center(cx, cy, str(ln), FONT_BODY, MUTED))
            cy += 27
        dot = _box("ellipse", cx - 17, axis_y - 17, 34, 34, scol, scol,
                   rough=1, tier="body")
        els.append(dot)
        els.append(_bound(dot, CIRCLED[i], FONT_BODY, "#ffffff"))
        els.append(_line(cx, axis_y + 17, cx, card_top, EDGE, rough=1.2))
        if i < n - 1:
            els.append(_arrow(cx + 19, axis_y, centers[i + 1] - 19, axis_y,
                              rough=1.2))
    tip = scene.get("tip")
    content_bottom = card_top + card_h
    if tip:
        els.extend(_tip_bar(str(tip), scene.get("tip_emoji") or "\U0001F95F",
                            150, content_bottom + 40, total_w=total_w,
                            pal=pal, slot=main + ACCENT_OFFSET))
        content_bottom += 40 + max(100.0, estimate_text_height(str(tip), FONT_BODY) + 70)
    # 图例条（任务 122）：标题条下方的空档；有副标题占掉空档时改放内容下沿 ——
    # **再往下让开提示条**（真课样图「六步时间轴」就是「副标题 + 提示条」同时在，
    # 图例若压在提示条上会被 validate_elements 判成「文字穿出未绑定的框」）。
    _lg_entries = [{"shape": "none", "label": "圆点序号 = 阶段先后"},
                   {"shape": "arrow", "color": EDGE, "label": "箭头 = 时间推进方向"}]
    _lg_y = (content_bottom + 24) if subtitle else (140.0 if title else 40.0)
    els.extend(_legend_in_place(_lg_entries, mx, _lg_y, pal=pal, slot=main, ambient=els))
    return _finalize(els)


def _timeline_width(steps: list[dict]) -> float:
    w = 200.0
    for s in steps:
        title = s.get("title") or s.get("text") or ""
        w = max(w, estimate_text_width(str(title), FONT_SUBTITLE) + 44)
        for sub in s.get("subs") or []:
            w = max(w, estimate_text_width(str(sub), FONT_BODY) + 44)
    return w

def _compare_col(els: list[dict[str, Any]], col: dict, x: float, y0: float,
                 colw: float, colh: float, pal: FigurePalette, slot: int) -> None:
    # 场景可以用 ``family="accent"`` 指定这一栏用**强调色**（任务 121；不写就用主色）
    if str(col.get("family") or "") == "accent":
        slot += ACCENT_OFFSET
    fam = pal.family(slot)
    fill = col.get("fill") or fam["fill"]
    stroke = col.get("stroke") or fam["stroke"]
    soft = col.get("soft") or fam["soft"]
    tcol = col.get("tcol") or fam["tcol"]
    b = _box("rectangle", x, y0, colw, colh, fill, stroke)
    els.append(b)
    if col.get("emoji"):
        els.append(_emoji_el(str(col["emoji"]), x + 150, y0 + 96, ICON_ROOT))
    bubble_txt = col.get("bubble") or col.get("text") or ""
    bubble_w = max(300.0, estimate_text_width(bubble_txt, FONT_BODY) + 48)
    bubble = _box("rectangle", x + 270, y0 + 32, bubble_w, 128,
                  "#ffffff", soft, rough=1.8)
    els.append(bubble)
    if bubble_txt:
        els.append(_bound(bubble, bubble_txt, FONT_BODY, tcol))
    cap = col.get("name") or col.get("title") or ""
    if cap:
        els.append(_free_center(x + 150, y0 + 190, cap, FONT_SUBTITLE, tcol))
    els.append(_line(x + 24, y0 + 220, x + colw - 24, y0 + 220,
                     soft, rough=1))
    rows = col.get("rows") or []
    ry = y0 + 236
    for i, row in enumerate(rows):
        label = str(row.get("label") or row.get("title") or "")
        value = str(row.get("value") or row.get("text") or "")
        chip_w = max(92.0, estimate_text_width(label, FONT_CAPTION) + 20)
        chip = _box("rectangle", x + 24, ry - 15, chip_w, 30,
                    col.get("chipfill") or fam["chipfill"], soft, rough=1.6, tier="caption")
        els.append(chip)
        if label:
            els.append(_bound(chip, label, FONT_CAPTION, tcol))
        if value:
            els.append(_free_left(x + 140, ry - 12, value, FONT_BODY, INK))
        ry += 46


def layout_compare(scene: dict, pal: FigurePalette) -> list[dict[str, Any]]:
    """对比分栏：左右两栏 + 头像气泡 + 属性行 + 中间 VS（左 = 主色，右 = 强调色）。"""
    cols = scene.get("columns") or scene.get("nodes") or []
    if len(cols) != 2:
        raise ValueError("compare 布局需要左右两栏")
    main = slot_of("compare")
    els = _header(scene, pal, main)
    subtitle = scene.get("subtitle") or scene.get("caption") or ""
    specs: list[tuple[float, float]] = []
    for col in cols:
        bubble_txt = col.get("bubble") or col.get("text") or ""
        max_val = max((estimate_text_width(str(r.get("value") or r.get("text") or ""), FONT_BODY)
                       for r in col.get("rows") or []), default=0.0)
        colw = max(600.0, 270 + estimate_text_width(bubble_txt, FONT_BODY) + 60,
                   140 + max_val + 60)
        n_rows = len(col.get("rows") or [])
        colh = max(430.0, 316 + (n_rows - 1) * 46)
        specs.append((colw, colh))
    x1 = 70.0
    total_w = x1 + specs[0][0] + 60 + specs[1][0] + 70
    if subtitle:
        els.append(_free_center(total_w / 2, 158, subtitle, FONT_BODY, MUTED))
    y0 = 200.0
    _compare_col(els, cols[0], x1, y0, specs[0][0], specs[0][1], pal, main)
    x2 = x1 + specs[0][0] + 60
    _compare_col(els, cols[1], x2, y0, specs[1][0], specs[1][1], pal,
                 main + ACCENT_OFFSET)
    vfam = pal.family(main)
    vs = _box("ellipse", x1 + specs[0][0] + 2, y0 + 194, 56, 56,
              vfam["fill"], vfam["stroke"], rough=1.4)
    els.append(vs)
    els.append(_bound(vs, "VS", FONT_SUBTITLE, vfam["tcol"]))
    # 图例条（任务 122）：两栏的**主色 / 强调色**在这里第一次被说明（左 = 主色、右 = 强调色）。
    # 放 y=132（中心 152）：栏从 y0=200 起 ⇒ 在空档里、包围盒不变（有副标题时改放内容下沿）。
    left_name = str(cols[0].get("name") or cols[0].get("title") or "左栏")
    right_name = str(cols[1].get("name") or cols[1].get("title") or "右栏")
    _lg_entries = [{"color": pal.base(main), "label": f"{left_name}（主色）"},
                   {"color": pal.base(main + ACCENT_OFFSET),
                    "label": f"{right_name}（强调色）"}]
    _lg_y = (y0 + max(specs[0][1], specs[1][1]) + 24) if subtitle else 132.0
    els.extend(_legend_in_place(_lg_entries, x1, _lg_y, pal=pal, slot=main, ambient=els))
    return _finalize(els)


def layout_decision(scene: dict, pal: FigurePalette) -> list[dict[str, Any]]:
    """决策流程：起点 → 判断菱形（否→左侧提示）→ 好/坏出口 + 口诀条。

    色相：主色 = 起点 / 否出口，强调色 = 判断菱形 / 通过出口，第三色 = 不通过出口
    （三色法上限；换主题时三个色相一起换）。"""
    checks = scene.get("checks") or []
    start_text = scene.get("start") or ""
    good = scene.get("good") or ""
    bad = scene.get("bad") or ""
    if not checks or not good or not bad:
        raise ValueError("decision 需要 start/checks/good/bad")
    main = slot_of("decision")
    accent = main + ACCENT_OFFSET
    third = main + THIRD_OFFSET
    els = _header(scene, pal, main)
    canvas_w = 1400.0
    cx = canvas_w / 2
    top0 = 250.0 if scene.get("title") else 160.0
    sw_start = max(300.0, estimate_text_width(start_text, FONT_SUBTITLE) + 60)
    mfam = pal.family(main)
    afam = pal.family(accent)
    start = _box("rectangle", cx - sw_start / 2, top0, sw_start, 74,
                 mfam["fill"], mfam["stroke"])
    els.append(start)
    if start_text:
        els.append(_bound(start, start_text, FONT_SUBTITLE, mfam["tcol"]))
    dh = 140.0
    row_y = top0 + 74 + 46
    diamond_w = max(300.0,
                    *(estimate_text_width(str(c.get("q") or c.get("question") or ""),
                                          FONT_SUBTITLE) + 60
                      for c in checks))
    diamond_xs = []
    for i, check in enumerate(checks):
        q = str(check.get("q") or check.get("question") or "")
        d = _box("diamond", cx - diamond_w / 2, row_y + i * 216, diamond_w, dh,
                 afam["fill"], afam["stroke"])
        els.append(d)
        if q:
            els.append(_bound(d, q, FONT_SUBTITLE, afam["tcol"]))
        diamond_xs.append((d["x"], d["width"]))
    els.append(_arrow(cx, top0 + 74, cx, row_y, rough=1.6))
    for i, check in enumerate(checks):
        qx, qw = diamond_xs[i]
        cy = row_y + i * 216
        no_text = check.get("no") or ""
        if i < len(checks) - 1:
            if no_text:
                nw = max(300.0, estimate_text_width(no_text, FONT_BODY) + 60)
                nh = max(140.0, estimate_text_height(no_text, FONT_BODY) + 60)
                nb = _box("rectangle", 60, cy, nw, nh, mfam["fill"], mfam["stroke"])
                els.append(nb)
                els.append(_bound(nb, no_text, FONT_BODY, mfam["tcol"]))
                els.append(_arrow(qx, cy + dh / 2, 60 + nw, cy + dh / 2,
                                  rough=1.6))
            els.append(_arrow(cx, cy + dh, cx, cy + 216, rough=1.6))
        else:
            gfam, bfam = pal.family(accent), pal.family(third)
            gw = max(300.0, estimate_text_width(good, FONT_BODY) + 60)
            bw = max(300.0, estimate_text_width(bad, FONT_BODY) + 60)
            gh = max(150.0, estimate_text_height(good, FONT_BODY) + 60)
            bh = max(150.0, estimate_text_height(bad, FONT_BODY) + 60)
            gb = _box("rectangle", 60, cy, gw, gh, gfam["fill"], gfam["stroke"])
            els.append(gb)
            els.append(_bound(gb, good, FONT_BODY, gfam["tcol"]))
            els.append(_arrow(qx, cy + dh / 2, 60 + gw, cy + dh / 2,
                              rough=1.6))
            bb = _box("rectangle", canvas_w - 60 - bw, cy, bw, bh,
                      bfam["fill"], bfam["stroke"])
            els.append(bb)
            els.append(_bound(bb, bad, FONT_BODY, bfam["tcol"]))
            els.append(_arrow(qx + qw, cy + dh / 2, canvas_w - 60 - bw,
                              cy + dh / 2, rough=1.6))
    tip = scene.get("tip")
    if tip:
        els.extend(_tip_bar(str(tip), scene.get("tip_emoji") or "\U0001F3AF",
                            140, row_y + (len(checks) - 1) * 216 + dh + 60,
                            pal=pal, slot=accent))
    # 图例条（任务 122）：决策图纵向很长，图例放在标题条下方的空档（不撑高画布 ⇒ 磅数零回归）。
    # 放在最后：图例字号要按**全图**（含提示条）重排后的最小字号算（见 _legend_font_units）。
    els.extend(_legend_in_place(
        [{"color": pal.base(accent), "label": "条件满足 ⇒ 通过"},
         {"color": pal.base(third), "label": "条件不满足 ⇒ 不通过"}],
        60.0, top0 - 110.0, pal=pal, slot=main, ambient=els))
    return _finalize(els)


def _mindmap_branch_need(branch: dict) -> tuple[float, float]:
    """思维导图分支盒尺寸：标题 + 最多 2 条要点。"""
    title = str(branch.get("title") or "")
    subs = [str(s) for s in (branch.get("subs") or [])][:2]
    lines = [title] + subs
    size = FONT_BODY
    w = max(300.0, *(estimate_text_width(ln, size) + 56 for ln in lines))
    h = max(120.0, estimate_text_height("\n".join(lines), size) + 52)
    return w, h


def layout_mindmap(scene: dict, pal: FigurePalette) -> list[dict[str, Any]]:
    """思维导图（左根水平树）：根节点靠左，一级分支自上而下排列，无箭头连线。"""
    root = scene.get("root") or {}
    branches = (scene.get("branches") or scene.get("nodes") or [])[:6]
    if not branches:
        raise ValueError("mindmap 布局至少需要 1 个分支")
    main = slot_of("mindmap")
    els = _header(scene, pal, main)
    root_text = str(root.get("title") or "").strip() or str(scene.get("title") or "主题")
    rx = 120.0
    root_w = max(360.0, estimate_text_width(root_text, FONT_SUBTITLE) + 200.0)
    root_h = 150.0
    specs = [_mindmap_branch_need(b) for b in branches]
    gaps = 46.0
    total_h = sum(h for _, h in specs) + gaps * (len(branches) - 1)
    y0 = 190.0 if scene.get("title") else 80.0
    bx = rx + root_w + 110.0
    root_y = y0 + (total_h - root_h) / 2
    rb = _box("rectangle", rx, root_y, root_w, root_h, pal.fill(main, 1),
              pal.base(main), rough=1.6)
    els.append(rb)
    remoji = str(root.get("emoji") or "\U0001F331").strip()
    if remoji:
        els.append(_emoji_el(remoji, rx + 86, root_y + root_h / 2, ICON_HERO))
    rt_w = estimate_text_width(root_text, FONT_SUBTITLE)
    els.append(_free_center(rx + 96 + (root_w - 96 - rt_w) / 2 + rt_w / 2,
                            root_y + root_h / 2, root_text, FONT_SUBTITLE,
                            pal.deep(main)))
    ry = y0
    for i, branch in enumerate(branches):
        w, h = specs[i]
        fill, stroke = _card_paint(pal, main, i)
        b = _box("rectangle", bx, ry, w, h, fill, stroke, rough=1.6)
        els.append(b)
        emoji = str(branch.get("emoji") or "").strip()
        if emoji:
            # 图标压在分支盒顶部（原 42 会把三行文字的第一行压住 —— 存量 mindmap 的 text∩text
            # 就是这么来的：63 行分支盒里图标占 39、正文 53，图标必须让出上半区）
            els.append(_emoji_el(emoji, bx + w / 2, ry + 28, ICON_BADGE))
        title = str(branch.get("title") or "")
        subs = [str(s) for s in (branch.get("subs") or [])][:2]
        content = "\n".join([ln for ln in ([title] + subs) if ln])
        if content:
            els.append(_bound(b, content, FONT_BODY, pal.deep(main), dy=20))
        els.append(_line(rx + root_w, root_y + root_h / 2, bx, ry + h / 2,
                         pal.soft(main), rough=1.2))
        ry += h + gaps
    return _finalize(els)


_LAYOUTS = {
    "chain": layout_chain,
    "comic": layout_comic,
    "radial": layout_radial,
    "ring": layout_ring,
    "timeline": layout_timeline,
    "compare": layout_compare,
    "decision": layout_decision,
    "mindmap": layout_mindmap,
}


def render(layout: str, scene: dict, *, theme: str | None = None) -> list[dict[str, Any]]:
    """按布局名排版场景，返回 Excalidraw 元素列表。

    ``theme`` 给 PPT 主题（id / 中文名，留空用后端 ``PPT_THEME`` 配置）—— 图内配色一律由它
    派生（:func:`app.services.figure_png.figure_palette`）。取色不参与任何几何计算 ⇒
    换主题只换颜色，字号 / 跨度 / 磅数一个都不动。
    """
    fn = _LAYOUTS.get(layout)
    if fn is None:
        raise ValueError(f"未知布局：{layout}（可选 {sorted(_LAYOUTS)}）")
    return fn(scene, figure_palette(theme))


def build_excalidraw_json(elements: list[dict[str, Any]],
                          background: str = BG) -> dict[str, Any]:
    """把元素列表包成合法 .excalidraw 文件结构。

    出口统一走 `normalize_document`：补齐插件规范要求的字段、把贴着图形的箭头与
    连线**双向绑定**（拖节点时箭头跟随），保证产物在 Obsidian Excalidraw 插件里
    可以直接双击编辑（M2）。
    """
    doc = {
        "type": "excalidraw",
        "version": 2,
        "source": "https://excalidraw.com",
        "elements": elements,
        "appState": {"gridSize": None, "viewBackgroundColor": background},
        "files": {},
    }
    return normalize_document(doc)


def layout_to_excalidraw_text(layout: str, scene: dict,
                              background: str | None = None, *,
                              theme: str | None = None) -> str:
    """排版并序列化为 .excalidraw 文件文本（画布底色同样从 ``theme`` 派生）。"""
    pal = figure_palette(theme)
    doc = build_excalidraw_json(render(layout, scene, theme=pal.theme),
                                background or pal.bg)
    return json.dumps(doc, ensure_ascii=False, indent=1)


def _bbox(el: dict[str, Any]) -> tuple[float, float, float, float]:
    return el["x"], el["y"], el.get("width", 0), el.get("height", 0)


def _intersection(a: tuple, b: tuple) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    w = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    h = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    return w * h


def _inside(a: tuple, b: tuple, tol: float = 2.0) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return (ax >= bx - tol and ay >= by - tol
            and ax + aw <= bx + bw + tol and ay + ah <= by + bh + tol)


def validate_elements(elements: list[dict[str, Any]]) -> dict[str, list]:
    """几何校验：text∩text 重叠、文字越界（镜像 P1 的 cg_overlap 检查）。"""
    texts = [el for el in elements if el["type"] == "text"]
    boxes = {el["id"]: el for el in elements
             if el["type"] in ("rectangle", "ellipse", "diamond")}
    overlaps: list[str] = []
    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            a, b = texts[i], texts[j]
            ia = _intersection(_bbox(a), _bbox(b))
            aa = _bbox(a)[2] * _bbox(a)[3]
            bb = _bbox(b)[2] * _bbox(b)[3]
            if ia > 8 and min(aa, bb) > 0 and ia / min(aa, bb) > 0.06:
                overlaps.append(f"{a['id']}({a['text'][:10]})×{b['id']}({b['text'][:10]})={ia:.1f}")
    overflow: list[str] = []
    for t in texts:
        tb = _bbox(t)
        for bid, box_el in boxes.items():
            if t.get("containerId") == bid:
                continue
            if _intersection(tb, _bbox(box_el)) > 6 and not _inside(tb, _bbox(box_el)):
                overflow.append(f"{t['id']}({t['text'][:10]}) 穿出 {bid}")
    return {"text_overlaps": overlaps, "overflow": overflow}
