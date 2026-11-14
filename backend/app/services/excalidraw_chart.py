# -*- coding: utf-8 -*-
"""教学数据图表 → Excalidraw 小图（柱状/折线/饼图/表格）。

把 /api/chart 生成的结构化数据画成“小而可读”的 Excalidraw 矢量小图：
bar = 竖条、line = 折线（支持多系列）、pie = 饼形比例（实心扇区 + 右侧图例）、
table = 表头 + 行列文本、quadrant = 两轴四象限（十字轴线 + 四张象限卡，任务 122）。
风格参数与 excalidraw_layout 同一套（**图内色从 PPT 主题派生**、
手写体、圆角；任务 121），导出时同一元素既写 .excalidraw 原件，也由 kb 复用 _scene_svg
生成笔记里直接可见的 .svg（本模块不 import diagram，避免 kb<->diagram 循环）。
"""
from __future__ import annotations

import random
import time
from itertools import count
from typing import Any

from app.services.excalidraw_layout import (
    INK,
    MUTED,
    build_excalidraw_json,
    estimate_text_height,
    estimate_text_width,
    render,
)
from app.services.figure_png import (FigurePalette, figure_palette,  # 任务 107 / 120 / 121
                                     source_font_units_for_pt, tier_radius,
                                     tier_stroke, tier_units)

TS = int(time.time() * 1000)
RND = random.Random()
_IDS = count(1)

FONT_FAMILY = 2
ROUGH = 1.6

# —— 字号四档（任务 120）——
# 与 excalidraw_layout 同一套档位（口径正本 figure_png，档距黄金比 φ = 1.618，同一档位同时
# 决定线宽与圆角）。图表没有标题条那种版面、画布固定 640 单位宽 ⇒ 图题挂小标题档，
# 表头/表体文字挂正文档，脚注/饼图标签挂注释档。
FONT_SUBTITLE = tier_units("subtitle")
FONT_BODY = tier_units("body")
FONT_CAPTION = tier_units("caption")
SW = tier_stroke("body")       # 2.0 → 1.6：正文档线宽

CANVAS_W = 640.0
PAD_L = 30.0
PAD_R = 30.0
PAD_T = 56.0

#: 数据图的主色槽位（取主题 6 色的第 0 个）：单系列 = 单色阶（饼图按明度分档），
#: 多系列 = 主色 + 强调色（槽位 +1）—— 一张图的非灰彩色 ≤3 种（任务 121 三色法）。
CHART_SLOT = 0

# 任务 107 护栏：本模块的画布宽度是**固定**的（CANVAS_W）⇒ 标签字号直接决定「上页后的
# 等效磅数」（= 像素字号 ÷ PNG 宽度 × 显示宽度 × 72）。这里用 figure_png 的同一口径算出
# 下限，_text() 统一兜底：今天 12 / 13 号标签在 6.9 英寸档约 9.4 / 10.2 磅（已达标，
# 因此是零行为变化），往后有人调小字号就会被自动抬回来。
MIN_TEXT_UNITS = source_font_units_for_pt(CANVAS_W - PAD_L - PAD_R)


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


def _rect(x: float, y: float, w: float, h: float,
          fill: str, stroke: str, dash: bool = False, *,
          tier: str = "body") -> dict[str, Any]:
    """矩形：线宽与圆角都取档（任务 120）。"""
    el = {
        "id": _nid("b"),
        "type": "rectangle",
        "x": round(x, 1),
        "y": round(y, 1),
        "width": round(w, 1),
        "height": round(h, 1),
        "angle": 0,
        "strokeColor": stroke,
        "backgroundColor": fill,
        "fillStyle": "solid",
        "strokeWidth": tier_stroke(tier),
        "strokeStyle": "dashed" if dash else "solid",
        "roughness": ROUGH,
        "opacity": 100,
        "roundness": {"type": 2, "value": tier_radius(tier)},
    }
    el.update(_defaults())
    return el


def _ellipse(x: float, y: float, w: float, h: float,
             fill: str, stroke: str) -> dict[str, Any]:
    el = {
        "id": _nid("e"),
        "type": "ellipse",
        "x": round(x, 1),
        "y": round(y, 1),
        "width": round(w, 1),
        "height": round(h, 1),
        "angle": 0,
        "strokeColor": stroke,
        "backgroundColor": fill,
        "fillStyle": "solid",
        "strokeWidth": SW,
        "strokeStyle": "solid",
        "roughness": ROUGH,
        "opacity": 100,
        "roundness": None,
    }
    el.update(_defaults())
    return el


def _text(content: str, x: float, y: float, size: float,
          color: str = INK, align: str = "left") -> dict[str, Any]:
    size = max(float(size), MIN_TEXT_UNITS)
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
        "containerId": None,
        "autoResize": True,
        "lineHeight": 1.35,
    }
    el.update(_defaults())
    return el


def _centered_text(content: str, cx: float, cy: float, size: float,
                    color: str = INK) -> dict[str, Any]:
    """按 (cx, cy) 居中放一段文字。宽高必须用**兜底后的**字号算：``_text()`` 会把小于
    :data:`MIN_TEXT_UNITS` 的字号抬上去，用未抬的字号算坐标会让文字偏出预留位置
    （任务 122 实测：象限图角标签因此压到卡片上，被 ``validate_elements`` 判为穿出）。"""
    size = max(float(size), MIN_TEXT_UNITS)
    return _text(content, cx - estimate_text_width(content, size) / 2,
                 cy - estimate_text_height(content, size) / 2, size, color)


def _poly(points: list[list[float]], fill: str, stroke: str) -> dict[str, Any]:
    """闭合/折线路径：用 line 元素表达（Excalidraw 可填色，SVG 预览描边）。"""
    pts = [[round(p[0], 1), round(p[1], 1)] for p in points]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    el = {
        "id": _nid("l"),
        "type": "line",
        "x": round(min(xs), 1),
        "y": round(min(ys), 1),
        "width": round(max(xs) - min(xs), 1),
        "height": round(max(ys) - min(ys), 1),
        "angle": 0,
        "strokeColor": stroke,
        "backgroundColor": fill,
        "fillStyle": "solid",
        "strokeWidth": SW,
        "strokeStyle": "solid",
        "roughness": ROUGH,
        "opacity": 100,
        "roundness": None,
        "points": [[round(p[0] - min(xs), 1), round(p[1] - min(ys), 1)] for p in pts],
    }
    el.update(_defaults())
    return el


def _num(value: Any) -> float:
    return float(value)


def _fmt(value: float) -> str:
    if abs(value - round(value)) < 1e-6:
        return str(int(round(value)))
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _title_el(title: str) -> list[dict]:
    return [_text(str(title or "")[:30], PAD_L, 14, FONT_SUBTITLE, INK)]


def _series_of(data: dict, pal: FigurePalette) -> tuple[list[str], list[dict[str, Any]]]:
    labels = [str(v)[:10] for v in (data.get("labels") or [])]
    datasets = data.get("datasets") or []
    if not isinstance(datasets, list) or not datasets:
        raise ValueError("图表缺少 datasets")
    series = []
    for ds in datasets[:2]:
        values = [_num(v) for v in (ds.get("data") or [])]
        if not values:
            continue
        series.append({
            "label": str(ds.get("label") or "")[:12],
            "values": values,
            "color": (pal.fill(CHART_SLOT + len(series), 0),
                      pal.base(CHART_SLOT + len(series))),
        })
    if not series:
        raise ValueError("datasets 无可用数值")
    return labels, series


def _cap_cjk(text: str, limit: float, size: float = FONT_CAPTION) -> str:
    """粗限长，避免画布超宽。"""
    text = str(text or "")
    width = 0.0
    for idx, ch in enumerate(text):
        width += 1.0 if ord(ch) > 0x2E7F else 0.55
        if width > limit:
            return text[:idx].rstrip() + "…"
    return text


def _bar_elements(data: dict, title: str, pal: FigurePalette) -> list[dict]:
    labels, series = _series_of(data, pal)
    first = series[0]
    values = first["values"][:12]
    if not labels:
        labels = [str(i + 1) for i in range(len(values))]
    labels = labels[: len(values)]
    while len(labels) < len(values):
        labels.append("")
    vmin = min(0.0, min(values))
    vmax = max(1.0, max(values))
    x0 = PAD_L + 44.0
    x1 = CANVAS_W - PAD_R
    y_base = 258.0
    y_top = 92.0
    band = (x1 - x0) / max(len(values), 1)
    bar_w = min(54.0, band * 0.62)
    els = _title_el(title)
    els.append(_text("数值", x0 - 44, y_base - 14, FONT_CAPTION, MUTED, "left"))
    els.append(_rect(x0 - 8, y_base - 1.2, (x1 - x0) + 16, 2.4, "#94a3b8", "#94a3b8"))
    for i, v in enumerate(values):
        cx = x0 + band * i + band / 2
        h = (v - vmin) / (vmax - vmin) * (y_base - y_top - 14)
        color = (pal.fill(CHART_SLOT, i), pal.base(CHART_SLOT))
        if v >= 0:
            y = y_base - h
        else:
            y = y_base
            h = abs((v - vmin) / (vmax - vmin) * (y_base - y_top - 14))
        els.append(_rect(cx - bar_w / 2, y, bar_w, max(h, 2.0), color[0], color[1]))
        if abs(h) >= 16:
            els.append(_text(_fmt(v), cx - 40, y - 20, FONT_CAPTION, INK))
        else:
            els.append(_text(_fmt(v), cx - 40, y + 4, FONT_CAPTION, INK))
        label = _cap_cjk(labels[i], 3.5, FONT_CAPTION)
        els.append(_text(label, cx - band / 2 + 2, y_base + 14, FONT_CAPTION, MUTED))
    return els


def _line_elements(data: dict, title: str, pal: FigurePalette) -> list[dict]:
    labels, series = _series_of(data, pal)
    n = max((len(s["values"]) for s in series), default=0)
    if n == 0:
        raise ValueError("折线图无数据点")
    if not labels:
        labels = [str(i + 1) for i in range(n)]
    labels = labels[:n]
    while len(labels) < n:
        labels.append("")
    flat = [v for s in series for v in s["values"]]
    vmin = min(0.0, min(flat))
    vmax = max(1.0, max(flat))
    x0 = PAD_L + 44.0
    x1 = CANVAS_W - PAD_R
    y_base = 250.0
    y_top = 84.0
    els = _title_el(title)
    span = (x1 - x0) / max(n - 1, 1)
    els.append(_rect(x0 - 8, y_base - 1.2, (x1 - x0) + 16, 2.4, "#94a3b8", "#94a3b8"))
    for s in series:
        color = s["color"]
        pts = []
        for i in range(min(n, len(s["values"]))):
            x = x0 + i * span
            v = s["values"][i]
            y = y_base - (v - vmin) / (vmax - vmin) * (y_base - y_top - 10)
            pts.append([x, y])
        els.append(_poly(pts, "transparent", color[1]))
    if labels and n > 0:
        step = max(1, (n + 11) // 12)
        for i in range(0, n, step):
            x = x0 + i * span
            label = _cap_cjk(labels[i], 3.5, FONT_CAPTION)
            els.append(_text(label, x - 22, y_base + 16, FONT_CAPTION, MUTED))
    return els


def _pie_elements(data: dict, title: str, pal: FigurePalette) -> list[dict]:
    labels, series = _series_of(data, pal)
    values = series[0]["values"][:8]
    if not labels:
        labels = [str(i + 1) for i in range(len(values))]
    labels = labels[: len(values)]
    while len(labels) < len(values):
        labels.append("")
    total = sum(abs(v) for v in values) or 1.0
    cx, cy, r = 200.0, 190.0, 118.0
    els = _title_el(title)
    els.append(_ellipse(cx - r, cy - r, r * 2, r * 2, "#f8fafc", "#cbd5e1"))
    start = -90.0
    for i, v in enumerate(values):
        frac = abs(v) / total
        sweep = frac * 360.0
        color = pal.band(CHART_SLOT, i, len(values))
        pts = [[cx, cy]]
        steps = max(2, int(sweep / 12.0))
        for k in range(steps + 1):
            ang = (start + sweep * k / steps) * 3.141592653589793 / 180.0
            pts.append([cx + r * 0.98 * _cos(ang), cy + r * 0.98 * _sin(ang)])
        pts.append([cx, cy])
        els.append(_poly(pts, color[0], color[1]))
        if frac >= 0.07:
            mid = (start + sweep / 2) * 3.141592653589793 / 180.0
            lx = cx + r * 0.58 * _cos(mid)
            ly = cy + r * 0.58 * _sin(mid)
            els.append(_text(f"{round(frac * 100)}%", lx - 12, ly - 9, FONT_CAPTION, INK))
        start += sweep
    lx = 360.0
    ly = 84.0
    for i in range(len(values)):
        color = pal.band(CHART_SLOT, i, len(values))
        els.append(_rect(lx, ly, 16, 16, color[0], color[1]))
        line = f"{_cap_cjk(labels[i], 6.5, FONT_CAPTION)}　{round(abs(values[i]) / total * 100)}%"
        els.append(_text(line, lx + 24, ly - 1, FONT_CAPTION, INK))
        ly += 34.0
    return els


def _cos(a: float) -> float:
    import math
    return math.cos(a)


def _sin(a: float) -> float:
    import math
    return math.sin(a)


def _table_elements(data: dict, title: str, pal: FigurePalette) -> list[dict]:
    headers = [str(h) for h in (data.get("headers") or [])][:6]
    rows = [[str(c) for c in row] for row in (data.get("rows") or [])][:10]
    if not headers and not rows:
        raise ValueError("表格无内容")
    cols = max(len(headers), max((len(r) for r in rows), default=0))
    headers = (headers + [""] * cols)[:cols]
    rows = [r + [""] * (cols - len(r)) for r in rows]
    x0 = PAD_L
    size = FONT_BODY
    widths = []
    for ci in range(cols):
        cells = [headers[ci]] + [r[ci] for r in rows]
        w = max(estimate_text_width(c, size) for c in cells) + 26.0
        widths.append(min(max(w, 56.0), 190.0))
    total_w = sum(widths)
    if total_w > CANVAS_W - PAD_L - PAD_R:
        scale = (CANVAS_W - PAD_L - PAD_R) / total_w
        widths = [w * scale for w in widths]
    els = _title_el(title)
    y = 70.0
    row_h = 34.0
    x = x0
    for ci, header in enumerate(headers):
        els.append(_text(header, x + 8, y + 6, FONT_BODY, pal.deep(CHART_SLOT)))
        x += widths[ci]
    els.append(_rect(x0 - 6, y + 32, sum(widths) + 12, 2.2,
                     pal.base(CHART_SLOT), pal.base(CHART_SLOT)))
    y += row_h + 10
    for ri, row in enumerate(rows):
        x = x0
        for ci, cell in enumerate(row):
            els.append(_text(cell, x + 8, y + 6, FONT_CAPTION, INK))
            x += widths[ci]
        y += row_h
    return els


# —— 象限图（任务 122）——
# 研究报告（素材 §「比较与决策」）实测：任务书里写了 quadrant，但代码里根本不存在 ——
# 「重要性 × 紧急度」「成本 × 效果」这类两轴权衡在我们的图里整类缺失。本版式补上：
# 两条轴线（十字）+ 四枚角标签 + 四张象限卡。
# 几何口径（源单位，画布宽固定 CANVAS_W=640）：十字把画布分成四格，卡片贴格摆放，
# 行/列之间留出「十字甬道」，角标签就落在甬道里 ⇒ 文字互不压（validate_elements 口径）。
#: 四个象限的**阅读顺序**（与四张卡的位置一一对应）：左上 → 右上 → 左下 → 右下。
QUADRANT_ORDER = ("左上", "右上", "左下", "右下")
QUADRANT_CX = 320.0        # 十字中心横坐标（左 20 / 右 620 的中点）
QUADRANT_CY = 232.0        # 十字中心纵坐标（上 76 / 下 384 的中点）
QUADRANT_TOP = 76.0
QUADRANT_BOTTOM = 384.0
QUADRANT_CARD_W = 270.0
QUADRANT_CARD_H = 142.0
QUADRANT_AXIS_X0 = 96.0    # 横轴端点（左右各留 76 / 56 单位放角标签）
QUADRANT_AXIS_X1 = 584.0
QUADRANT_CARD_XS = (20.0, 350.0)      # 左列 / 右列
QUADRANT_CARD_YS = (76.0, 242.0)      # 上排 / 下排


def _quadrant_elements(data: dict, title: str, pal: FigurePalette) -> list[dict]:
    """两轴四象限（任务 122）：十字轴线 + 四枚角标签 + 四张象限卡。

    ``data``：``{"x_low" / "x_high" / "y_low" / "y_high": 四枚角标签（≤4 字），
    "quadrants": [{"name": ≤8 字, "desc": ≤12 字}]}`` —— 象限按**阅读顺序**
    （左上 → 右上 → 左下 → 右下）给；缺的用占位补齐（**不截断内容、也不报错**）。
    四个轴端标签本身就是轴名（如 x_low=「不重要」/ x_high=「重要」）⇒ 十字上只有四个
    text，元素账最好算：图形 4（每格一张卡，≤ 上限 6）+ text ≤ 15（≤ 上限 20）。
    """
    quads = data.get("quadrants")
    quads = quads if isinstance(quads, list) else []
    if not any(isinstance(q, dict) and (q.get("name") or q.get("desc")) for q in quads):
        raise ValueError("象限图缺少 quadrants")

    els = _title_el(title)
    axis = pal.base(CHART_SLOT)
    # 十字轴线用 **line 元素**：不占单图图形上限（见 figure_png.FIGURE_SHAPE_TYPES）
    els.append(_poly([[QUADRANT_CX, QUADRANT_TOP], [QUADRANT_CX, QUADRANT_BOTTOM]],
                     "transparent", axis))
    els.append(_poly([[QUADRANT_AXIS_X0, QUADRANT_CY], [QUADRANT_AXIS_X1, QUADRANT_CY]],
                     "transparent", axis))
    # 四枚角标签（落在十字甬道里，不与卡片文字相交）
    # 横轴两端落在「上排卡下沿 218 / 下排卡上沿 242」之间的甬道中点 230（上下各留 ≈3 单位，
    # 文字不会压到卡片上）；纵轴两端在画布上下沿外，与标题条也有余量。
    corners = (("y_high", "高", QUADRANT_CX, 62.0), ("y_low", "低", QUADRANT_CX, 400.0),
               ("x_low", "低", 62.0, 230.0), ("x_high", "高", 616.0, 230.0))
    for key, fallback, cx, cy in corners:
        label = _cap_cjk(str(data.get(key) or "").strip() or fallback, 4.0, FONT_CAPTION)
        els.append(_centered_text(label, cx, cy, FONT_CAPTION, MUTED))
    # 四张象限卡（阅读顺序：左上 → 右上 → 左下 → 右下）
    for i, (px, py) in enumerate([(x, y) for y in QUADRANT_CARD_YS
                                  for x in QUADRANT_CARD_XS]):
        raw = quads[i] if i < len(quads) and isinstance(quads[i], dict) else {}
        name = str(raw.get("name") or "").strip() or f"象限 {i + 1}"
        els.append(_rect(px, py, QUADRANT_CARD_W, QUADRANT_CARD_H,
                         pal.fill(CHART_SLOT, i), pal.base(CHART_SLOT)))
        els.append(_text(_cap_cjk(name, 8.0, FONT_BODY), px + 14, py + 16,
                         FONT_BODY, pal.deep(CHART_SLOT)))
        desc = _cap_cjk(str(raw.get("desc") or "").strip(), 13.0, FONT_CAPTION)
        if desc:
            els.append(_text(desc, px + 14, py + 54, FONT_CAPTION, INK))
    return els


def _scene_node(node: dict, index: int) -> dict:
    """把 chart 数据里的一个节点归一到 layout 场景节点（标题/副行/emoji）。"""
    node = node if isinstance(node, dict) else {"text": str(node)}
    title = _cap_cjk(str(node.get("title") or node.get("text") or ""), 18, FONT_BODY)
    subs = [_cap_cjk(str(s), 22, FONT_CAPTION) for s in (node.get("subs") or [])][:2]
    return {
        "emoji": str(node.get("emoji") or "").strip() or ["\U0001F4CC",
                "\U0001F4A1", "\u2B50", "\U0001F3AF", "\u2705", "\U0001F512"][index % 6],
        "title": title or f"要点 {index + 1}",
        "subs": subs,
    }


def _mindmap_elements(data: dict, title: str, pal: FigurePalette) -> list[dict]:
    """思维导图：中心放射树 → excalidraw_layout.mindmap 左根水平树。"""
    raw_root = data.get("root")
    if isinstance(raw_root, dict):
        root = {"emoji": str(raw_root.get("emoji") or "").strip(),
                "title": _cap_cjk(str(raw_root.get("title") or raw_root.get("text") or ""),
                                  16, FONT_SUBTITLE)}
    else:
        root = {"title": _cap_cjk(str(raw_root or title or ""), 16, FONT_SUBTITLE)}
    branches_raw = data.get("branches")
    if not isinstance(branches_raw, list) or not branches_raw:
        raise ValueError("思维导图缺少 branches")
    branches = []
    for i, raw in enumerate(branches_raw[:6]):
        branch = raw if isinstance(raw, dict) else {"title": str(raw)}
        subs = [_cap_cjk(str(s), 26, FONT_CAPTION) for s in (branch.get("subs") or [])][:2]
        branches.append({
            "emoji": str(branch.get("emoji") or "").strip() or _scene_node({}, i)["emoji"],
            "title": _cap_cjk(str(branch.get("title") or ""), 16, FONT_BODY),
            "subs": subs,
        })
    return render("mindmap", {"title": str(title or "")[:30], "root": root,
                              "branches": branches}, theme=pal.theme)


def _timeline_elements(data: dict, title: str, pal: FigurePalette) -> list[dict]:
    """时间轴：chart 的阶段数据 → excalidraw_layout.timeline 横向阶段卡。"""
    steps_raw = data.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ValueError("时间轴缺少 steps")
    steps = [_scene_node(raw, i) for i, raw in enumerate(steps_raw[:6])]
    return render("timeline", {"title": str(title or "")[:30], "steps": steps},
                  theme=pal.theme)


_ELEMENT_BUILDERS = {
    "bar": _bar_elements,
    "line": _line_elements,
    "pie": _pie_elements,
    "table": _table_elements,
    "quadrant": _quadrant_elements,
    "mindmap": _mindmap_elements,
    "timeline": _timeline_elements,
}


def chart_to_excalidraw_doc(chart_type: str, data: dict, title: str = "",
                            *, theme: str | None = None) -> dict:
    """把 chart_* 的结构化 data 渲染成 Excalidraw 文档 dict（含 elements）。

    ``theme`` 给 PPT 主题（id / 中文名，留空用后端 ``PPT_THEME`` 配置）：图内色一律由它
    派生（:func:`app.services.figure_png.figure_palette`），与概念图 / 流程图同一出处。
    """
    builder = _ELEMENT_BUILDERS.get(str(chart_type or "").strip())
    if builder is None:
        raise ValueError(f"暂不支持该图表的 Excalidraw 渲染：{chart_type}")
    pal = figure_palette(theme)
    elements = builder(data or {}, str(title or "")[:30], pal)
    return build_excalidraw_json(elements, background=pal.bg)
