"""学习数据分析路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.db.session import get_db
from app.services.analytics import course_stats, course_usage

router = APIRouter(prefix="/analytics", tags=["analytics"], dependencies=[Depends(require_api_key)])


@router.get("/{course_id}/usage")
def get_usage(course_id: str, top_n: int = 5, db: Session = Depends(get_db)):
    """课程级用量与成本（任务 23-A）：累计 tokens/费用、平均耗时、最贵 N 篇、按 kind 分布。"""
    try:
        return course_usage(db, course_id, top_n=max(1, min(50, int(top_n))))
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/{course_id}")
def get_stats(course_id: str, db: Session = Depends(get_db)):
    try:
        return course_stats(db, course_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
