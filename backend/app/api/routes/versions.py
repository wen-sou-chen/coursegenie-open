"""版本控制路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.db.session import get_db
from app.services.timefmt import as_utc_iso
from app.services.versions import list_versions, restore, snapshot

router = APIRouter(prefix="/version", tags=["version"], dependencies=[Depends(require_api_key)])


@router.post("/{course_id}/snapshot")
def create_snapshot(course_id: str, db: Session = Depends(get_db), note: str = ""):
    try:
        version = snapshot(db, course_id, note)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"version_id": version.id, "version_no": version.version_no, "note": version.note}


@router.get("/{course_id}")
def get_versions(course_id: str, db: Session = Depends(get_db)):
    # 任务 83 §B1：`created_at` 落库时是 utcnow()，但 SQLite 不存时区 ⇒ 读回来是 naive，原样
    # isoformat() 出来没有 +00:00，前端 `new Date()` 会当本地时间解释（少 8 小时）⇒ 统一补时区。
    return [
        {"id": v.id, "version_no": v.version_no, "note": v.note,
         "created_at": as_utc_iso(v.created_at)}
        for v in list_versions(db, course_id)
    ]


@router.post("/{course_id}/restore/{version_id}")
def restore_version(course_id: str, version_id: str, db: Session = Depends(get_db)):
    try:
        restore(db, course_id, version_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True, "message": f"已回滚到版本 {version_id}"}
