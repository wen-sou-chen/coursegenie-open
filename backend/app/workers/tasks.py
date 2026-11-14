"""异步任务：MVP 用 FastAPI BackgroundTasks；预留 Celery 入口。"""
from __future__ import annotations

from typing import Callable

from app.db.session import SessionLocal


def run_background(fn: Callable, *args) -> dict:
    """在独立 DB 会话中执行耗时任务（BackgroundTasks 使用）。"""
    db = SessionLocal()
    try:
        return fn(db, *args)
    finally:
        db.close()


try:  # Celery 可选（docker-compose 提供 redis/worker 后可启用）
    from celery import Celery  # type: ignore

    celery_app = Celery("coursegenie", broker="redis://localhost:6379/0")

    @celery_app.task
    def generate_course_content_async(course_id: str, tone: str) -> dict:
        from app.services.content import generate_all_texts

        db = SessionLocal()
        try:
            count = generate_all_texts(db, course_id, [], tone)
            return {"ok": True, "count": count}
        finally:
            db.close()

except Exception:  # celery 未安装时静默降级
    celery_app = None  # type: ignore[assignment]
