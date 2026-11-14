# -*- coding: utf-8 -*-
"""P0-3 文件生命周期：删条目连带删文件、孤儿文件 GC、删除课程/回滚清理。"""
from __future__ import annotations

import time
from pathlib import Path

from app.core.config import get_settings
def _plan_approve(client, course_id: str) -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "初中生物光合作用", "num_chapters": 2,
    })
    assert resp.status_code == 200, resp.text
    assert client.post("/api/plan/approve", json={
        "course_id": course_id, "approved": True}).status_code == 200
    return resp.json()


def _leaf(plan: dict) -> dict:
    return next((n for n in plan["nodes"] if "." in n["key"]), None) or plan["nodes"][0]


def _images_dir() -> Path:
    return get_settings().storage_dir / "images"


def _write_orphan(name: str) -> Path:
    images_dir = _images_dir()
    images_dir.mkdir(parents=True, exist_ok=True)
    p = images_dir / name
    p.write_bytes(b"orphan-test")
    return p


def _wait_job(client, job_id: str, tries: int = 120) -> dict:
    for _ in range(tries):
        state = client.get(f"/api/content/jobs/{job_id}").json()
        if state["status"] in ("success", "failed"):
            return state
        time.sleep(0.1)
    raise AssertionError(f"job timeout: {job_id}")


def test_gc_removes_orphan_images_keeps_referenced(client, course_id, gen_lecture_row):
    plan = _plan_approve(client, course_id)
    node = _leaf(plan)
    row = gen_lecture_row(course_id, node["id"])

    rows = client.get(f"/api/content/{course_id}").json()
    referenced = {
        Path(str(r["body"]["svg_preview"])).name
        for r in rows
        if isinstance(r.get("body") or {}, dict) and r["body"].get("svg_preview")
    }
    assert referenced, "讲义应生成带 svg_preview 的图条目"
    orphan1 = _write_orphan("orphan-0001.svg")
    orphan2 = _write_orphan("orphan-0002.png")

    dry = client.post("/api/maintenance/gc-images", json={"dry_run": True}).json()
    assert dry["dry_run"] is True and dry["removed"] >= 2, dry
    assert orphan1.exists() and orphan2.exists(), "dry_run 不应真的删除"

    real = client.post("/api/maintenance/gc-images", json={"dry_run": False}).json()
    assert real["dry_run"] is False
    assert real["removed"] >= 2, real
    assert not orphan1.exists() and not orphan2.exists()
    for name in referenced:
        assert (_images_dir() / name).exists(), f"被引用文件不应被 GC 删除: {name}"
    assert row["id"]


def test_delete_content_item_removes_owned_file(client, course_id):
    data = b"fake-image-bytes"
    resp = client.post(
        "/api/image/import",
        data={"course_id": course_id},
        files={"file": ("diagram.png", data, "image/png")},
    )
    assert resp.status_code == 200, resp.text
    item = resp.json()
    rel = item["body"]["path"]
    file_path = get_settings().storage_dir / rel
    assert file_path.exists()

    deleted = client.delete(f"/api/content/{item['id']}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["removed_files"] >= 1
    assert not file_path.exists(), "删除条目应连带删除其 storage 文件"


def test_delete_course_removes_content_files(client, course_id, gen_lecture_row):
    plan = _plan_approve(client, course_id)
    node = _leaf(plan)
    gen_lecture_row(course_id, node["id"])
    rows = client.get(f"/api/content/{course_id}").json()
    files = [
        get_settings().storage_dir / r["body"]["svg_preview"]
        for r in rows
        if isinstance(r.get("body") or {}, dict) and r["body"].get("svg_preview")
    ]
    assert files and all(f.exists() for f in files)

    title = client.get(f"/api/course/{course_id}").json()["title"]
    deleted = client.delete(f"/api/course/{course_id}", params={"confirm": title})
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["removed_files"] >= len(files)
    assert not any(f.exists() for f in files), "删除课程应清理其内容文件"


def test_restore_keeps_snapshot_files_removes_extra(client, course_id, gen_lecture_row):
    plan = _plan_approve(client, course_id)
    node = _leaf(plan)
    gen_lecture_row(course_id, node["id"])

    snap = client.post(f"/api/version/{course_id}/snapshot")
    assert snap.status_code == 200, snap.text
    version_id = snap.json()["version_id"]

    extra = client.post(
        "/api/image/import",
        data={"course_id": course_id},
        files={"file": ("extra.png", b"extra", "image/png")},
    )
    assert extra.status_code == 200, extra.text
    extra_file = get_settings().storage_dir / extra.json()["body"]["path"]
    assert extra_file.exists()

    snapshot_row_files = [
        get_settings().storage_dir / r["body"]["svg_preview"]
        for r in client.get(f"/api/content/{course_id}").json()
        if isinstance(r.get("body") or {}, dict) and r["body"].get("svg_preview")
    ]
    restored = client.post(f"/api/version/{course_id}/restore/{version_id}")
    assert restored.status_code == 200, restored.text

    assert not extra_file.exists(), "回滚后不属于快照的文件应被清理"
    assert all(p.exists() for p in snapshot_row_files), "快照引用的文件应保留"
