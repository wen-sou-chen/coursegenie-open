"""运维接口：孤儿文件 GC（仅清理 backend/storage 运行时产物，不触碰用户 Vault）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.db.session import get_db
from app.services.storage_files import gc_images

router = APIRouter(prefix="/maintenance", tags=["maintenance"], dependencies=[Depends(require_api_key)])


class GcImagesRequest(BaseModel):
    dry_run: bool = True


@router.post("/gc-images")
def gc_images_endpoint(payload: GcImagesRequest | None = None, db: Session = Depends(get_db)) -> dict:
    """清理 storage/images 下未被 DB 引用的文件；默认 dry_run=True 只统计。"""
    dry_run = payload.dry_run if payload is not None else True
    return gc_images(db, dry_run=dry_run)
