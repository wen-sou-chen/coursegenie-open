# -*- coding: utf-8 -*-
"""任务 75 回归测试：素材要点抽取的「质量」与「呈现」收敛。

背景（DSH 2026-09-21 复验任务 70 时实测）：机制（落库/常显/扩面/默认关语义等价）都对，
但**抽出来的要点本身有脏数据** —— 真素材（AI 课 6 份 12,878 字）60 条里 26 条带
`**` / `[[wikilink]]` / 反引号并原样进建议文案；10 条「案例」抽的是 `### 案例 N：X`
小标题（分类名）；未覆盖 53 条直接铺开，还给「建议新增知识点《用户身份》」
「《任务目标等）》」这种不可用的建议。

本文件把三处的**新口径**钉死（去装饰 / 限长 24 / 碎片过滤 / 案例类取消 / filtered_out_count），
并守住「老口径不回退」：既有四类（方法/步骤/编号/行内枚举）在本来就干净的行上**逐字不变**。
"""
from __future__ import annotations

from app.db.session import SessionLocal
from app.models.course import Course
from app.schemas.plan import PlanRequest
from app.services import planner
from app.services.planner import (
    POINT_MAX_LEN,
    generate_outline,
    list_nodes,
    material_point_rows,
    material_points,
    outline_check,
)

# 与真素材同形（照抄 DSH 抽出来的真片段）：装饰 / 行内尾注 / 超长句 / 不成对括号 /
# 末尾带「等」的括号内枚举 / Markdown 小标题
DIRTY_MATERIAL = (
    "1. **预防**：在生成阶段通过设计合理 Prompt 降低幻觉出现概率\n"
    "2. **检测**：结合 [[PE_幻觉检测]] 方法识别潜在错误\n"
    "3. **逻辑一致性检查**（→ 参见 [[PE_分步推理]])\n"
    "4. `引用权威信息`\n"
    "5. 在多轮任务中逐步优化上下文的呈现方式（结合 [[PE_迭代优化]]）。\n"
    "6. 任务目标等）\n"
    "上下文调优（Contextual Prompt Optimization）指的是在设计 Prompt 时，有效利用上下文信息"
    "（包括用户身份、历史对话、数据背景、任务目标等），帮助模型更准确地理解任务。\n"
    "### 案例 1：事实性治理\n"
    "### 案例 2：教育场景\n"
    "方法一：反思校对\n"
    "幻觉治理是指对模型编造事实的系统性干预。\n"
    "概念：可信输出就是可复现可核对的输出\n"
)

# 本来就干净的老口径样例行（方法 / 步骤 / 行首编号 / 行内枚举）
CLEAN_MATERIAL = (
    "AI降幻五法：从胡说八道到可信输出\n"
    "Kimi AI\n"
    "方法一：反思校对\n"
    "方法二：引用锚定\n"
    "第一步：打开日志\n"
    "1. 建立评测集\n"
    "幻觉的五大成因包括：概率机器本质、训练数据的裂纹、采样策略与输出长度。\n"
)

_MARK_DECOR = ("**", "[[", "]]", "`")


def test_points_are_plain_labels_without_markdown_decoration():
    """① 去装饰：抽出来的要点里不许再有 `**` / `[[` / 反引号 / 行内尾注，括号必须成对。

    （反证据：把 `_clean_point` 的去装饰那步还原 ⇒ 本条立刻变红。）
    """
    rows = material_point_rows([DIRTY_MATERIAL], limit=60)
    assert rows, "脏素材里一条要点都没抽出来"
    for row in rows:
        point = row["point"]
        assert not any(mark in point for mark in _MARK_DECOR), point
        assert point.count("（") == point.count("）"), point
        assert point.count("(") == point.count(")"), point
        assert "→" not in point and "参见" not in point, point
    points = {row["point"] for row in rows}
    assert "逻辑一致性检查" in points, points      # 尾注被去掉，只留术语
    assert "引用权威信息" in points, points        # 反引号被去掉


def test_points_are_short_labels_not_sentences():
    """① 限长：最长要点 ≤ POINT_MAX_LEN（24 字），不把 40 字的句子当要点。

    （反证据：把限长那步还原 ⇒ 本条变红。）
    """
    rows = material_point_rows([DIRTY_MATERIAL], limit=60)
    assert max(len(row["point"]) for row in rows) <= POINT_MAX_LEN
    truncated = [row["point"] for row in rows if row["point"].endswith("…")]
    assert truncated, "超长句没有被截断（§B1 要求截断到上限 / 丢弃）"
    assert all(len(point) <= POINT_MAX_LEN for point in truncated)
    # 原句 34 字：`预防：在生成阶段通过设计合理 Prompt 降低幻觉出现概率`
    assert any(point.startswith("预防：在生成阶段") for point in truncated)


def test_case_class_is_not_extracted_from_markdown_headings():
    """② 案例类取消：`### 案例 N：X` 小标题不再抽成「事实性治理 / 教育场景」这类分类名。

    （反证据：把案例分支还原 ⇒ 本条变红。）
    """
    rows = material_point_rows([DIRTY_MATERIAL], limit=60)
    assert "case" not in {row["kind"] for row in rows}
    points = {row["point"] for row in rows}
    assert "事实性治理" not in points and "教育场景" not in points


def test_enum_fragments_and_unbalanced_fragments_are_dropped():
    """① 碎片：括号内「末尾带等」的枚举（含尾随 `）`）与括号不成对的碎片都不再当要点。"""
    points = material_points([DIRTY_MATERIAL], limit=60)
    for junk in ("用户身份", "历史对话", "数据背景", "任务目标等）", "任务目标"):
        assert junk not in points, junk
    # 扩面没被误伤：定义类 / 概念类都还在（去掉的只是碎片）
    assert "幻觉治理" in points and "可信输出就是可复现可核对的输出" in points, points


def test_legacy_prefix_is_verbatim_unchanged_on_clean_lines():
    """② 老口径不回退：本来就干净的行上，老四类（方法/步骤/编号/枚举）输出**逐字一致**。"""
    assert material_points([CLEAN_MATERIAL], limit=60) == [
        "反思校对", "引用锚定", "打开日志", "建立评测集",
        "概率机器本质", "训练数据的裂纹", "采样策略与输出长度",
    ]


def test_legacy_new_kinds_are_appended_after_the_four_legacy_kinds():
    """② 只追加不挤掉：新三类（定义/概念）接在老四类**之后**，顺序稳定。"""
    rows = material_point_rows([DIRTY_MATERIAL], limit=60)
    kinds = [row["kind"] for row in rows]
    legacy_kinds = {"method", "step", "numbered", "enum"}
    first_new = next(index for index, kind in enumerate(kinds) if kind not in legacy_kinds)
    assert all(kind in legacy_kinds for kind in kinds[:first_new])
    assert {"definition", "concept"} <= set(kinds[first_new:])
    assert "幻觉治理" in [row["point"] for row in rows]


def test_looks_like_point_filters_sentences_and_fragments():
    """③ 未覆盖明细的入场券：整句 / 引语 / 截断 / 装饰 / 过短一律不进建议清单。"""
    assert planner._looks_like_point("反思校对")
    assert planner._looks_like_point("事实校验（Fact-checking）")
    assert planner._looks_like_point("在多轮任务中逐步优化上下文的呈现方式")
    assert not planner._looks_like_point("预防：在生成阶段通过设计合理 Prompt 降…")  # 截断的长句
    assert not planner._looks_like_point("显式告诉模型“基于上下文回答”")            # 含引号
    assert not planner._looks_like_point("任务目标等）")                             # 括号不成对
    assert not planner._looks_like_point("**检测**")                                # 装饰残留
    assert not planner._looks_like_point("短")                                      # 过短


def test_suggestion_templates_are_uniform_and_have_no_decoration():
    """③ 建议文案统一：能挂靠 ⇒「建议并入第 N 章「章名」」；否则 ⇒「建议新增知识点《X》」。"""
    chapters = [("1", "幻觉检测"), ("2", "上下文调优策略")]
    assert planner._suggest_for_point("幻觉检测", chapters) == "建议并入第 1 章「幻觉检测」"
    assert planner._suggest_for_point("上下文调优", chapters) == "建议并入第 2 章「上下文调优策略」"
    assert planner._suggest_for_point("天体物理", chapters) == "建议新增知识点《天体物理》"


def test_uncovered_detail_reports_filtered_out_count(client, course_id):
    """③ 未覆盖明细收敛：条数 = 未覆盖 − 过滤数，且过滤数**如实给出**（不静默）。"""
    client.post("/api/material/paste", json={
        "course_id": course_id, "title": "长句素材.txt", "text": DIRTY_MATERIAL})
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        generate_outline(db, course, PlanRequest(
            course_id=course_id, goal="学会降幻方法", num_chapters=3))
        nodes = list_nodes(db, course_id)
        check = outline_check(db, course, PlanRequest(
            course_id=course_id, goal="学会降幻方法", num_chapters=3), nodes)
    finally:
        db.close()

    for key in ("filtered_out_count", "filtered_out_samples", "material_uncovered_shown_count"):
        assert key in check, key
    assert len(check["material_uncovered_detail"]) == check["material_uncovered_shown_count"]
    assert len(check["material_uncovered_detail"]) == \
        check["material_uncovered_count"] - check["filtered_out_count"]
    for row in check["material_uncovered_detail"]:
        assert row["suggestion"].startswith(("建议新增知识点", "建议并入第"))
        assert not row["point"].endswith("…"), row["point"]
        assert "**" not in row["point"] and "[[" not in row["point"], row["point"]
        assert row["point"].count("（") == row["point"].count("）"), row["point"]

    # §B3 附带：未覆盖提示首行也不带 Markdown 装饰
    # （修前写成 `**没覆盖**：`，在卡片里原样显示成星号）
    assert "**" not in check["material_notice"], check["material_notice"]
    # 60 条素材要点本身也不再带装饰 / 不再超长 / 不再有案例类
    rows = check["material_points"]
    assert rows and max(len(row["point"]) for row in rows) <= POINT_MAX_LEN
    assert "case" not in {row["kind"] for row in rows}
    assert sum(item["total"] for item in check["material_points_by_kind"]) == check["material_points_total"]