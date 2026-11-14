"""课程领域模型：课程 / 大纲 / 知识点 / 内容 / 习题 / 版本 / 任务。"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utcnow


def new_id() -> str:
    return uuid.uuid4().hex


# 课程阶段状态：草稿 -> 已生成大纲(planned) -> 大纲已通过(approved，锁定为生成依据)
STATUS_DRAFT = "draft"
STATUS_PLANNED = "planned"
STATUS_APPROVED = "approved"


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(100), default="")
    audience: Mapped[str] = mapped_column(String(255), default="")
    duration_minutes: Mapped[int] = mapped_column(Integer, default=60)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft/planned/approved
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    outlines: Mapped[list["Outline"]] = relationship(back_populates="course", cascade="all, delete-orphan")
    content_items: Mapped[list["ContentItem"]] = relationship(back_populates="course", cascade="all, delete-orphan")
    quizzes: Mapped[list["QuizItem"]] = relationship(back_populates="course", cascade="all, delete-orphan")
    versions: Mapped[list["CourseVersion"]] = relationship(back_populates="course", cascade="all, delete-orphan")
    source_materials: Mapped[list["SourceMaterial"]] = relationship(
        back_populates="course", cascade="all, delete-orphan")
    generation_jobs: Mapped[list["GenerationJob"]] = relationship(
        back_populates="course", cascade="all, delete-orphan")


class Outline(Base):
    __tablename__ = "outlines"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(255))
    goal: Mapped[str] = mapped_column(Text, default="")
    # 原始 LLM 输出（任务 86 起多一个 scenario_used 键 = 生成时的口径标记，见 services/scenario.py）
    content: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    course: Mapped[Course] = relationship(back_populates="outlines")
    nodes: Mapped[list["OutlineNode"]] = relationship(back_populates="outline", cascade="all, delete-orphan")


class OutlineNode(Base):
    __tablename__ = "outline_nodes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    outline_id: Mapped[str] = mapped_column(ForeignKey("outlines.id", ondelete="CASCADE"))
    node_key: Mapped[str] = mapped_column(String(20))  # 如 "1.2"
    node_type: Mapped[str] = mapped_column(String(20), default="section")  # chapter/section/point
    title: Mapped[str] = mapped_column(String(255))
    objective: Mapped[str] = mapped_column(Text, default="")
    teaching_strategy: Mapped[str] = mapped_column(Text, default="")
    assessment: Mapped[str] = mapped_column(Text, default="")
    prerequisites: Mapped[list] = mapped_column(JSON, default=list)  # 前置知识点 key 列表
    content_md: Mapped[str] = mapped_column(Text, default="")
    order_index: Mapped[int] = mapped_column(Integer, default=0)

    outline: Mapped[Outline] = relationship(back_populates="nodes")


class ContentItem(Base):
    __tablename__ = "content_items"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"))
    node_id: Mapped[str | None] = mapped_column(ForeignKey("outline_nodes.id", ondelete="SET NULL"), nullable=True)
    kind: Mapped[str] = mapped_column(String(30))  # text/flowchart/image/ppt/quiz
    title: Mapped[str] = mapped_column(String(255), default="")
    body: Mapped[dict] = mapped_column(JSON, default=dict)  # {markdown: str, summary: str, keywords: []}
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    course: Mapped[Course] = relationship(back_populates="content_items")


class QuizItem(Base):
    __tablename__ = "quiz_items"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"))
    node_id: Mapped[str | None] = mapped_column(ForeignKey("outline_nodes.id", ondelete="SET NULL"), nullable=True)
    qtype: Mapped[str] = mapped_column(String(20), default="choice")  # choice/blank/short
    difficulty: Mapped[str] = mapped_column(String(10), default="medium")  # easy/medium/hard
    question: Mapped[str] = mapped_column(Text)
    options: Mapped[list] = mapped_column(JSON, default=list)
    answer: Mapped[str] = mapped_column(Text, default="")
    explanation: Mapped[str] = mapped_column(Text, default="")
    # 任务 55：分层（level=基础/提升/挑战）/ 预计用时（minutes）/ 开放题评分要点（rubric）
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    course: Mapped[Course] = relationship(back_populates="quizzes")


class CourseVersion(Base):
    __tablename__ = "course_versions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"))
    version_no: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    course: Mapped[Course] = relationship(back_populates="versions")


class GenerationJob(Base):
    __tablename__ = "generation_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"))
    job_type: Mapped[str] = mapped_column(String(30))  # content/ppt/quiz/flowchart/image
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/running/success/failed
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    course: Mapped[Course] = relationship(back_populates="generation_jobs")


class SourceMaterial(Base):
    """教师提供的原始资料（素材库）：文件或粘贴文本，文本供 LLM 生成时注入。"""

    __tablename__ = "source_materials"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(20), default="text")  # text/file
    file_rel: Mapped[str] = mapped_column(String(500), default="")  # storage/materials/<name>
    text: Mapped[str] = mapped_column(Text, default="")  # 提取后的正文（注入用）
    meta: Mapped[dict] = mapped_column(JSON, default=dict)  # 原文件名/ext/字数等
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    course: Mapped[Course] = relationship(back_populates="source_materials")

