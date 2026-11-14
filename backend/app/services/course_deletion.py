"""整门课程删除：数据库级联 + storage 产物清理。

文件只按两种可靠关系认领：
1. 数据库里属于这门课的引用（条目 body/meta、素材 file_rel、大纲/版本快照）；
2. 产物路径里的课程 ``id`` 前缀（课件、导出）或交付包 manifest 里的 ``course.id``。

绝不按课程名匹配 —— 两门课重名或前缀相同也不会误删对方的文件。
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.course import (
    ContentItem,
    Course,
    CourseVersion,
    Outline,
    QuizItem,
    SourceMaterial,
)
from app.services.storage_files import collect_image_refs_in, item_file_refs


def _storage_path(settings: Settings, rel: str | None) -> Path | None:
    """storage 相对路径 → 绝对路径；空值 / 越界返回 None。"""
    if not rel or not isinstance(rel, str):
        return None
    base = settings.storage_dir.resolve()
    target = (base / rel.replace("\\", "/").lstrip("/")).resolve()
    if target == base or base not in target.parents:
        return None
    return target


def _add_file(files: dict[str, Path], path: Path | None) -> None:
    if path is None:
        return
    try:
        key = str(path.resolve())
    except OSError:
        key = str(path)
    files[key] = path


def _add_refs(files: dict[str, Path], obj) -> None:
    for path in collect_image_refs_in(obj):
        _add_file(files, path)


def _course_files(db: Session, course: Course, settings: Settings) -> dict[str, Path]:
    files: dict[str, Path] = {}

    for item in db.query(ContentItem).filter(ContentItem.course_id == course.id).all():
        for path in item_file_refs(item):
            _add_file(files, path)
        _add_refs(files, item.body)
        _add_refs(files, item.meta)

    for quiz in db.query(QuizItem).filter(QuizItem.course_id == course.id).all():
        _add_refs(files, {
            "question": quiz.question,
            "options": quiz.options,
            "answer": quiz.answer,
            "explanation": quiz.explanation,
            "meta": quiz.meta,
        })

    for material in db.query(SourceMaterial).filter(SourceMaterial.course_id == course.id).all():
        _add_file(files, _storage_path(settings, material.file_rel))
        _add_refs(files, {"text": material.text, "meta": material.meta})

    _add_refs(files, course.meta)

    for version in db.query(CourseVersion).filter(CourseVersion.course_id == course.id).all():
        _add_refs(files, version.snapshot)

    for outline in db.query(Outline).filter(Outline.course_id == course.id).all():
        _add_refs(files, outline.content)
        for node in outline.nodes:
            _add_refs(files, {"content_md": node.content_md})

    return files


def _delivery_artifacts(course_id: str, settings: Settings) -> tuple[list[Path], list[Path]]:
    """交付包目录 / zip：只认 manifest 里的 course.id，不按目录名猜。"""
    root = (settings.storage_dir / "deliveries").resolve()
    files: list[Path] = []
    dirs: list[Path] = []
    if not root.is_dir():
        return files, dirs
    for manifest_path in root.glob("*/manifest.json"):
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str((data.get("course") or {}).get("id") or "") != course_id:
            continue
        build_dir = manifest_path.parent
        dirs.append(build_dir)
        zip_path = root / f"{build_dir.name}.zip"
        if zip_path.is_file():
            files.append(zip_path)
    return files, dirs


def collect_course_artifacts(
    db: Session, course: Course, settings: Settings | None = None,
) -> tuple[list[Path], list[Path]]:
    """返回（待删文件、待删目录）；调用前先取，数据库行删掉后就查不到引用了。"""
    settings = settings or get_settings()
    files = _course_files(db, course, settings)

    dirs: dict[str, Path] = {}
    for root, pattern in (
        (settings.ppt_work_dir, f"{course.id}_*"),
        (settings.ppt_work_dir / "uploads", f"{course.id}_*"),
        (settings.export_dir, f"{course.id}*"),
    ):
        if not root.is_dir():
            continue
        for path in root.glob(pattern):
            if path.is_dir():
                dirs[str(path.resolve())] = path
            elif path.is_file():
                _add_file(files, path)

    delivery_files, delivery_dirs = _delivery_artifacts(course.id, settings)
    for path in delivery_files:
        _add_file(files, path)
    for path in delivery_dirs:
        dirs[str(path.resolve())] = path

    return list(files.values()), list(dirs.values())


def _remaining_references(db: Session, settings: Settings) -> set[str]:
    """删除后仍被任何课程引用的文件；目标课程删掉后这里就是「不能碰」的名单。"""
    files: dict[str, Path] = {}

    for item in db.query(ContentItem).all():
        for path in item_file_refs(item):
            _add_file(files, path)
        _add_refs(files, item.body)
        _add_refs(files, item.meta)
    for quiz in db.query(QuizItem).all():
        _add_refs(files, {
            "question": quiz.question,
            "options": quiz.options,
            "answer": quiz.answer,
            "explanation": quiz.explanation,
            "meta": quiz.meta,
        })
    for material in db.query(SourceMaterial).all():
        _add_file(files, _storage_path(settings, material.file_rel))
        _add_refs(files, {"text": material.text, "meta": material.meta})
    for course in db.query(Course).all():
        _add_refs(files, course.meta)
    for version in db.query(CourseVersion).all():
        _add_refs(files, version.snapshot)
    for outline in db.query(Outline).all():
        _add_refs(files, outline.content)
        for node in outline.nodes:
            _add_refs(files, {"content_md": node.content_md})
    return set(files)


def _is_protected_dir(path: Path, protected: set[str]) -> bool:
    prefix = str(path.resolve()) + os.sep
    return any(ref.startswith(prefix) for ref in protected)


def delete_course_with_artifacts(
    db: Session, course: Course, settings: Settings | None = None,
) -> dict:
    """删 DB（ORM 级联）→ 删 storage 产物；返回前端提示与复验所需的计数。"""
    settings = settings or get_settings()
    counts = {
        "outlines": db.query(Outline).filter(Outline.course_id == course.id).count(),
        "content_items": db.query(ContentItem).filter(ContentItem.course_id == course.id).count(),
        "quizzes": db.query(QuizItem).filter(QuizItem.course_id == course.id).count(),
        "materials": db.query(SourceMaterial).filter(SourceMaterial.course_id == course.id).count(),
    }
    files, dirs = collect_course_artifacts(db, course, settings)

    db.delete(course)
    db.commit()

    protected = _remaining_references(db, settings)
    removed_files = 0
    for path in files:
        try:
            resolved = str(path.resolve())
        except OSError:
            resolved = str(path)
        if resolved in protected:
            continue
        try:
            if path.exists():
                path.unlink()
                removed_files += 1
        except OSError:
            continue

    removed_dirs = 0
    for path in dirs:
        if _is_protected_dir(path, protected):
            continue
        try:
            if not path.exists():
                continue
            removed_files += sum(1 for child in path.rglob("*") if child.is_file())
            shutil.rmtree(path)
            removed_dirs += 1
        except OSError:
            continue

    return {
        "ok": True,
        "removed_files": removed_files,
        "removed_dirs": removed_dirs,
        "counts": counts,
    }
