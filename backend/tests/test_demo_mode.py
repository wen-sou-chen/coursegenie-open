"""任务 154：单租户演示模式（DEMO_ONLY）的配额、限流与 24h 清理。"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.config import get_settings
from app.db.base import utcnow
from app.db.session import SessionLocal
from app.models.course import Course, GenerationJob, SourceMaterial
from app.services import demo_mode


@pytest.fixture(autouse=True)
def _reset_demo_state():
    demo_mode.reset_state()
    yield
    demo_mode.reset_state()


def _make_course(client, title: str = "演示配额探针课") -> str:
    resp = client.post("/api/course/", json={"title": title, "subject": "生物",
                                             "audience": "初中生", "duration_minutes": 60})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _recent_course_count() -> int:
    db = SessionLocal()
    try:
        cutoff = utcnow() - timedelta(hours=24)
        return sum(1 for c in db.query(Course).all()
                   if demo_mode._utc(c.created_at) >= cutoff)
    finally:
        db.close()


class _FakeQuery:
    def filter(self, *args, **kwargs):
        return self

    def count(self) -> int:
        return 0


class _FakeDb:
    def query(self, *args, **kwargs):
        return _FakeQuery()


def test_health_reports_demo_limits(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "demo_only", True)
    body = client.get("/api/health").json()
    assert body["demo_only"] is True
    assert body["demo"]["enabled"] is True
    assert body["demo"]["retention_hours"] == 24
    assert body["demo"]["max_concurrent_jobs"] == 1


def test_demo_blocks_account_registration(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "demo_only", True)
    resp = client.post("/api/auth/register", json={"username": "someone", "password": "secret1"})
    assert resp.status_code == 403
    assert "不提供账号" in resp.json()["detail"]


def test_daily_course_quota_returns_429(client, monkeypatch):
    recent = _recent_course_count()
    limit = recent + 1
    monkeypatch.setattr(get_settings(), "demo_only", True)
    monkeypatch.setattr(get_settings(), "demo_max_courses_per_day", limit)
    first = _make_course(client, "配额内课程")
    try:
        second = client.post("/api/course/", json={"title": "超配额课程"})
        assert second.status_code == 429, second.text
        body = second.json()
        assert "每天最多新建" in body["detail"]
        assert second.headers.get("Retry-After")
    finally:
        title = client.get(f"/api/course/{first}").json()["title"]
        client.delete(f"/api/course/{first}", params={"confirm": title})


def test_generation_rate_limit_returns_429(client, monkeypatch):
    cid = _make_course(client, "限流探针课")
    demo_mode.reset_state()
    monkeypatch.setattr(get_settings(), "demo_only", True)
    monkeypatch.setattr(get_settings(), "demo_max_generations_per_minute", 1)
    monkeypatch.setattr(get_settings(), "demo_max_concurrent_jobs", 10)
    payload = {"course_id": cid, "goal": "让零基础学员理解光合作用"}
    first = client.post("/api/plan/generate", json=payload)
    assert first.status_code == 200, first.text
    second = client.post("/api/plan/generate", json=payload)
    assert second.status_code == 429, second.text
    assert "每分钟最多 1 次" in second.json()["detail"]
    assert second.headers.get("Retry-After")


def test_concurrency_slot_is_global(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "demo_max_concurrent_jobs", 1)
    monkeypatch.setattr(settings, "demo_max_generations_per_minute", 99)
    demo_mode.reset_state()
    fake = _FakeDb()
    try:
        ok, _detail, _retry = demo_mode.acquire_generation_slot(fake, settings, "1.2.3.4")
        assert ok is True
        blocked, detail, _retry = demo_mode.acquire_generation_slot(fake, settings, "1.2.3.4")
        assert blocked is False
        assert "只跑 1 个生成任务" in detail
    finally:
        demo_mode.release_generation_slot()
    ok, _detail, _retry = demo_mode.acquire_generation_slot(fake, settings, "1.2.3.4")
    assert ok is True
    demo_mode.release_generation_slot()


def test_purge_expired_course_removes_db_files_and_dirs(client):
    settings = get_settings()
    cid = _make_course(client, "过期清理探针课")

    material_dir = settings.storage_dir / "materials"
    material_dir.mkdir(parents=True, exist_ok=True)
    material = material_dir / f"{cid}_old.md"
    material.write_text("old material", encoding="utf-8")

    ppt_dir = settings.ppt_work_dir / f"{cid}_course"
    ppt_dir.mkdir(parents=True, exist_ok=True)
    (ppt_dir / "deck.pptx").write_bytes(b"old deck")

    export_zip = settings.export_dir / f"{cid}_notes_all.zip"
    export_zip.parent.mkdir(parents=True, exist_ok=True)
    export_zip.write_bytes(b"old zip")

    db = SessionLocal()
    try:
        course = db.get(Course, cid)
        assert course is not None
        course.created_at = utcnow() - timedelta(hours=25)
        db.add(SourceMaterial(course_id=cid, title="旧素材", kind="file",
                              file_rel=f"materials/{material.name}"))
        db.commit()
        kb_dir = demo_mode._course_kb_dir(course)[0]
        assert kb_dir is not None
        kb_dir.mkdir(parents=True, exist_ok=True)
        (kb_dir / "old.md").write_text("old note", encoding="utf-8")

        # dry-run 不删任何东西
        dry = demo_mode.purge_expired_courses(db, hours=24, dry_run=True)
        assert dry["courses"] >= 1
        assert db.get(Course, cid) is not None
        assert material.exists()

        stats = demo_mode.purge_expired_courses(db, hours=24)
        assert stats["courses"] >= 1
        assert not stats["errors"], stats["errors"]
        assert db.get(Course, cid) is None
        assert not material.exists()
        assert not ppt_dir.exists()
        assert not export_zip.exists()
        assert not kb_dir.exists()
    finally:
        db.close()


def test_purge_skips_course_with_active_job(client):
    cid = _make_course(client, "运行中不清理探针课")
    db = SessionLocal()
    try:
        course = db.get(Course, cid)
        course.created_at = utcnow() - timedelta(hours=25)
        job = GenerationJob(course_id=cid, job_type="content", status="running")
        db.add(job)
        db.commit()

        stats = demo_mode.purge_expired_courses(db, hours=24)
        assert cid in stats["skipped_active"]
        assert db.get(Course, cid) is not None

        job.status = "failed"
        db.commit()
        stats = demo_mode.purge_expired_courses(db, hours=24)
        assert db.get(Course, cid) is None
    finally:
        db.close()


def test_rate_window_counts_per_ip(monkeypatch):
    demo_mode.reset_state()
    assert demo_mode.check_rate("a", 2)[0] is True
    assert demo_mode.check_rate("a", 2)[0] is True
    allowed, retry = demo_mode.check_rate("a", 2)
    assert allowed is False and retry >= 1
    assert demo_mode.check_rate("b", 2)[0] is True


def test_kb_dir_outside_demo_roots_is_not_deleted(client, monkeypatch, tmp_path):
    """外部真实笔记库（不在演示实例管辖内）只报 skipped，不删。"""
    settings = get_settings()
    cid = _make_course(client, "外部笔记库探针课")
    outside = tmp_path / "real_vault"
    outside.mkdir()
    course_dir = outside / "课程" / "外部笔记库探针课"
    course_dir.mkdir(parents=True)
    (course_dir / "note.md").write_text("keep me", encoding="utf-8")

    db = SessionLocal()
    try:
        course = db.get(Course, cid)
        course.created_at = utcnow() - timedelta(hours=25)
        course.meta = {**(course.meta or {}), "kb": {"vault_root": str(outside)}}
        db.commit()
        stats = demo_mode.purge_expired_courses(db, hours=24)
        assert any("不在演示实例管辖内" in reason for reason in stats["skipped_external"])
        assert course_dir.exists()
    finally:
        db.close()
