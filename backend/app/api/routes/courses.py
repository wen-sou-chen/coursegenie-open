"""课程项目管理路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.db.session import get_db
from app.models.course import Course
from app.schemas.course import CourseCreate, CourseListItem, CourseOut, CourseUpdate
from app.services.course_deletion import delete_course_with_artifacts

router = APIRouter(prefix="/course", tags=["course"], dependencies=[Depends(require_api_key)])


@router.get("/", response_model=list[CourseListItem])
def list_courses(db: Session = Depends(get_db)):
    """课程列表（任务 48v）：走精简投影，不回 ``meta.kb`` 导出清单（可达数百 KB）。"""
    return db.query(Course).order_by(Course.updated_at.desc()).all()


@router.post("/", response_model=CourseOut)
def create_course(payload: CourseCreate, db: Session = Depends(get_db)):
    course = Course(**payload.model_dump())
    db.add(course)
    db.commit()
    db.refresh(course)
    return course


@router.get("/{course_id}", response_model=CourseOut)
def get_course(course_id: str, db: Session = Depends(get_db)):
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    return course


@router.patch("/{course_id}", response_model=CourseOut)
def update_course(course_id: str, payload: CourseUpdate, db: Session = Depends(get_db)):
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    data = payload.model_dump(exclude_none=True)
    meta = data.pop("meta", None)
    for key, value in data.items():
        setattr(course, key, value)
    if meta:
        # 任务 51：meta 走**合并**（PATCH 语义）—— 只带 K-12 字段的请求不会清掉 kb 绑定等既有键
        course.meta = {**(course.meta or {}), **meta}
    db.commit()
    db.refresh(course)
    return course


@router.delete("/{course_id}")
def delete_course(course_id: str, confirm: str | None = None, db: Session = Depends(get_db)):
    """删除整门课：``confirm`` 必须与课程全名一致（防脚本 / 误点直删）。"""
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    if confirm != course.title:
        raise HTTPException(
            400,
            f"确认名称不一致：请输入完整课程名《{course.title}》后再删除",
        )
    return delete_course_with_artifacts(db, course)
