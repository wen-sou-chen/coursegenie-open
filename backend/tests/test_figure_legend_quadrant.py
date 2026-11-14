# -*- coding: utf-8 -*-
"""任务 122：图例条（legend）+ 象限图（quadrant）—— 概念图版式与数据图类型各补一样。

规格正本：``给Codex-2026-10-02-任务120-122.md`` 的「任务 122」；
设计正本：``研究报告-图示方法能否用进我们的应用.md``（图例条 / 象限图两节）。

本文件钉死四件事：
  ① **图例一个条目 = 一个 text 元素**（色块 = 行首几何字符，色在字上）⇒ 图形个数与线性
     元素个数都不涨，加图例不会把图推去拆图；
  ② **既有 8 种版式的几何零回归**：chain / timeline / compare / decision 加图例前后宽 / 高 /
     **渲染口径**（几何重排后）的最小等效磅数与**图形个数**一字不变，只有文本数 +1~2。
     磅数不变是「不改版式行为」的硬口径 —— ``ppt.media_tier_of`` 只用宽度 + 最小磅数选档，
     磅数不变 ⇒ 档位与页面组成逐项不变（图例字号按本图反算，见 ``_legend_font_units``）；
  ③ **象限图**：图形 4（≤6）/ 文本 13（≤20）、四格按「左上 → 右上 → 左下 → 右下」、
     ``validate_elements`` 0 问题、色从 PPT 主题派生、容量判据不触发拆图；
  ④ **判据与类型表三处一致**：``chart.CHART_TYPES`` / ``CHART_SYSTEM`` / ``lecture.CARDS_SYSTEM``。

反证据（本文件即证据）：把 ``quadrant`` 从 ``CHART_TYPES`` 或 ``_ELEMENT_BUILDERS`` 拿掉，
或删掉两处提示词里的 quadrant 判据行 ⇒ 下面 4 个用例立刻红（实测记录见
``backend/docs/task122-legend-quadrant.md``）。

硬边界：只测图生成侧；不动 ``MIN_TEXT_PT=9`` / ``MIN_MEDIA_WIDTH_IN=6.00`` /
``MAX_DECOR_PER_SLIDE=14``，也不动 ``MAX_FIGURE_SHAPES=6`` / ``MAX_FIGURE_TEXT_ELEMENTS=20``。
"""
from __future__ import annotations

import pytest

from app.services import chart as chart_svc
from app.services import excalidraw_layout as L
from app.services import figure_png as F
from app.services import lecture as lecture_svc
from app.services.excalidraw_chart import (CHART_SLOT, _ELEMENT_BUILDERS,
                                            chart_to_excalidraw_doc)

QUADRANT_DATA = {
    "x_low": "不紧急", "x_high": "紧急", "y_low": "不重要", "y_high": "重要",
    "quadrants": [{"name": "计划做", "desc": "重要但不紧急，排进日程"},
                  {"name": "马上做", "desc": "又重要又紧急，立刻处理"},
                  {"name": "不要做", "desc": "既不重要也不紧急，直接删掉"},
                  {"name": "委托做", "desc": "紧急但不重要，交给别人"}],
}
QUADRANT_TITLE = "重要 × 紧急：任务怎么排"


def _scene(layout: str, n: int = 3, *, title: bool = True, **extra) -> dict:
    """四张既有版式的 scene（有标题 = 真实生成链路：caption 永远有值）。"""
    scene: dict = {"emoji": "\U0001F4D8"}
    if title:
        scene["title"] = "标题文字"
    scene.update(extra)
    if layout == "chain":
        scene["nodes"] = [{"emoji": "\U0001F4D8", "text": f"步骤 {i} 的说明"} for i in range(n)]
        scene["edge_labels"] = ["然后"] * max(0, n - 1)
    elif layout == "timeline":
        scene["steps"] = [{"emoji": "\U0001F501", "title": f"阶段{i}", "subs": ["要点"]}
                          for i in range(n)]
    elif layout == "compare":
        scene["columns"] = [{"name": f"方案{i}", "bubble": "一句话", "emoji": "\U0001F4D8",
                             "rows": [{"label": "指标", "value": "表现"}]} for i in range(2)]
    else:
        scene["start"] = "起点"
        scene["good"] = "通过"
        scene["bad"] = "不通过"
        scene["checks"] = [{"q": f"条件{i}？", "no": "处置"} for i in range(n)]
    return scene


def _bbox(els: list[dict]) -> tuple[float, float]:
    x0 = min(e["x"] for e in els)
    y0 = min(e["y"] for e in els)
    x1 = max(e["x"] + e.get("width", 0) for e in els)
    y1 = max(e["y"] + e.get("height", 0) for e in els)
    return round(x1 - x0, 3), round(y1 - y0, 3)


def _min_pt(els: list[dict]) -> float:
    w, h = _bbox(els)
    disp = F.fit_display_width_in(w, h, F.REFERENCE_MEDIA_WIDTH_IN, F.REFERENCE_MEDIA_HEIGHT_IN)
    sizes = [e["fontSize"] for e in els
             if e["type"] == "text" and str(e.get("text") or "").strip()]
    return round(min(F.pt_equivalent(s, w, disp) for s in sizes), 4)


# ---------------------------------------------------------------------------
# ① 图例条：一个条目 = 一个 text 元素（色块 = 行首几何字符）、不占图形与线性元素额度
# ---------------------------------------------------------------------------

def test_legend_entries_are_text_elements_with_a_glyph_swatch():
    """图例一个条目就是一个 text：色块是行首几何字符，整行按所代表的颜色书写。

    为什么不用真正的线段 / 矩形当色块，见 ``excalidraw_layout._legend`` 上方两条硬约束
    （chain / radial / decision 的线性元素必须双向绑定；图形与文本各有单图容量上限）。
    """
    pal = F.figure_palette("clean")
    els = L._legend([{"shape": "line", "label": "甲"},
                     {"shape": "arrow", "color": "#123456", "label": "乙"},
                     {"shape": "none", "label": "丙"}],
                    10.0, 20.0, pal=pal, slot=0)
    assert [e["type"] for e in els] == ["text", "text", "text"]
    assert F.figure_shape_count(els) == 0            # 一条线 / 一个矩形都没有
    assert F.figure_text_count(els) == 3
    assert els[0]["text"] == f"{L.LEGEND_GLYPHS['line']} 甲"
    assert els[1]["text"].startswith(L.LEGEND_GLYPHS["arrow"])
    assert els[2]["text"] == "丙"                    # shape=none ⇒ 只写文字（如「圆点序号 = 阶段先后」）
    assert els[0]["strokeColor"] == pal.base(0)      # 缺省色 = 主题主色（色就在字上）
    assert els[1]["strokeColor"] == "#123456"
    assert els[0]["fontSize"] == L.FONT_SUBTITLE     # 小标题档，理由见 _legend 文档串
    assert all(e["x"] >= 10.0 for e in els)          # 左起排列、不往左越界
    gap = els[1]["x"] - (els[0]["x"] + els[0]["width"])
    assert round(gap, 3) == round(L.LEGEND_GAP, 3)   # 条目间距 = LEGEND_GAP


def test_legend_only_adds_text_elements(monkeypatch):
    """整图口径：加图例只增加 text 元素 —— 图形个数一个不涨、也不多画线性元素。"""
    layout, scene = "chain", _scene("chain", 5)
    real = L._legend
    monkeypatch.setattr(L, "_legend", lambda *a, **k: [])
    off = L.render(layout, scene, theme="clean")
    monkeypatch.setattr(L, "_legend", real)
    on = L.render(layout, scene, theme="clean")
    assert F.figure_shape_count(on) == F.figure_shape_count(off)
    assert F.figure_text_count(on) == F.figure_text_count(off) + 1
    assert len(on) - len(off) == 1                    # 一条图例 = 一个 text（不是「色块 + 文字」）
    # 两次 render 的元素 id 是各自新生成的，只能按文字内容认「新增的那一个」
    before = {str(e.get("text") or "") for e in off}
    added = [e for e in on if str(e.get("text") or "") not in before]
    assert [e["type"] for e in added] == ["text"]


def _plan_min_pt(els: list[dict], box_w: float = 6.90) -> float:
    """**渲染口径**的最小等效磅数（与 ``ppt`` 选档同一把尺子：plan_render + 参考档位）。"""
    box_h = F.REFERENCE_MEDIA_HEIGHT_IN
    plan = F.plan_render(els, display_box=(box_w, box_h))
    disp = F.fit_display_width_in(plan["canvas_w"], plan["canvas_h"], box_w, box_h)
    return round(F.pt_equivalent(plan["text_px"][0], plan["canvas_w"], disp), 4)


def _legend_texts(els: list[dict]) -> list[dict]:
    """从整图元素里挑出**带色块字符**的图例条目（shape=none 的条目没有色块字符，挑不到）。"""
    glyphs = tuple(g for g in L.LEGEND_GLYPHS.values() if g)
    return [e for e in els if e["type"] == "text"
            and str(e.get("text") or "").startswith(glyphs)]


#: ppt.MEDIA_TIER_KEEP_PT 的口径：等效磅数贴着 9 磅硬下限（+15% 以内）的档位会被判成
#: 「被密度增益刚推过线」，此时 media_tier_of 会换到等效磅数更大的档（要点进备注）⇒ 只要
#: 加图例后的最小磅数仍 ≥ 这个阈值，页面的档位与组成就与加图例前逐项相同。
TIER_KEEP_PT = F.MIN_TEXT_PT * 1.15


def _reflowed_min_units(els: list[dict]) -> float:
    """全图**几何重排后**的最小源字号（与渲染时 ``plan_render`` 同一份重排结果）。"""
    return min(float(e["fontSize"]) for e in F.reflow_text_elements(els)
               if e["type"] == "text" and str(e.get("text") or "").strip())


def _render_with_legend(layout: str, scene: dict, monkeypatch) -> tuple[list[dict], list[dict]]:
    """同一 scene 各渲染一次「不加图例 / 加图例」（返回 off, on）。"""
    real = L._legend
    monkeypatch.setattr(L, "_legend", lambda *a, **k: [])
    off = L.render(layout, scene, theme="clean")
    monkeypatch.setattr(L, "_legend", real)
    return off, L.render(layout, scene, theme="clean")


@pytest.mark.parametrize("layout,n", [("chain", 1), ("chain", 2), ("chain", 3), ("chain", 6),
                                      ("timeline", 1), ("timeline", 3), ("timeline", 6),
                                      ("compare", 2), ("decision", 1), ("decision", 3)])
def test_legend_never_pushes_the_figure_below_the_tier_switch_threshold(monkeypatch, layout, n):
    """加图例后的最小等效磅数：要么与加图例前**逐位相等**，要么落在标题档上且仍 ≥ 换档阈值。

    正文档做不到：chain 六节点的最小字号会被图例从 9.00 磅拉到 6.18 磅（掉出护栏 ⇒
    ``media_tier_of`` 换档、要点进备注）。所以图例字号由 :func:`L._legend_font_units` 按
    本图反算 —— 这条用例是它的回归钉。
    """
    off, on = _render_with_legend(layout, _scene(layout, n), monkeypatch)
    before, after = _plan_min_pt(off), _plan_min_pt(on)
    assert after <= before, (layout, n)                 # 多一行字只会拉平，不会拉高
    legend_units = L._legend_font_units(off)
    if legend_units >= _reflowed_min_units(off):
        assert after == before, (layout, n)             # 装得下 ⇒ 最小字号逐位不变
    else:                                               # 只有「比标题档还大」的极端图落到标题档
        assert legend_units == F.tier_units("title"), (layout, n)
    if before >= TIER_KEEP_PT:                          # 本来就在阈值之上 ⇒ 不许被拖到阈值之下
        assert after >= TIER_KEEP_PT, (layout, n)
    if before >= F.MIN_TEXT_PT:                         # 本来就达标的图，加图例后必须仍然达标
        assert after >= F.MIN_TEXT_PT, (layout, n)


@pytest.mark.parametrize("layout,n", [("chain", 3), ("chain", 6), ("timeline", 3),
                                      ("compare", 2), ("decision", 3)])
def test_legend_font_is_taken_from_the_tier_ladder(layout, n):
    """图例字号只许取四档之一（与图内其他文字同一套字号语法），且不小于全图重排后的最小字号。"""
    els = L.render(layout, _scene(layout, n), theme="clean")
    legend = _legend_texts(els)
    assert legend, layout
    plain = [e for e in els if e not in legend]
    sizes = {float(e["fontSize"]) for e in legend}
    assert len(sizes) == 1                                # 一条图例里所有条目同字号
    size = sizes.pop()
    assert size in {F.tier_units(t) for t in F.FONT_TIERS}   # 档外字号一个都不许有
    assert size == L._legend_font_units(plain)            # 就是 _legend_font_units 给的值
    assert size >= min(_reflowed_min_units(plain), F.tier_units("title"))
    assert size >= L.FONT_SUBTITLE                        # 贴住下限：小标题档
    assert size > L.FONT_BODY                             # 不是正文档（正文档会被护栏打红）


# ---------------------------------------------------------------------------
# ② 既有版式的几何零回归（有标题：图例落在标题条下方的既有空档里）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("layout,n", [("chain", 5), ("timeline", 6),
                                      ("compare", 2), ("decision", 3)])
def test_legend_keeps_titled_layouts_geometry_and_pt_unchanged(monkeypatch, layout, n):
    """有标题时：宽 / 高 / 最小等效磅数 / 图形个数逐项相等（图例只落在既有空档里）。"""
    scene = _scene(layout, n)
    real = L._legend
    monkeypatch.setattr(L, "_legend", lambda *a, **k: [])
    off = L.render(layout, scene, theme="clean")
    monkeypatch.setattr(L, "_legend", real)
    on = L.render(layout, scene, theme="clean")
    assert _bbox(on) == _bbox(off)
    assert _min_pt(on) == _min_pt(off)
    assert F.figure_shape_count(on) == F.figure_shape_count(off)
    assert F.figure_text_count(on) > F.figure_text_count(off)


@pytest.mark.parametrize("layout", ["chain", "timeline", "compare", "decision"])
@pytest.mark.parametrize("title", [True, False])
@pytest.mark.parametrize("extra", [{}, {"subtitle": "一句副标题"}, {"tip": "一句小提示"}])
def test_legend_never_adds_a_selfcheck_issue(monkeypatch, layout, title, extra):
    """全矩阵（版式 × 有/无标题 × 副标题 × 提示条）：图例不许**新增** text∩text 或越界。

    口径是「问题条数不增加」：既有缺陷（timeline 无标题 + 有副标题）不算在本任务账上，
    归因单列见下一个用例。
    """
    scene = _scene(layout, 3, title=title, **extra)
    real = L._legend
    monkeypatch.setattr(L, "_legend", lambda *a, **k: [])
    off = L.validate_elements(L.render(layout, scene, theme="clean"))
    monkeypatch.setattr(L, "_legend", real)
    on = L.validate_elements(L.render(layout, scene, theme="clean"))
    assert len(on["text_overlaps"]) <= len(off["text_overlaps"]), (layout, title, extra)
    assert len(on["overflow"]) <= len(off["overflow"]), (layout, title, extra)


def test_selfcheck_is_clean_without_the_pre_existing_timeline_combo():
    """除「timeline 无标题 + 有副标题」这一既有缺陷组合外，全矩阵自检 0 问题。"""
    combos = [(layout, title, extra)
              for layout in ("chain", "timeline", "compare", "decision")
              for title in (True, False)
              for extra in ({}, {"subtitle": "一句副标题"}, {"tip": "一句小提示"})]
    for layout, title, extra in combos:
        if layout == "timeline" and not title and "subtitle" in extra:
            continue
        iss = L.validate_elements(L.render(layout, _scene(layout, 3, title=title, **extra),
                                           theme="clean"))
        assert not iss["text_overlaps"] and not iss["overflow"], (layout, title, extra)


def test_timeline_untitled_subtitle_overlap_exists_without_the_legend(monkeypatch):
    """归因（只提不改）：该组合的 text∩text / 越界在**不加图例**时就存在，非本任务引入。"""
    scene = _scene("timeline", 3, title=False, subtitle="一句副标题")
    monkeypatch.setattr(L, "_legend", lambda *a, **k: [])
    iss = L.validate_elements(L.render("timeline", scene, theme="clean"))
    assert iss["text_overlaps"] and iss["overflow"]


# ---------------------------------------------------------------------------
# ③ 象限图：元素账 / 阅读顺序 / 自检 / 容量 / 取色
# ---------------------------------------------------------------------------

def test_quadrant_element_account_reading_order_and_selfcheck():
    """四张卡（图形 4 ≤ 6）+ 13 条文字（≤ 20）；四格按阅读顺序；自检 0 问题；不触发拆图。"""
    els = chart_to_excalidraw_doc("quadrant", QUADRANT_DATA, QUADRANT_TITLE,
                                  theme="clean")["elements"]
    assert F.figure_shape_count(els) == 4
    assert F.figure_text_count(els) == 13
    assert not F.over_capacity(els)
    assert chart_svc.chart_fits("quadrant", QUADRANT_DATA, QUADRANT_TITLE)
    assert L.validate_elements(els) == {"text_overlaps": [], "overflow": []}
    cards = sorted((e for e in els if e["type"] == "rectangle"), key=lambda e: (e["y"], e["x"]))
    texts = [(e["text"], e["x"], e["y"]) for e in els if e["type"] == "text"]
    for card, quad in zip(cards, QUADRANT_DATA["quadrants"]):
        hits = [t for t in texts if t[0] == quad["name"]]
        assert len(hits) == 1, quad["name"]
        _text, tx, ty = hits[0]
        assert card["x"] <= tx <= card["x"] + card["width"], quad["name"]
        assert card["y"] <= ty <= card["y"] + card["height"], quad["name"]
    labels = {t[0] for t in texts}
    assert {"不紧急", "紧急", "不重要", "重要"} <= labels      # 四枚角标签
    # 坐标钉死：左列 20 / 右列 350，上排 76 / 下排 242（阅读顺序 = 左上 → 右上 → 左下 → 右下）
    assert [round(e["x"]) for e in cards] == [20, 350, 20, 350]
    assert [round(e["y"]) for e in cards] == [76, 76, 242, 242]


def test_quadrant_never_splits_and_has_no_mermaid_branch():
    """quadrant 是「一张图一件事」（4 格固定）⇒ 不拆；也没有 mermaid 回推分支。"""
    plans = chart_svc.split_chart_by_capacity("quadrant", QUADRANT_DATA, QUADRANT_TITLE)
    assert len(plans) == 1 and plans[0]["note"] is None
    assert chart_svc._derive_mermaid("quadrant", QUADRANT_DATA) == ""


@pytest.mark.parametrize("theme", ["clean", "professional", "handdrawn"])
def test_quadrant_colors_are_derived_from_the_theme(theme):
    """卡片色 = 主题色阶（不是写死色板）：换主题整套换，且同色相两档交替。"""
    els = chart_to_excalidraw_doc("quadrant", QUADRANT_DATA, QUADRANT_TITLE,
                                  theme=theme)["elements"]
    fills = [e["backgroundColor"] for e in els if e["type"] == "rectangle"]
    strokes = [e["strokeColor"] for e in els if e["type"] == "rectangle"]
    assert len(fills) == 4
    assert len(set(strokes)) == 1                     # 四张卡同一主题色描边（单色阶）
    assert all(F.color_origin(c, theme=theme)[0] in ("theme", "tint", "shade")
               for c in fills + strokes)
    assert all(F.color_origin(c, theme=theme)[1] == CHART_SLOT for c in strokes)


def test_quadrant_fills_differ_across_themes():
    """同一份数据在三种主题下的取色必须不同（证明「图内色从主题派生」真的生效）。"""
    seen = set()
    for theme in ("clean", "professional", "handdrawn"):
        els = chart_to_excalidraw_doc("quadrant", QUADRANT_DATA, QUADRANT_TITLE,
                                      theme=theme)["elements"]
        seen.add(tuple(e["backgroundColor"] for e in els if e["type"] == "rectangle"))
    assert len(seen) == 3


# ---------------------------------------------------------------------------
# ④ 判据与类型表三处一致（chart.CHART_TYPES / CHART_SYSTEM / lecture.CARDS_SYSTEM）
# ---------------------------------------------------------------------------

def test_quadrant_is_registered_in_types_builders_and_both_prompts():
    """新类型三处同时在场：类型表、元素构造器、两处提示词的格式与判据。"""
    assert "quadrant" in chart_svc.CHART_TYPES
    assert chart_svc.CHART_TYPES[-1] == "quadrant"          # 排在 table 之后（与文档一致）
    assert "quadrant" in _ELEMENT_BUILDERS
    assert "quadrant" in chart_svc.CHART_SYSTEM
    assert "quadrant" in lecture_svc.CARDS_SYSTEM
    assert "左上 → 右上 → 左下 → 右下" in chart_svc.CHART_SYSTEM
    assert "左上 → 右上 → 左下 → 右下" in lecture_svc.CARDS_SYSTEM
    assert "两个维度上的权衡取舍" in lecture_svc.CARDS_SYSTEM   # 自动选象限图的那条判据


def test_generate_quadrant_chart_through_the_api(client, course_id):
    """端到端（0 LLM，mock）：手工生成 quadrant 也走同一条渲染链路并落 kind=chart_quadrant。"""
    plan = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "时间管理与任务排序", "num_chapters": 2,
    })
    assert plan.status_code == 200, plan.text
    node_id = plan.json()["nodes"][0]["id"]
    approved = client.post("/api/plan/approve", json={"course_id": course_id, "approved": True})
    assert approved.status_code == 200, approved.text
    resp = client.post("/api/chart/generate", json={
        "course_id": course_id, "node_id": node_id, "chart_type": "quadrant",
        "instruction": "重要性 × 紧急度",
    })
    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["kind"] == "chart_quadrant"
    assert row["body"]["excalidraw_json"]["elements"]
    assert str(row["body"]["svg_preview"]).endswith(".svg")
    assert "mermaid" not in row["body"]                     # quadrant 没有 mermaid 分支
    assert F.figure_shape_count(row["body"]["excalidraw_json"]["elements"]) == 4# ---------------------------------------------------------------------------
# ⑤ 新增类型在「图类条目」清单里与其它 chart_* 同等可见（纯新增，不改既有类型）
# ---------------------------------------------------------------------------

def test_quadrant_is_a_figure_kind_in_delivery_and_lesson_plan():
    """``chart_quadrant`` 必须进「图类条目」清单，否则新图型会在交付包里**静默消失**。

    交付包图表目录 / 讲义 HTML 的 ``chart://`` 解析 / 网页版预览都走
    ``delivery.figure_rows``（同一份 ``FIGURE_KINDS`` 口径），教案「课前准备」表按
    ``lesson_plan._prep_rows`` 的同一批 kind 判有没有数据图表。清单漏了新类型不会报错，
    只会少一张图、多一个破图 —— 所以在这里钉住。既有 8 类一个都没动（纯新增）。
    """
    from app.services import delivery as delivery_svc

    assert "chart_quadrant" in delivery_svc.FIGURE_KINDS
    assert delivery_svc.FIGURE_KINDS[:8] == (
        "diagram", "chart_bar", "chart_line", "chart_pie",
        "chart_table", "chart_mindmap", "chart_timeline", "chart_quadrant")


def test_quadrant_chart_reaches_the_delivery_package(client, course_id):
    """端到端：象限图落库后 ``delivery.figure_rows`` 必须收货（含 svg 源）。"""
    from app.db.session import SessionLocal
    from app.models.course import ContentItem, Course, OutlineNode
    from app.services import delivery as delivery_svc
    from app.services import lesson_plan as lesson_plan_svc

    plan = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "成本与效果怎么取舍", "num_chapters": 1,
    })
    assert plan.status_code == 200, plan.text
    node_id = plan.json()["nodes"][0]["id"]
    assert client.post("/api/plan/approve",
                       json={"course_id": course_id, "approved": True}).status_code == 200
    row = client.post("/api/chart/generate", json={
        "course_id": course_id, "node_id": node_id, "chart_type": "quadrant",
        "instruction": "成本 × 效果",
    }).json()

    db = SessionLocal()
    try:
        item = db.get(ContentItem, row["id"])
        assert item is not None and item.kind == "chart_quadrant"
        figures = delivery_svc.figure_rows([item])
        prep = lesson_plan_svc._prep_rows(db.get(Course, course_id), db,
                                         db.get(OutlineNode, node_id))
    finally:
        db.close()
    assert [f["kind"] for f in figures] == ["chart_quadrant"]
    assert str(figures[0]["svg"] or "").endswith(".svg")
    assert ["数据图表", "本知识点数据图表（交付包「图表/」目录）"] in prep


@pytest.mark.parametrize("layout,n", [("chain", 1), ("chain", 3), ("chain", 6),
                                      ("timeline", 3), ("timeline", 6),
                                      ("compare", 2), ("decision", 1), ("decision", 3)])
@pytest.mark.parametrize("extra", [{}, {"subtitle": "一句副标题"}])
def test_legend_never_changes_the_bbox_in_any_combo(monkeypatch, layout, n, extra):
    """硬规则回归钉：加图例**不许**改变既有包围盒（版式 × 有/无标题 × 副标题 × 节点数）。

    落点判据见 ``L._legend_in_place``：图例只画在既有空档里，撑大画布 / 新增自检问题就
    **不画**。真课生成链路的 ``caption`` 永远有值（``delivery._figure_caption`` 兜底「图」）、
    提示词里没有 ``subtitle`` 字段 ⇒ 「无标题 / 带副标题」只可能来自存量 / 手写数据；
    宁可少画一条图例，也不许动既有版式的几何（画布一变 ⇒ 等效磅数变 ⇒ ``media_tier_of``
    可能换档、要点进备注）。
    """
    for title in (True, False):
        off, on = _render_with_legend(layout, _scene(layout, n, title=title, **extra), monkeypatch)
        assert _bbox(on) == _bbox(off), (layout, n, title, extra)


def test_legend_is_still_drawn_in_the_real_path_combo(monkeypatch):
    """反向钉：真实生成链路的组合（caption 永远有值 = 有标题、无副标题）**必须**有图例。"""
    for layout, n in (("chain", 3), ("chain", 6), ("timeline", 3), ("compare", 2), ("decision", 3)):
        off, on = _render_with_legend(layout, _scene(layout, n), monkeypatch)
        assert F.figure_text_count(on) > F.figure_text_count(off), layout
        assert _legend_texts(on) or any(str(e.get("text") or "") for e in on
                                        if e not in off), layout