# -*- coding: utf-8 -*-
"""存量图的「重跑图生成」入口（任务 124，0 LLM、默认只报不写）。

问题（``验收-120-字号四档实测.md``）：任务 120 定的单图容量上限 / 超限拆图只挂在**生成侧**，
而重建课件**只复用库里既有的图元素、不重新生成** ⇒ 存量超限图一张没拆，「页数没涨」
是「没拆」的结果，不能算那一条的成绩。

做法：按当前口径把库里的图逐个复测 —— **超限的、且手里还有原始结构化 data 的**，就地重跑
一遍生成链路（本条原地更新，超限的其余张另建条目）；没有原始 data 的存量图**如实计数**，
不拿渲染后的 elements 反推（反推出来的不是原始 nodes，硬造等于重新编一版内容）。

口径：
- 超限判据有**两条**，正本在 :func:`app.services.figure_png.over_capacity`：图形元素
  （矩形 / 椭圆 / 菱形，计数口径见 ``figure_png.FIGURE_SHAPE_TYPES``）> 6 **或** 文本元素 > 20
  —— 任一超即超限（任务 125 收口：120 定了这两条上限，124 只落了文本那一条）。报告逐条
  记 ``over_text`` / ``over_shape``，好看出是**哪一条**超的。
- 原始 data：``chart_*`` 从任务 64 起就落在 ``body.data``；``diagram`` 从任务 124 起才落
  （见 :func:`app.services.diagram._persist_one`）⇒ 更早的概念图**重跑不了**，要重新生成。
- 拆不动（只有一个节点 / 左右对比 / bar·line·pie·table 这类一张图一件事）**不硬拆**：
  保持原样并计入报告 —— 不许把「装不下」换成「渲染失败」这种退化。
- 幂等：拆完每一张都在上限内 ⇒ 再跑一次 0 处改动（拆不动的那些本来就不动）。

用法见 ``scripts/regenerate_figures.py``（默认 dry-run，``--apply`` 才落库）。
"""
from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from app.models.course import ContentItem, Course
from app.services import chart as chart_svc
from app.services import diagram as diagram_svc
from app.services import figure_png

logger = logging.getLogger(__name__)

#: 条目状态（报告里的 ``state``）
OK = "ok"                          # 未超限，口径内
NO_ELEMENTS = "no-elements"        # 没有可重绘的元素（表格等）
NO_SOURCE = "no-source"            # 超限，但没落原始 data（存量图）
NOT_SPLITTABLE = "not-splittable"  # 超限，但拆不动 / 拆了也不达标
READY = "ready"                    # 超限且拆得动 —— dry-run 报的就是这一档
REFRESHED = "refreshed"            # 已重跑并落库

#: 「存量图配色迁移」（任务 127）的两档状态（其余沿用上面那组）。
PALETTE_STALE = "stale"            # 图内取色不是本主题派生 ⇒ 待迁移
PALETTE_MIGRATED = "migrated"      # 已重映射并落库

_FIGURE_KINDS = ["diagram", *(f"chart_{t}" for t in chart_svc.CHART_TYPES)]


def figure_items(db: Session, course_id: str = "") -> list[ContentItem]:
    """该课程的全部图条目（概念图 + 各类数据图），按创建时间排。"""
    query = db.query(ContentItem).filter(ContentItem.kind.in_(_FIGURE_KINDS))
    if course_id:
        query = query.filter(ContentItem.course_id == course_id)
    return list(query.order_by(ContentItem.created_at).all())


def _source_of(item: ContentItem) -> tuple[str, dict | None]:
    """条目的原始结构化 data（数据图从任务 64 起有；概念图从任务 124 起有）。"""
    kind = str(item.kind)
    data = (item.body or {}).get("data")
    return ("chart" if kind.startswith("chart_") else "diagram",
            data if isinstance(data, dict) and data else None)


def _inspect(item: ContentItem) -> dict:
    """复测一个图条目：超限吗 / 有原始 data 吗 / 拆得动且拆完达标吗（只算，不落库）。"""
    body = item.body or {}
    kind = str(item.kind)
    row = {"id": item.id, "kind": kind, "title": str(item.title or ""),
           "node_id": item.node_id, "layout": str(body.get("layout")
                                                   or body.get("chart_type") or ""),
           "source": "chart" if kind.startswith("chart_") else "diagram",
           "texts": 0, "shapes": 0, "over_text": False, "over_shape": False,
           "over": False, "state": OK, "parts": 0, "reason": ""}
    try:
        elements = figure_png.elements_of(item)
    except Exception:  # noqa: BLE001 —— 没有可重绘的元素（表格等）
        row["state"] = NO_ELEMENTS
        return row
    row["texts"] = figure_png.figure_text_count(elements)
    row["shapes"] = figure_png.figure_shape_count(elements)
    row["over_text"] = row["texts"] > figure_png.MAX_FIGURE_TEXT_ELEMENTS
    row["over_shape"] = row["shapes"] > figure_png.MAX_FIGURE_SHAPES
    if not (row["over_text"] or row["over_shape"]):
        return row
    row["over"] = True
    _source, data = _source_of(item)
    if data is None:
        row["state"] = NO_SOURCE
        row["reason"] = ("没有原始 data（概念图从任务 124 起才落 body.data）⇒ 只能重新生成，"
                         "不能重跑")
        return row
    try:
        if row["source"] == "chart":
            plans = chart_svc.split_chart_by_capacity(
                str(body.get("chart_type") or ""), data,
                str(body.get("title") or item.title or ""))
            fits = all(chart_svc.chart_fits(p["chart_type"], p["data"], p["title"])
                       for p in plans)
        else:
            plans = diagram_svc.split_scene_by_capacity(
                str(body.get("layout") or ""), data, str(body.get("caption") or ""))
            fits = all(diagram_svc.scene_fits(p["layout"], p["data"], p["caption"])
                       for p in plans)
    except Exception as exc:  # noqa: BLE001 —— 数据不合格 / 渲染失败：如实报，不改动
        row["state"] = NOT_SPLITTABLE
        row["reason"] = f"重算失败（数据不合格或渲染不过自检）：{exc}"
        return row
    row["parts"] = len(plans)
    if len(plans) < 2:
        row["state"] = NOT_SPLITTABLE
        row["reason"] = "拆不动（节点太少 / 结构固定 / 一张图一件事）"
        return row
    if not fits:
        row["state"] = NOT_SPLITTABLE
        row["reason"] = f"拆到 {len(plans)} 张仍超上限"
        return row
    row["state"] = READY
    return row


def _tally(rows: list[dict]) -> dict:
    """汇总计数（报告表头）。"""
    return {
        "figures": len(rows),
        "over": sum(1 for r in rows if r["over"]),
        "over_text": sum(1 for r in rows if r.get("over_text")),
        "over_shape": sum(1 for r in rows if r.get("over_shape")),
        "refreshable": sum(1 for r in rows if r["state"] == READY),
        "refreshed": sum(1 for r in rows if r["state"] == REFRESHED),
        "new_parts": 0,
        "no_source": sum(1 for r in rows if r["state"] == NO_SOURCE),
        "not_splittable": sum(1 for r in rows if r["state"] == NOT_SPLITTABLE),
        "no_elements": sum(1 for r in rows if r["state"] == NO_ELEMENTS),
    }


def _scan(db: Session, course_id: str) -> tuple[list[ContentItem], list[dict]]:
    items = figure_items(db, course_id)
    return items, [_inspect(item) for item in items]


def capacity_report(db: Session, course_id: str = "") -> dict:
    """只读盘点：每个图条目按当前口径复测的结果 + 汇总计数（不写库）。"""
    _items, rows = _scan(db, course_id)
    return {"apply": False, "rows": rows, **_tally(rows)}


def refresh_over_capacity_figures(db: Session, course_id: str = "",
                                  *, apply: bool = False) -> dict:
    """重跑超限图（``apply=False`` 只报告、不写库）。

    返回报告：``figures / over / refreshable / refreshed / new_parts / no_source /
    not_splittable / no_elements`` + 逐条明细 ``rows``（状态与理由）+ ``after``（重跑后的
    复测汇总，``apply`` 时才有）。
    """
    if course_id and db.get(Course, course_id) is None:
        raise ValueError(f"课程不存在：{course_id}")
    items, rows = _scan(db, course_id)
    report = {"apply": bool(apply), "rows": rows, **_tally(rows)}
    if not apply:
        return report
    new_parts = 0
    for item, row in zip(items, rows):
        if row["state"] != READY:
            continue
        try:
            parts = (chart_svc.rerender_chart_item(db, item) if row["source"] == "chart"
                     else diagram_svc.rerender_diagram_item(db, item))
        except Exception as exc:  # noqa: BLE001 —— 单张失败不拖垮整轮，如实记账
            row["state"] = NOT_SPLITTABLE
            row["reason"] = f"重跑失败：{exc}"
            logger.warning("figure regen failed for %s: %s", item.id, exc)
            continue
        row["state"] = REFRESHED
        row["parts"] = len(parts)
        new_parts += max(0, len(parts) - 1)
    report["new_parts"] = new_parts
    report["refreshed"] = sum(1 for r in rows if r["state"] == REFRESHED)
    _after_items, after_rows = _scan(db, course_id)
    report["after"] = _tally(after_rows)
    return report


# —— 存量图配色迁移（任务 127，一次性）——
# 问题（``验收-121-122-独立复测`` ②）：任务 121 把图内取色改成「从 PPT 主题派生」只改了
# **生成侧**；任务 124 之前落库的概念图没有 ``body.data`` ⇒ 重跑不了，元素里仍是当年写死的
# pastel（PASTEL / ACCENTS / TIP_ACCENT / _MINDMAP_GREENS / 流程图三套）。真课 13 册上页的
# 89 张位图里 60 张含非主题色，用户一眼就能看出「图里是另一套色」。
#
# 做法：按当前口径把库里的图逐个复测取色，**含非主题色的就地重映射**到目标主题（同色相 +
# 同明度档，几何 / 文本 / 字号一个字节不动 —— 见 :func:`figure_png.recolor_elements`），
# 并同步重写图文原件旁的 SVG 预览；理由逐条写进 ``meta.layout_notes``。
# **不反推 ``body.data``、不重建内容**（反推出来的不是原始 nodes，硬造等于重新编一版内容，
# 与 :func:`refresh_over_capacity_figures` 同一条既有口径）；**不动讲义正文**。
# 幂等：已经是本主题派生色的图再次运行 0 处改动（迁移前后各自复测取色）。


def _swap_alive(original: list, recolored: list[dict]) -> list:
    """把重映射后的元素换回原件里**未删除**的那几条（顺序 / 已删除的 / 其它元素都不动）。"""
    out: list = []
    index = 0
    for el in original or []:
        if isinstance(el, dict) and not el.get("isDeleted"):
            out.append(recolored[index] if index < len(recolored) else el)
            index += 1
        else:
            out.append(el)
    return out


def _document_of(raw) -> tuple[dict, list]:
    """``body.excalidraw_json`` → ``(整个原件, 元素列表)``；格式不认识就给 ``({}, [])``。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}, []
    if isinstance(raw, dict) and isinstance(raw.get("elements"), list):
        return raw, list(raw["elements"])
    if isinstance(raw, list):
        return {}, list(raw)
    return {}, []


def _apply_palette(item: ContentItem, recolored: list[dict], *,
                   palette) -> None:
    """就地写回：元素取色 + SVG 预览 + ``meta.layout_notes``（不动 data / 正文 / 其它字段）。"""
    body = dict(item.body or {})
    doc, original = _document_of(body.get("excalidraw_json"))
    merged = _swap_alive(original, recolored)
    if doc:
        doc = dict(doc)
        doc["elements"] = merged
        body["excalidraw_json"] = doc
    elif original:
        body["excalidraw_json"] = merged
    if merged:
        svg = diagram_svc.diagram_to_svg(merged, background=palette.bg)
        body["svg_preview"] = diagram_svc.write_svg(svg, str(body.get("svg_preview") or ""))
    item.body = body
    meta = dict(item.meta or {})
    meta["layout_notes"] = [{
        "kind": "figure-palette",
        "reason": (f"存量图取色迁移（任务 127）：本图是任务 124 之前的存量图（没有原始 data，"
                   f"重跑不了），{len(recolored)} 个元素的旧写死色板按同色相 + 同明度档重映射到"
                   f"「{palette.theme}」主题派生色（几何 / 文本 / 字号未动，未反推 data、未动讲义正文）"),
    }, *list(meta.get("layout_notes") or [])]
    item.meta = meta


def migrate_figure_colors(db: Session, course_id: str = "", *,
                          theme: str | None = None, apply: bool = False) -> dict:
    """存量图配色迁移：把「图内取色不是本主题派生」的图条目重映射到主题色（默认只报不写）。

    ``apply=False`` 只盘点（不连写操作、不改文件）；``apply=True`` 就地写回元素取色 + SVG 预览
    + ``meta.layout_notes``。返回报告：``figures / stale / migrated / no_elements /
    unknown_before / unknown_after / theme`` + 逐条明细 ``rows``（每条给出迁移前后的未知色个数
    与色值，便于人工核对「还剩几张、为什么」）。
    """
    if course_id and db.get(Course, course_id) is None:
        raise ValueError(f"课程不存在：{course_id}")
    palette = figure_png.figure_palette(theme)
    rows: list[dict] = []
    for item in figure_items(db, course_id):
        row = {"id": item.id, "kind": str(item.kind), "title": str(item.title or ""),
               "theme": palette.theme, "unknown_before": [], "unknown_after": [],
               "elements": 0, "state": OK, "reason": ""}
        try:
            elements = figure_png.elements_of(item)
        except figure_png.FigureError:
            row["state"] = NO_ELEMENTS
            row["reason"] = "没有可重绘的 Excalidraw 元素（如表格）"
            rows.append(row)
            continue
        row["elements"] = len(elements)
        row["unknown_before"] = figure_png.figure_color_audit(
            elements, theme=palette.theme)["unknown"]
        if not row["unknown_before"]:
            rows.append(row)
            continue
        recolored, changed = figure_png.recolor_elements(elements, theme=palette.theme)
        row["unknown_after"] = figure_png.figure_color_audit(
            recolored, theme=palette.theme)["unknown"]
        row["changed"] = changed
        row["state"] = PALETTE_STALE
        row["reason"] = f"图内 {len(row['unknown_before'])} 个色值不是本主题派生"
        if apply and changed:
            try:
                _apply_palette(item, recolored, palette=palette)
                db.commit()
            except Exception as exc:  # noqa: BLE001 —— 单张失败不拖垮整轮，如实记账
                db.rollback()
                row["state"] = NOT_SPLITTABLE
                row["reason"] = f"迁移失败：{exc}"
                logger.warning("figure palette migration failed for %s: %s", item.id, exc)
                rows.append(row)
                continue
            row["state"] = PALETTE_MIGRATED
        rows.append(row)
    report = {
        "apply": bool(apply), "rows": rows, "theme": palette.theme,
        "figures": len(rows),
        "stale": sum(1 for r in rows if r["state"] in (PALETTE_STALE, PALETTE_MIGRATED)),
        "migrated": sum(1 for r in rows if r["state"] == PALETTE_MIGRATED),
        "no_elements": sum(1 for r in rows if r["state"] == NO_ELEMENTS),
        "unknown_before": sum(len(r["unknown_before"]) for r in rows),
        "unknown_after": sum(len(r["unknown_after"]) for r in rows),
    }
    return report
