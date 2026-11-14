# -*- coding: utf-8 -*-
"""概念图 → PNG 重绘（Pillow），供 PPT 课件插图使用。

库内 ``diagram`` / ``chart_mindmap`` / ``chart_timeline`` 的 body 里存着
``excalidraw_json``（Excalidraw 元素列表）；本模块用 Pillow 直接把这些元素重绘成位图
（不新增 C 依赖、不走 SVG→PNG 转换链），几何语义与
:func:`app.services.diagram.diagram_to_svg` 保持一致：

- rectangle（圆角 rx）/ ellipse / diamond / line+arrow（含箭头）/ text（多行、居中或左对齐）；
- 画布按元素包围盒自适应（带内边距），输出宽度不小于 ``MIN_WIDTH``（默认 1200px）；
- 图内正文按「上页后等效磅数 ≥ ``MIN_TEXT_PT``」抬字号（见 :func:`min_text_px`），
  不再用与页上尺寸无关的「8 像素」护栏；
- 字体按序探测 Noto Sans SC → 思源黑体 → msyh.ttc → simhei.ttf（OFL 免费可商用优先，
  微软雅黑只是兜底）；全部探测不到时抛
  :class:`FigureError`（调用方记录原因并退回文字版，绝不静默）；
- 中文按字宽、英文按词宽折行，文字不溢出画布。

产物按 ``figures/fig-<节点key>-<序号>.png`` 稳定命名；同名文件旁写一份 ``.sha1``
指纹，内容未变时不再重复渲染（幂等）。
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFont

from app.core.config import get_settings
from app.services.ppt_theme import resolve_theme

MIN_WIDTH = 1200          # 输出最小宽度（px）
PAD = 14.0                # 画布内边距（源坐标单位）；任务 120 由 28 收到半档（见下「白边档位」）
MAX_SCALE = 4.0           # 放大上限，避免小图被拉成巨幅
DEFAULT_BG = "#fffbf2"    # 与 excalidraw_layout.BG 一致
DEFAULT_INK = "#374151"
DEFAULT_EDGE = "#94a3b8"
LINE_HEIGHT = 1.35
SIGNATURE_SUFFIX = ".sha1"

# —— 可读性口径（任务 107）——
# 图内文字「上页后的等效磅数」= 该文字在 PNG 里的像素字号 ÷ PNG 宽度(px)
# × 图在页上的显示宽度(英寸) × 72。正文等效磅数 < MIN_TEXT_PT 视为不合格：渲染时按
# 参考档位（= ppt.MEDIA_COLUMN_IN，最窄的图档）把字号抬到下限。
MIN_TEXT_PT = 9.0                 # 正文等效磅数下限
REFERENCE_MEDIA_WIDTH_IN = 6.90   # 参考显示宽度（英寸，与 ppt.MEDIA_COLUMN_IN 同口径）
REFERENCE_MEDIA_HEIGHT_IN = 4.95  # 参考显示高度（英寸，与 ppt.CONTENT_HEIGHT_IN 同口径）
MAX_FONT_BOOST = 3.0              # 字号上浮上限：超过说明图太密，该拆图（见 layout_notes）
MIN_PIL_FONT_PX = 1               # 交给 PIL 的合法下限；可读性下限由 min_text_px() 负责

# —— 图内字号重排（任务 110）——
# 任务 107 只在**元素自己的框内**抬字号（见 _text_ceiling），而生成侧的元素框是「贴着
# 原字号」算出来的 ⇒ 实测抬不动（free 文本一点都抬不动）。任务 110 改成**重排**：
# 文字占满它所在卡片的内宽、按卡片高度重新居中，字号因此能真正涨上去，而**卡片与画布
# 尺寸一个都不动** —— 画布宽度不变 ⇒ 等效磅数（字号 ÷ 画布宽 × 显示宽度 × 72）只会
# 变大不会变小。卡片之间不挪窝，所以不会把标签压到别的卡片上。
CARD_PAD_UNITS = 4.0        # 卡片内边距（源单位）：重排后文字不贴卡片边（任务 120 收到半档）
TEXT_GAP_UNITS = 3.0        # 同一卡片内相邻文本的最小间距（源单位，任务 120 收到半档）
REFLOW_SAFETY_UNITS = 1.0   # 版面安全余量（源单位，任务 120 收到半档）
REFLOW_STEPS = 30           # 自由文本可用倍数的搜索步数（确定：同输入同结果）
# 容器可用宽度系数：文字在菱形/椭圆里能用的净宽比外接矩形小
CONTAINER_FIT = {"rectangle": 1.0, "ellipse": 0.85, "diamond": 0.62}
CONTAINER_TYPES = tuple(CONTAINER_FIT)

# —— 字号四档（任务 120）——
# 图内字号原先散落在 13~28 度的十来个值上，层级糊在一起（大小夹在中间「不上不下」）。这里收成
# 四档 **标题 / 小标题 / 正文 / 注释**，**档距用黄金比 φ = 1.618**（不是随手取数）：
#   注释 φ⁰ = 9.00 磅（= 既有护栏 MIN_TEXT_PT，最低档钉在可读性下限上）
#   正文 φ¹ = 14.56 · 小标题 φ² = 23.57 · 标题 φ³ = 38.12（磅）
# 源坐标单位里另锚一次（同一个 φ 级数）：小标题 = 20 单位（现语料里卡片标题/节点标题最常见的那档，
# 而且它决定卡片宽度、进而决定画布跨度，锚在这里跨度最稳）⇒ 注释 7.64 / 正文 12.36 /
# 小标题 20.00 / 标题 32.36。要锚两次是因为「源坐标 → 上页磅数」还要乘一个「画布跨度」因子
# （本模块的不变量，见 excalidraw_layout 顶部注释），两个域各锚一次才可比。
GOLDEN_RATIO = 1.618
FONT_TIERS = ("caption", "body", "subtitle", "title")
_TIER_STEP = {name: i for i, name in enumerate(FONT_TIERS)}
TIER_BASE_UNITS = 20.0        # 源坐标域的锚：小标题档（卡片标题/节点标题的最常见字号）
TIER_UNITS_ANCHOR = "subtitle"  # 上面这个锚落在哪一档
TIER_STROKE_BASE = 1.0        # 线宽的锚（注释档 1.0）
TIER_RADIUS_BASE = 4.0        # 圆角的锚（注释档 4.0）
DEFAULT_CORNER_UNITS = 14.0   # 老元素（没写 roundness.value）的圆角，保持既有观感

# —— 单图容量上限（任务 120 定值 · 任务 125 收口计数口径）——
# 实测真课 44 张概念图（docs/task120-figure-tiers.md §验收），按图里的**图形个数**分档：
#   4 个 → min_pt 中位 16.64（span 中位 790）· 5 个 → 12.72（1154）· 6 个 → 11.46（1544）
#   7 个 → 7.98（1934）—— 图形越多跨度越大，跨度越大等效磅数越低（本模块的不变量）。
# ≤6 个的 35 张图 min_pt **全部 ≥9.52 磅**；7 个的 9 张掉到最低 7.57 ⇒ 上限取 6。
MAX_FIGURE_SHAPES = 6          # 单图图形（卡片/节点框）上限
MAX_FIGURE_TEXT_ELEMENTS = 20  # 同源：6 张卡 × 每卡最多 3 条文字（18）+ 页头/提示（2）
MAX_FIGURE_PARTS = 3           # 超限最多拆成几张（沿用任务 110 的拆图口径）
#: 「图形元素」的计数口径（**正本就在这一处**，任务 125 收口）：= 渲染后 type 属于
#: :data:`FIGURE_SHAPE_TYPES` 的元素个数。三类都是「能装字的节点框」，与
#: :data:`CONTAINER_TYPES` 同源；**标题条矩形与中心圆也算** —— 任务 120 当年量拐点的
#: ``cards`` 就是这三类的计数（`cap120c.py`）⇒ 口径逐字一致，上限 6 才是同一个 6。
#: **不算** line / arrow / image：连线与箭头的条数随节点数同步增长（再计一次是重复计数），
#: image 只在数据图里当数据标记、不是节点框。
FIGURE_SHAPE_TYPES = ("rectangle", "ellipse", "diamond")


# —— 图内配色派生自 PPT 主题（任务 121）——
# 断点（研究结论）：图内原来写死 7 组 pastel 轮换（PASTEL）+ 8 个布局主色（ACCENTS）+ 提示条色
# （TIP_ACCENT）+ 思维导图 6 组绿 + 流程图 3 套，与 PPT 侧的 `ppt_theme` 6 色**互不相干** ——
# 同一页上「卡片装饰用主题色、图内节点用 pastel 轮换色」，看起来不像一套课件。
# 现在图内**不再自带色板**：一律从 `ppt_theme.resolve_theme()` 的 6 色派生（与
# `chart_data.active_palette()` 同一出处），换主题时整套图跟着换。派生规则（三色法）：
#   · 一张图 = **单色阶**（一个色相的多档明度）+ **一个强调色**（第二个色相）+ 中性灰；
#   · 描边 / 主色 = 主题色**原样**（字面 ⊆ 主题 6 色）；
#   · 填充 / 浅底 = 同色相兑白（TINT_STEPS 两档；提示条底用最浅的 TINT_TIP）；
#   · 浅底上的文字 = 同色相**压暗**（SHADE_STEP）—— 不是新色相，是同一色阶的另一头；
#   · 中性（墨 / 次要 / 边 / 浅面）不走派生：跨主题恒定，PPT 侧用的也是这一族。
# 取色不参与任何几何计算 ⇒ 磅数与显示宽度零回归（见 tests/test_figure_theme.py）。
TINT_STEPS = (0.86, 0.70)      # 单色阶两档（卡片填充交替用）
TINT_SOFT = 0.62               # 同色阶更浅一档（浅边框 / 细分隔线）
TINT_TIP = 0.92                # 同色阶最浅一档（提示条底）
SHADE_STEP = 0.52              # 同色相压暗档（浅底上的标题 / 文字）
BAND_LIGHT = 0.92              # 单色阶明度带：浅端（饼图扇区 / 多档序列）
BAND_DARK = 0.50               # 单色阶明度带：深端
#: 中性色封闭集（跨主题恒定；彩色一律走派生，见 :class:`FigurePalette`）
NEUTRALS = ("#374151", "#1f2937", "#334155", "#475569", "#64748b", "#94a3b8",
            "#cbd5e1", "#e2e8f0", "#f8fafc", "#ffffff")


def fit_display_width_in(canvas_w: float, canvas_h: float,
                         box_w_in: float, box_h_in: float) -> float:
    """图等比缩放（不拉伸）进 ``box_w_in × box_h_in`` 后实际占用的宽度（英寸）。"""
    if canvas_w <= 0 or canvas_h <= 0:
        return 0.0
    ratio = float(canvas_w) / float(canvas_h)
    if ratio >= float(box_w_in) / max(float(box_h_in), 0.1):
        return float(box_w_in)
    return float(box_h_in) * ratio


def pt_equivalent(px: float, canvas_w_px: float, display_width_in: float) -> float:
    """像素字号 → 上页后的等效磅数。"""
    if canvas_w_px <= 0:
        return 0.0
    return float(px) / float(canvas_w_px) * float(display_width_in) * 72.0


def px_for_pt(pt: float, canvas_w_px: float, display_width_in: float) -> float:
    """等效磅数 → 该画布宽度下需要的像素字号（:func:`pt_equivalent` 的逆运算）。"""
    if canvas_w_px <= 0 or display_width_in <= 0:
        return 0.0
    return float(pt) * float(canvas_w_px) / (float(display_width_in) * 72.0)


def min_text_px(canvas_w_px: float, canvas_h_px: float, *,
                display_box: tuple[float, float] | None = None,
                min_text_pt: float = MIN_TEXT_PT) -> float:
    """该画布上「正文等效磅数 ≥ min_text_pt」所需的像素字号下限（0 = 不设下限）。"""
    if min_text_pt <= 0:
        return 0.0
    box = display_box or (REFERENCE_MEDIA_WIDTH_IN, REFERENCE_MEDIA_HEIGHT_IN)
    width = fit_display_width_in(canvas_w_px, canvas_h_px, box[0], box[1])
    return px_for_pt(min_text_pt, canvas_w_px, width)


def source_font_units_for_pt(span_units: float, *,
                             display_width_in: float = REFERENCE_MEDIA_WIDTH_IN,
                             min_text_pt: float = MIN_TEXT_PT) -> float:
    """生成侧护栏：跨度 ``span_units``（源单位）的图，正文源字号至少要多大（源单位）。

    与 :func:`pt_equivalent` 同一口径：``字号 / 画布单位`` 才是决定上页磅数的量，
    所以「调大源字号」的有效做法是把字号抬到本函数的返回值以上。
    """
    if display_width_in <= 0:
        return 0.0
    return float(min_text_pt) * (float(span_units) + PAD * 2) / (float(display_width_in) * 72.0)


def max_span_units_for_pt(font_units: float, *,
                          display_width_in: float = REFERENCE_MEDIA_WIDTH_IN,
                          min_text_pt: float = MIN_TEXT_PT) -> float:
    """生成侧护栏的另一半：源字号 ``font_units`` 的图，跨度不许超过多少（超了要拆图）。"""
    if min_text_pt <= 0:
        return math.inf
    return float(font_units) * float(display_width_in) * 72.0 / float(min_text_pt) - PAD * 2


def tier_pt(tier: str) -> float:
    """字号四档的等效磅数（φ 级数，最低档 = 既有护栏 :data:`MIN_TEXT_PT`）。"""
    return round(MIN_TEXT_PT * GOLDEN_RATIO ** _TIER_STEP[tier], 2)


def tier_units(tier: str) -> float:
    """字号四档的源字号（源坐标单位，φ 级数，小标题档 = :data:`TIER_BASE_UNITS`）。"""
    return round(TIER_BASE_UNITS
                 * GOLDEN_RATIO ** (_TIER_STEP[tier] - _TIER_STEP[TIER_UNITS_ANCHOR]), 2)


def tier_stroke(tier: str) -> float:
    """该档的线宽（源坐标单位，与字号同一 φ 级数、同一档位顺序）。"""
    return round(TIER_STROKE_BASE * GOLDEN_RATIO ** _TIER_STEP[tier], 1)


def tier_radius(tier: str) -> float:
    """该档的圆角半径（源坐标单位，与字号同一 φ 级数、同一档位顺序）。"""
    return round(TIER_RADIUS_BASE * GOLDEN_RATIO ** _TIER_STEP[tier], 1)


def figure_shape_count(elements: list[dict]) -> int:
    """一张图里的**图形元素**（节点框：矩形 / 椭圆 / 菱形）个数。

    计数口径的正本在 :data:`FIGURE_SHAPE_TYPES` 上方（与任务 120 量拐点的口径逐字一致）：
    标题条矩形与中心圆都算，line / arrow / image 都不算。
    """
    return sum(1 for el in elements
               if isinstance(el, dict) and str(el.get("type") or "") in FIGURE_SHAPE_TYPES)


def figure_text_count(elements: list[dict]) -> int:
    """一张图里的文本元素数（= 单图容量上限的另一半度量口径）。"""
    return sum(1 for el in elements
               if isinstance(el, dict) and str(el.get("type") or "") == "text"
               and str(el.get("text") or "").strip())


def over_capacity(elements: list[dict], *,
                  text_cap: int = MAX_FIGURE_TEXT_ELEMENTS,
                  shape_cap: int = MAX_FIGURE_SHAPES) -> bool:
    """单图容量是否超限：**两个上限都要判**，任一超 ⇒ 超限 ⇒ 生成侧拆图。

    - 图形元素 > :data:`MAX_FIGURE_SHAPES`（口径见 :data:`FIGURE_SHAPE_TYPES`）；
    - 文本元素 > :data:`MAX_FIGURE_TEXT_ELEMENTS`。

    任务 120 定了这两条上限，任务 125 复测发现只判了文本那一条（矩形+椭圆+菱形 > 6 的
    一张都没拆）⇒ 收口到本函数一处：生成侧、重跑侧、重跑报告三处共用同一个判据。
    """
    return (figure_shape_count(elements) > int(shape_cap)
            or figure_text_count(elements) > int(text_cap))


def partition_balanced(nodes: list, parts: int) -> list[list]:
    """把节点均分成 ``parts`` 组（前几组各多一个；确定、可测、一个不丢）。

    图生成侧的两处拆图共用这一份分组口径：概念图
    （:func:`app.services.diagram.split_scene_by_capacity`）与数据图
    （:func:`app.services.chart.split_chart_by_capacity`）。
    """
    total = len(nodes)
    parts = max(1, int(parts))
    size, extra = divmod(total, parts)
    out: list[list] = []
    start = 0
    for k in range(parts):
        take = size + (1 if k < extra else 0)
        out.append(list(nodes[start:start + take]))
        start += take
    return [group for group in out if group]


def _hex6(value, default: str = "#000000") -> str:
    """归一成小写 ``#rrggbb``（Excalidraw 只认 6 位 hex）。"""
    raw = str(value or "").strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    if len(raw) != 6:
        return default
    try:
        int(raw, 16)
    except ValueError:
        return default
    return "#" + raw.lower()


def _channels(color: str) -> list[int]:
    """``#rrggbb`` → 三个 0–255 通道（输入非法时按 :func:`_hex6` 的兜底色算）。"""
    base = _hex6(color)
    return [int(base[i:i + 2], 16) for i in (1, 3, 5)]


def tint(color: str, ratio: float = TINT_STEPS[0]) -> str:
    """主题色兑白成同一色阶的浅档（``ratio`` 越大越浅；口径与 ``style_library._tint()`` 一致）。"""
    return "#" + "".join(f"{min(255, max(0, round(c + (255 - c) * float(ratio)))):02x}"
                         for c in _channels(color))


def shade(color: str, ratio: float = SHADE_STEP) -> str:
    """主题色压暗成同一色阶的深档（浅底上的文字用；色相不变）。"""
    return "#" + "".join(f"{min(255, max(0, round(c * (1.0 - float(ratio))))):02x}"
                         for c in _channels(color))


def theme_colors(theme: str | None = None) -> tuple[str, tuple[str, ...], str]:
    """图内取色的**唯一出处**：``(主题 id, 主题 6 色, 该主题的图底色)``。"""
    spec, _error = resolve_theme(theme or get_settings().ppt_theme)
    colors = tuple(_hex6(c) for c in spec["palette"]["colors"])
    return str(spec["id"]), colors, _hex6(spec["figure"]["bg"], DEFAULT_BG)


class FigurePalette:
    """一张图的取色器：只有「主题 6 色 + 它们的同色阶明度档 + 中性灰」，没有第二个色源。"""

    def __init__(self, theme: str | None = None) -> None:
        self.theme, self.colors, self.bg = theme_colors(theme)

    def __len__(self) -> int:
        return len(self.colors)

    def base(self, slot: int) -> str:
        """第 ``slot`` 个色相的主色 = 主题色原样（描边用；字面 ⊆ 主题 6 色）。"""
        return self.colors[int(slot) % len(self.colors)]

    def fill(self, slot: int, step: int = 0) -> str:
        """同色阶的浅档（``step`` 0/1 交替，卡片填充用）。"""
        return tint(self.base(slot), TINT_STEPS[int(step) % len(TINT_STEPS)])

    def soft(self, slot: int) -> str:
        """同色阶更浅的一档（浅边框 / 细分隔线）。"""
        return tint(self.base(slot), TINT_SOFT)

    def tip(self, slot: int) -> str:
        """同色阶最浅的一档（提示条底）。"""
        return tint(self.base(slot), TINT_TIP)

    def deep(self, slot: int) -> str:
        """同色相压暗（浅底上的标题 / 文字）。"""
        return shade(self.base(slot))

    def family(self, slot: int) -> dict[str, str]:
        """一个色相家族的五件套（对比栏等用；描边 = 原色，其余 = 同色阶）。"""
        return {"stroke": self.base(slot), "fill": self.fill(slot, 0),
                "soft": self.soft(slot), "tcol": self.deep(slot),
                "chipfill": self.fill(slot, 1)}

    def band(self, slot: int, index: int, count: int) -> tuple[str, str]:
        """单色阶明度带上的第 ``index`` 档（饼图扇区 / 多档序列；都在浅底可读范围内）。"""
        total = max(int(count), 2)
        ratio = BAND_LIGHT + (BAND_DARK - BAND_LIGHT) * (int(index) / (total - 1))
        color = self.base(slot)
        return tint(color, ratio), color


def figure_palette(theme: str | None = None) -> FigurePalette:
    """图生成侧的统一取色入口（概念图 / 数据图 / 流程图共用，口径正本 = 本模块）。"""
    return FigurePalette(theme)


def _channel_span(value: int, base: int, *, mix: bool) -> tuple[float, float] | None:
    """单通道上「取整前的档位」的可行区间（``tint``/``shade`` 都按 ``round()`` 取整 ⇒ 半格误差）。

    近白 / 近黑通道的分母极小 ⇒ 档位本身就**不可辨识**（例如底色 249 时 254 与 255 都合法）；
    用区间求交而不是「算出来的档位 + 容差」，才能既接纳真派生色、又把外来的相近色挡在外面。
    """
    if mix:
        span = 255 - base
        if span <= 0:
            return None
        return ((value - 0.5 - base) / span, (value + 0.5 - base) / span)
    if base <= 0:
        return None
    return (1.0 - (value + 0.5) / base, 1.0 - (value - 0.5) / base)


def _feasible_ratio(value: str, base: str, *, mix: bool) -> float | None:
    """三通道可行区间求交：非空 ⇒ ``value`` 确实是 ``base`` 的一档，返回档位；否则 ``None``。"""
    low, high, used = 0.0, 1.0, False
    for v, b in zip(_channels(value), _channels(base)):
        span = _channel_span(v, b, mix=mix)
        if span is None:
            continue
        used = True
        low, high = max(low, span[0]), min(high, span[1])
        if low > high:
            return None
    if not used:
        return None
    return round((low + high) / 2, 4)


def _mix_ratio(value: str, base: str) -> float | None:
    """``value`` 是不是 ``base`` 兑白得到的一档？是则返回档位（0 = 原色），否则 ``None``。"""
    return _feasible_ratio(value, base, mix=True)


def _darken_ratio(value: str, base: str) -> float | None:
    """``value`` 是不是 ``base`` 压暗得到的一档？是则返回档位，否则 ``None``。"""
    return _feasible_ratio(value, base, mix=False)


def color_origin(value: str, *, theme: str | None = None) -> tuple[str, int | None]:
    """图内一个色值的出处 → ``(出处, 主题槽位)``（验收 ①「证明图内色来自主题」的口径）。

    出处 ∈ ``"theme"``（主题色原样）/ ``"neutral"``（中性灰或该主题图底色，跨主题恒定）/
    ``"tint"``（同色相兑白档）/ ``"shade"``（同色相压暗档）/ ``"unknown"``（既不是主题色、
    也不是它的色阶、又不是中性色 —— 出现即说明这个颜色不是从主题派生的）。槽位 0–5，
    中性 / 未知为 ``None``。
    """
    palette = FigurePalette(theme)
    value = _hex6(value)
    for slot, base in enumerate(palette.colors):
        if value == base:
            return "theme", slot
    if value in NEUTRALS or value == palette.bg:
        return "neutral", None
    for slot, base in enumerate(palette.colors):
        ratio = _mix_ratio(value, base)
        if ratio is not None:
            return "tint", slot
    for slot, base in enumerate(palette.colors):
        ratio = _darken_ratio(value, base)
        if ratio is not None and 0.0 < ratio <= 1.01:
            return "shade", slot
    return "unknown", None


def figure_color_audit(elements: list[dict], *, theme: str | None = None) -> dict:
    """一张图的取色审计（验收证据）：逐色归因 + 出现次数 + 用到的主题色相。

    ``colors`` 为「色值 → 出现次数」，``scopes`` 为「色值 → (出处, 槽位)」，``hues`` 为这张图
    用到的主题色相槽位（三色法上限见 ``docs/task121-figure-theme.md``），``unknown`` 必须为空。
    """
    counts: dict[str, int] = {}
    for el in elements:
        if not isinstance(el, dict) or el.get("isDeleted"):
            continue
        for key in ("strokeColor", "backgroundColor"):
            raw = str(el.get(key) or "").strip()
            if not raw or raw.lower() in ("transparent", "none"):
                continue
            value = _hex6(raw)
            counts[value] = counts.get(value, 0) + 1
    scopes = {value: color_origin(value, theme=theme) for value in counts}
    hues = sorted({slot for source, slot in scopes.values()
                   if source != "neutral" and slot is not None})
    return {"colors": counts, "scopes": scopes, "hues": hues,
            "unknown": sorted(v for v, (source, _slot) in scopes.items() if source == "unknown")}


# —— 旧图配色迁移（任务 127）——
# 问题（``验收-121-122-独立复测``）：任务 121 只改了**生成侧**的取色，任务 124 之前落库的
# 概念图（没有 ``body.data``）元素里的色值是当年写死的 pastel（PASTEL / ACCENTS / TIP_ACCENT /
# _MINDMAP_GREENS / 流程图三套）⇒ 换主题时这些图**一个都不跟**，同一份课件里两套色。
# 反推 ``body.data`` 重渲染是被否掉的做法（反推出来的不是原始 nodes，硬造等于重新编一版内容，
# 见 ``figure_regen`` 顶部口径）⇒ 这里只做**重映射**：几何 / 文本 / 字号一个字节不动，
# 只把「不是本主题派生」的色值按它自己的**色相 + 明度档**落到本主题的同色阶上。
#
# 档位判据用 **WCAG 相对亮度**（不是 HSL 的 L）：真库实测这两者在本项目语料上分得开 ——
# 深色文字（#9a3412 0.094 / #991b1b 0.076 / #5b21b6 0.066）与中档描边（#10b981 0.365 /
# #16a34a 0.269 / #0ea5e9 0.328）的亮度区间不重叠；四档边界见下（RECOLOR_*）。
RECOLOR_GRAY_SAT = 0.12      # 饱和度（HSV）低于它就当灰阶，就近落到中性封闭集
RECOLOR_TIP_LUM = 0.78       # ≥ 此亮度 = 最浅档（提示条底 / 大色块）
RECOLOR_TINT_LUM = 0.62      # 次浅档（卡片填充）
RECOLOR_SOFT_LUM = 0.45      # 中浅档（次级浅面）
RECOLOR_BASE_LUM = 0.16      # ≥ 此亮度 = 主色（描边）；更低 = 压暗档（浅底上的文字）
RECOLOR_FIELDS = ("strokeColor", "backgroundColor")


def _relative_luminance(color: str) -> float:
    """WCAG 相对亮度（0=黑，1=白）：档位判据用它，见上面 RECOLOR_* 的来历。"""
    out = 0.0
    for value, weight in zip(_channels(color), (0.2126, 0.7152, 0.0722)):
        ratio = value / 255.0
        out += weight * (ratio / 12.92 if ratio <= 0.03928
                         else ((ratio + 0.055) / 1.055) ** 2.4)
    return out


def _saturation(color: str) -> float:
    """HSV 饱和度（0=灰，1=纯色）：近灰的色相不可辨 ⇒ 走中性集，不硬派到主题色。"""
    high, low = max(_channels(color)), min(_channels(color))
    return 0.0 if high <= 0 else (high - low) / high


def _hue(color: str) -> float:
    """HSV 色相（0–360 度）。"""
    r, g, b = (v / 255.0 for v in _channels(color))
    high, low = max(r, g, b), min(r, g, b)
    span = high - low
    if span <= 0:
        return 0.0
    if high == r:
        hue = ((g - b) / span) % 6
    elif high == g:
        hue = (b - r) / span + 2
    else:
        hue = (r - g) / span + 4
    return (hue * 60.0) % 360.0


def _nearest_slot(color: str, colors: tuple[str, ...]) -> int:
    """色相最接近的主题槽位（环形距离；并列取靠前的槽位 ⇒ 结果确定）。"""
    hue = _hue(color)
    best, best_gap = 0, None
    for slot, base in enumerate(colors):
        gap = abs(_hue(base) - hue)
        gap = min(gap, 360.0 - gap)
        if best_gap is None or gap < best_gap:
            best, best_gap = slot, gap
    return best


def _nearest_neutral(color: str, palette: FigurePalette) -> str:
    """灰阶色 → 中性封闭集里亮度最接近的一个（跨主题恒定，图内不再自带第二个色源）。"""
    target = _relative_luminance(color)
    pool = [*NEUTRALS, palette.bg]
    return min(pool, key=lambda cand: abs(_relative_luminance(cand) - target))


def recolor_color(value: str, *, theme: str | None = None) -> str:
    """把一个「不是本主题派生」的色值重映射到本主题派生色（任务 127；几何不动，只换色）。

    幂等：已经属于本主题的（主题色原样 / 同色相兑白或压暗档 / 中性集 / 图底色）**原样返回**，
    所以同一张图重映射两次结果不变，也不会把任务 121 之后的派生色改坏。
    非法的空值 / ``transparent`` 原样返回（不参与重映射）。
    """
    raw = str(value or "").strip()
    if not raw or raw.lower() in ("transparent", "none"):
        return str(value or "")
    palette = figure_palette(theme)
    parsed = _hex6(raw, default="")
    if not parsed:
        return str(value)
    if color_origin(parsed, theme=palette.theme)[0] != "unknown":
        return parsed
    if _saturation(parsed) < RECOLOR_GRAY_SAT:
        return _nearest_neutral(parsed, palette)
    slot = _nearest_slot(parsed, palette.colors)
    lum = _relative_luminance(parsed)
    if lum >= RECOLOR_TIP_LUM:
        return tint(palette.base(slot), TINT_TIP)
    if lum >= RECOLOR_TINT_LUM:
        return tint(palette.base(slot), TINT_STEPS[0])
    if lum >= RECOLOR_SOFT_LUM:
        return tint(palette.base(slot), TINT_STEPS[1])
    if lum >= RECOLOR_BASE_LUM:
        return palette.base(slot)
    return shade(palette.base(slot))


def recolor_elements(elements: list[dict], *,
                     theme: str | None = None) -> tuple[list[dict], int]:
    """把一份元素列表里的取色全量重映射到 ``theme``（返回**新列表** + 改动的元素个数）。

    只碰 ``strokeColor`` / ``backgroundColor`` 两个字段；坐标 / 宽高 / 字号 / 线宽 / 圆角 /
    文本 / 对齐一个字节不动 ⇒ 换色不触发任何重排（9 磅 / 6 英寸 / 装饰上限的数值都不在这里）。
    入参元素**不被修改**（逐个浅拷贝后再写）—— 调用方常常直接拿着 ORM 里的 ``body`` 用。
    """
    out: list[dict] = []
    changed = 0
    for el in elements:
        if not isinstance(el, dict):
            out.append(el)
            continue
        row = el
        for key in RECOLOR_FIELDS:
            raw = str(el.get(key) or "").strip()
            if not raw or raw.lower() in ("transparent", "none"):
                continue
            new = recolor_color(raw, theme=theme)
            if _hex6(new, default="") == _hex6(raw, default=""):
                continue
            if row is el:                      # 写前才拷贝：没改动的元素保持同一份对象
                row = dict(el)
            row[key] = new
            changed += 1
        out.append(row)
    return out, changed


# 按序探测的字体（OFL 优先：Noto Sans SC → 思源黑体 → 微软雅黑 → 黑体）
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\NotoSansSC-VF.ttf",
    r"C:\Windows\Fonts\sourcehansansi.ttf",
    r"C:\Windows\Fonts\sourcehansans.ttf",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyh.ttf",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)
# Noto Sans SC 常见安装位置（按 glob 兜底）
FONT_GLOBS = (
    r"C:\Windows\Fonts\NotoSansSC*.otf",
    r"C:\Windows\Fonts\NotoSansSC*.ttf",
    "/usr/share/fonts/**/NotoSansSC*.otf",
    "/usr/share/fonts/**/NotoSansSC*.ttf",
)

# 彩色 emoji 字体（可选）：命中时用 embedded_color 画真 emoji，否则退化为去掉 emoji
EMOJI_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\seguiemj.ttf",
    "/System/Library/Fonts/Apple Color Emoji.ttc",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
)

_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
_EMOJI_RANGES = ((0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x2B00, 0x2BFF))
_EMOJI_SKIP = ("\u200d", "\ufe0f")


class FigureError(RuntimeError):
    """概念图重绘失败（元素为空 / 字体缺失 / 渲染异常）。"""


def find_font_path() -> str | None:
    """按序探测可用中文字体，返回第一个存在的字体文件；都没有则返回 None。"""
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    for pattern in FONT_GLOBS:
        base = pattern.split("*")[0]
        directory = Path(base).parent if base else Path(".")
        name = Path(pattern).name
        if not directory.exists():
            continue
        for path in sorted(directory.glob(name)):
            if path.is_file():
                return str(path)
    return None


def load_font(size: int) -> ImageFont.FreeTypeFont:
    """按字号取字体（带缓存）；探测不到字体时抛 FigureError。"""
    size = max(MIN_PIL_FONT_PX, int(round(size)))
    path = find_font_path()
    if path is None:
        raise FigureError("未探测到可用中文字体（NotoSansSC / sourcehansans / msyh / simhei）")
    key = (path, size)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        font = ImageFont.truetype(path, size)
    except OSError as exc:  # pragma: no cover —— 字体文件损坏
        raise FigureError(f"字体加载失败：{path}（{exc}）") from exc
    _pin_regular_weight(font)
    _FONT_CACHE[key] = font
    return font


def _pin_regular_weight(font: ImageFont.FreeTypeFont) -> None:
    """可变字体显式取 Regular：``NotoSansSC-VF`` 的默认实例是 Thin（wght=100），
    不钉住会画出极细笔画（与原来 msyh Regular 的观感差一截）。"""
    try:
        names = font.get_variation_names() or []
    except OSError:      # 非可变字体（msyh / simhei / 思源黑体静态档）会走到这里
        return
    if b"Regular" in names:
        try:
            font.set_variation_by_name(b"Regular")
        except OSError:  # pragma: no cover —— 字体声明了却没有该实例
            pass


def find_emoji_font_path() -> str | None:
    """探测彩色 emoji 字体；没有就返回 None（调用方退化为去掉 emoji）。"""
    for candidate in EMOJI_FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


def load_emoji_font(size: int):
    """按字号取 emoji 字体；探测不到或加载失败都返回 None（绝不报错）。"""
    path = find_emoji_font_path()
    if path is None:
        return None
    size = max(MIN_PIL_FONT_PX, int(round(size)))
    key = (path, size)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        font = ImageFont.truetype(path, size)
    except OSError:
        return None
    _FONT_CACHE[key] = font
    return font


def is_emoji(ch: str) -> bool:
    code = ord(ch)
    return any(low <= code <= high for low, high in _EMOJI_RANGES)


def strip_emoji(text: str) -> str:
    """去掉 emoji 与零宽连接符（没有 emoji 字体时避免画成方框）。"""
    return "".join(ch for ch in str(text or "") if ch not in _EMOJI_SKIP and not is_emoji(ch))


def is_icon_text(text: str) -> bool:
    """整段是不是「一枚图标」（全部是 emoji，允许 ZWJ / 变体选择符）？

    任务 92 的存量图标归一按这个判「这枚文字是图标还是正文」：``①`` 这类圈号**不是** emoji，
    于是天然落进「不动」那一类（它们是排版序号，不是主题图标）。口径与 :func:`strip_emoji` 同源。
    """
    stripped = "".join(ch for ch in str(text or "") if ch not in _EMOJI_SKIP)
    return bool(stripped) and all(is_emoji(ch) for ch in stripped)


def text_runs(text: str) -> list[tuple[str, bool]]:
    """把一行拆成 (文本, 是否 emoji) 的连续段，供混排绘制。"""
    runs: list[tuple[str, bool]] = []
    for ch in str(text or ""):
        if ch in _EMOJI_SKIP:
            continue
        flag = is_emoji(ch)
        if runs and runs[-1][1] == flag:
            runs[-1] = (runs[-1][0] + ch, flag)
        else:
            runs.append((ch, flag))
    return runs


def elements_of(item) -> list[dict]:
    """从内容项 body 里取出未删除的 Excalidraw 元素。"""
    raw = (getattr(item, "body", None) or {}).get("excalidraw_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise FigureError(f"excalidraw_json 不是合法 JSON：{exc}") from exc
    if isinstance(raw, dict):
        elements = raw.get("elements") or []
    elif isinstance(raw, list):
        elements = raw
    else:
        raise FigureError("该图没有可重绘的 Excalidraw 元素")
    alive = [el for el in elements if isinstance(el, dict) and not el.get("isDeleted")]
    if not alive:
        raise FigureError("该图没有可重绘的 Excalidraw 元素")
    return alive


def element_signature(elements: list[dict], style: dict | None = None, *,
                      display_box: tuple[float, float] | None = None,
                      min_text_pt: float = MIN_TEXT_PT) -> str:
    """元素内容 + 主题色板 + 可读性口径的稳定指纹（用于「同内容不重复渲染」）。"""
    payload = json.dumps([elements, dict(style or {}), list(display_box or []), float(min_text_pt)],
                         ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _rgb(value, default=None):
    text = str(value or "").strip()
    if not text or text.lower() in ("transparent", "none"):
        return default
    try:
        return ImageColor.getrgb(text)
    except ValueError:
        return default


def _tokens(text: str):
    """折行用的最小单位：ASCII 连续串按词，其余（含中文）按字。"""
    buffer = ""
    for ch in text:
        if ch.isascii() and not ch.isspace():
            buffer += ch
            continue
        if buffer:
            yield buffer
            buffer = ""
        yield ch
    if buffer:
        yield buffer


def _wrap_lines(text: str, font, max_w: float) -> list[str]:
    """按像素宽度折行：中文按字、英文按词，超长单token 硬切。"""
    lines: list[str] = []
    for raw in str(text or "").split("\n"):
        if not raw:
            lines.append("")
            continue
        line = ""
        for token in _tokens(raw):
            if line and font.getlength(line + token) > max_w:
                lines.append(line.rstrip())
                line = token.lstrip()
            else:
                line += token
            while font.getlength(line) > max_w and len(line) > 1:
                cut = 1
                while cut < len(line) and font.getlength(line[: cut + 1]) <= max_w:
                    cut += 1
                lines.append(line[:cut])
                line = line[cut:]
        lines.append(line.rstrip())
    return lines or [""]


def _bounds(alive: list[dict]) -> tuple[float, float, float, float]:
    min_x = min(float(el.get("x") or 0) for el in alive)
    min_y = min(float(el.get("y") or 0) for el in alive)
    max_x = max(float(el.get("x") or 0) + float(el.get("width") or 0) for el in alive)
    max_y = max(float(el.get("y") or 0) + float(el.get("height") or 0) for el in alive)
    return min_x, min_y, max_x, max_y


def _corner_radius(el: dict, box: dict) -> float:
    """矩形圆角半径（px）：四档口径写在 ``roundness.value``（type=2）；老元素沿用固定 14 单位。"""
    roundness = el.get("roundness")
    value = None
    if isinstance(roundness, dict):
        try:
            value = float(roundness.get("value"))
        except (TypeError, ValueError):
            value = None
    base = value if value and value > 0 else DEFAULT_CORNER_UNITS
    return min(base * box["scale"], max(0.0, box["w"] / 2), max(0.0, box["h"] / 2))


def _draw_shape(draw: ImageDraw.ImageDraw, el: dict, box: dict) -> None:
    kind = str(el.get("type") or "")
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    stroke = _rgb(el.get("strokeColor"), _rgb(box.get("ink"), _rgb(DEFAULT_INK)))
    fill = _rgb(el.get("backgroundColor"))
    width = box["width"]
    if kind == "rectangle":
        radius = _corner_radius(el, box)
        draw.rounded_rectangle([x, y, x + w, y + h], radius=radius, fill=fill,
                               outline=stroke, width=width)
    elif kind == "ellipse":
        draw.ellipse([x, y, x + w, y + h], fill=fill, outline=stroke, width=width)
    elif kind == "diamond":
        points = [(x + w / 2, y), (x + w, y + h / 2), (x + w / 2, y + h), (x, y + h / 2)]
        draw.polygon(points, fill=fill, outline=stroke)
        if width > 1:
            draw.line([*points, points[0]], fill=stroke, width=width, joint="curve")


def _draw_polyline(draw: ImageDraw.ImageDraw, el: dict, box: dict) -> None:
    raw = el.get("points") or [[0, 0], [el.get("width") or 0, el.get("height") or 0]]
    scale, x, y = box["scale"], box["x"], box["y"]
    points = [(x + float(p[0]) * scale, y + float(p[1]) * scale) for p in raw]
    if len(points) < 2:
        return
    stroke = _rgb(el.get("strokeColor"), _rgb(box.get("edge"), _rgb(DEFAULT_EDGE)))
    width = box["width"]
    draw.line(points, fill=stroke, width=width, joint="curve")
    if el.get("endArrowhead"):
        _draw_arrow_head(draw, points[-2], points[-1], stroke, max(6.0, width * 3.2))


def _draw_arrow_head(draw: ImageDraw.ImageDraw, start, end, color, size: float) -> None:
    angle = math.atan2(end[1] - start[1], end[0] - start[0])
    spread = math.radians(26)
    tip = (end[0], end[1])
    left = (end[0] - size * math.cos(angle - spread), end[1] - size * math.sin(angle - spread))
    right = (end[0] - size * math.cos(angle + spread), end[1] - size * math.sin(angle + spread))
    draw.polygon([tip, left, right], fill=color)


def _draw_runs(draw: ImageDraw.ImageDraw, line: str, x: float, y: float, box: dict,
               align: str) -> bool:
    """emoji 与正文混排；返回 True 表示已用 emoji 字体画过。"""
    runs = [(text, flag) for text, flag in text_runs(line) if text]
    emoji_font = box.get("emoji_font")
    if not runs:
        return True
    if emoji_font is None or not any(flag for _, flag in runs):
        # 居中 = 对齐到元素外框的**中心**（与 SVG 版 diagram.py 同口径：x + w / 2）；
        # 任务 110 修：原先直接把元素左边界当锚点，居中文字整体左偏半个框宽。
        anchor_x = x if align == "left" else x + box["w"] / 2
        draw.text((anchor_x, y), "".join(text for text, _ in runs), font=box["font"],
                  fill=box["color"], anchor="la" if align == "left" else "ma")
        return False
    widths = [(emoji_font if flag else box["font"]).getlength(text) for text, flag in runs]
    cursor = x if align == "left" else x + (box["w"] - sum(widths)) / 2
    for (text, flag), width in zip(runs, widths):
        font = emoji_font if flag else box["font"]
        try:
            draw.text((cursor, y), text, font=font, fill=box["color"],
                      embedded_color=flag, anchor="la")
        except (ValueError, OSError):  # 彩色 emoji 画不了就退化为纯色
            draw.text((cursor, y), text, font=font, fill=box["color"], anchor="la")
        cursor += width
    return all(flag for _, flag in runs)


def _draw_text(draw: ImageDraw.ImageDraw, el: dict, box: dict) -> None:
    text = str(el.get("text") or "")
    if not text.strip():
        return
    size = box["size"]
    font = box["font"]
    align = str(el.get("textAlign") or "center")
    lines = _wrap_lines(text, font, max(20.0, box["w"]))
    if box.get("emoji_font") is None:
        lines = [strip_emoji(line) for line in lines]
    top = box["y"]
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        line_y = top + idx * size * LINE_HEIGHT
        _draw_runs(draw, line, box["x"], line_y, box, align)


def _text_ceiling(el: dict, by_id: dict, scale: float, natural: float) -> float:
    """该文本按自身几何能吸收的最大绘制字号：折行宽度与绘制一致，且画完不越下边界。

    折行宽度取**元素自己的 width**（与 :func:`_draw_text` 同一口径；容器只是允许文字
    占满容器，不许把折行放宽）；下边界取元素与容器的较大者（文字从 ``y`` 往下画）。
    这是「调大源字号」的安全边界 —— 生成侧（excalidraw_layout 等）的卡片**贴着文字**
    排，硬抬会把字压到卡片外（实测 2.25 倍时标签互相重叠），所以上限必须由图自己的
    几何说了算；抬不动的情形由 ``meta.layout_notes`` 如实上报（结构性解法 = 拆图）。
    """
    text = str(el.get("text") or "")
    if not text.strip():
        return natural
    wrap_w = float(el.get("width") or 0) * scale
    if wrap_w <= 1:
        return natural
    top_units = float(el.get("y") or 0)
    bottom_units = top_units + float(el.get("height") or 0)
    container = by_id.get(str(el.get("containerId") or ""))
    if container:
        bottom_units = max(bottom_units,
                           float(container.get("y") or 0) + float(container.get("height") or 0))
    step = max(0.02, (MAX_FONT_BOOST - 1.0) / 10)
    best = natural
    for i in range(1, 11):
        size = natural * (1.0 + step * i)
        try:
            font = load_font(size)
        except FigureError:
            break
        lines = _wrap_lines(text, font, max(20.0, wrap_w))
        drawn_bottom = top_units + len(lines) * size * LINE_HEIGHT / scale
        if drawn_bottom <= bottom_units + 1.0:
            best = size
        else:
            break
    return best


def _text_px(el: dict, scale: float, font_floor: float = 0.0, by_id: dict | None = None) -> float:
    """该文本的绘制像素字号：源字号 × 比例，再抬到可读性下限（受自身框与上限约束）。"""
    natural = float(el.get("fontSize") or 20) * float(scale)
    if font_floor <= 0 or natural <= 0:
        return natural
    # 几何边界无条件生效：元素没有 id / 没有容器时也一样受自身框约束（否则字号能一路
    # 涨到 MAX_FONT_BOOST 倍，把标签压到卡片外）。
    ceiling = min(natural * MAX_FONT_BOOST, _text_ceiling(el, by_id or {}, scale, natural))
    return max(natural, min(float(font_floor), ceiling))


def _text_sizes(alive: list[dict], scale: float, font_floor: float = 0.0) -> dict[int, float]:
    """一次算好所有文本的绘制字号（同一份结果供画布估高与真正绘制共用）。"""
    by_id = {str(el["id"]): el for el in alive if el.get("id")}
    return {id(el): _text_px(el, scale, font_floor, by_id) for el in alive}


def _text_overshoot(alive: list[dict], scale: float, max_y: float,
                    sizes: dict[int, float] | None = None) -> float:
    """文字折行后可能比元素本身更高：算出画布需要额外补多少底边（**源坐标单位**）。

    任务 110 修：``size`` 是绘制像素字号，要 ÷ ``scale`` 才是源坐标单位（与
    :func:`_text_ceiling` 同一口径）；不除会把 scale > 1 的图（窄于 ``MIN_WIDTH``）底边
    撑大好几倍 —— 画布越高、上页后显示宽度越窄、等效磅数越小，正好把重排的收益吃掉。
    """
    overshoot = 0.0
    sizes = sizes or {}
    for el in alive:
        if str(el.get("type") or "") != "text" or not str(el.get("text") or "").strip():
            continue
        size = sizes.get(id(el)) or _text_px(el, scale)
        try:
            font = load_font(size)
        except FigureError:
            continue
        lines = _wrap_lines(str(el.get("text")), font, max(20.0, float(el.get("width") or 0) * scale))
        bottom = (float(el.get("y") or 0)
                  + len(lines) * size * LINE_HEIGHT / max(scale, 1e-6))
        overshoot = max(overshoot, bottom - max_y)
    return max(0.0, overshoot)


def _box4(el: dict) -> tuple[float, float, float, float]:
    """元素外框 ``(x0, y0, x1, y1)``（源单位）。"""
    x, y = float(el.get("x") or 0), float(el.get("y") or 0)
    return x, y, x + float(el.get("width") or 0), y + float(el.get("height") or 0)


def _container_of(el: dict, shapes: list[dict]) -> dict | None:
    """文本所属卡片：先认 ``containerId``，再退到「最小的、包住文字中心的外框」。"""
    cid = str(el.get("containerId") or "")
    if cid:
        for shape in shapes:
            if str(shape.get("id") or "") == cid:
                return shape
    x0, y0, x1, y1 = _box4(el)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    best, area = None, None
    for shape in shapes:
        sx0, sy0, sx1, sy1 = _box4(shape)
        if sx0 - 1.0 <= cx <= sx1 + 1.0 and sy0 - 1.0 <= cy <= sy1 + 1.0:
            size = (sx1 - sx0) * (sy1 - sy0)
            if area is None or size < area:
                best, area = shape, size
    return best


def _text_metrics(el: dict, scale: float, wrap_w: float) -> tuple[float, float, float, float]:
    """``(盒高, 墨高, 墨顶, 最宽行宽)``（源单位）—— 与绘制同一锚点 ``"ma"``。

    盒高 = 折行数 × 字号 × 行距（``_draw_text`` 的行距口径）；墨高 = 真正落笔的上下界
    （``font.getbbox(..., anchor="ma")``）。排版**按墨高**算才不浪费行间空白：单行文本
    的墨高约等于 1.0 倍字号，而盒高是 1.35 倍 ⇒ 直接按盒高算会把卡片高度白白吃掉 35%。
    """
    size = float(el.get("fontSize") or 20) * scale
    font = load_font(size)
    lines = _wrap_lines(str(el.get("text")), font, max(20.0, wrap_w)) or [""]
    widest = 0.0
    top, bottom, seen = 0.0, size, False
    for idx, line in enumerate(lines):
        widest = max(widest, font.getlength(line))
        if not line.strip():
            continue
        bbox = font.getbbox(line, anchor="ma")
        offset = idx * size * LINE_HEIGHT
        if not seen:
            top, bottom, seen = offset + bbox[1], offset + bbox[3], True
        else:
            top = min(top, offset + bbox[1])
            bottom = max(bottom, offset + bbox[3])
    return len(lines) * size * LINE_HEIGHT, max(1.0, bottom - top), top, widest


def _block_span(members: list[dict], scale: float, wrap_w: float
                ) -> tuple[float, float, list[tuple[float, float]]]:
    """整块文字的 ``(盒跨度, 最宽行宽, 逐元素 (墨高, 墨顶))``：按墨高排、按盒跨度校核。

    排法：把「墨」首尾相接（相邻两块之间留 ``TEXT_GAP_UNITS``），再取整块盒子的上下界
    —— 这样既不会浪费行距空白，又能保证每个元素的**盒子**也在卡片里（不越界、不撑大画布）。
    """
    if not members:
        return 0.0, 0.0, []
    items = [_text_metrics(el, scale, wrap_w) for el in members]
    cursor = 0.0
    box_top, box_bottom = 0.0, 0.0
    info: list[tuple[float, float]] = []
    for idx, (box_h, ink_h, ink_top, _w) in enumerate(items):
        y = cursor - ink_top
        if idx == 0:
            box_top, box_bottom = y, y + box_h
        else:
            box_top, box_bottom = min(box_top, y), max(box_bottom, y + box_h)
        info.append((ink_h, ink_top))
        cursor += ink_h + TEXT_GAP_UNITS
    widest = max(item[3] for item in items)
    return box_bottom - box_top, widest, info

def _card_scale(members: list[dict], card: dict) -> float:
    """卡片里这组文字能放大到的倍数（二分；只由几何决定，与显示档位无关）。"""
    factor = CONTAINER_FIT.get(str(card.get("type") or ""), 1.0)
    card_w = float(card.get("width") or 0) * factor
    card_h = float(card.get("height") or 0)
    # 小卡片（编号徽章之类）按比例留边：34×34 的徽章里再扣 8 单位内边距就没地方放字了
    pad = max(2.0, min(CARD_PAD_UNITS, card_w * 0.12, card_h * 0.12))
    wrap_w = card_w - 2 * pad
    avail_h = card_h - 2 * pad - REFLOW_SAFETY_UNITS
    if not members or wrap_w <= 20.0 or avail_h <= 1.0:
        return 1.0
    try:
        span, widest, _info = _block_span(members, 1.0, wrap_w)
    except FigureError:
        return 1.0
    if span > avail_h or widest > card_w - CARD_PAD_UNITS:
        return 1.0
    lo, hi = 1.0, MAX_FONT_BOOST
    for _ in range(16):
        mid = (lo + hi) / 2
        try:
            span, widest, _info = _block_span(members, mid, wrap_w)
        except FigureError:
            break
        if span <= avail_h and widest <= card_w - CARD_PAD_UNITS:
            lo = mid
        else:
            hi = mid
    return lo


def _apply_card_scale(card: dict, members: list[dict], scale: float) -> None:
    """把卡片里的文字放大 ``scale`` 倍并重排：占满卡片内宽，整块在卡片里居中。"""
    if scale <= 1.0:
        return
    factor = CONTAINER_FIT.get(str(card.get("type") or ""), 1.0)
    card_x, card_y, card_x1, card_y1 = _box4(card)
    card_w = (card_x1 - card_x) * factor
    card_h = card_y1 - card_y
    pad = max(2.0, min(CARD_PAD_UNITS, card_w * 0.12, card_h * 0.12))
    wrap_w = card_w - 2 * pad
    cx = (card_x + card_x1) / 2
    ordered = sorted(members, key=lambda el: _box4(el)[1])
    try:
        span, _widest, info = _block_span(ordered, scale, wrap_w)
        boxes = [_text_metrics(el, scale, wrap_w)[0] for el in ordered]
    except FigureError:
        return
    # 墨与盒子一起居中：先按墨首尾相接算 y，再把整块的盒子上下界挪到卡片正中
    tops: list[float] = []
    cursor_ink = 0.0
    for (ink_h, ink_top), box_h in zip(info, boxes):
        y = cursor_ink - ink_top
        tops.append(y)
        cursor_ink += ink_h + TEXT_GAP_UNITS
    offset = card_y + (card_h - span) / 2 - min(tops)
    for el, box_h, y in zip(ordered, boxes, tops):
        el["fontSize"] = round(float(el.get("fontSize") or 20) * scale, 2)
        el["width"] = math.ceil(wrap_w * 10) / 10.0
        el["height"] = round(box_h + 0.5, 1)
        el["x"] = round(cx - el["width"] / 2, 1)
        el["y"] = round(offset + y, 1)


def _free_scale(el: dict, alive: list[dict], bounds: tuple[float, float, float, float]) -> float:
    """不在卡片里的文字（页头标题、箭头标签）能放大到的倍数：不压别的元素、不越整图外框。"""
    x0, y0, x1, y1 = _box4(el)
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return 1.0
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    others = [_box4(other) for other in alive if other is not el]
    spans = MAX_FONT_BOOST - 1.0
    for step in range(REFLOW_STEPS):
        scale = MAX_FONT_BOOST - step * spans / (REFLOW_STEPS - 1)
        bw, bh = w * scale, h * scale
        bx0, by0 = cx - bw / 2 - 2.0, cy - bh / 2 - 2.0
        bx1, by1 = cx + bw / 2 + 2.0, cy + bh / 2 + 2.0
        if (bx0 < bounds[0] - 0.5 or by0 < bounds[1] - 0.5
                or bx1 > bounds[2] + 0.5 or by1 > bounds[3] + 0.5):
            continue
        if any(ox0 < bx1 and ox1 > bx0 and oy0 < by1 and oy1 > by0
               for ox0, oy0, ox1, oy1 in others):
            continue
        return scale
    return 1.0


def _apply_free_scale(el: dict, scale: float) -> None:
    """自由文本整体放大（保持中心与相对留白）：字号、外框一起放大。"""
    if scale <= 1.0:
        return
    x0, y0, x1, y1 = _box4(el)
    w, h = (x1 - x0) * scale, (y1 - y0) * scale
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    el["fontSize"] = round(float(el.get("fontSize") or 20) * scale, 2)
    el["width"] = round(w + 0.5, 1)
    el["height"] = round(h + 0.5, 1)
    el["x"] = round(cx - el["width"] / 2, 1)
    el["y"] = round(cy - el["height"] / 2, 1)


def reflow_text_elements(elements: list[dict]) -> list[dict]:
    """按卡片几何重排图内文字（字号抬到几何边界）；卡片与画布尺寸一个都不动。

    只动文本元素自己的 ``x/y/width/height/fontSize``（不碰卡片、连线、画布），所以
    「字号 ÷ 画布宽」只升不降 ⇒ 上页后的等效磅数只会变大。返回值是新列表（不改入参），
    渲染与「图内字号」测量共用同一份结果，绝不各算各的。
    """
    out = [dict(el) for el in elements if isinstance(el, dict)]
    texts = [el for el in out
             if str(el.get("type") or "") == "text" and str(el.get("text") or "").strip()]
    if not texts:
        return out
    shapes = [el for el in out if str(el.get("type") or "") in CONTAINER_TYPES]
    bounds = _bounds(out)
    groups: dict[str, tuple[dict, list[dict]]] = {}
    free: list[dict] = []
    for el in texts:
        card = _container_of(el, shapes)
        if card is None:
            free.append(el)
            continue
        groups.setdefault(str(card.get("id") or id(card)), (card, []))[1].append(el)
    for card, members in groups.values():
        _apply_card_scale(card, members, _card_scale(members, card))
    for el in free:
        _apply_free_scale(el, _free_scale(el, out, bounds))
    return out


def plan_render(elements: list[dict], *, min_width: float = MIN_WIDTH,
                display_box: tuple[float, float] | None = None,
                min_text_pt: float = MIN_TEXT_PT) -> dict:
    """渲染前的几何与可读性规划（:func:`render_png` 画图、测试与验收出数字共用）。

    返回 ``alive`` / 包围盒 / ``scale`` / ``canvas_w`` / ``canvas_h`` / ``font_floor``
    （字号下限，px）/ ``text_px``（正文文本绘制字号，升序）以及按 ``display_box``
    等比缩放后的 ``display_width_in`` / ``min_pt`` / ``median_pt`` / ``max_pt``。
    """
    alive = [el for el in elements if isinstance(el, dict) and not el.get("isDeleted")]
    if not alive:
        raise FigureError("图形元素为空，无法重绘")
    alive = reflow_text_elements(alive)   # 任务 110：先重排（字号抬到几何边界），再算画布
    min_x, min_y, max_x, max_y = _bounds(alive)
    span_x = max(1.0, max_x - min_x)
    span_y = max(1.0, max_y - min_y)
    scale = min(MAX_SCALE, max(1.0, float(min_width) / span_x))
    canvas_w = int(math.ceil((span_x + PAD * 2) * scale))
    canvas_h = int(math.ceil((span_y + PAD * 2) * scale))
    font_floor = 0.0
    sizes_map: dict[int, float] = {}
    for _ in range(3):   # 字号下限 ⇄ 画布高度互相影响，固定 3 次迭代收敛
        font_floor = min_text_px(canvas_w, canvas_h, display_box=display_box,
                                 min_text_pt=min_text_pt)
        sizes_map = _text_sizes(alive, scale, font_floor)
        grown = int(math.ceil((span_y + _text_overshoot(alive, scale, max_y, sizes_map)
                               + PAD * 2) * scale))
        if grown <= canvas_h:
            break
        canvas_h = grown
    box = display_box or (REFERENCE_MEDIA_WIDTH_IN, REFERENCE_MEDIA_HEIGHT_IN)
    display_width = fit_display_width_in(canvas_w, canvas_h, box[0], box[1])
    sizes = sorted(sizes_map[id(el)] for el in alive
                   if str(el.get("type") or "") == "text" and str(el.get("text") or "").strip())

    def _pt(px: float | None) -> float | None:
        return None if px is None else pt_equivalent(px, canvas_w, display_width)

    return {
        "alive": alive, "min_x": min_x, "min_y": min_y, "span_x": span_x, "span_y": span_y,
        "scale": scale, "canvas_w": canvas_w, "canvas_h": canvas_h, "font_floor": font_floor,
        "display_width_in": display_width, "text_px": sizes, "sizes_map": sizes_map,
        "min_pt": _pt(sizes[0] if sizes else None),
        "median_pt": _pt(sizes[len(sizes) // 2] if sizes else None),
        "max_pt": _pt(sizes[-1] if sizes else None),
        "text_elements": len(sizes),
    }


def render_png(elements: list[dict], out_path: str | Path, *,
               min_width: float = MIN_WIDTH, background: str = DEFAULT_BG,
               style: dict | None = None,
               display_box: tuple[float, float] | None = None,
               min_text_pt: float = MIN_TEXT_PT) -> Path:
    """把 Excalidraw 元素重绘成 PNG（可选主题色板：bg / ink / edge）。

    ``display_box`` = 这张图在页上可用的 (宽, 高) 英寸：给了它就按 :func:`min_text_px`
    把正文字号抬到「等效磅数 ≥ ``min_text_pt``」（``min_text_pt=0`` 关闭护栏，只用于对照）。
    字号下限与画布高度互相影响（字大了折行更多 ⇒ 画布更高 ⇒ 参考显示宽度更小），
    用固定 3 次迭代收敛，结果确定。
    """
    style = dict(style or {})
    plan = plan_render(elements, min_width=min_width, display_box=display_box,
                       min_text_pt=min_text_pt)
    alive = plan["alive"]
    min_x, min_y, scale = plan["min_x"], plan["min_y"], plan["scale"]
    canvas_w, canvas_h = plan["canvas_w"], plan["canvas_h"]
    font_floor = plan["font_floor"]
    background = str(style.get("bg") or background or DEFAULT_BG)
    image = Image.new("RGB", (canvas_w, canvas_h), _rgb(background, (255, 251, 242)))
    draw = ImageDraw.Draw(image)
    origin = (min_x - PAD, min_y - PAD)
    for el in alive:
        kind = str(el.get("type") or "")
        if kind not in ("rectangle", "ellipse", "diamond", "line", "arrow", "text"):
            continue
        box = {
            "scale": scale,
            "x": (float(el.get("x") or 0) - origin[0]) * scale,
            "y": (float(el.get("y") or 0) - origin[1]) * scale,
            "w": float(el.get("width") or 0) * scale,
            "h": float(el.get("height") or 0) * scale,
            "width": max(1, int(round(float(el.get("strokeWidth") or 1) * scale))),
            "ink": style.get("ink"),
            "edge": style.get("edge"),
        }
        if kind == "text":
            box["size"] = max(float(MIN_PIL_FONT_PX),
                              plan["sizes_map"].get(id(el)) or _text_px(el, scale, font_floor))
            box["font"] = load_font(box["size"])
            box["emoji_font"] = load_emoji_font(box["size"])
            box["color"] = _rgb(el.get("strokeColor"), _rgb(box.get("ink"), _rgb(DEFAULT_INK)))
            _draw_text(draw, el, box)
        elif kind in ("line", "arrow"):
            _draw_polyline(draw, el, box)
        else:
            _draw_shape(draw, el, box)
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, format="PNG")
    return target


def render_figure(item, out_path: str | Path, *,
                  style: dict | None = None,
                  display_box: tuple[float, float] | None = None,
                  min_text_pt: float = MIN_TEXT_PT,
                  elements: list[dict] | None = None) -> tuple[Path | None, str | None]:
    """把内容项的概念图重绘到 out_path。

    返回 ``(产物路径, 失败原因)``：成功时原因为 None；失败时路径为 None 且原因非空
    （调用方把它记进 meta.figure_failures，退回文字版，绝不静默）。
    ``display_box`` / ``min_text_pt`` 见 :func:`render_png`（可读性下限的口径）。
    ``elements`` 给了就用它（而不是条目里存的那份）渲染 —— 课件侧按**本次课件主题**重映射
    取色就走这条（任务 127，见 :func:`recolor_elements`）；条目本身一个字节不改。
    同内容（指纹一致）且产物已存在时不重复渲染；指纹含可读性参数，口径变了会重画。
    """
    target = Path(out_path)
    stamp = target.with_name(target.name + SIGNATURE_SUFFIX)
    if elements is None:
        try:
            elements = elements_of(item)
        except FigureError as exc:
            return None, str(exc)
    # 指纹按「重排后」的元素算：重排口径一变，指纹就变 ⇒ 不会拿旧 PNG 当新产物。
    signature = element_signature(reflow_text_elements(elements), style,
                                  display_box=display_box, min_text_pt=min_text_pt)
    if target.exists() and stamp.exists():
        try:
            if stamp.read_text(encoding="utf-8").strip() == signature:
                return target, None
        except OSError:
            pass
    try:
        render_png(elements, target, style=style, display_box=display_box,
                   min_text_pt=min_text_pt)
    except FigureError as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 —— 单图失败不许拖垮整份课件
        return None, f"重绘概念图失败：{exc}"
    try:
        stamp.write_text(signature, encoding="utf-8")
    except OSError:
        pass
    return target, None
