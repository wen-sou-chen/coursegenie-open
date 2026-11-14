# -*- coding: utf-8 -*-
"""任务 107：图在页上的尺寸分档、图内字号等效磅数下限、预览清晰度。

护栏口径（figure_png）与版面分档（ppt）必须互相自洽：本文件把两边的换算、
「选档判据确定可测」、「满幅档几何不越页」、「PNG 导出尺寸常量一致」、
「layout_notes 如实记录」都钉住 —— 任一环回到「恒定 5.60 英寸」或「字号随便调」
都会在这里红。
"""
from __future__ import annotations

import contextlib
import time

import pytest
from PIL import Image
from pptx.enum.shapes import MSO_SHAPE_TYPE

from app.db.session import SessionLocal
from app.models.course import ContentItem, OutlineNode
from app.services import excalidraw_chart, figure_png, ppt, ppt_com
from app.services.ppt import (BODY_COLUMN_IN, CONTENT_HEIGHT_IN, CONTENT_TOP_IN, MARGIN_IN,
                              MEDIA_COLUMN_IN, MEDIA_FULL_WIDTH_IN, MEDIA_STRIP_GAP_IN,
                              MEDIA_STRIP_MAX_IN, MEDIA_TIER_COLUMN, MEDIA_TIER_FIGURE,
                              MEDIA_TIER_FULL, MIN_MEDIA_WIDTH_IN, SLIDE_HEIGHT_IN,
                              SLIDE_WIDTH_IN)

POINT_TITLE = "光合作用与能量转换"
MARKDOWN = ("# 光合作用\n\n"
            "- 光反应把光能转成 ATP 与 NADPH\n"
            "- 暗反应用 ATP 固定二氧化碳\n")


# ---------------------------------------------------------------------------
# 夹具（与 test_ppt_v3 同款：直接落节点与内容项，不调 LLM）
# ---------------------------------------------------------------------------

def _plan_and_approve(client, course_id: str, num_chapters: int = 1) -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "让初中生理解光合作用", "num_chapters": num_chapters,
    })
    assert resp.status_code == 200, resp.text
    assert client.post("/api/plan/approve",
                       json={"course_id": course_id, "approved": True}).status_code == 200
    return client.get(f"/api/plan/{course_id}").json()


def _chapter(client, course_id: str) -> dict:
    nodes = client.get(f"/api/plan/{course_id}").json()["nodes"]
    return ([n for n in nodes if n["type"] in ("chapter", "section")] or nodes)[0]


def _add_point(course_id: str, chapter_id: str, *, key: str = "1.1",
               title: str = POINT_TITLE, markdown: str = "") -> dict:
    db = SessionLocal()
    try:
        chapter = db.get(OutlineNode, chapter_id)
        node = OutlineNode(outline_id=chapter.outline_id, node_key=key, node_type="point",
                           title=title, objective=f"理解{title}", order_index=1)
        db.add(node)
        db.commit()
        db.refresh(node)
        if markdown:
            db.add(ContentItem(course_id=course_id, node_id=node.id, kind="lecture", title=title,
                               body={"markdown": markdown, "tone": "正式"}, meta={}))
            db.commit()
        return {"id": node.id, "key": node.node_key}
    finally:
        db.close()


def _add_diagram(course_id: str, node_id: str, title: str, elements: list[dict]) -> str:
    db = SessionLocal()
    try:
        row = ContentItem(course_id=course_id, node_id=node_id, kind="diagram", title=title,
                          body={"excalidraw_json": {"type": "excalidraw", "elements": elements},
                                "caption": title},
                          meta={"role": "concept"})
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    finally:
        db.close()


def _wait(client, job_id: str, timeout: float = 60.0) -> dict:
    state: dict | None = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = client.get(f"/api/ppt/jobs/{job_id}").json()
        if state["status"] in ("success", "failed"):
            return state
        time.sleep(0.1)
    raise AssertionError(f"任务超时未结束：{state}")


def _gen(client, course_id: str, **payload) -> dict:
    resp = client.post("/api/ppt/generate", json={"course_id": course_id, **payload})
    assert resp.status_code == 200, resp.text
    state = _wait(client, resp.json()["job_id"])
    assert state["status"] == "success", state
    return state


def _chapter_deck(client, course_id: str) -> dict:
    decks = client.get(f"/api/ppt/{course_id}").json()["chapters"]
    assert decks, "应已生成章节课件"
    return decks[0]


# ---------------------------------------------------------------------------
# 元素构造：几何宽松（放得下下限）与几何紧（抬不动）两种
# ---------------------------------------------------------------------------

def _rect(x, y, w, h, key: str) -> dict:
    return {"id": f"r-{key}", "type": "rectangle", "x": x, "y": y, "width": w, "height": h,
            "backgroundColor": "#e0f2fe", "strokeColor": "#0ea5e9", "strokeWidth": 2}


def _text(x, y, w, h, content, size, key: str) -> dict:
    return {"id": f"t-{key}", "type": "text", "x": x, "y": y, "width": w, "height": h,
            "text": content, "fontSize": size, "strokeColor": "#374151", "textAlign": "center"}


def _roomy_elements() -> list[dict]:
    """一个文本框周围有大片余量：字号能一路抬到 9 磅下限。"""
    return [_rect(20, 40, 2000, 600, "box"),
            _text(60, 200, 1500, 200, "光合作用原理与能量转换", 24, "label")]


def _dense_elements(count: int = 12) -> list[dict]:
    """12 个「卡片贴文字」的小标签：折行下边界挡住字号，抬不到 9 磅。"""
    els: list[dict] = []
    for i in range(count):
        x = 40 + (i % 6) * 500
        y = 60 + (i // 6) * 900
        els.append(_rect(x, y, 420, 220, str(i)))
        els.append(_text(x + 20, y + 60, 120, 36, f"节点{i}", 12, str(i)))
    return els


def _tall_dense_elements(count: int = 8) -> list[dict]:
    """竖排的「卡片贴满文字」标签：整图又高又窄（宽高比 ≈ 0.2）⇒ 一页一图也放不到 6 英寸宽。"""
    els: list[dict] = []
    for i in range(count):
        y = 40 + i * 240
        els.append(_rect(40, y, 380, 200, f"v{i}"))
        els.append(_text(50, y + 30, 360, 40, f"很长的节点标签文字第{i}号", 12, f"v{i}"))
    return els


@contextlib.contextmanager
def _no_reflow():
    """关掉图内重排（对照 / 反证据）：重排是本轮「字号真的变大」的唯一来源。"""
    original = figure_png.reflow_text_elements
    figure_png.reflow_text_elements = lambda elements: [dict(el) for el in elements]
    try:
        yield
    finally:
        figure_png.reflow_text_elements = original


def _tiny_png(tmp_path, size: tuple[int, int] = (40, 30)) -> str:
    path = tmp_path / "fig.png"
    Image.new("RGB", size, (255, 255, 255)).save(path)
    return str(path)


def _bitmap(path: str, px, min_px: float | None = None, caption: str = "图") -> dict:
    entry: dict = {"type": "image", "path": path, "px": [int(px[0]), int(px[1])],
                   "caption": caption}
    if min_px is not None:
        entry["legibility"] = {"texts": 1, "min_px": float(min_px)}
    return entry


def _spec(bullets: list[str], px: tuple[int, int] | None = None, **extra) -> dict:
    spec: dict = {"layout": ppt.LAYOUT_POINT, "title": "一页", "bullets": list(bullets),
                  "media": []}
    if px is not None:
        spec["media"] = [_bitmap("x.png", px)]
    spec.update(extra)
    return spec


def _media_spec(bullets: list[str], px: tuple[int, int], min_px: float | None = None) -> dict:
    """带位图 + 已量字号（``legibility.min_px``）的页规格：选档判据要靠它。"""
    return _spec(bullets, px, media=[_bitmap("x.png", px, min_px)])


def _native_chart(caption: str = "原生图表") -> dict:
    """原生图表 / 表格：没有像素尺寸（随框缩放），但**同样占一格**图框。"""
    return {"type": "chart", "path": "", "caption": caption}


# ---------------------------------------------------------------------------
# ① 等效磅数口径（像素 ⇄ 磅 ⇄ 源字号）
# ---------------------------------------------------------------------------

def test_pt_equivalent_roundtrip():
    canvas_w, width_in = 2976.0, MEDIA_COLUMN_IN
    px = figure_png.px_for_pt(figure_png.MIN_TEXT_PT, canvas_w, width_in)
    assert px == pytest.approx(figure_png.MIN_TEXT_PT * canvas_w / (width_in * 72.0))
    assert figure_png.pt_equivalent(px, canvas_w, width_in) == pytest.approx(
        figure_png.MIN_TEXT_PT)


def test_fit_display_width_in_width_or_height_limited():
    # 宽图：宽度受限 = 框宽
    assert figure_png.fit_display_width_in(2000, 500, MEDIA_COLUMN_IN,
                                           CONTENT_HEIGHT_IN) == pytest.approx(MEDIA_COLUMN_IN)
    # 高图：高度受限 ⇒ 宽 = 框高 × 宽高比
    assert figure_png.fit_display_width_in(500, 2000, MEDIA_COLUMN_IN,
                                           CONTENT_HEIGHT_IN) == pytest.approx(
        CONTENT_HEIGHT_IN * 0.25)
    assert figure_png.fit_display_width_in(0, 2000, MEDIA_COLUMN_IN, CONTENT_HEIGHT_IN) == 0.0


def test_min_text_px_is_px_for_pt_of_fitted_width():
    canvas_w, canvas_h = 2976, 1176
    box = (figure_png.REFERENCE_MEDIA_WIDTH_IN, figure_png.REFERENCE_MEDIA_HEIGHT_IN)
    width = figure_png.fit_display_width_in(canvas_w, canvas_h, *box)
    assert figure_png.min_text_px(canvas_w, canvas_h) == pytest.approx(
        figure_png.px_for_pt(figure_png.MIN_TEXT_PT, canvas_w, width))
    assert figure_png.min_text_px(canvas_w, canvas_h, min_text_pt=0) == 0.0


def test_reference_box_matches_the_narrowest_ppt_media_tier():
    """护栏的参考框必须就是 ppt 里最窄的图档，否则「下限」与「实际档位」对不上。"""
    assert figure_png.REFERENCE_MEDIA_WIDTH_IN == pytest.approx(MEDIA_COLUMN_IN)
    assert figure_png.REFERENCE_MEDIA_HEIGHT_IN == pytest.approx(CONTENT_HEIGHT_IN)
    assert ppt.MEDIA_FLOOR_BOX == (MEDIA_COLUMN_IN, CONTENT_HEIGHT_IN)


def test_source_font_guard_is_inverse_of_span_guard():
    span = 1200.0
    units = figure_png.source_font_units_for_pt(span)
    assert units > 0
    assert figure_png.max_span_units_for_pt(units) == pytest.approx(span)


def test_load_font_accepts_below_the_old_8px_guard():
    """旧的「8 px 护栏」已作废；交给 PIL 的合法下限是 MIN_PIL_FONT_PX。"""
    assert figure_png.MIN_PIL_FONT_PX < 8
    if not figure_png.find_font_path():
        pytest.skip("本机没有中文字体")
    assert figure_png.load_font(figure_png.MIN_PIL_FONT_PX) is not None


# ---------------------------------------------------------------------------
# ② 渲染侧：有边界地抬字号
# ---------------------------------------------------------------------------

def test_element_signature_depends_on_legibility_params():
    els = _roomy_elements()
    base = figure_png.element_signature(els)
    assert base != figure_png.element_signature(els, None, display_box=(5.0, 4.0))
    assert base != figure_png.element_signature(els, None, min_text_pt=0.0)


def test_plan_render_reflow_raises_font_beyond_floor_and_keeps_canvas():
    """任务 110：先按卡片几何**重排**（字号 ÷ 画布只升不降），再叠 9 磅护栏。

    余量大的图重排后会**越过**下限（不是刚好卡在 9）—— 这正是「字真的变大」；
    画布尺寸一个数都没动，所以等效磅数（字号 ÷ 画布宽 × 显示宽度 × 72）只会变大。
    """
    els = _roomy_elements()
    with _no_reflow():
        off = figure_png.plan_render(els, min_text_pt=0)
    on = figure_png.plan_render(els)
    assert off["min_pt"] < figure_png.MIN_TEXT_PT      # 源字号上页后本来不合格
    assert on["min_pt"] > figure_png.MIN_TEXT_PT       # 重排后越过下限
    assert on["canvas_w"] == off["canvas_w"]           # 画布不动，只是字大了
    assert on["canvas_h"] == off["canvas_h"]
    assert min(on["text_px"]) > min(off["text_px"])
    assert els[1]["fontSize"] == 24                    # 入参不被改写（返回的是新列表）


def test_plan_render_caps_font_by_geometry_and_reports():
    els = _dense_elements()
    with _no_reflow():
        off = figure_png.plan_render(els, min_text_pt=0)
    on = figure_png.plan_render(els)
    assert on["text_elements"] >= 12
    floor = figure_png.min_text_px(on["canvas_w"], on["canvas_h"],
                                   display_box=ppt.MEDIA_FLOOR_BOX)
    sizes = list(on["sizes_map"].values())
    assert min(sizes) < floor                              # 抬不动 ⇒ 只抬到几何边界
    assert on["min_pt"] < figure_png.MIN_TEXT_PT           # 分栏档参考框里仍不够 9 磅
    assert on["min_pt"] > off["min_pt"]                    # 抬了，只是这个框里够不着
    natural = 12.0 * on["scale"]
    assert min(sizes) >= natural - 1e-6                    # 只抬不缩
    assert max(sizes) <= natural * figure_png.MAX_FONT_BOOST + 1e-6


def test_reflow_only_moves_text_and_never_the_card_or_canvas():
    """重排只动文字自己的 x/y/width/height/fontSize：不越卡片、不改画布。"""
    card = _rect(0, 0, 400, 200, "c")
    label = _text(20, 60, 120, 36, "光合作用原理与能量转换", 24, "c")
    els = [card, label]
    with _no_reflow():
        raw = figure_png.plan_render(els, min_text_pt=0)
    out = figure_png.reflow_text_elements(els)
    text = [el for el in out if el["type"] == "text"][0]
    assert text["fontSize"] > 24                            # 字号抬上去了
    assert card["x"] - 0.5 <= text["x"]
    assert text["x"] + text["width"] <= card["x"] + card["width"] + 0.5
    assert card["y"] - 0.5 <= text["y"]
    assert text["y"] + text["height"] <= card["y"] + card["height"] + 0.5
    assert (out[0]["x"], out[0]["y"], out[0]["width"], out[0]["height"]) == \
        (card["x"], card["y"], card["width"], card["height"])   # 卡片一个数都没动
    on = figure_png.plan_render(els, min_text_pt=0)
    # 画布**宽度**不涨（重排只把文字摆进卡片，不撑大画布）；
    # 底边在小图被放大（scale > 1）时可能补一点高度 —— 字号涨得比画布多，磅数照样只升不降。
    assert on["canvas_w"] <= raw["canvas_w"]
    assert on["min_pt"] > raw["min_pt"]
    assert els[1]["fontSize"] == 24                         # 入参不被改写


# ---------------------------------------------------------------------------
# ③ 版面分档：确定、可测
# ---------------------------------------------------------------------------

def test_media_tier_keeps_column_when_the_column_is_already_legible():
    """任务 110：满幅档要挪走一大堆要点、图反而不够宽 ⇒ 保持分栏档（「看得清」优先）。"""
    spec = _media_spec(["光照强度与二氧化碳浓度共同决定速率"] * 6, (2000, 1000), 100.0)
    assert ppt.media_strip_height_in(spec["bullets"], 18.0) > MEDIA_STRIP_MAX_IN
    tier, reason = ppt.media_tier_of(spec, 18.0)
    assert tier == MEDIA_TIER_COLUMN
    assert "右栏" in reason and MIN_MEDIA_WIDTH_IN == 6.00
    entry = spec["media"][0]
    column = ppt.media_tier_boxes(spec["bullets"], 18.0)[0][1]
    assert ppt._media_ok(entry, column)
    assert ppt._fitted_media_width_in(entry, ppt.media_picture_box(column)) >= 6.0


def test_media_tier_goes_full_and_moves_bullets_when_strip_does_not_fit():
    """要点条放不下也不再回退分栏（任务 110）：压条 + 要点进备注，图必须够宽够清楚。"""
    bullets = ["光照强度与二氧化碳浓度共同决定速率"] * 6
    spec = _media_spec(bullets, (2000, 500), 60.0)
    assert ppt.media_strip_height_in(bullets, 18.0) > MEDIA_STRIP_MAX_IN
    tier, reason = ppt.media_tier_of(spec, 18.0)
    assert tier == MEDIA_TIER_FULL
    assert "要点条" in reason and "移入备注" in reason
    box = ppt.page_media_plan(spec, {"body": 18.0})["media_box"]
    assert box[3] <= CONTENT_HEIGHT_IN - MEDIA_STRIP_GAP_IN + 1e-9
    assert ppt._media_ok(spec["media"][0], box)
    assert ppt._fitted_media_width_in(spec["media"][0], ppt.media_picture_box(box)) >= 6.0


def test_media_tier_goes_full_for_wide_figure_with_few_bullets():
    spec = _spec(["光反应", "暗反应"], (2000, 500))
    tier, reason = ppt.media_tier_of(spec, 18.0)
    assert tier == MEDIA_TIER_FULL
    assert "图显示" in reason
    # 满幅档的宽度必须真的比分栏档大（判据就是「显示宽度更大」）
    col = ppt.media_tier_boxes(spec["bullets"], 18.0)[0][1]
    full = ppt.media_tier_boxes(spec["bullets"], 18.0)[1][1]
    assert ppt._fitted_media_width_in(spec["media"][0], full) > \
        ppt._fitted_media_width_in(spec["media"][0], col) + 0.01


def test_media_tier_keeps_figure_on_page_when_switch_would_shrink_type():
    """任务 146b A1：**不许丢图 > 不许把图压小**。

    复刻任务 124 的同一张图（分栏 6.17 英寸 / 10.00 磅，整页 8.56 英寸 / 13.87 磅）：
    纯图页会把本页要点全挤进备注，146 又不许 0 要点图页 ⇒ 图只能被挪进备注。分栏档仍满足
    6.00 英寸 / 9 磅硬下限 ⇒ 接受字号从 13.87 降到 10.00，图与要点同页。
    """
    spec = _media_spec(["光反应", "暗反应"], (2000, 1500), 45.0)
    tier, reason = ppt.media_tier_of(spec, 18.0)
    column = ppt.media_tier_boxes(spec["bullets"], 18.0)[0][1]
    _, width_col, pt_col = ppt.media_cells_report(spec["media"], column)
    _, _width_fig, pt_fig = ppt.media_cells_report(spec["media"], ppt.MEDIA_FIGURE_BOX,
                                                   columns=1, caption_in=ppt.FIGURE_CAPTION_IN)
    assert ppt._media_ok(spec["media"][0], column)      # 旧口径的「能放先放要点」本来会选它
    assert width_col >= ppt.MIN_MEDIA_WIDTH_IN
    assert pt_col >= ppt.MIN_TEXT_PT
    assert pt_col < pt_fig                              # 换纯图页确实更大，但会把图弄丢
    assert tier == MEDIA_TIER_COLUMN
    assert "为了保住图，接受图内字号从" in reason and "图与要点同页" in reason
    assert "放弃换档" not in reason


def test_media_tier_keeps_bullets_when_the_original_tier_is_legible_enough():
    """原档的磅数本来就够大时照旧保住要点（A1 之后这条路径直接留在原档）。"""
    spec = _media_spec(["光反应", "暗反应"], (2000, 1500), 60.0)
    tier, reason = ppt.media_tier_of(spec, 18.0)
    assert tier == MEDIA_TIER_COLUMN
    assert "放弃换档" not in reason


def test_a1_does_not_depend_on_the_keep_pt_threshold(monkeypatch):
    """反证据：把 124 的阈值抬到不可能达到，A1 仍必须把图留在页上。"""
    spec = _media_spec(["光反应", "暗反应"], (2000, 1500), 45.0)
    monkeypatch.setattr(ppt, "MEDIA_TIER_KEEP_PT", 100.0)
    tier, reason = ppt.media_tier_of(spec, 18.0)
    column = ppt.media_tier_boxes(spec["bullets"], 18.0)[0][1]
    _, _, pt_col = ppt.media_cells_report(spec["media"], column)
    assert tier == MEDIA_TIER_COLUMN
    assert pt_col >= ppt.MIN_TEXT_PT
    assert "为了保住图，接受" in reason


def test_media_tier_falls_back_to_figure_page_for_tall_figure():
    """又高又窄的图（宽高比 ≈ 0.9）：分栏 / 满幅都放不到 6 英寸宽 ⇒ 一页一图。"""
    spec = _media_spec(["光反应", "暗反应"], (1277, 1156), 23.08)
    tier, reason = ppt.media_tier_of(spec, 18.0)
    assert tier == MEDIA_TIER_FIGURE
    assert "整页给图" in reason and "达标" in reason
    plan = ppt.page_media_plan(spec, {"body": 18.0})
    assert plan["layout"] == ppt.LAYOUT_FIGURE
    assert plan["caption_in"] == ppt.FIGURE_CAPTION_IN == 0.0
    entry = spec["media"][0]
    picture = ppt.media_picture_box(plan["media_box"], caption_in=plan["caption_in"])
    assert picture[3] == pytest.approx(plan["media_box"][3])     # 图吃满整框（无图注条）
    width = ppt._fitted_media_width_in(entry, picture)
    assert width > MIN_MEDIA_WIDTH_IN
    assert figure_png.pt_equivalent(23.08, 1277, width) >= figure_png.MIN_TEXT_PT
    assert ppt._media_ok(entry, plan["media_box"], caption_in=0.0)


def test_media_tier_without_pixels_stays_column():
    """原生图表 / 表格没有位图 ⇒ 随框缩放 ⇒ 分栏档（不瞎猜尺寸）。"""
    tier, reason = ppt.media_tier_of(_spec(["光反应"], None), 18.0)
    assert tier == MEDIA_TIER_COLUMN
    assert "原生图表" in reason


# ---------------------------------------------------------------------------
# ④ 满幅档的几何
# ---------------------------------------------------------------------------

def test_full_tier_geometry_stays_inside_slide():
    bullets = ["光反应", "暗反应"]
    (col_body, col_media), (full_body, full_media) = ppt.media_tier_boxes(bullets, 18.0)
    # 分栏档：右栏贴右页边，左右两栏不重叠
    assert col_media[0] + col_media[2] == pytest.approx(SLIDE_WIDTH_IN - MARGIN_IN)
    assert col_body[0] + col_body[2] <= col_media[0] + 1e-9
    assert col_body[2] == pytest.approx(BODY_COLUMN_IN)
    # 满幅档：要点条在图上方，两者都是版心宽
    assert full_body[0] == pytest.approx(MARGIN_IN)
    assert full_body[2] == pytest.approx(MEDIA_FULL_WIDTH_IN)
    assert full_media[0] == pytest.approx(MARGIN_IN)
    assert full_media[2] == pytest.approx(MEDIA_FULL_WIDTH_IN)
    assert full_media[1] == pytest.approx(full_body[1] + full_body[3] + MEDIA_STRIP_GAP_IN)
    assert full_body[3] <= MEDIA_STRIP_MAX_IN + 1e-9
    # 任何档位都不许越出页面
    for box in (col_body, col_media, full_body, full_media):
        assert box[0] >= 0.0 and box[1] >= CONTENT_TOP_IN - 1e-9
        assert box[0] + box[2] <= SLIDE_WIDTH_IN + 1e-9
        assert box[1] + box[3] <= SLIDE_HEIGHT_IN + 1e-9
    assert full_media[3] < CONTENT_HEIGHT_IN


def test_full_media_box_shrinks_as_bullet_strip_grows():
    one = ppt.media_tier_boxes(["光反应"], 18.0)[1][1]
    two = ppt.media_tier_boxes(["光反应", "暗反应"], 18.0)[1][1]
    assert two[3] < one[3]


def test_page_media_plan_for_figure_only_and_cover():
    figure_only = ppt.page_media_plan({"layout": ppt.LAYOUT_FIGURE, "media": []},
                                      {"body": 18.0})
    assert figure_only["tier"] == ppt.MEDIA_TIER_FIGURE
    assert figure_only["media_box"][2] == pytest.approx(MEDIA_FULL_WIDTH_IN)
    assert figure_only["body_box"][2] == 0.0
    cover = ppt.page_media_plan({"layout": ppt.LAYOUT_COVER}, {"body": 18.0})
    assert cover["tier"] is None and cover["media_box"] is None


def test_page_media_plan_matches_media_tier_of():
    spec = _spec(["光反应", "暗反应"], (2000, 500))
    plan = ppt.page_media_plan(spec, {"body": 18.0})
    assert plan["tier"] == ppt.media_tier_of(spec, 18.0)[0]
    assert plan["media_box"] == ppt.media_tier_boxes(spec["bullets"], 18.0)[1][1]


# ---------------------------------------------------------------------------
# ⑤ 预览清晰度：PNG 导出尺寸
# ---------------------------------------------------------------------------

def test_png_export_size_is_2x_shared_constant():
    assert (ppt.PPT_PNG_WIDTH, ppt.PPT_PNG_HEIGHT) == (2560, 1440)
    assert (ppt_com.PNG_WIDTH, ppt_com.PNG_HEIGHT) == (ppt.PPT_PNG_WIDTH, ppt.PPT_PNG_HEIGHT)
    assert ppt.PPT_PNG_WIDTH == 2 * 1280 and ppt.PPT_PNG_HEIGHT == 2 * 720
    assert ppt.PPT_PNG_WIDTH / ppt.PPT_PNG_HEIGHT == pytest.approx(
        SLIDE_WIDTH_IN / SLIDE_HEIGHT_IN, rel=1e-3)


def test_chart_font_floor_uses_png_metric_without_changing_defaults():
    expected = figure_png.source_font_units_for_pt(
        excalidraw_chart.CANVAS_W - excalidraw_chart.PAD_L - excalidraw_chart.PAD_R)
    assert excalidraw_chart.MIN_TEXT_UNITS == pytest.approx(expected)
    assert excalidraw_chart.MIN_TEXT_UNITS <= 12.0     # 既有 12 / 13 号标签不受影响
    assert excalidraw_chart._text("标签", 10, 10, 13)["fontSize"] == pytest.approx(13.0)
    assert excalidraw_chart._text("标签", 10, 10, 6)["fontSize"] == pytest.approx(
        excalidraw_chart.MIN_TEXT_UNITS)


def test_legibility_note_fires_only_when_reported_pt_is_below_floor():
    """上报的数字与判据必须同一个口径（不许出现「9.00 磅 < 9 磅」）。"""
    for min_px in (20.0, 37.19, 9.0, 60.0):
        spec = _spec(["光反应"], (1340, 415))
        spec["media"][0]["legibility"] = {"min_px": min_px, "texts": 1}
        plan = ppt.page_media_plan(spec, {"body": 18.0})
        notes = ppt.media_legibility_notes(spec, plan, 1)
        width = ppt._fitted_media_width_in(spec["media"][0], plan["media_box"])
        pt = round(figure_png.pt_equivalent(min_px, 1340, width), 2)
        assert bool(notes) == (pt < figure_png.MIN_TEXT_PT), (min_px, pt, notes)
        for note in notes:
            assert f"{pt:.2f} 磅" in note["reason"], note


def test_legibility_note_reports_unmeasurable_bitmap_honestly():
    spec = _spec(["光反应"], (1340, 415))
    spec["media"][0]["legibility"] = {}
    plan = ppt.page_media_plan(spec, {"body": 18.0})
    notes = ppt.media_legibility_notes(spec, plan, 1)
    assert len(notes) == 1
    assert "测不出字号" in notes[0]["reason"]


# ---------------------------------------------------------------------------
# ⑥ 端到端：layout_notes 如实记录档位与图内字号
# ---------------------------------------------------------------------------

def test_layout_notes_record_tier_and_legibility(client, course_id):
    _plan_and_approve(client, course_id, num_chapters=1)
    chapter = _chapter(client, course_id)
    point = _add_point(course_id, chapter["id"], markdown=MARKDOWN)
    # 竖排密图：一页一图也放不到 6 英寸宽 ⇒ 必然留下一条 legibility 上报
    _add_diagram(course_id, point["id"], "竖密图", _tall_dense_elements())

    _gen(client, course_id, scope="chapter", chapter_node_id=chapter["id"])
    deck = _chapter_deck(client, course_id)
    assert deck["figure_failures"] == []

    notes = deck["meta"]["layout_notes"]
    tiers = [n for n in notes if n.get("kind") == "media-tier"]
    leg = [n for n in notes if n.get("kind") == "legibility"]
    assert tiers, notes
    labels = (f"{ppt.MEDIA_TIER_LABELS[ppt.MEDIA_TIER_COLUMN]}：",
              f"{ppt.MEDIA_TIER_LABELS[ppt.MEDIA_TIER_FULL]}：",
              f"{ppt.MEDIA_TIER_LABELS[ppt.MEDIA_TIER_FIGURE]}：")
    assert any(str(n.get("reason", "")).startswith(labels) for n in tiers), tiers
    assert leg, notes
    assert any("磅" in str(n.get("reason")) for n in leg), leg
    # 图不越页（生成时同一套几何自检）
    assert ppt.audit_pptx(deck["body"]["pptx"]) == []


# ---------------------------------------------------------------------------
# ⑦ 任务 110：窄放下限 / 纯图页几何 / 图注同一把尺子 / 拆图 / 反算余量
# ---------------------------------------------------------------------------

def test_figure_page_box_is_the_full_page_below_the_head_line():
    """纯图页的图框 = 整块可用页高（页底对齐页脚分隔线），页首只留一行页标题。"""
    left, top, width, height = ppt.MEDIA_FIGURE_BOX
    assert left == pytest.approx(MARGIN_IN)
    assert width == pytest.approx(MEDIA_FULL_WIDTH_IN)
    assert top == pytest.approx(ppt.FIGURE_TOP_IN)
    assert top + height == pytest.approx(SLIDE_HEIGHT_IN - ppt.FIGURE_RULE_IN)
    assert ppt.FIGURE_HEAD_TOP_IN + ppt.FIGURE_HEAD_H_IN <= top + 1e-9   # 页标题不压图
    assert ppt.FIGURE_CAPTION_IN == 0.0                                 # 图注让位给页标题
    # 比正文区更高是**故意的**：近方形图在 16:9 页面上只能靠页高放大
    assert height > CONTENT_HEIGHT_IN
    assert ppt.MEDIA_FIGURE_BOX[2] == pytest.approx(MEDIA_FULL_WIDTH_IN)


def test_media_caption_in_is_the_same_ruler_as_the_renderer():
    """图注条高度只有一处定义：选档 / 报数 / 真正缩图都用它（不许各算各的）。"""
    assert ppt.media_caption_in(CONTENT_HEIGHT_IN) == pytest.approx(0.32)   # 顶端 CAPTION_BOX_IN
    assert ppt.media_caption_in(1.0) == pytest.approx(0.24)                 # 小图走 0.24 下限
    box = (0.0, 0.0, MEDIA_COLUMN_IN, CONTENT_HEIGHT_IN)
    assert ppt.media_picture_box(box)[3] == pytest.approx(CONTENT_HEIGHT_IN - 0.32)
    assert ppt.media_picture_box(box, caption_in=0.0)[3] == pytest.approx(CONTENT_HEIGHT_IN)


def test_media_need_height_in_leaves_margin_over_the_floor():
    """反算带余量：落地后必须 > 9 磅（卡在正好 9.00 的图上页会被取整吃成 8.99）。"""
    spec = _media_spec(["光反应"], (1990, 869), 29.5)
    entry = spec["media"][0]
    need = ppt.media_need_height_in(entry)
    assert need is not None
    box = (MARGIN_IN, CONTENT_TOP_IN, MEDIA_FULL_WIDTH_IN, need)
    pt = ppt._media_pt_in(entry, box)
    assert pt is not None and pt > figure_png.MIN_TEXT_PT
    assert pt >= figure_png.MIN_TEXT_PT * 1.01
    assert ppt._media_ok(entry, box)


def test_narrow_placement_is_refused_by_the_width_floor():
    """窄放下限是真判据：宽度 < 6 英寸的档位直接淘汰（不是记一笔就算完）。"""
    spec = _media_spec(["光反应", "暗反应"], (1277, 1156), 23.08)
    entry = spec["media"][0]
    column = ppt.media_tier_boxes(spec["bullets"], 18.0)[0][1]
    width = ppt._fitted_media_width_in(entry, ppt.media_picture_box(column))
    assert width < MIN_MEDIA_WIDTH_IN
    assert not ppt._media_ok(entry, column)
    assert ppt.media_tier_of(spec, 18.0)[0] != MEDIA_TIER_COLUMN


def test_split_multi_media_slides_one_image_per_page():
    """一页多张位图、每页都有足够要点 ⇒ 拆成多页，每页 1 图 + 自己那份要点。

    任务 146：不再产生「一图 + 一行图注」的纯图页；拆法写进 layout_notes。
    """
    spec = {"layout": ppt.LAYOUT_POINT, "title": "一页",
            "bullets": ["光反应把光能转成 ATP 与 NADPH，为暗反应提供能量。",
                        "暗反应在叶绿体基质里固定二氧化碳并合成糖类，供植物生长使用。",
                        "温度与光照强度会同时影响两阶段的速率与产物，需要一起调节参数。"],
            "media": [_bitmap("a.png", (1000, 400)), _bitmap("b.png", (1000, 400)),
                      _bitmap("c.png", (1000, 400))]}
    specs = [spec]
    notes: list[dict] = []
    ppt.split_multi_media_slides(specs, notes)
    assert len(specs) == 3
    assert specs[0]["media"][0]["path"] == "a.png"
    assert specs[0]["layout"] == ppt.LAYOUT_POINT
    assert specs[0]["bullets"] == [spec["bullets"][0]]
    for page, path in zip(specs[1:], ("b.png", "c.png")):
        assert page["layout"] == ppt.LAYOUT_POINT
        assert [m["path"] for m in page["media"]] == [path]
        assert page["bullets"] and len("".join(page["bullets"])) >= ppt.PAGE_MIN_TEXT_CHARS
        assert page["title"].endswith(ppt.CONT_SUFFIX)
    assert [n["kind"] for n in notes] == ["media-split"]
    assert "拆成 3 页" in notes[0]["reason"]


def test_split_keeps_a_page_whose_cells_are_already_legible():
    """放得下就不拆：两张原生图表没有图内字号问题，同页照旧（拆图不是无脑一刀切）。"""
    spec = {"layout": ppt.LAYOUT_POINT, "title": "一页", "bullets": ["光反应"],
            "media": [_native_chart("图一"), _native_chart("图二")]}
    specs = [spec]
    notes: list[dict] = []
    ppt.split_multi_media_slides(specs, notes)
    assert specs == [spec] and notes == []


def test_split_multi_media_slides_splits_native_chart_from_bitmap():
    """同页「原生图表 + 位图」一起判：渲染按图数切格子，位图分不到整幅（110 实测的漏网点）。"""
    chart = _native_chart("原生图表")
    bitmap = _bitmap("b.png", (1277, 1156), 23.08)
    spec = {"layout": ppt.LAYOUT_POINT, "title": "一页",
            "bullets": ["光反应把光能转成 ATP 与 NADPH，为暗反应提供能量。",
                        "暗反应在叶绿体基质里固定二氧化碳并合成糖类，供植物生长使用。"],
            "media": [chart, bitmap]}
    specs = [spec]
    notes: list[dict] = []
    ppt.split_multi_media_slides(specs, notes)
    assert len(specs) == 2
    assert specs[0]["media"] == [chart] and specs[0]["layout"] == ppt.LAYOUT_POINT
    assert [m["path"] for m in specs[1]["media"]] == ["b.png"]
    assert specs[1]["layout"] == ppt.LAYOUT_POINT
    assert specs[1]["bullets"] == [spec["bullets"][1]]
    assert [n["kind"] for n in notes] == ["media-split"]
    assert "拆成 2 页" in notes[0]["reason"]


def test_multi_media_page_with_bullets_is_not_rescued_to_a_figure_page():
    """带要点的多图页不许靠「整页给图」兜底 —— 那会把要点静默挤进备注；拆页才对。"""
    spec = {"layout": ppt.LAYOUT_POINT, "title": "一页",
            "bullets": ["光反应把光能转成 ATP 与 NADPH，为暗反应提供能量。",
                        "暗反应在叶绿体基质里固定二氧化碳并合成糖类，供植物生长使用。"],
            "media": [_bitmap("a.png", (1990, 885), 30.5), _native_chart("原生图表")]}
    plan = ppt.page_media_plan(spec, ppt.deck_palette()["sizes"])
    assert plan["tier"] == MEDIA_TIER_FIGURE          # 并排看得清，但会吃掉本页要点
    specs: list[dict] = [spec]
    notes: list[dict] = []
    ppt.split_multi_media_slides(specs, notes)
    assert len(specs) == 2
    assert specs[0]["bullets"] == [spec["bullets"][0]]     # 要点留在页上
    assert specs[1]["bullets"] == [spec["bullets"][1]]
    assert all(page["layout"] == ppt.LAYOUT_POINT for page in specs)
    assert "每页都保留各自那份要点" in notes[0]["reason"]


def test_media_cells_report_and_renderer_use_the_same_grid(tmp_path):
    """规划侧报数用的格子 = 渲染侧切出来的格子（同页多图时否则会多报尺寸）。"""
    from types import SimpleNamespace

    first, second = tmp_path / "a.png", tmp_path / "b.png"
    Image.new("RGB", (1277, 1119), (255, 255, 255)).save(first)
    Image.new("RGB", (1294, 506), (255, 255, 255)).save(second)
    media = [_bitmap(str(first), (1277, 1119), 30.0), _bitmap(str(second), (1294, 506), 30.0)]
    spec = {"layout": ppt.LAYOUT_FIGURE, "title": "一页图", "bullets": [], "notes": "",
            "media": media}
    palette = ppt.deck_palette()
    prs = ppt._build_prs(SimpleNamespace(title="测试课程"), [spec], palette, None)
    cells = ppt.media_cell_boxes(ppt.MEDIA_FIGURE_BOX, 2, columns=2)
    pics = [sh for sh in prs.slides[0].shapes if sh.shape_type == MSO_SHAPE_TYPE.PICTURE]
    assert len(pics) == 2 and len(cells) == 2
    for pic, entry, cell in zip(pics, media, cells):
        width = pic.width / 914400
        picture = ppt.media_picture_box(cell, caption_in=ppt.FIGURE_CAPTION_IN)
        assert width == pytest.approx(ppt._fitted_media_width_in(entry, picture), abs=0.01)
        assert pic.left / 914400 == pytest.approx(cell[0] + (cell[2] - width) / 2, abs=0.01)
        assert pic.top / 914400 == pytest.approx(cell[1], abs=0.01)
    # 报数按格子（半幅 ≈ 6.03 英寸），不是整幅 12.23 —— 两张并排都达标 ⇒ 不拆、无 legibility note
    ok, width, pt = ppt.media_cells_report(media, ppt.MEDIA_FIGURE_BOX, columns=2,
                                           caption_in=ppt.FIGURE_CAPTION_IN)
    assert (ok and width < MEDIA_FULL_WIDTH_IN / 2 + 0.01 and pt is not None
            and pt >= figure_png.MIN_TEXT_PT)
    plan = ppt.page_media_plan(spec, palette["sizes"])
    assert ppt.media_legibility_notes(spec, plan, 1) == []
    specs: list[dict] = [spec]
    notes: list[dict] = []
    ppt.split_multi_media_slides(specs, notes)
    assert specs == [spec] and notes == []            # 纯图页本来就没有要点可丢 ⇒ 不拆


def test_figure_only_page_drops_title_band_and_keeps_title_plus_caption(tmp_path):
    """纯图页不画标题条（图才能吃满页高）：页标题与图注同占页首一行 —— 两者都不许消失。"""
    from types import SimpleNamespace

    png = _tiny_png(tmp_path, (128, 116))
    spec = {"layout": ppt.LAYOUT_FIGURE, "title": "幻觉特点分析维度", "bullets": [], "notes": "",
            "media": [_bitmap(png, (1277, 1156), 23.08, "别名")]}
    course = SimpleNamespace(title="测试课程")
    prs = ppt._build_prs(course, [spec], ppt.deck_palette(), None)
    slide = prs.slides[0]
    assert slide.shapes.title.text == "幻觉特点分析维度"
    captions = [sh.text_frame.text.strip() for sh in slide.shapes
                if getattr(sh, "has_text_frame", False)
                and sh.text_frame.text.strip().startswith("图 ")]
    assert captions == ["图 1 · 别名"]                    # 图注仍在页上（在页首行）
    bands = [sh for sh in slide.shapes if sh.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE
             and abs(sh.top or 0) < 1 and abs((sh.width or 0) / 914400 - SLIDE_WIDTH_IN) < 0.01]
    assert bands == []                                   # 没有整幅标题条
    pics = [sh for sh in slide.shapes if sh.shape_type == MSO_SHAPE_TYPE.PICTURE]
    assert len(pics) == 1
    assert pics[0].height / 914400 == pytest.approx(ppt.MEDIA_FIGURE_BOX[3], abs=0.02)
    assert pics[0].top / 914400 == pytest.approx(ppt.FIGURE_TOP_IN, abs=0.01)
    assert ppt.audit_presentation(prs) == []
    # 连图注都没有时也不会空着（`_media_title` 兜底成「图」），标题一个字也不许丢
    bare = {**spec, "media": [{**spec["media"][0], "caption": "", "title": ""}]}
    prs2 = ppt._build_prs(course, [bare], ppt.deck_palette(), None)
    texts = [sh.text_frame.text.strip() for sh in prs2.slides[0].shapes
             if getattr(sh, "has_text_frame", False)]
    assert "幻觉特点分析维度" in texts and "图 1 · 图" in texts
