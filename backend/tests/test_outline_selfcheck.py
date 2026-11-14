# -*- coding: utf-8 -*-
"""任务 70 回归测试：大纲自检「落库常显」+ 素材要点覆盖扩面（定义 / 概念 / 案例）。

背景（DSH 2026-09-20 走查）：`outline_check()` 算得很细，但结论只作为
`POST /api/plan/generate` 的响应返回、前端存内存 state ⇒ 刷新页面 / 换设备就消失，
第二次打开这门课再也看不到「当初哪几个素材要点没覆盖」；而且素材要点只认
「方法N / 第N步 / 行首序号」三类，定义 / 概念 / 案例完全不查。
"""
from __future__ import annotations

from app.db.session import SessionLocal
from app.models.course import Course
from app.schemas.plan import PlanRequest
from app.services import planner
from app.services.planner import (
    OUTLINE_CHECK_KEY,
    generate_outline,
    list_nodes,
    material_point_rows,
    outline_check,
    stored_outline_check,
)

# 与真实课件同形的样例：老三类（方法 / 步骤 / 行首编号）+ 任务 70 新三类（定义 / 概念 / 案例）
FAKE_MATERIAL = (
    "AI 幻觉治理实战：从成因到可信输出\n"
    "Kimi AI\n"
    "方法一：反思校对\n"
    "方法二：引用锚定\n"
    "第一步：打开日志\n"
    "1. 建立评测集\n"
    "幻觉是指模型生成看似合理却缺乏依据的内容。\n"
    "所谓接地，就是把回答约束在检索到的原文里。\n"
    "决策矩阵（把每个选项按多条标准逐项打分后比较）\n"
    "案例一：某团队把幻觉率降到 4%\n"
    "例如：客服场景先检索工单再作答\n"
)


def _generate(db, course_id: str, chapters: int = 3):
    """直调 service 生成一版大纲（mock LLM），返回 (course, nodes)。"""
    course = db.get(Course, course_id)
    generate_outline(db, course, PlanRequest(
        course_id=course_id, goal="学会五种降幻方法", num_chapters=chapters))
    return course, list_nodes(db, course_id)


def _paste(client, course_id: str, text: str = FAKE_MATERIAL) -> None:
    resp = client.post("/api/material/paste", json={
        "course_id": course_id, "title": "AI降幻五法.pptx", "text": text})
    assert resp.status_code == 200, resp.text


def test_outline_check_lands_in_course_meta_and_survives_reopen(client, course_id):
    """① 落库可核：生成后结论 + 时间戳 + 素材指纹都在 course.meta，重开一次仍读得到同一份。"""
    _paste(client, course_id)

    # 生成前：空态可判（没有大纲、也没有自检记录）
    empty = client.get(f"/api/plan/{course_id}/check").json()
    assert empty["has_outline"] is False and empty["check"] is None
    assert empty["check_generated_at"] == ""

    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "学会五种降幻方法", "num_chapters": 3})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["check"]["requested_chapters"] == 3
    assert data["check_generated_at"], "接口没返回自检时间戳"
    assert data["check_materials_fingerprint"], "接口没返回素材指纹"
    first_generated_at = data["check_generated_at"]

    # 第一次读取（同一进程）：GET /plan/{id} 与 GET /plan/{id}/check 都是落库那份
    read1 = client.get(f"/api/plan/{course_id}").json()
    assert read1["check"]["material_points_total"] == data["check"]["material_points_total"]
    assert read1["check_generated_at"] == first_generated_at
    read1b = client.get(f"/api/plan/{course_id}/check").json()
    assert read1b["check"]["material_uncovered_count"] == data["check"]["material_uncovered_count"]

    # 第二次读取：换一条数据库会话（模拟重启 / 换设备），结论与时间戳必须一模一样
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        stored = stored_outline_check(course)
    finally:
        db.close()
    assert stored is not None, "自检结论没落库（刷新就丢）"
    assert stored["generated_at"] == first_generated_at
    assert stored["check"]["material_points_by_kind"], "落库结论里没有按类型汇总"
    fingerprint = stored["materials_fingerprint"]
    assert fingerprint and all(item["id"] and item["chars"] > 0 for item in fingerprint)
    assert len(stored["materials_fingerprint_hash"]) == 12

    read2 = client.get(f"/api/plan/{course_id}/check").json()
    assert read2["check_generated_at"] == first_generated_at
    assert read2["check_materials_fingerprint_hash"] == stored["materials_fingerprint_hash"]
    assert read2["check"]["material_uncovered"] == stored["check"]["material_uncovered"]


def test_regenerate_refreshes_stored_check(client, course_id):
    """重新生成大纲 ⇒ 自检记录被更新（不是叠加第二条，也不留旧指纹）。"""
    _paste(client, course_id)
    client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "学会五种降幻方法", "num_chapters": 3})
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        first = stored_outline_check(course)
        # 再插一份新素材，重新生成 → 指纹与结论都应更新
        client.post("/api/material/paste", json={
            "course_id": course_id, "title": "增补材料.txt",
            "text": "案例二：把提示词模板固化后复核成本下降\n概念：可信输出就是可复现可核对的输出"})
        db.expire_all()
        course = db.get(Course, course_id)
        generate_outline(db, course, PlanRequest(
            course_id=course_id, goal="学会五种降幻方法", num_chapters=3))
        db.expire_all()
        course = db.get(Course, course_id)
        second = stored_outline_check(course)
    finally:
        db.close()
    assert second is not None and first is not None
    assert second["materials_fingerprint_hash"] != first["materials_fingerprint_hash"]
    assert len(second["materials_fingerprint"]) == 2
    assert OUTLINE_CHECK_KEY == "outline_check"  # 只写 course.meta：没新建任何表


def test_point_tap_extended_to_definition_concept_not_case(monkeypatch):
    """③ 扩面：新口径 ≥ 旧口径，真的多认出 定义 / 概念；**案例类已取消**（任务 75 §B2①）。"""
    monkeypatch.setattr(planner, "_sentence_point_rows", lambda text: [])
    legacy = material_point_rows([FAKE_MATERIAL])
    monkeypatch.undo()
    now = material_point_rows([FAKE_MATERIAL])

    legacy_points = {row["point"] for row in legacy}
    now_points = {row["point"] for row in now}
    assert legacy_points <= now_points, "扩面把老口径认出来的要点挤掉了"
    assert len(now) >= len(legacy), (len(now), len(legacy))
    assert len(now) > len(legacy), "抽点没有扩面（新口径要点数没有增加）"
    kinds = {row["kind"] for row in now}
    assert {"definition", "concept"} <= kinds
    # 任务 75 §B2①：`### 案例 N：X` 小标题抽出来的是分类名（不是可覆盖的知识点）⇒ 案例类取消
    assert "case" not in kinds, "案例类没有被取消（小标题会被当成要点）"
    assert "事实性治理" not in now_points and "教育场景" not in now_points


def test_uncovered_points_carry_actionable_suggestions(client, course_id):
    """③ 未覆盖项要给可操作建议（建议并入第 N 章「章名」/ 建议新增知识点）；碎片不铺出来。"""
    _paste(client, course_id)
    db = SessionLocal()
    try:
        course, nodes = _generate(db, course_id)
        check = outline_check(db, course, PlanRequest(
            course_id=course_id, goal="学会五种降幻方法", num_chapters=3), nodes)
    finally:
        db.close()
    assert check["material_uncovered_count"] > 0
    # 任务 75 §B3：明细只留「像知识点」的条目，过滤掉多少条如实给 filtered_out_count
    assert len(check["material_uncovered_detail"]) == \
        check["material_uncovered_count"] - check["filtered_out_count"]
    assert check["material_uncovered_shown_count"] == len(check["material_uncovered_detail"])
    for row in check["material_uncovered_detail"]:
        assert row["suggestion"].startswith(("建议新增知识点", "建议并入第"))
        assert row["kind_label"], "未覆盖项没有类型标签"
    # 口径已扩面：提示里不再把自己说成只查「方法 / 步骤类要点」，且按类型报了数
    assert any(label in check["material_notice"] for label in ("定义", "概念", "案例")), \
        "覆盖提示的口径没有扩面（仍然只说方法/步骤）"
    assert "方法/步骤类要点" not in check["material_notice"]
    total_by_kind = sum(item["total"] for item in check["material_points_by_kind"])
    assert total_by_kind == check["material_points_total"]


def test_semantic_equivalence_is_off_by_default_and_never_calls_llm(
        client, course_id, monkeypatch):
    """④ 语义等价判定默认关：不调 LLM、不产生用量；开启也只用本地词面相似度。"""
    _paste(client, course_id)
    db = SessionLocal()
    try:
        course, nodes = _generate(db, course_id)
        calls: list[str] = []

        def _boom(*args, **kwargs):  # pragma: no cover - 只用来断言「没被调用」
            calls.append("build_llm_client")
            raise AssertionError("自检不该调 LLM")

        monkeypatch.setattr(planner, "build_llm_client", _boom)
        used: list[str] = []
        monkeypatch.setattr(planner.usage_log, "record_aux",
                            lambda *a, **k: used.append("record_aux"))
        default_check = outline_check(db, course, PlanRequest(
            course_id=course_id, goal="学会五种降幻方法", num_chapters=3), nodes)
        assert default_check["semantic_enabled"] is False
        assert calls == [] and used == [], "默认路径调了 LLM 或记了用量"

        # 开启开关：仍然不调 LLM（本地词面相似度），但会把「语义相近」的要点算成已覆盖
        titles = ["方法四：把温度锁到零"]
        off = planner.material_coverage(["温度锁零"], titles)
        on = planner.material_coverage(["温度锁零"], titles, semantic=True)
        assert off["covered_count"] == 0 and on["covered_count"] == 1
        assert on["points"][0]["matched_by"] == "方法四：把温度锁到零"
        assert calls == [], "开了语义等价也不许调 LLM"
    finally:
        db.close()


def test_plan_generate_writes_check_without_extra_usage(client, course_id):
    """自检落库不额外产生 LLM 用量：生成的 usage.llm_calls 仍然是 1。"""
    _paste(client, course_id)
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "学会五种降幻方法", "num_chapters": 3,
        "semantic": False})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["usage"]["llm_calls"] == 1
    assert data["check_semantic_enabled"] is False


def test_semantic_flag_flows_from_request_to_stored_check(client, course_id):
    """开关是显式的、随请求走：传 true 才落成 semantic_enabled=true（默认仍是 false）。"""
    _paste(client, course_id)
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "学会五种降幻方法", "num_chapters": 3,
        "semantic": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["check"]["semantic_enabled"] is True
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        stored = stored_outline_check(course)
    finally:
        db.close()
    assert stored["semantic_enabled"] is True
    assert stored["check"]["semantic_enabled"] is True


def test_get_plan_without_stored_check_returns_null_not_500(client, course_id):
    """有旧大纲、但还没自检记录（升级前生成 / 换门课）时：GET /plan/{id} 必须 200 + check=null。

    踩过的坑：`_stored_check_out(None)` 会让「切到另一门课」直接 500（临时环境走查时发现），
    所以这里把「没有落库记录」当成一等场景钉住。
    """
    db = SessionLocal()
    try:
        course, _ = _generate(db, course_id)
        meta = dict(course.meta or {})
        meta.pop(OUTLINE_CHECK_KEY, None)   # 模拟「升级前生成的老大纲」：没有自检记录
        course.meta = meta
        db.commit()
    finally:
        db.close()

    resp = client.get(f"/api/plan/{course_id}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["nodes"], "大纲还在"
    assert data["check"] is None and data["check_generated_at"] == ""

    only = client.get(f"/api/plan/{course_id}/check")
    assert only.status_code == 200, only.text
    assert only.json()["has_outline"] is True and only.json()["check"] is None


def test_post_check_reruns_without_regenerating_outline(client, course_id):
    """任务 81 §B3：「立即自检」给**已有大纲**补跑一次 —— 不动大纲、不留历史版本、只更新落库结论。

    场景就是真库那门 AI 课：状态「大纲已通过（锁定）」、节点都在，但 `course.meta.outline_check`
    为空（升级前生成的，从没跑过自检）。原先只有 GET，界面上没法补这一次结论。
    """
    _paste(client, course_id)
    db = SessionLocal()
    try:
        course, nodes = _generate(db, course_id)
        before_ids = [n.id for n in nodes]
        meta = dict(course.meta or {})
        meta.pop(OUTLINE_CHECK_KEY, None)        # 模拟「老大纲、没有自检记录」
        course.meta = meta
        db.commit()
    finally:
        db.close()

    empty = client.get(f"/api/plan/{course_id}/check").json()
    assert empty["has_outline"] is True and empty["check"] is None

    resp = client.post(f"/api/plan/{course_id}/check")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["has_outline"] is True
    assert data["check_generated_at"], "补跑的自检没有落库时间戳"
    # 生成参数落过库 ⇒ 按「当初请求了几章」对照（而不是 0）
    assert data["check"]["requested_chapters"] == 3, data["check"]
    assert data["check"]["material_points_total"] >= 1, data["check"]

    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        assert [n.id for n in list_nodes(db, course_id)] == before_ids, "补跑自检动了大纲节点"
        assert planner.plan_history(course) == [], "补跑自检不该留下历史版本（那是重新生成才有的）"
        stored = stored_outline_check(course)
    finally:
        db.close()
    assert stored and stored["check"]["material_points_total"] >= 1

    # 再读一次：GET 拿到的就是刚落库的那份（与生成时是同一个键、同一份形状）
    again = client.get(f"/api/plan/{course_id}/check").json()
    assert again["check"] == data["check"]
    assert again["check_generated_at"] == data["check_generated_at"]


def test_post_check_falls_back_to_actual_chapters_without_stored_params(client, course_id):
    """生成参数没落库的老课：按现大纲章数对照，不报「请求 0 章」这种假结论。"""
    _paste(client, course_id)
    db = SessionLocal()
    try:
        course, nodes = _generate(db, course_id)
        meta = dict(course.meta or {})
        meta.pop(OUTLINE_CHECK_KEY, None)
        meta.pop(planner.PLAN_PARAMS_KEY, None)   # 真库 AI 课就是这种：meta 里没有 plan_params
        course.meta = meta
        db.commit()
    finally:
        db.close()

    data = client.post(f"/api/plan/{course_id}/check").json()
    check = data["check"]
    assert check["actual_chapters"] == len([n for n in nodes if "." not in n.node_key])
    assert check["requested_chapters"] == check["actual_chapters"]
    assert check["chapter_match"] is True
    assert check["chapter_notice"].startswith("请求")


def test_post_check_without_outline_is_400(client, course_id):
    """还没有大纲时补跑自检 ⇒ 明确 400（先「生成大纲」，别给一份空结论）。"""
    resp = client.post(f"/api/plan/{course_id}/check")
    assert resp.status_code == 400, resp.text
    assert "生成大纲" in resp.text
