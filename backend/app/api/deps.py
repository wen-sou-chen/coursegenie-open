"""认证依赖：auth_enabled=False（默认）时全部放行；开启后校验 X-API-Key。"""
from __future__ import annotations

from fastapi import Header, HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.user import User


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if not settings.auth_enabled:
        return
    if not x_api_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少 X-API-Key")
    if x_api_key == settings.dev_api_key:
        return
    db: Session = SessionLocal()
    try:
        user = db.query(User).filter(User.api_key == x_api_key).first()
    finally:
        db.close()
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "无效 API Key")
