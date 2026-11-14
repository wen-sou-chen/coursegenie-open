"""流程图生成与导入路由。"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.db.session import get_db
from app.models.course import STATUS_APPROVED, Course
from app.schemas.kb import KbImportFlowchart
from app.schemas.ppt import FlowchartGenerateRequest
from app.services.flowchart import generate_flowchart, import_flowchart

router = APIRouter(prefix="/flowchart", tags=["flowchart"], dependencies=[Depends(require_api_key)])


@router.post("/generate")
def generate(payload: FlowchartGenerateRequest, db: Session = Depends(get_db)):
    course = db.get(Course, payload.course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    if course.status != STATUS_APPROVED:
        raise HTTPException(409, "请先在大纲步骤点「大纲通过」，锁定大纲后再生成流程图")
    try:
        item = generate_flowchart(db, course, payload.description, payload.kind, payload.item_id,
                                  node_id=payload.node_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"流程图生成失败（LLM 服务暂不可用或限流）：{exc}") from exc
    return {"id": item.id, "kind": item.kind, "title": item.title, "body": item.body}


@router.post("/import")
def import_user(payload: KbImportFlowchart, db: Session = Depends(get_db)):
    """教师自绘/粘贴 Mermaid 源码导入（重做时带 item_id 原地替换）。"""
    course = db.get(Course, payload.course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    try:
        item = import_flowchart(db, course, payload.title, payload.mermaid, payload.item_id,
                                node_id=payload.node_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": item.id, "kind": item.kind, "title": item.title, "body": item.body}
