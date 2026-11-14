# -*- coding: utf-8 -*-
"""PPT v4 内置主题（母版级）：五套色板 + 字号层级 + 字体 + 页脚样式。

同一套色板同时驱动 python-pptx 渲染（标题 / 正文 / 页脚 / 原生图表 / 表格）与
:mod:`app.services.figure_png` 的概念图 PNG，保证整份课件色调统一。

配置项 ``PPT_THEME`` 可以给主题 id（``clean`` / ``professional`` / ``handdrawn`` /
``modern`` / ``navy``）、中文名，或历史遗留的 dashi 主题 id；未知名字回落到默认主题，
并**显式返回原因**（调用方记进 ``meta.theme_error``，不静默）。

字体（任务 144）：每套主题自持一个 ``font`` 字段（``latin`` / ``cjk``），**只声明不嵌入**
（python-pptx 不支持嵌入字体，且会牵动交付包体积与字体分发授权）。``ppt.py`` 在 pptx 落盘
后把 ``a:latin`` / ``a:ea`` / ``a:cs`` 写进包里**每个**文字属性（``a:rPr`` / ``a:defRPr`` /
``a:endParaRPr``，含 notesSlides）—— 只写 ``run.font.name`` 等于只写 ``a:latin``，
**中文一个字都不会变**（中文走 ``a:ea``，python-pptx 默认模板的 ``a:ea`` 是空串、
``a:font script="Hans"`` 是宋体，这才是「模板感」的第一来源）。
"""
from __future__ import annotations

import copy

THEME_CLEAN = "clean"
THEME_PROFESSIONAL = "professional"
THEME_HANDDRAWN = "handdrawn"
THEME_MODERN = "modern"
THEME_NAVY = "navy"
DEFAULT_THEME = THEME_CLEAN

#: 主题字体（**每套主题各写一份自己的 ``font`` 字段**，方便以后单独给某套换字体）。
#: ``latin`` 写进 ``a:latin`` 与 ``a:cs``，``cjk`` 写进 ``a:ea``。
DEFAULT_FONT = {"latin": "Microsoft YaHei", "cjk": "微软雅黑"}

THEMES: dict[str, dict] = {
    THEME_CLEAN: {
        "id": THEME_CLEAN,
        "name": "清简教学",
        "description": "白底、低饱和、大留白，日常课堂投影最耐看",
        # 任务 144：**就地升级**调色板（id / name 一字不改）—— 存量课的配置与库里记的主题名
        # 就是 clean，就地升级 ⇒ 不碰任何存量数据、重出一次就变好看。
        # 旧值（对照用）：accent/title/cover_title=1D4ED8、muted/caption=64748B、footer=94A3B8、
        # panel=F1F5F9、rule=DBEAFE、colors=(4C8DF6,F59E0B,10B981,8B5CF6,EF4444,06B6D4)。
        "palette": {
            "accent": "4338CA", "title": "4338CA", "cover_title": "4338CA",
            "secondary": "0F766E",
            "text": "1F2937", "muted": "475569", "caption": "475569",
            "footer": "64748B", "bg": "FFFFFF", "panel": "EEF2FF", "rule": "C7D2FE",
            "colors": ("4338CA", "0E7490", "B45309", "15803D", "BE123C", "6D28D9"),
            "warm": False,
        },
        "sizes": {"cover_title": 44, "title": 32, "body": 18, "body_dense": 16,
                  "caption": 12, "footer": 10},
        "footer_style": "plain",
        "title_band": False,
        "round": False,
        "font": dict(DEFAULT_FONT),
        "figure": {"bg": "#ffffff", "ink": "#1f2937", "edge": "#94a3b8"},
    },
    THEME_PROFESSIONAL: {
        "id": THEME_PROFESSIONAL,
        "name": "专业培训",
        "description": "深色标题条 + 蓝灰体系，适合对外培训与客户交付",
        "palette": {
            "accent": "2563EB", "title": "FFFFFF", "cover_title": "1E293B",
            "secondary": "0EA5E9",
            "text": "1E293B", "muted": "475569", "caption": "475569",
            "footer": "94A3B8", "bg": "FFFFFF", "panel": "1E293B", "rule": "3B82F6",
            "colors": ("2563EB", "0EA5E9", "14B8A6", "F59E0B", "8B5CF6", "F43F5E"),
            "warm": False,
        },
        "sizes": {"cover_title": 40, "title": 28, "body": 18, "body_dense": 16,
                  "caption": 12, "footer": 10},
        "footer_style": "band",
        "title_band": True,
        "round": False,
        "font": dict(DEFAULT_FONT),
        "figure": {"bg": "#f8fafc", "ink": "#1e293b", "edge": "#94a3b8"},
    },
    THEME_HANDDRAWN: {
        "id": THEME_HANDDRAWN,
        "name": "手绘亲和",
        "description": "暖色纸感 + 圆角，适合低龄段与工作坊",
        "palette": {
            "accent": "B45309", "title": "B45309", "cover_title": "B45309",
            "secondary": "0F766E",
            "text": "44403C", "muted": "78716C", "caption": "78716C",
            "footer": "A8A29E", "bg": "FFFBF2", "panel": "FEF3C7", "rule": "FDE68A",
            "colors": ("F97316", "EF4444", "F59E0B", "14B8A6", "6366F1", "0EA5E9"),
            "warm": True,
        },
        "sizes": {"cover_title": 40, "title": 28, "body": 18, "body_dense": 16,
                  "caption": 12, "footer": 10},
        "footer_style": "hand",
        "title_band": False,
        "round": True,
        "font": dict(DEFAULT_FONT),
        "figure": {"bg": "#fffbf2", "ink": "#44403c", "edge": "#d6d3d1"},
    },
    # 任务 144 新增：现代简约（深墨字 + 一个高饱和强调色、圆角、无标题条；与 clean 的
    # 靛蓝直角盘明显不同）。
    THEME_MODERN: {
        "id": THEME_MODERN,
        "name": "现代简约",
        "description": "深墨字 + 高饱和紫罗兰强调色，圆角卡片、无标题条，适合产品与设计类课程",
        "palette": {
            "accent": "6D28D9", "title": "111827", "cover_title": "111827",
            "secondary": "475569",
            "text": "111827", "muted": "4B5563", "caption": "4B5563",
            "footer": "6B7280", "bg": "FFFFFF", "panel": "F5F3FF", "rule": "DDD6FE",
            "colors": ("6D28D9", "0891B2", "EA580C", "16A34A", "DB2777", "4F46E5"),
            "warm": False,
        },
        "sizes": {"cover_title": 44, "title": 32, "body": 18, "body_dense": 16,
                  "caption": 12, "footer": 10},
        "footer_style": "plain",
        "title_band": False,
        "round": True,
        "font": dict(DEFAULT_FONT),
        "figure": {"bg": "#ffffff", "ink": "#111827", "edge": "#9ca3af"},
    },
    # 任务 144 新增：深蓝商务（深色页眉带 + 白字标题、蓝绿系色板；比 professional 更克制）。
    THEME_NAVY: {
        "id": THEME_NAVY,
        "name": "深蓝商务",
        "description": "深蓝标题带 + 白字标题、干净的蓝绿系色板，对外商务交付更正式",
        "palette": {
            "accent": "0E7490", "title": "FFFFFF", "cover_title": "0F2A47",
            "secondary": "0369A1",
            "text": "0F172A", "muted": "475569", "caption": "475569",
            "footer": "64748B", "bg": "FFFFFF", "panel": "0F2A47", "rule": "0E7490",
            "colors": ("0E7490", "1D4ED8", "0F766E", "0369A1", "B45309", "7C3AED"),
            "warm": False,
        },
        "sizes": {"cover_title": 44, "title": 32, "body": 18, "body_dense": 16,
                  "caption": 12, "footer": 10},
        "footer_style": "band",
        "title_band": True,
        "round": False,
        "font": dict(DEFAULT_FONT),
        "figure": {"bg": "#f8fafc", "ink": "#0f172a", "edge": "#94a3b8"},
    },
}

# 中文名与历史主题 id → 内置主题
ALIASES: dict[str, str] = {
    "清简教学": THEME_CLEAN, "专业培训": THEME_PROFESSIONAL, "手绘亲和": THEME_HANDDRAWN,
    "现代简约": THEME_MODERN, "深蓝商务": THEME_NAVY,
    "theme01": THEME_CLEAN, "theme07": THEME_PROFESSIONAL, "theme03": THEME_PROFESSIONAL,
    "theme09": THEME_HANDDRAWN, "theme11": THEME_HANDDRAWN,
}


def theme_names() -> list[str]:
    """五套内置主题的中文名（按定义顺序）。"""
    return [t["name"] for t in THEMES.values()]


def resolve_theme(name: str | None) -> tuple[dict, str | None]:
    """解析主题名 → ``(主题定义副本, 回退原因)``；未知名字回落默认主题并给出原因。"""
    raw = str(name or "").strip()
    if not raw:
        return copy.deepcopy(THEMES[DEFAULT_THEME]), None
    key = ALIASES.get(raw) or ALIASES.get(raw.lower()) or raw.lower()
    if key in THEMES:
        return copy.deepcopy(THEMES[key]), None
    return copy.deepcopy(THEMES[DEFAULT_THEME]), (
        f"未知主题「{raw}」，已回退默认主题「{THEMES[DEFAULT_THEME]['name']}」")


def theme_keys() -> tuple[str, ...]:
    """允许的主题 id（含历史别名），供配置校验与接口校验使用。"""
    return tuple(THEMES) + tuple(ALIASES)


def theme_options() -> list[dict]:
    """给前端的主题下拉：id / 中文名 / 说明 / 主色。"""
    out: list[dict] = []
    for spec in THEMES.values():
        out.append({"id": spec["id"], "name": spec["name"], "description": spec["description"],
                    "accent": spec["palette"]["accent"], "colors": list(spec["palette"]["colors"])})
    return out
