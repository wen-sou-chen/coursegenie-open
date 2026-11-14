"""单租户演示模式：IP 限流、每日课程配额、全局并发 1、24 小时数据保留。

只由 ``DEMO_ONLY=true`` 启用；默认关闭时对现有行为零影响。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.base import utcnow
from app.db.session import SessionLocal
from app.models.course import Course, ContentItem, CourseVersion, GenerationJob, SourceMaterial
from app.services import storage_files

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 哪些请求算「一次生成」
# ---------------------------------------------------------------------------
# 只匹配 POST；轮询 GET /content/jobs/... 不计入配额。
_GENERATION_RE = re.compile(
    r"^(?:"
    r"/plan/generate|"
    r"/plan/[^/]+/check|"
    r"/content/generate|"
    r"/lecture/generate|/lecture/generate-all|/lecture/generate-chapter|"
    r"/lecture/[^/]+/regenerate-qc|"
    r"/concept/course/[^/]+/(?:generate|supplement)|"
    r"/chart/generate|/flowchart/generate|/image/generate|/quiz/generate|"
    r"/ppt/generate|/ppt/generate-chapters|"
    r"/material/generate|/material/[^/]+/transcribe|"
    r"/material/import-link|/material/[^/]+/import-link|"
    r"/kb/course/[^/]+/export|/publish/export-notes-zip|"
    r"/delivery/course/[^/]+/build"
    r")$"
)

_COURSE_CREATE_PATHS = frozenset({"/course", "/course/"})


def is_generation_request(path: str, method: str) -> bool:
    return method.upper() == "POST" and bool(_GENERATION_RE.match(path))


def is_course_create(path: str, method: str) -> bool:
    return method.upper() == "POST" and path in _COURSE_CREATE_PATHS


def client_ip(request: Request, settings: Settings) -> str:
    """取客户端 IP；托管在 Cloudflare 隧道后面时优先用 CF 的真实 IP。"""
    if settings.demo_trust_proxy_headers:
        for header in ("cf-connecting-ip", "x-forwarded-for"):
            value = request.headers.get(header)
            if value:
                first = value.split(",", 1)[0].strip()
                if first:
                    return first[:64]
    return (request.client.host if request.client else "unknown")[:64]


# ---------------------------------------------------------------------------
# 限流 + 并发
# ---------------------------------------------------------------------------
_rate_lock = threading.Lock()
_generation_hits: dict[str, list[float]] = {}

_slot_lock = threading.Lock()
_inflight_generations = 0


def reset_state() -> None:
    """测试/运维用：清空内存里的限流与在途计数。"""
    global _inflight_generations
    # 锁顺序必须与 acquire_generation_slot 一致（先 slot 后 rate），避免死锁。
    with _slot_lock:
        _inflight_generations = 0
        with _rate_lock:
            _generation_hits.clear()


def check_rate(ip: str, limit_per_minute: int, *, now: float | None = None) -> tuple[bool, int]:
    """滑动窗口：返回（是否放行，被拒时建议等待秒数）。"""
    if limit_per_minute <= 0:
        return True, 0
    now = time.monotonic() if now is None else now
    with _rate_lock:
        hits = [t for t in _generation_hits.get(ip, []) if now - t < 60.0]
        if len(hits) >= limit_per_minute:
            retry = max(1, int(math.ceil(60.0 - (now - hits[0]))))
            _generation_hits[ip] = hits
            return False, retry
        hits.append(now)
        _generation_hits[ip] = hits
    return True, 0


def active_job_count(db: Session) -> int:
    return int(
        db.query(GenerationJob.id)
        .filter(GenerationJob.status.in_(("pending", "running")))
        .count()
    )


def acquire_generation_slot(db: Session, settings: Settings, ip: str) -> tuple[bool, str, int]:
    """占用「全局 1 个生成任务」的槽位；返回（是否成功，人话原因，建议等待秒数）。

    背景作业本身在路由里才建行，这里用内存里的在途计数把「请求刚进来、job 还没落库」
    的窗口也挡住；作业落库后，``active_job_count`` 接管。
    """
    global _inflight_generations
    limit = max(1, int(settings.demo_max_concurrent_jobs))
    with _slot_lock:
        active = active_job_count(db)
        if active + _inflight_generations >= limit:
            return (
                False,
                "演示服务一次只跑 1 个生成任务，现在已有一个在跑。"
                "请等它结束后再试；进度可用 check_task_status 查看。",
                5,
            )
        allowed, retry = check_rate(ip, int(settings.demo_max_generations_per_minute))
        if not allowed:
            return (
                False,
                f"演示服务每个 IP 每分钟最多 {settings.demo_max_generations_per_minute} 次生成，"
                "本分钟额度已经用完。请稍等再试。",
                retry,
            )
        _inflight_generations += 1
    return True, "", 0


def release_generation_slot() -> None:
    global _inflight_generations
    with _slot_lock:
        _inflight_generations = max(0, _inflight_generations - 1)


def too_many(detail: str, retry_after: int = 0) -> JSONResponse:
    retry = max(1, int(retry_after or 1))
    return JSONResponse(
        status_code=429,
        content={"detail": detail, "retry_after": retry},
        headers={"Retry-After": str(retry)},
    )


# ---------------------------------------------------------------------------
# 每日建课配额
# ---------------------------------------------------------------------------
def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def check_daily_course_quota(db: Session, settings: Settings) -> tuple[bool, int, int]:
    """单日（滚动 24 小时）最多新建 M 门课；演示实例全局计数。"""
    limit = max(1, int(settings.demo_max_courses_per_day))
    cutoff = utcnow() - timedelta(hours=24)
    recent = sum(1 for course in db.query(Course).all() if _utc(course.created_at) >= cutoff)
    return recent < limit, recent, limit


def daily_quota_response(count: int, limit: int) -> JSONResponse:
    return too_many(
        f"演示服务每天最多新建 {limit} 门课，最近 24 小时已经建了 {count} 门。"
        "旧课程会在 24 小时后自动清理；请稍后再试。",
        3600,
    )


# ---------------------------------------------------------------------------
# 24 小时数据保留
# ---------------------------------------------------------------------------
def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _app_owned_roots(settings: Settings) -> list[Path]:
    """演示实例允许清理的根：应用自己的 storage/导出/课件目录，以及演示 vault。

    ⚠️ 只在 ``DEMO_ONLY`` 下调用；自建实例把 ``OBSIDIAN_VAULT_ROOT`` 指向真实笔记库时
    **不要**开 ``DEMO_ONLY``（部署文档里写明了）。
    """
    roots = [settings.storage_dir, settings.export_dir, settings.ppt_work_dir]
    if settings.obsidian_vault_root:
        roots.append(Path(settings.obsidian_vault_root))
    return roots


def _course_kb_dir(course: Course) -> tuple[Path | None, str | None]:
    """返回（可删除的课程笔记目录，跳过原因）。只接受应用自己的根。"""
    settings = get_settings()
    from app.services import kb as kb_service

    try:
        root = Path(kb_service.export_root_for(course)).expanduser().resolve()
    except Exception:  # noqa: BLE001
        return None, "无法解析课程笔记根目录"
    if not any(_is_within(root, allowed) for allowed in _app_owned_roots(settings)):
        return None, f"笔记根目录不在演示实例管辖内：{root}"
    rel = kb_service.course_kb(course).get("output_rel") or (
        f"课程/{kb_service._slug(course.title, 40)}"
    )
    target = (root / str(rel)).resolve()
    if not _is_within(target, root):
        return None, f"课程笔记目录越界：{rel}"
    return target, None


def _delivery_artifacts(course: Course) -> list[Path]:
    """交付包目录 / zip：靠 manifest 里的 course.id 认领，不能按标题猜。"""
    root = (get_settings().storage_dir / "deliveries").resolve()
    if not root.is_dir():
        return []
    out: list[Path] = []
    for manifest_path in root.glob("*/manifest.json"):
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str((data.get("course") or {}).get("id") or "") != course.id:
            continue
        build_dir = manifest_path.parent
        out.append(build_dir)
        zip_path = root / f"{build_dir.name}.zip"
        if zip_path.is_file():
            out.append(zip_path)
    return out


def _collect_course_artifacts(db: Session, course: Course, settings: Settings) -> tuple[list[Path], list[Path], list[str]]:
    files: dict[str, Path] = {}
    dirs: dict[str, Path] = {}
    skipped: list[str] = []

    def add_file(path: Path) -> None:
        if path.is_file():
            files[str(path.resolve())] = path

    for item in db.query(ContentItem).filter(ContentItem.course_id == course.id).all():
        for path in storage_files.item_file_refs(item):
            add_file(path)
    for material in db.query(SourceMaterial).filter(SourceMaterial.course_id == course.id).all():
        if material.file_rel:
            candidate = (settings.storage_dir / material.file_rel).resolve()
            if _is_within(candidate, settings.storage_dir):
                add_file(candidate)
    for version in db.query(CourseVersion).filter(CourseVersion.course_id == course.id).all():
        for path in storage_files.collect_image_refs_in(version.snapshot or {}):
            if _is_within(path, settings.storage_dir):
                add_file(path)
    for path in storage_files.collect_image_refs_in(course.meta or {}):
        if _is_within(path, settings.storage_dir):
            add_file(path)

    for pattern in (f"{course.id}_*", f"{course.id}*"):
        for path in settings.ppt_work_dir.glob(pattern):
            if path.is_dir():
                dirs[str(path.resolve())] = path

    for path in settings.export_dir.glob(f"{course.id}*"):
        if path.is_dir():
            dirs[str(path.resolve())] = path
        elif path.is_file():
            add_file(path)

    for path in _delivery_artifacts(course):
        if path.is_dir():
            dirs[str(path.resolve())] = path
        elif path.is_file():
            add_file(path)

    kb_dir, reason = _course_kb_dir(course)
    if kb_dir is not None and kb_dir.is_dir():
        dirs[str(kb_dir)] = kb_dir
    elif reason:
        skipped.append(reason)

    return list(files.values()), list(dirs.values()), skipped


def purge_expired_courses(
    db: Session,
    *,
    hours: int | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """删除超过保留期的课程及其素材/导出/课件/笔记目录（DB 先删，再删文件）。"""
    settings = get_settings()
    keep_hours = int(hours if hours is not None else settings.demo_retention_hours)
    cutoff = (now or utcnow()) - timedelta(hours=max(1, keep_hours))
    stats: dict[str, Any] = {
        "cutoff": cutoff.isoformat(timespec="seconds"),
        "retention_hours": keep_hours,
        "dry_run": bool(dry_run),
        "courses": 0,
        "files": 0,
        "dirs": 0,
        "bytes": 0,
        "skipped_active": [],
        "skipped_external": [],
        "errors": [],
    }

    expired = [course for course in db.query(Course).all() if _utc(course.created_at) < cutoff]
    for course in expired:
        active = (
            db.query(GenerationJob.id)
            .filter(
                GenerationJob.course_id == course.id,
                GenerationJob.status.in_(("pending", "running")),
            )
            .first()
        )
        if active is not None:
            stats["skipped_active"].append(course.id)
            continue
        files, dirs, skipped = _collect_course_artifacts(db, course, settings)
        stats["skipped_external"].extend(skipped)
        size = 0
        for path in files:
            try:
                size += path.stat().st_size
            except OSError:
                pass
        stats["files"] += len(files)
        stats["dirs"] += len(dirs)
        stats["bytes"] += size
        stats["courses"] += 1
        if dry_run:
            continue
        try:
            db.delete(course)
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            stats["errors"].append(f"{course.id}: 删除数据库行失败：{exc}")
            continue
        for path in files:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                stats["errors"].append(f"{path}: {exc}")
        for path in dirs:
            try:
                shutil.rmtree(path)
            except OSError as exc:
                stats["errors"].append(f"{path}: {exc}")

    if stats["courses"]:
        logger.info(
            "演示模式清理：删除 %s 门课、%s 个文件、%s 个目录（保留 %s 小时）",
            stats["courses"], stats["files"], stats["dirs"], keep_hours,
        )
    return stats


def purge_once(*, hours: int | None = None) -> dict[str, Any]:
    db = SessionLocal()
    try:
        return purge_expired_courses(db, hours=hours)
    finally:
        db.close()


async def cleanup_loop(app) -> None:
    """启动时先清一次，之后按保留期的 1/4 周期（5 分钟~1 小时）再清。"""
    settings = get_settings()
    interval = max(300, min(3600, int(max(1, settings.demo_retention_hours) * 3600 / 4)))
    while True:
        await asyncio.sleep(interval)
        try:
            stats = await asyncio.to_thread(purge_once)
            app.state.demo_last_purge = stats
        except Exception:  # noqa: BLE001 —— 清理失败不能拖垮主服务
            logger.exception("演示模式定期清理失败")


def health_payload(settings: Settings, last_purge: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "enabled": bool(settings.demo_only),
        "retention_hours": int(settings.demo_retention_hours),
        "max_generations_per_minute": int(settings.demo_max_generations_per_minute),
        "max_courses_per_day": int(settings.demo_max_courses_per_day),
        "max_concurrent_jobs": int(settings.demo_max_concurrent_jobs),
        "trust_proxy_headers": bool(settings.demo_trust_proxy_headers),
        "last_purge": last_purge,
    }
