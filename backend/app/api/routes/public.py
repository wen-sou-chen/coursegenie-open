"""公开侧只读分享路由（M5a）：免登录、仅凭 token。

约定：
- 任何写方法（POST/PATCH/PUT/DELETE）在本模块的路由上都会由 Starlette 直接回 405；
- token 无效 → 403；已撤销 / 已过期 → 410；密码缺失或错误 → 401；
- 只转发文本与图片资源，绝不暴露 pptx/pdf 下载直链、vault 与素材原文。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_db
from app.models.course import ContentItem, Course
from app.services import audience as audience_service
from app.services import share as share_service

router = APIRouter(prefix="/public", tags=["public-share"])

# 公开侧只允许读这些后缀（挡住 .pptx / .pdf 直接下载）
PUBLIC_FILE_SUFFIXES = {".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp"}


def _password_of(request: Request) -> str | None:
    """密码放 header（推荐）或 query 均可；query 里的密码不会写进访问日志。"""
    return request.headers.get("X-Share-Password") or request.query_params.get("password")


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for") or ""
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _authorize(request: Request, token: str, db: Session,
               course_id: str | None = None) -> tuple:
    """校验 token（+密码）→ 取课程 → 校验范围 → 记一条访问日志。"""
    try:
        link = share_service.resolve_token(db, token, _password_of(request))
    except share_service.ShareAccessError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    course = db.get(Course, link.course_id)
    if course is None:
        raise HTTPException(404, "课程不存在或已被删除")
    if course_id and course_id != course.id:
        raise HTTPException(403, "该分享链接只能访问被授权的那门课程")
    share_service.record_access(
        db, link,
        path=request.url.path,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent") or "",
    )
    return link, course


def _safe_file(base: Path, rel: str) -> Path:
    """把相对路径解析到 base 下并挡住目录穿越；找不到就 404。"""
    try:
        root = base.resolve()
        cand = (root / rel).resolve()
        cand.relative_to(root)
    except (ValueError, OSError):
        raise HTTPException(404, "资源不存在") from None
    if not cand.is_file():
        raise HTTPException(404, "资源不存在")
    return cand


def _deck_prefixes(db: Session, course_id: str) -> list[str]:
    """该课程每个课件所在目录（相对 ppt_work_dir）——公开侧只能读这些目录里的图片。"""
    base = get_settings().ppt_work_dir.resolve()
    prefixes: list[str] = []
    items = (db.query(ContentItem)
             .filter(ContentItem.course_id == course_id, ContentItem.kind == "ppt").all())
    for item in items:
        raw = str((item.body or {}).get("pptx") or "")
        if not raw:
            continue
        try:
            path = Path(raw)
            cand = (path if path.is_absolute() else base / path).resolve()
            rel = cand.relative_to(base).as_posix()
        except (ValueError, OSError):
            continue
        prefixes.append(rel.rsplit("/", 1)[0] if "/" in rel else "")
    return prefixes


@router.get("/{token}")
def public_overview(token: str, request: Request,
                    course_id: str | None = Query(default=None),
                    db: Session = Depends(get_db)):
    link, course = _authorize(request, token, db, course_id)
    return share_service.public_overview(db, course, link)


@router.get("/{token}/plan")
def public_plan(token: str, request: Request,
                course_id: str | None = Query(default=None),
                db: Session = Depends(get_db)):
    _, course = _authorize(request, token, db, course_id)
    return share_service.public_plan(db, course)


@router.get("/{token}/content")
def public_content(token: str, request: Request,
                   course_id: str | None = Query(default=None),
                   view: str = Query(default=audience_service.AUDIENCE_TEACHER,
                                     description=("答案可见性（任务 86）：teacher=教师版"
                                                  "（默认，含答案与解析）/ student=学员版"
                                                  "（讲义去答案与解析、习题清空答案字段）")),
                   db: Session = Depends(get_db)):
    _, course = _authorize(request, token, db, course_id)
    return share_service.public_content(db, course, audience_view=view)


@router.get("/{token}/ppt")
def public_ppt(token: str, request: Request,
               course_id: str | None = Query(default=None),
               db: Session = Depends(get_db)):
    _, course = _authorize(request, token, db, course_id)
    return share_service.public_ppt(db, course, token)


@router.get("/{token}/images/{name:path}")
def public_image(token: str, name: str, request: Request,
                 db: Session = Depends(get_db)):
    _authorize(request, token, db)
    return FileResponse(_safe_file(get_settings().storage_dir / "images", name))


@router.get("/{token}/ppt-files/{rel:path}")
def public_ppt_file(token: str, rel: str, request: Request,
                    db: Session = Depends(get_db)):
    """代理 /ppt-files：只放行该课程课件目录下的图片，pptx/pdf 一律 404。"""
    _, course = _authorize(request, token, db)
    if Path(rel).suffix.lower() not in PUBLIC_FILE_SUFFIXES:
        raise HTTPException(404, "资源不存在")
    prefixes = _deck_prefixes(db, course.id)
    if not any(not p or rel.startswith(f"{p}/") for p in prefixes):
        raise HTTPException(404, "资源不存在")
    return FileResponse(_safe_file(get_settings().ppt_work_dir, rel))