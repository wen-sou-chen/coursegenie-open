# -*- coding: utf-8 -*-
"""任务 124：单图容量上限对**存量图**真正生效 —— 重跑图生成（0 LLM）与数据图拆图。

背景（``验收-120-字号四档实测.md``）：任务 120 的容量上限只挂在**生成侧**，重建课件只复用
库里既有的元素、不重新生成 ⇒ 存量超限图一张没拆，「页数没涨」不能算那条的成绩。

硬边界：只测图生成侧（chart / diagram / figure_png / figure_regen 的重跑链路）；不碰 ppt.py
的判据与交付，也不动 ``MIN_TEXT_PT=9`` / ``MIN_MEDIA_WIDTH_IN=6.00`` / ``MAX_DECOR_PER_SLIDE=14``。
"""
from __future__ import annotations

from app.db.session import SessionLocal
from app.models.course import ContentItem, Course
from app.services import chart as chart_svc
from app.services import diagram
from app.services import figure_png as F
from app.services import figure_regen


def _steps_data(steps: int, subs: int = 2) -> dict:
    """图表 timeline 的原始 data（``steps`` 节点；subs 决定文本密度）。"""
    return {"steps": [{"emoji": "1", "title": f"阶段{i + 1}",
                       "subs": [f"要点{k + 1}" for k in range(subs)]}
                      for i in range(steps)]}


def _diagram_steps(steps: int) -> dict:
    """概念图 timeline 布局的原始 DIAGRAM_JSON（``nodes`` 节点）。"""
    return {"nodes": [{"title": f"第 {i + 1} 步", "subs": ["要点一", "要点二"]}
                      for i in range(steps)]}


def _texts(item: ContentItem) -> int:
    return F.figure_text_count(F.elements_of(item))


def _shapes(item: ContentItem) -> int:
    return F.figure_shape_count(F.elements_of(item))


# ---------------------------------------------------------------------------
# ① 数据图这一侧也能拆（任务 120 只做了概念图那一侧）
# ---------------------------------------------------------------------------

def test_split_chart_by_capacity_splits_timeline_until_every_part_fits():
    """超限的时间轴拆到每张都不再超上限，节点一个不丢，理由照任务 110 的记法。"""
    data = _steps_data(6)
    assert not chart_svc.chart_fits("timeline", data, "流程")
    plans = chart_svc.split_chart_by_capacity("timeline", data, "流程")
    assert len(plans) >= 2
    assert [p["part"] for p in plans] == list(range(1, len(plans) + 1))
    for plan in plans:
        assert chart_svc.chart_fits("timeline", plan["data"], "流程")
        assert plan["note"]["kind"] == "media-split"
        assert plan["note"]["figure"] == f"图 {plan['part']}/{len(plans)}"
    assert sum(len(p["data"]["steps"]) for p in plans) == 6      # 一个节点都不丢
    small = chart_svc.split_chart_by_capacity("timeline", _steps_data(2), "流程")
    assert len(small) == 1 and small[0]["note"] is None


def test_chart_types_that_cannot_be_split_stay_one_figure():
    """bar / line / pie / table 是「一张图一件事」⇒ 不硬拆（保持一张，如实标注）。"""
    bar = {"labels": ["一", "二"], "datasets": [{"label": "值", "data": [1, 2]}]}
    plans = chart_svc.split_chart_by_capacity("bar", bar, "对比")
    assert len(plans) == 1 and plans[0]["note"] is None


def test_persist_charts_from_data_splits_and_writes_notes(client, course_id):
    """超限落库：拆成「图 1 / 图 2」，标题带序号，理由进 meta.layout_notes（kind=media-split）。"""
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        items = chart_svc.persist_charts_from_data(
            db, course, None, "timeline", "流程", _steps_data(6), title="发酵流程")
        assert len(items) >= 2
        assert items[0].title.endswith(f"（图 1/{len(items)}）")
        assert items[1].title.endswith(f"（图 2/{len(items)}）")
        for i, item in enumerate(items, 1):
            assert _texts(item) <= F.MAX_FIGURE_TEXT_ELEMENTS
            assert _shapes(item) <= F.MAX_FIGURE_SHAPES
            notes = (item.meta or {}).get("layout_notes") or []
            assert notes and notes[0]["kind"] == "media-split"
            assert notes[0]["figure"] == f"图 {i}/{len(items)}"
            assert item.body["excalidraw_json"]["elements"]
    finally:
        db.close()


def test_split_off_keeps_one_over_capacity_figure(client, course_id):
    """反证据开关：``split=False``（= 把容量上限去掉）时只落一张，而且确实超限。"""
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        item = chart_svc.persist_chart_from_data(
            db, course, None, "timeline", "流程", _steps_data(6), title="发酵流程", split=False)
        assert _texts(item) > F.MAX_FIGURE_TEXT_ELEMENTS
        assert not (item.meta or {}).get("layout_notes")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# ② 存量图重跑：超限且有原始 data 的才动，动的时候 id / 引用 / 缓存路径都不换
# ---------------------------------------------------------------------------

def test_refresh_over_capacity_figures_reruns_chart_in_place(client, course_id):
    """存量超限数据图：条目 id 不变（讲义引用不动）、元素就地重跑、理由进备注、再跑幂等。"""
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        item = chart_svc.persist_chart_from_data(
            db, course, None, "timeline", "流程", _steps_data(6), title="发酵流程", split=False)
        item_id, svg_rel, before = item.id, item.body["svg_preview"], _texts(item)

        dry = figure_regen.refresh_over_capacity_figures(db, course_id)
        assert (dry["figures"], dry["over"], dry["refreshable"]) == (1, 1, 1)
        assert dry["refreshed"] == 0 and dry["rows"][0]["state"] == figure_regen.READY

        report = figure_regen.refresh_over_capacity_figures(db, course_id, apply=True)
        # 任务 125 起图形上限也生效：6 步时间轴 13 个图形 ⇒ 拆 2 张每组仍 7 个图形（超）⇒
        # 一直拆到 3 张（每组 2 步 = 5 个图形）才收手，所以新增 2 张、不是 1 张。
        assert (report["over"], report["refreshed"], report["new_parts"]) == (1, 1, 2)
        rows = (db.query(ContentItem)
                .filter(ContentItem.course_id == course_id,
                        ContentItem.kind == "chart_timeline").all())
        assert len(rows) == 3
        kept = db.get(ContentItem, item_id)
        assert kept is not None and kept.id == item_id
        assert _texts(kept) <= F.MAX_FIGURE_TEXT_ELEMENTS < before
        assert _shapes(kept) <= F.MAX_FIGURE_SHAPES
        assert kept.body["svg_preview"] == svg_rel          # 就地覆盖 ⇒ 不留孤儿文件
        assert (kept.meta or {}).get("layout_notes")[0]["kind"] == "media-split"
        assert report["after"]["over"] == 0
        # 任务 125 ③：拆图后**第 1 张**标题也标分册（原来只有第 2 张带「（图 2/3）」）
        assert kept.title.endswith("（图 1/3）")
        assert sorted(i.title.rsplit("（图 ", 1)[1] for i in rows) == \
            ["1/3）", "2/3）", "3/3）"]

        again = figure_regen.refresh_over_capacity_figures(db, course_id, apply=True)
        assert (again["over"], again["refreshed"], again["new_parts"]) == (0, 0, 0)
        assert (db.query(ContentItem)
                .filter(ContentItem.course_id == course_id,
                        ContentItem.kind == "chart_timeline").count()) == 3
        assert db.get(ContentItem, item_id).title.endswith("（图 1/3）")   # 幂等：不叠后缀
    finally:
        db.close()


def test_refresh_reports_stock_diagrams_without_source_data(client, course_id):
    """存量概念图（任务 120 及以前没落 ``body.data``）重跑不了：如实报 no-source，一个字不动。"""
    db = SessionLocal()
    try:
        elements = diagram.scene_elements("timeline", _diagram_steps(6), "酵母发面六步")
        assert F.over_capacity(elements)                    # 确实超限，不是「本来就没事」
        stock = ContentItem(
            course_id=course_id, node_id=None, kind="diagram", title="存量概念图",
            body={"layout": "timeline", "caption": "酵母发面六步",
                  "excalidraw_json": {"type": "excalidraw", "elements": elements},
                  "svg_preview": "images/stock.svg"}, meta={})
        db.add(stock)
        db.commit()
        db.refresh(stock)
        stock_id = stock.id

        report = figure_regen.refresh_over_capacity_figures(db, course_id, apply=True)
        assert (report["over"], report["no_source"], report["refreshed"]) == (1, 1, 0)
        assert report["rows"][0]["state"] == figure_regen.NO_SOURCE
        assert "重新生成" in report["rows"][0]["reason"]
        kept = db.get(ContentItem, stock_id)
        assert "data" not in (kept.body or {})              # 没有原始 data 就不反推、不动它
        assert (kept.meta or {}).get("layout_notes") in (None, [])
    finally:
        db.close()


def test_refresh_reruns_diagram_when_source_data_is_present(client, course_id):
    """任务 124 起概念图也落 ``body.data`` ⇒ 超限的存量图能就地重跑（拆成「图 1 / 图 2」）。"""
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        item = diagram.persist_diagrams_from_data(
            db, course, None, "timeline", "酵母发面六步", _diagram_steps(6),
            title="课程 · 概念图", split=False)[0]
        assert isinstance(item.body.get("data"), dict) and item.body["data"]["nodes"]
        assert _texts(item) > F.MAX_FIGURE_TEXT_ELEMENTS
        item_id = item.id

        report = figure_regen.refresh_over_capacity_figures(db, course_id, apply=True)
        assert (report["over"], report["refreshed"], report["new_parts"]) == (1, 1, 2)
        kept = db.get(ContentItem, item_id)
        assert _texts(kept) <= F.MAX_FIGURE_TEXT_ELEMENTS
        assert _shapes(kept) <= F.MAX_FIGURE_SHAPES
        assert (kept.meta or {}).get("layout_notes")[0]["kind"] == "media-split"
        assert report["after"]["over"] == 0
        assert kept.title.endswith("（图 1/3）")             # 任务 125 ③：第 1 张也标分册
    finally:
        db.close()
