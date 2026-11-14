"""PPT 服务测试：强制走 python-pptx（v1 默认引擎），验证产物为合法 PPTX。"""
from __future__ import annotations

import zipfile

from app.db.session import SessionLocal
from app.models.course import Course
from app.schemas.plan import PlanRequest
from app.services.planner import generate_outline
from app.services.ppt import generate_ppt


def test_ppt_fallback_generates_pptx():
    db = SessionLocal()
    try:
        course = Course(title="Python 入门", subject="编程", audience="零基础", duration_minutes=120)
        db.add(course)
        db.commit()
        db.refresh(course)
        generate_outline(db, course, PlanRequest(course_id=course.id, goal="Python 基础", num_chapters=3))

        item = generate_ppt(db, course, theme="theme07")
        assert item.body["engine"] == "python-pptx"
        assert item.meta["scope"] == "course"
        assert item.meta["slide_count"] > 0
        pptx_path = item.body["pptx"]
        assert zipfile.is_zipfile(pptx_path), "PPTX 必须是合法 zip"
        assert "[Content_Types].xml" in zipfile.ZipFile(pptx_path).namelist()
    finally:
        db.close()
