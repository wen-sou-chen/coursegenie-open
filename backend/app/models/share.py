"""分享链接模型（M5a）：课程级只读分享 + 访问留痕。

安全约定：token 明文只在创建响应里返回一次，库里只存 sha256 哈希；
密码只存 pbkdf2 的 salt+digest（复用 app.models.user.hash_password）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, utcnow
from app.models.course import new_id

PERMISSION_VIEW = "view"
PERMISSION_COMMENT = "comment"  # 预留：M5b 意见闭环用


class ShareLink(Base):
    """一门课 + 一个 token：只读、可设有效期、可撤销、可加密码。"""

    __tablename__ = "share_links"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    course_id: Mapped[str] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), index=True)
    label: Mapped[str] = mapped_column(String(255), default="")  # 给谁用/备注
    permission: Mapped[str] = mapped_column(String(20), default=PERMISSION_VIEW)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(200), default="")  # 空 = 无密码
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    access_count: Mapped[int] = mapped_column(Integer, default=0)
    last_accessed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


class ShareAccessLog(Base):
    """公开侧每次成功访问留一条痕：谁、什么时候、看了哪个接口/资源。"""

    __tablename__ = "share_access_logs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    link_id: Mapped[str] = mapped_column(
        ForeignKey("share_links.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(500), default="")
    path: Mapped[str] = mapped_column(String(500), default="")