"""任务 51：K-12 结构与教材元数据（`course.meta` → 大纲/讲义口径）测试。

三条口径（只加不改）：
1. 元数据放 `course.meta`（顶层散写或嵌套 `k12` 块都认，**不改表结构**），`CourseOut.k12` 透出；
2. **非 K-12 课一个字都不加** —— 既有职场课（AI 课 / 包子课）的提示词与指纹不受影响；
3. 大纲走「单元 → 课时」，讲义按课时控篇幅（提示词 `lecture-v12`）。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from app.db.session import SessionLocal
from app.models.course import Course, OutlineNode
from app.schemas.plan import PlanRequest
from app.services import k12
from app.services import planner as planner_mod
from app.services.lecture import _lecture_user_prompt, input_fingerprint

K12_META = {"stage": "小学", "grade": "二年级", "textbook": "部编版",
            "volume": "上册", "unit": "第一单元", "class_hours": 3}
WORK_META = {"kb": {"vault_root": "D:/vault", "output_rel": "课程/x"},
             "plan_params": {"goal": "让客服看懂幻觉"}}


def _node() -> OutlineNode:
    return OutlineNode(node_key="1.1", node_type="point", title="静夜思",
                       objective="能朗读并说出诗里写了什么",
                       teaching_strategy="范读 + 跟读", assessment="抽读")


# ---------------------------------------------------------------- A. 纯函数

def test_k12_meta_accepts_both_shapes_and_nested_wins():
    """顶层散写与嵌套块都认；嵌套块优先；空值不进结果。"""
    assert k12.k12_meta({"stage": "小学", "grade": "二年级"}) == {"stage": "小学", "grade": "二年级"}
    assert k12.k12_meta({"k12": {"stage": "初中", "unit": "第三单元"}}) == {
        "stage": "初中", "unit": "第三单元"}
    merged = k12.k12_meta({"stage": "小学", "grade": "二年级", "k12": {"stage": "初中"}})
    assert merged == {"stage": "初中", "grade": "二年级"}          # 嵌套块赢
    assert k12.k12_meta({"stage": "", "grade": None, "unit": []}) == {}
    assert k12.k12_meta(Course(title="古诗三首", meta=dict(K12_META)))["textbook"] == "部编版"


def test_is_k12_ignores_work_courses():
    """既有职场课的 meta（kb 绑定 / plan_params）绝不能被当成 K-12。"""
    assert k12.is_k12({"stage": "小学"}) is True
    assert k12.is_k12({"textbook": "部编版"}) is True
    assert k12.is_k12(WORK_META) is False
    assert k12.is_k12({"subject": "语文", "audience": "三年级"}) is False
    assert k12.is_k12({}) is False


def test_word_range_by_stage_and_grade():
    """篇幅区间按学段/年级取；非 K-12 课沿用既有「不少于 900 字、不限上限」。"""
    assert k12.word_range({"stage": "小学", "grade": "二年级"}) == (900, 1500)
    assert k12.word_range({"stage": "小学", "grade": "五年级"}) == (1000, 1800)
    assert k12.word_range({"stage": "初中", "grade": "七年级"}) == (1200, 2200)
    assert k12.word_range({"stage": "高中"}) == (1400, 2600)
    assert k12.word_range(WORK_META) == (900, None)


def test_prompt_sections_empty_for_work_courses():
    """口径护栏：非 K-12 课的提示词段必须为空串（否则 AI 课会被要求「一节课 900–1500 字」）。"""
    assert k12.outline_prompt_section({"meta": WORK_META}) == ""
    assert k12.lecture_prompt_section({"meta": WORK_META}) == ""
    assert k12.outline_prompt_section(Course(title="AI 幻觉治理", meta=WORK_META)) == ""


def test_outline_prompt_section_says_unit_to_class_hour():
    text = k12.outline_prompt_section(Course(title="古诗三首", meta=dict(K12_META)))
    assert "K-12 教材口径" in text
    assert "单元（或课文）" in text
    assert "一节课（40/45 分钟）" in text
    assert "小学 二年级 · 部编版 上册 · 第一单元" in text
    assert "3 课时" in text                      # class_hours 进入提示词
    assert "课时目标" in text
    assert "小学低年级" in text                   # 学段语气


def test_lecture_prompt_section_carries_word_limit():
    text = k12.lecture_prompt_section(Course(title="古诗三首", meta=dict(K12_META)))
    assert "K-12 课时口径" in text
    assert "900–1500 字" in text
    assert "5000 字长文" in text                  # 明确反对写成长文
    assert "二年级" in text


# ---------------------------------------------------------------- B. 提示词接线

def test_lecture_user_prompt_adds_k12_only_when_course_given():
    plain = _lecture_user_prompt(_node(), "正式")
    assert "K-12 课时口径" not in plain            # 不传 course ⇒ 与今天一致（职场课口径不变）

    k12_prompt = _lecture_user_prompt(_node(), "正式", course=Course(title="古诗三首",
                                                                     meta=dict(K12_META)))
    assert "K-12 课时口径" in k12_prompt
    assert "900–1500 字" in k12_prompt

    work = _lecture_user_prompt(_node(), "正式", course=Course(title="AI 幻觉治理", meta=WORK_META))
    assert "K-12 课时口径" not in work


def test_input_fingerprint_changes_with_k12_section():
    """K-12 元数据改了 ⇒ 指纹变了 ⇒ 批量生成不会误跳过。"""
    node = _node()
    assert input_fingerprint(node=node) == input_fingerprint(node=node, k12="")
    assert input_fingerprint(node=node, k12="A") != input_fingerprint(node=node, k12="")


def test_outline_prompt_carries_k12_section(monkeypatch):
    seen: dict = {}

    def fake_complete(client, system, user, **kwargs):
        seen["user"] = user
        return {"title": "古诗三首", "goal": "理解三首古诗", "nodes": []}

    monkeypatch.setattr(planner_mod.usage_log, "complete_json", fake_complete)
    db = SessionLocal()
    try:
        k12_course = Course(title="古诗三首", subject="语文", audience="三年级学生",
                            duration_minutes=120, meta=dict(K12_META))
        work_course = Course(title="AI 幻觉治理", subject="职场", audience="客服",
                             duration_minutes=120, meta=dict(WORK_META))
        db.add_all([k12_course, work_course])
        db.commit()
        db.refresh(k12_course)
        db.refresh(work_course)

        planner_mod.generate_outline(db, k12_course, PlanRequest(
            course_id=k12_course.id, goal="理解三首古诗", num_chapters=2))
        assert "K-12 教材口径" in seen["user"]
        assert "一节课（40/45 分钟）" in seen["user"]
        assert "3 课时" in seen["user"]

        planner_mod.generate_outline(db, work_course, PlanRequest(
            course_id=work_course.id, goal="降低客服幻觉率", num_chapters=3))
        assert "K-12 教材口径" not in seen["user"]      # 职场课一字不加
    finally:
        db.close()


# ---------------------------------------------------------------- C. 接口

def _bind(client, course_id: str) -> Path:
    root = Path(tempfile.mkdtemp(prefix="coursegenie_k12_"))
    resp = client.post(f"/api/kb/course/{course_id}/bind", json={
        "vault_root": str(root), "output_rel": "课程/K12"})
    assert resp.status_code == 200, resp.text
    return root


def test_course_k12_meta_roundtrip_and_patch_merges(client):
    """建课写入 K-12 元数据 → 归一化透出；PATCH meta 是合并，不会清掉 kb 绑定。"""
    resp = client.post("/api/course/", json={
        "title": "古诗三首", "subject": "语文", "audience": "三年级学生",
        "duration_minutes": 120, "meta": dict(K12_META)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["k12"] == {k: K12_META[k] for k in
                           ("stage", "grade", "textbook", "volume", "unit", "class_hours")}
    course_id = body["id"]

    plain = client.post("/api/course/", json={"title": "AI 幻觉治理", "meta": dict(WORK_META)})
    assert plain.json()["k12"] == {}

    assert client.post("/api/course/", json={"title": "无元数据"}).json()["k12"] == {}

    _bind(client, course_id)
    patched = client.patch(f"/api/course/{course_id}", json={"meta": {"stage": "初中"}})
    assert patched.status_code == 200, patched.text
    meta = patched.json()["meta"]
    assert meta["stage"] == "初中"
    assert meta["unit"] == "第一单元"             # 旧键仍在（合并）
    assert meta["kb"]["vault_root"]               # kb 绑定没被清掉
    assert patched.json()["k12"]["stage"] == "初中"


def test_plan_nodes_expose_both_key_spellings(client, course_id):
    """接口字段与库字段统一：`key`/`type` 与 `node_key`/`node_type` 同时给出、值一致。"""
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "生物光合作用", "num_chapters": 2})
    assert resp.status_code == 200, resp.text

    data = client.get(f"/api/plan/{course_id}").json()
    assert data["nodes"], data
    for node in data["nodes"]:
        assert node["key"] == node["node_key"]
        assert node["type"] == node["node_type"]
    for node in data["graph"]["nodes"]:
        assert node["key"] == node["node_key"]
        assert node["type"] == node["node_type"]
