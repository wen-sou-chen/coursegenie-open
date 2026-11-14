"""图片生成与导入路由。"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.core.config import get_settings
from app.db.session import get_db
from app.models.course import STATUS_APPROVED, Course
from app.schemas.ppt import ImageGenerateRequest
from app.services.image import generate_image, import_image
from app.utils.uploads import UploadTooLarge, read_upload

router = APIRouter(prefix="/image", tags=["image"], dependencies=[Depends(require_api_key)])


@router.post("/generate")
def generate(payload: ImageGenerateRequest, db: Session = Depends(get_db)):
    course = db.get(Course, payload.course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    if course.status != STATUS_APPROVED:
        raise HTTPException(409, "请先在大纲步骤点「大纲通过」，锁定大纲后再生成配图")
    try:
        item = generate_image(db, course, payload.prompt, payload.size,
                              payload.item_id, node_id=payload.node_id,
                              text_lang=payload.text_lang,
                              overlay_text=payload.overlay_text,
                              overlay_pos=payload.overlay_pos,
                              model=payload.model)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except (httpx.HTTPError, RuntimeError) as e:
        # 外部图像服务失败：明确告知而非静默落占位图
        raise HTTPException(502, f"图片生成失败：{e}") from e
    return {"id": item.id, "kind": item.kind, "title": item.title, "body": item.body}


@router.post("/import")
async def import_upload(
    course_id: str = Form(...),
    item_id: str | None = Form(default=None),
    node_id: str | None = Form(default=None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """教师上传自己的图片（重做时带 item_id 原地替换）。"""
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    try:
        data = await read_upload(file, get_settings().max_upload_mb * 1024 * 1024)
    except UploadTooLarge as exc:
        raise HTTPException(413, str(exc)) from exc
    if not data:
        raise HTTPException(400, "空文件")
    item = import_image(db, course, file.filename or "image.png", data, item_id, node_id=node_id)
    return {"id": item.id, "kind": item.kind, "title": item.title, "body": item.body}
