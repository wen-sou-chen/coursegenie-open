"""任务 48y：课程级版本回滚不再清空「课程目标」。

DSH 实测（临时副本）发现：`_outline_snapshot()` 没记 `outline.goal`，`restore()` 又把
`goal` 写死成 `""`、并把**整个版本快照**塞进 `outline.content` ⇒ 回滚后 `outlines.goal`
变空、`content.get("goal")` 变 None，连累大纲 API（`plan.py:49/161`）、配图提示词
（`image.py:125`）与课件封面（`ppt.py:792`）。

本文件把三件事钉成断言：① 新快照记下 goal / 原始大纲输出；② 回滚写回 goal；
③ **旧快照（没有新键）回滚时保持现值、绝不清空**（兼容护栏）。
"""
from __future__ import annotations

from app.db.session import SessionLocal
from app.models.course import Course, CourseVersion, Outline
from app.services import planner, ppt


def _plan_approve(client, course_id: str, goal: str = "生物光合作用") -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": goal, "num_chapters": 2,
    })
    assert resp.status_code == 200, resp.text
    approved = client.post("/api/plan/approve", json={
        "course_id": course_id, "approved": True})
    assert approved.status_code == 200, approved.text
    return resp.json()


def _outline(db, course_id: str) -> Outline | None:
    return (
        db.query(Outline)
        .filter(Outline.course_id == course_id)
        .order_by(Outline.created_at.desc())
        .first()
    )


def _goal_of(course_id: str) -> str:
    db = SessionLocal()
    try:
        return _outline(db, course_id).goal
    finally:
        db.close()


def _snapshot(client, course_id: str) -> str:
    resp = client.post(f"/api/version/{course_id}/snapshot")
    assert resp.status_code == 200, resp.text
    return resp.json()["version_id"]


def _restore(client, course_id: str, version_id: str) -> None:
    resp = client.post(f"/api/version/{course_id}/restore/{version_id}")
    assert resp.status_code == 200, resp.text


def test_snapshot_records_outline_goal(client, course_id):
    """快照里必须有 outline_goal 与 outline_content（且 outline 仍是列表，结构没变）。"""
    _plan_approve(client, course_id)
    before = _goal_of(course_id)
    assert before, "前置：大纲本来应该是有课程目标的"

    version_id = _snapshot(client, course_id)
    db = SessionLocal()
    try:
        snap = db.get(CourseVersion, version_id).snapshot
        assert snap.get("outline_goal") == before, snap.get("outline_goal")
        assert isinstance(snap.get("outline_content"), dict)
        assert snap["outline_content"].get("goal") == before
        assert isinstance(snap.get("outline"), list), "快照整体结构不该被改"
    finally:
        db.close()


def test_restore_writes_back_goal(client, course_id):
    """快照 → 改掉目标 → 回滚 ⇒ goal 与 content["goal"] 都回到快照那一刻的值。"""
    _plan_approve(client, course_id)
    before = _goal_of(course_id)
    version_id = _snapshot(client, course_id)

    db = SessionLocal()
    try:
        _outline(db, course_id).goal = "被后来改掉的目标"
        db.commit()
    finally:
        db.close()

    _restore(client, course_id, version_id)

    db = SessionLocal()
    try:
        outline = _outline(db, course_id)
        assert outline.goal == before, f"回滚后课程目标没写回：{outline.goal!r}"
        assert (outline.content or {}).get("goal") == before, outline.content
    finally:
        db.close()


def test_restore_old_snapshot_keeps_current_goal(client, course_id):
    """兼容护栏：旧格式快照（没有 outline_goal / outline_content）回滚不许清空目标。"""
    _plan_approve(client, course_id)
    version_id = _snapshot(client, course_id)

    db = SessionLocal()
    try:
        row = db.get(CourseVersion, version_id)
        row.snapshot = {k: v for k, v in row.snapshot.items()
                        if k not in ("outline_goal", "outline_content")}
        db.commit()
    finally:
        db.close()
    current = _goal_of(course_id)
    assert current

    _restore(client, course_id, version_id)

    assert _goal_of(course_id) == current, "旧快照回滚把课程目标清空了（回归）"


def test_restore_content_is_not_the_version_snapshot(client, course_id):
    """content 不该再被整个版本快照覆盖（那是 ppt.py:792 取不到 goal 的根因）。"""
    _plan_approve(client, course_id)
    version_id = _snapshot(client, course_id)
    _restore(client, course_id, version_id)

    db = SessionLocal()
    try:
        content = _outline(db, course_id).content or {}
        assert set(content) != {"course", "outline", "content", "quizzes"}, \
            "content 又被整个版本快照覆盖了"
        assert content.get("goal"), content
    finally:
        db.close()


def test_restore_goal_reaches_plan_api_and_ppt(client, course_id):
    """端到端：回滚后大纲 API 的 goal 非空；课件「课程目标」页拿得到目标。"""
    _plan_approve(client, course_id)
    before = _goal_of(course_id)
    version_id = _snapshot(client, course_id)
    _restore(client, course_id, version_id)

    plan = client.get(f"/api/plan/{course_id}")
    assert plan.status_code == 200, plan.text
    assert plan.json()["goal"] == before, plan.json().get("goal")

    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        nodes = planner.list_nodes(db, course_id)
        assert nodes, "回滚后大纲节点不该为空"
        slides, _meta = ppt.course_slides(db, course, nodes)
    finally:
        db.close()

    goal_slides = [s for s in slides if s.get("title") == ppt.COURSE_GOAL_TITLE]
    assert goal_slides, [s.get("title") for s in slides]
    assert goal_slides[0]["bullets"][0] == before, goal_slides[0]["bullets"]