# -*- coding: utf-8 -*-
"""教学图表服务：柱状/折线/饼图 + 思维导图/时间轴/表格（自动配图与网页手工共用）。

- chart_type 落 ContentItem.kind：chart_bar / chart_line / chart_pie /
  chart_mindmap / chart_timeline / chart_table / chart_quadrant。
- bar/line/pie：Chart.js 结构化 data + Excalidraw 矢量小图（body.excalidraw_json
  与 svg_preview），网页端仍用 Chart.js 交互渲染。
- mindmap/timeline：改为 Excalidraw 可编辑版（body.excalidraw_json + svg_preview），
  body.mermaid 保留备用（网页端 Mermaid 预览与导出兼容旧版）。
- quadrant（任务 122）：两轴四象限（十字轴线 + 四枚角标签 + 四张象限卡），
  Excalidraw 可编辑版，用于「重要性 × 紧急度」「成本 × 效果」这类比较与决策语境。
- table：直接出 Markdown 表格（body.markdown_table），导出时原样写进笔记。
- 生成成功后自动增量写入 Vault（失败不影响主流程）。
"""
from __future__ import annotations

import logging
import re
import uuid

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.course import ContentItem, Course, OutlineNode
from app.services import usage as usage_log
from app.services.chart_data import CHART_DATA_ISSUES_KEY, normalize_chart_data
from app.services.diagram import diagram_to_svg
from app.services.excalidraw_chart import chart_to_excalidraw_doc
from app.services.figure_png import (MAX_FIGURE_PARTS, MAX_FIGURE_SHAPES,
                                     MAX_FIGURE_TEXT_ELEMENTS, figure_palette,
                                     over_capacity, partition_balanced)
from app.services.kb import attach_vault_write
from app.services.llm import build_llm_client

logger = logging.getLogger(__name__)

CHART_TYPES = ("bar", "line", "pie", "mindmap", "timeline", "table", "quadrant")

# 任务 121：数据图的 Excalidraw 小图与 chart.js 的上色**都从 PPT 主题派生**（图内不再自带色板；
# 口径正本 = :func:`app.services.figure_png.figure_palette`）。各入口的 ``theme`` 参数留空时用
# 后端 ``PPT_THEME`` 配置；取色不参与几何 ⇒ 拆图判据与磅数不受影响。

CHART_SYSTEM = """CHART_JSON
你是资深教学图表设计师。根据课程背景与讲义正文生成教学图表数据，只输出 JSON（不要代码块）。
格式约定：
- 类型为 bar / line / pie 时：
  {"title": "图表标题", "data": {"labels": ["类别..."], "datasets": [{"label": "系列名", "data": [数值...]}]}}
- 类型为 mindmap 时：
  {"title": "图表标题",
   "data": {"root": "中心主题",
            "branches": [{"emoji": "单个字符或省略", "title": "一级分支（≤10字）",
                          "subs": ["二级要点 1~2 条，每条 ≤14字"]}]}}
- 类型为 timeline 时：
  {"title": "图表标题",
   "data": {"steps": [{"emoji": "单个字符或省略", "title": "阶段名（≤10字）",
                       "subs": ["该阶段要点 1~2 条，每条 ≤14字"]}]}}
- 类型为 table 时：
  {"title": "表格标题", "data": {"headers": ["列1", "列2"], "rows": [["值", "值"], ...]}}
- 类型为 quadrant（两轴四象限：重要性 × 紧急度、成本 × 效果这类**比较与决策**）时：
  {"title": "图表标题",
   "data": {"x_low": "横轴左端标签（≤4字）", "x_high": "横轴右端标签（≤4字）",
            "y_low": "纵轴下端标签（≤4字）", "y_high": "纵轴上端标签（≤4字）",
            "quadrants": [{"name": "象限名（≤8字）", "desc": "一句话说明（≤12字）"}]}}
要求：内容准确、贴合教学主题、语言为中文；bar/line/pie 的 labels 与数据点数量控制在 3-12，
数值必须是讲义/素材中实际给出的数据，禁止编造；表格行列数控制在 2-10；
mindmap 分支 3~6 个；timeline 阶段 3~6 个；quadrant 的 quadrants 必须给满 4 个、
顺序固定为**左上 → 右上 → 左下 → 右下**（四枚角标签就是两个轴的两端），
象限名与说明必须来自讲义里的观点，禁止编造。"""


def _markdown_table(data: dict) -> str:
    """把 chart_table 的结构数据渲染成 Markdown 表格文本。"""
    headers = [str(h) for h in (data.get("headers") or [])]
    rows = data.get("rows") or []
    if not headers:
        return ""
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "---|" * len(headers)]
    for row in rows:
        cells = [str(c).replace("|", "\\|") for c in row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _derive_mermaid(chart_type: str, data: dict) -> str:
    """从结构化数据回推 Mermaid 源码（保留给网页端/旧版渲染用）。"""
    if chart_type == "mindmap":
        raw_root = data.get("root")
        if isinstance(raw_root, dict):
            raw_root = raw_root.get("title") or ""
        root = str(raw_root or "主题")[:20]
        lines = ["mindmap", f"  {root}"]
        for branch in (data.get("branches") or [])[:6]:
            if not isinstance(branch, dict):
                continue
            title = str(branch.get("title") or "").strip()
            if not title:
                continue
            lines.append(f"    {title}")
            for sub in (branch.get("subs") or [])[:2]:
                lines.append(f"      {sub}")
        return "\n".join(lines)
    if chart_type == "timeline":
        steps = (data.get("steps") or [])[:6]
        lines = ["timeline", f"    title {str(data.get('title') or '进程阶段')[:24]}"]
        for i, step in enumerate(steps, 1):
            if not isinstance(step, dict):
                continue
            title = str(step.get("title") or "").strip() or f"第 {i} 阶段"
            subs = [str(s).strip() for s in (step.get("subs") or []) if str(s).strip()][:2]
            line = f"    阶段{i} : {title}"
            for s in subs:
                line += f" : {s}"
            lines.append(line)
        return "\n".join(lines)
    return ""


def _write_svg(svg: str, rel: str | None = None) -> str:
    """把 Excalidraw 场景渲染出的 SVG 写入 storage/images/，返回相对路径。

    ``rel`` 给了就**就地覆盖**那条既有缓存（任务 92 的重渲染口径 ⇒ 重跑图生成不留孤儿文件）。
    """
    settings = get_settings()
    out_dir = settings.storage_dir / "images"
    out_dir.mkdir(parents=True, exist_ok=True)
    name = str(rel).rsplit("/", 1)[-1] if rel else f"{uuid.uuid4().hex}.svg"
    (out_dir / name).write_text(svg, encoding="utf-8")
    return f"images/{name}"


def _chart_payload(chart_type: str, title: str, instruction: str, data: dict,
                   source: str, svg_rel: str | None = None, *,
                   theme: str | None = None) -> tuple[dict, str, list[dict]]:
    """生成 chart 条目的 body 与展示标题：按类型补齐 Excalidraw/SVG/表格/mermaid。

    bar/line/pie 先过 :func:`app.services.chart_data.normalize_chart_data`：
    labels 与每条 data 长度对齐（否则网页端柱子与标签错位），并按当前主题色板写
    `borderColor`/`backgroundColor`（否则 chart.js 默认灰，与 PPT/概念图不一致）。
    返回 ``(body, 展示标题, 数据问题清单)``；问题清单由落库方写进 ``meta.chart_data_issues``。
    ``svg_rel`` 给了就把 SVG 写回那条既有缓存（重跑图生成就地覆盖 ⇒ 不留孤儿文件）。
    """
    pal = figure_palette(theme)
    data = dict(data or {})
    issues: list[dict] = []
    if chart_type in ("bar", "line", "pie"):
        # chart_data 的口径是**不带 # 的大写裸 hex**（与 chart_data.active_palette 同形，
        # 见其 ``f"#{color}"``）；这里从主题派生同一份色板 ⇒ chart.js 与图内两边同色。
        data, issues = normalize_chart_data(chart_type, data,
                                            [c.lstrip("#").upper() for c in pal.colors])
    body: dict = {
        "chart_type": chart_type,
        "title": title,
        "instruction": instruction,
        "data": data,
        "source": source,
    }
    if chart_type == "table":
        body["markdown_table"] = _markdown_table(data)
        return body, title, issues
    if chart_type in ("mindmap", "timeline"):
        mermaid = str(data.get("mermaid") or "").strip()
        if mermaid and not data.get("branches") and not data.get("steps"):
            # 旧版只给 Mermaid 源：保留兼容，不强行渲染 Excalidraw
            body["mermaid"] = mermaid
            return body, title, issues
    try:
        doc = chart_to_excalidraw_doc(chart_type, data, title, theme=pal.theme)
    except Exception as exc:  # noqa: BLE001 —— 结构化数据不合格则整单失败
        raise ValueError(f"{chart_type} 图表数据不完整：{exc}") from exc
    elements = (doc or {}).get("elements") or []
    svg = diagram_to_svg(elements, background=pal.bg) if elements else ""
    if svg:
        body["svg_preview"] = _write_svg(svg, svg_rel)
        body["excalidraw_json"] = doc
    if chart_type in ("mindmap", "timeline"):
        mermaid = str(body.get("mermaid") or "").strip() or _derive_mermaid(chart_type, data)
        if mermaid:
            body["mermaid"] = mermaid
    return body, title, issues


#: 能「超限拆图」的图表类型 → data 里装着节点的字段。bar / line / pie / table 是「一张图
#: 一件事」，拆开就不成图 ⇒ 不拆（保持原样，并在重跑报告里如实标注，任务 124）。
_CHART_NODE_KEY = {"mindmap": "branches", "timeline": "steps"}
#: 拆分后的条目标题后缀：「（图 i/N）」只加一次（重跑幂等的前提之一）。
_PART_SUFFIX_RE = re.compile(r"（图 \d+/\d+）$")
#: 另建的「图 2…」要继承的归属字段（与 diagram._CARD_META_KEYS 同一份口径）。
_CARD_META_KEYS = ("from_lecture", "role", "card_anchor", "card_seq", "chart_type", "node_key")


def chart_elements(chart_type: str, data: dict, title: str, *,
                   theme: str | None = None) -> list[dict]:
    """按既有渲染链路出一张数据图的元素（容量判定 / 拆图验证共用；不落库、不写文件）。"""
    doc = chart_to_excalidraw_doc(chart_type, data, title, theme=theme)
    return list((doc or {}).get("elements") or [])


def chart_fits(chart_type: str, data: dict, title: str) -> bool:
    """这张数据图落进页面后是否不超单图容量上限（判据正本：:func:`figure_png.over_capacity`）。"""
    return not over_capacity(chart_elements(chart_type, data, title))


def split_chart_by_capacity(chart_type: str, data: dict, title: str,
                            *, max_parts: int = MAX_FIGURE_PARTS) -> list[dict]:
    """单图容量上限（任务 120 的口径，任务 124 补上数据图这一侧）：超限就均分拆成 2~3 张。

    与 :func:`app.services.diagram.split_scene_by_capacity` 同一口径：返回
    ``[{chart_type, data, title, part, parts, note}]``（没超 / 拆不动时原样一张，note=None），
    **绝不截断内容**（旧的 ``_mindmap_elements`` / ``_timeline_elements`` 会把第 7 个节点
    直接切掉，拆图正是为了不再切）。
    """
    base = {"chart_type": chart_type, "data": dict(data or {}), "title": title,
            "part": 1, "parts": 1, "note": None}
    key = _CHART_NODE_KEY.get(chart_type)
    nodes = list((data or {}).get(key) or []) if key else []
    if not key or len(nodes) < 2 or chart_fits(chart_type, data, title):
        return [base]
    best: list[dict] | None = None
    for parts in range(2, int(max_parts) + 1):
        groups = partition_balanced(nodes, parts)
        if len(groups) < 2:
            break
        cand: list[dict] = []
        for i, group in enumerate(groups, 1):
            part_data = dict(data)
            part_data[key] = group
            cand.append({"chart_type": chart_type, "data": part_data, "title": title,
                         "part": i, "parts": parts, "note": None})
        best = cand
        if all(chart_fits(chart_type, c["data"], title) for c in cand):
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


def _part_title(title: str, part: int, parts: int) -> str:
    """拆图后的图表标题（``（图 i/N）`` 只加一次；整条标题仍 ≤ 80 字）。"""
    base = _PART_SUFFIX_RE.sub("", str(title or ""))
    return base if parts <= 1 else f"{base[:70]}（图 {part}/{parts}）"


def _card_meta(item: ContentItem) -> dict:
    """另建的那几张图要继承的归属字段（讲义引用 / 卡片序号那一套）。"""
    meta = dict(item.meta or {})
    return {key: meta[key] for key in _CARD_META_KEYS if key in meta}


def _persist_chart_one(
    db: Session,
    course: Course,
    node: OutlineNode | None,
    chart_type: str,
    caption: str,
    data: dict,
    *,
    title: str,
    instruction: str,
    source: str,
    meta_extra: dict | None = None,
    theme: str | None = None,
) -> ContentItem:
    """渲染**一张**数据图并落库（不调 LLM）；拆图后的每一张都走这条链路。"""
    body, _, issues = _chart_payload(chart_type, str(title)[:80], instruction, data, source,
                                     theme=theme)
    node_name = (node.title if node is not None else course.title) or course.title
    item = ContentItem(
        course_id=course.id,
        kind=f"chart_{chart_type}",
        node_id=node.id if node is not None else None,
        title=str(title)[:80],
        body=body,
        meta={"node_key": node.node_key if node is not None else "",
              "chart_type": chart_type, "caption": str(caption or "")[:120],
              CHART_DATA_ISSUES_KEY: issues},
    )
    if meta_extra:
        item.meta = {**dict(item.meta or {}), **meta_extra}
    db.add(item)
    db.commit()
    db.refresh(item)
    try:
        attach_vault_write(db, item, "chart", {"chart_type": chart_type})
    except Exception:  # noqa: BLE001 —— Vault 增量失败不影响主流程
        logger.warning("vault incremental write skipped for chart %s", item.id)
    return item


def persist_charts_from_data(
    db: Session,
    course: Course,
    node: OutlineNode | None,
    chart_type: str,
    caption: str,
    data: dict,
    *,
    title: str | None = None,
    instruction: str = "",
    source: str = "auto-card",
    meta_extra: dict | None = None,
    split: bool = True,
    theme: str | None = None,
) -> list[ContentItem]:
    """单图容量上限的执行点（任务 124）：超限的数据图拆成 2~3 张落库（第一张是主图）。

    ``split=False`` 只画一张（对照用，也是反证据用例的开关）——与
    :func:`app.services.diagram.persist_diagrams_from_data` 同一口径。
    """
    if chart_type not in CHART_TYPES:
        raise ValueError(f"不支持的图表类型：{chart_type}（可选：{', '.join(CHART_TYPES)}）")
    disp_title = str(title or caption or f"{chart_type} 教学图表")[:80]
    plans = split_chart_by_capacity(chart_type, data, disp_title) if split else [
        {"chart_type": chart_type, "data": data, "title": disp_title,
         "part": 1, "parts": 1, "note": None}]
    items: list[ContentItem] = []
    for plan in plans:
        extra = dict(meta_extra or {})
        if plan["note"]:
            extra["layout_notes"] = [plan["note"], *list(extra.get("layout_notes") or [])]
        items.append(_persist_chart_one(
            db, course, node, chart_type, caption, plan["data"],
            title=_part_title(disp_title, plan["part"], plan["parts"]),
            instruction=instruction, source=source, meta_extra=extra or None,
            theme=theme))
    return items


def persist_chart_from_data(
    db: Session,
    course: Course,
    node: OutlineNode | None,
    chart_type: str,
    caption: str,
    data: dict,
    *,
    title: str | None = None,
    instruction: str = "",
    source: str = "auto-card",
    meta_extra: dict | None = None,
    split: bool = True,
    theme: str | None = None,
) -> ContentItem:
    """把已定稿的结构化图表数据落库（不调 LLM），返回 kind="chart_*" 条目。

    讲义「概念卡按内容自动分流数据图」与网页端手工生成共用此渲染链路；
    meta_extra 支持携带 from_lecture / role=card 等归属信息，供清理与导出区分。
    任务 124：超限（文本元素 > 20，口径正本 figure_png）时拆成 2~3 张并返回第一张
    —— 与 :func:`app.services.diagram.persist_diagram_from_data` 同一口径。
    """
    return persist_charts_from_data(
        db, course, node, chart_type, caption, data, title=title, instruction=instruction,
        source=source, meta_extra=meta_extra, split=split, theme=theme)[0]


def rerender_chart_item(db: Session, item: ContentItem, *,
                        split: bool = True, theme: str | None = None) -> list[ContentItem]:
    """按条目自带的原始 data **就地重跑**图表生成（0 LLM）：本条原地更新，超限的其余张另建。

    与 :func:`app.services.diagram.rerender_diagram_item` 同一口径：本条图的 ``id`` /
    讲义里的 ``chart://<id>`` 引用**都不动**，``svg_preview`` 就地覆盖（不留孤儿文件）。
    数据图的原始 ``data`` 从任务 64 起就落在 ``body.data`` ⇒ 存量数据图能重跑。
    """
    body0 = dict(item.body or {})
    chart_type = str(body0.get("chart_type") or "")
    if chart_type not in CHART_TYPES:
        raise ValueError(f"chart {item.id} 的 chart_type 不合法：{chart_type!r}")
    data = body0.get("data")
    if not isinstance(data, dict) or not data:
        raise ValueError(f"chart {item.id} 没有原始 data：无法重跑图生成")
    course = db.get(Course, item.course_id)
    node = db.get(OutlineNode, item.node_id) if item.node_id else None
    disp_title = str(body0.get("title") or item.title or "")
    instruction = str(body0.get("instruction") or "")
    source = str(body0.get("source") or "auto-card")
    caption = str((item.meta or {}).get("caption") or disp_title)
    plans = split_chart_by_capacity(chart_type, data, disp_title) if split else [
        {"chart_type": chart_type, "data": data, "title": disp_title,
         "part": 1, "parts": 1, "note": None}]
    first, rest = plans[0], plans[1:]
    # 拆图后第 1 张也要标分册（任务 125）：条目标题与图内标题同一条 _part_title 口径
    # （第 2 张早就带「（图 2/2）」），先剥旧后缀再加 ⇒ 重跑幂等。id 与讲义里的
    # chart://<id> 引用不动（引用按 id 认，不按标题）。
    first_title = (_part_title(disp_title, first["part"], first["parts"])
                   if len(plans) > 1 else disp_title)
    body, _, issues = _chart_payload(
        chart_type, first_title, instruction, first["data"], source,
        svg_rel=str(body0.get("svg_preview") or "").strip() or None, theme=theme)
    item.body = body
    if first_title != disp_title:
        item.title = first_title
    meta = dict(item.meta or {})
    meta[CHART_DATA_ISSUES_KEY] = issues
    if first["note"]:
        meta["layout_notes"] = [first["note"], *list(meta.get("layout_notes") or [])]
    item.meta = meta
    db.commit()
    db.refresh(item)
    try:
        attach_vault_write(db, item, "chart", {"chart_type": chart_type})
    except Exception:  # noqa: BLE001 —— Vault 增量失败不影响主流程
        logger.warning("vault incremental write skipped for chart %s", item.id)
    extra: list[ContentItem] = []
    for plan in rest:
        extra.append(_persist_chart_one(
            db, course, node, chart_type, caption, plan["data"],
            title=_part_title(disp_title, plan["part"], plan["parts"]),
            instruction=instruction, source=source, meta_extra=_card_meta(item),
            theme=theme))
    return [item, *extra]


def generate_chart(
    db: Session,
    course: Course,
    node: OutlineNode | None,
    chart_type: str,
    instruction: str,
    item_id: str | None = None,
) -> ContentItem:
    """按 chart_type 生成图表数据并落库（原地替换 item_id / 同类型重做）。"""
    with usage_log.collect() as collector:
        item = _generate_chart_inner(db, course, node, chart_type, instruction, item_id)
        if collector.depth == 1 and usage_log.attach(item, collector) is not None:
            db.commit()
        return item


def _generate_chart_inner(
    db: Session,
    course: Course,
    node: OutlineNode | None,
    chart_type: str,
    instruction: str,
    item_id: str | None = None,
) -> ContentItem:
    if chart_type not in CHART_TYPES:
        raise ValueError(f"不支持的图表类型：{chart_type}（可选：{', '.join(CHART_TYPES)}）")
    client = build_llm_client()
    topic = f"{node.title}（{node.objective or '教学要点'}）" if node else course.title
    user = (
        f"课程：{course.title}\n"
        f"受众：{course.audience or '未指定'}\n"
        f"对应知识点：{topic}\n"
        f"图类型：{chart_type}\n"
        f"要求：{instruction}"
    )
    data = usage_log.complete_json(client, CHART_SYSTEM, user,
                                   max_tokens=2000, purpose="chart")
    title = str(data.get("title") or instruction or f"{chart_type} 教学图表")[:80]
    chart_data = data.get("data") or {}

    node_ref = _resolve_node(db, course, node.id if node else None)
    item = _target_item(db, course, item_id, chart_type)
    if item is None:
        return persist_chart_from_data(
            db, course, node_ref, chart_type, title, chart_data,
            title=title, instruction=instruction, source="generate",
        )
    history = list(item.meta.get("history") or [])
    history.insert(0, {"body": item.body})
    item.meta = {**(item.meta or {}), "history": history[:5]}
    if node_ref is not None:
        item.node_id = node_ref.id
    item.title = title
    body, _, issues = _chart_payload(chart_type, title, instruction, chart_data, "generate")
    item.body = body
    item.meta = {**(item.meta or {}), CHART_DATA_ISSUES_KEY: issues}
    db.commit()
    db.refresh(item)
    try:
        attach_vault_write(db, item, "chart", {"chart_type": chart_type})
    except Exception:  # noqa: BLE001 —— Vault 增量失败不影响主流程
        logger.warning("vault incremental write skipped for chart %s", item.id)
    return item


def _resolve_node(db: Session, course: Course, node_id: str | None) -> OutlineNode | None:
    if not node_id:
        return None
    node = db.get(OutlineNode, node_id)
    if node is None or node.outline is None or node.outline.course_id != course.id:
        raise ValueError("所选节点不属于当前课程，请重新选择")
    return node


def _target_item(db: Session, course: Course, item_id: str | None, chart_type: str) -> ContentItem | None:
    if not item_id:
        return None
    item = db.get(ContentItem, item_id)
    if item is None or item.course_id != course.id or item.kind != f"chart_{chart_type}":
        raise ValueError(f"内容项不存在或类型不符：{item_id}")
    return item
