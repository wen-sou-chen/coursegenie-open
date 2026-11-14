"""分享链接服务（M5a）：token / 密码哈希、访问留痕、公开侧只读序列化。

安全约定：
- 库里只存 token 的 sha256 哈希，明文只在创建响应里返回一次；
- 密码只存 pbkdf2 的 salt$digest（复用 app.models.user.hash_password）；
- 公开侧只读：只暴露课程内容，不暴露 vault / 素材原文 / 下载直链。
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.base import utcnow
from app.models.course import ContentItem, Course, Outline, OutlineNode
from app.models.share import PERMISSION_VIEW, ShareAccessLog, ShareLink
from app.models.user import hash_password
from app.services import audience
from app.services.concept import CONCEPT_KIND
from app.services.content import build_content_rows
from app.services.planner import list_nodes
from app.services.ppt import SCOPE_COURSE, list_decks, outline_slides
from app.services.timefmt import as_aware, as_utc_iso

TOKEN_BYTES = 24
INVALID_TOKEN_CODE = 403
GONE_CODE = 410
UNAUTHORIZED_CODE = 401


class ShareAccessError(Exception):
    """公开侧访问失败（路由层转成 HTTPException，这里不依赖 fastapi）。"""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# ---------------------------------------------------------------------------
# token / 密码
# ---------------------------------------------------------------------------

def generate_token() -> str:
    """明文 token：只在创建响应里出现一次，之后库里只有哈希。"""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_secret(password: str) -> str:
    digest, salt = hash_password(password)
    return f"{salt}${digest}"


def verify_secret(password: str, stored: str) -> bool:
    if not stored or "$" not in stored:
        return False
    salt, digest = stored.split("$", 1)
    candidate, _ = hash_password(password, salt)
    return secrets.compare_digest(candidate, digest)


# ---------------------------------------------------------------------------
# 管理侧（沿用 require_api_key）
# ---------------------------------------------------------------------------

def create_link(db: Session, course: Course, *, label: str = "",
                expires_in_days: int | None = None, password: str = "",
                permission: str = PERMISSION_VIEW) -> tuple[ShareLink, str]:
    """建链接并返回 (记录, 明文 token)；明文 token 之后再也拿不到。"""
    token = generate_token()
    expires_at = utcnow() + timedelta(days=int(expires_in_days)) if expires_in_days else None
    link = ShareLink(
        course_id=course.id,
        token_hash=hash_token(token),
        label=label or "",
        permission=permission or PERMISSION_VIEW,
        expires_at=expires_at,
        password_hash=hash_secret(password) if password else "",
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    return link, token


def list_links(db: Session, course_id: str) -> list[ShareLink]:
    return list(
        db.query(ShareLink)
        .filter(ShareLink.course_id == course_id)
        .order_by(ShareLink.created_at.desc())
        .all()
    )


def get_link(db: Session, link_id: str) -> ShareLink | None:
    return db.get(ShareLink, link_id)


def revoke_link(db: Session, link: ShareLink) -> ShareLink:
    if link.revoked_at is None:
        link.revoked_at = utcnow()
        db.commit()
        db.refresh(link)
    return link


def delete_link(db: Session, link: ShareLink) -> None:
    db.delete(link)
    db.commit()


def link_status(link: ShareLink) -> str:
    if link.revoked_at is not None:
        return "revoked"
    expires_at = as_aware(link.expires_at)
    if expires_at is not None and expires_at <= utcnow():
        return "expired"
    return "active"


def link_out(link: ShareLink, url: str | None = None) -> dict:
    """序列化：永远不含明文 token，也不含密码哈希。

    任务 83 §B1：时间字段一律走 ``()`` —— 库里是 naive（SQLite 不存时区），
    直接吐给前端会被 ``new Date()`` 当本地时间解释（界面上比本地时间少 8 小时）。
    """
    data = {
        "id": link.id,
        "course_id": link.course_id,
        "label": link.label,
        "permission": link.permission,
        "expires_at": as_utc_iso(link.expires_at),
        "revoked_at": as_utc_iso(link.revoked_at),
        "has_password": bool(link.password_hash),
        "created_at": as_utc_iso(link.created_at),
        "access_count": int(link.access_count or 0),
        "last_accessed_at": as_utc_iso(link.last_accessed_at),
        "status": link_status(link),
    }
    if url:
        data["url"] = url
    return data


# ---------------------------------------------------------------------------
# 公开侧（免登录，仅凭 token）
# ---------------------------------------------------------------------------

#: 公开侧 ``meta`` 白名单（**唯一出处**，任务 89）。
#:
#: 取向是**白名单**而不是黑名单：`meta` 以后长出新键（新的内部统计 / 指纹 / 诊断）
#: 时默认**不外泄**，只有明确列在这里的内容属性才发得出去。
#: 两类：内容属性（`/content` 各行的 `meta`）+ 课件版面属性（`public_deck` 读的键）。
PUBLIC_META_KEYS: tuple[str, ...] = (
    # 内容属性
    "node_key", "sources", "caption", "role", "from_lecture", "generated_at",
    "minutes", "layout", "chart_type", "concept_id", "aliases", "chapters",
    "prompt_version",
    # 课件版面属性
    "scope", "chapter_node_id", "chapter_key", "engine", "slide_count",
)


def public_meta(meta) -> dict:
    """``meta`` → 公开侧可发的那一份（纯函数，无副作用）。

    刻意剔除的内部字段：成本与用量 ``usage``（含 ``cost_cny`` / ``*_tokens`` /
    模型名）、``input_fingerprint`` / ``input_fingerprint_at``、``history``（旧版本
    正文，里面**带答案**）、``locked`` / ``locked_reason``、``quiz_ids`` /
    ``diagram_ids`` / ``chart_ids`` / ``image_ids``、``structured_block`` /
    ``content_issues`` / ``diagram_failures`` / ``style_anchor_ref`` / ``quiz_issues``、
    ``qc``（内部质检明细）、``llm_used`` / ``quizzes`` / ``student_version`` 等。

    ⚠️ 不是 dict（``None`` / 异常值）一律回 ``{}``：宁可少发，不可原样透传。
    """
    if not isinstance(meta, dict):
        return {}
    return {key: meta[key] for key in PUBLIC_META_KEYS if key in meta}


def resolve_token(db: Session, token: str, password: str | None = None) -> ShareLink:
    """校验 token + 密码；无效 / 撤销 / 过期 / 密码错一律显式抛错。"""
    if not token:
        raise ShareAccessError(INVALID_TOKEN_CODE, "分享链接无效")
    link = db.query(ShareLink).filter(ShareLink.token_hash == hash_token(token)).first()
    if link is None:
        raise ShareAccessError(INVALID_TOKEN_CODE, "分享链接无效或已被删除")
    if link.revoked_at is not None:
        raise ShareAccessError(GONE_CODE, "分享链接已被撤销")
    expires_at = as_aware(link.expires_at)
    if expires_at is not None and expires_at <= utcnow():
        raise ShareAccessError(GONE_CODE, "分享链接已过期")
    if link.password_hash:
        if not password:
            raise ShareAccessError(UNAUTHORIZED_CODE, "该分享链接需要密码")
        if not verify_secret(password, link.password_hash):
            raise ShareAccessError(UNAUTHORIZED_CODE, "分享密码错误")
    return link


def record_access(db: Session, link: ShareLink, *, path: str,
                  ip: str = "", user_agent: str = "") -> None:
    """公开侧每次成功访问：写一条日志 + access_count 累加 + 刷新最近访问时间。"""
    db.add(ShareAccessLog(link_id=link.id, path=(path or "")[:500],
                          ip=(ip or "")[:64], user_agent=(user_agent or "")[:500]))
    link.access_count = int(link.access_count or 0) + 1
    link.last_accessed_at = utcnow()
    db.commit()


def public_overview(db: Session, course: Course, link: ShareLink) -> dict:
    chapters = (
        db.query(OutlineNode)
        .join(Outline, OutlineNode.outline_id == Outline.id)
        .filter(Outline.course_id == course.id, OutlineNode.node_type == "chapter")
        .count()
    )
    nodes = list_nodes(db, course.id)
    concept_count = (
        db.query(ContentItem)
        .filter(ContentItem.course_id == course.id, ContentItem.kind == CONCEPT_KIND)
        .count()
    )
    return {
        "course": {
            "id": course.id,
            "title": course.title,
            "subject": course.subject,
            "audience": course.audience,
            "duration_minutes": course.duration_minutes,
            "status": course.status,
        },
        "chapter_count": chapters,
        "node_count": len(nodes),
        "point_count": len([n for n in nodes if "." in n.node_key]),
        "concept_count": concept_count,
        "share": {
            "label": link.label,
            "permission": link.permission,
            "expires_at": as_utc_iso(link.expires_at),
            "has_password": bool(link.password_hash),
            "access_count": int(link.access_count or 0),
        },
    }


def public_plan(db: Session, course: Course) -> dict:
    nodes = list_nodes(db, course.id)
    key_to_id = {n.node_key: n.id for n in nodes}
    edges = []
    for node in nodes:
        for pre in node.prerequisites or []:
            target_id = key_to_id.get(pre)
            if target_id:
                edges.append({"source": target_id, "target": node.id, "label": "前置"})
    return {
        "outline_id": nodes[0].outline_id if nodes else None,
        "title": course.title,
        "goal": "",
        "nodes": [
            {
                "id": n.id,
                "key": n.node_key,
                "type": n.node_type,
                "title": n.title,
                "objective": n.objective,
                "teaching_strategy": n.teaching_strategy,
                "assessment": n.assessment,
                "prerequisites": n.prerequisites,
            }
            for n in nodes
        ],
        "graph": {
            "nodes": [
                {"id": n.id, "key": n.node_key, "label": n.title, "type": n.node_type}
                for n in nodes
            ],
            "edges": edges,
        },
    }


def public_row(row: dict) -> dict:
    """公开侧内容行：`meta` 过白名单（`public_meta` 是唯一出处），其余字段原样。"""
    return {**row, "meta": public_meta(row.get("meta"))}


def public_content(db: Session, course: Course, *,
                   audience_view: str = audience.AUDIENCE_TEACHER) -> list[dict]:
    """与内部 /content 同构，但不带 vault（笔记）与 `meta` 里的任何内部字段。

    任务 89：每行的 `meta` 走 :func:`public_meta` **白名单投影**（成本/用量、指纹、
    历史版本、锁、内部 ID、结构化块、质检明细一律不出门）。
    任务 86：``audience_view="student"`` ⇒ **学员版**（讲义去答案与解析、习题行清空答案
    与解析字段）；默认 ``teacher`` 一字不改。**只做确定性裁剪与文本处理，不调模型。**
    """
    rows = build_content_rows(db, course.id, include_vault=False)
    rows = [public_row(row) for row in rows]
    if audience.is_student(audience_view):
        rows = audience.student_rows(rows)
    return rows


def public_ppt(db: Session, course: Course, token: str) -> dict:
    """页级大纲 + 缩略图 URL；刻意不给 pptx / pdf 下载直链。"""
    decks = [public_deck(item, token) for item in list_decks(db, course.id)]
    course_decks = [d for d in decks if d["scope"] == SCOPE_COURSE]
    chapter_decks = [d for d in decks if d["scope"] != SCOPE_COURSE]
    chapter_decks.sort(key=lambda d: (str(d["chapter_key"] or ""), str(d["generated_at"] or "")))
    return {"items": decks, "course": course_decks, "chapters": chapter_decks}


def public_deck(item: ContentItem, token: str) -> dict:
    """课件公开视图：`meta` 同样过白名单（任务 89 §A3）。"""
    meta = public_meta(item.meta)
    slides: list[dict] = []
    for slide in outline_slides(item):
        entry = {k: v for k, v in slide.items() if k != "image"}
        url = public_ppt_file_url(token, slide.get("image"))
        if url:
            entry["image_url"] = url
        slides.append(entry)
    return {
        "id": item.id,
        "title": item.title,
        "scope": str(meta.get("scope") or SCOPE_COURSE),
        "chapter_node_id": meta.get("chapter_node_id"),
        "chapter_key": meta.get("chapter_key"),
        "engine": meta.get("engine") or (item.body or {}).get("engine"),
        "slide_count": meta.get("slide_count") or len(slides),
        "generated_at": meta.get("generated_at"),
        "page_count": len(slides),
        "has_images": any(bool(s.get("image_url")) for s in slides),
        "slides": slides,
    }


def public_ppt_file_url(token: str, raw) -> str | None:
    """meta.slides[].image（相对 ppt_work_dir 或绝对路径）→ 公开侧代理 URL。"""
    if not raw:
        return None
    try:
        base = get_settings().ppt_work_dir.resolve()
        path = Path(str(raw))
        cand = (path if path.is_absolute() else base / path).resolve()
        if not cand.is_file():
            return None
        rel = cand.relative_to(base).as_posix()
    except (ValueError, OSError):
        return None
    return f"{get_settings().api_prefix}/public/{token}/ppt-files/{rel}"
