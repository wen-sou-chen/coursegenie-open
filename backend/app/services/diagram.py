# -*- coding: utf-8 -*-
"""概念图（diagram）生成服务（P2 阶段二）。

链路：按学习目标自动推断布局（规则：流程→chain / 条件因素→radial / 对比→compare /
时间→timeline / 分类→ring / 判断→decision / 默认 radial）→ 文本 LLM 输出
DIAGRAM_JSON（{layout, caption, nodes, edges}）→ excalidraw_layout 排版成合法
Excalidraw 元素 → 落库 kind="diagram"（body: excalidraw_json/caption/layout/
svg_preview）→ 同时把 SVG 预览写入 storage/images/ 供网页端 /images 显示。
"""
from __future__ import annotations

import copy
import json
import logging
import re
import uuid
from html import escape

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.course import ContentItem, Course, OutlineNode
from app.services import usage as usage_log
from app.services.excalidraw_layout import (
    BG,
    build_excalidraw_json,
    estimate_text_width,
    render,
    validate_elements,
)
from app.services.figure_png import (MAX_FIGURE_PARTS, MAX_FIGURE_SHAPES,
                                     MAX_FIGURE_TEXT_ELEMENTS, figure_palette,
                                     is_icon_text, over_capacity, partition_balanced)
from app.services.kb import attach_vault_write
from app.services.llm import build_llm_client

logger = logging.getLogger(__name__)

LAYOUTS = ("chain", "comic", "radial", "ring", "timeline", "compare", "decision")
_DEFAULT_LAYOUT = "radial"

_LAYOUT_RULES = [
    (("判断", "决策", "该不该", "要不要", "是否值得", "分支判断"), "decision"),
    (("对比", "比较", "区别", "异同", "优缺点"), "compare"),
    (("分类", "种类", "类型", "归类"), "ring"),
    (("时间", "历程", "发展", "演变", "阶段", "先后顺序"), "timeline"),
    (("条件", "因素", "要素", "原因", "影响", "构成", "维度"), "radial"),
    (("流程", "步骤", "过程", "原理", "机制", "操作步骤"), "chain"),
]
# 任务 77：概念图节点图标收敛到**白名单**（主题中性 / 语义通用，16 个，顺序稳定）。
# 起因：原先提示词只写「单个字符或省略」、`_emoji_of` 模型给什么就用什么 ⇒ 真交付包里
# 出过 👨‍🍳 配「AI 幻觉的三大特征」、🥟 / 🌡️ / 🌀 这类与主题不搭的图（用户 2026-09-22 看到）。
# 排除：食物 / 厨具 / 人物 / 表情 / 手势 / 国旗 / 货币 / 动物。
DIAGRAM_EMOJI_WHITELIST = [
    "\U0001F4D8",        # 📘 知识 / 教材
    "\U0001F4D7",        # 📗 要点 / 条目
    "\U0001F539",        # 🔹 分项 / 小点
    "\u2705",            # ✅ 达成 / 正确
    "\u274C",            # ❌ 未达成 / 错误
    "\u2753",            # ❓ 疑问 / 待核查
    "\u26A0\uFE0F",      # ⚠️ 注意 / 风险
    "\U0001F4A1",        # 💡 洞察 / 提示
    "\U0001F50D",        # 🔍 分析 / 排查
    "\U0001F9E9",        # 🧩 组成 / 拼合
    "\U0001F9ED",        # 🧭 方向 / 路径
    "\u2699\uFE0F",      # ⚙️ 机制 / 流程
    "\U0001F501",        # 🔁 循环 / 迭代
    "\U0001F4C8",        # 📈 趋势 / 增长
    "\U0001F578\uFE0F",  # 🕸️ 关联 / 网络
    "\U0001F3AF",        # 🎯 目标 / 核心
]
# 兜底轮换（模型没给、或给了白名单外的图标时按序取）：**白名单子集**，顺序固定可复现。
_EMOJI_WHEEL = ["\U0001F4D8", "\U0001F9E9", "\U0001F4A1", "\U0001F50D",
                "\U0001F501", "\U0001F4C8", "\U0001F9ED", "\u2699\uFE0F"]
# 标题条图标：渲染器取 scene["emoji"] 优先（缺省是 excalidraw_layout.DEFAULT_HEAD = 👨‍🍳）。
# 这里只填**场景字段**，不动渲染器本身。
_DIAGRAM_HEAD_EMOJI = "\U0001F4D8"
# 中心要点（radial / ring）的默认图标：🎯 目标，白名单内。
_DIAGRAM_CENTER_EMOJI = "\U0001F3AF"

# 提示词版本：改 DIAGRAM_SYSTEM 必须升版本，并按既有口径记进条目 meta.prompt_version
# （回查「这张图是哪个提示词版本生成的」）。
# 任务 77：节点图标收敛到白名单 + 提示词点名「只能从图标清单里选」→ v2（v1 = 无图标约束的初版）。
DIAGRAM_PROMPT_VERSION = "diagram-v2"

DIAGRAM_SYSTEM = """DIAGRAM_JSON
你是教学概念图设计师。为一个知识点设计「一张一图看懂的矢量概念图」，只输出 JSON：
{"layout": "...", "caption": "图的标题（一句话，≤18字）",
 "nodes": [{"emoji": "从规则 6 的图标清单里原样选一个", "title": "要点名（≤12字）",
            "subs": ["补充说明 1~2 条，每条 ≤14字"]}],
 "edges": [{"from": 0, "to": 1, "label": "连线说明（≤6字）"}]}

规则：
1) nodes 给 3~6 个要点；chain 用 edges 说明步骤衔接。
2) comic 只需 4 个 nodes，title 写一格对白（≤20字）。
3) compare 需 2 个 nodes 且各带 "side": "left" 或 "right"；title 写对比方，
   subs 写一句小结，rows 可带 [{"label": "项目", "value": "表现"}]。
4) decision 的 nodes 各带 "kind": "start"|"check"|"good"|"bad"；
   start.title 写起点；check.title 写判断问题、subs 写「否」时提示；
   good/bad 的 title+subs 写结论。
5) 一律中文；不虚构事实；标题与要点必须贴合该知识点的学习目标。
6) emoji 只能从下面这 %d 个图标里**原样选一个**（直接复制一个字符），不许自创、不许拼接；
   也不要用食物 / 厨具 / 人物 / 表情 / 国旗 / 货币类的图标：
   %s""" % (len(DIAGRAM_EMOJI_WHITELIST), " ".join(DIAGRAM_EMOJI_WHITELIST))


def infer_layout(text: str) -> str:
    """按学习目标文本推断布局（关键词命中优先，无命中回退 radial）。"""
    for keywords, layout in _LAYOUT_RULES:
        if any(keyword in (text or "") for keyword in keywords):
            return layout
    return _DEFAULT_LAYOUT


def _resolve_layout(diagram_type: str | None, objective: str, node_title: str) -> str:
    if diagram_type and str(diagram_type).strip() in LAYOUTS:
        return str(diagram_type).strip()
    return infer_layout(f"{objective} {node_title}")


def _title_of(data: dict, fallback: str) -> str:
    title = str(data.get("caption") or "").strip()
    return title or str(fallback or "").strip() or "一图看懂"


def _cap(text: str, limit: int = 18) -> str:
    """按「CJK≈1、ASCII≈0.6」粗略限长，防止单行过长撑破画布。"""
    text = str(text or "").strip()
    if not text:
        return ""
    width = 0.0
    for idx, ch in enumerate(text):
        width += 1.0 if ord(ch) > 0x2E7F else 0.6
        if width > limit:
            return text[:idx].rstrip() + "…"
    return text


def _norm_nodes(raw_nodes) -> list[dict]:
    """把 DIAGRAM_JSON.nodes 归一为 {title, subs, emoji, ...} 列表。"""
    out: list[dict] = []
    for raw in raw_nodes or []:
        if isinstance(raw, str):
            raw = {"text": raw}
        if not isinstance(raw, dict):
            continue
        node: dict = {}
        for key, value in raw.items():
            if key == "text":
                continue
            node[key] = value
        title = str(raw.get("title") or "").strip()
        subs = [str(s).strip() for s in (raw.get("subs") or []) if str(s).strip()]
        if not title and raw.get("text"):
            lines = [ln.strip() for ln in str(raw.get("text")).split("\n") if ln.strip()]
            if lines:
                title = lines[0]
                subs = lines[1:] + subs
        if title:
            node["title"] = title
        if subs:
            node["subs"] = subs
        out.append(node)
    return out


def _pick_emoji(raw, fallback: str) -> str:
    """图标口径的唯一入口：白名单内照用，白名单外（含空）一律走兜底 —— 不照抄模型自创的图标。"""
    candidate = str(raw or "").strip()
    return candidate if candidate in DIAGRAM_EMOJI_WHITELIST else fallback


def _emoji_of(node: dict, index: int) -> str:
    return _pick_emoji(node.get("emoji"), _EMOJI_WHEEL[index % len(_EMOJI_WHEEL)])


def _fit_to(text: str, size: float, max_w: float) -> str:
    """把单行文本截到估计宽度不超 max_w（带省略号）。"""
    text = str(text or "").strip()
    while text and estimate_text_width(text + "…", size) > max_w:
        text = text[:-1]
    return (text + "…") if text and estimate_text_width(text + "…", size) <= max_w else ""


def _card_from(node: dict, index: int, max_subs: int = 2) -> dict:
    """radial/ring/timeline 通用要点卡：首行作标题，其余作副行。"""
    subs = [str(s).strip() for s in (node.get("subs") or []) if str(s).strip()]
    title = _cap(str(node.get("title") or ""), 16)
    if not title and subs:
        title = _cap(subs.pop(0), 16)
    return {
        "emoji": _emoji_of(node, index),
        "title": title or f"要点 {index + 1}",
        "subs": [_cap(s, 16) for s in subs[:max_subs]],
    }


def _chain_scene(data: dict, caption: str) -> dict:
    nodes = _norm_nodes(data.get("nodes"))
    cards = [
        {
            "emoji": _emoji_of(n, i),
            "text": _cap("\n".join([str(n.get("title") or "")] + [str(s) for s in (n.get("subs") or [])][:2]), 34)
            or f"步骤 {i + 1}",
        }
        for i, n in enumerate(nodes[:5])
    ]
    if not cards:
        cards = [{"emoji": _EMOJI_WHEEL[0], "text": _cap(caption, 20)}]
    labels = []
    for edge in data.get("edges") or []:
        label = str(edge.get("label") or "").strip()
        if label:
            labels.append(_cap(label, 8))
    return {"title": _cap(caption, 24), "nodes": cards, "edge_labels": labels[: len(cards) - 1]}


def _comic_scene(data: dict, caption: str) -> dict:
    panels = []
    for i, node in enumerate(_norm_nodes(data.get("nodes"))[:4]):
        lines = [_cap(str(node.get("title") or ""), 18)]
        lines += [_cap(s, 18) for s in (node.get("subs") or [])]
        lines = [ln for ln in lines if ln][:3]
        if not lines:
            lines = [f"第 {i + 1} 步，围绕「{_cap(caption, 12)}」继续。"]
        panel: dict = {"emoji": _emoji_of(node, i), "lines": lines}
        note = str(node.get("caption") or "").strip()
        if note:
            panel["caption"] = _cap(note, 10)
        panels.append(panel)
    scene: dict = {"title": _cap(caption, 24), "nodes": panels}
    if not panels:
        raise ValueError("comic 布局至少需要 1 格分镜")
    return scene


def _center_scene(layout: str, data: dict, caption: str) -> dict:
    """radial / ring：中心一句话 + 四周要点卡。"""
    if layout == "radial":
        max_cards, center_w = 5, 220.0
    else:
        max_cards, center_w = 4, 220.0
    cards = [_card_from(n, i) for i, n in enumerate(_norm_nodes(data.get("nodes"))[:max_cards])]
    if not cards:
        cards = [_card_from({"title": _cap(caption, 12), "subs": ["围绕目标梳理要点"]}, 0)]
    center_text = _fit_to(str(data.get("center") or _cap(caption, 10)), 20, center_w)
    center: dict = {"emoji": _pick_emoji(data.get("center_emoji"), _DIAGRAM_CENTER_EMOJI),
                    "text": center_text}
    if center_text == "":
        center.pop("text", None)
    return {"title": _cap(caption, 24), "center": center, "cards": cards}


def _timeline_scene(data: dict, caption: str) -> dict:
    steps = [_card_from(n, i, max_subs=2) for i, n in enumerate(_norm_nodes(data.get("nodes"))[:6])]
    if not steps:
        steps = [_card_from({"title": "开始", "subs": ["明确起点"]}, 0),
                 _card_from({"title": "推进", "subs": ["按序完成"]}, 1),
                 _card_from({"title": "收尾", "subs": ["检查结果"]}, 2)]
    scene: dict = {"title": _cap(caption, 24), "steps": steps}
    subtitle = str(data.get("subtitle") or "").strip()
    if subtitle:
        scene["subtitle"] = _cap(subtitle, 40)
    return scene


def _compare_scene(data: dict, caption: str) -> dict:
    """compare：左右两栏，各栏先选侧，未标注时前一半为左、后一半为右。"""
    nodes = _norm_nodes(data.get("nodes"))
    left, right = [], []
    for node in nodes:
        side = str(node.get("side") or "").lower()
        if side in ("left", "l", "左"):
            left.append(node)
        elif side in ("right", "r", "右"):
            right.append(node)
    if not left and not right:
        half = (len(nodes) + 1) // 2
        left, right = nodes[:half], nodes[half:]

    def _column(parts: list[dict], default_name: str, index: int) -> dict:
        if not parts:
            return {"name": default_name, "bubble": "", "rows": []}
        head = parts[0]
        rows = [r for r in (head.get("rows") or []) if isinstance(r, dict)][:5]
        extras = [
            {"label": _cap(str(n.get("title") or ""), 8),
             "value": _cap(str((n.get("subs") or [""])[0] if n.get("subs") else ""), 12)}
            for n in parts[1:]
        ]
        rows = (rows + extras)[:5]
        bubble = "\n".join([_cap(s, 22) for s in (head.get("subs") or [])][:2])
        col: dict = {"emoji": _emoji_of(head, index),
                     "name": _cap(str(head.get("title") or ""), 10) or default_name,
                     "bubble": bubble, "rows": rows}
        return col

    left_col = _column(left, "对比方 A", 0)
    right_col = _column(right, "对比方 B", 1)
    left_col["name"] = left_col["name"] or "对比方 A"
    right_col["name"] = right_col["name"] or "对比方 B"
    # 任务 121：不再写死「左暖红 / 右冷蓝」，只给**色相槽位**（左 = 主色、右 = 强调色），
    # 具体色值由 excalidraw_layout 从当前 PPT 主题派生 ⇒ 换主题整套对比图跟着换。
    left_col = {"family": "main", **left_col}
    right_col = {"family": "accent", **right_col}
    return {"title": _cap(caption, 24), "columns": [left_col, right_col]}


def _decision_scene(data: dict, caption: str) -> dict:
    nodes = _norm_nodes(data.get("nodes"))
    starts = [n for n in nodes if str(n.get("kind") or "") in ("start", "开始")]
    checks = [n for n in nodes if str(n.get("kind") or "") == "check"]
    goods = [n for n in nodes if str(n.get("kind") or "") in ("good", "是", "达成")]
    bads = [n for n in nodes if str(n.get("kind") or "") in ("bad", "否", "未达成")]
    if not starts and nodes:
        starts = [nodes[0]]
    start_text = _cap(str(starts[0].get("title") or "") if starts else caption, 26)
    if not checks:
        checks = [{"title": _cap(f"{caption}的条件是否全部满足？", 24)}]
    check_rows = []
    for node in checks[:3]:
        subs = [str(s).strip() for s in (node.get("subs") or []) if str(s).strip()]
        row: dict = {"q": _cap(str(node.get("title") or "条件是否满足？"), 24)}
        if subs:
            row["no"] = _cap(subs[0], 20)
        check_rows.append(row)
    good_text = _block_text(goods, "\u2705 条件满足，继续推进")
    bad_text = _block_text(bads, "\u274C 条件不满足，调整后重试")
    return {
        "title": _cap(caption, 24),
        "start": start_text,
        "checks": check_rows,
        "good": good_text,
        "bad": bad_text,
    }


def _block_text(nodes: list[dict], fallback: str) -> str:
    """good/bad 结论块：title 一行 + subs 两行。"""
    if not nodes:
        return fallback
    parts = [_cap(str(nodes[0].get("title") or ""), 18)]
    parts += [_cap(s, 18) for s in (nodes[0].get("subs") or [])][:2]
    return "\n".join([p for p in parts if p]) or fallback


def _build_scene(layout: str, data: dict, caption: str) -> dict:
    """把 DIAGRAM_JSON 归一为该布局的排版 scene（并统一补标题条图标口径）。"""
    if layout == "chain":
        scene = _chain_scene(data, caption)
    elif layout == "comic":
        scene = _comic_scene(data, caption)
    elif layout in ("radial", "ring"):
        scene = _center_scene(layout, data, caption)
    elif layout == "timeline":
        scene = _timeline_scene(data, caption)
    elif layout == "compare":
        scene = _compare_scene(data, caption)
    elif layout == "decision":
        scene = _decision_scene(data, caption)
    else:
        raise ValueError(f"未知布局：{layout}")
    # 标题条图标：渲染器缺省取 excalidraw_layout.DEFAULT_HEAD（任务 92 起 = 📘），这里只填**场景字段**
    scene.setdefault("emoji", _DIAGRAM_HEAD_EMOJI)
    return scene


def _svg_attr(el: dict, *, fill_override: str | None = None) -> str:
    """元素的描边/填充属性。

    fill_override 用于覆盖 backgroundColor:线/箭头必须 fill="none"。
    早期实现既在 polyline 上写字面 fill="none"、又由本函数再输出一次 fill,
    导致同一属性出现两次 → SVG 不是合法 XML → 浏览器按 XML 解析直接失败
    （这正是"网页里概念图/流程图不显示、而数据图正常、Obsidian 能显示"的根因）。
    """
    stroke = str(el.get("strokeColor") or "#374151")
    fill = fill_override if fill_override is not None else str(el.get("backgroundColor") or "transparent")
    width = float(el.get("strokeWidth") or 1)
    dash = ' stroke-dasharray="9 6"' if str(el.get("strokeStyle")) == "dashed" else ""
    return (f' fill="{escape(fill)}" stroke="{escape(stroke)}" '
            f'stroke-width="{width:.1f}" stroke-linecap="round"{dash}')


def diagram_to_svg(elements: list[dict], *, background: str = BG) -> str:
    """把 Excalidraw 元素转成简单 SVG 文本（无外部依赖，粗糙但可看）。

    ``background`` 是底色（任务 121 起由 PPT 主题派生；不给时用 ``excalidraw_layout.BG``）——
    与 figure_png 的 PNG 底色同一出处，避免同一张图在 SVG 与 PNG 里两套底色。
    """
    alive = [el for el in elements if not el.get("isDeleted")]
    if not alive:
        return ""
    pad = 24.0
    min_x = min(el["x"] for el in alive)
    min_y = min(el["y"] for el in alive)
    max_x = max(el["x"] + float(el.get("width") or 0) for el in alive)
    max_y = max(el["y"] + float(el.get("height") or 0) for el in alive)
    view = (min_x - pad, min_y - pad, max_x - min_x + pad * 2, max_y - min_y + pad * 2)
    marker = f"arr{uuid.uuid4().hex[:6]}"
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view[0]:.1f} {view[1]:.1f} '
        f'{view[2]:.1f} {view[3]:.1f}" width="{view[2]:.1f}" height="{view[3]:.1f}">',
        f'<defs><marker id="{marker}" viewBox="0 0 10 10" refX="8" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M0,0 L10,5 L0,10 z" fill="#94a3b8"/></marker></defs>',
        f'<rect x="{view[0]:.1f}" y="{view[1]:.1f}" width="{view[2]:.1f}" '
        f'height="{view[3]:.1f}" fill="{escape(background)}"/>',
    ]
    for el in alive:
        kind = el["type"]
        x, y = float(el["x"]), float(el["y"])
        w, h = float(el.get("width") or 0), float(el.get("height") or 0)
        if kind == "rectangle":
            rx = min(14.0, w / 2, h / 2)
            parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
                         f'rx="{rx:.1f}" opacity="0.9"{_svg_attr(el)}/>')
        elif kind == "ellipse":
            parts.append(f'<ellipse cx="{x + w / 2:.1f}" cy="{y + h / 2:.1f}" '
                         f'rx="{w / 2:.1f}" ry="{h / 2:.1f}" opacity="0.9"{_svg_attr(el)}/>')
        elif kind == "diamond":
            pts = f"{x + w / 2:.1f},{y:.1f} {x + w:.1f},{y + h / 2:.1f} "
            pts += f"{x + w / 2:.1f},{y + h:.1f} {x:.1f},{y + h / 2:.1f}"
            parts.append(f'<polygon points="{pts}" opacity="0.9"{_svg_attr(el)}/>')
        elif kind in ("line", "arrow"):
            raw = el.get("points") or [[0, 0], [w, h]]
            pts = " ".join(f"{x + float(p[0]):.1f},{y + float(p[1]):.1f}" for p in raw)
            extra = f' marker-end="url(#{marker})"' if el.get("endArrowhead") else ""
            parts.append(f'<polyline points="{pts}"{_svg_attr(el, fill_override="none")}{extra}/>')
        elif kind == "text":
            text = str(el.get("text") or "")
            if not text.strip():
                continue
            size = float(el.get("fontSize") or 20)
            anchor = "start" if str(el.get("textAlign")) == "left" else "middle"
            tx = x if anchor == "start" else x + w / 2
            lines = text.split("\n")
            first_y = y + size
            tspans = []
            for idx, line in enumerate(lines):
                dy = 0.0 if idx == 0 else size * 1.35
                tspans.append(f'<tspan x="{tx:.1f}" dy="{dy:.1f}">{escape(line) or " "}</tspan>')
            parts.append(
                f'<text x="{tx:.1f}" y="{first_y:.1f}" font-size="{size:.1f}" '
                f'font-family="&quot;Segoe UI Emoji&quot;,&quot;Noto Sans SC&quot;,sans-serif" '
                f'text-anchor="{anchor}" fill="{escape(str(el.get("strokeColor") or "#374151"))}">'
                + "".join(tspans) + "</text>"
            )
    parts.append("</svg>")
    return "\n".join(parts)


def _write_svg(svg: str, rel: str | None = None) -> str:
    """把 SVG 写入 storage/images/，返回相对 storage_dir 的路径。

    ``rel`` 给了就**就地覆盖**那条既有缓存（任务 92 的重渲染口径 ⇒ 重跑图生成不留孤儿文件）。
    """
    settings = get_settings()
    out_dir = settings.storage_dir / "images"
    out_dir.mkdir(parents=True, exist_ok=True)
    name = str(rel).rsplit("/", 1)[-1] if rel else f"{uuid.uuid4().hex}.svg"
    (out_dir / name).write_text(svg, encoding="utf-8")
    return f"images/{name}"


def write_svg(svg: str, rel: str | None = None) -> str:
    """把 SVG 写进 ``storage/images/``（``rel`` 给了就就地覆盖那条既有缓存，不留孤儿）。

    口径与 :func:`_write_svg` 逐字一致；对外公开这一条是因为**存量图配色迁移**（任务 127）
    在 :mod:`app.services.figure_regen` 里也要重写图文原件旁的 SVG 预览。
    """
    return _write_svg(svg, rel)


def _scene_art(layout: str, data: dict, caption: str, *,
               theme: str | None = None) -> tuple[dict, str]:
    """渲染一张图：排版自检 + 组装 Excalidraw 原件 + SVG 文本（生成与重跑图生成共用这一条链路）。

    ``theme`` 给 PPT 主题（id / 中文名，留空用后端 ``PPT_THEME`` 配置）：图内配色由它派生
    （任务 121，口径正本 = :func:`app.services.figure_png.figure_palette`），换主题整套换肤。
    """
    pal = figure_palette(theme)
    scene = _build_scene(layout, data, caption)
    elements = render(layout, scene, theme=pal.theme)
    issues = validate_elements(elements)
    if issues["text_overlaps"] or issues["overflow"]:
        raise ValueError(f"diagram 排版自检未通过：{issues}")
    return (build_excalidraw_json(elements, background=pal.bg),
            diagram_to_svg(elements, background=pal.bg))


#: 标题条图标的**渲染槽位**：``excalidraw_layout._header`` 里
#: ``_emoji_el(scene.get("emoji") or emoji, 64, 78, 54)`` 的落点。
#: 存量场景里「哪一枚是标题图标」只能按坐标认（场景没有语义字段）—— 真库 52 张场景实测：
#: 51 张有标题条、图标都落在 (64, 78)，节点 / 中心图标最近的在 cy=300；
#: 1 张（讲义概念卡）没有标题条 ⇒ 一枚都不匹配（这是对的，不许乱认）。
_HEAD_SLOT = (64.0, 78.0)
_HEAD_SLOT_TOL = 60.0

#: Excalidraw 的文本元素里同一个字符串有**三份**：``text``（渲染用的那份）+ ``rawText`` /
#: ``originalText``（**编辑用的镜像**）。客户拿 ``.excalidraw`` 原件回编辑器改那几行标题时，
#: 编辑器读的就是镜像 ⇒ 归一必须一起改，否则厨师头会在编辑器里「回来」（任务 100）。
#: **只改这三个字段**，坐标 / 字号 / 颜色 / 其它元素一个字节不动。
_ICON_MIRROR_FIELDS = ("rawText", "originalText")


def _center_of(el: dict) -> tuple[float, float]:
    """元素中心点（图标元素都独立顶置，按中心认位置）。"""
    return (float(el.get("x") or 0) + float(el.get("width") or 0) / 2,
            float(el.get("y") or 0) + float(el.get("height") or 0) / 2)


def scene_icon_text(el: dict) -> str | None:
    """这个元素是不是「一枚图标」？是则返回图标原文，否则 ``None``（正文 / 矩形 / 箭头都不算）。"""
    if el.get("type") != "text":
        return None
    text = str(el.get("text") or "")
    return text if is_icon_text(text) else None


def normalize_scene_icons(elements: list[dict]) -> tuple[list[dict], list[str]]:
    """把一张**存量**概念图场景里的图标就地归一到白名单（0 LLM、纯函数、幂等）。

    - 标题条那一枚 ⇒ :data:`_DIAGRAM_HEAD_EMOJI`（📘）；
    - 其余（节点 / 中心 / 小提示条）⇒ :func:`_emoji_of`（白名单内照用、白名单外按
      :data:`_EMOJI_WHEEL` 轮换）—— **复用生成时那唯一一套判定**，不另写一套；
    - 圈号（①②③④）与普通文字**不动**：它们不是 emoji，是排版序号 / 正文。

    只改 ``text`` / ``rawText`` / ``originalText`` **这三个字段**（Excalidraw 里同一个字符串的
    渲染份 + 两份编辑镜像，见 :data:`_ICON_MIRROR_FIELDS`）：坐标 / 字号 / 颜色 / 其它元素
    一个字节不动。
    返回 ``(新元素列表, 改动说明)``；本来就干净时说明为空 ⇒ 幂等。
    """
    out = [dict(el) for el in elements]
    icons = [(index, el) for index, el in enumerate(out) if scene_icon_text(el) is not None]
    head: int | None = None
    for index, el in icons:
        cx, cy = _center_of(el)
        if abs(cx - _HEAD_SLOT[0]) <= _HEAD_SLOT_TOL and abs(cy - _HEAD_SLOT[1]) <= _HEAD_SLOT_TOL:
            head = index
    notes: list[str] = []
    slot = 0
    for index, el in icons:
        old = scene_icon_text(el) or ""
        if index == head:
            label, new = "标题条", _DIAGRAM_HEAD_EMOJI
        else:
            label, new = f"节点 {slot + 1}", _emoji_of({"emoji": old}, slot)
            slot += 1
        if new != old:
            el["text"] = new
            notes.append(f"{label} {old} → {new}")
        for field in _ICON_MIRROR_FIELDS:
            was = el.get(field)
            if isinstance(was, str) and was != new:
                el[field] = new
                notes.append(f"{label}.{field} {was} → {new}")
    return out, notes


def rerender_svg_preview(item: ContentItem) -> str:
    """按既有渲染器把这张图的 SVG **就地**重出到它自己的缓存路径（返回相对 storage 的路径）。

    走生成时那唯一一条渲染入口（:func:`diagram_to_svg`）：不复制渲染代码、不调模型、
    不新建文件（覆盖 ``body.svg_preview`` 那一份 ⇒ 不留孤儿缓存）。
    """
    body = item.body or {}
    doc = body.get("excalidraw_json")
    elements = doc.get("elements") if isinstance(doc, dict) else None
    if not elements:
        raise ValueError(f"diagram {item.id} 没有 excalidraw_json.elements")
    rel = str(body.get("svg_preview") or "").strip()
    if not rel:
        raise ValueError(f"diagram {item.id} 没有 svg_preview 缓存路径")
    path = get_settings().storage_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(diagram_to_svg(elements), encoding="utf-8")
    return rel


def normalize_item_icons(item: ContentItem, *, render: bool = True) -> list[str]:
    """把库内一条 ``kind='diagram'`` 的存量图标归一（只改 ``body``，**不提交事务**）。

    返回改动说明（空 = 本来就干净）。幂等：归一过的条目再跑一次返回空。
    ``render=False`` 时**只改 body、不出图**，由调用方 ``commit()`` 之后再统一
    :func:`rerender_svg_preview` —— 迁移脚本走这条路径，免得中途异常留下
    「SVG 换了、库没换」的不同步（任务 99）。缺省 ``True`` = 既有行为（就地重渲染）。
    """
    body = dict(item.body or {})
    doc = body.get("excalidraw_json")
    if not isinstance(doc, dict):
        return []
    elements, notes = normalize_scene_icons(list(doc.get("elements") or []))
    if not notes:
        return []
    body["excalidraw_json"] = {**doc, "elements": elements}
    item.body = body
    if render:
        rerender_svg_preview(item)
    return notes


def excalidraw_text_of(item: ContentItem) -> str:
    """把 diagram 条目的 excalidraw_json 序列化为 .excalidraw 文件文本。"""
    doc = (item.body or {}).get("excalidraw_json")
    if not isinstance(doc, dict):
        raise ValueError(f"diagram {item.id} 缺少 excalidraw_json")
    return json.dumps(doc, ensure_ascii=False, indent=1)


def _persist_one(
    db: Session,
    course: Course,
    node: OutlineNode | None,
    layout: str,
    caption: str,
    data: dict,
    *,
    title: str | None = None,
    meta_extra: dict | None = None,
    theme: str | None = None,
) -> ContentItem:
    """渲染**一张**图并落库（不再调 LLM）。generate_diagram 与「讲义概念卡」共用这条链路。"""
    doc, svg = _scene_art(layout, data, caption, theme=theme)
    svg_rel = _write_svg(svg)
    node_name = (node.title if node is not None else course.title) or course.title
    body = {
        "layout": layout,
        "caption": caption,
        "excalidraw_json": doc,
        "svg_preview": svg_rel,
        # 任务 124：原始 DIAGRAM_JSON 一并落库 —— 渲染后的 elements 反推不回原始 nodes，
        # 没有这一份就「重跑不了图生成」（存量图只能重新生成，见 rerender_diagram_item）。
        "data": copy.deepcopy(data),
    }
    item = ContentItem(
        course_id=course.id,
        kind="diagram",
        node_id=node.id if node is not None else None,
        title=title or f"{node_name} · 概念图",
        body=body,
        meta={"node_key": node.node_key if node is not None else "",
              "layout": layout, "caption": caption,
              "prompt_version": DIAGRAM_PROMPT_VERSION},
    )
    if meta_extra:
        item.meta = {**dict(item.meta or {}), **meta_extra}
    db.add(item)
    db.commit()
    db.refresh(item)
    try:
        attach_vault_write(db, item, "diagram")
    except Exception:  # noqa: BLE001 —— Vault 增量失败不影响主流程
        logger.warning("vault incremental write skipped for diagram %s", item.id)
    return item


# —— 单图容量上限 + 超限拆图（任务 120；判据收口见任务 125）——
# 上限口径正本在 figure_png.over_capacity —— **两条上限都要生效**：图形元素 >
# MAX_FIGURE_SHAPES=6 **或** 文本元素 > MAX_FIGURE_TEXT_ELEMENTS=20（图形 = 矩形/椭圆/菱形，
# 计数口径见 figure_png.FIGURE_SHAPE_TYPES）。实测拐点：≤6 个图形的 35 张图 min_pt 全部
# ≥9.52 磅，7 个的 9 张最低掉到 7.57。超限**不再丢节点**（旧代码用 max_cards 直接把多余节点
# 截掉），而是把节点均分拆成 2~3 张，沿用任务 110 的拆图口径：记 `layout_notes`、
# kind="media-split"，页面上「图 1 / 图 2」由 ppt.split_multi_media_slides 落到相邻页。
#: 超限拆图时按哪个 data 字段取节点。**所有能拆的布局都把节点写在 ``nodes``**（见各
#: ``*_scene`` 分支）⇒ 不需要逐布局映射（任务 125 之前把 decision 映射到 ``checks``，
#: 而 ``checks`` 根本不是 DIAGRAM_JSON 的字段：决策图超限时永远取到空列表、静默不拆）。
_CAPACITY_KEY: dict[str, str] = {}


def scene_elements(layout: str, data: dict, caption: str) -> list[dict]:
    """按既有渲染链路出一张图的元素（容量判定 / 拆图验证共用；不落库、不写文件）。"""
    return render(layout, _build_scene(layout, data, caption))


def scene_fits(layout: str, data: dict, caption: str) -> bool:
    """这张图落进页面后是否不超单图容量上限（判据正本：:func:`figure_png.over_capacity`）。"""
    return not over_capacity(scene_elements(layout, data, caption))
_UNSPLITTABLE = ("compare", "decision")   # 结构固定的两类：拆开就不成图（见下）
# compare：左右对比固定两栏；decision：开始 → 条件 → 结论是一条判断链，按节点均分会把
# 条件与结论拆散（每张都要重造 start/good/bad 兜底文案）= 变形，不是拆图 ⇒ 明确列为拆不动，
# 由重跑报告如实记账（任务 125 收口：原来 decision 是「取错 key ⇒ 静默不拆」，现在是明说）。


def _partition(nodes: list, parts: int) -> list[list]:
    """把节点均分成 ``parts`` 组（口径正本：:func:`figure_png.partition_balanced`）。"""
    return partition_balanced(nodes, parts)


def split_scene_by_capacity(layout: str, data: dict, caption: str,
                            *, max_parts: int = MAX_FIGURE_PARTS) -> list[dict]:
    """单图容量上限：超限就把节点均分拆成 2~3 张；没超就原样一张。

    返回 ``[{layout, data, caption, part, parts, note}]``（``note`` 是拆图理由，没拆时为 None）。
    拆不动（只有一个节点 / 结构固定）时原样返回一张 —— **绝不截断内容**。
    """
    if scene_fits(layout, data, caption) or layout in _UNSPLITTABLE:
        return [{"layout": layout, "data": data, "caption": caption,
                 "part": 1, "parts": 1, "note": None}]
    key = _CAPACITY_KEY.get(layout, "nodes")
    nodes = list(data.get(key) or [])
    if len(nodes) < 2:
        return [{"layout": layout, "data": data, "caption": caption,
                 "part": 1, "parts": 1, "note": None}]
    best: list[dict] | None = None
    for parts in range(2, int(max_parts) + 1):
        groups = _partition(nodes, parts)
        if len(groups) < 2:
            break
        cand: list[dict] = []
        for i, group in enumerate(groups, 1):
            part_data = dict(data)
            part_data[key] = group
            # 头尾的「结论/提示」类字段只留在第一张（避免同一句结论在两张图上重复）；
            # center / good / bad / start 是布局的必需字段，每张都要有。
            if i > 1:
                for field in ("subtitle", "tip", "tip_emoji", "banner"):
                    part_data.pop(field, None)
            cand.append({"layout": layout, "data": part_data, "caption": caption,
                         "part": i, "parts": parts, "note": None})
        best = cand
        if all(scene_fits(layout, c["data"], caption) for c in cand):
            break
    assert best is not None
    parts = best[0]["parts"]
    for entry in best:
        entry["note"] = {
            "kind": "media-split",
            "figure": f"图 {entry['part']}/{parts}",
            "reason": f"单图容量上限（图形 ≤{MAX_FIGURE_SHAPES} · 文本 ≤{MAX_FIGURE_TEXT_ELEMENTS}）"
                      f"：原图超出上限 ⇒ 均分拆成 {parts} 张",
        }
    return best


def persist_diagram_from_data(
    db: Session,
    course: Course,
    node: OutlineNode | None,
    layout: str,
    caption: str,
    data: dict,
    *,
    title: str | None = None,
    meta_extra: dict | None = None,
    theme: str | None = None,
) -> ContentItem:
    """把已定稿的 DIAGRAM_JSON data 渲染落库，返回第一张（沿用原签名；超限时另存「图 2…」）。"""
    return persist_diagrams_from_data(
        db, course, node, layout, caption, data, title=title, meta_extra=meta_extra,
        theme=theme)[0]


def persist_diagrams_from_data(
    db: Session,
    course: Course,
    node: OutlineNode | None,
    layout: str,
    caption: str,
    data: dict,
    *,
    title: str | None = None,
    meta_extra: dict | None = None,
    split: bool = True,
    theme: str | None = None,
) -> list[ContentItem]:
    """单图容量上限的执行点：超限拆成 2~3 张落库（返回全部条目，第一张是主图）。

    ``split=False`` 只画一张（对照用，也是反证据用例的开关）。拆图理由逐个写进图条目的
    ``meta.layout_notes``（kind="media-split"，沿用任务 110 的记法）。
    """
    plans = split_scene_by_capacity(layout, data, caption) if split else [
        {"layout": layout, "data": data, "caption": caption, "part": 1, "parts": 1, "note": None}]
    items: list[ContentItem] = []
    for plan in plans:
        one_title = title
        if plan["parts"] > 1 and title:
            one_title = f"{title}（图 {plan['part']}/{plan['parts']}）"
        extra = dict(meta_extra or {})
        if plan["note"]:
            extra["layout_notes"] = [plan["note"], *list(extra.get("layout_notes") or [])]
        items.append(_persist_one(db, course, node, plan["layout"], plan["caption"],
                                  plan["data"], title=one_title, meta_extra=extra or None,
                                  theme=theme))
    return items


#: 拆分后的条目标题后缀：「（图 i/N）」只加一次（重跑幂等的前提之一）。
_PART_SUFFIX_RE = re.compile(r"（图 \d+/\d+）$")
#: 另建的「图 2…」要继承的归属字段（讲义卡片那一套；不带 layout_notes / history / vault 状态）。
_CARD_META_KEYS = ("from_lecture", "role", "card_anchor", "card_seq", "chart_type", "node_key")


def _part_title(title: str, part: int, parts: int) -> str:
    """拆图后的条目标题（``（图 i/N）`` 只加一次，重跑不会叠成「（图 1/2）（图 1/2）」）。"""
    base = _PART_SUFFIX_RE.sub("", str(title or ""))
    return base if parts <= 1 else f"{base}（图 {part}/{parts}）"


def _card_meta(item: ContentItem) -> dict:
    """另建的那几张图要继承的归属字段（讲义引用 / 卡片序号那一套）。"""
    meta = dict(item.meta or {})
    return {key: meta[key] for key in _CARD_META_KEYS if key in meta}


def rerender_diagram_item(db: Session, item: ContentItem, *,
                          split: bool = True, theme: str | None = None) -> list[ContentItem]:
    """按条目自带的原始 data **就地重跑**图生成（0 LLM）：本条原地更新，超限的其余张另建。

    任务 124 的「重新生成图」入口的一步：容量上限 / 字号口径改了之后，**存量图不会自己变**
    （重建课件只复用库里既有的元素），必须显式重跑这条链路。原始 ``DIAGRAM_JSON`` 从任务 124
    起落在 ``body.data``（见 :func:`_persist_one`）；之前的存量图没有这一份 ⇒ 显式报错，
    不拿渲染后的 elements 反推（反推出来的不是原始 nodes，硬造等于重新编一版内容）。

    本条图的 ``id`` / ``title`` / 讲义里的 ``diagram://<id>`` 引用**都不动**（元素与
    ``svg_preview`` 那一份缓存就地覆盖 ⇒ 不留孤儿文件）；超限的其余张沿用
    :func:`persist_diagrams_from_data` 的口径另建条目，理由同样记进 ``meta.layout_notes``。
    """
    body = dict(item.body or {})
    data = body.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"diagram {item.id} 没有原始 data：存量图无法重跑，只能重新生成")
    layout = str(body.get("layout") or "")
    caption = str(body.get("caption") or "")
    course = db.get(Course, item.course_id)
    node = db.get(OutlineNode, item.node_id) if item.node_id else None
    plans = split_scene_by_capacity(layout, data, caption) if split else [
        {"layout": layout, "data": data, "caption": caption,
         "part": 1, "parts": 1, "note": None}]
    first, rest = plans[0], plans[1:]
    if len(plans) > 1:
        # 拆图后第 1 张也要标分册（任务 125）：与第 2 张同一条 _part_title 口径，
        # 且**先剥旧后缀再加** ⇒ 重跑幂等、不会叠成「（图 1/2）（图 1/2）」。
        # 条目 id 与讲义里的 diagram://<id> 引用都不动（引用按 id 认，不按标题）。
        item.title = _part_title(item.title, first["part"], first["parts"])
    doc, svg = _scene_art(first["layout"], first["data"], first["caption"], theme=theme)
    body["layout"] = first["layout"]
    body["caption"] = first["caption"]
    body["excalidraw_json"] = doc
    body["data"] = copy.deepcopy(first["data"])
    body["svg_preview"] = _write_svg(svg, str(body.get("svg_preview") or "").strip() or None)
    item.body = body
    if first["note"]:
        meta = dict(item.meta or {})
        meta["layout_notes"] = [first["note"], *list(meta.get("layout_notes") or [])]
        item.meta = meta
    db.commit()
    db.refresh(item)
    try:
        attach_vault_write(db, item, "diagram")
    except Exception:  # noqa: BLE001 —— Vault 增量失败不影响主流程
        logger.warning("vault incremental write skipped for diagram %s", item.id)
    extra: list[ContentItem] = []
    for plan in rest:
        extra += persist_diagrams_from_data(
            db, course, node, plan["layout"], plan["caption"], plan["data"],
            title=_part_title(item.title, plan["part"], plan["parts"]),
            meta_extra=_card_meta(item), theme=theme)
    return [item, *extra]


def generate_diagrams(
    db: Session,
    course: Course,
    node: OutlineNode | None = None,
    diagram_type: str | None = None,
    instruction: str = "",
    meta_extra: dict | None = None,
) -> list[ContentItem]:
    """为一个知识点生成 Excalidraw 概念图并落库，返回**全部**条目。

    单图容量超限时是「… （图 1/2）」「… （图 2/2）」两张（任务 120/125）⇒ 调用方要**逐张**
    引用进讲义、并用 ``meta_extra`` 给每一张挂归属：只认第 1 张的话，拆出的后几张在交付包里
    就是没人引用的孤儿文件（导出体检会报出来）。只需要一张的调用方用 :func:`generate_diagram`。
    """
    with usage_log.collect() as collector:
        items = _generate_diagrams_inner(db, course, node, diagram_type, instruction,
                                         meta_extra=meta_extra)
        first = items[0] if items else None
        if (collector.depth == 1 and first is not None
                and usage_log.attach(first, collector) is not None):
            db.commit()
        return items


def generate_diagram(
    db: Session,
    course: Course,
    node: OutlineNode | None = None,
    diagram_type: str | None = None,
    instruction: str = "",
    meta_extra: dict | None = None,
) -> ContentItem:
    """同 :func:`generate_diagrams`，只返回第一张（沿用原签名；超限拆出的其余张也在库里）。"""
    return generate_diagrams(db, course, node, diagram_type, instruction, meta_extra)[0]


def _generate_diagrams_inner(
    db: Session,
    course: Course,
    node: OutlineNode | None = None,
    diagram_type: str | None = None,
    instruction: str = "",
    meta_extra: dict | None = None,
) -> list[ContentItem]:
    node_title = (node.title if node is not None else course.title) or course.title
    objective = ((node.objective if node is not None else "") or instruction or node_title)
    layout = _resolve_layout(diagram_type, objective, node_title)
    client = build_llm_client()
    node_key = node.node_key if node is not None else "课程级"
    user = (
        f"当前知识点：{node_title}（编号 {node_key}）\n"
        f"学习目标：{objective}\n"
        f"布局类型：{layout}（请按 DIAGRAM_JSON 规则给出该布局所需 nodes）\n"
    )
    if instruction:
        user += f"补充说明：{instruction}\n"
    data = usage_log.complete_json(client, DIAGRAM_SYSTEM, user,
                                   max_tokens=1600, purpose="diagram")
    if not isinstance(data, dict):
        raise ValueError("DIAGRAM_JSON 解析失败：未返回对象")
    caption = _title_of(data, f"{node_title} · 一图看懂")
    # 单图容量上限（任务 120/125）：超限自动拆成「图 1 / 图 2」，**全部**返回给调用方
    return persist_diagrams_from_data(
        db, course, node, layout, caption, data,
        title=f"{node_title} · 概念图", meta_extra=meta_extra,
    )
