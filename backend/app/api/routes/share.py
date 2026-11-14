"""分享链接路由（M5a 管理侧）：创建 / 列表 / 撤销 / 删除。

- 沿用现有鉴权（dependencies=[Depends(require_api_key)]）；
- 明文 token 只在创建响应里返回一次，列表接口永远不含它；
- 修 bug 时注意：公开侧接口在 app/api/routes/public.py，绝不能带这里的鉴权依赖。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.db.session import get_db
from app.models.course import Course
from app.schemas.share import ShareCreateRequest
from app.services import share as share_service

router = APIRouter(prefix="/share", tags=["share"], dependencies=[Depends(require_api_key)])


def share_url(request: Request, token: str, base_url: str | None = None) -> str:
    base = (base_url or "").strip()
    if not base:
        base = str(request.base_url)
    return f"{base.rstrip('/')}/share/{token}"


@router.post("/course/{course_id}")
def create_share(course_id: str, payload: ShareCreateRequest, request: Request,
                 db: Session = Depends(get_db)):
    """建分享链接：明文 token + 完整链接只在这一次响应里出现。"""
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    link, token = share_service.create_link(
        db, course,
        label=payload.label,
        expires_in_days=payload.expires_in_days,
        password=payload.password,
        permission=payload.permission,
    )
    data = share_service.link_out(link)
    data["token"] = token
    data["url"] = share_url(request, token, payload.base_url)
    data["notice"] = "明文 token 只显示这一次，请立即复制保存；之后列表里只有访问统计。"
    return data


@router.get("/course/{course_id}")
def list_shares(course_id: str, db: Session = Depends(get_db)):
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    return {
        "course_id": course_id,
        "items": [share_service.link_out(link) for link in share_service.list_links(db, course_id)],
    }


@router.post("/{link_id}/revoke")
def revoke_share(link_id: str, db: Session = Depends(get_db)):
    """撤销后公开侧立即 410。"""
    link = share_service.get_link(db, link_id)
    if link is None:
        raise HTTPException(404, "分享链接不存在")
    share_service.revoke_link(db, link)
    return {"ok": True, **share_service.link_out(link)}


@router.delete("/{link_id}")
def delete_share(link_id: str, db: Session = Depends(get_db)):
    link = share_service.get_link(db, link_id)
    if link is None:
        raise HTTPException(404, "分享链接不存在")
    share_service.delete_link(db, link)
    return {"ok": True, "id": link_id}