# -*- coding: utf-8 -*-
"""任务 125：容量判据「两个上限都要生效」+ regenerate 脚本安全闸 + 拆图后第 1 张标分册。

判据正本：``figure_png.over_capacity`` —— **图形元素（矩形/椭圆/菱形）> 6 或 文本元素 > 20**，
任一超即超限；计数口径见 ``figure_png.FIGURE_SHAPE_TYPES``（与 120 量拐点时的 ``cards`` 同源）。

反证据（任务 125 §二.5）：把 ``over_capacity`` 改回「只看文本元素」⇒
``test_shape_limit_alone_splits_chart_and_diagram`` 与 ``test_radial_shape_limit_splits``
立刻红（实测记录见 ``backend/docs/task125-capacity-criterion.md``）。

硬边界：只测图生成侧（figure_png / chart / diagram / figure_regen / regenerate_figures.py）；
不动 ``MIN_TEXT_PT=9`` / ``MIN_MEDIA_WIDTH_IN=6.00`` / ``MAX_DECOR_PER_SLIDE=14``，
也不动 ``MAX_FIGURE_SHAPES=6`` / ``MAX_FIGURE_TEXT_ELEMENTS=20`` / ``MAX_FIGURE_PARTS=3`` 的数值。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from app.db.session import SessionLocal
from app.models.course import ContentItem, Course
from app.services import chart as chart_svc
from app.services import diagram
from app.services import figure_png as F
from app.services import figure_regen

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "regenerate_figures.py"


def _steps_chart(steps: int) -> dict:
    """数据图 timeline 的原始 data（``steps`` 节点，每个 2 条要点）。"""
    return {"steps": [{"emoji": "1", "title": f"阶段{i + 1}",
                       "subs": ["要点一", "要点二"]} for i in range(steps)]}


def _radial(nodes: int) -> dict:
    return {"center": "组件库",
            "nodes": [{"title": f"组件{i + 1}", "subs": ["说明"]} for i in range(nodes)]}


# ---------------------------------------------------------------------------
# ① 计数口径：图形 = 节点框（矩形/椭圆/菱形），连线/箭头/位图都不算
# ---------------------------------------------------------------------------

def test_shape_count_is_node_boxes_only():
    """图形元素只数矩形/椭圆/菱形（含标题条与中心圆），line/arrow/image 与空文本都不算。"""
    elements = [
        {"type": "rectangle"}, {"type": "ellipse"}, {"type": "diamond"},
        {"type": "line"}, {"type": "arrow"}, {"type": "image"},
        {"type": "text", "text": "正文"}, {"type": "text", "text": "   "},
        {"type": "text"}, "不是元素",
    ]
    assert F.figure_shape_count(elements) == 3
    assert F.figure_text_count(elements) == 1


def test_two_limits_share_the_same_source_and_both_apply():
    """两上限同源（图形口径 = 装字的节点框三类），且 ``over_capacity`` 两条都判。"""
    assert F.FIGURE_SHAPE_TYPES == F.CONTAINER_TYPES == ("rectangle", "ellipse", "diamond")
    text_only = [{"type": "text", "text": "字"} for _ in range(21)]
    shape_only = [{"type": "rectangle"} for _ in range(7)]
    fits = [{"type": "rectangle"} for _ in range(6)] + \
        [{"type": "text", "text": "字"} for _ in range(20)]
    assert F.figure_text_count(text_only) == 21 and F.figure_shape_count(text_only) == 0
    assert F.figure_shape_count(shape_only) == 7 and F.figure_text_count(shape_only) == 0
    assert F.over_capacity(text_only)          # 文本这条
    assert F.over_capacity(shape_only)         # 图形这条（124 时只判文本 ⇒ 这条不生效）
    assert not F.over_capacity(fits)           # 两条都恰好压在线上 ⇒ 不超
    assert not F.over_capacity(shape_only, shape_cap=99, text_cap=99)


# ---------------------------------------------------------------------------
# ② 图形这条**单独**就能触发拆图（反证据：改回只看文本 ⇒ 本组红）
# ---------------------------------------------------------------------------

def test_shape_limit_alone_splits_chart_and_diagram():
    """3 步时间轴：文本 17 ≤ 20（文本那条不超），图形 7 > 6 ⇒ 只可能是图形这条超的。"""
    caption, steps = "流程", 3
    chart_data = _steps_chart(steps)
    chart_els = chart_svc.chart_elements("timeline", chart_data, caption)
    assert F.figure_text_count(chart_els) <= F.MAX_FIGURE_TEXT_ELEMENTS
    assert F.figure_shape_count(chart_els) > F.MAX_FIGURE_SHAPES
    assert F.over_capacity(chart_els) and not chart_svc.chart_fits("timeline", chart_data, caption)

    plans = chart_svc.split_chart_by_capacity("timeline", chart_data, caption)
    assert len(plans) >= 2
    for plan in plans:
        assert chart_svc.chart_fits("timeline", plan["data"], caption)
        assert plan["note"]["kind"] == "media-split"
    assert sum(len(p["data"]["steps"]) for p in plans) == steps      # 一个节点不丢

    diag_data = {"nodes": [{"title": f"第 {i + 1} 步", "subs": ["要点一", "要点二"]}
                           for i in range(steps)]}
    diag_els = diagram.scene_elements("timeline", diag_data, caption)
    assert F.figure_text_count(diag_els) <= F.MAX_FIGURE_TEXT_ELEMENTS
    assert F.figure_shape_count(diag_els) > F.MAX_FIGURE_SHAPES
    parts = diagram.split_scene_by_capacity("timeline", diag_data, caption)
    assert len(parts) >= 2
    for part in parts:
        assert diagram.scene_fits(part["layout"], part["data"], caption)
        assert part["note"]["reason"].startswith("单图容量上限（图形 ≤6")


def test_radial_shape_limit_splits():
    """真库里最多的那类超限图：radial 6 张卡（图形 7 / 文本 19）⇒ 图形这条单独触发拆图。"""
    caption = "提示词工程组件库"
    data = _radial(6)
    elements = diagram.scene_elements("radial", data, caption)
    assert (F.figure_shape_count(elements), F.figure_text_count(elements)) == (7, 19)
    assert F.over_capacity(elements)
    parts = diagram.split_scene_by_capacity("radial", data, caption)
    assert len(parts) == 2
    assert [p["part"] for p in parts] == [1, 2]
    for part in parts:
        assert diagram.scene_fits(part["layout"], part["data"], caption)
    assert sum(len(p["data"]["nodes"]) for p in parts) == 6


def test_decision_is_explicitly_unsplittable():
    """决策图是「开始 → 条件 → 结论」一条链，均分会把条件与结论拆散 ⇒ 明确列为拆不动。"""
    assert "decision" in diagram._UNSPLITTABLE
    data = {"nodes": [{"kind": "start", "title": "开始"}] +
                     [{"kind": "check", "title": f"条件{i}"} for i in range(3)] +
                     [{"kind": "good", "title": "满足"}, {"kind": "bad", "title": "不满足"}]}
    elements = diagram.scene_elements("decision", data, "决策")
    assert F.figure_shape_count(elements) > F.MAX_FIGURE_SHAPES      # 确实超限
    parts = diagram.split_scene_by_capacity("decision", data, "决策")
    assert len(parts) == 1 and parts[0]["note"] is None              # 不硬拆


# ---------------------------------------------------------------------------
# ③ 拆图后第 1 张也标分册（重跑路径：原来只有新条目带「（图 2/3）」）
# ---------------------------------------------------------------------------

def test_rerun_marks_the_first_part_title(client, course_id):
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        item = chart_svc.persist_chart_from_data(
            db, course, None, "timeline", "流程", _steps_chart(6),
            title="发酵流程", split=False)
        assert not (item.title or "").endswith("）")                 # 单张：没有分册后缀

        figure_regen.refresh_over_capacity_figures(db, course_id, apply=True)
        rows = (db.query(ContentItem)
                .filter(ContentItem.course_id == course_id,
                        ContentItem.kind == "chart_timeline").all())
        assert len(rows) == 3
        titles = sorted(r.title for r in rows)
        assert titles == [f"发酵流程（图 {i}/3）" for i in (1, 2, 3)]

        again = figure_regen.refresh_over_capacity_figures(db, course_id, apply=True)
        assert (again["over"], again["new_parts"]) == (0, 0)         # 幂等
        assert sorted(r.title for r in db.query(ContentItem)
                      .filter(ContentItem.course_id == course_id).all()) == titles
    finally:
        db.close()


# ---------------------------------------------------------------------------
# ④ 脚本安全闸：不带显式目标参数 ⇒ 绝不写真实机器（任务 125 ②）
# ---------------------------------------------------------------------------

def _run_script(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", LLM_PROVIDER="mock")
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, cwd=str(REPO / "backend"), timeout=300)


def test_regenerate_figures_refuses_apply_without_explicit_targets():
    """``--apply`` 不给 ``--db/--storage/--vault`` ⇒ 拒绝（退出码 2），且连库都不连。"""
    proc = _run_script("--apply")
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "拒绝写入" in proc.stderr
    for flag in ("--db", "--storage", "--vault"):
        assert flag in proc.stderr
    assert "图条目" not in proc.stdout and "重跑前" not in proc.stdout   # 没进主流程 ⇒ 没连库


def test_regenerate_figures_dry_run_never_writes():
    """不带 ``--apply``：只报不写（打印目标三件套 + 模式），DB 文件逐字节不变。"""
    db_file = Path(os.environ["DATABASE_URL"].replace("sqlite:///", ""))
    before = db_file.read_bytes()
    proc = _run_script()
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "dry-run 只报不写" in proc.stdout
    assert "目标 db" in proc.stdout and "目标 storage" in proc.stdout and "目标 vault" in proc.stdout
    assert db_file.read_bytes() == before
