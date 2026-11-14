# -*- coding: utf-8 -*-
"""P4-1 回归测试：点过「以这篇为样板」的那节会被按章批量重做。

现象：样板锚写进 course.meta 后，该节点的输入指纹随之变化 → 批量时它不再算
「输入未变」而被重做，老师认可的那一版悄悄进了 meta.history。
修法：设样板时自动加锁（已有 locked 语义）+ 界面提示；批量列出 locked_skipped；
样板节被显式解锁时，批量必须先确认（否则 409），另给一键加锁/解锁入口。
"""
from __future__ import annotations

import time

from app.db.session import SessionLocal
from app.models.course import ContentItem


def _plan_and_approve(client, course_id: str) -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "初中生物光合作用", "num_chapters": 2})
    assert resp.status_code == 200, resp.text
    assert client.post("/api/plan/approve", json={
        "course_id": course_id, "approved": True}).status_code == 200
    return resp.json()


def _wait_job(client, job_id: str, tries: int = 120) -> dict:
    for _ in range(tries):
        state = client.get(f"/api/content/jobs/{job_id}").json()
        if state["status"] in ("success", "failed"):
            return state
        time.sleep(0.1)
    raise AssertionError(f"job timeout: {job_id}")


def _chatper_nodes(client, course_id: str) -> dict:
    return client.get(f"/api/plan/{course_id}").json()["nodes"]


def _chapter(client, course_id: str, key: str) -> dict:
    return next(n for n in _chatper_nodes(client, course_id) if n["key"] == key)


def _lecture_of(client, course_id: str, node_id: str) -> dict:
    rows = client.get(f"/api/content/{course_id}").json()
    return next(r for r in rows if r["kind"] == "lecture" and r["node_id"] == node_id)


def _anchored_course(client, course_id: str, gen_lecture_row):
    """建好 2 章 + 第 1 章两节讲义，并把 1.1 设为样板，返回 (plan, chapter, sample_id)。"""
    plan = _plan_and_approve(client, course_id)
    chapter = _chapter(client, course_id, "1")
    sample = _chapter(client, course_id, "1")  # 章节点即叶子（mock 大纲只有章）
    gen_lecture_row(course_id, sample["id"])
    resp = client.post("/api/lecture/style-anchor", json={
        "course_id": course_id, "source_item_id": _lecture_of(client, course_id, sample["id"])["id"]})
    assert resp.status_code == 200, resp.text
    return plan, chapter, sample, resp.json()


def test_setting_anchor_locks_the_sample_lecture(client, course_id, gen_lecture_row):
    plan, _chapter_node, sample, body = _anchored_course(client, course_id, gen_lecture_row)
    assert body["locked"] is True
    assert "确认" in body["lock_notice"] or "锁定" in body["lock_notice"]

    state = client.get(f"/api/lecture/style-anchor?course_id={course_id}").json()
    assert state["locked"] is True
    assert "样板节已锁定" in state["lock_notice"]
    assert state["source_item_id"]

    db = SessionLocal()
    try:
        row = db.get(ContentItem, state["source_item_id"])
        assert row.meta["locked"] is True
        assert row.meta["locked_reason"] == "样板节：批量生成不会覆盖"
    finally:
        db.close()


def test_batch_skips_locked_sample_and_history_unchanged(client, course_id, gen_lecture_row):
    plan, chapter, sample, _body = _anchored_course(client, course_id, gen_lecture_row)
    before = _lecture_of(client, course_id, sample["id"])
    before_history = len(((before["meta"] or {}).get("history") or []))

    resp = client.post("/api/lecture/generate-chapter", json={
        "course_id": course_id, "chapter_node_id": chapter["id"], "tone": "正式"})
    assert resp.status_code == 200, resp.text
    job = _wait_job(client, resp.json()["job_id"])
    assert job["status"] == "success", job
    result = job["result"]
    assert [row["node_key"] for row in result["locked_skipped"]] == ["1"], result
    assert result["locked_skipped"][0]["reason"]

    after = _lecture_of(client, course_id, sample["id"])
    assert after["body"]["markdown"] == before["body"]["markdown"], "样板节正文必须逐字不变"
    assert len(((after["meta"] or {}).get("history") or [])) == before_history, "不应新增历史版本"


def test_unlocked_sample_needs_explicit_confirmation(client, course_id, gen_lecture_row):
    plan, chapter, sample, _body = _anchored_course(client, course_id, gen_lecture_row)
    resp = client.post("/api/lecture/style-anchor/source-lock", json={
        "course_id": course_id, "locked": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["locked"] is False
    assert "没有" in resp.json()["lock_notice"]

    blocked = client.post("/api/lecture/generate-chapter", json={
        "course_id": course_id, "chapter_node_id": chapter["id"], "tone": "正式"})
    assert blocked.status_code == 409, blocked.text
    assert "样板节" in blocked.json()["detail"]

    allowed = client.post("/api/lecture/generate-chapter", json={
        "course_id": course_id, "chapter_node_id": chapter["id"], "tone": "正式",
        "allow_anchor_overwrite": True})
    assert allowed.status_code == 200, allowed.text
    job = _wait_job(client, allowed.json()["job_id"])
    assert job["status"] == "success", job
    assert job["result"]["ok"] >= 1


def test_generate_all_is_also_guarded(client, course_id, gen_lecture_row):
    plan, chapter, sample, _body = _anchored_course(client, course_id, gen_lecture_row)
    client.post("/api/lecture/style-anchor/source-lock", json={
        "course_id": course_id, "locked": False})
    blocked = client.post("/api/lecture/generate-all", json={
        "course_id": course_id, "tone": "正式"})
    assert blocked.status_code == 409, blocked.text
    assert "样板节" in blocked.json()["detail"]

    ok = client.post("/api/lecture/generate-all", json={
        "course_id": course_id, "tone": "正式", "allow_anchor_overwrite": True})
    assert ok.status_code == 200, ok.text
    assert _wait_job(client, ok.json()["job_id"])["status"] == "success"


def test_relock_after_unlock(client, course_id, gen_lecture_row):
    _plan, chapter, sample, _body = _anchored_course(client, course_id, gen_lecture_row)
    client.post("/api/lecture/style-anchor/source-lock", json={
        "course_id": course_id, "locked": False})
    again = client.post("/api/lecture/style-anchor/source-lock", json={
        "course_id": course_id, "locked": True})
    assert again.status_code == 200 and again.json()["locked"] is True
    resp = client.post("/api/lecture/generate-chapter", json={
        "course_id": course_id, "chapter_node_id": chapter["id"], "tone": "正式"})
    assert resp.status_code == 200, resp.text
    assert _wait_job(client, resp.json()["job_id"])["status"] == "success"