"""MVP 用户模型：注册/登录返回 API Key（认证默认关闭，开启后接口需 X-API-Key）。"""
from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 120_000)
    return digest.hex(), salt


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: os.urandom(16).hex())
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    password_salt: Mapped[str] = mapped_column(String(64))
    api_key: Mapped[str] = mapped_column(String(64), unique=True, default=lambda: secrets.token_hex(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @classmethod
    def create(cls, username: str, password: str) -> "User":
        digest, salt = hash_password(password)
        return cls(username=username, password_hash=digest, password_salt=salt)

    def verify_password(self, password: str) -> bool:
        digest, _ = hash_password(password, self.password_salt)
        return secrets.compare_digest(digest, self.password_hash)
