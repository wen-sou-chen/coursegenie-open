# -*- coding: utf-8 -*-
"""P3-2 回归测试：章节数校验 + 素材要点覆盖自检（大纲生成后的显式提示）。

背景：`num_chapters` 原先只写进提示词、不做任何校验（实测要 8 章给 5 章），
前端默认 4 章，素材里明列 5 个方法时界面也不提示漏了什么。
"""
from __future__ import annotations

from app.db.session import SessionLocal
from app.models.course import Course
from app.schemas.plan import PlanRequest
from app.services.planner import (
    generate_outline,
    list_nodes,
    material_coverage,
    material_points,
    outline_check,
)

# 与真实素材同形的样例（PPTX 抽出来的正文就是一行一条）
FAKE_PPT = (
    "AI降幻五法：从胡说八道到可信输出\n"
    "Kimi AI\n"
    "方法一：反思校对\n"
    "方法二：引用锚定\n"
    "方法三：手动RAG\n"
    "方法四：温度锁零\n"
    "方法五：多模型交叉\n"
    "第一步：打开日志\n"
    "本次演讲将探讨幻觉的五大成因，包括模型本质、数据问题、提示词陷阱等，并提出相应的解决对策。\n"
    "幻觉的五大成因包括：概率机器本质、训练数据的裂纹、提示词的模糊性与角色设定、"
    "采样策略与输出长度、以及模型的自信口吻。\n"
)


def test_material_points_extracts_methods_and_skips_fragments():
    """抽出「方法/步骤/编号/行内枚举」要点；没写完的句子片段整条丢弃。"""
    points = material_points([FAKE_PPT])
    assert points[:5] == ["反思校对", "引用锚定", "手动RAG", "温度锁零", "多模型交叉"]
    assert "打开日志" in points, points                     # 「第一步：打开日志」
    assert "概率机器本质" in points and "模型的自信口吻" in points
    # 末尾带「等」的枚举说明没写完（「…包括 A、B、C 等，并提出…」），整条不报
    assert "模型本质" not in points and "数据问题" not in points
    assert len(points) == len(set(points))
    assert material_points(["毫无结构的正文，没有方法也没有编号。"]) == []


def test_material_coverage_matches_ignoring_particles():
    """覆盖比对是字面比对，但容忍虚词差异（「概率机器本质」命中「概率机器的本质」）。"""
    coverage = material_coverage(
        ["反思校对", "概率机器本质", "温度锁零"],
        ["3 方法一：反思校对", "2.1 概率机器的本质（逐词预测只顾顺不顺真）"])
    assert [row["covered"] for row in coverage["points"]] == [True, True, False]
    assert coverage["covered_count"] == 2 and coverage["total"] == 3
    assert coverage["uncovered"] == ["温度锁零"]
    assert coverage["points"][0]["matched_by"] == "3 方法一：反思校对"


def test_outline_check_reports_chapter_mismatch_and_uncovered_material(client, course_id):
    """请求 8 章、大纲只有 3 章 + 素材要点没全覆盖 → 两条提示都要显式给出来。"""
    paste = client.post("/api/material/paste", json={
        "course_id": course_id, "title": "AI降幻五法.pptx", "text": FAKE_PPT})
    assert paste.status_code == 200, paste.text

    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        generate_outline(db, course, PlanRequest(
            course_id=course_id, goal="学会五种降幻方法", num_chapters=3))
        nodes = list_nodes(db, course_id)
        check = outline_check(db, course, PlanRequest(
            course_id=course_id, goal="学会五种降幻方法", num_chapters=8), nodes)
    finally:
        db.close()

    assert check["requested_chapters"] == 8
    assert check["actual_chapters"] == 3 and check["chapter_match"] is False
    assert "请求 8 章，实际生成 3 章" in check["chapter_notice"]
    assert check["material_points_total"] >= 8, check["material_points"]
    assert "反思校对" in [row["point"] for row in check["material_points"]]
    assert check["material_uncovered"], check["material_notice"]
    assert "没覆盖" in check["material_notice"] and "反思校对" in check["material_notice"]
    assert check["suggested_chapters"] >= 8
    assert check["action_hint"].startswith("按此补生成")


def test_plan_generate_returns_check_and_usage(client, course_id):
    """接口返回里带 check（自检）与 usage（P3-4）：界面才能显式提示、页签才有数。"""
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "学会五种降幻方法", "num_chapters": 3})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["check"]["requested_chapters"] == 3
    assert data["check"]["chapter_match"] is True
    assert "一致" in data["check"]["chapter_notice"]
    assert data["usage"]["llm_calls"] == 1
