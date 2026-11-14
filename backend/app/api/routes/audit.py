"""课程资产体检报告路由：八段报告预览 + 生成并写入笔记库。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.db.session import get_db
from app.models.course import Course
from app.services import audit as audit_service

router = APIRouter(prefix="/audit", tags=["audit"], dependencies=[Depends(require_api_key)])


def _course_or_404(db: Session, course_id: str) -> Course:
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    return course


@router.post("/course/{course_id}/report")
def write_report(course_id: str, db: Session = Depends(get_db)):
    """生成课程资产体检报告并写入 课程/<课名>/00-课程体检报告.md。"""
    course = _course_or_404(db, course_id)
    try:
        return audit_service.write_report(db, course)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/course/{course_id}/overview")
def preview_overview(course_id: str, db: Session = Depends(get_db)):
    """首屏轻量概览（任务 48z）：不跑整库笔记一致性检查，实测 <0.5 秒。

    面板先拿这一段渲染覆盖度/链接/质检/用量/导出健康，再并行请求下面的完整 report
    补齐一致性、结论与建议动作 —— 判定口径一个字没改。
    """
    course = _course_or_404(db, course_id)
    return audit_service.course_overview(db, course)


@router.get("/course/{course_id}/report")
def preview_report(course_id: str, db: Session = Depends(get_db)):
    """预览八段体检报告（不写盘），供网页端「资产体检」页签每次打开刷新。"""
    course = _course_or_404(db, course_id)
    return audit_service.course_report(db, course)
