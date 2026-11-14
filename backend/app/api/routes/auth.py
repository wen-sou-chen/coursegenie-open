"""MVP 认证路由：注册/登录返回 API Key（auth_enabled=true 时接口需带 X-API-Key）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.user import User

router = APIRouter(prefix="/auth", tags=["auth"])


class AuthRequest(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=6, max_length=128)


@router.post("/register")
def register(payload: AuthRequest, db: Session = Depends(get_db)):
    if db.query(User).filter(User.username == payload.username).first():
        raise HTTPException(409, "用户名已存在")
    user = User.create(payload.username, payload.password)
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"username": user.username, "api_key": user.api_key}


@router.post("/login")
def login(payload: AuthRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == payload.username).first()
    if user is None or not user.verify_password(payload.password):
        raise HTTPException(401, "用户名或密码错误")
    return {"username": user.username, "api_key": user.api_key}
