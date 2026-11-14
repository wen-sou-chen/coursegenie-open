"""数据库引擎与会话管理。"""
from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


def _connect_args(url: str) -> dict:
    if url.startswith("sqlite"):
        return {"check_same_thread": False}
    return {}


settings = get_settings()
engine = create_engine(
    settings.database_url,
    connect_args=_connect_args(settings.database_url),
    pool_pre_ping=True,
)


def _enable_sqlite_fk(dbapi_connection, _connection_record) -> None:
    """SQLite 默认外键关闭；开启后删课程等操作由 DB 级 CASCADE 兜底（P1-5）。"""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


if settings.database_url.startswith("sqlite"):
    event.listen(engine, "connect", _enable_sqlite_fk)


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_sqlite_columns() -> list[str]:
    """把新加的列补进**既有** SQLite 库（``create_all`` 只建表、不改表）。

    任务 55 给 ``quiz_items`` 加了 ``meta``（分层 / 预计用时 / 评分要点）。老库启动时
    这里用 ``ALTER TABLE ... ADD COLUMN`` **幂等**补齐（SQLite 支持且不改写数据）；
    非 SQLite 交给 Alembic，返回空列表。返回补了哪些列，调用方记日志。
    """
    if not settings.database_url.startswith("sqlite"):
        return []
    wanted = {"quiz_items": {"meta": "JSON NOT NULL DEFAULT '{}'"}}
    added: list[str] = []
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in wanted.items():
            if table not in tables:
                continue
            existing = {col["name"] for col in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name in existing:
                    continue
                conn.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {ddl}')
                added.append(f"{table}.{name}")
    return added
