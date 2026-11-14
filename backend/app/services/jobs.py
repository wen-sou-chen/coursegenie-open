"""任务可靠性助手：遗留任务恢复 + 单课程互斥 + 作用域划分。

BackgroundTasks 只存在于进程内存：进程重启后任务即丢失，因此启动时把
遗留的 pending/running 任务标记为 failed，避免 job 永久 running。
同一门课的同类生成（content / lecture / ppt）同时只允许一个在跑，
用「DB 现存活动任务 + 进程内锁」双保险：DB 挡跨进程/重启后的并发，
进程内锁挡同一进程里的线程并发。
"""
from __future__ import annotations

import threading

from sqlalchemy.orm import Session

from app.models.course import GenerationJob

# job_type -> 互斥作用域。讲义单篇与整课批量都写 course 的 lecture/图表，
# 因此共用 scope="lecture"，避免同课程并发重复生成。
JOB_SCOPE: dict[str, str] = {
    "content": "content",
    "lecture": "lecture",
    "lecture_batch": "lecture",
    "ppt": "ppt",
    "concept": "concept",
}

_ACTIVE_STATUSES = ("pending", "running")

_locks: dict[tuple[str, str], threading.Lock] = {}
_locks_guard = threading.Lock()


def scope_for(job_type: str) -> str:
    return JOB_SCOPE.get(job_type, job_type)


def _scoped_types(scope: str) -> list[str]:
    return [t for t, s in JOB_SCOPE.items() if s == scope]


def reset_stale_jobs(db: Session) -> int:
    """进程启动恢复：把遗留的 pending/running 任务标记为 failed。"""
    rows = (
        db.query(GenerationJob)
        .filter(GenerationJob.status.in_(_ACTIVE_STATUSES))
        .all()
    )
    for row in rows:
        row.status = "failed"
        row.error = "服务重启导致任务中断，请重新发起生成"
    if rows:
        db.commit()
    return len(rows)


def has_active_job(
    db: Session,
    course_id: str,
    job_type: str,
    *,
    exclude_job_id: str | None = None,
) -> bool:
    """同课程同作用域是否已有未结束任务（pending/running）。"""
    q = db.query(GenerationJob.id).filter(
        GenerationJob.course_id == course_id,
        GenerationJob.job_type.in_(_scoped_types(scope_for(job_type))),
        GenerationJob.status.in_(_ACTIVE_STATUSES),
    )
    if exclude_job_id:
        q = q.filter(GenerationJob.id != exclude_job_id)
    return q.first() is not None


def acquire_scope(course_id: str, job_type: str) -> bool:
    """尝试获取进程内互斥锁（非阻塞），成功返回 True。"""
    key = (course_id, scope_for(job_type))
    with _locks_guard:
        lock = _locks.setdefault(key, threading.Lock())
    return lock.acquire(blocking=False)


def release_scope(course_id: str, job_type: str) -> None:
    """释放进程内互斥锁（幂等）。"""
    key = (course_id, scope_for(job_type))
    with _locks_guard:
        lock = _locks.get(key)
    if lock is not None:
        try:
            lock.release()
        except RuntimeError:
            pass


def claim_job_exclusive(db: Session, job: GenerationJob | None) -> bool:
    """任务开工声明：同课程同作用域已有其它未结束任务则拒绝本次并返回 False。

    路由创建任务后、真正执行前再次以 DB 为准做互斥裁决，可挡多 worker 场景
    下「两条任务都已落库」的并发窗口。
    """
    if job is None:
        return False
    if has_active_job(db, job.course_id, job.job_type, exclude_job_id=job.id):
        job.status = "failed"
        job.error = "同课程存在并发生成任务，本次任务已跳过"
        db.commit()
        return False
    job.status = "running"
    db.commit()
    return True
