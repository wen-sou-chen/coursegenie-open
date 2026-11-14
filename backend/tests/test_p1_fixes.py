"""任务 3（P1-1~5）回归测试。每项修复都带独立断言，防旧 bug 复活。"""
from __future__ import annotations

import tempfile
from pathlib import Path

from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import SessionLocal, engine
from app.models.course import (
    ContentItem,
    Course,
    CourseVersion,
    GenerationJob,
    Outline,
    OutlineNode,
    QuizItem,
    SourceMaterial,
)
from app.schemas.plan import PlanRequest
from app.services import planner
from app.services.kb import incremental_write_content


def _bind(client, course_id: str, root: Path) -> None:
    resp = client.post(f"/api/kb/course/{course_id}/bind", json={
        "vault_root": str(root), "output_rel": "课程/P1",
    })
    assert resp.status_code == 200, resp.text


def _plan_approve(client, course_id: str) -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "生物光合作用", "num_chapters": 2,
    })
    assert resp.status_code == 200, resp.text
    approved = client.post("/api/plan/approve", json={
        "course_id": course_id, "approved": True})
    assert approved.status_code == 200, approved.text
    return resp.json()


def test_p1_1_planner_no_double_key_prefix(monkeypatch):
    """知识点绝对 key 不再被重复拼接成 1.1.1，相对序号仍可补前缀。"""

    class _FakeLLM:
        def complete_json(self, system, user, *, max_tokens=2000) -> dict:
            return {
                "title": "植物学",
                "goal": "理解光合作用",
                "nodes": [
                    {
                        "key": "1", "title": "第一章", "node_type": "chapter",
                        "objective": "o", "teaching_strategy": "t", "assessment": "a",
                        "prerequisites": [],
                        "points": [
                            {"key": "1.1", "title": "绝对key知识点", "node_type": "point",
                             "objective": "o", "teaching_strategy": "t", "assessment": "a",
                             "prerequisites": []},
                            {"key": "2", "title": "相对key知识点", "node_type": "point",
                             "objective": "o", "teaching_strategy": "t", "assessment": "a",
                             "prerequisites": []},
                        ],
                    },
                    {
                        "key": "2", "title": "第二章", "node_type": "chapter",
                        "objective": "o", "teaching_strategy": "t", "assessment": "a",
                        "prerequisites": ["1"],
                        "points": [
                            {"key": "2.1", "title": "叶绿体", "node_type": "point",
                             "objective": "o", "teaching_strategy": "t", "assessment": "a",
                             "prerequisites": []},
                        ],
                    },
                ],
            }

    monkeypatch.setattr(planner, "build_llm_client", lambda: _FakeLLM())
    db = SessionLocal()
    try:
        course = Course(title="植物学", subject="生物", audience="初中生")
        db.add(course)
        db.commit()
        db.refresh(course)
        outline = planner.generate_outline(
            db, course, PlanRequest(course_id=course.id, goal="理解光合作用", num_chapters=2))
        assert outline.id
        keys = [n.node_key for n in planner.list_nodes(db, course.id)]
        assert keys == ["1", "1.1", "1.2", "2", "2.1"], keys
        leaves = [k for k in keys if not any(o.startswith(f"{k}.") for o in keys)]
        assert leaves == ["1.1", "1.2", "2.1"], f"叶判定受双前缀影响：{leaves}"
        nodes = planner.list_nodes(db, course.id)
        chapter2 = next(n for n in nodes if n.node_key == "2")
        assert all(p in keys for p in (chapter2.prerequisites or [])), chapter2.prerequisites
    finally:
        db.close()


def test_p1_2_restore_rebinds_node_links(client, course_id):
    """回滚后内容/习题仍挂在原大纲节点上，不再变课程级孤儿。"""
    plan = _plan_approve(client, course_id)
    node_id = plan["nodes"][0]["id"]
    db = SessionLocal()
    try:
        db.add(ContentItem(course_id=course_id, node_id=node_id, kind="text",
                           title="绑定讲解", body={"markdown": "v1"}))
        db.add(QuizItem(course_id=course_id, node_id=node_id, qtype="choice",
                        difficulty="medium", question="光合作用发生在？",
                        options=["细胞质", "叶绿体"], answer="1", explanation="叶绿体是光合作用场所。"))
        db.commit()
    finally:
        db.close()

    snap = client.post(f"/api/version/{course_id}/snapshot")
    assert snap.status_code == 200, snap.text
    version_id = snap.json()["version_id"]
    restored = client.post(f"/api/version/{course_id}/restore/{version_id}")
    assert restored.status_code == 200, restored.text

    rows = client.get(f"/api/content/{course_id}").json()
    texts = [r for r in rows if r["kind"] == "text"]
    quizzes = [r for r in rows if r["kind"] == "quiz"]
    assert len(texts) == 1 and len(quizzes) == 1, (texts, quizzes)
    assert texts[0]["node_id"] is not None, "回滚后正文变成课程级孤儿"
    assert quizzes[0]["node_id"] is not None, "回滚后习题变成课程级孤儿"

    db = SessionLocal()
    try:
        node_ids = {n.id for n in db.query(OutlineNode).all()}
        assert texts[0]["node_id"] in node_ids
        assert quizzes[0]["node_id"] in node_ids
        assert node_id in node_ids, "回滚应按快照原 id 重建大纲节点"
    finally:
        db.close()


def test_p1_3_incremental_write_no_clobber(client, course_id, gen_lecture_row):
    """增量写入与整课导出一致：教师手改过的文件跳过，未手改则正常覆盖。"""
    root = Path(tempfile.mkdtemp(prefix="coursegenie_p13_"))
    _bind(client, course_id, root)
    plan = _plan_approve(client, course_id)
    row = gen_lecture_row(course_id, plan["nodes"][0]["id"])

    items = client.get(f"/api/content/{course_id}").json()
    lecture = next(i for i in items if i["id"] == row["id"])
    vault = (lecture.get("meta") or {}).get("vault") or {}
    note_path = Path(vault["note_path"])
    assert note_path.exists(), vault
    rel = note_path.relative_to(root).as_posix()
    v1_bytes = note_path.read_bytes()

    # 教师在笔记里手动修改 → 内容更新后的增量写必须跳过该文件
    note_path.write_bytes(v1_bytes + "\n<!-- teacher manual edit -->\n".encode("utf-8"))
    db = SessionLocal()
    try:
        item = db.get(ContentItem, row["id"])
        md = str((item.body or {}).get("markdown") or "")
        item.body = {**(item.body or {}), "markdown": md + "\n\n## 新增小节v2"}
        db.commit()
        node_id = item.node_id
    finally:
        db.close()

    db = SessionLocal()
    try:
        res = incremental_write_content(db, course_id, node_id, "lecture")
        db.commit()
    finally:
        db.close()
    assert res["ok"] is True, res
    preserved = res.get("preserved") or []
    assert [(p["file"], p["reason"]) for p in preserved] == [(rel, "manual-edit")], res
    text = note_path.read_text(encoding="utf-8")
    assert "teacher manual edit" in text
    assert "新增小节v2" not in text, "手改文件被静默覆盖（P1-3 未修）"

    # 未手改（文件与上次导出一致）→ 内容更新应正常覆盖
    note_path.write_bytes(v1_bytes)
    db = SessionLocal()
    try:
        res2 = incremental_write_content(db, course_id, node_id, "lecture")
        db.commit()
    finally:
        db.close()
    assert res2["ok"] is True, res2
    assert "新增小节v2" in note_path.read_text(encoding="utf-8"), res2


def test_p1_4_upload_size_limit_413(client, course_id, monkeypatch):
    """material/image/ppt 上传超过上限一律 413，正常小文件不受影响。"""
    monkeypatch.setattr(get_settings(), "max_upload_mb", 1)
    big = b"x" * (2 * 1024 * 1024)
    cases = [
        ("/api/material/upload", ("big.txt", big, "text/plain")),
        ("/api/image/import", ("big.png", big, "image/png")),
        ("/api/ppt/import", ("big.pptx", big, "application/octet-stream")),
    ]
    for url, upload in cases:
        resp = client.post(url, data={"course_id": course_id}, files={"file": upload})
        assert resp.status_code == 413, f"{url}: {resp.status_code} {resp.text}"

    ok = client.post("/api/material/upload", data={"course_id": course_id},
                     files={"file": ("ok.txt", b"ok small", "text/plain")})
    assert ok.status_code == 200, ok.text


def test_p1_5_delete_course_no_orphan_rows_and_fk_on(client, course_id):
    """删课不留孤儿行：素材/任务/内容/习题/大纲/版本全清，素材文件连带删除。"""
    material = client.post("/api/material/upload", data={"course_id": course_id},
                           files={"file": ("material.txt", b"guanghe-material-body", "text/plain")}).json()
    material_file = get_settings().storage_dir / material["file_rel"]
    assert material_file.exists()

    db = SessionLocal()
    try:
        db.add(GenerationJob(course_id=course_id, job_type="ppt", status="success",
                             payload={}, result={}))
        db.commit()
    finally:
        db.close()

    with engine.connect() as conn:
        fk = conn.execute(text("PRAGMA foreign_keys")).scalar()
    assert fk == 1, "SQLite 应开启 PRAGMA foreign_keys=ON（P1-5）"

    title = client.get(f"/api/course/{course_id}").json()["title"]
    resp = client.delete(f"/api/course/{course_id}", params={"confirm": title})
    assert resp.status_code == 200, resp.text
    assert not material_file.exists(), "删课应连带删除素材 storage 文件"

    db = SessionLocal()
    try:
        assert db.get(Course, course_id) is None
        for model in (SourceMaterial, GenerationJob, ContentItem, QuizItem,
                      CourseVersion, Outline):
            left = db.query(model).filter(model.course_id == course_id).count()
            assert left == 0, f"{model.__name__} 残留孤儿行：{left}"
        orphan_nodes = (
            db.query(OutlineNode)
            .join(Outline, OutlineNode.outline_id == Outline.id)
            .filter(Outline.course_id == course_id)
            .count()
        )
        assert orphan_nodes == 0, f"大纲节点残留：{orphan_nodes}"
    finally:
        db.close()
