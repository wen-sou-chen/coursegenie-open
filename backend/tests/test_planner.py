"""规划服务测试。"""
from __future__ import annotations

from app.db.session import SessionLocal
from app.models.course import Course
from app.schemas.plan import PlanRequest
from app.services.planner import generate_outline, list_nodes


def test_generate_outline_creates_nodes():
    db = SessionLocal()
    try:
        course = Course(title="Python 入门", subject="编程", audience="零基础", duration_minutes=120)
        db.add(course)
        db.commit()
        db.refresh(course)

        outline = generate_outline(
            db, course, PlanRequest(course_id=course.id, goal="掌握 Python 基础", num_chapters=3)
        )
        assert outline.id
        nodes = list_nodes(db, course.id)
        assert len(nodes) >= 3
        assert nodes[0].node_key == "1"
        assert nodes[1].prerequisites == ["1"]  # 章节依赖链条
    finally:
        db.close()
