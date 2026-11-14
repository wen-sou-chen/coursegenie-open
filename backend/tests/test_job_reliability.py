# -*- coding: utf-8 -*-
"""P0-2 任务可靠性回归：启动恢复、单课程互斥、讲义单篇入队。"""
from __future__ import annotations

import time

from app.db.session import SessionLocal
from app.models.course import GenerationJob
from app.services.jobs import acquire_scope, has_active_job, release_scope, reset_stale_jobs


def _plan_approve(client, course_id: str) -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "初中生物光合作用", "num_chapters": 2,
    })
    assert resp.status_code == 200, resp.text
    assert client.post("/api/plan/approve", json={
        "course_id": course_id, "approved": True}).status_code == 200
    return resp.json()


def _leaf(plan: dict) -> dict:
    return next((n for n in plan["nodes"] if "." in n["key"]), None) or plan["nodes"][0]


def _insert_job(course_id: str, job_type: str, status: str = "running") -> str:
    db = SessionLocal()
    try:
        job = GenerationJob(course_id=course_id, job_type=job_type, status=status)
        db.add(job)
        db.commit()
        return job.id
    finally:
        db.close()


def _wait_job(client, job_id: str, tries: int = 120) -> dict:
    for _ in range(tries):
        state = client.get(f"/api/content/jobs/{job_id}").json()
        if state["status"] in ("success", "failed"):
            return state
        time.sleep(0.1)
    raise AssertionError(f"job timeout: {job_id}")


def test_restart_marks_stale_running_job_failed(client, course_id):
    job_id = _insert_job(course_id, "content", status="running")
    db = SessionLocal()
    try:
        assert reset_stale_jobs(db) >= 1
    finally:
        db.close()
    state = client.get(f"/api/content/jobs/{job_id}").json()
    assert state["status"] == "failed"
    assert "重启" in state["error"]


def test_scope_locks_shared_between_lecture_and_batch():
    assert acquire_scope("unit-scope-c", "lecture")
    assert not acquire_scope("unit-scope-c", "lecture_batch"), "单篇与整课批量应互斥"
    assert acquire_scope("unit-scope-c", "content"), "不同作用域可并行"
    release_scope("unit-scope-c", "lecture")
    release_scope("unit-scope-c", "content")


def test_same_course_active_content_job_conflicts(client, course_id):
    _plan_approve(client, course_id)
    job_id = _insert_job(course_id, "content", status="running")
    resp = client.post("/api/content/generate", json={"course_id": course_id, "tone": "正式"})
    assert resp.status_code == 409, resp.text
    assert "进行中" in resp.text

    db = SessionLocal()
    try:
        job = db.get(GenerationJob, job_id)
        job.status = "failed"
        db.commit()
    finally:
        db.close()
    resp2 = client.post("/api/content/generate", json={"course_id": course_id, "tone": "正式"})
    assert resp2.status_code == 200, resp2.text


def test_lecture_batch_blocks_single_lecture_for_same_course(client, course_id):
    _plan_approve(client, course_id)
    node = _leaf(client.get(f"/api/plan/{course_id}").json())
    _insert_job(course_id, "lecture_batch", status="running")
    resp = client.post("/api/lecture/generate", json={
        "course_id": course_id, "node_id": node["id"], "tone": "正式",
    })
    assert resp.status_code == 409, resp.text


def test_single_lecture_enqueued_as_job_and_returns_row(client, course_id):
    plan = _plan_approve(client, course_id)
    node = _leaf(plan)
    resp = client.post("/api/lecture/generate", json={
        "course_id": course_id, "node_id": node["id"], "tone": "正式",
    })
    assert resp.status_code == 200, resp.text
    queued = resp.json()
    assert queued["status"] == "running" and "job_id" in queued
    state = _wait_job(client, queued["job_id"])
    assert state["status"] == "success", state
    item = state["result"]["item"]
    assert item["kind"] == "lecture"
    assert item["node_id"] == node["id"]
    rows = client.get(f"/api/content/{course_id}").json()
    assert any(r["id"] == item["id"] and r["kind"] == "lecture" for r in rows)


def test_single_lecture_job_failed_records_error(client, course_id, monkeypatch):
    plan = _plan_approve(client, course_id)
    node = _leaf(plan)

    import app.api.routes.lecture as lecture_routes

    def _boom(*args, **kwargs):
        raise RuntimeError("模拟讲义生成崩溃")

    monkeypatch.setattr(lecture_routes, "generate_lecture", _boom)
    resp = client.post("/api/lecture/generate", json={
        "course_id": course_id, "node_id": node["id"], "tone": "正式",
    })
    assert resp.status_code == 200, resp.text
    state = _wait_job(client, resp.json()["job_id"])
    assert state["status"] == "failed"
    assert "模拟讲义生成崩溃" in state["error"]
