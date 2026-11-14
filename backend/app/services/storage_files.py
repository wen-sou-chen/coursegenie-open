"""storage 文件生命周期管理：条目删除连带删文件、孤儿图片 GC。

范围限定在 backend/storage/（应用自身运行时目录，非用户 Obsidian Vault）。
判定引用依据：ContentItem / QuizItem 的 body、meta（含 meta.history 旧版本）
与正文 Markdown 里出现的 /images/... 相对引用；SourceMaterial 的 file_rel。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.course import ContentItem, QuizItem, SourceMaterial

# "images/xxx.svg"（body.path / svg_preview）与 Markdown 里 "/images/xxx.png" 两种形态
_REL_IMAGE_RE = re.compile(r"(?:^|[/\\])(images/[A-Za-z0-9._-]+)")
_BODY_PATH_KEYS = ("path", "svg_preview", "file_rel")


def _storage_root() -> Path:
    return get_settings().storage_dir.resolve()


def _safe_storage_path(rel: str | None) -> Path | None:
    """把 storage 相对路径换算为安全绝对路径；越界/空值返回 None。"""
    if not rel or not isinstance(rel, str):
        return None
    rel = rel.replace("\\", "/").lstrip("/")
    base = _storage_root()
    target = (base / rel).resolve()
    if target == base or base not in target.parents:
        return None
    return target


def item_file_refs(item) -> list[Path]:
    """条目自身关联的 storage 文件（body/meta + 正文图片引用，去重、限定 storage 内）。"""
    refs: dict[str, Path] = {}

    def _add(rel: str) -> None:
        p = _safe_storage_path(rel)
        if p is not None:
            refs[p.as_posix()] = p

    def _walk(obj) -> None:
        if isinstance(obj, str):
            for m in _REL_IMAGE_RE.finditer(obj):
                _add(m.group(1))
        elif isinstance(obj, dict):
            for key, value in obj.items():
                if key in _BODY_PATH_KEYS and isinstance(value, str):
                    _add(value)
                else:
                    _walk(value)
        elif isinstance(obj, list):
            for value in obj:
                _walk(value)

    _walk(item.body)
    _walk(item.meta)
    return list(refs.values())


def remove_files(paths) -> int:
    """删除文件（不存在忽略），返回实际删除数。"""
    count = 0
    for p in paths:
        try:
            if p.exists():
                p.unlink()
                count += 1
        except OSError:
            continue
    return count


def schedule_removal(db: Session, paths) -> None:
    """登记待删除文件：等外层事务 commit 后再真正 unlink，避免回滚后行在文件无。"""
    if not paths:
        return
    pending = getattr(db, "_cg_pending_file_removals", None)
    if pending is None:
        pending = set()
        db._cg_pending_file_removals = pending  # type: ignore[attr-defined]
    pending.update(str(p) for p in paths)


def flush_scheduled_removals(db: Session) -> int:
    """在事务 commit 之后调用：真正删除登记的文件并清空登记表。"""
    pending = getattr(db, "_cg_pending_file_removals", None)
    if not pending:
        return 0
    removed = remove_files(Path(p) for p in pending)
    pending.clear()
    return removed


def delete_rows_and_files(db: Session, rows) -> int:
    """删除 DB 行并登记其文件清理（外层 commit 后请调用 flush_scheduled_removals）。"""
    paths = []
    for row in rows:
        paths.extend(item_file_refs(row))
    for row in rows:
        db.delete(row)
    schedule_removal(db, paths)
    return len(paths)


def delete_matching_items(db: Session, clause) -> int:
    """按过滤条件删除 ContentItem，并登记其 storage 文件清理。"""
    rows = db.query(ContentItem).filter(clause).all()
    removed = delete_rows_and_files(db, rows)
    db.flush()
    return removed


def collect_referenced_image_names(db: Session) -> set[str]:
    """扫描 DB 中被引用的 storage/images 文件名（含 meta.history 里的旧路径）。"""
    names: set[str] = set()

    def _collect(obj) -> None:
        if isinstance(obj, str):
            for m in _REL_IMAGE_RE.finditer(obj):
                rel = m.group(1)
                if rel.startswith("images/"):
                    names.add(Path(rel).name)
        elif isinstance(obj, dict):
            for value in obj.values():
                _collect(value)
        elif isinstance(obj, list):
            for value in obj:
                _collect(value)

    for item in db.query(ContentItem).all():
        _collect(item.body)
        _collect(item.meta)
    for quiz in db.query(QuizItem).all():
        _collect(quiz.question)
        _collect(quiz.explanation)
    for material in db.query(SourceMaterial).all():
        if material.file_rel:
            _collect(material.file_rel)
        _collect(material.text)
        _collect(material.meta)
    return names


def gc_images(db: Session, *, dry_run: bool = False) -> dict:
    """清理 storage/images 下不再被 DB 引用的文件；dry_run=True 只统计。"""
    images_dir = _storage_root() / "images"
    if not images_dir.exists():
        return {"scanned": 0, "removed": 0, "kept": 0, "errors": 0, "dry_run": dry_run}
    referenced = collect_referenced_image_names(db)
    files = sorted(p for p in images_dir.iterdir() if p.is_file())
    removed = 0
    kept = 0
    errors = 0
    for p in files:
        if p.name in referenced:
            kept += 1
            continue
        if not dry_run:
            try:
                p.unlink()
                removed += 1
            except OSError:
                errors += 1
        else:
            removed += 1
    return {
        "scanned": len(files),
        "removed": removed,
        "kept": kept,
        "errors": errors,
        "dry_run": dry_run,
    }


def remove_course_files(db: Session, course_id: str) -> int:
    """删除课程所有内容条目引用的文件（删除整门课前调用）。"""
    rows = db.query(ContentItem).filter(ContentItem.course_id == course_id).all()
    files: list[Path] = []
    for row in rows:
        files.extend(item_file_refs(row))
    return remove_files(files)


def summarize_json_refs(obj) -> list[str]:
    """供调试/审计：JSON 里形如 images/xxx 的相对引用。"""
    return sorted({m.group(1) for m in _REL_IMAGE_RE.finditer(json.dumps(obj, ensure_ascii=False))})


def collect_image_refs_in(obj) -> set[Path]:
    """在任意 JSON 结构（body/meta/快照）中收集 images/ 文件引用对应的绝对路径。"""
    out: set[Path] = set()

    def _walk(value) -> None:
        if isinstance(value, str):
            for m in _REL_IMAGE_RE.finditer(value):
                p = _safe_storage_path(m.group(1))
                if p is not None:
                    out.add(p)
        elif isinstance(value, dict):
            for v in value.values():
                _walk(v)
        elif isinstance(value, list):
            for v in value:
                _walk(v)

    _walk(obj)
    return out
