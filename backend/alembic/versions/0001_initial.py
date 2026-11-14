"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-01
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(128), nullable=False),
        sa.Column("password_salt", sa.String(64), nullable=False),
        sa.Column("api_key", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "courses",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("subject", sa.String(100), nullable=False, server_default=""),
        sa.Column("audience", sa.String(255), nullable=False, server_default=""),
        sa.Column("duration_minutes", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("meta", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "outlines",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("course_id", sa.String(32), sa.ForeignKey("courses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False, server_default=""),
        sa.Column("content", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "outline_nodes",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("outline_id", sa.String(32), sa.ForeignKey("outlines.id", ondelete="CASCADE"), nullable=False),
        sa.Column("node_key", sa.String(20), nullable=False),
        sa.Column("node_type", sa.String(20), nullable=False, server_default="section"),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False, server_default=""),
        sa.Column("teaching_strategy", sa.Text(), nullable=False, server_default=""),
        sa.Column("assessment", sa.Text(), nullable=False, server_default=""),
        sa.Column("prerequisites", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("content_md", sa.Text(), nullable=False, server_default=""),
        sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "content_items",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("course_id", sa.String(32), sa.ForeignKey("courses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("node_id", sa.String(32), sa.ForeignKey("outline_nodes.id", ondelete="SET NULL"), nullable=True),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("title", sa.String(255), nullable=False, server_default=""),
        sa.Column("body", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("meta", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "quiz_items",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("course_id", sa.String(32), sa.ForeignKey("courses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("node_id", sa.String(32), sa.ForeignKey("outline_nodes.id", ondelete="SET NULL"), nullable=True),
        sa.Column("qtype", sa.String(20), nullable=False, server_default="choice"),
        sa.Column("difficulty", sa.String(10), nullable=False, server_default="medium"),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("answer", sa.Text(), nullable=False, server_default=""),
        sa.Column("explanation", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "course_versions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("course_id", sa.String(32), sa.ForeignKey("courses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("note", sa.String(255), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "generation_jobs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("course_id", sa.String(32), sa.ForeignKey("courses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_type", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("result", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    for table in (
        "generation_jobs",
        "course_versions",
        "quiz_items",
        "content_items",
        "outline_nodes",
        "outlines",
        "courses",
        "users",
    ):
        op.drop_table(table)
