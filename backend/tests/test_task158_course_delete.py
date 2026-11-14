"""任务 158：课程删除的确认保护、级联清理与「不误删其他课程」。"""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.course import (
    ContentItem,
    Course,
    CourseVersion,
    Outline,
    OutlineNode,
    QuizItem,
    SourceMaterial,
)


def _new_course(client, title: str) -> dict:
    resp = client.post("/api/course/", json={
        "title": title,
        "subject": "生物",
        "audience": "中学生",
        "duration_minutes": 60,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed_rows(course_id: str, *, image_names: list[str]) -> dict:
    """一门课各建一条大纲/章节/内容/习题/版本/素材，内容引用给定图片。"""
    settings = get_settings()
    image_refs = [f"images/{name}" for name in image_names]
    body = {
        "markdown": "".join(f"![图](/{rel})\n" for rel in image_refs) + "正文",
        **({"path": image_refs[0]} if image_refs else {}),
    }
    material_rel = f"materials/{uuid.uuid4().hex}.txt"
    _write(settings.storage_dir / material_rel, b"course material")

    db = SessionLocal()
    try:
        outline = Outline(course_id=course_id, title="测试大纲", goal="测试目标")
        db.add(outline)
        db.flush()
        node = OutlineNode(outline_id=outline.id, node_key="1", node_type="chapter",
                           title="第一章", content_md="章节正文")
        db.add(node)
        db.flush()
        item = ContentItem(course_id=course_id, node_id=node.id, kind="lecture",
                           title="讲义", body=body, meta={})
        quiz = QuizItem(course_id=course_id, node_id=node.id, question="题目",
                        answer="答案", explanation="解析")
        version = CourseVersion(course_id=course_id, version_no=1,
                                snapshot={"item": body}, note="快照")
        material = SourceMaterial(course_id=course_id, title="素材", kind="file",
                                  file_rel=material_rel)
        db.add_all([item, quiz, version, material])
        db.commit()
        return {
            "outline": outline.id,
            "node": node.id,
            "item": item.id,
            "quiz": quiz.id,
            "version": version.id,
            "material": material.id,
            "material_file": settings.storage_dir / material_rel,
        }
    finally:
        db.close()


def _counts(course_id: str) -> dict[str, int]:
    db = SessionLocal()
    try:
        return {
            "outlines": db.query(Outline).filter(Outline.course_id == course_id).count(),
            "outline_nodes": (
                db.query(OutlineNode)
                .join(Outline, OutlineNode.outline_id == Outline.id)
                .filter(Outline.course_id == course_id)
                .count()
            ),
            "content_items": db.query(ContentItem).filter(ContentItem.course_id == course_id).count(),
            "quizzes": db.query(QuizItem).filter(QuizItem.course_id == course_id).count(),
            "versions": db.query(CourseVersion).filter(CourseVersion.course_id == course_id).count(),
            "materials": db.query(SourceMaterial).filter(SourceMaterial.course_id == course_id).count(),
        }
    finally:
        db.close()


def test_delete_course_requires_exact_title_confirm(client):
    course = _new_course(client, "确认保护测试课")
    cid = course["id"]

    missing = client.delete(f"/api/course/{cid}")
    assert missing.status_code == 400, missing.text
    assert "完整课程名" in missing.json()["detail"]

    wrong = client.delete(f"/api/course/{cid}", params={"confirm": "确认保护测试"})
    assert wrong.status_code == 400, wrong.text
    assert client.get(f"/api/course/{cid}").status_code == 200

    ok = client.delete(f"/api/course/{cid}", params={"confirm": course["title"]})
    assert ok.status_code == 200, ok.text
    assert client.get(f"/api/course/{cid}").status_code == 404


def test_delete_missing_course_returns_404(client):
    resp = client.delete("/api/course/not-a-real-course", params={"confirm": "随便"})
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "课程不存在"


def test_delete_course_cleans_artifacts_without_touching_other_course(client):
    settings = get_settings()
    target = _new_course(client, "受众与删除测试课")
    other = _new_course(client, "受众与删除测试课-兄弟课")

    target_img = f"{uuid.uuid4().hex}.png"
    shared_img = f"{uuid.uuid4().hex}.png"
    _write(settings.storage_dir / "images" / target_img, b"target image")
    _write(settings.storage_dir / "images" / shared_img, b"shared image")

    target_rows = _seed_rows(target["id"], image_names=[target_img, shared_img])
    other_rows = _seed_rows(other["id"], image_names=[shared_img])

    target_ppt = settings.ppt_work_dir / f"{target['id']}_course"
    _write(target_ppt / "deck.pptx", b"target deck")
    other_ppt = settings.ppt_work_dir / f"{other['title']}_course"
    _write(other_ppt / "deck.pptx", b"other deck")

    target_export = _write(settings.export_dir / f"{target['id']}_html.zip", b"target export")
    other_export = _write(settings.export_dir / f"{other['title']}_notes_all.zip", b"other export")

    deliveries = settings.storage_dir / "deliveries"
    target_build = deliveries / "build-target"
    _write(target_build / "manifest.json", json.dumps(
        {"course": {"id": target["id"]}}, ensure_ascii=False).encode("utf-8"))
    target_zip = _write(deliveries / "build-target.zip", b"target delivery")
    other_build = deliveries / "build-other"
    _write(other_build / "manifest.json", json.dumps(
        {"course": {"id": other["id"]}}, ensure_ascii=False).encode("utf-8"))
    other_zip = _write(deliveries / "build-other.zip", b"other delivery")

    other_counts = _counts(other["id"])
    other_hashes = {p: _sha(p) for p in (other_ppt / "deck.pptx", other_export,
                                         other_build / "manifest.json", other_zip,
                                         other_rows["material_file"])}

    resp = client.delete(f"/api/course/{target['id']}",
                         params={"confirm": target["title"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["counts"] == {
        "outlines": 1, "content_items": 1, "quizzes": 1, "materials": 1,
    }
    assert _counts(target["id"]) == {
        "outlines": 0, "outline_nodes": 0, "content_items": 0,
        "quizzes": 0, "versions": 0, "materials": 0,
    }

    assert client.get(f"/api/course/{other['id']}").status_code == 200
    assert _counts(other["id"]) == other_counts

    assert (settings.storage_dir / "images" / shared_img).exists(), "另一门课仍引用的图不能删"
    for path, digest in other_hashes.items():
        assert path.exists(), f"另一门课文件被误删：{path}"
        assert _sha(path) == digest, f"另一门课文件被改动：{path}"

    assert body["removed_files"] >= 5, body
    assert body["removed_dirs"] >= 2, body
    for path in (target_ppt, target_export, target_build, target_zip,
                 settings.storage_dir / "images" / target_img,
                 target_rows["material_file"]):
        assert not path.exists(), f"目标课程产物应被清理：{path}"
