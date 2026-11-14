# -*- coding: utf-8 -*-
"""PPT v4 版式约束与几何自检（确定性，不依赖渲染引擎回读）。

三件事：

1. **生成前精简** :func:`fit_spec`：标题 ≤27 字、要点 ≤4 条且每条 ≤22 字，
   超出部分自动精简并把原因写进 ``meta.layout_notes``（被砍掉的要点移进备注，不丢内容）；
2. **生成后几何自检** :func:`audit_presentation`：文本∩文本 = 0、文本越界 = 0、
   图片出页 = 0；文本高度按字号与列宽估算折行（CJK 按 1.0 字宽、ASCII 按 0.55）；
3. **不通过时降级** :func:`degrade_spec`：先减要点、再等比缩图；每次降级都记原因。

备注长度统一由 :func:`compose_notes` 控制（150~300 字），保证「口播稿」足够念、
又不至于在页级预览里被截断。
"""
from __future__ import annotations

import math
from itertools import combinations
from pathlib import Path

SLIDE_WIDTH_IN = 13.333
SLIDE_HEIGHT_IN = 7.5
EPS_IN = 0.02

# 标题上限：内页标题最大 32 磅、可用宽 12.23 英寸 ⇒ 单行容量 27.5 个汉字单位。
# 取 27 保证加「（接上页）」后仍单行、不折行、不触发几何自检；任务 146 A1 之前是 24，
# 会把 20 字标题加后缀后截成「…（接上…」，页面无法自述。
MAX_TITLE_CHARS = 27
MAX_POINT_CHARS = 22      # 单条要点上限
MAX_POINTS_PER_SLIDE = 4  # 单页要点条数上限
LINE_SPACING = 1.35
SPACE_AFTER_PT = 8.0

NOTES_LIMIT = 300         # 备注（口播稿）总长上限
NOTES_EXTRA_LIMIT = 140   # 诊断信息（未嵌入的图 / 被精简的要点）上限
SPEAK_MIN = 150           # 口播稿最短长度

# 单条要点的字数上限：知识点页严格 22 字（规格硬要求）；目录 / 回顾 / 封面这类
# 版式的「一句话」本身就长，放宽到 40；练习页题干+选项、提问页问题再放宽。
POINT_CHARS = {"quiz": 80, "questions": 60, "toc": 40, "chapter": 40, "cover": 40,
               "goals": 40, "review": 40, "end": 40}

# 各版式族允许的要点条数（未知版式按 MAX_POINTS_PER_SLIDE）
POINT_LIMITS = {
    "cover": 4, "toc": 6, "goals": 6, "chapter": 5, "point": 4, "figure_only": 0,
    "review": 5, "quiz": 6, "questions": 3, "end": 4,
    # 任务 108：知识点页的三种变体版式，配额与 "point" 一致（每条 ≤22 字、每页 ≤4 条）
    "point_compare": 4, "point_steps": 4, "point_data": 4,
}


def char_units(text: str) -> float:
    """按字宽折算的「字符单位」：CJK/全角算 1.0，ASCII 算 0.55。"""
    total = 0.0
    for ch in str(text or ""):
        total += 1.0 if ord(ch) > 0x2E7F else 0.55
    return total


def est_height_in(lines: list[str], size_pt: float, width_in: float,
                  *, line_spacing: float = LINE_SPACING,
                  space_after_pt: float = SPACE_AFTER_PT) -> float:
    """估算一组文本行在某字号 / 列宽下需要的高度（英寸）。"""
    if not lines:
        return 0.0
    size_pt = max(1.0, float(size_pt or 18))
    capacity = max(1.0, float(width_in) * 72.0 / size_pt)
    total_pt = 0.0
    for line in lines:
        rows = max(1, int(math.ceil(char_units(line) / capacity)))
        total_pt += rows * size_pt * line_spacing
    total_pt += space_after_pt * (len(lines) - 1)
    return total_pt / 72.0


def clip_chars(text: str, limit: int) -> tuple[str, bool]:
    """按「字符单位」截断到 limit（中文按字、英文按 2 字符算 1），返回 (文本, 是否截断)。"""
    raw = str(text or "").strip()
    if char_units(raw) <= limit:
        return raw, False
    out = ""
    used = 0.0
    for ch in raw:
        unit = 1.0 if ord(ch) > 0x2E7F else 0.55
        if used + unit > limit - 1:   # 留一位给省略号
            break
        out += ch
        used += unit
    return (out.rstrip() + "…") if out else raw[:limit], True


def point_limit(layout: str) -> int:
    """该版式族允许的要点条数。"""
    return int(POINT_LIMITS.get(str(layout or ""), MAX_POINTS_PER_SLIDE))


def point_chars(layout: str) -> int:
    """该版式族单条要点的字数上限（练习页题干+选项、提问页问题天然更长）。"""
    return int(POINT_CHARS.get(str(layout or ""), MAX_POINT_CHARS))


def fit_spec(spec: dict, *, layout: str, width_in: float, height_in: float,
             size_pt: float, notes: list[dict], slide_no: int,
             limit_chars: int | None = None, limit_points: int | None = None,
             check_height: bool = True) -> dict:
    """按版式配额精简标题与要点；精简原因写进 notes（调用方落 meta.layout_notes）。

    返回 ``{"clipped": [...], "dropped": [...]}``：被精简掉的原句由调用方移进备注。

    ``limit_points`` 覆盖该版式的要点条数配额（不传就按 point_limit）；
    ``check_height`` 决定是否按「一列文本」估算高度 —— 练习页一页多题时不适用（任务 111：
    它由 ``ppt.quiz_pack_fits`` 的分组判据与生成后几何自检负责，这里再砍一刀会把第 2 题的
    选项当「放不下」移进备注）。
    """
    title = str(spec.get("title") or "")
    clipped: list[str] = []
    new_title, cut = clip_chars(title, MAX_TITLE_CHARS)
    if cut:
        spec["title"] = new_title
        clipped.append(title)
        notes.append({"slide": slide_no, "layout": layout, "kind": "title",
                      "reason": f"标题超长（> {MAX_TITLE_CHARS} 字），已精简为「{new_title}」"})

    bullets = [str(b) for b in (spec.get("bullets") or []) if str(b).strip()]
    chars = int(limit_chars or point_chars(layout))
    limit = int(limit_points) if limit_points else min(
        point_limit(layout), MAX_POINTS_PER_SLIDE if layout == "point" else 99)
    kept: list[str] = []
    dropped: list[str] = []
    long_cut = False
    for line in bullets:
        text, was_cut = clip_chars(line, chars)
        if len(kept) >= limit:
            dropped.append(line)
            continue
        if check_height and kept and est_height_in([*kept, text], size_pt, width_in) > height_in:
            dropped.append(line)
            continue
        kept.append(text)
        long_cut = long_cut or was_cut
    if long_cut:
        notes.append({"slide": slide_no, "layout": layout, "kind": "point",
                      "reason": f"要点超长（> {chars} 字），已精简后放回页面"})
    if dropped:
        notes.append({"slide": slide_no, "layout": layout, "kind": "point",
                      "reason": f"要点过多（{len(bullets)} 条），已保留前 {len(kept)} 条，"
                                f"其余 {len(dropped)} 条移入备注"})
    spec["bullets"] = kept
    return {"clipped": clipped, "dropped": dropped}


def degrade_spec(spec: dict, notes: list[dict], slide_no: int) -> dict | None:
    """几何自检未通过的降级：先减 1 条要点，再等比缩图；返回被移出的要点原句。"""
    bullets = [str(b) for b in (spec.get("bullets") or []) if str(b).strip()]
    if len(bullets) > 1:
        dropped = bullets.pop()
        spec["bullets"] = bullets
        notes.append({"slide": slide_no, "layout": str(spec.get("layout") or ""),
                      "kind": "degrade", "reason": "几何自检未通过，再减少 1 条要点（移入备注）"})
        return {"dropped": [dropped]}
    if spec.get("media"):
        scale = float(spec.get("media_scale") or 1.0)
        if scale > 0.6:
            spec["media_scale"] = round(scale * 0.85, 3)
            notes.append({"slide": slide_no, "layout": str(spec.get("layout") or ""),
                          "kind": "degrade", "reason": "几何自检未通过，图片等比缩小一档"})
            return {"dropped": []}
    return None


def compose_notes(spoken: str, extras: list[str] | None = None,
                  *, limit: int = NOTES_LIMIT) -> str:
    """口播稿 + 诊断信息拼装成一份备注：总长 ≤ limit，诊断信息永远保留在末尾。"""
    extra_text = " ".join(str(e).strip() for e in (extras or []) if str(e).strip())
    if extra_text:
        extra_text = _clip_plain(extra_text, NOTES_EXTRA_LIMIT)
        room = max(SPEAK_MIN, limit - len(extra_text) - 1)
    else:
        room = limit
    body = str(spoken or "").strip()
    if len(body) > room:
        body = body[: max(1, room - 1)].rstrip() + "…"
    return "\n".join(part for part in (body, extra_text) if part)


def _clip_plain(text: str, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# 几何自检（对内存中的 Presentation 或磁盘上的 pptx 都适用）
# ---------------------------------------------------------------------------

def _emu_to_in(value) -> float:
    try:
        return float(value) / 914400.0
    except (TypeError, ValueError):
        return 0.0


def _text_boxes(slide) -> tuple[list[dict], list[dict]]:
    """分拣一页里的文本框（按实际需要的行高）与图/表（按外框）。"""
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    texts: list[dict] = []
    media: list[dict] = []
    for shape in slide.shapes:
        if str(getattr(shape, "name", "")) == "cg-watermark":
            continue
        left, top = _emu_to_in(shape.left), _emu_to_in(shape.top)
        width, height = _emu_to_in(shape.width), _emu_to_in(shape.height)
        box = {"left": left, "top": top, "right": left + width, "bottom": top + height,
               "width": width, "height": height}
        if getattr(shape, "has_chart", False) or getattr(shape, "has_table", False):
            media.append({**box, "label": "图表"})
            continue
        if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            media.append({**box, "label": "图片"})
            continue
        if not getattr(shape, "has_text_frame", False):
            continue
        text = (shape.text_frame.text or "").strip()
        if not text:
            continue
        lines = [p.text for p in shape.text_frame.paragraphs if (p.text or "").strip()]
        sizes = [run.font.size.pt for p in shape.text_frame.paragraphs
                 for run in (p.runs or []) if run.font.size]
        size = max(sizes) if sizes else 18.0
        need = est_height_in(lines, size, max(0.5, width))
        texts.append({**box, "text": text, "need": need, "size": size,
                      "needed_bottom": top + need})
    return texts, media


def _overlap_area(a: dict, b: dict) -> float:
    width = min(a["right"], b["right"]) - max(a["left"], b["left"])
    height = min(a["needed_bottom"], b["needed_bottom"]) - max(a["top"], b["top"])
    return width * height if width > 0 and height > 0 else 0.0


def audit_presentation(prs) -> list[dict]:
    """逐页几何自检：文本∩文本 = 0、文本越界 = 0、图片出页 = 0。"""
    issues: list[dict] = []
    for idx, slide in enumerate(prs.slides, 1):
        texts, media = _text_boxes(slide)
        for box in texts:
            if (box["left"] < -EPS_IN or box["top"] < -EPS_IN
                    or box["right"] > SLIDE_WIDTH_IN + EPS_IN
                    or box["needed_bottom"] > SLIDE_HEIGHT_IN + EPS_IN):
                issues.append({"slide": idx, "kind": "text-out-of-page",
                               "detail": f"文本越界：「{box['text'][:16]}」"})
            elif box["needed_bottom"] > box["bottom"] + 0.06:
                issues.append({"slide": idx, "kind": "text-overflow",
                               "detail": f"文本超出文本框：「{box['text'][:16]}」"})
        for a, b in combinations(texts, 2):
            if _overlap_area(a, b) > 0.01:
                issues.append({"slide": idx, "kind": "text-overlap",
                               "detail": f"文本重叠：「{a['text'][:12]}」∩「{b['text'][:12]}」"})
        for item in media:
            if (item["left"] < -EPS_IN or item["top"] < -EPS_IN
                    or item["right"] > SLIDE_WIDTH_IN + EPS_IN
                    or item["bottom"] > SLIDE_HEIGHT_IN + EPS_IN):
                issues.append({"slide": idx, "kind": "media-out-of-page",
                               "detail": f"{item['label']}出页：右 {item['right']:.2f} / 下 {item['bottom']:.2f} 英寸"})
    return issues


def audit_pptx(pptx_path: str | Path) -> list[dict]:
    """对磁盘上的 pptx 做一次几何自检（与生成时同一套判定）。"""
    from pptx import Presentation

    return audit_presentation(Presentation(str(pptx_path)))
