"""发布与导出路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.db.session import get_db
from app.models.course import Course
from app.services.publish import (
    export_html_package,
    export_notes_zip,
    export_scorm_package_with_report,
    publish,
)

router = APIRouter(prefix="/publish", tags=["publish"], dependencies=[Depends(require_api_key)])


class PublishRequest(BaseModel):
    course_id: str
    platform: str
    payload: dict = {}


class ExportRequest(BaseModel):
    course_id: str
    format: str = "html"  # html | scorm


class NotesZipRequest(BaseModel):
    course_id: str
    scope: str = "all"  # all | nodes
    node_ids: list[str] = []


@router.post("/export")
def export(payload: ExportRequest, db: Session = Depends(get_db)):
    course = db.get(Course, payload.course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    report: dict = {}
    if payload.format == "html":
        path = export_html_package(db, course)
    elif payload.format == "scorm":
        try:
            path, report = export_scorm_package_with_report(db, course)
        except ValueError as exc:      # 自检不通过 ⇒ 不落盘，把原因原样返回
            raise HTTPException(500, str(exc)) from exc
    else:
        raise HTTPException(400, f"不支持的格式：{payload.format}")
    return {"ok": True, "file": str(path).replace("\\", "/"), "format": payload.format,
            "url": f"/exports/{path.name}", "self_check": report}


@router.post("/export-notes-zip")
def export_notes(payload: NotesZipRequest, db: Session = Depends(get_db)):
    """把课程笔记打包为 ZIP：整门课或所选知识点（含依赖图片/习题/可编辑图）。"""
    course = db.get(Course, payload.course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    if payload.scope not in ("all", "nodes"):
        raise HTTPException(400, "scope 仅支持 all / nodes")
    try:
        path = export_notes_zip(db, course, payload.scope, payload.node_ids)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "file": path.name, "url": f"/exports/{path.name}", "scope": payload.scope}


@router.post("/")
def do_publish(payload: PublishRequest, db: Session = Depends(get_db)):
    course = db.get(Course, payload.course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    return publish(db, course, payload.platform, payload.payload)
