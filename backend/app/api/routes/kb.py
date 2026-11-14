"""笔记系统导出路由：绑定目标目录、一键自动生成 Obsidian 笔记系统。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.db.session import get_db
from app.models.course import Course, OutlineNode
from app.schemas.kb import KbBindRequest, KbVerifyRequest
from app.services import kb as kb_service
from app.services import style_library

router = APIRouter(prefix="/kb", tags=["kb"], dependencies=[Depends(require_api_key)])


def _course_or_404(db: Session, course_id: str) -> Course:
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    return course


def _state(course: Course | None = None) -> dict:
    root = kb_service.export_root_for(course)
    return {"configured": bool(root), "vault_root": root or None,
            "exists": bool(root and Path(root).is_dir())}


@router.get("/vault")
def vault_info():
    return _state()


@router.get("/course/{course_id}")
def course_binding(course_id: str, db: Session = Depends(get_db)):
    course = _course_or_404(db, course_id)
    return {"course_id": course.id, "vault": _state(course), "kb": kb_service.course_kb(course)}


@router.post("/course/{course_id}/bind")
def bind(course_id: str, payload: KbBindRequest, db: Session = Depends(get_db)):
    course = _course_or_404(db, course_id)
    kb_service.bind_course(course, payload.vault_root, payload.output_rel)
    db.commit()
    db.refresh(course)
    return {"ok": True, "kb": kb_service.course_kb(course)}


@router.post("/course/{course_id}/verify")
def verify(course_id: str, payload: KbVerifyRequest | None = None,
           db: Session = Depends(get_db)):
    """一致性校验：逐知识点比较「网页端讲义」与「笔记 md」，列出差异。"""
    course = _course_or_404(db, course_id)
    return kb_service.verify_course_notes(db, course, (payload.node_ids if payload else None) or None)


@router.post("/course/{course_id}/repair")
def repair(course_id: str, payload: KbVerifyRequest | None = None,
           db: Session = Depends(get_db)):
    """以网页端为准，强制覆盖指定（或全部不一致）知识点的笔记文件。"""
    course = _course_or_404(db, course_id)
    node_ids = (payload.node_ids if payload else None) or None
    report = kb_service.verify_course_notes(db, course, node_ids)
    if not report.get("ok"):
        raise HTTPException(400, str(report.get("reason")))
    targets = [r for r in report["nodes"] if r["status"] != "consistent"]
    repaired, failed = [], []
    for row in targets:
        node = db.get(OutlineNode, row["node_id"]) if row.get("node_id") else None
        if node is None:
            continue  # 课程级/概览/素材页在下面统一修
        try:
            res = kb_service.force_write_node(db, course, node)
            repaired.append({"node_key": row["node_key"], "part": row.get("part"),
                             "files": res["files"]})
        except Exception as exc:  # noqa: BLE001 —— 单个节点失败不影响其它
            db.rollback()
            failed.append({"node_key": row["node_key"], "error": str(exc)})
    if any(not r.get("node_id") for r in targets):
        try:
            res = kb_service.force_write_course_files(db, course)
            repaired.append({"node_key": "00", "part": "course",
                             "files": res["files"]})
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            failed.append({"node_key": "00", "error": str(exc)})
    return {"ok": True, "repaired": repaired, "failed": failed,
            "before": {"checked": report["checked"], "inconsistent": report["inconsistent"]},
            "after": kb_service.verify_course_notes(db, course, node_ids)}


@router.post("/course/{course_id}/export")
def export(course_id: str, force: bool = False, prune: bool = True,
           db: Session = Depends(get_db)):
    course = _course_or_404(db, course_id)
    try:
        return kb_service.export_course(db, course, force=force, prune=prune)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/course/{course_id}/style-library")
def write_style_library(course_id: str, db: Session = Depends(get_db)):
    """生成 .excalidrawlib 风格库（角色/卡片/连线/色卡四组）并写入 图表库/。"""
    course = _course_or_404(db, course_id)
    try:
        return style_library.write_style_library(db, course)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/course/{course_id}/style-library")
def preview_style_library(course_id: str, db: Session = Depends(get_db)):
    """预览风格库（不写盘）：条目数 / 元素数 / 分组 / 主题 + 完整库 JSON。"""
    course = _course_or_404(db, course_id)
    library = style_library.build_library(course=course)
    return {
        "course_id": course_id,
        "file": style_library.STYLE_LIB_REL,
        "stats": style_library.library_stats(library),
        "library": library,
    }
