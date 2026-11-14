"""PPT 课件生成：整课 1 个 + 每章 1 个（v1 确定性生成，不调 LLM）。

数据模型（规划文档 §3.1）：
- 复用 ``ContentItem(kind="ppt")``；``meta.scope`` = ``course`` | ``chapter``，
  每章课件另记 ``chapter_node_id`` / ``chapter_key``；同一 scope 只保留 1 个
  （重做 = 原地替换，旧版进 ``meta.history``，≤5）。
- ``meta.source`` 记录 ``outline_rev`` / ``lecture_ids`` / ``chart_ids`` /
  ``style_anchor_ref`` / ``concept_ids``，为 v2「内容变了要重做」的判断留好依据。

引擎：默认 python-pptx（纯 Python、离线、可断言页数与备注）；``dashi-ppt`` 是可选引擎
（配置探测到且显式选择时才走），失败**必须显式回退并记录原因**（``meta.engine_error``）。

v4（2026-09-11）：三套内置主题驱动整套配色（同时驱动概念图 PNG 与原生图表）、母版级版式族（封面/目录/章节/知识点/纯图/回顾/练习/提问/结束）、生成前按
每页配额精简（标题 ≤27 字、要点 ≤4 条且每条 ≤22 字）、生成后逐页几何自检并自动降级
（原因写进 ``meta.layout_notes``）、备注改成 150~300 字的可照念口播稿。

v5（2026-09-15）：讲义里的图引用（``diagram://`` / ``chart://`` / ``gen://``）在取要点行时
统一解析 / 过滤 —— 引用到的图优先真正嵌进页面，确实超出每页上限的从文字里删掉，并把
「原计划配图 …」写进演讲备注与 ``meta.layout_notes``（``kind="figure-dropped"``）；
任何页型的文字里都不再出现协议引用原文（任务 35）。

v6（2026-09-15）：整课课件去掉「内部话术」—— 末页换成给学员的收尾（学完能做什么 + 3 条可
立即落地的行动 / 自检，产品操作提示只进演讲备注）、章节概览页真嵌 1 张代表图（概念图 >
数据图 > 概念卡）并删掉「本章图 N 张」内部清单、目录页去掉「图 N」计数；另加词表自检
:func:`_purge_internal_words`（正文命中即移进备注 + 记 ``layout_notes``）。
"""
from __future__ import annotations

import copy
import json
import logging
import math
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.course import ContentItem, Course, OutlineNode, QuizItem
from app.services.deck_label import chapter_label as node_chapter_label
from app.services.deck_label import deck_title
from app.services.figure_png import (MIN_TEXT_PT, fit_display_width_in, pt_equivalent,
                                     render_figure)
from app.services import figure_png
from app.services.lecture import PROMPT_TEMPLATE_VERSION
from app.services.planner import list_nodes
from app.services.ppt_layout import (
    NOTES_LIMIT, SPEAK_MIN, SPACE_AFTER_PT, audit_pptx, audit_presentation,
    char_units, clip_chars, compose_notes,
    degrade_spec, est_height_in, fit_spec, point_chars,
)
from app.services.ppt_theme import resolve_theme, theme_names, theme_options
from app.services import ppt_theme_setting, scenario
from app.services.style_anchor import anchor_summary, get_style_anchor

logger = logging.getLogger(__name__)

NPM = "npm.cmd" if os.name == "nt" else "npm"

ENGINE_PPTX = "python-pptx"
ENGINE_DASHI = "dashi"
ENGINE_UPLOAD = "upload"

SCOPE_COURSE = "course"
SCOPE_CHAPTER = "chapter"

# 会被记进「该章图数」并在幻灯片上留占位的内容类型
FIGURE_KINDS = ("chart_bar", "chart_line", "chart_pie", "chart_table",
                "diagram", "image", "flowchart")

MAX_BULLETS = 6          # 单页正文要点上限
MAX_NOTE_CHARS = 600     # 演讲备注上限（讲义原文会截断）
LONG_PARAGRAPH = 40      # 超过该长度的段落不进幻灯片正文，只进备注
MAX_FIGURES_PER_SLIDE = 2  # 每页最多嵌入的图（概念图 / 原生图表 / 表格）数量
# 任务 146 A2：一个知识点默认 1 页，要点 / 图确实放不下才 2 页；**加出来的页必须配得上内容**。
# 「一页只有一张图 + 一行图注」算缺陷：有图的页可见文字 < PAGE_MIN_TEXT_CHARS 就不许单独成页。
PAGE_MIN_TEXT_CHARS = 30
# 任务 146b A3：图页的**正文**底线（至少 1 条要点 + >= 12 字）。验收表还要求带图的页
# 整页可见文字 >= PAGE_MIN_TEXT_CHARS（30 字，含标题 / 图注），实现取两者更严者：
# 图页必须至少 1 条要点、正文 >= 12 字，且标题 + 正文 >= 30 字。
MEDIA_PAGE_MIN_TEXT_CHARS = 12
# 任务 146 A3：练习页可见文字下限（题干 + 作答提示；答案与解析仍只进备注）。
QUIZ_MIN_TEXT_CHARS = 40
# 无选项简答题的排面：题干卡加高、题干放大，下方补「作答提示」面板（0 LLM，固定引导语）
QUIZ_OPEN_STEM_H_IN = 2.30
QUIZ_OPEN_STEM_SIZE_PT = 28.0
QUIZ_OPEN_HINTS = (
    "作答提示：先独立写出答案，再对照讲师备注里的参考答案与解析。",
    "作答要点：先给结论，再补 1–2 条依据或例子。",
    "想不出来？回到本节要点，逐条对照后再补充。",
)
QUIZ_OPEN_HINTS_MIN_IN = 0.90   # 打包页给「作答提示」面板预留的高度（判据与渲染共用）
QUIZ_OPEN_HINT_SIZE_PT = 16.0

# 统一版式（16:9，单位英寸）；字号层级由主题提供（见 ppt_theme）
SLIDE_WIDTH_IN = 13.333
SLIDE_HEIGHT_IN = 7.5
MARGIN_IN = 0.55
FIGURES_SUBDIR = "figures"
# 任务 107：图在页上分两档 —— 「分栏档」（左栏要点 + 右栏图）与「满幅档」（图占满版心宽，
# 要点并成图上方一条）。右栏从 5.60 放宽到 6.90 英寸、左栏相应收窄；满幅档宽度 = 页宽 −
# 左右边距。选档判据见 media_tier_of()，每页用了哪档、为什么落 meta.layout_notes
# （kind="media-tier"），图内最小字号的等效磅数不足 9 磅另记 kind="legibility"。
BODY_COLUMN_IN = 5.03    # 有图时左栏（要点）宽度
MEDIA_COLUMN_IN = 6.90   # 分栏档：右栏宽度
MEDIA_LEFT_IN = SLIDE_WIDTH_IN - MARGIN_IN - MEDIA_COLUMN_IN   # 分栏档：右栏起点（≈5.88）
MEDIA_WIDTH_IN = MEDIA_COLUMN_IN   # 旧名保留：分栏档宽度
MEDIA_FULL_WIDTH_IN = SLIDE_WIDTH_IN - MARGIN_IN * 2           # 满幅档宽度（≈12.23）
MEDIA_STRIP_MAX_IN = 1.90   # 满幅档顶部「要点条」高度上限（放不下就保持分栏，不挪要点）
MEDIA_STRIP_GAP_IN = 0.08   # 要点条与图之间的间距
MEDIA_STACK_GAP_IN = 0.18   # 一页多张图时每张之间的间距（_place_media 与「拆不拆」共用）
# 任务 110：图在页上的显示宽度下限（禁止窄放）。窄于它图就只剩「糊」一条路 ——
# 与其硬塞，不如换满幅档 / 换版式（纯图页），选择理由照样写进 meta.layout_notes。
MIN_MEDIA_WIDTH_IN = 6.00
CONTENT_TOP_IN = 1.50
CONTENT_HEIGHT_IN = 4.95
# 图内字号下限的参考框（任务 107）：分栏档是两档里最窄的图档，按它算下限 ⇒ 任一档
# （含满幅）上页后的等效磅数都不低于 figure_png.MIN_TEXT_PT —— 选档只会把图放得更大。
MEDIA_FLOOR_BOX = (MEDIA_COLUMN_IN, CONTENT_HEIGHT_IN)
# 反算「图要多少高度」时留的余量（任务 110）：落地的显示宽度按 EMU / 英寸取整，
# 卡在「正好 9.00 磅」的图上页后实测会被取整吃到 8.99（实测 1990×869 那张：算 9.0000、
# 量 8.9976）⇒ 反算时按 9 磅 × 1.02 要高度，落地后仍 > 9 磅。
MEDIA_PT_MARGIN = 1.02
# 纯图页（LAYOUT_FIGURE）的页面几何（任务 110）：本页不画标题条，只在最上方留一行
# 「本页标题 + 图注」，图占整页。近方形的概念图在 16:9 页面上只能靠**页高**放大，而标题
# 条要吃掉 1.16 英寸 ⇒ 图被压到 6 英寸宽以下（实测 4.26 英寸 / 6.67 磅）。本页的图
# 不吃图注条（图注与页标题同占页首这一行）⇒ 图框就是整块可用高度，且图注仍在页上。
FIGURE_RULE_IN = 0.62        # 页脚分隔线距页底（与 _add_footer 同一把尺子）
FIGURE_HEAD_TOP_IN = 0.14    # 纯图页：页首一行（标题 + 图注）的顶
FIGURE_HEAD_H_IN = 0.28      # 纯图页：页首这一行的高（一行 caption 字号）
FIGURE_HEAD_TITLE_IN = 7.60  # 纯图页：页首一行里「标题」占的宽（其余给图注）
FIGURE_HEAD_GAP_IN = 0.23    # 纯图页：页首一行里标题与图注之间的空隙
FIGURE_TOP_IN = 0.46         # 纯图页：图框顶（页首一行之下）
FIGURE_CAPTION_IN = 0.0      # 纯图页：图注与页标题同占页首一行 ⇒ 图框里不再留图注条
MEDIA_FIGURE_BOX = (MARGIN_IN, FIGURE_TOP_IN, MEDIA_FULL_WIDTH_IN,
                    SLIDE_HEIGHT_IN - FIGURE_RULE_IN - FIGURE_TOP_IN)
# 任务 146b A3：图页必须至少 1 条要点。整页图最下方每条要点留 0.58 英寸（1~3 条），
# 图框相应让出这点高度 —— 仍远大于分栏 / 满幅档，图能保住 6 英寸宽 / 9 磅下限。
FIGURE_BULLET_STRIP_IN = 0.58

# 版式族（母版级）：封面 / 目标 / 目录 / 章节页 / 知识点页 / 纯图页 /
# 章节回顾页 / 练习页 / 课堂提问页 / 结束页
LAYOUT_COVER = "cover"
LAYOUT_GOALS = "goals"
LAYOUT_TOC = "toc"
LAYOUT_CHAPTER = "chapter"
LAYOUT_POINT = "point"
LAYOUT_FIGURE = "figure_only"
LAYOUT_REVIEW = "review"
LAYOUT_QUIZ = "quiz"
LAYOUT_QUESTIONS = "questions"
LAYOUT_END = "end"

# 任务 108：知识点页（LAYOUT_POINT）的三种变体版式 —— 按内容判据自动选（见 point_variant_of），
# 都不命中就保持 LAYOUT_POINT。选哪种、为什么，逐页落 meta.layout_notes（kind="point-variant"）。
LAYOUT_POINT_COMPARE = "point_compare"   # 对比页：两栏对照
LAYOUT_POINT_STEPS = "point_steps"       # 时间线 / 步骤页：横向（窄栏竖排）带编号
LAYOUT_POINT_DATA = "point_data"         # 数据页：大数字 + 一行注解
POINT_VARIANTS = (LAYOUT_POINT_COMPARE, LAYOUT_POINT_STEPS, LAYOUT_POINT_DATA)

# 任务 128：课件**逐页**记「为什么选了这个版式」（kind="layout-choice"）。
# 起因（``验收-127-独立复测``）：任务 127 的 ③ 把象限图判据只写在**图条目**的 layout_notes 上，
# 课件页这一层「象限」出现 0 次 ⇒ 读者打开课件看不到「这页为什么是象限图 / 为什么是这两栏对照」。
# 这里只**记录已经做出的选择**（不选版式、不改判据、不动几何），理由复用既有判据文案。
LAYOUT_CHOICE_KIND = "layout-choice"
#: 规则名 → 人话标签（写在理由开头，便于按页检索「规则名 + 命中词 + 为什么」）。
LAYOUT_CHOICE_RULES = {
    "quadrant": "数据图选型：象限图（两轴四象限）",
    "point-variant": "知识点页版式判据（对比 > 步骤 > 数据）",
    "point-default": "知识点页默认版式（内容判据都没命中）",
    "figure-only": "纯图页（图的档位放不下要点，整页给图）",
    "structural": "结构页固定版式（按页在课件里的位置定，不参与内容判据）",
}
#: 不参与内容判据的结构页版式 → 页名（写进理由，读者一眼知道这是哪一页）。
STRUCTURAL_LAYOUT_NAMES = {
    LAYOUT_COVER: "封面", LAYOUT_GOALS: "课程目标", LAYOUT_TOC: "课程目录",
    LAYOUT_CHAPTER: "章节页", LAYOUT_REVIEW: "章节回顾", LAYOUT_QUIZ: "随堂练习",
    LAYOUT_QUESTIONS: "课堂提问", LAYOUT_END: "结束页",
}

# 任务 108：版面视觉元素（卡片 / 图标 / 色块）。命名前缀让「每页装饰元素数」可逐页测量，
# 上限防「为了好看堆砌」；超上限的装饰不再画，并记一条 layout_notes（kind="decor-cap"）。
DECOR_PREFIX = "cg-decor"      # 纯装饰：无文字，几何自检自动忽略
CARD_PREFIX = "cg-card"        # 卡片正文：页级大纲（meta.slides）照样读得到
ICON_PREFIX = "cg-icon"        # 图标 / 编号徽章里的字符：不算正文
META_PREFIX = "cg-meta"        # 署名 / 日期这类页脚文字：不算正文也不算装饰
MAX_DECOR_PER_SLIDE = 14       # 单页装饰元素上限（实测最大 12：目录 / 目标各 6 张卡 × 2）

# 后段 4 页与卡片网格的几何（版心 = 左右边距内、标题条之下、页脚之上）
CARD_LEFT_IN = MARGIN_IN
CARD_TOP_IN = CONTENT_TOP_IN
CARD_WIDTH_IN = SLIDE_WIDTH_IN - MARGIN_IN * 2
CARD_HEIGHT_IN = CONTENT_HEIGHT_IN
CARD_GAP_IN = 0.28            # 卡片之间的横 / 纵间距
CARD_PAD_IN = 0.18            # 卡片内边距（文字离卡片边）
BADGE_IN = 0.40               # 图标 / 编号徽章边长
END_META_TOP_IN = 5.82        # 结束页「署名 / 日期」一行的顶
QUIZ_STEM_H_IN = 1.75         # 练习页题干卡的高（题干 ≤80 字 ⇒ 24 磅最多 3 行）

# 与 dashi-ppt DEFAULT_BODY_ROLES 保持一致，轮换使用以保证版式充足
_BODY_ROLES = [
    "statement", "breakdown", "context", "metrics", "comparison",
    "distribution", "relationship", "case", "trend", "process",
    "risks", "actions", "result",
]

COURSE_GOAL_TITLE = "课程目标"
COURSE_TOC_TITLE = "课程目录"
COURSE_END_TITLE = "总结与下一步"
QUIZ_TITLE = "随堂练习"
END_TITLE = "谢谢观看"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 素材收集（大纲 / 讲义 / 习题 / 图表 / 概念库）
# ---------------------------------------------------------------------------

def _items_for(db: Session, course_id: str, node_id: str) -> list[ContentItem]:
    return list(db.query(ContentItem).filter(
        ContentItem.course_id == course_id, ContentItem.node_id == node_id,
    ).order_by(ContentItem.created_at).all())


def _lecture_of(items: list[ContentItem]) -> ContentItem | None:
    lectures = [i for i in items if i.kind == "lecture"]
    return lectures[-1] if lectures else None


def _is_figure(item: ContentItem) -> bool:
    """是不是「要嵌进课件」的图（概念图 / 数据图 / 表格 / 流程图 / 位图）。"""
    kind = str(item.kind or "")
    return kind in FIGURE_KINDS or kind.startswith("chart_")


def _figures_of(items: list[ContentItem]) -> list[ContentItem]:
    return [i for i in items if _is_figure(i)]


def _members_of(chapter: OutlineNode, nodes: list[OutlineNode]) -> list[OutlineNode]:
    """章 + 其下所有知识点（保持大纲顺序）。"""
    out = [n for n in nodes
           if n.node_key == chapter.node_key or n.node_key.startswith(f"{chapter.node_key}.")]
    return out or [chapter]


def _chapters_of(nodes: list[OutlineNode]) -> list[OutlineNode]:
    chapters = [n for n in nodes if n.node_type in ("chapter", "section")]
    return chapters or nodes


def _figure_label(item: ContentItem) -> str:
    return str(item.title or item.id).strip()


def _figure_placeholders(figures: list[ContentItem], limit: int = 2) -> list[str]:
    return [f"[图:{_figure_label(f)}]" for f in figures[:limit]]


def _point_label(node) -> str:
    """知识点的人话标识：有 node_key 用「第 X 个知识点『标题』」，否则用标题。"""
    if isinstance(node, str):
        return node
    key = str(getattr(node, "node_key", "") or "").strip()
    title = str(getattr(node, "title", "") or "").strip()
    if key and title:
        return f"第 {key} 个知识点「{title}」"
    return f"知识点「{title}」" if title else "本知识点"


def _page_limit_drop_reason(node, total: int, fig_no: int, label: str,
                            limit: int) -> str:
    """A4 允许的三类真实原因之一：页数上限（带 limit / 图数 / 第几张）。"""
    return (f"页数上限：limit={limit} 已排满（{_point_label(node)}共 {total} 张图，"
            f"第 {fig_no} 张 [图:{label}] 未上页），已挪到备注")


def _bullet_shortage_drop_reason(node, total: int, fig_no: int, label: str,
                                 bullets: int, pages: int, chars: int) -> str:
    """A4 允许的三类真实原因之一：要点不足（带条数 / 页数 / 字数）。"""
    if bullets < total:
        detail = f"要点 {bullets} 条 < 图 {total} 张，均分后仍有一页没有要点"
    else:
        detail = (f"要点 {bullets} 条均分到 {pages} 页后，最少一页可见正文 {chars} 字"
                  f" < {PAGE_MIN_TEXT_CHARS} 字")
    return (f"要点不足：{_point_label(node)}{detail}；"
            f"第 {fig_no} 张 [图:{label}] 无页可上，已挪到备注")


def _figure_drop_reasons(leftover: list[str], missing: list[str],
                         reasons: list[str] | None = None) -> list[str]:
    """图没能进页面时的统一说法（同时进演讲备注与 meta.layout_notes，任务 35）。

    任务 146b A4：``reasons`` 必须由丢图现场逐张给出（页数上限 / 要点不足 / 几何门槛，
    每条都带具体数值）。旧文案「因每页图上限 2 张」与事实不符，已删除；没有逐图原因时
    只如实说「未上页、原因见 layout_notes」，绝不编一个原因。
    """
    out: list[str] = []
    if leftover:
        if reasons:
            out.extend(reasons)
        else:
            listed = "、".join(f"[图:{label}]" for label in leftover[:6])
            more = f" 等 {len(leftover)} 张" if len(leftover) > 6 else ""
            out.append(f"原计划配图：{listed}{more}（未上页；逐图原因见 layout_notes）")
    if missing:
        listed = "、".join(f"[图:{label}]" for label in missing[:3])
        more = f" 等 {len(missing)} 张" if len(missing) > 3 else ""
        out.append(f"讲义引用的图没有对应条目：{listed}{more}（引用原文已从页面文字里移除）")
    return out


def _clip(text: str, limit: int = MAX_NOTE_CHARS) -> str:
    text = re.sub(r"[ \t]+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _chunk(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


# 任务 146 A1：页标题续后缀的**唯一出处** —— 一页只许一个后缀，任何页都不许叠两个。
CONT_SUFFIX = "（接上页）"
_TRAILING_CONT_SUFFIX = re.compile(r"(（续）|（续图）|（接上页）)\s*$")
_TRAILING_FIG_NO = re.compile(r"\s*·\s*图\s*\d+\s*$")


def strip_page_suffix(title: str) -> str:
    """去掉标题尾部**所有**续后缀（含历史遗留的「（续）」「（续图）」与「· 图 N」）。"""
    text = str(title or "").strip()
    while True:
        new = _TRAILING_FIG_NO.sub("", _TRAILING_CONT_SUFFIX.sub("", text)).strip()
        if new == text or not new:
            return text
        text = new


def continue_title(title: str) -> str:
    """续页标题：<原名>（接上页）—— 只加一个后缀，且先洗掉旧后缀。"""
    return f"{strip_page_suffix(title)}{CONT_SUFFIX}"


def figure_page_title(base_title: str, caption: str, fig_no: int) -> str:
    """图页标题：优先用这张图的图注；没有图注才用「X · 图 N」。"""
    caption_text = _clip(str(caption or "").strip(), 40)
    if caption_text:
        return strip_page_suffix(caption_text)
    return f"{strip_page_suffix(base_title)} · 图 {fig_no}"


def suffix_count(title: str) -> int:
    """标题里续后缀的个数（A1 的断言口径）：>=2 即违规。"""
    text = str(title or "")
    return sum(text.count(suffix) for suffix in ("（续）", "（续图）", CONT_SUFFIX))


def _is_bitmap_entry(entry: dict) -> bool:
    """位图（概念图 / 位图）才吃 MIN_MEDIA_WIDTH_IN；原生图表 / 表格可矢量缩放。"""
    return bool((entry or {}).get("px"))


def _media_page_plan(media: list[dict]) -> list[list[dict]]:
    """把图分到页：位图一页最多一张（两张同页过不了 6 英寸下限），原生图表 / 表格可同页。"""
    pages: list[list[dict]] = []
    for entry in media:
        if pages:
            last = pages[-1]
            if (len(last) < MAX_FIGURES_PER_SLIDE
                    and not _is_bitmap_entry(last[-1]) and not _is_bitmap_entry(entry)):
                last.append(entry)
                continue
        pages.append([entry])
    return pages


def _split_evenly(lines: list[str], pages: int) -> list[list[str]]:
    """按原顺序把要点尽量均分到 pages 页（每页不超过 MAX_BULLETS 条）。"""
    pages = max(1, min(int(pages or 1), len(lines) or 1))
    base, extra = divmod(len(lines), pages)
    out: list[list[str]] = []
    pos = 0
    for idx in range(pages):
        size = base + (1 if idx < extra else 0)
        out.append(list(lines[pos:pos + size]))
        pos += size
    return out


def _groups_carry_content(groups: list[list[str]], media_pages: list[list[dict]],
                          titles: list[str] | None = None) -> bool:
    """每张有图的页都要有足够的可见文字（任务 146 A2.2 + 146b A3）。

    146b 把「要点不够就砍页」放宽成「图页至少 1 条要点」：一页 1 条也算，但
    正文不能少于 ``MEDIA_PAGE_MIN_TEXT_CHARS``（12 字，A3 的底线）。验收表的
    「有图但正文 < 30 字」按**标题 + 正文**口径量（不靠页脚凑字数）；图页由
    :func:`_figure_bullet_lines` 把标题 + 要点补到 30 字。不满足就减页，图按 A4
    的真实原因进备注。
    """
    _ = titles  # 保留参数：调用方仍按页传标题，便于以后把标题计入门槛
    for idx, group in enumerate(groups):
        has_media = idx < len(media_pages) and bool(media_pages[idx])
        if not has_media:
            continue
        bullet_chars = sum(len(line) for line in group)
        if not group or bullet_chars < MEDIA_PAGE_MIN_TEXT_CHARS:
            return False
    return True


# ---------------------------------------------------------------------------
# 讲义正文里的图引用（协议链接）：取 bullets 前必须先解析 / 过滤（任务 35）
#   讲义用 ![概念卡：X](diagram://<content_item_id>) / chart:// / gen:// 写图。
#   这类协议原文**绝不允许**印到页面上：能解析到图的真嵌图，超出每页上限的从文字里
#   删掉并写进演讲备注与 meta.layout_notes（否则该页就只剩一行黑字）。
# ---------------------------------------------------------------------------
_RE_PROTOCOL_ITEM = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\(\s*(?P<scheme>diagram|chart|gen)://(?P<ref>[A-Za-z0-9_\-]+)")
_RE_PROTOCOL_REF = re.compile(
    r"!\[[^\]]*\]\(\s*(?:diagram|chart|gen)://[A-Za-z0-9_\-./#%=]*\)?\s*(?:…)?")
_RE_PROTOCOL_BARE = re.compile(r"(?:diagram|chart|gen)://[A-Za-z0-9_\-./#%=]*(?:…)?")
_RE_MD_IMAGE = re.compile(r"!\[(?P<alt>[^\]]*)\]\([^)\s]*\)?")
_RE_MD_LINK = re.compile(r"(?<!!)\[(?P<text>[^\]]*)\]\([^)\s]*\)?")
# 整行本来只为「引用了某张图」：删掉引用后只剩前缀（如「要点回顾：」）就整行丢弃
_RE_ORPHAN_PREFIX = re.compile(
    r"^(?:[【\[]\s*[^】\]]{0,12}\s*[】\]]\s*)?"
    r"(?:要点回顾|常见误区|详见|见图|如下图|图示|图片?|配图|来源|参考|提示)"
    r"\s*[:：]?\s*…?$")


def protocol_refs(text: str) -> list[dict]:
    """讲义正文里出现的图引用（按出现顺序，同一 id 去重）：``{scheme, ref, alt}``。"""
    raw = str(text or "")
    hits: list[tuple[int, dict]] = []
    covered: list[tuple[int, int]] = []
    for match in _RE_PROTOCOL_ITEM.finditer(raw):
        covered.append((match.start(), match.end()))
        hits.append((match.start(), {"scheme": match.group("scheme"), "ref": match.group("ref"),
                                     "alt": match.group("alt").strip()}))
    for match in _RE_PROTOCOL_BARE.finditer(raw):
        if any(start <= match.start() < end for start, end in covered):
            continue
        scheme, _, ref = match.group(0).partition("://")
        hits.append((match.start(), {"scheme": scheme, "ref": ref.rstrip("…"), "alt": ""}))
    out: list[dict] = []
    seen: set[str] = set()
    for _, entry in sorted(hits, key=lambda pair: pair[0]):
        ref = str(entry.get("ref") or "")
        if not ref or ref in seen:
            continue
        seen.add(ref)
        entry["ref"] = ref
        out.append(entry)
    return out


def sanitize_text(text: str) -> str:
    """清掉文字里的 markdown 图引用 / 链接残渣；整行只剩前缀时返回空串。

    协议引用（``diagram://`` / ``chart://`` / ``gen://``）一律删除：讲义里的图由页面上的
    真实图片承载，原文落到 bullets 就变成「只有黑字没有图」（任务 35）。普通图片链接
    退化成 alt 文字、``[文字](链接)`` 退化成文字、表格行首尾的 ``|`` 去掉，``**加粗**``
    原样保留（任务 21b 的版式 / 图注 / 口播稿观感不变）。
    """
    raw = str(text or "")
    if not raw:
        return ""
    stripped = _RE_PROTOCOL_REF.sub(" ", raw)
    removed = stripped != raw
    after_bare = _RE_PROTOCOL_BARE.sub(" ", stripped)
    removed = removed or after_bare != stripped
    out = _RE_MD_IMAGE.sub(lambda m: m.group("alt").strip(), after_bare)
    out = _RE_MD_LINK.sub(lambda m: m.group("text").strip(), out)
    out = re.sub(r"[ \t]+", " ", out).strip()
    out = re.sub(r"^[\s|]+|[\s|]+$", "", out)  # 表格行首尾的 pipe 残片
    if removed and (not out or _RE_ORPHAN_PREFIX.match(out)):
        return ""
    return out


# 内部话术词表（产品操作 / 开发口径）：只许出现在演讲备注，不许进页面正文（任务 36）
# 任务 37 收窄：不再收「接口 / 重做 / 后端」—— 技术课正文会合法出现
# （「调用接口」「重做这一步」），误伤代价高于收益；真正的泄漏来自模板句。
INTERNAL_WORDS = ("工作台", "第 3 步", "第3步", "原地替换", "选中该章",
                  "item_id", "slug", "_图谱", "meta.", "verify")
_RE_INTERNAL_WORD = re.compile("|".join(re.escape(word) for word in INTERNAL_WORDS))
# 兜底截断后要去掉的悬空字符（标点 + 常见连接词：「…课件：在」→「…课件」）
_INTERNAL_TRIM = " \t·:：，,；;、-—～~（(【[在的与和及或对为向从"


def find_internal_words(text: str) -> list[str]:
    """正文里命中的内部话术（按词表顺序、去重）。"""
    raw = str(text or "")
    return [word for word in INTERNAL_WORDS if word in raw]


def strip_internal_words(text: str) -> str:
    """标题类文字的内部话术兜底清洗：截到第一个命中词之前。

    整句都是内部话术（截完为空）时退化成「删词保句」，保证标题不会变成空串。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    match = _RE_INTERNAL_WORD.search(raw)
    if match is None:
        return raw
    head = raw[: match.start()].rstrip(_INTERNAL_TRIM).strip()
    if head:
        return head
    return _RE_INTERNAL_WORD.sub("", raw).strip(_INTERNAL_TRIM).strip()


def _point_lines(markdown: str) -> list[str]:
    """讲义 → 幻灯片要点行：小节标题与首条内容合并成一行。

    这样投影上不会出现孤零零的「【核心讲解】」，也不会把标题和内容配错；
    「随堂练习」之后的内容不进知识点页（另有练习页）。
    """
    lines: list[str] = []
    for head, items in _sections(markdown):
        if re.match(r"^(随堂练习|课堂练习|练习|习题|作业)", head):
            break
        items = [x for x in items if x.strip()]
        if not items:
            continue
        lines.append(f"【{head}】{items[0]}" if head else items[0])
        lines += items[1:]
    return lines


_CN_NUM = ("一", "二", "三", "四", "五", "六")

# 版式族对应的收口句（口播稿最后一句）
_SPEAK_CLOSING = {
    LAYOUT_COVER: "开场先把课程价值与节奏交代清楚，让学员知道学完能做什么。",
    LAYOUT_GOALS: "先给学员一个整体预期，后面每一章都会回到这张目标清单上对照。",
    LAYOUT_TOC: "带学员过一遍章节顺序，说明每一章解决什么问题、彼此怎么衔接。",
    LAYOUT_CHAPTER: "进入本章前先给一张地图，让学员知道这一章要解决的核心问题。",
    LAYOUT_POINT: "讲完这一页可以停一下，请一位学员用自己的话复述一遍再继续。",
    LAYOUT_FIGURE: "这一页以图为主，指着图把关键关系讲一遍，再回到文字要点。",
    LAYOUT_REVIEW: "回顾页用来收口，请学员逐条对照，把没弄懂的地方圈出来。",
    LAYOUT_QUIZ: "先让学员独立作答，再公布答案与解析，重点讲错得多的选项。",
    LAYOUT_QUESTIONS: "这几问用来课堂互动，先请学员自由回答，再补上参考答案。",
    LAYOUT_END: "最后留出提问时间，并把课后任务和下次课的内容交代清楚。",
}

_SPEAK_FILLERS = (
    "讲解时可以结合学员的日常经验举一个具体例子，帮助建立直观印象。",
    "如果时间紧张，这一页也可以只讲最核心的一条，其余留给学员课下阅读。",
    "讲到这里可以停顿一下，观察学员的反应，确认跟上了再往下推进。",
    "鼓励学员把这一页的关键词记在笔记上，方便课后复习时快速回想。",
)

# 任务 85：口播稿的场景适配 —— 讲义 / 大纲走 LLM 提示词（app/services/scenario.py），
# 课件口播稿在本文件是**确定性模板**（v1 起 0 LLM），所以按场景换「现场向」措辞：
# 录课 / 自学的场合**没有现场学员**，也不许**依赖画面 / 手势指代**（任务 85 §B2）。
# ⚠️ 换后的句子一律**不短于**原句 ⇒ 片段尾部的「≥ SPEAK_MIN」补白口径不受影响；
# 未选场景（或主场景是课堂 / 直播 / 投放）⇒ 一个字都不换（老课件备注逐字不变）。
_SCENARIO_ASYNC_REPLACE: tuple[tuple[str, str], ...] = (
    ("请一位学员用自己的话复述一遍再继续。", "请停下来用自己的话复述一遍要点再继续往下看。"),
    ("指着图把关键关系讲一遍", "把图里的关键关系用话说清楚"),
    ("请学员逐条对照，把没弄懂的地方圈出来。", "请逐条对照，把没弄懂的地方记下来回头再看。"),
    ("先让学员独立作答，再公布答案与解析", "先自己独立作答，再对照答案与解析复盘"),
    ("这几问用来课堂互动，先请学员自由回答，再补上参考答案。",
     "这几问用来自测，先自己认真回答一遍，再对照后面的参考答案。"),
    ("观察学员的反应，确认跟上了再往下推进", "留一点自己消化的时间，确认跟上了再往下推进"),
)

# 开场句：口语态（现状）→ 书面态（只给「学生自学」用；书面句不短于口语句）
_NOTE_OPENING_SPOKEN = "这一页我们来看「"
_NOTE_OPENING_WRITTEN = "本页要讲清楚的是「"


def notes_scenario_style(course: Course | dict | None) -> str:
    """口播稿要不要按场景换口气：``""``（不换，现状）/ ``recorded`` / ``self_study``。

    只认**主场景**（任务 85 §B2：primary 定口气，其余场景作为附加要求叠加）：
    主场景不是「录成视频课 / 学生自学」时——含未选场景、课堂、直播、投放——都返回空串。
    """
    if course is None:
        return ""
    primary = str(scenario.scenario_meta(course).get("primary") or "")
    return primary if primary in ("recorded", "self_study") else ""


def apply_scenario_notes(slides: list[dict], course: Course | dict | None) -> None:
    """就地按场景改写口播稿（``recorded`` 去现场互动与手势指代、``self_study`` 另加书面态开场）。"""
    style = notes_scenario_style(course)
    if not style:
        return
    for slide in slides:
        notes = str(slide.get("notes") or "")
        if not notes:
            continue
        for old, new in _SCENARIO_ASYNC_REPLACE:
            notes = notes.replace(old, new)
        if style == "self_study":
            notes = notes.replace(_NOTE_OPENING_SPOKEN, _NOTE_OPENING_WRITTEN)
        slide["notes"] = notes


def _cn_num(idx: int) -> str:
    return _CN_NUM[idx - 1] if 1 <= idx <= len(_CN_NUM) else str(idx)


def _sentences(text: str, *, min_len: int = 8) -> list[str]:
    """把讲义长段落切成可照念的句子（保留句末标点）。"""
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    if not raw:
        return []
    out: list[str] = []
    for part in re.split(r"(?<=[。！？；!?;])", raw):
        sentence = part.strip()
        if len(sentence) < min_len:
            continue
        out.append(sentence if sentence[-1] in "。！？!?" else sentence + "。")
    return out


def _clean_bullet(text: str) -> str:
    """要点转口播句：去掉「【…】」标记与句尾标点。"""
    raw = re.sub(r"^[【\[](.+?)[】\]]", r"先看「\1」", str(text or "").strip())
    return raw.rstrip("。；;，,")


def _speak_script(title: str, *, bullets, paragraphs: str = "", kind: str = LAYOUT_POINT,
                  head: list[str] | None = None, tail: list[str] | None = None) -> str:
    """把讲义要点扩写成可直接照念的口播稿（150~300 字，确定性，不调 LLM）。"""
    parts: list[str] = [f"这一页我们来看「{title}」。"]
    parts += [str(block).strip() for block in (head or []) if str(block).strip()]
    parts += _sentences(paragraphs)[:3]
    for idx, bullet in enumerate([b for b in bullets if str(b).strip()][:6], 1):
        parts.append(f"第{_cn_num(idx)}条，{_clean_bullet(bullet)}。")
    parts += [str(block).strip() for block in (tail or []) if str(block).strip()]
    parts.append(_SPEAK_CLOSING.get(kind, _SPEAK_CLOSING[LAYOUT_POINT]))
    spoken = "".join(parts)
    filler = 0
    while len(spoken) < SPEAK_MIN and filler < len(_SPEAK_FILLERS) * 3:
        spoken += _SPEAK_FILLERS[filler % len(_SPEAK_FILLERS)]
        filler += 1
    return spoken if len(spoken) <= NOTES_LIMIT else spoken[: NOTES_LIMIT - 1] + "…"


def _cover_meta_bullets() -> list[str]:
    """封面上的作者 / 机构与日期（PPT_COVER_AUTHOR 可配）。"""
    author = str(get_settings().ppt_cover_author or "").strip()
    lines = [f"主讲：{author}"] if author else []
    lines.append(f"日期：{datetime.now().strftime('%Y-%m-%d')}")
    return lines


def _sections(markdown: str) -> list[tuple[str, list[str]]]:
    """讲义 markdown → [(小节标题, 行内容)]：跳过代码块 / 引用 / 表格。"""
    out: list[tuple[str, list[str]]] = []
    in_fence = False
    for raw in str(markdown or "").splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line or line.startswith((">", "|")):
            continue
        if line.startswith("#"):
            head = sanitize_text(line.lstrip("#").strip())
            if head:
                out.append((head, []))
            continue
        match = re.match(r"^([-*+]|\d+[.、)])\s*(.+)$", line)
        content = sanitize_text(match.group(2) if match else line)
        if content and out:
            out[-1][1].append(content)
    return out


def _review_slide(db: Session, course: Course, chapter: OutlineNode,
                  members: list[OutlineNode]) -> dict:
    """章节回顾页：本章 3~5 条要点 + 常见误区（取讲义「小结 / 误区」段）。"""
    summary: list[str] = []
    mistakes: list[str] = []
    for node in members:
        lecture = _lecture_of(_items_for(db, course.id, node.id))
        markdown = str((lecture.body or {}).get("markdown") or "") if lecture else ""
        for head, lines in _sections(markdown):
            if re.search(r"小结|总结|回顾", head):
                summary += [x for x in lines if x][:2]
            if re.search(r"误区|易错|注意|错误", head):
                mistakes += [x for x in lines if x][:2]
            if not re.search(r"误区|易错|注意|错误", head):
                for line in lines:
                    if re.search(r"容易|误区|切忌|往往出错", line):
                        mistakes.append(line)
    if not summary:
        summary = [f"{n.title}：{n.objective or '掌握本知识点的定义与用法'}" for n in members[:3]]
    bullets = ([f"要点回顾：{_clip(x, 40)}" for x in summary[:3]]
               + [f"常见误区：{_clip(x, 40)}" for x in mistakes[:2]])[:MAX_BULLETS - 1]
    if not bullets:
        bullets = [f"{chapter.node_key} {chapter.title}：核心概念与用法"]
    return {"layout": LAYOUT_REVIEW, "title": f"{chapter.node_key} 本章回顾",
            "bullets": bullets,
            "notes": _speak_script(f"{chapter.node_key} 本章回顾", bullets=bullets,
                                   kind=LAYOUT_REVIEW)}


def _question_slide(chapter: OutlineNode, points: list[OutlineNode]) -> dict:
    """课堂提问页（PPT_INCLUDE_QUESTIONS 默认开）：2~3 个提问，参考答案在备注。"""
    asks: list[str] = []
    answers: list[str] = []
    for node in points[:2]:
        asks.append(f"{node.title} 的核心是什么？请用自己的话说一遍。")
        answers.append(f"第{_cn_num(len(answers) + 1)}问 参考答案："
                       f"{node.objective or f'能说出「{node.title}」的定义与关键特征'}")
    asks.append(f"本章哪一步最容易出错？为什么？（结合 {chapter.title}）")
    answers.append(f"第{_cn_num(len(answers) + 1)}问 参考答案：结合讲义小结与常见误区作答，"
                   "能各举一例即可。")
    asks, answers = asks[:3], answers[:3]
    return {"layout": LAYOUT_QUESTIONS, "title": f"{chapter.node_key} 课堂提问",
            "bullets": asks,
            "notes": _speak_script(f"{chapter.node_key} 课堂提问", bullets=asks,
                                   kind=LAYOUT_QUESTIONS, head=answers)}


# ---------------------------------------------------------------------------
# 讲义 → 幻灯片素材
# ---------------------------------------------------------------------------

def _lecture_digest(markdown: str) -> tuple[list[str], list[str], str]:
    """讲义 markdown → (标题行, 要点行, 长段落原文)。

    幻灯片只放标题与短要点（长段落去掉），长段落原文进演讲备注当讲稿。
    """
    heads: list[str] = []
    bullets: list[str] = []
    paragraphs: list[str] = []
    for raw in (markdown or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("|"):  # 表格行另有版式，不进页面文字 / 备注
            continue
        if line.startswith("#"):
            text = sanitize_text(line.lstrip("#").strip())
            if re.match(r"^(随堂练习|课堂练习|练习|习题|作业)", text):
                break  # 习题另有「随堂练习」页，不再混进知识点页
            if text:
                heads.append(text)
            continue
        match = re.match(r"^([-*+]|\d+[.、)])\s*(.+)$", line)
        if match:
            content = sanitize_text(match.group(2))
            if content:
                bullets.append(content)
            continue
        line = sanitize_text(line.lstrip(">").strip())
        if line and len(line) >= LONG_PARAGRAPH:
            paragraphs.append(line)
    return heads, bullets, "\n".join(paragraphs)


def _safe_name(text: str) -> str:
    """文件名安全化（课程 id / 章节 key / 节点 key 都可能带 . 或中文）。"""
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "_", str(text or "")).strip("_")
    return cleaned or "deck"


def deck_palette(theme: str = "", anchor: dict | None = None) -> dict:
    """主题驱动的整套色板：标题 / 正文 / 页脚 / 原生图表 / 概念图 PNG 共用一套颜色。

    ``anchor``（样板风格锚）只把 tone 记进 ``anchor_tone`` 作溯源；颜色一律以主题为准，
    保证同一份课件的文字、图表与概念图不会跑成两套色调。
    """
    spec, _ = resolve_theme(theme or get_settings().ppt_theme)
    palette: dict = dict(spec["palette"])
    palette["colors"] = list(palette["colors"])
    palette.update({
        "theme": spec["id"],
        "theme_name": spec["name"],
        "sizes": dict(spec["sizes"]),
        "footer_style": str(spec["footer_style"]),
        "title_band": bool(spec["title_band"]),
        "round": bool(spec["round"]),
        "font": dict(spec.get("font") or {}),
        "figure": dict(spec["figure"]),
        "warm": bool(palette.get("warm")),
        "anchor_tone": str((anchor or {}).get("tone") or ""),
    })
    return palette


def deck_work_dir(course_id: str, scope: str, chapter_key: str | None = None) -> Path:
    """课件的稳定工作目录：同一 scope 重复生成复用同一目录（概念图可复用、不重复渲染）。"""
    name = f"{course_id}_{scope}"
    if scope == SCOPE_CHAPTER and chapter_key:
        name = f"{name}_{chapter_key}"
    return get_settings().ppt_work_dir.resolve() / _safe_name(name)


def _image_path_of(item: ContentItem) -> Path | None:
    """image 条目的真实文件（body.path 相对 storage_dir）。"""
    raw = str((item.body or {}).get("path") or "")
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else get_settings().storage_dir / raw


def _chart_series(data: dict) -> tuple[list[str], list[dict]]:
    """chart_bar/pie/line 的 body.data → (类别, 系列)，供 PPT 原生图表使用。"""
    labels = [str(x) for x in (data.get("labels") or [])]
    datasets: list[dict] = []
    for raw in data.get("datasets") or []:
        if not isinstance(raw, dict):
            continue
        values = [float(v) if isinstance(v, (int, float)) else 0.0 for v in (raw.get("data") or [])]
        datasets.append({"label": str(raw.get("label") or "系列"), "data": values})
    return labels, datasets


def _table_rows(data: dict) -> tuple[list[str], list[list[str]]]:
    headers = [str(h) for h in (data.get("headers") or [])]
    rows = [[str(cell) for cell in (row or [])] for row in (data.get("rows") or [])]
    return headers, rows


def _media_legibility_of(item: ContentItem, png_path: str | Path) -> dict:
    """图的像素尺寸 + 图内正文的绘制字号（与渲染同一口径，供选档与 meta.layout_notes）。

    量不到（外来位图没有 Excalidraw 元素 / 文件读不开）就只给 ``px`` 或什么都不给 ——
    绝不瞎猜字号，缺什么在 layout_notes 里如实写明。
    """
    from PIL import Image

    from app.services.figure_png import elements_of, plan_render

    try:
        with Image.open(str(png_path)) as image:
            stats: dict = {"px": [int(image.width), int(image.height)]}
    except Exception:  # noqa: BLE001 —— 读不到尺寸就不参与选档
        return {}
    try:
        plan = plan_render(elements_of(item), display_box=MEDIA_FLOOR_BOX)
    except Exception:  # noqa: BLE001 —— 外来位图测不了字号
        return stats
    if plan["text_px"]:
        stats["legibility"] = {
            "texts": plan["text_elements"],
            "min_px": round(float(plan["text_px"][0]), 2),
            "max_px": round(float(plan["text_px"][-1]), 2),
            "span": [round(plan["span_x"], 1), round(plan["span_y"], 1)],
        }
    return stats


def _themed_figure(item: ContentItem, theme: str) -> tuple[list[dict] | None, dict | None]:
    """按**本次课件主题**准备这张图要渲染的元素（任务 127）。

    图内取色以「这份课件实际用的主题」为准（不再只跟后端 ``PPT_THEME`` 配置）：存进库里的
    元素若不是本主题派生色（任务 124 之前生成的存量概念图没有原始 ``data``，重跑不了），
    就地按**同色相 + 同明度档**重映射一次再渲染 —— 几何 / 文本一个字节不动，并把理由写进
    ``layout_notes``（kind="figure-kind"），不静默。返回 ``(要渲染的元素, 说明)``：
    元素为 None 表示「本来就是这套主题的色」⇒ 沿用条目里那一份，不额外记理由。
    """
    try:
        elements = figure_png.elements_of(item)
    except figure_png.FigureError:
        return None, None            # 没有可重绘元素：仍然交给 render_figure 去报错（口径不变）
    recolored, changed = figure_png.recolor_elements(elements, theme=theme)
    if not changed:
        return None, None
    name = deck_palette(theme)["theme_name"]
    return recolored, {
        "kind": "figure-kind",
        "reason": (f"本页这版课件用的是「{name}」主题，而这张存量图（任务 124 之前生成、"
                   f"没有原始 data，重跑不了）存的是旧写死色板 ⇒ 就地按同色相 + 同明度档把 "
                   f"{changed} 处取色重映射到本次主题（几何 / 文本 / 字号未动）"),
    }


def _media_of_figure(item: ContentItem, figures_dir: Path, node_key: str, index: int,
                     failures: list[dict], style: dict | None = None,
                     theme: str = "") -> dict | None:
    """单个图 → 课件素材（图片 / 原生图表 / 原生表格）；失败记进 failures 并返回 None。"""
    label = _figure_label(item)
    kind = str(item.kind or "")
    data = dict((item.body or {}).get("data") or {})
    if kind in ("chart_bar", "chart_line", "chart_pie"):
        labels, datasets = _chart_series(data)
        if labels and datasets:
            return {"type": "chart", "kind": kind, "title": label, "labels": labels, "datasets": datasets}
        reason = "图表数据不完整（缺少 labels / datasets），本页退回文字版"
    elif kind == "chart_table":
        headers, rows = _table_rows(data)
        if headers and rows:
            return {"type": "table", "title": label, "headers": headers, "rows": rows}
        reason = "表格数据不完整（缺少 headers / rows），本页退回文字版"
    elif kind == "image":
        path = _image_path_of(item)
        if path is not None and path.exists():
            entry = {"type": "image", "path": str(path), "caption": label}
            entry.update(_media_legibility_of(item, path))
            return entry
        reason = f"配图文件不存在（{path if path is not None else 'body.path 为空'}），本页退回文字版"
    else:
        target = Path(figures_dir) / f"fig-{_safe_name(node_key)}-{index:02d}.png"
        elements, note = _themed_figure(item, theme)
        path, problem = render_figure(item, target, style=style, display_box=MEDIA_FLOOR_BOX,
                                      elements=elements)
        if path is not None:
            entry = {"type": "image", "path": str(path), "caption": label}
            entry.update(_media_legibility_of(item, path))
            if note:
                entry["figure_note"] = note
            choice = figure_choice_of(item)      # 任务 128：「为什么用这种图」带到课件页上
            if choice:
                entry["figure_choice"] = choice
            return entry
        reason = problem or "重绘概念图失败，本页退回文字版"
    failures.append({"node_key": str(node_key), "figure": label, "reason": str(reason)})
    return None


def _point_media(node: OutlineNode, figures: list[ContentItem], figures_dir: Path,
                 failures: list[dict], style: dict | None = None,
                 theme: str = "") -> list[dict]:
    media: list[dict] = []
    for index, item in enumerate(figures, 1):
        entry = _media_of_figure(item, figures_dir, node.node_key, index, failures, style, theme)
        if entry is not None:
            media.append(entry)
    return media


def _order_by_refs(db: Session, course_id: str, figures: list[ContentItem],
                   markdown: str) -> tuple[list[ContentItem], list[str]]:
    """讲义引用到的图排到最前，保证「能解析到图 → 真的嵌进页面」（任务 35）。

    返回 ``(排好序的图, 找不到条目的引用标签)``：后者由调用方写进备注与
    ``meta.layout_notes``（引用原文本身已被 :func:`sanitize_text` 清掉）。
    """
    refs = protocol_refs(markdown)
    if not refs:
        return list(figures), []
    by_id = {str(f.id): f for f in figures}
    unknown = [r["ref"] for r in refs if r["ref"] not in by_id]
    if unknown:  # 跨知识点引用（讲义里提到别节的图）：课程内找得到就补进来
        extra = db.query(ContentItem).filter(
            ContentItem.course_id == course_id, ContentItem.id.in_(unknown)).all()
        by_id.update({str(item.id): item for item in extra if _is_figure(item)})
    front: list[ContentItem] = []
    missing: list[str] = []
    for entry in refs:
        item = by_id.get(str(entry["ref"]))
        if item is None:
            missing.append(str(entry.get("alt") or "") or f"{str(entry['ref'])[:8]}…")
            continue
        if not any(item is seen for seen in front):
            front.append(item)
    rest = [f for f in figures if not any(f is seen for seen in front)]
    return front + rest, missing


# 知识点页版式判据（任务 108）：只看讲义要点的文本，确定、可测（无随机、无时间、无 LLM）。
# 引用标注先剥掉 —— 「（来源:《…》第 21 段）」里的数字会把「数据页」判据带偏（真库实测）。
_RE_CITATION = re.compile(r"（来源[:：][^）]*）|\[[^\]]*段\]|《[^》]*》第\s*\d+\s*段")
# 「步骤页」判据：序号 / 流程词必须落在**要点开头**（真库实测：正文里出现的「步骤」「流程」多半是
# 叙述（「以下是一个…流程示意」），只有开头带序号的才是真正的分步内容）。
_RE_BULLET_LEAD = re.compile(r"^(?:[【\[](.+?)[】\]])?\s*[*_>\-\s]*")
_RE_STEP_HEAD = re.compile(
    r"^\d+[.、)]|^第[一二三四五六七八九十\d]+步|^[①②③④⑤]|^→|^首先|^其次|^然后|^接着|^最后|^步骤|^阶段")
# 「对比页」判据：标题是「A 与 B 的…」式对照（真库实测：「确定性输出与创意输出的平衡」）；
# 正文里的「而非 / 对比」多半只是行文用词，不足以撑起两栏对照版式，故不作判据。
_RE_TITLE_PAIR = re.compile(r"(.{2,8})与(.{2,8})的(?:平衡|对比|区别|差异|取舍|权衡|关系|搭配)")
_RE_STAT_MARK = re.compile(r"\d+(?:\.\d+)?\s*(?:%|％|倍)")
STEP_MARK_MIN = 2   # ≥2 条要点带序号 / 流程词才算「步骤页」（单条命中多半是巧合）
STAT_MARK_MIN = 2   # ≥2 条要点带百分比 / 倍数才算「数据页」（同样避开巧合）


def _mark_hit(text: str, pattern: re.Pattern) -> str:
    """命中判据的那几个字（写进 layout_notes 的「为什么」，便于人工核对）。"""
    hit = pattern.search(str(text or ""))
    return hit.group(0).strip() if hit else ""


def compare_sides(title: str) -> tuple[str, str]:
    """「A 与 B 的…」式标题 → 两栏的栏名（取自标题原文，不新造词）；不是对照式标题就给空串。"""
    hit = _RE_TITLE_PAIR.search(str(title or ""))
    return (hit.group(1).strip(), hit.group(2).strip()) if hit else ("", "")


def point_variant_of(bullets: list, title: str = "") -> tuple[str, str]:
    """知识点页选哪种版式：返回 ``(layout, reason)`` —— 确定、可测、可复现。

    优先级 **对比 > 步骤 > 数据 > 默认**：标题是「A 与 B 的…」式对照 => 两栏对照；
    否则 ≥2 条带序号 / 流程词 ⇒ 步骤（时间线）页；否则 ≥2 条带百分比 / 倍数 ⇒ 数据页；
    都不命中 ⇒ ``LAYOUT_POINT``（沿用「左要点 + 右图」）。
    """
    lines = [x for x in (_RE_CITATION.sub("", str(b or "")).strip() for b in (bullets or [])) if x]
    total = len(lines)
    left, right = compare_sides(title)
    if left and right:
        return LAYOUT_POINT_COMPARE, (
            f"标题是「{left} 与 {right}」式对照（{total} 条要点）⇒ 两栏对照版式")
    steps = [x for x in lines if _RE_STEP_HEAD.search(_RE_BULLET_LEAD.sub("", x))]
    if len(steps) >= STEP_MARK_MIN:
        return LAYOUT_POINT_STEPS, (
            f"{len(steps)}/{total} 条要点以序号或流程词开头"
            f"（如「{_mark_hit(_RE_BULLET_LEAD.sub('', steps[0]), _RE_STEP_HEAD)}」）⇒ 步骤 / 时间线版式")
    stats = [x for x in lines if _RE_STAT_MARK.search(x)]
    if len(stats) >= STAT_MARK_MIN:
        return LAYOUT_POINT_DATA, (
            f"{len(stats)}/{total} 条要点带百分比或倍数"
            f"（如「{_mark_hit(stats[0], _RE_STAT_MARK)}」）⇒ 大数字数据页")
    return LAYOUT_POINT, f"标题不是对照式、要点里也没有 ≥{STEP_MARK_MIN} 条序号或 ≥{STAT_MARK_MIN} 条百分比（{total} 条）⇒ 默认知识点页"


def figure_choice_of(item: ContentItem) -> dict | None:
    """图条目 → 「为什么用这种图」（任务 128）；目前只有象限图有内容判据，其余返回 None。

    象限图的判据是任务 127 写在图条目 ``meta.layout_notes``（``kind="figure-kind"``）上的
    :func:`app.services.lecture.quadrant_criteria` 理由 —— 这里把它**引用**到课件页的选型理由里
    （只读图条目、不改动它）；手工建的象限图没有那条判据时给一句固定说法，仍然不静默。
    """
    if str(item.kind or "") != "chart_quadrant":
        return None
    base = "本页数据图是象限图（两轴四象限：把两个维度的取舍摊成四格）"
    note = next((n for n in (dict(item.meta or {}).get("layout_notes") or [])
                 if isinstance(n, dict) and str(n.get("kind") or "") == "figure-kind"
                 and str(n.get("reason") or "").strip()), None)
    # 有判据就用判据原文（它自己带着命中的词与「⇒ 数据卡选象限图」的结论，不再叠一层说法）；
    # 手工建的象限图没有那条判据时，给一句固定说法，仍然不静默。
    return {"rule": "quadrant", "reason": str(note["reason"]) if note else base}


def slide_choice_of(spec: dict) -> dict:
    """一页的版式选型理由（任务 128）：``{"rule": 规则名, "reason": 人话理由}``。

    只读规格、不改任何东西：理由一律**复用既有判据文案**（``point_variant_of`` 的说明、
    结构页的页名、纯图页与象限图的既有说法），本函数不新造一套版式口径。
    """
    layout = _layout_of(spec)
    info = dict(spec.get("point_variant") or {})
    why = str(info.get("reason") or "").strip()
    if str(info.get("layout") or "") == layout and why:
        rule = "point-variant" if layout in POINT_VARIANTS else "point-default"
    elif layout in POINT_VARIANTS:
        rule, why = "point-variant", f"本页要点命中 {layout} 的版式判据（口径见 point_variant_of）"
    elif layout == LAYOUT_POINT:
        rule = "point-default"
        why = (f"标题不是「A 与 B 的…」式对照、要点里也没有 ≥{STEP_MARK_MIN} 条序号"
               f"或 ≥{STAT_MARK_MIN} 条百分比 ⇒ 默认知识点页（左要点 + 右图）")
    elif layout == LAYOUT_FIGURE:
        rule = "figure-only"
        why = "本页的图放不进分栏 / 满幅档，改用整页给图（放不下的要点移进备注）"
    else:
        rule = "structural"
        why = f"本页是{STRUCTURAL_LAYOUT_NAMES.get(layout, layout)}页，版式按它在课件里的位置固定"
    for entry in (spec.get("media") or []):
        choice = dict(entry.get("figure_choice") or {}) if isinstance(entry, dict) else {}
        if choice:
            rule = str(choice.get("rule") or rule)
            why = f"{choice.get('reason')}；本页版式「{layout}」：{why}"
            break
    return {"rule": rule, "reason": f"{LAYOUT_CHOICE_RULES.get(rule, rule)}：{why}"}


def _point_slides(node: OutlineNode, items: list[ContentItem], media: list[dict],
                  *, slides_per_point: int = 3, failed: list[dict] | None = None,
                  overflow: list[str] | None = None,
                  overflow_reasons: list[str] | None = None,
                  missing_refs: list[str] | None = None) -> list[dict]:
    """一个知识点 1~N 页：讲义「标题 + 要点」进正文，长段落进备注，概念图/数据图入页。

    任务 146b A2：页数 = ``min(上限, max(文本块数, 图页数))``，默认上限 3；图页同样
    至少 1 条要点（一页 1 条也算），不再要求每页先凑满 30 字。
    任务 146b A4：每张没上页的图都带一条真实原因（页数上限 / 要点不足 / 几何门槛），
    原因与数值落在 ``figure_dropped["reasons"]``，同时写进备注与 layout_notes。
    """
    lecture = _lecture_of(items)
    markdown = str((lecture.body or {}).get("markdown") or "") if lecture else ""
    _heads, bullets, paragraphs = _lecture_digest(markdown)
    lines = _point_lines(markdown) or list(bullets)
    if not lines:  # 没有要点：退回目标 / 教学策略，避免出现「一张图 + 一句图注」的空页
        lines = [node.objective or node.title]
        if node.teaching_strategy:
            lines.append(f"教学策略：{node.teaching_strategy}")
    notes = _speak_script(node.title, bullets=lines, paragraphs=paragraphs, kind=LAYOUT_POINT)
    if failed:  # 没插进课件的图：正文保持文字版，原因写进备注（面板另有 meta.figure_failures）
        detail = "；".join(f"[图:{entry['figure']}] {entry['reason']}" for entry in failed[:MAX_FIGURES_PER_SLIDE])
        notes = compose_notes(notes, [f"未能嵌入的图：{detail}"])
    chunks = _chunk(lines, MAX_BULLETS)
    limit = max(1, min(3, int(slides_per_point or 3)))
    # 任务 146b A2：页数同时吃进文本块数与图页数（默认上限 3）。
    # 位图一页最多一张（两张同页时每张只剩 ≈6.0 英寸，过不了 MIN_MEDIA_WIDTH_IN）；
    # 原生图表 / 表格可以同页。超出的图进备注，绝不为了「放图」单独开一页。
    media_pages = _media_page_plan(media)
    flat = [line for chunk in chunks for line in chunk]
    pages = max(1, min(limit, max(len(chunks), len(media_pages), 1)))
    if flat:
        # A2/A3：每页至少 1 条要点（一页 1 条也算）；不硬造 0 要点页。
        pages = min(pages, len(flat))
    titles = [node.title if idx == 0 else continue_title(node.title) for idx in range(pages)]
    groups = _split_evenly(flat[: pages * MAX_BULLETS], pages)
    dropped_lines = flat[pages * MAX_BULLETS:]
    while pages > 1 and not _groups_carry_content(groups, media_pages, titles):
        pages -= 1
        titles = [node.title if idx == 0 else continue_title(node.title)
                  for idx in range(pages)]
        groups = _split_evenly(flat[: pages * MAX_BULLETS], pages)
        dropped_lines = flat[pages * MAX_BULLETS:]
    pages = len(groups)
    if pages < len(media_pages):
        # 任务 146：文本不够分页时只能留一页图 ⇒ 优先保留**有选型理由**的图（象限图等
        # 由判据生成的图），其余图按既有口径进备注；不为了留图再造低信息页。
        ranked = sorted(range(len(media_pages)),
                        key=lambda i: (0 if any(m.get("figure_choice")
                                                for m in media_pages[i]) else 1, i))
        keep_idx = sorted(ranked[:pages])
        kept_media = [media_pages[i] for i in keep_idx]
        keep_set = set(keep_idx)
        dropped_idx = [i for i in range(len(media_pages)) if i not in keep_set]
        dropped_media = [m for i, group in enumerate(media_pages) if i not in keep_set
                         for m in group]
    else:
        kept_media = media_pages[:pages]
        dropped_idx = list(range(pages, len(media_pages)))
        dropped_media = [m for group in media_pages[pages:] for m in group]
    # 原生图表 / 表格没有 caption 字段（只有 title）：统一取一个可说出口的图名。
    leftover = [str(m.get("caption") or m.get("title") or "本页配图") for m in dropped_media] \
        + list(overflow or [])
    missing = [str(x).strip() for x in (missing_refs or []) if str(x).strip()]
    # A4：逐张给出真实原因 + 数值（绝不再写「因每页图上限 2 张」这种假话）。
    fig_no_of = {id(m): j + 1 for j, m in enumerate(media)}
    total_figures = len(media) + len([x for x in (overflow or []) if str(x).strip()])
    min_group_chars = min((sum(len(b) for b in group) for group in groups), default=0)
    drop_reasons: list[str] = []
    for i in dropped_idx:
        for m in media_pages[i]:
            label = str(m.get("caption") or m.get("title") or "本页配图")
            fig_no = fig_no_of.get(id(m), i + 1)
            if pages >= limit:
                drop_reasons.append(_page_limit_drop_reason(
                    node, total_figures, fig_no, label, limit))
            else:
                drop_reasons.append(_bullet_shortage_drop_reason(
                    node, total_figures, fig_no, label, len(flat), pages, min_group_chars))
    overflow_labels = [str(x).strip() for x in (overflow or []) if str(x).strip()]
    if overflow_labels:
        given = [str(x).strip() for x in (overflow_reasons or []) if str(x).strip()]
        if len(given) == len(overflow_labels):
            drop_reasons.extend(given)
        else:
            for k, label in enumerate(overflow_labels, 1):
                drop_reasons.append(_page_limit_drop_reason(
                    node, total_figures, len(media) + k, label, limit))
    slides: list[dict] = []
    for idx in range(pages):
        page_notes = notes
        drop: dict = {}
        if idx == pages - 1 and (leftover or missing):
            drop = {"overflow": list(leftover), "missing": list(missing)}
            if drop_reasons:
                drop["reasons"] = list(drop_reasons)
            page_notes = compose_notes(
                notes, _figure_drop_reasons(leftover, missing, drop_reasons))
        if idx == pages - 1 and dropped_lines:
            page_notes = compose_notes(page_notes,
                                       [f"本页未放入的要点：{'；'.join(dropped_lines)}"])
        # 任务 108：有要点就按内容判据选知识点页版式（对比 / 步骤 / 数据 / 默认）；
        # 选了什么、为什么，渲染前不改动，逐页落 meta.layout_notes（kind="point-variant"）。
        page_media = kept_media[idx] if idx < len(kept_media) else []
        page_title = (node.title if idx == 0
                      else continue_title(node.title) if groups[idx]
                      else figure_page_title(node.title,
                                             (page_media[0].get("caption") if page_media else ""),
                                             idx + 1))
        variant, why = (point_variant_of(groups[idx], page_title) if groups[idx]
                        else (LAYOUT_FIGURE, ""))
        slides.append({
            "layout": variant,
            "title": page_title,
            "bullets": list(groups[idx]),
            "notes": page_notes,
            "media": list(page_media),
            "figure_dropped": drop,
            "point_variant": {"layout": variant, "reason": why} if why else {},
        })
    return slides


QUIZ_OPTION_LIMIT = 5   # 单题选项上限（A~E）：题干 1 条 + 选项 ≤5 条 = 练习页 6 条上限

# 任务 111：随堂练习改「一页 2–3 题」，把课件长度收回来（108 的一页一题把课件拉到 35% 是练习页）。
# 一页放几题**不靠感觉**：按「题干卡 + 选项卡」的高度（由字号与列宽算出来，见 quiz_pack_fits）
# 与既有上限一起判，放不下就少放一题；每题放了几题、为什么，逐页写进 meta.layout_notes
# （kind="quiz-pack"）。上限口径一个字不改：单题选项 ≤ QUIZ_OPTION_LIMIT、装饰元素
# ≤ MAX_DECOR_PER_SLIDE、题干 / 选项文字与「答案只进备注」沿用 108 的口径。
QUIZ_PER_PAGE = 2               # 一页最多几题（规格建议 2，可配 2–3；判据不随它改）
QUIZ_POINTS_PER_QUESTION = 1 + QUIZ_OPTION_LIMIT   # 题干 1 条 + 选项 ≤5 条（= 108 的练习页口径）
QUIZ_MAX_POINTS_PER_PAGE = QUIZ_PER_PAGE * QUIZ_POINTS_PER_QUESTION   # 一页要点上限（随题数放大）
QUIZ_STEM_SIZE_PT = 24.0        # 题干字号（与 108 同一口径）
QUIZ_OPTION_SIZE_PT = 15.0      # 选项字号（与 108 同一口径）
QUIZ_PACK_GAP_IN = 0.26         # 打包页里题与题之间的间距（中间画一条分隔线）
QUIZ_PACK_STEM_MIN_IN = 0.62    # 打包页题干卡最小高（编号徽章 0.44 + 上下边距）
QUIZ_PACK_PAD_IN = 0.10         # 打包页题干卡内边距（题干文字与卡片边）
QUIZ_PACK_STEM_GAP_IN = 0.12    # 打包页题干卡与选项卡之间的间距
QUIZ_OPTION_ROW_MIN_IN = 0.72   # 打包页选项卡行高下限（只加卡片高度，不压小字号）
QUIZ_OPTION_ROW_MAX_IN = 1.10   # 选项卡行高上限（= 108 的 row_height_max，未动）
QUIZ_OPTION_PAD_IN = 0.22       # 选项卡内文字上下留白（= 108 的 cell_top+0.12 / 高 −0.22）
QUIZ_OPTION_TEXT_PAD_IN = 0.50  # 选项卡内文字左右留白（= 108：左 0.28 / 右 0.22）
QUIZ_STEM_TEXT_W_IN = CARD_WIDTH_IN - 0.94   # 题干文字框宽（= 108：左移 0.76 / 右留白 0.18）

_QUIZ_OPTION_RE = re.compile(r"^[A-H][.、．]\s*")


def quiz_stem_height_in(stem: str) -> float:
    """打包页里一题的题干卡高：题干折行需要的高度 + 上下内边距（确定、可测）。"""
    need = est_height_in([str(stem or "")], QUIZ_STEM_SIZE_PT, QUIZ_STEM_TEXT_W_IN)
    return max(QUIZ_PACK_STEM_MIN_IN, need + QUIZ_PACK_PAD_IN * 2)


def quiz_option_need_in(options: list[str]) -> float:
    """打包页里这一题「最长的那个选项」在它那一格里需要的高度（英寸）。"""
    count = max(1, len(options))
    cell_w = (CARD_WIDTH_IN - CARD_GAP_IN * (count - 1)) / count
    text_w = cell_w - QUIZ_OPTION_TEXT_PAD_IN
    return max(est_height_in([opt], QUIZ_OPTION_SIZE_PT, text_w) for opt in options)


def quiz_option_row_height_in(options: list[str]) -> float:
    """打包页里选项卡一行的高度：够放最长的选项，且不低于下限、不高于 108 的上限。"""
    return min(QUIZ_OPTION_ROW_MAX_IN,
               max(QUIZ_OPTION_ROW_MIN_IN, quiz_option_need_in(options) + QUIZ_OPTION_PAD_IN))


def quiz_block_height_in(stem: str, options: list[str]) -> float:
    """打包页里一题占的总高：题干卡（+ 间距 + 选项卡行；没选项就只剩题干卡）。"""
    stem_h = quiz_stem_height_in(stem)
    if not options:
        return stem_h
    return stem_h + QUIZ_PACK_STEM_GAP_IN + quiz_option_row_height_in(options)


def quiz_page_height_in(blocks: list[tuple[str, list[str]]]) -> float:
    """这一页所有题的合计高度（含题与题之间的分隔间距）。"""
    if not blocks:
        return 0.0
    return (sum(quiz_block_height_in(stem, options) for stem, options in blocks)
            + QUIZ_PACK_GAP_IN * (len(blocks) - 1))


def quiz_page_decor_count(blocks: list[tuple[str, list[str]]]) -> int:
    """这一页要画的装饰元素数：每题「题干卡 + 编号徽章 + 选项卡片」+ 题间分隔线。"""
    hints = int(any(not options for _stem, options in blocks))
    return (sum(2 + len(options) for _stem, options in blocks)
            + max(0, len(blocks) - 1) + hints)


def quiz_pack_fits(blocks: list[tuple[str, list[str]]]) -> tuple[bool, str]:
    """确定判据：这几题放一页放得下吗 —— 返回 ``(放得下?, 放不下的原因)``。

    三条一起判（与 ``_render_quiz`` 同一套几何，不靠试渲染）：① 题数 ≤ :data:`QUIZ_PER_PAGE`；
    ② 各题「题干卡 + 选项卡行」合计（含题间分隔）≤ 正文区高 :data:`CARD_HEIGHT_IN`；
    ③ 装饰元素（题干卡 + 徽章 + 选项卡 + 分隔线）≤ :data:`MAX_DECOR_PER_SLIDE`。
    选项在自己那一格里放不下（只剩「压小字号」一条路）也算放不下 —— 宁可少放一题，不压字号。
    """
    if not blocks:
        return False, "没有题"
    if len(blocks) > QUIZ_PER_PAGE:
        return False, f"{len(blocks)} 题超过每页上限 {QUIZ_PER_PAGE} 题"
    for _stem, options in blocks:
        if options and quiz_option_need_in(options) + QUIZ_OPTION_PAD_IN > QUIZ_OPTION_ROW_MAX_IN:
            return False, (f"最长的一个选项要 {quiz_option_need_in(options):.2f} 英寸 > 行高上限 "
                           f"{QUIZ_OPTION_ROW_MAX_IN:.2f} 英寸（要放只能压小字号）")
    used = quiz_page_height_in(blocks)
    if any(not options for _stem, options in blocks):
        used += QUIZ_OPEN_HINTS_MIN_IN + QUIZ_PACK_GAP_IN   # 简答题必须留出「作答提示」面板
    if used > CARD_HEIGHT_IN:
        return False, f"题干卡 + 选项卡合计 {used:.2f} 英寸 > 可用 {CARD_HEIGHT_IN:.2f} 英寸"
    decor = quiz_page_decor_count(blocks)
    if decor > MAX_DECOR_PER_SLIDE:
        return False, f"装饰元素 {decor} 个 > 上限 {MAX_DECOR_PER_SLIDE} 个"
    return True, ""


def _quiz_slides(chapter: OutlineNode, members: list[OutlineNode],
                 quizzes: list[QuizItem]) -> list[dict]:
    """随堂练习：一页放 **1–QUIZ_PER_PAGE 题**（任务 111；108 是一页一题）—— 放几题由
    :func:`quiz_pack_fits` 的确定判据定（题干行数 + 选项数 + 卡片高度，见同处注释），
    放不下就少放一题；「这一页几题、为什么」写进 ``quiz_pack`` ⇒ 逐页落 ``meta.layout_notes``。

    题干与选项在页面上分成两类文本：题干走「大字」、选项走卡片（见 ``_render_quiz``）。
    答案 / 解析仍然**只在备注**里，沿用既有「学员版去答案」口径（正文里一个字都不出现）。
    """
    letters = "ABCDEFGH"
    questions: list[dict] = []
    for idx, quiz in enumerate(quizzes, 1):
        # 题干按练习页的字数口径裁剪（POINT_CHARS["quiz"]，与 fit_spec 同一把尺子）：此前是
        # 先剪到 120 再被 fit_spec 剪到 80，上页的文字一模一样；这里直接用同一个数，判据
        # 算的高度才与页上真正显示的文字一致（否则「放不下」的理由会拿没上页的长文本去算）。
        question = clip_chars(quiz.question, point_chars(LAYOUT_QUIZ))[0]
        options = [f"{letters[i]}. {_clip(opt, 40)}"
                   for i, opt in enumerate(list(quiz.options or [])[:QUIZ_OPTION_LIMIT])] \
            if quiz.qtype == "choice" else []
        # 任务 55：开放题（draw/material/experiment）没有答案 —— 备注里回退到评分要点
        answer = (_clip(quiz.answer, 260)
                  or _clip((quiz.meta or {}).get("rubric") or "", 260)
                  or "（未填写答案）")
        questions.append({"index": idx, "stem": question, "options": options,
                          "answer": answer, "explain": _clip(quiz.explanation, 260)})
    if not questions:
        bullets = ["（本章暂无习题：可在网页端生成习题后重做本课件）"]
        return [{"layout": LAYOUT_QUIZ, "title": QUIZ_TITLE, "bullets": bullets,
                 "quiz_index": 0,
                 "notes": _speak_script(QUIZ_TITLE, bullets=bullets, kind=LAYOUT_QUIZ, head=[
                     f"{chapter.node_key}《{chapter.title}》尚未生成随堂练习。"])}]

    pages: list[list[dict]] = []
    # 与 pages 对齐：这一页为什么不再往下放一题（"" = 已达每页上限；None = 本章没题了）
    stops: list[str | None] = []
    page: list[dict] = []
    for quiz in questions:
        if not page:
            page = [quiz]
            continue
        ok, why = quiz_pack_fits(_quiz_blocks([*page, quiz]))
        if ok:
            page.append(quiz)
            continue
        pages.append(page)
        stops.append(f"第 {quiz['index']} 题放不下：{why}")
        page = [quiz]
    pages.append(page)
    stops.append(None)

    specs: list[dict] = []
    for group, stop in zip(pages, stops):
        bullets = [line for quiz in group for line in [quiz["stem"], *quiz["options"]]]
        head = [f"第{quiz['index']}题 答案：{quiz['answer']}"
                + (f"\n解析：{quiz['explain']}" if quiz["explain"] else "")
                for quiz in group]
        head.append(f"{chapter.node_key} {chapter.title}：请学员先独立作答，再对照答案与解析。")
        specs.append({"layout": LAYOUT_QUIZ, "title": QUIZ_TITLE, "bullets": bullets,
                      "quiz_index": group[0]["index"],
                      "quiz_indices": [quiz["index"] for quiz in group],
                      "quiz_pack": {"count": len(group),
                                    "reason": _quiz_pack_reason(group, stop)},
                      "notes": _speak_script(QUIZ_TITLE, bullets=bullets,
                                             kind=LAYOUT_QUIZ, head=head)})
    return specs


def _quiz_blocks(questions: list[dict]) -> list[tuple[str, list[str]]]:
    """题 dict → 判据要的 ``[(题干, 选项), …]`` 形状。"""
    return [(str(quiz.get("stem") or ""), list(quiz.get("options") or []))
            for quiz in questions]


def _quiz_pack_reason(group: list[dict], stopped_by: str | None) -> str:
    """「这一页几题、为什么」—— 逐页进 ``meta.layout_notes``（kind="quiz-pack"），绝不静默。"""
    first, last = group[0]["index"], group[-1]["index"]
    where = f"第 {first} 题" if first == last else f"第 {first}–{last} 题"
    used = quiz_page_height_in(_quiz_blocks(group))
    why = (f"本页 {len(group)} 题（{where}）：题干卡 + 选项卡合计 {used:.2f} 英寸 "
           f"≤ 可用 {CARD_HEIGHT_IN:.2f} 英寸")
    if stopped_by is None:
        return why + "；本章练习到此为止"
    if stopped_by:
        return why + f"；{stopped_by} ⇒ 从它起另起一页"
    return why + f"；已达每页上限 {QUIZ_PER_PAGE} 题"


# ---------------------------------------------------------------------------
# 页面结构（规划文档 §3.2）
# ---------------------------------------------------------------------------

# 末页兜底动作：章节不足 3 章时补齐「3 条可立即落地」的通用自检（任务 36）
_DEFAULT_ACTIONS = (
    "把本课方法套到手头的一项真实任务上，做完记一条心得",
    "给关键结论补一条可核验的来源或依据",
    "交付前用本课的自检清单过一遍，再发给客户 / 学员",
)


def _figure_rank(item: ContentItem) -> int:
    """代表图优先级：0 概念图 > 1 数据图 / 表 / 流程图 / 位图 > 2 概念卡。"""
    kind = str(item.kind or "")
    role = str((item.meta or {}).get("role") or "")
    if kind == "diagram" and role == "concept":
        return 0
    if kind.startswith("chart_") or kind in ("image", "flowchart"):
        return 1
    return 2


def _chapter_overview_slide(db: Session, course_id: str, chapter: OutlineNode,
                            nodes: list[OutlineNode], *,
                            figures_dir: str | Path | None = None,
                            failures: list[dict] | None = None,
                            style: dict | None = None,
                            theme: str = "") -> dict:
    """章节概览页：章名 + 知识点 + 一句本章目标 + 该章的 1 张代表图（任务 36）。

    正文里不再出现「本章图 N 张：[图:…]」这种给开发看的内部清单 —— 图由页面上的真实图片
    承载（概念图 > 数据图 > 概念卡，渲染失败就换下一张候选并记进 ``figure_failures``）。
    """
    members = _members_of(chapter, nodes)
    points = [n for n in members if n.node_key != chapter.node_key]
    figures = [f for node in members for f in _figures_of(_items_for(db, course_id, node.id))]
    out_dir = Path(figures_dir) if figures_dir else (
        get_settings().ppt_work_dir.resolve() / FIGURES_SUBDIR)
    media: list[dict] = []
    for index, candidate in enumerate(sorted(figures, key=_figure_rank)[:3], 1):
        entry = _media_of_figure(candidate, out_dir, f"{chapter.node_key}-cover", index,
                                 failures if failures is not None else [], style, theme)
        if entry is not None:
            media.append(entry)
            break
    bullets = [f"{n.node_key} {n.title}" for n in points[:MAX_BULLETS]] or ["（本章暂无知识点）"]
    target = _clip(chapter.objective or "", 40)
    if target:
        bullets = [f"本章目标：{target}"] + bullets
    notes = _speak_script(chapter.title, bullets=bullets, kind=LAYOUT_CHAPTER,
                          paragraphs="；".join(x for x in (chapter.objective,
                                                          chapter.teaching_strategy) if x))
    return {"layout": LAYOUT_CHAPTER, "title": f"{chapter.node_key} {chapter.title}",
            "bullets": bullets, "notes": notes, "media": media}


# 末页「落地清单」用的模板句：固定部分约 30 字位，end 版式单条上限 40 字位（任务 37）。
# 章名按 9 字位截断，保证动作句本身（「挑一份你手上的真实材料试一遍」）不被版式砍掉。
END_ACTION_CHARS = point_chars("end")
_END_ACTION_NAME_UNITS = END_ACTION_CHARS - char_units("落地清单 1：用「」这一章的方法，"
                                                    "挑一份你手上的真实材料试一遍")


def _end_action_text(title: str) -> str:
    """0-LLM 动作句：用「章名」这一章的方法，挑一份你手上的真实材料试一遍（任务 37）。"""
    name = clip_chars(str(title or "").strip(), int(_END_ACTION_NAME_UNITS))[0]
    return f"用「{name}」这一章的方法，挑一份你手上的真实材料试一遍"


def _course_end_bullets(course: Course, chapters: list[OutlineNode], goal: str) -> list[str]:
    """整课末页正文：给学员的收尾（学完能做什么 + 3 条可立即落地的清单）。

    章节条目是 0-LLM 模板句（任务 37）：套章名说清「拿这一章的方法做什么」，不再把
    章目标（「掌握…」）当动作；章不足 3 章时用 ``_DEFAULT_ACTIONS`` 兜底。
    产品操作提示只进演讲备注，绝不进正文（任务 36）。
    """
    actions = [_end_action_text(chapter.title) for chapter in chapters[:3]]
    while len(actions) < len(_DEFAULT_ACTIONS):
        actions.append(_DEFAULT_ACTIONS[len(actions)])
    bullets = [f"学完你将能：{_clip(goal or course.title, 40)}"]
    bullets += [f"落地清单 {idx}：{text}" for idx, text in enumerate(actions[:3], 1)]
    return bullets[:MAX_BULLETS]


def course_slides(db: Session, course: Course, nodes: list[OutlineNode], *,
                  theme: str = "", figures_dir: str | Path | None = None
                  ) -> tuple[list[dict], dict]:
    """整课 PPT：封面 + 课程目标 + 目录 + 每章 1 页概览（含代表图）+ 末页。"""
    chapters = _chapters_of(nodes)
    anchor = get_style_anchor(course)
    outline = nodes[0].outline if nodes else None
    goal = str((outline.content or {}).get("goal") or "") if outline is not None else ""
    out_dir = Path(figures_dir) if figures_dir else (
        get_settings().ppt_work_dir.resolve() / FIGURES_SUBDIR)
    figure_style = dict(deck_palette(theme).get("figure") or {})
    failures: list[dict] = []
    chart_ids: list[str] = []
    media_count = 0

    cover_bullets = [f"受众：{course.audience or '通用受众'}", f"课时：{course.duration_minutes} 分钟"]
    if anchor:
        cover_bullets.append(f"样板风格：{anchor_summary(anchor)}")
    cover_bullets += _cover_meta_bullets()
    slides: list[dict] = [{
        "layout": LAYOUT_COVER,
        "title": course.title,
        "subtitle": f"{course.subject or '课程'} · {course.audience or '通用受众'}",
        "bullets": cover_bullets,
        # 任务 144：封面数据条的真实值来源（章数 = 大纲章节数，不靠页面数去猜）
        "cover_facts": {"chapters": len(chapters)},
        "notes": _speak_script(course.title, bullets=cover_bullets, kind=LAYOUT_COVER,
                               paragraphs=goal or "本课程围绕大纲逐章展开。"),
    }]

    goal_bullets = [goal] if goal else []
    goal_bullets += [f"{c.node_key} {c.title}：{c.objective or '掌握本章核心内容'}" for c in chapters[:MAX_BULLETS]]
    slides.append({
        "layout": LAYOUT_GOALS,
        "title": COURSE_GOAL_TITLE,
        "bullets": goal_bullets or ["按大纲逐章掌握核心概念与基本方法"],
        "notes": _speak_script(COURSE_GOAL_TITLE, bullets=goal_bullets, kind=LAYOUT_GOALS,
                               paragraphs=goal or "课程目标来自大纲总目标与各章目标。"),
    })

    toc: list[str] = []
    for chapter in chapters:
        line = f"{chapter.node_key} {chapter.title}"
        look = _clip(chapter.objective or "", 22)   # 本章看点（学员向；没有目标就不写）
        toc.append(f"{line} · {look}" if look else line)
    toc_lines = toc[:MAX_BULLETS] or ["（尚无章节）"]
    slides.append({
        "layout": LAYOUT_TOC,
        "title": COURSE_TOC_TITLE,
        "bullets": toc_lines,
        "notes": _speak_script(COURSE_TOC_TITLE, bullets=toc_lines, kind=LAYOUT_TOC),
    })

    for chapter in chapters:
        members = _members_of(chapter, nodes)
        chart_ids += [f.id for n in members
                      for f in _figures_of(_items_for(db, course.id, n.id))]
        overview = _chapter_overview_slide(db, course.id, chapter, nodes,
                                           figures_dir=out_dir, failures=failures,
                                           style=figure_style, theme=theme)
        media_count += len(overview.get("media") or [])
        slides.append(overview)

    end_bullets = _course_end_bullets(course, chapters, goal)
    ops = ["每章课件在工作台第 3 步选中该章生成；"
           "整课课件与各章课件均可单独重做（原地替换）。"]
    slides.append({
        "layout": LAYOUT_END,
        "title": COURSE_END_TITLE,
        "bullets": end_bullets,
        "notes": compose_notes(_speak_script(COURSE_END_TITLE, bullets=end_bullets,
                                             kind=LAYOUT_END, paragraphs=goal),
                               ["课件操作提示（仅讲师 / 运营可见，勿投屏）：" + "；".join(ops)]),
    })
    source = {"outline_rev": (outline.id if outline is not None else ""),
              "lecture_ids": [], "chart_ids": chart_ids,
              "figure_count": media_count, "figure_failures": failures}
    apply_scenario_notes(slides, course)   # 任务 85：场景只改口播稿口气（未选场景一字不改）
    return slides, source


def chapter_slides(db: Session, course: Course, chapter: OutlineNode,
                   nodes: list[OutlineNode], *, figures_dir: str | Path | None = None,
                   slides_per_point: int = 3,
                   theme: str = "") -> tuple[list[dict], dict]:
    """每章 PPT：封面 + 知识点 1~N 页 + 章节回顾 + 练习 + 课堂提问 + 末页。"""
    members = _members_of(chapter, nodes)
    points = [n for n in members if n.node_key != chapter.node_key] or [chapter]
    lecture_ids: list[str] = []
    chart_ids: list[str] = []
    failures: list[dict] = []
    media_count = 0
    out_dir = Path(figures_dir) if figures_dir else (get_settings().ppt_work_dir.resolve() / FIGURES_SUBDIR)
    figure_style = dict(deck_palette(theme).get("figure") or {})

    figures = [f for node in members for f in _figures_of(_items_for(db, course.id, node.id))]
    cover_bullets = [f"学习目标：{chapter.objective or '掌握本章核心内容'}"]
    if figures:
        cover_bullets.append(f"本章配图 {len(figures)} 张（按知识点嵌入，未成功的在课件诊断里列出）")
    cover_bullets += _cover_meta_bullets()
    cover_notes = "；".join(x for x in (chapter.objective, chapter.teaching_strategy,
                                      chapter.assessment) if x)
    slides: list[dict] = [{
        "layout": LAYOUT_COVER,
        "title": f"{chapter.node_key} {chapter.title}",
        "subtitle": "本章课件",
        "bullets": cover_bullets,
        # 任务 144：本章封面的数据条口径（知识点数 = 本章要讲的知识点节点数）
        "cover_facts": {"points": len(points)},
        "notes": _speak_script(chapter.title, bullets=cover_bullets,
                               paragraphs=cover_notes, kind=LAYOUT_COVER),
    }]

    for node in points:
        items = _items_for(db, course.id, node.id)
        lecture = _lecture_of(items)
        if lecture is not None:
            lecture_ids.append(lecture.id)
        node_figures = _figures_of(items)
        chart_ids += [f.id for f in node_figures]
        # 讲义正文引用到的图优先嵌入（引用原文绝不留成页面文字，任务 35）
        markdown = str((lecture.body or {}).get("markdown") or "") if lecture else ""
        ordered, missing_refs = _order_by_refs(db, course.id, node_figures, markdown)
        # 每页最多 2 张图，页数上限决定该知识点最多渲染几张（其余只在备注里列出）
        per_point_limit = max(1, min(3, int(slides_per_point or 3)))
        cap = per_point_limit * MAX_FIGURES_PER_SLIDE
        overflow_figs = ordered[cap:]
        overflow_reasons = [
            _page_limit_drop_reason(node, len(ordered), cap + k, _figure_label(f),
                                    per_point_limit)
            for k, f in enumerate(overflow_figs, 1)
        ]
        before = len(failures)
        media = _point_media(node, ordered[:cap], out_dir, failures, figure_style, theme)
        media_count += len(media)
        slides += _point_slides(node, items, media, slides_per_point=slides_per_point,
                                failed=list(failures[before:]),
                                overflow=[_figure_label(f) for f in overflow_figs],
                                overflow_reasons=overflow_reasons,
                                missing_refs=missing_refs)

    slides.append(_review_slide(db, course, chapter, members))
    quizzes = list(db.query(QuizItem).filter(
        QuizItem.course_id == course.id,
        QuizItem.node_id.in_([n.id for n in members]),
    ).order_by(QuizItem.created_at).all())
    slides += _quiz_slides(chapter, members, quizzes)   # 任务 108：一页一题
    if get_settings().ppt_include_questions:
        slides.append(_question_slide(chapter, points))
    end_bullets = [f"回顾：{chapter.node_key} {chapter.title}",
                   "课后完成随堂练习并对照备注中的解析。"]
    slides.append({
        "layout": LAYOUT_END,
        "title": END_TITLE,
        "bullets": end_bullets,
        "notes": _speak_script(END_TITLE, bullets=end_bullets[:1], kind=LAYOUT_END),
    })
    source = {
        "outline_rev": (chapter.outline_id or ""),
        "lecture_ids": lecture_ids,
        "chart_ids": chart_ids,
        "figure_count": media_count,
        "figure_failures": failures,
    }
    apply_scenario_notes(slides, course)   # 任务 85：同上（chapter_slides 也吃场景）
    return slides, source


# ---------------------------------------------------------------------------
# 渲染引擎（v4）：主题母版 + 生成前精简 + 生成后几何自检
# ---------------------------------------------------------------------------

TITLE_BOX = (MARGIN_IN, 0.34, SLIDE_WIDTH_IN - MARGIN_IN * 2, 0.95)
COVER_TITLE_BOX = (0.9, 1.95, SLIDE_WIDTH_IN - 1.8, 1.75)
COVER_BODY_BOX = (0.9, 3.85, SLIDE_WIDTH_IN - 1.8, 2.5)
COVER_RULE_TOP = 3.72
TITLE_RULE_TOP = 1.30
# 任务 144：内页标题条 —— 宽从 1.6 英寸加宽到「与正文同宽」（12.23），高沿用 0.05
TITLE_RULE_W_IN = SLIDE_WIDTH_IN - MARGIN_IN * 2
TITLE_RULE_H_IN = 0.05
TITLE_BAND_IN = 1.16
CAPTION_BOX_IN = 0.32


def _rgb(color):
    from pptx.dml.color import RGBColor

    return RGBColor(*color)


def _rgb_of(value):
    from pptx.dml.color import RGBColor

    return RGBColor.from_string(str(value or "000000").lstrip("#").upper())


# ---------------------------------------------------------------------------
# 任务 144：字体显式化 —— 把主题字体写进 pptx 包里每一个文字属性
#
# python-pptx 的 ``run.font.name`` **只写 a:latin**；中文走 ``a:ea``（东亚字体），另有
# ``a:cs``（复杂文种）。⇒ 必须在同一个 ``rPr`` 上同时写三个 typeface，否则中文一个字都
# 不会变（python-pptx 默认模板的 ``a:ea`` 是空串、``a:font script="Hans"`` 是宋体 ——
# 这正是「素 / 模板感」的第一来源）。
#
# 做法：pptx 落盘后**统一改写包内 XML**，而不是在每一处 run 上各写一遍：
#   ① 一处就覆盖**所有**文字：标题 / 正文 / 要点 / 页脚 / 图注 / 表格单元格 / 原生图表 /
#      口播稿（notesSlides）；
#   ② 连没有显式 rPr 的段落（落在 ``a:defRPr`` / ``a:endParaRPr`` 上的）与主题字体方案
#      （``a:majorFont`` / ``a:minorFont``）一起覆盖。
# 不改文字、不改几何、不新增依赖（lxml 本来就是 python-pptx 的依赖）。
# ---------------------------------------------------------------------------
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
FONT_PROP_TAGS = ("rPr", "defRPr", "endParaRPr")
#: CT_TextCharacterProperties 里排在 a:latin / a:ea / a:cs **之后**的元素 ⇒ 新元素插在它们之前
FONT_PROP_AFTER_TAGS = ("sym", "hlinkClick", "hlinkMouseOver", "rtl", "extLst")
#: a:majorFont / a:minorFont 里排在 a:cs 之后的元素（a:font script=… / a:extLst）
FONT_SCHEME_AFTER_TAGS = ("font", "extLst")


def font_triple(font: dict | None) -> tuple[str, str, str]:
    """主题 ``font`` → ``(latin, ea, cs)``；缺项按「同一种字体」补齐（``cs`` 跟随 ``latin``）。"""
    spec = dict(font or {})
    latin = str(spec.get("latin") or "").strip()
    cjk = str(spec.get("cjk") or "").strip()
    if not latin and not cjk:
        return "", "", ""
    latin, cjk = latin or cjk, cjk or latin
    return latin, cjk, str(spec.get("cs") or "").strip() or latin


def _font_props_patch(el, faces: tuple[str, str, str], after_tags: tuple[str, ...]) -> int:
    """给一个文字属性元素写满 ``a:latin`` / ``a:ea`` / ``a:cs``（缺则按 schema 顺序插入）。"""
    from lxml import etree

    changed = 0
    for tag, face in zip(("latin", "ea", "cs"), faces):
        child = el.find(f"{{{A_NS}}}{tag}")
        if child is None:
            child = etree.Element(f"{{{A_NS}}}{tag}")
            index = len(el)
            for pos, node in enumerate(el):
                if etree.QName(node).localname in after_tags:
                    index = pos
                    break
            el.insert(index, child)
            changed += 1
        if child.get("typeface") != face:
            child.set("typeface", face)
            changed += 1
    return changed


def _font_xml_rewrite(data: bytes, faces: tuple[str, str, str]) -> tuple[bytes | None, dict]:
    """改写一份 XML 里所有文字属性；一个都没命中就返回 ``(None, 计数)``（原字节不动）。"""
    from lxml import etree

    root = etree.fromstring(data)
    counts = {tag: 0 for tag in FONT_PROP_TAGS}
    counts["fontScheme"] = 0
    changed = 0
    # python-pptx 只给「设过字号 / 颜色」的 run 建 rPr（口播稿这类只赋 text 的 run 没有）
    # ⇒ 先给这些裸 run 补一个 rPr，字体才真正落到每一个 run 上（a:r 的 schema：rPr 在 t 之前）
    for tag in ("r", "fld"):
        for el in root.iter(f"{{{A_NS}}}{tag}"):
            parent = el.getparent()
            if parent is None or etree.QName(parent).localname != "p":
                continue
            if el.find(f"{{{A_NS}}}rPr") is not None:
                continue
            rpr = etree.Element(f"{{{A_NS}}}rPr")
            el.insert(0, rpr)
            counts["bareRuns"] = counts.get("bareRuns", 0) + 1
            changed += _font_props_patch(rpr, faces, FONT_PROP_AFTER_TAGS)
    for tag in FONT_PROP_TAGS:
        for el in root.iter(f"{{{A_NS}}}{tag}"):
            counts[tag] += 1
            changed += _font_props_patch(el, faces, FONT_PROP_AFTER_TAGS)
    for tag in ("majorFont", "minorFont"):
        for el in root.iter(f"{{{A_NS}}}{tag}"):
            counts["fontScheme"] += 1
            changed += _font_props_patch(el, faces, FONT_SCHEME_AFTER_TAGS)
            # 主题字体方案的「Hans 脚本字体」默认是宋体 —— 一并换成主题的 CJK 字体
            for node in el.findall(f"{{{A_NS}}}font"):
                if (node.get("script") or "") == "Hans" and node.get("typeface") != faces[1]:
                    node.set("typeface", faces[1])
                    changed += 1
    if not changed:
        return None, counts
    counts["changed"] = changed
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True), counts


def apply_deck_fonts(pptx_path: str | Path, font: dict | None) -> dict:
    """把主题字体写进 pptx **包内每个 XML** 的 ``a:latin`` / ``a:ea`` / ``a:cs``。

    只改字体属性：文字、几何、备注一个字节都不动（落盘后整包改写）。返回统计 + ``error``；
    ``error`` 非空表示这次没写成（调用方记进 ``meta.layout_notes``，绝不静默）。
    """
    import zipfile

    faces = font_triple(font)
    stats: dict = {"latin": faces[0], "cjk": faces[1], "cs": faces[2], "parts": 0,
                   "rPr": 0, "defRPr": 0, "endParaRPr": 0, "fontScheme": 0,
                   "changed": 0, "error": None}
    if not faces[0]:
        stats["error"] = "主题没有配置 font（latin / cjk 都为空），字体仍由模板决定"
        return stats
    src = Path(pptx_path)
    tmp = src.with_name(src.name + ".fonttmp")
    try:
        with zipfile.ZipFile(src) as zin:
            infos = zin.infolist()
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
                for info in infos:
                    data = zin.read(info.filename)
                    if info.filename.endswith(".xml"):
                        new_data, counts = _font_xml_rewrite(data, faces)
                        if new_data is not None:
                            data = new_data
                            stats["parts"] += 1
                            stats["changed"] += counts["changed"]
                            for key in ("rPr", "defRPr", "endParaRPr", "fontScheme"):
                                stats[key] += counts[key]
                    zout.writestr(info, data)
    except Exception as exc:  # noqa: BLE001 —— 字体没写进去只影响观感，但必须显式上报
        stats["error"] = f"{type(exc).__name__}: {exc}"
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return stats
    os.replace(tmp, src)
    return stats


def _set_slide_bg(slide, bg: str) -> None:
    """主题背景色（部分主题是暖色纸感）；设置失败只影响观感。"""
    try:
        fill = slide.background.fill
        fill.solid()
        fill.fore_color.rgb = _rgb_of(bg)
    except Exception:  # noqa: BLE001
        pass


def _add_rect(slide, left: float, top: float, width: float, height: float, color: str, *,
              round_corner: bool = False):
    """装饰性色块（标题条 / 卡片 / 徽章）：无文字，几何自检自动忽略。"""
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    corner = MSO_SHAPE.ROUNDED_RECTANGLE if round_corner else MSO_SHAPE.RECTANGLE
    shape = slide.shapes.add_shape(corner, Inches(left), Inches(top),
                                   Inches(width), Inches(height))
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb_of(color)
    shape.line.fill.background()
    if round_corner:
        try:  # 圆角半径收到「小圆角」（默认 0.1667 在大卡片上会变成胶囊）
            shape.adjustments[0] = min(0.25, 0.10 / max(0.2, min(width, height)))
        except (IndexError, ValueError):
            pass
    try:
        shape.shadow.inherit = False
    except (AttributeError, ValueError):
        pass
    return shape


def _send_to_back(slide, shape) -> None:
    """把色块挪到形状树最底层（占位符由 add_slide 先建，后加的色块会盖住标题）。"""
    tree = slide.shapes._spTree  # noqa: SLF001 —— 只有它能调整 z 序
    tree.remove(shape._element)
    tree.insert(2, shape._element)


def _add_title_band(slide, palette: dict) -> None:
    """深色标题条（「专业培训」主题的母版特征）：置底，绝不盖住标题。"""
    shape = _add_rect(slide, 0.0, 0.0, SLIDE_WIDTH_IN, TITLE_BAND_IN, palette["panel"])
    try:
        _send_to_back(slide, shape)
    except Exception:  # noqa: BLE001 —— 置底失败就干脆不画，标题可读优先
        shape._element.getparent().remove(shape._element)


def _add_watermark(slide, text: str) -> None:
    """可选水印：整页旋转浅色文字（命名 cg-watermark，几何自检自动跳过）。"""
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches, Pt

    if not str(text or "").strip():
        return
    box = slide.shapes.add_textbox(Inches(1.4), Inches(2.6),
                                   Inches(SLIDE_WIDTH_IN - 2.8), Inches(1.8))
    box.name = "cg-watermark"
    try:
        box.rotation = -24
    except (AttributeError, ValueError):
        pass
    frame = box.text_frame
    frame.word_wrap = False
    para = frame.paragraphs[0]
    para.alignment = PP_ALIGN.CENTER
    para.text = str(text)
    for run in para.runs:
        run.font.size = Pt(46)
        run.font.bold = True
        run.font.color.rgb = _rgb((0xE2, 0xE8, 0xF0))


def _set_title(placeholder, spec: dict, left: float, top: float, width: float,
               height: float, size: float, color: str, *, align: str | None = None) -> None:
    """标题占位符：位置 / 字号 / 颜色全部由主题决定（深色主题为白字）。"""
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches, Pt

    placeholder.left, placeholder.top = Inches(left), Inches(top)
    placeholder.width, placeholder.height = Inches(width), Inches(height)
    text_frame = placeholder.text_frame
    text_frame.word_wrap = True
    text_frame.text = str(spec.get("title") or "")
    for para in text_frame.paragraphs:
        if align == "center":
            para.alignment = PP_ALIGN.CENTER
        for run in para.runs:
            run.font.size = Pt(size)
            run.font.bold = True
            run.font.color.rgb = _rgb_of(color)


def _set_body(placeholder, lines: list[str], left: float, top: float, width: float,
              height: float, *, size: float, align: str | None = None,
              color: str | None = None) -> None:
    """正文占位符：字号 / 颜色来自主题，段后距与几何自检的估算模型保持一致。"""
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches, Pt

    placeholder.left, placeholder.top = Inches(left), Inches(top)
    placeholder.width, placeholder.height = Inches(width), Inches(height)
    text_frame = placeholder.text_frame
    text_frame.word_wrap = True
    text_frame.clear()
    for idx, line in enumerate(lines):
        para = text_frame.paragraphs[0] if idx == 0 else text_frame.add_paragraph()
        para.text = str(line)
        para.space_after = Pt(0 if idx == len(lines) - 1 else SPACE_AFTER_PT)
        if align == "center":
            para.alignment = PP_ALIGN.CENTER
        for run in para.runs:
            run.font.size = Pt(size)
            if color:
                run.font.color.rgb = _rgb_of(color)


def _bring_to_front(shape) -> None:
    """把形状挪到形状树末尾（最上层）。

    任务 146：知识点页的卡片是**后画**的，正文写进的是建页时就存在的占位符 ⇒
    卡片会把正文整个盖住（用户看到的「空白页 / 只有一句话」）。写完正文后把占位符
    提到最上层，文字才可见。
    """
    element = getattr(shape, "_element", None)
    tree = element.getparent() if element is not None else None
    if tree is None:
        return
    tree.remove(element)
    tree.append(element)


def _add_footer(slide, palette: dict, course_title: str, chapter_label: str,
                page_no: int) -> None:
    """页脚：<课程名> · <章号 章名> · P<页码>；PPT_SHOW_FOOTER=false 时整条不画。"""
    from pptx.util import Inches, Pt

    if not get_settings().ppt_show_footer:
        return
    if palette.get("footer_style") == "band":
        _add_rect(slide, 0.0, SLIDE_HEIGHT_IN - 0.62, SLIDE_WIDTH_IN, 0.015, palette["rule"])
    parts = [str(course_title or ""), str(chapter_label or ""), f"P{int(page_no)}"]
    box = slide.shapes.add_textbox(Inches(MARGIN_IN), Inches(SLIDE_HEIGHT_IN - 0.52),
                                   Inches(SLIDE_WIDTH_IN - MARGIN_IN * 2), Inches(0.32))
    para = box.text_frame.paragraphs[0]
    para.text = " · ".join(p for p in parts if p)
    for run in para.runs:
        run.font.size = Pt(palette["sizes"]["footer"])
        run.font.color.rgb = _rgb_of(palette["footer"])


def _picture_size(path: str, box_w: float, box_h: float) -> tuple[float, float]:
    """等比缩放到给定框内（不拉伸）。"""
    from PIL import Image

    try:
        with Image.open(path) as image:
            img_w, img_h = image.size
    except Exception:  # noqa: BLE001 —— 尺寸读不到就按 16:9 估
        img_w, img_h = 16, 9
    if img_w <= 0 or img_h <= 0:
        img_w, img_h = 16, 9
    ratio = img_w / img_h
    if ratio >= box_w / max(box_h, 0.1):
        return box_w, box_w / ratio
    return box_h * ratio, box_h


def _media_title(entry: dict) -> str:
    return str(entry.get("title") or entry.get("caption") or "图")


def _one_line(text: str, width_in: float, pt: float) -> str:
    """把文字裁成「在 ``width_in`` 宽、``pt`` 字号下一行放得下」（页首行 / 图注共用）。"""
    capacity = max(8, int(width_in * 72.0 / max(1.0, float(pt)) * 0.9))
    return clip_chars(str(text), capacity)[0]


def _add_figure_caption_line(slide, media: list[dict], *, left: float, top: float, width: float,
                             height: float, size: float, color: str):
    """纯图页页首的图注行（与页标题同一行、右对齐）。

    纯图页不画图注条（``caption_in=0``，图因此吃满页高）⇒「图 N · 题名」必须改由这一行
    承担，否则观众看不到「这张图叫什么」（pptx 里这条不许消失）。
    """
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches, Pt

    text = _one_line("；".join(f"图 {i + 1} · {_media_title(entry)}"
                               for i, entry in enumerate(media)), width, size)
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    box.text_frame.word_wrap = True
    para = box.text_frame.paragraphs[0]
    para.alignment = PP_ALIGN.RIGHT
    para.text = text
    for run in para.runs:
        run.font.size = Pt(size)
        run.font.color.rgb = _rgb_of(color)
    return box


def _add_image(slide, media: dict, left: float, top: float, width: float, height: float,
               palette: dict, *, caption: str, caption_in: float | None = None) -> None:
    """概念图 / 配图：等比缩放（不拉伸）+「图 N · 图题」图注。

    ``caption_in`` = 图注条占的高度（英寸）。``None`` 按 :func:`media_caption_in` 算
    （与选档 / 报数同一把尺子）；``0`` 表示本页不给图注留位置（纯图页把图注挪到页首
    当页标题），此时图吃满整个框、也不画图注。
    """
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches, Pt

    caption_h = media_caption_in(height) if caption_in is None else max(0.0, float(caption_in))
    pic_w, pic_h = _picture_size(str(media["path"]), width, max(0.6, height - caption_h))
    pic_x = left + (width - pic_w) / 2
    slide.shapes.add_picture(str(media["path"]), Inches(pic_x), Inches(top),
                             width=Inches(pic_w), height=Inches(pic_h))
    if caption_h <= 0 or not caption:
        return
    box = slide.shapes.add_textbox(Inches(left), Inches(top + pic_h + 0.02),
                                   Inches(width), Inches(CAPTION_BOX_IN))
    box.text_frame.word_wrap = True
    para = box.text_frame.paragraphs[0]
    para.alignment = PP_ALIGN.CENTER
    para.text = str(caption)
    for run in para.runs:
        run.font.size = Pt(palette["sizes"]["caption"])
        run.font.color.rgb = _rgb_of(palette["caption"])


def _color_chart(chart, media: dict, palette: dict) -> None:
    """原生图表配色：与文字、概念图 PNG 共用同一套主题色板。"""
    from pptx.dml.color import RGBColor

    colors = list(palette.get("colors") or [])
    if not colors:
        return
    try:
        plot = chart.plots[0]
        if media.get("kind") == "chart_pie":
            for idx, point in enumerate(plot.points):
                point.format.fill.solid()
                point.format.fill.fore_color.rgb = RGBColor.from_string(colors[idx % len(colors)])
        elif media.get("kind") == "chart_line":
            for idx, series in enumerate(plot.series):
                series.format.line.color.rgb = RGBColor.from_string(colors[idx % len(colors)])
        else:
            for idx, series in enumerate(plot.series):
                series.format.fill.solid()
                series.format.fill.fore_color.rgb = RGBColor.from_string(colors[idx % len(colors)])
    except Exception:  # noqa: BLE001 —— 配色失败只影响观感
        pass


def _add_chart(slide, media: dict, left: float, top: float, width: float, height: float,
               palette: dict, *, caption: str) -> None:
    """数据图：python-pptx 原生图表（可编辑、可改数）+「图 N · 图题」图注。"""
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches, Pt

    chart_types = {
        "chart_bar": XL_CHART_TYPE.COLUMN_CLUSTERED,
        "chart_line": XL_CHART_TYPE.LINE_MARKERS,
        "chart_pie": XL_CHART_TYPE.PIE,
    }
    chart_data = CategoryChartData()
    chart_data.categories = list(media.get("labels") or [])
    for series in (media.get("datasets") or [])[:6]:
        chart_data.add_series(str(series.get("label") or "系列"), tuple(series.get("data") or []))
    chart_h = max(1.2, height - CAPTION_BOX_IN)
    frame = slide.shapes.add_chart(chart_types[media["kind"]], Inches(left), Inches(top),
                                   Inches(width), Inches(chart_h), chart_data)
    chart = frame.chart
    chart.has_title = True
    chart.chart_title.text_frame.text = str(media.get("title") or "")
    for run in chart.chart_title.text_frame.paragraphs[0].runs:
        run.font.size = Pt(palette["sizes"]["caption"])
        run.font.color.rgb = _rgb_of(palette["muted"])
    _color_chart(chart, media, palette)
    box = slide.shapes.add_textbox(Inches(left), Inches(top + chart_h + 0.02),
                                   Inches(width), Inches(CAPTION_BOX_IN))
    para = box.text_frame.paragraphs[0]
    para.text = str(caption)
    for run in para.runs:
        run.font.size = Pt(palette["sizes"]["caption"])
        run.font.color.rgb = _rgb_of(palette["caption"])


def _add_table(slide, media: dict, left: float, top: float, width: float, height: float,
               palette: dict) -> None:
    """数据表：python-pptx 原生表格（表头用主题主色）。"""
    from pptx.dml.color import RGBColor
    from pptx.util import Inches, Pt

    headers = [str(h) for h in (media.get("headers") or [])][:6]
    rows = [[str(cell) for cell in row] for row in (media.get("rows") or [])][:8]
    shape = slide.shapes.add_table(len(rows) + 1, max(1, len(headers)), Inches(left), Inches(top),
                                   Inches(width), Inches(height))
    table = shape.table
    for col, text in enumerate(headers):
        table.cell(0, col).text = text
    for row_idx, row in enumerate(rows, 1):
        for col in range(len(headers)):
            table.cell(row_idx, col).text = row[col] if col < len(row) else ""
    accent = RGBColor.from_string(str(palette["accent"])) if palette.get("accent") else None
    for row_idx in range(len(rows) + 1):
        for col in range(max(1, len(headers))):
            for para in table.cell(row_idx, col).text_frame.paragraphs:
                for run in para.runs:
                    run.font.size = Pt(palette["sizes"]["caption"])
                    if row_idx == 0:
                        run.font.bold = True
                        if accent is not None:
                            run.font.color.rgb = accent


def media_draw_columns(layout: str | None, count: int) -> int:
    """该页渲染时会分几列：纯图页整行并排，其余竖排一列（与 :func:`_build_prs` 同口径）。"""
    return count if layout == LAYOUT_FIGURE else 1


def media_cell_boxes(box: tuple[float, float, float, float], count: int, *,
                     columns: int = 1) -> list[tuple[float, float, float, float]]:
    """把图框切成 ``count`` 个格子（与 :func:`_place_media` 同一把尺子）。

    一页有原生图表 / 表格时，位图要和它们**平分**图框；规划侧（选档 / 拆图 / 报数）必须
    按自己那一格算 —— 按整幅报出来的磅数会比真产物高一截（任务 110 实测 13 处）。
    """
    if count < 1 or not box:
        return []
    columns = max(1, min(int(columns or 1), count))
    rows = math.ceil(count / columns)
    gap = MEDIA_STACK_GAP_IN
    cell_w = (float(box[2]) - gap * (columns - 1)) / columns
    cell_h = (float(box[3]) - gap * (rows - 1)) / rows
    cells: list[tuple[float, float, float, float]] = []
    for idx in range(count):
        row, col = divmod(idx, columns)
        cells.append((float(box[0]) + col * (cell_w + gap),
                      float(box[1]) + row * (cell_h + gap), cell_w, cell_h))
    return cells


def _place_media(slide, media: list[dict], left: float, top: float, width: float,
                 height: float, palette: dict, *, columns: int = 1,
                 caption_in: float | None = None) -> None:
    """排布图（等比）/ 原生图表 / 原生表格：默认竖排一列，纯图页可整行并排。

    ``caption_in`` 透传给 :func:`_add_image`（``0`` = 不给图注留位置，纯图页用）；
    切格子走 :func:`media_cell_boxes` —— 规划侧用的是同一把尺子。
    """
    count = len(media)
    if count == 0:
        return
    cells = media_cell_boxes((left, top, width, height), count, columns=columns)
    for idx, (entry, (x, y, cell_w, cell_h)) in enumerate(zip(media, cells)):
        caption = "" if caption_in == 0 else f"图 {idx + 1} · {_media_title(entry)}"
        kind = str(entry.get("type"))
        if kind == "image":
            _add_image(slide, entry, x, y, cell_w, cell_h, palette, caption=caption,
                       caption_in=caption_in)
        elif kind == "chart":
            _add_chart(slide, entry, x, y, cell_w, cell_h, palette, caption=caption)
        else:
            _add_table(slide, entry, x, y, cell_w, cell_h, palette)


def _layout_of(spec: dict) -> str:
    return str(spec.get("layout") or LAYOUT_POINT)


MEDIA_TIER_COLUMN = "column"
MEDIA_TIER_FULL = "full"
MEDIA_TIER_FIGURE = "figure-only"
MEDIA_TIER_LABELS = {MEDIA_TIER_COLUMN: "分栏档", MEDIA_TIER_FULL: "满幅档",
                     MEDIA_TIER_FIGURE: "纯图页"}

# 任务 124：「换档不得让页面上看到的等效磅数变小」的落地阈值。任务 120 实测四档 +
# 白边收紧把图内字号抬了约 9~13%（74 处中位 11.40 → 12.37 磅）⇒ 等效磅数落在
# [MIN_TEXT_PT, MIN_TEXT_PT × 1.15) 的档位只是**被密度增益刚推过 9 磅硬下限**，
# 不算真的够大；此时宁可换到等效磅数更大的档（要点进备注），也不让图变小。
MEDIA_TIER_KEEP_PT = MIN_TEXT_PT * 1.15   # ≈ 10.35 磅（9 磅硬下限 + 15% 余量）


def media_strip_height_in(bullets: list[str], size_pt: float) -> float:
    """满幅档图上方「要点条」需要的高度（与几何自检同一把尺子估折行高度）。"""
    lines = [str(b) for b in bullets if str(b).strip()]
    if not lines:
        return 0.0
    return est_height_in(lines, size_pt, MEDIA_FULL_WIDTH_IN) + 0.10


def media_full_box(bullets: list[str], size_pt: float, *, strip_in: float | None = None
                   ) -> tuple[tuple[float, float, float, float], tuple[float, float, float, float]]:
    """满幅档的 (body_box, media_box)：要点条在上、图在下，两者都是版心宽。

    ``strip_in`` 给了就按它定要点条高（任务 110：图需要多高，条就压到多高）；不给就按
    要点自己的高度算（上限 ``MEDIA_STRIP_MAX_IN``）。
    """
    strip = media_strip_height_in(bullets, size_pt) if strip_in is None else float(strip_in)
    strip = max(0.0, min(MEDIA_STRIP_MAX_IN, strip))
    gap = MEDIA_STRIP_GAP_IN if strip else 0.0
    return ((MARGIN_IN, CONTENT_TOP_IN, MEDIA_FULL_WIDTH_IN, strip),
            (MARGIN_IN, CONTENT_TOP_IN + strip + gap, MEDIA_FULL_WIDTH_IN,
             CONTENT_HEIGHT_IN - strip - gap))


def media_tier_boxes(bullets: list[str], size_pt: float
                     ) -> tuple[tuple[float, float, float, float], tuple[float, float, float, float]]:
    """两档的 (body_box, media_box)：先算分栏档，再按要点条高算满幅档。"""
    column = ((MARGIN_IN, CONTENT_TOP_IN, BODY_COLUMN_IN, CONTENT_HEIGHT_IN),
              (MEDIA_LEFT_IN, CONTENT_TOP_IN, MEDIA_COLUMN_IN, CONTENT_HEIGHT_IN))
    return column, media_full_box(bullets, size_pt)


def _fitted_media_width_in(entry: dict, box: tuple[float, float, float, float]) -> float:
    """该图按框等比缩放（不拉伸）后实际占用的显示宽度（英寸）。"""
    px = list(entry.get("px") or [])
    if len(px) != 2 or float(px[0]) <= 0:
        return 0.0
    return fit_display_width_in(float(px[0]), float(px[1]), box[2], box[3])


def media_caption_in(box_h: float) -> float:
    """图框里「图注条」占掉的高度（英寸）—— 与 :func:`_add_image` 同一把尺子。"""
    return min(CAPTION_BOX_IN, max(0.24, float(box_h) * 0.16))


def media_picture_box(box: tuple[float, float, float, float], *,
                      caption_in: float | None = None) -> tuple[float, float, float, float]:
    """图框里**真正给图片**的框：图注条占掉一截（``_add_image`` 就是按这个缩图的）。

    任务 110：选档 / 报数都按这个框算，不然会把「图注条吃掉的 0.32 英寸」算成图，
    报出来的磅数比真产物高（实测真产物比计划窄 8%）。``caption_in=0`` 表示本页不给
    图注留位置（纯图页），与 :func:`_add_image` 的同一参数一一对应。
    """
    cap = media_caption_in(box[3]) if caption_in is None else max(0.0, float(caption_in))
    return (box[0], box[1], box[2], max(0.6, float(box[3]) - cap))


def _media_pt_in(entry: dict, box: tuple[float, float, float, float], *,
                 caption_in: float | None = None) -> float | None:
    """该图在这个框里「图内最小字号」的等效磅数；测不出字号返回 None（绝不瞎猜）。"""
    px = list(entry.get("px") or [])
    min_px = float((entry.get("legibility") or {}).get("min_px") or 0.0)
    if len(px) != 2 or float(px[0]) <= 0 or min_px <= 0:
        return None
    width = _fitted_media_width_in(entry, media_picture_box(box, caption_in=caption_in))
    return pt_equivalent(min_px, float(px[0]), width)


def media_need_height_in(entry: dict, box_w: float = MEDIA_FULL_WIDTH_IN) -> float | None:
    """这张图放在 ``box_w`` 宽的框里，要让图内最小字号 ≥ 9 磅所需的**框高**（英寸）。

    与 :func:`_media_pt_in` 同一口径反算（``磅 = 字号 ÷ 画布宽 × 显示宽度 × 72``）；
    连 ``box_w`` 宽度都凑不出来就返回 ``None``（说明这张图在这个版面上抬不到下限）。
    反算带 ``MEDIA_PT_MARGIN`` 余量：卡在正好 9.00 磅的图上页后会被取整吃到 8.99。
    """
    px = list(entry.get("px") or [])
    min_px = float((entry.get("legibility") or {}).get("min_px") or 0.0)
    if len(px) != 2 or float(px[0]) <= 0 or float(px[1]) <= 0 or min_px <= 0:
        return None
    need_w = MIN_TEXT_PT * MEDIA_PT_MARGIN * float(px[0]) / (min_px * 72.0)
    if need_w > float(box_w) + 1e-9:
        return None
    height = need_w * float(px[1]) / float(px[0])   # 图片自己要占的高度
    for _ in range(3):                              # 图注条高度与框高互相依赖，迭代收敛
        height = need_w * float(px[1]) / float(px[0]) + media_caption_in(height)
    return height


def _media_ok(entry: dict, box: tuple[float, float, float, float], *,
              caption_in: float | None = None) -> bool:
    """这张图在这个框里「显示宽度 ≥ 下限」且「图内最小字号 ≥ 下限」。"""
    px = list(entry.get("px") or [])
    if len(px) != 2:
        return True     # 原生图表 / 表格随框缩放，字号由 pptx 侧负责（测不出就不判）
    picture = media_picture_box(box, caption_in=caption_in)
    if _fitted_media_width_in(entry, picture) < MIN_MEDIA_WIDTH_IN - 1e-9:
        return False
    pt = _media_pt_in(entry, box, caption_in=caption_in)
    return pt is None or pt >= MIN_TEXT_PT - 1e-9


def media_cells_report(media: list[dict], box: tuple[float, float, float, float], *,
                       columns: int = 1, caption_in: float | None = None
                       ) -> tuple[bool, float, float | None]:
    """按**每张图自己那一格**报数：返回 (是否都达标, 最小显示宽度, 最小等效磅数)。

    一页有原生图表 / 表格时，位图和它们平分图框（见 :func:`media_cell_boxes`）；规划侧按
    整幅报出来的磅数会比真产物高一截（任务 110 实测 13 处正是这么漏掉的）。测不出字号的
    位图照样占宽度，只是不参与磅数比较（绝不瞎猜）。
    """
    cells = media_cell_boxes(box, len(media), columns=columns)
    if not cells:
        return False, 0.0, None
    pairs = [(m, c) for m, c in zip(media, cells) if m.get("px")]
    ok = all(_media_ok(m, cell, caption_in=caption_in) for m, cell in zip(media, cells))
    widths = [_fitted_media_width_in(m, media_picture_box(c, caption_in=caption_in))
              for m, c in pairs]
    pts = [pt for pt in (_media_pt_in(m, c, caption_in=caption_in) for m, c in pairs)
           if pt is not None]
    return ok, (min(widths) if widths else 0.0), (min(pts) if pts else None)


def media_stack_height_in(media: list[dict]) -> float:
    """满幅档竖排这 ``media`` 张图所需的框高：每张反算所需高度之和 + 层间距。"""
    needs = [media_need_height_in(m) for m in media if m.get("px")]
    return sum(n for n in needs if n is not None) + MEDIA_STACK_GAP_IN * max(0, len(media) - 1)


def _pt_text(pt: float | None) -> str:
    """磅数文案：测不出就如实说（不把「测不出」写成 0.00 磅）。"""
    return "测不出" if pt is None else f"{pt:.2f} 磅"


def _legible_tier(width_col: float, pt_col: float | None, col_ok: bool,
                  width_full: float, pt_full: float | None, full_ok: bool,
                  width_fig: float, pt_fig: float | None, fig_ok: bool
                  ) -> tuple[str, float, float] | None:
    """任务 124：在「看得清」的候选档里按**页面上实际看到的等效磅数**挑最大的一档。

    候选只有三条（分栏 / 满幅 / 纯图页），都要求 ``_media_ok`` 已经判过「够宽 + 够大」；
    ``pt`` 测不出（原生图表 / 表格）的档不参与比较，绝不瞎猜。磅数并列（公差 1e-9）时
    保留先出现的档（分栏 → 满幅 → 纯图），也就是既有口径的「优先保住要点」。
    返回 ``(档位, 显示宽度, 等效磅数)``；一个看得清的档都没有时返回 ``None``。
    """
    rows = ((MEDIA_TIER_COLUMN, col_ok, width_col, pt_col),
            (MEDIA_TIER_FULL, full_ok, width_full, pt_full),
            (MEDIA_TIER_FIGURE, fig_ok, width_fig, pt_fig))
    best: tuple[str, float, float] | None = None
    for tier, ok, width, pt in rows:
        if not ok or pt is None:
            continue
        if best is None or pt > best[2] + 1e-9:
            best = (tier, width, pt)
    return best


def media_tier_of(spec: dict, size_pt: float) -> tuple[str, str]:
    """选「分栏档 / 满幅档 / 纯图页」：先看**能不能看清**（宽度下限 + 等效磅数下限），
    都看得清再取显示宽度更大的那一档（沿用 107 的口径）。

    任务 110 起「看得清」是硬判据：窄于 ``MIN_MEDIA_WIDTH_IN`` 或图内最小字号低于
    ``MIN_TEXT_PT`` 的档位直接淘汰；满幅档现按「图需要多少高度」反推要点条能有多高
    （放不下的要点移入备注，页面几何因此保证图够大）。报数一律按**每张图自己那一格**
    （一页有原生图表 / 表格时位图和它们平分图框，见 :func:`media_cells_report`）。
    判据只用几何与像素尺寸，确定、可测（无随机、无时间、无 LLM）。
    任务 124 追加硬规则：**换档后页面上看到的等效磅数不得小于换档前** —— 在看得清的
    候选档里取等效磅数最大的一档，旧口径想换到更小的一档时放弃换档（见
    :func:`_legible_tier`），并把「本想换成 X 档……放弃换档」写进 layout_notes。

    任务 146b A1 的优先级：**不许丢图 > 不许把图压小**。本页带要点时纯图页会把要点
    全挤进备注（146 禁止 0 要点图页），随后 ``_fit_all`` 只能把图挪进备注 ⇒ 图消失。
    因此「换成纯图页」这条路在有要点时一律不走：只要分栏 / 满幅仍过硬下限
    （宽 ≥ 6.00 英寸、字号 ≥ 9 磅），就接受字号变小、把图留在页上；124 的「不许为了
    塞要点把图压小」仍然成立，但前提是**图还能在页上**。
    """
    media = list(spec.get("media") or [])
    if not any(m.get("px") for m in media):
        return MEDIA_TIER_COLUMN, "图没有像素尺寸（原生图表 / 表格随框缩放）⇒ 分栏档"
    bullets = [str(b) for b in (spec.get("bullets") or []) if str(b).strip()]
    (_col_body, col_media), (_full_body, _full_media) = media_tier_boxes(bullets, size_pt)
    # 报数口径 = 真上页的**图片**宽度（扣掉图注条、扣掉同页别的图占的格子），与
    # media_legibility_notes / 验收同一把尺子
    col_ok, width_col, pt_col = media_cells_report(media, col_media)
    need = media_stack_height_in(media)
    strip_max = CONTENT_HEIGHT_IN - MEDIA_STRIP_GAP_IN - need
    strip = max(0.0, min(MEDIA_STRIP_MAX_IN, strip_max,
                         media_strip_height_in(bullets, size_pt)))
    _strip_body, strip_media = media_full_box(bullets, size_pt, strip_in=strip)
    # 要点条至少要能完整放下 1 条要点（否则 fit_spec 仍会留下第一条 ⇒ 文本溢出 ⇒ 几何自检
    # 不过 ⇒ 反而把图缩回去）；连 1 条都放不下就走「纯图页」，要点进备注。
    min_strip = media_strip_height_in(bullets[:1], size_pt)
    full_ok, width_full, pt_full = media_cells_report(media, strip_media)
    full_ok = bool(full_ok and strip >= min_strip - 1e-9)
    # 纯图页：整页给图（本页不画标题条、图注当页标题 ⇒ 图框里没有图注条）
    fig_ok, width_fig, pt_fig = media_cells_report(media, MEDIA_FIGURE_BOX,
                                                  columns=len(media),
                                                  caption_in=FIGURE_CAPTION_IN)
    reason_col = (f"图在右栏显示 {width_col:.2f} 英寸（≥ {MIN_MEDIA_WIDTH_IN:.2f}）、"
                  f"图内最小字号 {_pt_text(pt_col)}（≥ {MIN_TEXT_PT:.0f} 磅）")
    reason_full = (f"图显示 {width_full:.2f} 英寸 > 分栏档 {width_col:.2f} 英寸，"
                   f"要点条压到 {strip:.2f} 英寸（本页图要 ≥ {need:.2f} 英寸高才够 {MIN_TEXT_PT:.0f} 磅）")
    if strip < media_strip_height_in(bullets, size_pt) - 1e-9:
        reason_full += "；放不下的要点移入备注"
    reason_fig = (f"满幅档也放不下（本页图要 {need:.2f} 英寸高才够 {MIN_TEXT_PT:.0f} 磅，"
                  f"可用 {CONTENT_HEIGHT_IN:.2f} 英寸）⇒ 整页给图（图 {width_fig:.2f} 英寸、"
                  f"图内最小字号 {_pt_text(pt_fig)}{'，达标' if fig_ok else '，仍不达标'}）"
                  f"；本页不画标题条、图注当页标题，要点进备注")
    if need <= 0.0:
        reason_fig = (f"图内字号在这个版面上凑不到 {MIN_TEXT_PT:.0f} 磅"
                      f"（显示宽度上限 {MEDIA_FULL_WIDTH_IN:.2f} 英寸）⇒ 整页给图"
                      f"（图 {width_fig:.2f} 英寸、图内最小字号 {_pt_text(pt_fig)}）；要点进备注")
    # —— 旧口径的选择（任务 107 / 110：宽度优先 + 能放先放要点），只用来写「本想换档」的说明 ——
    if col_ok and (not full_ok or width_col >= width_full - 0.01):
        old_tier, old_reason = MEDIA_TIER_COLUMN, reason_col
    elif full_ok:
        old_tier, old_reason = MEDIA_TIER_FULL, reason_full
    else:
        old_tier, old_reason = MEDIA_TIER_FIGURE, reason_fig
    old_width, old_pt = {MEDIA_TIER_COLUMN: (width_col, pt_col),
                         MEDIA_TIER_FULL: (width_full, pt_full),
                         MEDIA_TIER_FIGURE: (width_fig, pt_fig)}[old_tier]
    # —— 任务 124 硬规则：换档后页面上看到的等效磅数不得小于换档前 ——
    # 旧口径想换成更窄的档时，只要原档的等效磅数还够大（≥ MEDIA_TIER_KEEP_PT），就
    # **放弃换档、保持原档**（宁可保住要点，也不把图压小）；只有原档本身就贴着 9 磅
    # 硬下限（任务 120 的密度增益刚把它推过线）时才真的换到等效磅数更大的档，并把
    # 「本想换成 X 档……放弃换档」写进 layout_notes。磅数并列（公差 1e-9）时保留先
    # 出现的档（分栏 → 满幅 → 纯图），磅数一样时仍然优先保住要点。
    best = _legible_tier(width_col, pt_col, col_ok, width_full, pt_full, full_ok,
                         width_fig, pt_fig, fig_ok)
    reasons = {MEDIA_TIER_COLUMN: reason_col, MEDIA_TIER_FULL: reason_full,
               MEDIA_TIER_FIGURE: reason_fig}
    if best is None or best[0] == old_tier:
        return old_tier, old_reason
    # 任务 146b A1：带要点时绝不换纯图页 —— 换了也留不住（_fit_all 只能把图挪进备注），
    # 等于「为了不压小图」把整张图丢掉。分栏 / 满幅已达硬下限 ⇒ 接受字号变小，图留在页上。
    if bullets and best[0] == MEDIA_TIER_FIGURE and old_tier != MEDIA_TIER_FIGURE:
        note = (f"为了保住图，接受图内字号从 {_pt_text(pt_fig)} 降到 {_pt_text(old_pt)}："
                f"纯图页会把本页 {len(bullets)} 条要点挤进备注（146 不许 0 要点图页），"
                f"图会被挪进备注；{MEDIA_TIER_LABELS[old_tier]}的图 {old_width:.2f} 英寸"
                f" ≥ {MIN_MEDIA_WIDTH_IN:.2f}、字号 {_pt_text(old_pt)}"
                f" ≥ {MIN_TEXT_PT:.0f} 磅 ⇒ 图与要点同页")
        return old_tier, f"{old_reason}；{note}"
    if old_pt is None or old_pt >= MEDIA_TIER_KEEP_PT:
        return old_tier, old_reason
    tier, width, pt = best
    note = (f"本想换成{MEDIA_TIER_LABELS[old_tier]}（图 {old_width:.2f} 英寸 / {_pt_text(old_pt)}），"
            f"但换档会让图内字号从 {_pt_text(pt)}降到 {_pt_text(old_pt)}"
            f"（原档贴着 {MIN_TEXT_PT:.0f} 磅硬下限，只高出 "
            f"{MEDIA_TIER_KEEP_PT / MIN_TEXT_PT - 1:.0%} 以内）⇒ 放弃换档，"
            f"保持{MEDIA_TIER_LABELS[tier]}（图 {width:.2f} 英寸）")
    return tier, f"{reasons[tier]}；{note}"


def _criterion_figure_keep_plan(spec: dict, size_pt: float) -> tuple[str, str] | None:
    """任务 146：判据生成的图不因「换更大图」而把本页要点全挤进备注。

    ``media_tier_of`` 的任务 124 规则会在「分栏档贴着 9 磅硬下限（< 9×1.15）」时
    换到纯图页，让图更大；但纯图页会把本页要点全部挪进备注，和 146 A2/A4 冲突。
    这里只对带 ``figure_choice`` 的判据图做保底：只要分栏 / 满幅仍达标
    （宽 ≥ 6.00 英寸、图内最小字号 ≥ 9 磅），就保留该档，图与要点同页。
    阈值一个都不改，非判据图的既有选择也不动。
    """
    media = [m for m in (spec.get("media") or []) if isinstance(m, dict)]
    bullets = [str(b) for b in (spec.get("bullets") or []) if str(b).strip()]
    if not media or not bullets or not any(m.get("figure_choice") for m in media):
        return None
    col_media = media_tier_boxes(bullets, size_pt)[0][1]
    col_ok, width_col, pt_col = media_cells_report(media, col_media)
    if col_ok:
        return (MEDIA_TIER_COLUMN,
                f"判据图保留在分栏档（图 {width_col:.2f} 英寸、图内最小字号 "
                f"{_pt_text(pt_col)}，均达下限）⇒ 图与要点同页，本页要点不挪备注")
    strip = min(MEDIA_STRIP_MAX_IN, media_strip_height_in(bullets, size_pt))
    if strip < media_strip_height_in(bullets[:1], size_pt) - 1e-9:
        return None
    _body, full_media = media_full_box(bullets, size_pt, strip_in=strip)
    if full_media[3] <= 0.6:
        return None
    full_ok, width_full, pt_full = media_cells_report(media, full_media)
    if full_ok:
        return (MEDIA_TIER_FULL,
                f"判据图保留在满幅档（图 {width_full:.2f} 英寸、图内最小字号 "
                f"{_pt_text(pt_full)}），要点条压到 {strip:.2f} 英寸仍放得下本页要点 ⇒ "
                f"要点不挪备注")
    return None


def _figure_bullet_lines(bullets: list[str], title: str, *, limit: int = 3) -> int:
    """图页底部要点条要放几条：至少 1；标题 + 已截断要点不足 30 字就加到够 30。

    任务 146b A3 的底线是「至少 1 条 + 正文 >= 12 字」，但验收表还要求
    「有图但正文 < 30 字的页 = 0」。图页标题就是图注，短图注（如「引用要素」）
    配一条 22 字要点会低于 30 字 ⇒ 这里按渲染后的截断文本补第 2 条，最多 3 条；
    条高由 ``FIGURE_BULLET_STRIP_IN * N`` 让出，图仍须过 6.00 英寸 / 9 磅硬门槛。
    """
    lines = [str(b) for b in bullets if str(b).strip()]
    if not lines:
        return 0
    chars = len(str(title or ""))
    # 图页底部条是整幅宽度（≈12.23 英寸），单条放到 30 字仍是一行；22 字的知识点
    # 上限在这里会把「短图注 + 一条要点」压到 30 字以下，因此图页放宽到 30 字。
    chars_per_line = max(point_chars(LAYOUT_FIGURE), PAGE_MIN_TEXT_CHARS)
    count = 0
    for line in lines[:max(1, int(limit or 1))]:
        clipped, _cut = clip_chars(line, chars_per_line)
        chars += len(clipped)
        count += 1
        if chars >= PAGE_MIN_TEXT_CHARS:
            break
    return max(1, count)


def page_media_plan(spec: dict, sizes: dict, *,
                    keep_criterion_figure: bool = False) -> dict:
    """一页的图档位 + 正文框 + 图框：生成前精简（_fit_all）与生成时渲染共用同一份规划。"""
    layout = _layout_of(spec)
    size_pt = float(sizes.get("body") or 18.0)
    media = list(spec.get("media") or [])
    if layout == LAYOUT_COVER:
        return {"tier": None, "reason": "封面页不放图",
                "body_box": COVER_BODY_BOX, "media_box": None, "caption_in": None}
    if layout == LAYOUT_FIGURE:
        count = len(media)
        bullets = [str(b) for b in (spec.get("bullets") or []) if str(b).strip()]
        bullet_lines = _figure_bullet_lines(bullets, str(spec.get("title") or ""))
        strip = FIGURE_BULLET_STRIP_IN * bullet_lines if (media and bullets) else 0.0
        media_h = MEDIA_FIGURE_BOX[3] - (strip + MEDIA_STRIP_GAP_IN if strip else 0.0)
        box = (MEDIA_FIGURE_BOX[0], MEDIA_FIGURE_BOX[1], MEDIA_FIGURE_BOX[2], media_h)
        bullet_box = ((MARGIN_IN, MEDIA_FIGURE_BOX[1] + media_h + MEDIA_STRIP_GAP_IN,
                       MEDIA_FULL_WIDTH_IN, strip) if strip else None)
        why = ("本页只有一张图：图占整页、页首一行图注" if count <= 1
               else f"本页有 {count} 张图：并排占整页、页首一行图注")
        if strip:
            why += f"；底部留 {strip:.2f} 英寸给 {bullet_lines} 条要点"
        return {"tier": MEDIA_TIER_FIGURE, "reason": why,
                "body_box": (0.0, 0.0, 0.0, 0.0),
                "media_box": box, "caption_in": FIGURE_CAPTION_IN,
                "bullet_strip_in": strip, "bullet_box": bullet_box,
                "bullet_lines": bullet_lines, "layout": LAYOUT_FIGURE}
    if not media:
        return {"tier": None, "reason": "",
                "body_box": (MARGIN_IN, CONTENT_TOP_IN, SLIDE_WIDTH_IN - MARGIN_IN * 2,
                             CONTENT_HEIGHT_IN), "media_box": None, "caption_in": None,
                "layout": None}
    bullets = [str(b) for b in (spec.get("bullets") or []) if str(b).strip()]
    tier, reason = media_tier_of(spec, size_pt)
    if keep_criterion_figure and tier == MEDIA_TIER_FIGURE and bullets:
        rescue = _criterion_figure_keep_plan(spec, size_pt)
        if rescue is not None:
            tier, reason = rescue
    if tier == MEDIA_TIER_COLUMN:
        body_box, media_box = media_tier_boxes(bullets, size_pt)[0]
    elif tier == MEDIA_TIER_FULL:
        need = media_stack_height_in(media)
        strip = max(0.0, min(MEDIA_STRIP_MAX_IN, CONTENT_HEIGHT_IN - MEDIA_STRIP_GAP_IN - need,
                             media_strip_height_in(bullets, size_pt)))
        body_box, media_box = media_full_box(bullets, size_pt, strip_in=strip)
    else:
        body_box, media_box = (0.0, 0.0, 0.0, 0.0), MEDIA_FIGURE_BOX
    return {"tier": tier, "reason": reason, "body_box": body_box, "media_box": media_box,
            "caption_in": FIGURE_CAPTION_IN if tier == MEDIA_TIER_FIGURE else None,
            "layout": LAYOUT_FIGURE if tier == MEDIA_TIER_FIGURE else None}


def media_legibility_notes(spec: dict, plan: dict, slide_no: int) -> list[dict]:
    """图内最小字号的等效磅数 < MIN_TEXT_PT 就如实上报（结构性解法 = 拆图 / 重生成）。

    口径与 :func:`_media_pt_in` / :func:`media_need_height_in` 完全一致：同一个「显示宽度
    × 字号 ÷ 画布宽 × 72」。任务 110 起图内字号先按卡片几何重排（figure_png
    ``reflow_text_elements``）再量，量到的就是**真正画进 PNG 的字号**，不是源 JSON 里的。
    显示宽度同样按**这张图自己那一格**算（同页别的图 / 原生图表会分走图框）。
    """
    box = plan.get("media_box")
    out: list[dict] = []
    if not box:
        return out
    media = list(spec.get("media") or [])
    columns = media_draw_columns(plan.get("layout") or _layout_of(spec), len(media))
    cells = media_cell_boxes(box, len(media), columns=columns)
    for idx, (entry, cell) in enumerate(zip(media, cells), 1):
        px = list(entry.get("px") or [])
        if len(px) != 2:
            continue
        width = _fitted_media_width_in(
            entry, media_picture_box(cell, caption_in=plan.get("caption_in")))
        min_px = float((entry.get("legibility") or {}).get("min_px") or 0.0)
        title = _media_title(entry)
        if min_px <= 0:
            out.append({"slide": slide_no, "layout": _layout_of(spec), "kind": "legibility",
                        "reason": f"图 {idx}（{title}）是外来位图，测不出字号；"
                                  f"本档显示宽度 {width:.2f} 英寸"})
            continue
        # 判据与写进 reason 的数字必须同一个口径：先按 2 位小数取整再比较，避免出现
        # 「9.00 磅 < 9 磅」这种自相矛盾的上报（浮点刀口）。
        pt = round(pt_equivalent(min_px, float(px[0]), width), 2)
        if pt < MIN_TEXT_PT:
            out.append({"slide": slide_no, "layout": _layout_of(spec), "kind": "legibility",
                        "reason": f"图 {idx}（{title}）最小字号上页后 {pt:.2f} 磅 < "
                                  f"{MIN_TEXT_PT:.0f} 磅（本档显示宽度 {width:.2f} 英寸，已按卡片几何"
                                  f"重排到极限，还差 {MIN_TEXT_PT / max(pt, 0.01):.2f} 倍）"
                                  f"—— 结构性解法 = 拆图或重生成（等用户点头）"})
    return out


def _figure_drop_notes(slides: list[dict]) -> list[dict]:
    """「图超出每页上限 / 条目缺失，已挪到备注」逐页落进 meta.layout_notes（任务 35）。"""
    out: list[dict] = []
    for idx, spec in enumerate(slides, 1):
        info = dict(spec.get("figure_dropped") or {})
        reasons = _figure_drop_reasons(list(info.get("overflow") or []),
                                       list(info.get("missing") or []),
                                       list(info.get("reasons") or []))
        for reason in reasons:
            out.append({"slide": idx, "layout": _layout_of(spec),
                        "kind": "figure-dropped", "reason": reason})
    return out


def _sanitize_specs(specs: list[dict], notes: list[dict]) -> None:
    """兜底：任何页型的文字与备注都不许带图引用原文（任务 35）。

    正常路径在取 bullets 时就清了；这一层保证封面 / 回顾 / 提问 / 结束页也不漏，
    并记一条 ``layout_notes``（有图没嵌进去，绝不静默）。
    """
    for idx, spec in enumerate(specs, 1):
        touched: list[str] = []
        for key in ("title", "subtitle", "notes"):
            text = str(spec.get(key) or "")
            clean = sanitize_text(text)
            if clean != text:
                spec[key] = clean
                touched.append("备注" if key == "notes" else "标题")
        bullets = [str(b) for b in (spec.get("bullets") or []) if str(b).strip()]
        kept = [clean for clean in (sanitize_text(line) for line in bullets) if clean]
        if kept != bullets:
            spec["bullets"] = kept
            touched.append("要点")
        if touched:
            notes.append({"slide": idx, "layout": _layout_of(spec), "kind": "figure-dropped",
                          "reason": "页面" + "、".join(sorted(set(touched)))
                                    + "里含图引用原文，已移除（相应图见同页嵌入图片或本页备注）"})


def _purge_internal_words(specs: list[dict], notes: list[dict]) -> None:
    """正文（标题 / 副标题 / 要点）里不许出现内部话术，命中就搬进备注并记 layout_notes。

    备注里允许保留（讲师 / 运营要看操作步骤），所以只把原句搬进 ``notes``，不做删除。
    """
    for idx, spec in enumerate(specs, 1):
        layout = _layout_of(spec)
        moved: list[str] = []
        for key, label in (("title", "标题"), ("subtitle", "副标题")):
            text = str(spec.get(key) or "")
            hits = find_internal_words(text)
            if not hits:
                continue
            clean = strip_internal_words(text)
            spec[key] = clean
            moved.append(f"{label}「{text}」")
            notes.append({"slide": idx, "layout": layout, "kind": "internal-word",
                          "reason": f"{label}含内部话术「{'、'.join(hits)}」，已改为「{clean}」"})
        bullets = [str(b) for b in (spec.get("bullets") or []) if str(b).strip()]
        kept: list[str] = []
        for line in bullets:
            hits = find_internal_words(line)
            if not hits:
                kept.append(line)
                continue
            moved.append(line)
            notes.append({"slide": idx, "layout": layout, "kind": "internal-word",
                          "reason": f"正文含内部话术「{'、'.join(hits)}」，已移入本页备注"})
        if kept != bullets:
            spec["bullets"] = kept
        if moved:
            spec["notes"] = compose_notes(spec.get("notes") or "",
                                          ["（仅讲师可见）本页移出正文的内部话术："
                                           + "；".join(moved)])


def _figure_geometry_rows(media: list[dict], box,
                          *, columns: int = 1) -> list[tuple[float, float | None]]:
    """逐图报「这一档下显示多宽 / 图内最小字号多少」——A4 的几何门槛要具体数值。"""
    cells = media_cell_boxes(box, len(media), columns=columns)
    rows: list[tuple[float, float | None]] = []
    for entry, cell in zip(media, cells):
        width = _fitted_media_width_in(entry, media_picture_box(cell))
        rows.append((width, _media_pt_in(entry, cell)))
    return rows


def _figure_page_keep(media_list: list[dict], bullets: list[str],
                      sizes: dict) -> tuple[list[dict], list[dict], dict,
                                            tuple[float, float | None] | None]:
    """A1/A3：带要点的页放不进分栏 / 满幅时，先试「整页图 + 底部 1 条要点」。

    返回 ``(kept, rest, plan, geometry)``：``kept`` 是能留在整页图上的图（优先位图），
    ``rest`` 仍放不下、按 A4 几何门槛进备注。整页图比任何栏都大，是「不丢图」的最后
    一道保底；只要图在整页档仍 >= 6.00 英寸且 >= 9 磅，就绝不把它挪进备注。
    """
    if not media_list:
        return [], [], {}, None
    probe = {"layout": LAYOUT_FIGURE, "media": list(media_list), "bullets": list(bullets[:1])}
    plan = page_media_plan(probe, sizes)
    box = plan["media_box"]
    ok, width, pt = media_cells_report(media_list, box, columns=len(media_list),
                                       caption_in=FIGURE_CAPTION_IN)
    if ok:
        return list(media_list), [], plan, (width, pt)
    # 一组放不下时逐张试：位图优先（验收看 PICTURE），原生图表殿后。
    order = sorted(range(len(media_list)),
                   key=lambda i: (0 if media_list[i].get("px") else 1, i))
    for i in order:
        entry = media_list[i]
        ok, width, pt = media_cells_report([entry], box, columns=1,
                                           caption_in=FIGURE_CAPTION_IN)
        if ok:
            rest = [m for j, m in enumerate(media_list) if j != i]
            return [entry], rest, plan, (width, pt)
    return [], list(media_list), plan, None


def _fit_all(specs: list[dict], palette: dict, notes: list[dict]) -> None:
    """生成前按版式配额精简标题与要点；被精简掉的要点移进备注（不丢内容）。"""
    sizes = palette["sizes"]
    for idx, spec in enumerate(specs, 1):
        plan = page_media_plan(spec, sizes, keep_criterion_figure=True)
        if plan.get("layout") == LAYOUT_FIGURE and _layout_of(spec) != LAYOUT_FIGURE:
            bullets = [str(b) for b in (spec.get("bullets") or []) if str(b).strip()]
            if bullets:
                media_list = list(spec.get("media") or [])
                captions = [str(m.get("caption") or m.get("title") or "本页配图")
                            for m in media_list]
                # 图虽然不上页，**图内字号不达标**仍要如实上报（既有 legibility 口径不变）。
                notes.extend(media_legibility_notes(spec, plan, idx))
                # A1/A3：先试整页图 + 底部 1 条要点（图能保住，就不许挪进备注）。
                keep, rest, fig_plan, geom = _figure_page_keep(media_list, bullets, sizes)
                if keep:
                    first = keep[0]
                    caption = str(first.get("caption") or first.get("title") or "")
                    keep_n = _figure_bullet_lines(bullets, caption)
                    spec["layout"] = LAYOUT_FIGURE
                    spec["media"] = list(keep)
                    spec["bullets"] = bullets[:keep_n]
                    spec["title"] = figure_page_title(spec.get("title") or "", caption, idx)
                    plan = page_media_plan(spec, sizes, keep_criterion_figure=True)
                    # 补第 2 条要点若把图压到 6.00 英寸 / 9 磅以下，退回 1 条
                    # （A3 的 12 字底线仍成立；图优先，绝不为了凑字数丢图）。
                    ok, _w, _pt = media_cells_report(
                        spec["media"], plan["media_box"], columns=len(spec["media"]),
                        caption_in=FIGURE_CAPTION_IN)
                    if not ok and keep_n > 1:
                        keep_n = 1
                        spec["bullets"] = bullets[:1]
                        plan = page_media_plan(spec, sizes, keep_criterion_figure=True)
                    _ok, width, pt = media_cells_report(
                        spec["media"], plan["media_box"], columns=len(spec["media"]),
                        caption_in=FIGURE_CAPTION_IN)
                    moved = bullets[keep_n:]
                    if moved:
                        spec["notes"] = compose_notes(
                            spec.get("notes") or "",
                            [f"本页未放入的要点：{'；'.join(moved)}"])
                        notes.append({"slide": idx, "layout": LAYOUT_FIGURE,
                                      "kind": "point-overflow",
                                      "reason": f"图页只保留 {keep_n} 条要点，其余 {len(moved)} 条进备注"})
                    drop_reasons: list[str] = []
                    if rest:
                        rows = _figure_geometry_rows(rest, plan["media_box"])
                        for m, (w, pt) in zip(rest, rows):
                            k = media_list.index(m) + 1
                            label = str(m.get("caption") or m.get("title") or "本页配图")
                            drop_reasons.append(
                                f"几何门槛：第 {k} 张 [图:{label}] 在整页档 "
                                f"{w:.2f} 英寸 / {_pt_text(pt)}，未同时达到 "
                                f"≥{MIN_MEDIA_WIDTH_IN:.2f} 英寸且 ≥{MIN_TEXT_PT:.0f} 磅 ⇒ "
                                f"已挪到备注")
                        rest_caps = [str(m.get("caption") or m.get("title") or "本页配图")
                                     for m in rest]
                        dropped = dict(spec.get("figure_dropped") or {})
                        dropped["overflow"] = list(dropped.get("overflow") or []) + rest_caps
                        dropped["reasons"] = list(dropped.get("reasons") or []) + drop_reasons
                        spec["figure_dropped"] = dropped
                        spec["notes"] = compose_notes(spec.get("notes") or "", drop_reasons)
                    notes.append({"slide": idx, "layout": LAYOUT_FIGURE, "kind": "media-tier",
                                  "reason": f"几何门槛：分栏 / 满幅放不下本页要点；整页档图 "
                                            f"{width:.2f} 英寸 / {_pt_text(pt)} 达标 ⇒ 改为图页"
                                            f"（保留 {keep_n} 条要点，其余 {len(moved)} 条进备注）"})
                else:
                    # 连整页图都过不了硬下限：只能按 A4 几何门槛挪进备注（绝不写假原因）。
                    col_media = media_tier_boxes(bullets, sizes["body"])[0][1]
                    strip = max(0.0, min(MEDIA_STRIP_MAX_IN,
                                         media_strip_height_in(bullets, sizes["body"])))
                    _body, full_media = media_full_box(bullets, sizes["body"], strip_in=strip)
                    col_rows = _figure_geometry_rows(media_list, col_media)
                    full_rows = _figure_geometry_rows(media_list, full_media)
                    gate_reasons: list[str] = []
                    fig_rows = _figure_geometry_rows(media_list, fig_plan["media_box"])
                    for k, (m, (wcol, pcol), (wfull, pfull), (wfig, pfig)) in enumerate(
                            zip(media_list, col_rows, full_rows, fig_rows), 1):
                        label = str(m.get("caption") or m.get("title") or "本页配图")
                        gate_reasons.append(
                            f"几何门槛：第 {k} 张 [图:{label}] 分栏 {wcol:.2f} 英寸 / "
                            f"{_pt_text(pcol)}、满幅 {wfull:.2f} 英寸 / {_pt_text(pfull)}、"
                            f"整页档 {wfig:.2f} 英寸 / {_pt_text(pfig)}，均未同时达到 "
                            f"≥{MIN_MEDIA_WIDTH_IN:.2f} 英寸且 ≥{MIN_TEXT_PT:.0f} 磅 ⇒ "
                            f"已挪到备注")
                    spec["media"] = []
                    dropped = dict(spec.get("figure_dropped") or {})
                    dropped["overflow"] = list(dropped.get("overflow") or []) + captions
                    dropped["reasons"] = list(dropped.get("reasons") or []) + gate_reasons
                    spec["figure_dropped"] = dropped
                    spec["notes"] = compose_notes(spec.get("notes") or "", gate_reasons)
                    notes.append({"slide": idx, "layout": _layout_of(spec), "kind": "media-tier",
                                  "reason": f"{MEDIA_TIER_LABELS.get(plan.get('tier'), plan.get('tier'))}："
                                            f"{plan['reason']}；" + "；".join(gate_reasons)})
                    plan = page_media_plan(spec, sizes, keep_criterion_figure=True)
            else:
                spec["layout"] = LAYOUT_FIGURE   # 本来就没有要点：图占满整页
                notes.append({"slide": idx, "layout": LAYOUT_FIGURE, "kind": "media-tier",
                              "reason": f"{plan['reason']}（换纯图页版式）"})
        layout = _layout_of(spec)
        # 任务 146b A3：结构页（章节总览等）也必须保住 >=30 字正文。结构页的
        # 代表图若把正文条压得放不下全部要点，fit_spec 会静默丢一条要点 ⇒ 正文
        # 可能掉到 30 字以下。这里先在「图 + 全部要点」与「不要图 + 全部要点」
        # 之间做一次几何判决：后者能放下且正文 >=30 字时，图按几何门槛进备注。
        if (layout in STRUCTURAL_LAYOUT_NAMES and spec.get("media")
                and float(spec.get("media_scale") or 1.0) == 1.0):
            bullets_all = [str(b) for b in (spec.get("bullets") or []) if str(b).strip()]
            body_chars = sum(len(b) for b in bullets_all)
            body_box = plan.get("body_box") or (
                MARGIN_IN, CONTENT_TOP_IN, MEDIA_FULL_WIDTH_IN, CONTENT_HEIGHT_IN)
            # 用 fit_spec 的同一把尺子预演两种排法：带图时正文还能剩多少字、
            # 去掉图后正文能不能 >=30 字。只有「带图会掉到 30 字以下」才舍弃图。
            probe = copy.deepcopy(spec)
            fit_spec(probe, layout=layout, width_in=float(body_box[2]),
                     height_in=float(body_box[3]), size_pt=sizes["body"],
                     notes=[], slide_no=idx)
            kept_chars = sum(len(str(b)) for b in (probe.get("bullets") or []))
            no_media = copy.deepcopy(spec)
            no_media["media"] = []
            fit_spec(no_media, layout=layout, width_in=MEDIA_FULL_WIDTH_IN,
                     height_in=CONTENT_HEIGHT_IN, size_pt=sizes["body"],
                     notes=[], slide_no=idx)
            no_media_chars = sum(len(str(b)) for b in (no_media.get("bullets") or []))
            if (bullets_all and body_chars >= PAGE_MIN_TEXT_CHARS
                    and kept_chars < PAGE_MIN_TEXT_CHARS
                    and no_media_chars >= PAGE_MIN_TEXT_CHARS):
                media_list = list(spec.get("media") or [])
                needed = media_strip_height_in(bullets_all, sizes["body"])
                gap = MEDIA_STRIP_GAP_IN if needed else 0.0
                probe_box = (MARGIN_IN, CONTENT_TOP_IN + needed + gap,
                             MEDIA_FULL_WIDTH_IN,
                             max(0.0, CONTENT_HEIGHT_IN - needed - gap))
                _, width, pt = media_cells_report(media_list, probe_box)
                labels = [str(m.get("caption") or m.get("title") or "本页配图")
                          for m in media_list]
                reason = (f"几何门槛：本页 {len(bullets_all)} 条要点（正文 {body_chars} 字）"
                          f"需要正文条 {needed:.2f} 英寸，图只剩 {width:.2f} 英寸 / "
                          f"{_pt_text(pt)}（< {MIN_TEXT_PT:.0f} 磅）⇒ 已挪到备注，保留要点")
                spec["media"] = []
                dropped = dict(spec.get("figure_dropped") or {})
                dropped["overflow"] = list(dropped.get("overflow") or []) + labels
                dropped["reasons"] = list(dropped.get("reasons") or []) + [reason]
                spec["figure_dropped"] = dropped
                spec["notes"] = compose_notes(spec.get("notes") or "", [reason])
                notes.append({"slide": idx, "layout": layout, "kind": "figure-dropped",
                              "reason": reason})
                plan = page_media_plan(spec, sizes, keep_criterion_figure=True)
        if layout == LAYOUT_FIGURE and spec.get("bullets"):
            bullet_box = plan.get("bullet_box") or (
                MARGIN_IN, SLIDE_HEIGHT_IN - FIGURE_RULE_IN - FIGURE_BULLET_STRIP_IN,
                MEDIA_FULL_WIDTH_IN, FIGURE_BULLET_STRIP_IN)
            width, height = float(bullet_box[2]), float(bullet_box[3])
            point_limit_override = int(plan.get("bullet_lines") or 1)
        else:
            _left, _top, width, height = plan["body_box"]
            point_limit_override = None
        # 任务 111：练习页一页可以放多题 ⇒ 页上要点上限按题数放大（单题 6 条不变），
        # 高度也不再按「一列文本」估算 —— 那是单题的模型，会把第 2 题的选项当「放不下」
        # 砍进备注。练习页的放不下由 quiz_pack_fits 的分组判据 + 生成后几何自检负责。
        packed = layout == LAYOUT_QUIZ
        kept = fit_spec(spec, layout=layout, width_in=width, height_in=height,
                        size_pt=sizes["body"], notes=notes, slide_no=idx,
                        limit_chars=(max(point_chars(layout), PAGE_MIN_TEXT_CHARS)
                                     if layout == LAYOUT_FIGURE else None),
                        limit_points=(point_limit_override if point_limit_override is not None
                                      else QUIZ_MAX_POINTS_PER_PAGE if packed else None),
                        check_height=not packed)
        if kept["dropped"]:
            spec["notes"] = compose_notes(spec.get("notes") or "",
                                          [f"本页未放入的要点：{'；'.join(kept['dropped'])}"])


def split_multi_media_slides(specs: list[dict], notes: list[dict],
                             sizes: dict | None = None) -> None:
    """一页多张图：每张在自己的格子里够宽够清楚就不动它，放不下才拆成一页一张（0 LLM、确定可测）。

    格子按**渲染时的排法**切（纯图页整行并排、其余竖排一列，见 :func:`media_draw_columns`）——
    原生图表 / 表格也占格子，所以「图表 + 位图」同页时位图分不到整幅，必须一起判掉
    （任务 110 实测这类页正是不达标的来源）。另外，**带要点的页不许靠「整页给图」兜底**：
    纯图页没地方放要点，那一步是拆完之后的版式，否则要点会被静默挤进备注。放不下时
    只有**每页都分得到足够要点**才拆（要点按页均分、每页 1 张图）；否则不拆，第 1 张留在
    原页、其余进备注。理由逐条写进 ``layout_notes``
    （kind="media-split"），绝不静默。
    """
    sizes = sizes or deck_palette()["sizes"]
    out: list[dict] = []
    for idx, spec in enumerate(specs, 1):
        media = list(spec.get("media") or [])
        if len(media) < 2:
            out.append(spec)
            continue
        plan = page_media_plan(spec, sizes, keep_criterion_figure=True)
        box = plan.get("media_box")
        caption_in = plan.get("caption_in")
        columns = media_draw_columns(plan.get("layout") or _layout_of(spec), len(media))
        cells = media_cell_boxes(box, len(media), columns=columns) if box else []
        bullets = [str(b) for b in (spec.get("bullets") or []) if str(b).strip()]
        rescue = (plan.get("tier") == MEDIA_TIER_FIGURE and _layout_of(spec) != LAYOUT_FIGURE
                  and bool(bullets))
        keep = (bool(cells) and cells[0][3] > 0.6
                and all(_media_ok(m, cell, caption_in=caption_in)
                        for m, cell in zip(media, cells)))
        if keep and not rescue:
            out.append(spec)      # 一页放得下也看得清 ⇒ 不拆
            continue
        bitmaps = [(m, cell) for m, cell in zip(media, cells) if m.get("px")]
        widths = [_fitted_media_width_in(m, media_picture_box(cell, caption_in=caption_in))
                  for m, cell in bitmaps]
        pts = [pt for pt in (_media_pt_in(m, cell, caption_in=caption_in)
                             for m, cell in bitmaps) if pt is not None]
        how = "整行并排" if columns > 1 else "竖直叠放"
        if pts:
            detail = f"每张只剩 {min(widths):.2f} 英寸宽 / {min(pts):.2f} 磅"
        else:
            detail = "这一格连图注条都放不下"
        if rescue:
            detail = f"整页给图会把本页要点全挤进备注（{how}也是{detail}）"
        else:
            detail = f"{how}后{detail}"
        detail += f"（下限 {MIN_MEDIA_WIDTH_IN:.2f} 英寸 / {MIN_TEXT_PT:.0f} 磅）"
        # 任务 146 A2.2/A2.3：拆页时必须给每一页配上内容 —— 本页要点按页均分；
        # 有一页分不到 PAGE_MIN_TEXT_CHARS 字就**不拆**，多余的图按既有口径进备注，
        # 绝不产生「一张图 + 一行图注」的低信息页。要点不够图数时分页会越界，同样不拆。
        groups = _split_evenly(bullets, len(media)) if len(bullets) >= len(media) else []
        text_chars = sum(len(b) for b in bullets)
        if not groups or any(sum(len(b) for b in group) < PAGE_MIN_TEXT_CHARS for group in groups):
            extra_captions = [str(m.get("caption") or m.get("title") or "本页配图")
                              for m in media[1:]]
            min_chars = min((sum(len(b) for b in group) for group in groups), default=0)
            drop_reasons = [
                _bullet_shortage_drop_reason(f"第 {idx} 页", len(media), k, label,
                                             len(bullets), len(media), min_chars)
                for k, label in enumerate(extra_captions, 2)
            ]
            first = dict(spec)
            first["media"] = [media[0]]
            dropped = dict(spec.get("figure_dropped") or {})
            dropped["overflow"] = list(dropped.get("overflow") or []) + extra_captions
            dropped["reasons"] = list(dropped.get("reasons") or []) + drop_reasons
            first["figure_dropped"] = dropped
            first["notes"] = compose_notes(first.get("notes") or "",
                                           _figure_drop_reasons(extra_captions, [],
                                                                drop_reasons))
            out.append(first)
            notes.append({"slide": idx, "layout": _layout_of(spec), "kind": "media-split",
                          "reason": f"要点不足：第 {idx} 页有 {len(media)} 张图、要点 "
                                    f"{len(bullets)} 条（共 {text_chars} 字），拆页后最少一页 "
                                    f"{min_chars} 字 < {PAGE_MIN_TEXT_CHARS} 字 ⇒ 不拆，"
                                    f"其余 {len(media) - 1} 张图进备注"})
            continue
        for pos, extra in enumerate(media, 1):
            page = dict(spec)
            page["media"] = [extra]
            page["bullets"] = list(groups[pos - 1])
            page["figure_dropped"] = {}
            if pos == 1:
                page["title"] = spec.get("title") or ""
            else:
                page["title"] = continue_title(spec.get("title") or "")
            out.append(page)
        notes.append({"slide": idx, "layout": _layout_of(spec), "kind": "media-split",
                      "reason": f"拆页前的第 {idx} 页有 {len(media)} 张图：{detail}"
                                f"⇒ 拆成 {len(media)} 页，每页都保留各自那份要点与 1 张图"})
    # 页数没变也可能改了内容（如「不拆页、其余图进备注」）⇒ 必须按 out 回写，不能只比长度。
    specs[:] = out


# ---------------------------------------------------------------------------
# 任务 108：版面视觉（卡片 / 图标 / 色块 / 大字页）—— 0 LLM，几何确定可测
#
# 「不像 PPT」的两处病根都在这里治：后段 4 页（回顾 / 练习 / 提问 / 结束）过去只有一段横排
# 文字、有图页版式只有一种。做法是**每页都给版面级视觉**（卡片 / 图标 / 大数字 / 大字），
# 并且**每页都有判据与上限**：装饰元素 ≤ MAX_DECOR_PER_SLIDE、要点 ≤ 各版式配额（fit_spec）、
# 图 ≤ MAX_FIGURES_PER_SLIDE —— ⛔ 不靠给无图页塞一堆图充门面。
# 文字仍全部取自现有讲义 / 习题 / 大纲（0 LLM），只改版面与视觉元素，一个字都不改写。
# ---------------------------------------------------------------------------

def _tint(color: str, ratio: float) -> str:
    """把主题色往白里调（卡片底色）：各套主题都能得到「浅底 + 深字」的可读卡片。"""
    raw = str(color or "000000").lstrip("#")
    try:
        rgb = [int(raw[i:i + 2], 16) for i in (0, 2, 4)]
    except (ValueError, IndexError):
        rgb = [0, 0, 0]
    return "".join(f"{int(round(c + (255 - c) * ratio)):02X}" for c in rgb)


def _grid_cells(count: int, columns: int, *, left: float, top: float, width: float,
                height: float, gap: float = CARD_GAP_IN, row_gap: float = CARD_GAP_IN,
                row_height_max: float = 0.0) -> list[tuple[float, float, float, float]]:
    """``count`` 张卡片的网格几何（行优先）：确定、可测，无随机。"""
    count = max(1, int(count))
    columns = max(1, min(int(columns), count))
    rows = max(1, math.ceil(count / columns))
    cell_w = (width - gap * (columns - 1)) / columns
    cell_h = (height - row_gap * (rows - 1)) / rows
    if row_height_max:
        cell_h = min(cell_h, float(row_height_max))
    return [(left + (i % columns) * (cell_w + gap), top + (i // columns) * (cell_h + row_gap),
             cell_w, cell_h) for i in range(count)]


class _Decor:
    """单页装饰配额：到 ``MAX_DECOR_PER_SLIDE`` 就不再画并计数（调用方记 layout_notes）。"""

    def __init__(self, slide, limit: int | None = None):
        self._slide = slide
        self._limit = int(MAX_DECOR_PER_SLIDE if limit is None else limit)
        self.used = 0
        self.skipped = 0

    def box(self, left: float, top: float, width: float, height: float, color: str, *,
            round_corner: bool = False, name: str = "shape"):
        """画一块装饰（卡片 / 色块 / 徽章）；到上限不再画，返回 None。"""
        if self.used >= self._limit:
            self.skipped += 1
            return None
        shape = _add_rect(self._slide, left, top, width, height, color,
                          round_corner=round_corner)
        shape.name = f"{DECOR_PREFIX}-{name}-{self.used + 1}"
        self.used += 1
        return shape


def _add_visual_text(slide, text, left: float, top: float, width: float, height: float, *,
                     size: float, color: str, bold: bool = False, align: str = "left",
                     middle: bool = False, wrap: bool = True, name: str = CARD_PREFIX):
    """版面视觉里的文字（卡片正文 / 大字 / 徽章字符）。

    ``name`` 决定它算哪一类：``cg-card`` 是正文（页级大纲读得到）、``cg-icon`` 是图标字符、
    ``cg-meta`` 是署名日期。多行按**多个段落**写，与几何自检的折行估算同一口径。
    """
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.util import Inches, Pt

    items = [text] if isinstance(text, str) else list(text)
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    box.name = f"{name}-{len(slide.shapes)}"
    frame = box.text_frame
    frame.word_wrap = bool(wrap)
    if middle:
        frame.vertical_anchor = MSO_ANCHOR.MIDDLE
    if not wrap:   # 徽章：不留内边距，字符正好居中
        frame.margin_left = frame.margin_right = 0
        frame.margin_top = frame.margin_bottom = 0
    alignment = {"center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT}.get(align, PP_ALIGN.LEFT)
    for idx, item in enumerate(items):
        para = frame.paragraphs[0] if idx == 0 else frame.add_paragraph()
        para.alignment = alignment
        para.space_after = Pt(0 if idx == len(items) - 1 else SPACE_AFTER_PT)
        para.text = str(item)
        for run in para.runs:
            run.font.size = Pt(size)
            run.font.bold = bold
            run.font.color.rgb = _rgb_of(color)
    return box


def _review_icon(text: str) -> str:
    """回顾卡的小图标：按要点前缀确定（误区 -> !、要点/小结 -> ✓、其余 -> ◆）。"""
    raw = str(text or "")
    if re.search(r"误区|易错|注意|坑", raw):
        return "!"
    if re.search(r"要点|小结|回顾|总结", raw):
        return "✓"
    return "◆"


def _card_head_text(line: str) -> str:
    """卡片上那一行「结论」：原文照搬（只想拿到前缀做图标 / 数字化时用）。"""
    return str(line or "").strip()


def _lead_number(line: str) -> str:
    """行首的编号（目录卡的章节大数字）：取不到就返回空串（不新造数字）。"""
    hit = re.match(r"^\s*(?:第)?\s*(\d+)", str(line or ""))
    return hit.group(1) if hit else ""


def _split_sides(lines: list[str], left_name: str, right_name: str
                 ) -> tuple[list[str], list[str]]:
    """按栏名把要点分到两栏：提到哪一侧就归哪一侧，都没提就左右交替（从左侧开始）。"""
    left: list[str] = []
    right: list[str] = []
    for idx, line in enumerate(lines):
        has_l = bool(left_name) and left_name in line
        has_r = bool(right_name) and right_name in line
        if has_r and not has_l:
            right.append(line)
        elif has_l and not has_r:
            left.append(line)
        else:
            (left if idx % 2 == 0 else right).append(line)
    if len(lines) >= 2 and not right:
        right.append(left.pop())
    if len(lines) >= 2 and not left:
        left.append(right.pop(0))
    return left, right


def _render_goals(slide, spec, palette, sizes, plan, lines, decor, size) -> bool:
    """课程目标：3 列目标卡（最多 2 行 = 6 张，与既有 6 条上限一致），每卡一个编号徽章。"""
    cells = _grid_cells(len(lines), min(3, len(lines)), left=CARD_LEFT_IN, top=CARD_TOP_IN,
                        width=CARD_WIDTH_IN, height=CARD_HEIGHT_IN)
    bg = _tint(palette["accent"], 0.92)
    for idx, (line, (left, top, width, height)) in enumerate(zip(lines, cells), 1):
        decor.box(left, top, width, height, bg, round_corner=True, name="goal-card")
        decor.box(left + CARD_PAD_IN, top + CARD_PAD_IN, 0.34, 0.34, palette["accent"],
                  round_corner=True, name="goal-no")
        _add_visual_text(slide, str(idx), left + CARD_PAD_IN, top + CARD_PAD_IN, 0.34, 0.34,
                         size=14, color=palette["bg"], bold=True, align="center", middle=True,
                         wrap=False, name=ICON_PREFIX)
        _add_visual_text(slide, line, left + CARD_PAD_IN, top + CARD_PAD_IN + 0.46,
                         width - CARD_PAD_IN * 2, height - CARD_PAD_IN * 2 - 0.46,
                         size=sizes["body_dense"], color=palette["text"])
    return True


def _render_toc(slide, spec, palette, sizes, plan, lines, decor, size) -> bool:
    """课程目录：章节大数字卡（≤3 章一列，否则两列），大数字取自行首编号。"""
    columns = 1 if len(lines) <= 3 else 2
    cells = _grid_cells(len(lines), columns, left=CARD_LEFT_IN, top=CARD_TOP_IN,
                        width=CARD_WIDTH_IN, height=CARD_HEIGHT_IN)
    bg = _tint(palette["accent"], 0.92)
    for line, (left, top, width, height) in zip(lines, cells):
        decor.box(left, top, width, height, bg, round_corner=True, name="toc-card")
        decor.box(left + CARD_PAD_IN, top + CARD_PAD_IN, 0.92, 0.92, palette["accent"],
                  round_corner=True, name="toc-no")
        _add_visual_text(slide, _lead_number(line) or "·", left + CARD_PAD_IN, top + CARD_PAD_IN,
                         0.92, 0.92, size=30, color=palette["bg"], bold=True, align="center",
                         middle=True, wrap=False, name=ICON_PREFIX)
        _add_visual_text(slide, line, left + CARD_PAD_IN + 1.08, top + CARD_PAD_IN,
                         width - CARD_PAD_IN * 2 - 1.08, height - CARD_PAD_IN * 2,
                         size=sizes["body_dense"], color=palette["text"])
    return True


def _render_review(slide, spec, palette, sizes, plan, lines, decor, size) -> bool:
    """本章回顾：要点卡（≤4 条 2×2，5 条 3 列），每卡一个小图标 + 一行结论（原文照搬）。"""
    columns = 2 if len(lines) <= 4 else 3
    cells = _grid_cells(len(lines), columns, left=CARD_LEFT_IN, top=CARD_TOP_IN,
                        width=CARD_WIDTH_IN, height=CARD_HEIGHT_IN)
    bg = _tint(palette["accent"], 0.92)
    for line, (left, top, width, height) in zip(lines, cells):
        decor.box(left, top, width, height, bg, round_corner=True, name="review-card")
        decor.box(left + CARD_PAD_IN, top + CARD_PAD_IN, BADGE_IN, BADGE_IN, palette["accent"],
                  round_corner=True, name="review-icon")
        _add_visual_text(slide, _review_icon(line), left + CARD_PAD_IN, top + CARD_PAD_IN,
                         BADGE_IN, BADGE_IN, size=16, color=palette["bg"], bold=True,
                         align="center", middle=True, wrap=False, name=ICON_PREFIX)
        _add_visual_text(slide, line, left + CARD_PAD_IN + BADGE_IN + 0.16, top + CARD_PAD_IN,
                         width - CARD_PAD_IN * 2 - BADGE_IN - 0.16, height - CARD_PAD_IN * 2,
                         size=sizes["body_dense"], color=palette["text"])
    return True


def _render_quiz(slide, spec, palette, sizes, plan, lines, decor, size) -> bool:
    """随堂练习：一页 1–QUIZ_PER_PAGE 题 —— 题干卡（大字）+ 选项卡；答案解析只在备注里。

    一页一题时走 :func:`_render_quiz_single`（108 的版式，一个字没动）；一页多题时走
    :func:`_render_quiz_packed`：每题一块「题干卡 + 选项卡一行」，题与题之间画一条分隔线，
    每块的高度按同一套判据（:func:`quiz_pack_fits`）算出来，放不下就不会走到这里。
    """
    blocks = _quiz_blocks_of_lines(lines)
    if len(blocks) <= 1:
        return _render_quiz_single(slide, spec, palette, lines, decor)
    return _render_quiz_packed(slide, spec, palette, blocks, decor)


def _quiz_blocks_of_lines(lines: list[str]) -> list[tuple[str, list[str]]]:
    """把一页的正文行切成「题」：不是「A. 」开头的行就是新一题的题干（确定、可测）。"""
    blocks: list[tuple[str, list[str]]] = []
    for line in lines:
        text = str(line)
        if blocks and _QUIZ_OPTION_RE.match(text):
            blocks[-1][1].append(text)
            continue
        blocks.append((text, []))
    return blocks


def _render_quiz_single(slide, spec, palette, lines, decor) -> bool:
    """一页一题（任务 108 的版式）：题干卡 + 选项两列网格。

    任务 146 A3：**无选项的简答题**（开放题）不画空选项卡 —— 题干卡加高、题干放大，
    下方补「作答提示」面板；答案与解析仍只在备注里（既有口径未改）。
    """
    stem, options = lines[0], lines[1:]
    bg = _tint(palette["accent"], 0.92)
    stem_h = QUIZ_STEM_H_IN if options else QUIZ_OPEN_STEM_H_IN
    stem_pt = 24 if options else QUIZ_OPEN_STEM_SIZE_PT
    decor.box(CARD_LEFT_IN, CARD_TOP_IN, CARD_WIDTH_IN, stem_h, bg, round_corner=True,
              name="quiz-stem")
    decor.box(CARD_LEFT_IN + 0.18, CARD_TOP_IN + 0.16, 0.44, 0.44, palette["accent"],
              round_corner=True, name="quiz-no")
    _add_visual_text(slide, str(spec.get("quiz_index") or "1"), CARD_LEFT_IN + 0.18,
                     CARD_TOP_IN + 0.16, 0.44, 0.44, size=16, color=palette["bg"], bold=True,
                     align="center", middle=True, wrap=False, name=ICON_PREFIX)
    _add_visual_text(slide, stem, CARD_LEFT_IN + 0.76, CARD_TOP_IN + 0.10,
                     CARD_WIDTH_IN - 0.94, stem_h - 0.20, size=stem_pt,
                     color=palette["text"])
    if not options:
        hint_top = CARD_TOP_IN + stem_h + 0.26
        hint_h = CARD_TOP_IN + CARD_HEIGHT_IN - hint_top
        decor.box(CARD_LEFT_IN, hint_top, CARD_WIDTH_IN, hint_h,
                  _tint(palette["accent"], 0.96), round_corner=True, name="quiz-hints")
        for pos, hint in enumerate(QUIZ_OPEN_HINTS):
            _add_visual_text(slide, hint, CARD_LEFT_IN + 0.32,
                             hint_top + 0.30 + pos * 0.58,
                             CARD_WIDTH_IN - 0.64, 0.50, size=16,
                             color=palette["muted"])
        return True
    top = CARD_TOP_IN + QUIZ_STEM_H_IN + CARD_GAP_IN
    height = CARD_TOP_IN + CARD_HEIGHT_IN - top
    cells = _grid_cells(len(options), 2 if len(options) >= 2 else 1, left=CARD_LEFT_IN, top=top,
                        width=CARD_WIDTH_IN, height=height, row_gap=0.20, row_height_max=1.10)
    for option, (left, cell_top, width, cell_h) in zip(options, cells):
        decor.box(left, cell_top, width, cell_h, bg, round_corner=True, name="quiz-option")
        _add_visual_text(slide, option, left + 0.28, cell_top + 0.12, width - 0.50,
                         cell_h - 0.22, size=15, color=palette["text"])
    return True


def _render_quiz_packed(slide, spec, palette, blocks, decor) -> bool:
    """一页多题（任务 111）：每题「题干卡 + 编号徽章 + 选项卡一行」，题间一条分隔线。

    几何全用 :func:`quiz_stem_height_in` / :func:`quiz_option_row_height_in` 算出来的高度
    （与「放不下就回落一题」的判据同一套），字号仍是题干 24 磅、选项 15 磅 —— 不压字号。
    """
    bg = _tint(palette["accent"], 0.92)
    numbers = [str(x) for x in (spec.get("quiz_indices") or [])]
    y = CARD_TOP_IN
    for pos, (stem, options) in enumerate(blocks):
        stem_h = quiz_stem_height_in(stem)
        label = numbers[pos] if pos < len(numbers) else str(spec.get("quiz_index") or pos + 1)
        decor.box(CARD_LEFT_IN, y, CARD_WIDTH_IN, stem_h, bg, round_corner=True, name="quiz-stem")
        decor.box(CARD_LEFT_IN + 0.18, y + 0.16, 0.44, 0.44, palette["accent"],
                  round_corner=True, name="quiz-no")
        _add_visual_text(slide, label, CARD_LEFT_IN + 0.18, y + 0.16, 0.44, 0.44, size=16,
                         color=palette["bg"], bold=True, align="center", middle=True, wrap=False,
                         name=ICON_PREFIX)
        _add_visual_text(slide, stem, CARD_LEFT_IN + 0.76, y + QUIZ_PACK_PAD_IN,
                         QUIZ_STEM_TEXT_W_IN, stem_h - QUIZ_PACK_PAD_IN * 2, size=QUIZ_STEM_SIZE_PT,
                         color=palette["text"])
        y += stem_h
        if options:
            row_h = quiz_option_row_height_in(options)
            top = y + QUIZ_PACK_STEM_GAP_IN
            cells = _grid_cells(len(options), len(options), left=CARD_LEFT_IN, top=top,
                                width=CARD_WIDTH_IN, height=row_h, row_gap=0.20,
                                row_height_max=QUIZ_OPTION_ROW_MAX_IN)
            for option, (left, cell_top, width, cell_h) in zip(options, cells):
                decor.box(left, cell_top, width, cell_h, bg, round_corner=True, name="quiz-option")
                _add_visual_text(slide, option, left + 0.28, cell_top + 0.12,
                                 width - QUIZ_OPTION_TEXT_PAD_IN, cell_h - QUIZ_OPTION_PAD_IN,
                                 size=QUIZ_OPTION_SIZE_PT, color=palette["text"])
            y = top + row_h
        if pos < len(blocks) - 1:
            decor.box(CARD_LEFT_IN, y + QUIZ_PACK_GAP_IN / 2 - 0.01, CARD_WIDTH_IN, 0.02,
                      palette["accent"], name="quiz-split")
            y += QUIZ_PACK_GAP_IN
    # 任务 146 A3：一页多道**无选项简答题**时，单题版式的「作答提示」也要留在页面上，
    # 不能只剩几行题干。几何高度由 quiz_pack_fits 预留，这里只负责画。
    if any(not options for _stem, options in blocks):
        hint_top = CARD_TOP_IN + quiz_page_height_in(blocks) + QUIZ_PACK_GAP_IN
        hint_bottom = CARD_TOP_IN + CARD_HEIGHT_IN
        if hint_bottom - hint_top >= QUIZ_OPEN_HINTS_MIN_IN:
            decor.box(CARD_LEFT_IN, hint_top, CARD_WIDTH_IN, hint_bottom - hint_top,
                      _tint(palette["accent"], 0.96), round_corner=True, name="quiz-hints")
            row_h = (hint_bottom - hint_top - 0.30) / len(QUIZ_OPEN_HINTS)
            for pos, hint in enumerate(QUIZ_OPEN_HINTS):
                _add_visual_text(slide, hint, CARD_LEFT_IN + 0.32,
                                 hint_top + 0.15 + pos * row_h,
                                 CARD_WIDTH_IN - 0.64, min(0.50, row_h),
                                 size=QUIZ_OPEN_HINT_SIZE_PT, color=palette["muted"])
    return True


def _render_questions(slide, spec, palette, sizes, plan, lines, decor, size) -> bool:
    """课堂提问：大字提问页（≤2 句居中大字 + 1 句提示），不再是 bullet 列表。"""
    big, hint = lines[:2], lines[2:]
    decor.box(1.00, 1.72, SLIDE_WIDTH_IN - 2.00, 3.60, _tint(palette["accent"], 0.92),
              round_corner=True, name="ask-panel")
    _add_visual_text(slide, big, 1.30, 1.88, SLIDE_WIDTH_IN - 2.60, 3.28, size=28,
                     color=palette["text"], align="center", middle=True)
    if hint:
        decor.box(SLIDE_WIDTH_IN / 2 - 0.55, 5.40, 1.10, 0.06, palette["accent"],
                  name="ask-rule")
        _add_visual_text(slide, hint, 1.30, 5.58, SLIDE_WIDTH_IN - 2.60, 0.90, size=16,
                         color=palette["muted"], align="center", middle=True)
    return True


def _render_compare(slide, spec, palette, sizes, plan, lines, decor, size) -> bool:
    """对比页：两栏对照（栏名取自标题原文），要点按「提到哪一侧」归属，其余左右交替。"""
    left, top, width, height = plan["body_box"]
    if width <= 0 or len(lines) < 2:
        return False
    left_name, right_name = compare_sides(spec.get("title") or "")
    groups = _split_sides(lines, left_name, right_name)
    gap, col_w = 0.30, (width - 0.30) / 2
    bg = _tint(palette["accent"], 0.92)
    for idx, (name, items) in enumerate(zip((left_name, right_name), groups)):
        x = left + idx * (col_w + gap)
        decor.box(x, top, col_w, height, bg, round_corner=True, name="cmp-panel")
        decor.box(x, top, col_w, 0.44, palette["secondary"], round_corner=True, name="cmp-head")
        _add_visual_text(slide, name or "—", x + 0.10, top, col_w - 0.20, 0.44, size=14,
                         color=palette["bg"], bold=True, align="center", middle=True,
                         name=ICON_PREFIX)
        y = top + 0.60
        for item in items:
            row_h = est_height_in([item], float(size), col_w - 0.48)
            decor.box(x + 0.10, y - 0.04, col_w - 0.20, row_h + 0.08,
                      _tint(palette["accent"], 0.75), round_corner=True, name="cmp-item")
            _add_visual_text(slide, item, x + 0.24, y, col_w - 0.48, row_h + 0.02, size=size,
                             color=palette["text"])
            y += row_h + 0.16
    return True


def _render_steps(slide, spec, palette, sizes, plan, lines, decor, size) -> bool:
    """步骤 / 时间线页：≤5 步带编号徽章（宽栏横向、窄栏竖排），不带序号的要点收在下方。"""
    left, top, width, height = plan["body_box"]
    if width <= 0:
        return False
    stepped, rest = [], []
    for line in lines:
        head = _RE_BULLET_LEAD.sub("", line)
        (stepped if _RE_STEP_HEAD.search(head) else rest).append(line)
    if not stepped:
        return False
    steps = stepped[:5]
    rest_h = min(1.30, 0.34 * len(rest) + 0.10) if rest else 0.0
    if width >= 8.0:                      # 宽栏：横向时间线
        cell_w = (width - 0.24 * (len(steps) - 1)) / len(steps)
        centers = [left + i * (cell_w + 0.24) + cell_w / 2 for i in range(len(steps))]
        if len(steps) > 1:
            decor.box(centers[0], top + 0.23, centers[-1] - centers[0], 0.05, palette["rule"],
                      name="step-line")
        for idx, (line, cx) in enumerate(zip(steps, centers), 1):
            x = cx - cell_w / 2
            decor.box(cx - 0.25, top, 0.50, 0.50, palette["accent"], round_corner=True,
                      name="step-no")
            _add_visual_text(slide, str(idx), cx - 0.25, top, 0.50, 0.50, size=18,
                             color=palette["bg"], bold=True, align="center", middle=True,
                             wrap=False, name=ICON_PREFIX)
            row_h = est_height_in([line], float(size), cell_w - 0.24)
            decor.box(x, top + 0.68, cell_w, row_h + 0.24, _tint(palette["accent"], 0.92),
                      round_corner=True, name="step-card")
            _add_visual_text(slide, line, x + 0.12, top + 0.78, cell_w - 0.24, row_h + 0.04,
                             size=size, color=palette["text"], align="center")
        rest_top = top + 0.72 + max(
            [est_height_in([x], float(size), cell_w - 0.24) for x in steps] or [0.0]) + 0.30
    else:                                 # 窄栏（右栏有图）：竖排时间线
        decor.box(left + 0.20, top + 0.24, 0.05, max(0.05, 0.86 * len(steps) - 0.20),
                  palette["rule"], name="step-line")
        y = top
        for idx, line in enumerate(steps, 1):
            row_h = max(0.86, est_height_in([line], float(size), width - 0.86) + 0.20)
            decor.box(left, y + 0.06, 0.45, 0.45, palette["accent"], round_corner=True,
                      name="step-no")
            _add_visual_text(slide, str(idx), left, y + 0.06, 0.45, 0.45, size=17,
                             color=palette["bg"], bold=True, align="center", middle=True,
                             wrap=False, name=ICON_PREFIX)
            _add_visual_text(slide, line, left + 0.66, y, width - 0.66, row_h, size=size,
                             color=palette["text"])
            y += row_h
        rest_top = y + 0.10
    if rest:
        _add_visual_text(slide, rest, left, rest_top, width, height - (rest_top - top),
                         size=sizes["caption"] + 2, color=palette["muted"])
    return True


def _render_data(slide, spec, palette, sizes, plan, lines, decor, size) -> bool:
    """数据页：大数字卡（行内的百分比 / 倍数当大数字，整句原文当注解，一个字不改）。"""
    left, top, width, height = plan["body_box"]
    if width <= 0 or not lines:
        return False
    wide = width >= 8.0
    columns = min(len(lines), 3) if wide else 1
    cells = _grid_cells(len(lines), columns, left=left, top=top, width=width, height=height,
                        row_height_max=2.10 if wide else 1.15)
    for line, (cell_left, cell_top, cell_w, cell_h) in zip(lines, cells):
        decor.box(cell_left, cell_top, cell_w, cell_h, _tint(palette["accent"], 0.92),
                  round_corner=True, name="data-card")
        text_left, text_w = cell_left + CARD_PAD_IN, cell_w - CARD_PAD_IN * 2
        hit = _RE_STAT_MARK.search(line)
        if hit:
            decor.box(cell_left + CARD_PAD_IN, cell_top + CARD_PAD_IN, 0.98, 0.98,
                      _tint(palette["accent"], 0.72), round_corner=True, name="data-no")
            _add_visual_text(slide, hit.group(0), cell_left + CARD_PAD_IN, cell_top + CARD_PAD_IN,
                             0.98, 0.98, size=26, color=palette["accent"], bold=True,
                             align="center", middle=True, wrap=False, name=ICON_PREFIX)
            text_left, text_w = cell_left + CARD_PAD_IN + 1.14, cell_w - CARD_PAD_IN * 2 - 1.14
        _add_visual_text(slide, line, text_left, cell_top + CARD_PAD_IN, text_w,
                         cell_h - CARD_PAD_IN * 2, size=size, color=palette["text"])
    return True


def _render_point_rows(slide, palette, plan, lines, decor, size) -> None:
    """知识点页 / 章节概览页的默认版式：卡片画在正文占位符**后面**（要点仍是占位符文字）。

    每条的卡片高度用与几何自检**同一把尺子**（``est_height_in`` + ``SPACE_AFTER_PT``）算，
    所以卡片与文字永远对得上，不会出现「卡短字出头」。
    """
    left, top, width, height = plan["body_box"]
    if width <= 0:
        return
    bg = _tint(palette["accent"], 0.92)
    y = top
    for line in lines:
        row_h = est_height_in([line], float(size), width)
        decor.box(left - 0.12, y - 0.05, width + 0.24, row_h + 0.10, bg, round_corner=True,
                  name="point-card")
        decor.box(left - 0.09, y, 0.06, row_h, palette["accent"], name="point-bar")
        y += row_h + SPACE_AFTER_PT / 72.0


_VISUAL_RENDERERS = {
    LAYOUT_GOALS: _render_goals,
    LAYOUT_TOC: _render_toc,
    LAYOUT_REVIEW: _render_review,
    LAYOUT_QUIZ: _render_quiz,
    LAYOUT_QUESTIONS: _render_questions,
    LAYOUT_POINT_COMPARE: _render_compare,
    LAYOUT_POINT_STEPS: _render_steps,
    LAYOUT_POINT_DATA: _render_data,
}

# 结束页（大字收尾）：标题居中放大，标题与正文之间是「分隔线 + 课程名」，页脚位置只放署名 / 日期
END_TITLE_BOX = (1.00, 1.55, SLIDE_WIDTH_IN - 2.00, 1.25)
END_BODY_BOX = (1.40, 3.62, SLIDE_WIDTH_IN - 2.80, 2.05)
END_PANEL_BOX = (1.00, 1.45, SLIDE_WIDTH_IN - 2.00, 2.13)   # 大字与课程名的浅色底块
END_RULE_TOP_IN = 2.86        # 结束页标题下的分隔线
END_COURSE_TOP_IN = 3.00      # 结束页「课程名」一行的顶


# 任务 144：封面视觉三件套（大色块锚 / 贯穿强调条 / 真实数据条）的几何
COVER_BAR_IN = 0.30            # 左侧贯穿竖条宽（沿用旧值 0.30）
COVER_BAR_TWO_IN = 0.08        # 竖条旁的第二色细条（新增：双色竖条的节奏）
COVER_ANCHOR_W_IN, COVER_ANCHOR_H_IN = 1.90, 1.20      # 右上主色块（旧 1.55×0.95）
COVER_ANCHOR_SW_IN, COVER_ANCHOR_SH_IN = 1.20, 0.60    # 其下次级色块（旧 1.05×0.55）
COVER_RULE_H_IN = 0.09         # 标题下「贯穿整页」的强调横条（旧：2.2×0.06 的短横线）
COVER_STATS_TOP_IN = 1.45      # 数据条顶（标题框顶 1.95 之上，两者不重叠）
COVER_STATS_W_IN = 10.00       # 数据条宽（右端 10.90 英寸，避开右上角的大色块）
COVER_STATS_H_IN = 0.34


def _cover_stats(work: list[dict], course: Course, cover: dict | None = None) -> str:
    """封面数据条的**真实**数据：章节页数 / 最终页数 / 嵌入图张数 / 课时。

    全部取自真实出处：页数 = 本次生成的**最终规格**条数（拆页、精简之后）；配图数 = 最终规格
    里嵌入的图 / 图表 / 表格条数；章数与知识点数 = 大纲里的真实条数（封面规格上的
    ``cover_facts``，由 :func:`course_slides` / :func:`chapter_slides` 带来）；
    课时 = 课程字段 ``course.duration_minutes``。取不到的项**直接不显示**（这是客户可见面
    —— 绝不许估、不许编）。整课课件才报课时（章节课的课时口径是整门课，写在本章封面上
    会误导客户）。
    """
    pages = len(work)
    facts = dict((cover or {}).get("cover_facts") or {})
    chapters = int(facts.get("chapters") or 0)
    points = int(facts.get("points") or 0)
    figures = sum(len(spec.get("media") or []) for spec in work)
    parts = [f"{chapters} 章"] if chapters else ([f"{points} 个知识点"] if points else [])
    parts.append(f"{pages} 页")
    if figures:
        parts.append(f"配图 {figures} 张")
    # 现场调用方（几何自检 / 单测）可能给的是轻量对象 ⇒ 缺字段按「取不到」处理，不抛
    minutes = int(getattr(course, "duration_minutes", 0) or 0)
    if chapters and minutes > 0:
        parts.append(f"课时 {minutes} 分钟")
    return " · ".join(parts)


def _cover_visual(slide, palette, decor, stats: str = "") -> None:
    """封面视觉（任务 144 重做）：① 大色块锚 ② 贯穿性强调条 ③ 一条真实数据条。

    旧版只有三块色（左侧 0.30 竖条 + 右上 1.55×0.95 / 1.05×0.55）与标题下一条 2.2 英寸
    短横线，没有「设计过的」层次。这版：锚 = 左侧双色竖条（贯穿整页）+ 右上大小两块；
    贯穿 = 标题下方一条**从最左到最右**的主题色横条（把封面切成上下两半）；数据 = 标题上方
    的数据条（``_cover_stats``，数字全来自本次课件的真实值）。封面不受「正文几何零变化」
    约束（红线只锁内页），但装饰数仍守 ``MAX_DECOR_PER_SLIDE``。
    """
    from pptx.util import Inches, Pt

    decor.box(0.0, 0.0, COVER_BAR_IN, SLIDE_HEIGHT_IN, palette["accent"], name="cover-bar")
    decor.box(COVER_BAR_IN, 0.0, COVER_BAR_TWO_IN, SLIDE_HEIGHT_IN * 0.5,
              palette["secondary"], name="cover-bar")
    decor.box(SLIDE_WIDTH_IN - COVER_ANCHOR_W_IN, 0.0, COVER_ANCHOR_W_IN, COVER_ANCHOR_H_IN,
              palette["accent"], name="cover-block")
    decor.box(SLIDE_WIDTH_IN - COVER_ANCHOR_SW_IN, COVER_ANCHOR_H_IN, COVER_ANCHOR_SW_IN,
              COVER_ANCHOR_SH_IN, palette["secondary"], name="cover-block")
    decor.box(0.0, COVER_RULE_TOP, SLIDE_WIDTH_IN, COVER_RULE_H_IN, palette["accent"],
              name="cover-rule")
    if not stats:
        return
    box = slide.shapes.add_textbox(Inches(COVER_TITLE_BOX[0]), Inches(COVER_STATS_TOP_IN),
                                   Inches(COVER_STATS_W_IN), Inches(COVER_STATS_H_IN))
    # 元信息：不算正文、不算装饰（read_deck_slides 只读 cg-card-*）
    box.name = f"{META_PREFIX}-cover-stats"
    para = box.text_frame.paragraphs[0]
    para.text = stats
    for run in para.runs:
        run.font.size = Pt(palette["sizes"]["caption"])
        run.font.bold = True
        run.font.color.rgb = _rgb_of(palette["muted"])


def _end_visual(slide, course, spec, palette, sizes, decor) -> None:
    """结束页：浅色底块 + 分隔线 + 课程名（居中）+ 署名 / 日期（文字全是现有封面口径）。

    课程名单独一行（**不带「课程名 · …」前缀**）—— 页脚是「<课程名> · <章号 章名> · P<页码>」，
    结束页若也以「课程名 · 」开头，关掉页脚时（``PPT_SHOW_FOOTER=false``）就分不清页脚有没有画。
    """
    decor.box(*END_PANEL_BOX, _tint(palette["accent"], 0.94), round_corner=True,
              name="end-panel")
    title = str(course.title or "").strip()
    if title:
        _add_visual_text(slide, title, END_BODY_BOX[0], END_COURSE_TOP_IN, END_BODY_BOX[2], 0.50,
                         size=sizes["body"] + 2, color=palette["accent"], bold=True,
                         align="center", middle=True, name=META_PREFIX)
    meta = " · ".join(p for p in _cover_meta_bullets() if p)
    if meta:
        _add_visual_text(slide, meta, END_BODY_BOX[0], END_META_TOP_IN, END_BODY_BOX[2], 0.36,
                         size=sizes["caption"], color=palette["muted"], align="center",
                         name=META_PREFIX)
    decor.box(SLIDE_WIDTH_IN / 2 - 0.55, END_RULE_TOP_IN, 1.10, 0.07, palette["accent"],
              name="end-rule")
    decor.box(0.55, 6.30, 0.30, 0.30, palette["accent"], name="end-dot")
    decor.box(SLIDE_WIDTH_IN - 0.85, 6.30, 0.30, 0.30, palette["secondary"], name="end-dot")


def _render_body(slide, spec, layout, palette, sizes, plan, lines, size, decor) -> bool:
    """一页的正文视觉：返回 True = 已经自己画了（正文占位符留空）；False = 用正文占位符。"""
    if not lines:
        return False
    renderer = _VISUAL_RENDERERS.get(layout)
    if renderer is not None:
        return bool(renderer(slide, spec, palette, sizes, plan, lines, decor, size))
    if layout in (LAYOUT_POINT, LAYOUT_CHAPTER):
        _render_point_rows(slide, palette, plan, lines, decor, size)
    return False


def _build_prs(course: Course, specs: list[dict], palette: dict, chapter_label: str | None,
               tier_notes: list[dict] | None = None):
    """按主题母版与版式族把每页规格渲染成 Presentation（不落盘，便于几何自检）。

    ``tier_notes`` 是「图档位 + 图内字号」的出口：每次都先清空再按本次规格重算，
    多轮降级迭代后留下的永远是**最终规格**的口径（调用方在迭代结束后合并进 layout_notes）。
    """
    from pptx import Presentation
    from pptx.util import Inches

    if tier_notes is not None:
        tier_notes.clear()
    prs = Presentation()
    prs.slide_width = Inches(SLIDE_WIDTH_IN)
    prs.slide_height = Inches(SLIDE_HEIGHT_IN)
    sizes = palette["sizes"]
    watermark = str(get_settings().ppt_watermark or "")
    # 任务 144：封面数据条（真实值，见 _cover_stats；章数 / 知识点数来自封面规格的 cover_facts）
    cover_spec = next((spec for spec in specs if _layout_of(spec) == LAYOUT_COVER), None)
    cover_stats = _cover_stats(specs, course, cover_spec) if cover_spec else ""
    for idx, spec in enumerate(specs):
        layout = _layout_of(spec)
        is_cover = layout == LAYOUT_COVER
        media = list(spec.get("media") or [])
        scale = max(0.3, float(spec.get("media_scale") or 1.0))
        plan = page_media_plan(spec, sizes, keep_criterion_figure=True)
        # 纯图页（有图）：不画标题条、不另起标题 —— 图占整页，图注当页标题（见下）
        bare_figure = layout == LAYOUT_FIGURE and bool(media)
        slide = prs.slides.add_slide(prs.slide_layouts[0] if is_cover else prs.slide_layouts[1])
        _set_slide_bg(slide, palette["bg"])
        # 结束页是大字收尾页：不画深色标题条（大字用的是「封面标题色」，深色底上会看不见）
        if palette.get("title_band") and not is_cover and not bare_figure \
                and layout != LAYOUT_END:
            _add_title_band(slide, palette)
        decor = _Decor(slide)
        if tier_notes is not None and plan["tier"]:
            tier_notes.append({"slide": idx + 1, "layout": layout, "kind": "media-tier",
                               "reason": f"{MEDIA_TIER_LABELS.get(plan['tier'], plan['tier'])}："
                                         f"{plan['reason']}"})
            tier_notes.extend(media_legibility_notes(spec, plan, idx + 1))
        lines = [str(b) for b in (spec.get("bullets") or []) if str(b).strip()]
        if is_cover:
            if spec.get("subtitle"):
                lines = [str(spec["subtitle"]), *lines]
            # 封面没有深色标题条，白字主题要用独立封面色，否则白字白底看不见
            _set_title(slide.shapes.title, spec, *COVER_TITLE_BOX, sizes["cover_title"],
                       palette.get("cover_title") or palette["title"])
            _cover_visual(slide, palette, decor, cover_stats)
        elif layout == LAYOUT_END:
            # 大字收尾页：标题居中放大（用封面色，页面上没有深色标题条）
            _set_title(slide.shapes.title, spec, *END_TITLE_BOX, sizes["cover_title"],
                       palette.get("cover_title") or palette["title"], align="center")
            _end_visual(slide, course, spec, palette, sizes, decor)
        elif bare_figure:
            # 页首一行（不画标题条）：左边本页标题、右边图注 —— 图因此能吃满整块页高
            _set_title(slide.shapes.title,
                       {"title": _one_line(spec.get("title") or "", FIGURE_HEAD_TITLE_IN,
                                           sizes["caption"])},
                       MARGIN_IN, FIGURE_HEAD_TOP_IN, FIGURE_HEAD_TITLE_IN, FIGURE_HEAD_H_IN,
                       sizes["caption"], palette["caption"])
            caption_left = MARGIN_IN + FIGURE_HEAD_TITLE_IN + FIGURE_HEAD_GAP_IN
            _add_figure_caption_line(slide, media, left=caption_left, top=FIGURE_HEAD_TOP_IN,
                                     width=SLIDE_WIDTH_IN - MARGIN_IN - caption_left,
                                     height=FIGURE_HEAD_H_IN, size=sizes["caption"],
                                     color=palette["caption"])
        else:
            _set_title(slide.shapes.title, spec, *TITLE_BOX, sizes["title"], palette["title"])
            # 任务 144：内页标题条从「1.6 英寸短横线」加宽成**与正文同宽**的一条标尺
            # （标题区装饰；几何变化只允许出现在这里，正文框一格不动）
            _add_rect(slide, MARGIN_IN, TITLE_RULE_TOP, TITLE_RULE_W_IN, TITLE_RULE_H_IN,
                      palette["accent"])
        if layout == LAYOUT_FIGURE:
            fine = plan["media_box"]
            _place_media(slide, media, fine[0], fine[1], fine[2] * scale, fine[3] * scale,
                         palette, columns=len(media), caption_in=plan.get("caption_in"))
            # 任务 146b A3：图页底部一条要点（至少 1 条，12 字以上）；图框已让出这条高度。
            if lines and plan.get("bullet_box"):
                left, top, width, height = plan["bullet_box"]
                if len(slide.placeholders) > 1:
                    count = int(plan.get("bullet_lines") or 1)
                    _set_body(slide.placeholders[1], lines[:count], left, top, width, height,
                              size=sizes["body"], color=palette["text"])
                    _bring_to_front(slide.placeholders[1])
        else:
            size = sizes["body_dense"] if len(lines) > 4 else sizes["body"]
            box = END_BODY_BOX if layout == LAYOUT_END else plan["body_box"]
            drawn = _render_body(slide, spec, layout, palette, sizes, plan, lines, size, decor)
            if not drawn and len(slide.placeholders) > 1:
                _set_body(slide.placeholders[1], lines, *box, size=size,
                          align="center" if layout == LAYOUT_END else None,
                          color=palette["text"])
                _bring_to_front(slide.placeholders[1])
            if media and not is_cover and plan["media_box"]:
                mleft, mtop, mwidth, mheight = plan["media_box"]
                top = mtop
                width, height = mwidth * scale, mheight * scale
                if scale != 1.0:
                    top += (mheight - height) / 2
                _place_media(slide, media, mleft, top, width, height, palette)
        _add_footer(slide, palette, course.title, chapter_label, idx + 1)
        if decor.skipped and tier_notes is not None:
            tier_notes.append({"slide": idx + 1, "layout": layout, "kind": "decor-cap",
                               "reason": f"本页装饰元素超上限 {MAX_DECOR_PER_SLIDE} 个："
                                         f"超出的 {decor.skipped} 个未画（防堆砌）"})
        _add_watermark(slide, watermark)
        note = _clip(spec.get("notes") or "", 1200)
        slide.notes_slide.notes_text_frame.text = note or f"{spec.get('title') or '本页'}：暂无备注"
    return prs


def _render_pptx(course: Course, slides: list[dict], out_path: Path, *,
                 theme: str = "", chapter_label: str | None = None,
                 out_layouts: list[str] | None = None) -> tuple[list[dict], str | None]:
    """python-pptx 渲染（v4）：主题母版 → 生成前精简 → 生成后几何自检/降级。

    返回 ``(layout_notes, theme_error)``：``layout_notes`` 逐条记录「哪一页为什么被精简 /
    降级」，写进 ``meta.layout_notes`` 供前端展示（绝不静默）。

    ``out_layouts`` 是**最终**逐页版式的出口（任务 108）：拆页 / 换纯图页之后的页序与
    ``meta.slides`` 才对得上（此前 ``meta.layouts`` 记的是拆页**之前**的规格，页数会对不上）。
    """
    spec, theme_error = resolve_theme(theme or get_settings().ppt_theme)
    palette = deck_palette(spec["id"], get_style_anchor(course))
    notes: list[dict] = []
    if theme_error:
        notes.append({"slide": 0, "kind": "theme", "reason": f"{theme_error}（本次按该主题渲染）"})
    work = copy.deepcopy(list(slides))
    _sanitize_specs(work, notes)
    _purge_internal_words(work, notes)
    split_multi_media_slides(work, notes)
    _fit_all(work, palette, notes)

    prs = None
    tier_notes: list[dict] = []
    for _ in range(4):
        prs = _build_prs(course, work, palette, chapter_label, tier_notes=tier_notes)
        issues = audit_presentation(prs)
        if not issues:
            break
        changed = False
        for issue in issues:
            target = work[max(1, int(issue["slide"])) - 1]
            result = degrade_spec(target, notes, int(issue["slide"]))
            if result is None:
                notes.append({"slide": int(issue["slide"]), "layout": _layout_of(target),
                              "kind": "audit", "reason": f"几何自检未通过：{issue['detail']}"})
                continue
            changed = True
            if result["dropped"]:
                target["notes"] = compose_notes(target.get("notes") or "",
                                                [f"本页未放入的要点：{'；'.join(result['dropped'])}"])
        if not changed:
            break
    notes.extend(tier_notes)   # 最终规格的图档位 / 图内字号口径
    # 任务 108：知识点页选了哪种变体版式、为什么（只在**最终**版式仍是该变体时记；
    # 被几何 / 图档位改成纯图页的，版式以 meta.layouts 为准，不在这里误导）。
    for pos, target in enumerate(work, 1):
        info = dict(target.get("point_variant") or {})
        variant = str(info.get("layout") or "")
        if variant in POINT_VARIANTS and _layout_of(target) == variant:
            notes.append({"slide": pos, "layout": variant, "kind": "point-variant",
                          "reason": str(info.get("reason") or "")})
    # 任务 111：练习页「这一页放了几题、为什么」逐页记一条（只记最终版式仍是练习页的页，
    # 被几何降级改掉的以 meta.layouts 为准，不在这里误导）。
    for pos, target in enumerate(work, 1):
        pack = dict(target.get("quiz_pack") or {})
        if pack and _layout_of(target) == LAYOUT_QUIZ:
            notes.append({"slide": pos, "layout": LAYOUT_QUIZ, "kind": "quiz-pack",
                          "reason": str(pack.get("reason") or "")})
    # 任务 127：图内取色跟的是**本次课件主题**；存量图（没有原始 data）靠重映射跟上时，
    # 「本页为什么换了图内色」逐页记一条（kind="figure-kind"），不静默。
    for pos, target in enumerate(work, 1):
        for entry in (target.get("media") or []):
            note = dict(entry.get("figure_note") or {}) if isinstance(entry, dict) else {}
            if note:
                notes.append({"slide": pos, "layout": _layout_of(target),
                              "kind": "figure-kind", "reason": str(note.get("reason") or "")})
    # 任务 128：课件**逐页**记「为什么选了这个版式」（kind="layout-choice"）—— 任务 127 的 ③
    # 只把象限图判据写在**图条目**上，课件页这一层查不到「这页为什么是象限图 / 为什么是这两栏
    # 对照」。这里只**记录已经做出的选择**（不选版式、不改判据、不动几何），理由复用既有文案。
    for pos, target in enumerate(work, 1):
        notes.append({"slide": pos, "layout": _layout_of(target),
                      "kind": LAYOUT_CHOICE_KIND, **slide_choice_of(target)})
    if out_layouts is not None:
        out_layouts[:] = [_layout_of(target) for target in work]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(out_path)
    # 任务 144：落盘后把主题字体写进包里每个文字属性（中文走 a:ea，只写 font.name 是无效的）
    font_stats = apply_deck_fonts(out_path, palette.get("font") or {})
    if font_stats.get("error"):
        notes.append({"slide": 0, "kind": "font",
                      "reason": f"主题字体写入失败：{font_stats['error']}"
                                f"（本份课件的字体仍由模板决定）"})
    return notes, theme_error

SLIDE_NOTES_EXCERPT = 300  # 页级预览里的备注摘要上限


def read_deck_slides(pptx_path: str | Path) -> list[dict]:
    """用 python-pptx 读回真实文件：每页 {index, title, bullets, notes_excerpt}。

    以磁盘上的 pptx 为准（而不是生成时的内存结构），这样网页预览与实际文件
    永远一致；首页副标题也一并归入 bullets。

    任务 108 起，卡片版式（回顾 / 练习 / 提问 / 目录 / 目标 / 对比 / 步骤 / 数据）的正文写在
    ``cg-card-*`` 文本框里（不在正文占位符里）⇒ 这里一并按**落位顺序**读回来，页级大纲、
    网页版课件与前端预览因此仍拿得到全部正文（一个字都不丢）。
    """
    from pptx import Presentation

    prs = Presentation(str(pptx_path))
    slides: list[dict] = []
    for idx, slide in enumerate(prs.slides, 1):
        title = ""
        if slide.shapes.title is not None and slide.shapes.title.has_text_frame:
            title = slide.shapes.title.text.strip()
        bullets: list[str] = []
        for placeholder in slide.placeholders:
            if placeholder.placeholder_format.idx == 0 or not placeholder.has_text_frame:
                continue
            for para in placeholder.text_frame.paragraphs:
                text = (para.text or "").strip()
                if text:
                    bullets.append(text)
        for shape in slide.shapes:
            if not str(getattr(shape, "name", "")).startswith(CARD_PREFIX):
                continue
            if not getattr(shape, "has_text_frame", False):
                continue
            for para in shape.text_frame.paragraphs:
                text = (para.text or "").strip()
                if text:
                    bullets.append(text)
        notes = ""
        if slide.has_notes_slide:
            notes = (slide.notes_slide.notes_text_frame.text or "").strip()
        slides.append({
            "index": idx,
            "title": title,
            "bullets": bullets,
            "notes_excerpt": _clip(notes, SLIDE_NOTES_EXCERPT),
        })
    return slides


def _attach_slides(meta: dict, pptx_path: str | Path) -> None:
    """把页级大纲写进 meta.slides；读不回来必须显式记录，绝不静默。"""
    try:
        meta["slides"] = read_deck_slides(pptx_path)
    except Exception as exc:  # noqa: BLE001 —— 预览失败不能拖垮生成主流程
        meta["slides"] = []
        meta["slides_error"] = f"读取页级大纲失败：{exc}"


def outline_slides(item: ContentItem) -> list[dict]:
    """课件的页级大纲：优先用 meta.slides；旧版课件（此功能上线前生成的）现场读回 pptx。"""
    meta = dict(item.meta or {})
    cached = meta.get("slides")
    if isinstance(cached, list) and cached:
        return cached
    path = str((item.body or {}).get("pptx") or "")
    if path and Path(path).exists():
        try:
            return read_deck_slides(path)
        except Exception:  # noqa: BLE001 —— 预览读不回不该阻塞列表
            return []
    return []


def _dashi_project() -> str | None:
    """可用的 dashi-ppt 项目目录（测试用 DASHI_PPT_FORCE_FALLBACK 会强制返回 None）。"""
    settings = get_settings()
    if settings.dashi_ppt_force_fallback:
        return None
    return settings.resolved_dashi_project


def _slides_to_briefs(course: Course, slides: list[dict]) -> list[dict]:
    brand = course.subject or "CourseGenie"
    briefs: list[dict] = []
    for idx, spec in enumerate(slides):
        bullets = [str(b) for b in spec.get("bullets") or []]
        briefs.append({
            "role": _BODY_ROLES[idx % len(_BODY_ROLES)],
            "content": {
                "presentation": {
                    "title": str(spec.get("title") or ""),
                    "titleShort": str(spec.get("title") or "")[:12],
                    "summary": spec.get("subtitle") or (bullets[0] if bullets else ""),
                    "takeaway": bullets[-1] if bullets else "",
                    "items": [{"label": f"要点{i}", "displayValue": b[:18], "detail": b}
                              for i, b in enumerate(bullets[:4], 1)],
                },
                "meta": {"brand": brand, "panelTitle": _BODY_ROLES[idx % len(_BODY_ROLES)].upper(),
                         "pageLabel": f"{idx + 1} / {len(slides)}", "date": ""},
            },
        })
    return briefs


def _run_npm(root: str, script: str, args: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    cmd = [NPM, "--prefix", root, "run", script, "--", *args]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, cwd=root)


def _generate_with_dashi(course: Course, slides: list[dict], theme: str, work_dir: Path) -> dict:
    """dashi-ppt 链路（可选引擎）：briefs → goal → render → export。"""
    root = _dashi_project()
    if not root:
        raise RuntimeError("dashi-ppt 未配置或探测不到项目目录")
    briefs_path = work_dir / "briefs.json"
    goal_path = work_dir / "goal.json"
    deck_dir = work_dir / "deck"
    deck_dir.mkdir(parents=True, exist_ok=True)
    briefs_path.write_text(json.dumps(_slides_to_briefs(course, slides), ensure_ascii=False),
                           encoding="utf-8")
    pages = str(min(max(len(slides), 2), 50))
    log: list[str] = []

    def run(script: str, args: list[str]) -> None:
        proc = _run_npm(root, script, args)
        tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-5:]
        log.append(f"[{script}] rc={proc.returncode} {tail}")
        if proc.returncode != 0:
            raise RuntimeError(f"dashi-ppt {script} 失败:\n{proc.stdout}\n{proc.stderr}")

    run("goal:scaffold", [
        "--title", course.title,
        "--goal", course.title,
        "--audience", course.audience or "通用",
        "--theme", theme,
        "--pages", pages,
        "--layout-variants", "1",
        "--out", str(goal_path),
        "--briefs", str(briefs_path),
    ])
    run("props:safe", ["--goal", str(goal_path), "--write"])
    run("validate:goal-spec", [str(goal_path)])
    run("render:goal", [str(goal_path), str(deck_dir / "index.html")])

    pptx = work_dir / f"{course.id}.pptx"
    pdf = work_dir / f"{course.id}.pdf"
    run("export:pptx", [str(deck_dir), str(pptx)])
    run("export:pdf", [str(deck_dir), str(pdf)])
    return {"pptx": pptx, "pdf": pdf, "html": deck_dir / "index.html",
            "engine": ENGINE_DASHI, "log": log}


def _dashi_theme(theme: str) -> str:
    """dashi-ppt 只认它自己的主题 id：非 themeNN 一律回落 theme07。"""
    raw = str(theme or "").strip().lower()
    return raw if re.fullmatch(r"theme\d+", raw) else "theme07"


def _render_deck(course: Course, slides: list[dict], work_dir: Path, theme: str,
                 engine: str | None,
                 chapter_label: str | None = None) -> tuple[dict, str | None, list[dict]]:
    """按显式选择的引擎渲染；dashi 失败时显式回退 python-pptx 并返回原因与版式诊断。"""
    engine_error: str | None = None
    if engine == ENGINE_DASHI:
        if _dashi_project():
            try:
                result = _generate_with_dashi(course, slides, _dashi_theme(theme), work_dir)
                return result, None, []
            except Exception as exc:  # noqa: BLE001 —— 回退必须显式记录
                engine_error = f"dashi-ppt 渲染失败，已回退 python-pptx：{exc}"
        else:
            engine_error = "dashi-ppt 未配置或探测不到项目目录，已回退 python-pptx"
    pptx = work_dir / f"{course.id}_{uuid.uuid4().hex[:6]}.pptx"
    layouts: list[str] = []
    layout_notes, _ = _render_pptx(course, slides, pptx, theme=theme,
                                   chapter_label=chapter_label, out_layouts=layouts)
    return ({"pptx": pptx, "pdf": None, "html": None, "engine": ENGINE_PPTX,
             "layouts": layouts},
            engine_error, layout_notes)


# ---------------------------------------------------------------------------
# PDF 预览（可选增强：PPT_PREVIEW_PDF 开启且探测到 PowerPoint 时走 COM）
# ---------------------------------------------------------------------------

PPT_PREVIEW_TIMEOUT = 180  # COM 导出 PDF 的硬超时（秒）：超时直接 kill 子进程
PPT_SLIDE_TIMEOUT = 180   # COM 导出幻灯片图片的硬超时（秒）
SLIDES_SUBDIR = "slides"
# 每页一张 PNG 的导出尺寸（任务 107：从 1280×720 提到 2560×1440 = 2× 目标显示尺寸，
# 放大看不再是把 1280 宽的位图拉大）。真正导出的是 ppt_com.PNG_WIDTH/PNG_HEIGHT，
# 两处必须一致：tests/test_ppt_legibility.py 有断言。
PPT_PNG_WIDTH, PPT_PNG_HEIGHT = 2560, 1440
SLIDE_IMAGE_PREFIX = "slide"
_POWERPOINT_CANDIDATES = (
    r"C:\Program Files\Microsoft Office\root\Office16\POWERPNT.EXE",
    r"C:\Program Files (x86)\Microsoft Office\root\Office16\POWERPNT.EXE",
)


def powerpoint_exe() -> str | None:
    """探测 PowerPoint 可执行文件（支持 POWERPOINT_EXE 环境变量覆盖）。"""
    override = os.environ.get("POWERPOINT_EXE")
    candidates = [override, *_POWERPOINT_CANDIDATES] if override else list(_POWERPOINT_CANDIDATES)
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _run_com_export(src: Path, target: Path, timeout: int = PPT_PREVIEW_TIMEOUT) -> None:
    """跑 COM 导出的子进程（可超时强杀）；失败抛出带原因的异常。"""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "app.services.ppt_com", "pdf", str(src), str(target)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, cwd=str(_backend_root()),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"导出超时（>{timeout}s），已终止 PowerPoint 子进程") from exc
    if proc.returncode != 0 or not target.exists():
        detail = (proc.stderr or proc.stdout or "").strip() or f"退出码 {proc.returncode}"
        raise RuntimeError(detail)


def _run_com_png(src: Path, out_dir: Path, timeout: int = PPT_SLIDE_TIMEOUT) -> None:
    """跑 COM 导 PNG 的子进程（可超时强杀）；失败抛出带原因的异常。"""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "app.services.ppt_com", "png", str(src), str(out_dir)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, cwd=str(_backend_root()),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"导出超时（>{timeout}s），已终止 PowerPoint 子进程") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or f"退出码 {proc.returncode}"
        raise RuntimeError(detail)


def _powerpoint_pids() -> set[int]:
    """当前 POWERPNT.EXE 的 PID 集合（非 Windows / 取不到时返回空集）。"""
    if os.name != "nt":
        return set()
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq POWERPNT.EXE", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    pids: set[int] = set()
    for line in (proc.stdout or "").splitlines():
        parts = [chunk.strip().strip('"') for chunk in line.split(",")]
        if len(parts) >= 2 and parts[0].upper().startswith("POWERPNT"):
            try:
                pids.add(int(parts[1]))
            except ValueError:
                continue
    return pids


def _kill_powerpoint(pids: set[int]) -> None:
    """只清理本次导出新起的（残留）PowerPoint 进程，绝不动用户已开着的实例。"""
    for pid in sorted(pids):
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue


def _rmtree(path: Path) -> None:
    import shutil

    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def slides_dir_for(pptx_path: str | Path) -> Path:
    """课件同目录下的 slides/（每个课件一个独立目录）。"""
    return Path(str(pptx_path)).parent / SLIDES_SUBDIR


def _rel_to_work_dir(path: Path) -> str:
    base = get_settings().ppt_work_dir.resolve()
    try:
        return path.resolve().relative_to(base).as_posix()
    except (ValueError, OSError):
        return path.resolve().as_posix()


def _normalize_png_names(out_dir: Path) -> list[Path]:
    """PowerPoint 产出的「幻灯片N.PNG」→ 稳定的 slide-01.png（按页序编号）。"""
    files = [p for p in out_dir.iterdir() if p.is_file() and p.suffix.lower() == ".png"]

    def _order(path: Path) -> tuple[int, str]:
        match = re.search(r"(\d+)", path.name)
        return (int(match.group(1)) if match else 0, path.name)

    files.sort(key=_order)
    out: list[Path] = []
    for idx, path in enumerate(files, 1):
        target = out_dir / f"{SLIDE_IMAGE_PREFIX}-{idx:02d}.png"
        if path != target:
            path.replace(target)
        out.append(target)
    return out


def export_deck_pngs(pptx_path: str | Path,
                     *, timeout: int = PPT_SLIDE_TIMEOUT) -> tuple[list[str] | None, str | None]:
    """把课件每页导成 slides/slide-NN.png。

    返回 ``([相对 ppt_work_dir 的图片路径, ...], 失败原因)``；成功时原因为 None。
    PowerPoint 探测不到或 COM 报错一律**显式回退**（返回原因），并清理残留
    POWERPNT 进程与半成品目录，绝不静默。
    """
    if not get_settings().ppt_slide_images:
        return None, None  # 配置显式关闭：不是失败，无需上报
    exe = powerpoint_exe()
    if exe is None:
        logger.warning(
            "课件幻灯片图片降级：未探测到 PowerPoint（POWERPNT.EXE）⇒ 回退文字页级大纲；"
            "这是**环境缺组件**（装 Office/PPT 后重试即可），不是课件有问题。")
        return None, "未探测到 PowerPoint（POWERPNT.EXE），已回退为文字页级大纲"
    src = Path(str(pptx_path))
    if not src.exists():
        return None, f"课件源文件不存在：{src.name}，已回退为文字页级大纲"
    out_dir = slides_dir_for(src)
    _rmtree(out_dir)  # 先清干净，避免上次的残留被当成新产物
    before = _powerpoint_pids()
    try:
        _run_com_png(src, out_dir, timeout)
    except Exception as exc:  # noqa: BLE001 —— 失败必须显式上报
        _kill_powerpoint(_powerpoint_pids() - before)
        _rmtree(out_dir)
        return None, f"PowerPoint COM 导出幻灯片图片失败（已回退为文字页级大纲）：{exc}"
    images = _normalize_png_names(out_dir)
    if not images:
        _kill_powerpoint(_powerpoint_pids() - before)
        _rmtree(out_dir)
        return None, "PowerPoint 没有产出幻灯片图片（已回退为文字页级大纲）"
    return [_rel_to_work_dir(p) for p in images], None


def _apply_slide_images(meta: dict, images: list[str] | None) -> None:
    """把每页图片的相对路径写进 meta.slides[i].image。"""
    if not images:
        return
    for idx, slide in enumerate(meta.get("slides") or []):
        if idx < len(images):
            slide["image"] = images[idx]


def export_pdf_preview(pptx_path: str | Path, *, timeout: int = PPT_PREVIEW_TIMEOUT) -> dict:
    """把课件导成 PDF 供网页内嵌；不可用时显式回退到页级大纲并写明原因。"""
    if not get_settings().ppt_preview_pdf:
        return {"ok": False, "enabled": False, "fallback": "outline",
                "reason": "PDF 预览未开启（PPT_PREVIEW_PDF=false），已回退页级大纲"}
    exe = powerpoint_exe()
    if exe is None:
        logger.warning(
            "课件预览降级：未探测到 PowerPoint（POWERPNT.EXE）⇒ 回退页级大纲；"
            "这是**环境缺组件**（装 Office/PPT 后重试即可），不是课件有问题。")
        return {"ok": False, "enabled": True, "fallback": "outline",
                "reason": "未探测到 PowerPoint（POWERPNT.EXE），已回退页级大纲"}
    src = Path(str(pptx_path))
    if not src.exists():
        return {"ok": False, "enabled": True, "fallback": "outline",
                "reason": f"课件源文件不存在：{src.name}，已回退页级大纲"}
    target = src.parent / "preview.pdf"
    try:
        _run_com_export(src, target, timeout)
    except Exception as exc:  # noqa: BLE001 —— 失败必须显式上报，不许静默
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        return {"ok": False, "enabled": True, "fallback": "outline",
                "reason": f"PowerPoint COM 导出失败（已回退页级大纲）：{exc}"}
    return {"ok": True, "enabled": True, "fallback": None, "reason": None, "pdf": str(target)}


# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------

def _find_deck(db: Session, course: Course, scope: str,
               chapter: OutlineNode | None) -> ContentItem | None:
    """同一 scope（整课 / 某一章）只保留一个课件条目。

    章节课件的匹配优先看 ``meta.chapter_node_id``，老数据（早期生成、node_id 与
    meta 都缺失或大纲重建过）再按 ``meta.chapter_key``（章号）兜底——否则重做一章
    会新生成一条，把「每章只 1 份」变成两份。
    """
    rows = (db.query(ContentItem)
            .filter(ContentItem.course_id == course.id, ContentItem.kind == "ppt")
            .order_by(ContentItem.created_at).all())
    fallback: ContentItem | None = None
    for row in rows:
        meta = dict(row.meta or {})
        if str(meta.get("scope") or SCOPE_COURSE) != scope:
            continue
        if scope == SCOPE_CHAPTER and chapter is not None:
            if str(meta.get("chapter_node_id") or "") != chapter.id:
                if fallback is None and str(meta.get("chapter_key") or "") == chapter.node_key:
                    fallback = row
                continue
        return row
    return fallback


def _upsert(db: Session, course: Course, body: dict, title: str, meta: dict,
            item_id: str | None, node_id: str | None = None) -> ContentItem:
    """原地替换：同一 scope 复用同一条目，旧版进 meta.history（≤5）。

    章节课件同时写 ``node_id``（= 章节点 id），让数据模型与 ``meta.chapter_node_id``
    口径一致；老数据（node_id 为 NULL）在读取时按 ``meta.chapter_key`` 兜底，不做迁移。
    """
    item = _target_item(db, course, item_id) if item_id else None
    if item is None:
        item = ContentItem(course_id=course.id, node_id=node_id, kind="ppt",
                           title=title, body=body, meta=meta)
        db.add(item)
    else:
        history = list((item.meta or {}).get("history") or [])
        history.insert(0, {"body": item.body, "at": _now_iso(), "title": item.title})
        item.meta = {**(item.meta or {}), "history": history[:5], **meta}
        item.title = title
        item.body = body
        if node_id:
            item.node_id = node_id
    db.commit()
    db.refresh(item)
    return item


def _target_item(db: Session, course: Course, item_id: str) -> ContentItem:
    item = db.get(ContentItem, item_id)
    if item is None or item.course_id != course.id or item.kind != "ppt":
        raise ValueError(f"内容项不存在或类型不符：{item_id}")
    return item


def generate_deck(db: Session, course: Course, *, scope: str = SCOPE_COURSE,
                  chapter_node_id: str | None = None, item_id: str | None = None,
                  theme: str = "", engine: str | None = None,
                  slides_per_point: int | None = None) -> ContentItem:
    """生成/重做整课或某一章的 PPT（确定性，不调 LLM）。

    主题口径（任务 145，**唯一出处** ``ppt_theme_setting.resolve``）：
    **显式传的 theme（非空）> 课程 meta.ppt_theme > 后端配置 PPT_THEME > 内置默认**。
    ``theme`` 默认空串 = 走课程设置（**不再**默认「专业培训」——那会顶掉后端配置，
    同一门课换个入口重出就换色）；来源记进 ``meta.theme_source``，回退原因记进
    ``meta.theme_error`` 与 ``layout_notes``（不静默）。
    """
    scope = SCOPE_CHAPTER if str(scope) == SCOPE_CHAPTER else SCOPE_COURSE
    nodes = list_nodes(db, course.id)
    chapter: OutlineNode | None = None
    per_point = int(slides_per_point) if slides_per_point else get_settings().ppt_slides_per_point
    raw_theme = str(theme or "").strip()
    resolved, theme_source, theme_error = ppt_theme_setting.resolve(
        raw_theme, course, get_settings().ppt_theme)
    # 下游（配色 / 图色 / 渲染）一律用**规范 id**；调用方显式传的那一档按原样透传 ——
    # 这样「未知主题已回退」仍由 _render_pptx 自己记进 layout_notes（既有口径不变）。
    theme = raw_theme or resolved["id"]
    chapter_label: str | None = None
    if scope == SCOPE_CHAPTER:
        chapter = db.get(OutlineNode, chapter_node_id) if chapter_node_id else None
        if chapter is None or chapter not in nodes:
            raise ValueError("章节不存在或不属于该课程：请传入有效的 chapter_node_id")
        work_dir = deck_work_dir(course.id, scope, chapter.node_key)
        work_dir.mkdir(parents=True, exist_ok=True)
        slides, source = chapter_slides(db, course, chapter, nodes,
                                        figures_dir=work_dir / FIGURES_SUBDIR,
                                        slides_per_point=per_point, theme=theme)
        title = deck_title(SCOPE_CHAPTER, course.title, chapter)
        chapter_label = node_chapter_label(chapter)
    else:
        work_dir = deck_work_dir(course.id, scope)
        work_dir.mkdir(parents=True, exist_ok=True)
        slides, source = course_slides(db, course, nodes, theme=theme,
                                       figures_dir=work_dir / FIGURES_SUBDIR)
        title = deck_title(SCOPE_COURSE, course.title)
        chapter_label = "整课"

    result, engine_error, layout_notes = _render_deck(course, slides, work_dir, theme, engine,
                                                      chapter_label=chapter_label)
    if theme_error and not raw_theme:
        # 空串请求（走课程设置 / 后端配置）时的回退原因只有这里能记：显式传主题那一档
        # 由 ``_render_pptx`` 自己记同一句口径（一处只记一次，不重复）。
        layout_notes = [{"slide": 0, "kind": "theme",
                         "reason": f"{theme_error}（本次按该主题渲染）"}, *layout_notes]

    anchor = get_style_anchor(course)
    concept_ids = [c.id for c in db.query(ContentItem).filter(
        ContentItem.course_id == course.id, ContentItem.kind == "concept").all()]
    body = {
        "pptx": str(result["pptx"]).replace("\\", "/"),
        "pdf": str(result["pdf"]).replace("\\", "/") if result.get("pdf") else "",
        "html": str(result["html"]).replace("\\", "/") if result.get("html") else "",
        "engine": result["engine"],
        "source": "generate",
    }
    meta: dict = {
        "scope": scope,
        "chapter_node_id": chapter.id if chapter is not None else None,
        "chapter_key": chapter.node_key if chapter is not None else None,
        "engine": result["engine"],
        "theme": resolved["id"],
        "theme_name": resolved["name"],
        # 任务 145：这份课件的主题是**哪来的**（param / course / config / default）
        "theme_source": theme_source,
        "slide_count": len(slides),
        "generated_at": _now_iso(),
        "slides_per_point": per_point if scope == SCOPE_CHAPTER else None,
        "figure_count": int(source.get("figure_count") or 0),
        "figure_failures": list(source.get("figure_failures") or []),
        "source": {
            "outline_rev": source.get("outline_rev") or "",
            "lecture_ids": source.get("lecture_ids") or [],
            "chart_ids": source.get("chart_ids") or [],
            "style_anchor_ref": (anchor or {}).get("source_node_key") or "",
            "concept_ids": concept_ids,
        },
    }
    # 任务 86：记「生成时的口径」。课件是 0-LLM 确定性产物、没有自己的提示词，
    # 讲解正文取自讲义（lecture-v14 口径）⇒ prompt_version 记的是**上游讲义模板版本**。
    meta.update(scenario.run_meta(course, PROMPT_TEMPLATE_VERSION))
    # 任务 108：逐页版式与页数一律取**渲染后**的最终口径（拆页 / 换纯图页之后），
    # 否则 meta.layouts / slide_count 会和 meta.slides（从 pptx 读回）对不上 —— 真有这回事：
    # 任务 110 拆图后真库旗舰课 meta.slide_count = 130 而 pptx 实际 156 页。
    final_layouts = list(result.get("layouts") or [])
    meta["layouts"] = final_layouts or [_layout_of(s) for s in slides]
    meta["slide_count"] = len(meta["layouts"])
    meta["layout_notes"] = list(layout_notes) + _figure_drop_notes(slides)
    if theme_error:
        meta["theme_error"] = theme_error
    if engine_error:
        meta["engine_error"] = engine_error
    _attach_slides(meta, result["pptx"])
    images, images_error = export_deck_pngs(result["pptx"])
    _apply_slide_images(meta, images)
    if images_error:
        meta["images_error"] = images_error
    target = item_id
    if target is None:
        existing = _find_deck(db, course, scope, chapter)
        target = existing.id if existing else None
    return _upsert(db, course, body, title, meta, target,
                   node_id=chapter.id if chapter is not None else None)


def generate_ppt(db: Session, course: Course, theme: str, pages_per_chapter: int = 2,
                 item_id: str | None = None) -> ContentItem:
    """旧入口（整课课件）：保留给既有调用方，等价于 scope="course"（主题口径同 :func:`generate_deck`）。"""
    return generate_deck(db, course, scope=SCOPE_COURSE, item_id=item_id, theme=theme)


def _chapter_nodes(db: Session, course: Course) -> list[OutlineNode]:
    """当前大纲里的章节节点（与前端 `!key.includes(".")` 同一口径）。"""
    return [n for n in list_nodes(db, course.id) if "." not in str(n.node_key or "")]


def generate_all_chapter_decks(
    db: Session,
    course: Course,
    *,
    redo: bool = False,
    theme: str = "",
    engine: str | None = None,
    progress=None,
) -> dict:
    """逐章串行补齐章节课件（默认只补缺）。

    这里直接调 :func:`generate_deck`（不经过路由）：调用方（路由）已全程持有本次批量的
    scope 锁，逐章串行即可，不会再触发单章路由的「同课程并发」拒绝。

    ``theme`` 默认空串 ⇒ **与 :func:`generate_deck` 走同一条优先级**（课程 `meta.ppt_theme`
    > 后端配置 ``PPT_THEME`` > 内置默认；任务 145，唯一出处 ``ppt_theme_setting.resolve``）——
    不是「空串就直接落配置」。

    ``progress(idx, total, chapter, status, error)`` 每章完成后回调一次（status 取
    ``running`` / ``generated`` / ``skipped`` / ``failed``），供任务面板显示进度。
    """
    chapters = _chapter_nodes(db, course)
    existing = {str((d.meta or {}).get("chapter_key") or "")
                for d in list_decks(db, course.id)
                if str((d.meta or {}).get("scope") or SCOPE_COURSE) == SCOPE_CHAPTER}
    total = len(chapters)
    generated: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    for idx, chapter in enumerate(chapters, 1):
        label = node_chapter_label(chapter)
        if not redo and chapter.node_key in existing:
            skipped.append({"chapter_node_id": chapter.id, "chapter_key": chapter.node_key,
                            "label": label, "reason": "已生成（默认只补缺）"})
            if progress:
                progress(idx, total, chapter, "skipped", None)
            continue
        if progress:
            progress(idx, total, chapter, "running", None)
        try:
            item = generate_deck(db, course, scope=SCOPE_CHAPTER,
                                 chapter_node_id=chapter.id, theme=theme, engine=engine)
        except Exception as exc:  # noqa: BLE001 —— 单章失败不拖垮整批，逐章显式上报
            db.rollback()
            failed.append({"chapter_node_id": chapter.id, "chapter_key": chapter.node_key,
                           "label": label, "error": str(exc)})
            if progress:
                progress(idx, total, chapter, "failed", str(exc))
            continue
        generated.append({
            "chapter_node_id": chapter.id, "chapter_key": chapter.node_key, "label": label,
            "content_id": item.id, "title": item.title,
            "slide_count": (item.meta or {}).get("slide_count"),
            "theme_name": (item.meta or {}).get("theme_name"),
            "engine_error": (item.meta or {}).get("engine_error"),
        })
        if progress:
            progress(idx, total, chapter, "generated", None)
    return {"total": total, "generated": generated, "skipped": skipped, "failed": failed}


def import_ppt_upload(db: Session, course: Course, filename: str, data: bytes,
                      *, scope: str = SCOPE_COURSE,
                      chapter_node_id: str | None = None) -> ContentItem:
    """教师上传自己的 PPTX：原地替换目标 scope 的课件条目（source=upload）。"""
    scope = SCOPE_CHAPTER if str(scope) == SCOPE_CHAPTER else SCOPE_COURSE
    nodes = list_nodes(db, course.id)
    chapter: OutlineNode | None = None
    if scope == SCOPE_CHAPTER:
        chapter = db.get(OutlineNode, chapter_node_id) if chapter_node_id else None
        if chapter is None or chapter not in nodes:
            raise ValueError("章节不存在或不属于该课程：请传入有效的 chapter_node_id")
    up_root = get_settings().ppt_work_dir.resolve() / "uploads"
    safe = "".join(c for c in Path(filename).name if c.isalnum() or c in "._-") or "deck.pptx"
    deck_dir = up_root / f"{course.id}_{uuid.uuid4().hex[:8]}"
    deck_dir.mkdir(parents=True, exist_ok=True)
    out = deck_dir / safe
    out.write_bytes(data)
    body = {
        "pptx": str(out).replace("\\", "/"),
        "pdf": "",
        "html": "",
        "engine": ENGINE_UPLOAD,
        "source": "upload",
        "original_name": safe,
    }
    title = deck_title(scope, course.title, chapter, suffix="（教师上传）")
    meta = {
        "scope": scope,
        "chapter_node_id": chapter.id if chapter is not None else None,
        "chapter_key": chapter.node_key if chapter is not None else None,
        "engine": ENGINE_UPLOAD,
        "theme": "",
        "slide_count": 0,
        "generated_at": _now_iso(),
        "source": {"outline_rev": "", "lecture_ids": [], "chart_ids": [],
                   "style_anchor_ref": "", "concept_ids": []},
    }
    _attach_slides(meta, out)
    meta["slide_count"] = len(meta.get("slides") or [])
    images, images_error = export_deck_pngs(out)
    _apply_slide_images(meta, images)
    if images_error:
        meta["images_error"] = images_error
    existing = _find_deck(db, course, scope, chapter)
    return _upsert(db, course, body, title, meta, existing.id if existing else None,
                   node_id=chapter.id if chapter is not None else None)


def list_decks(db: Session, course_id: str) -> list[ContentItem]:
    return list(db.query(ContentItem).filter(
        ContentItem.course_id == course_id, ContentItem.kind == "ppt",
    ).order_by(ContentItem.created_at).all())
