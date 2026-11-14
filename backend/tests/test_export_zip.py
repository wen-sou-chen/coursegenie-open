"""导出 ZIP 测试：整门课打包含概览与各章笔记；单节点打包含节点页与依赖图。"""
from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path


def _plan_and_content(client, course_id: str, node_ids: list[str] | None = None) -> list[dict]:
    plan = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "初中生物光合作用与细胞呼吸", "num_chapters": 2,
    }).json()
    assert client.post("/api/plan/approve", json={"course_id": course_id, "approved": True}).status_code == 200
    body = {"course_id": course_id, "include_quiz": True}
    if node_ids:
        body["node_ids"] = node_ids
    job = client.post("/api/content/generate", json=body).json()["job_id"]
    assert client.get(f"/api/content/jobs/{job}").json()["status"] == "success"
    return plan["nodes"]


def _bind(client, course_id: str, root: Path) -> None:
    resp = client.post(f"/api/kb/course/{course_id}/bind", json={
        "vault_root": str(root), "output_rel": "课程/光合作用",
    })
    assert resp.status_code == 200, resp.text


def test_export_zip_all_course(client, course_id):
    root = Path(tempfile.mkdtemp(prefix="coursegenie_zip_all_"))
    _bind(client, course_id, root)
    nodes = _plan_and_content(client, course_id)
    assert len(nodes) >= 2

    resp = client.post("/api/publish/export-notes-zip", json={
        "course_id": course_id, "scope": "all",
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["url"].startswith("/exports/")
    from app.core.config import get_settings
    actual = get_settings().export_dir / data["file"]
    assert actual.exists()
    with zipfile.ZipFile(actual) as zf:
        names = zf.namelist()
        assert any(n.endswith("00-课程概览.md") for n in names)
        assert any("01-" in n and n.endswith(".md") for n in names)
        assert any("习题集" in n and n.endswith(".md") for n in names)


def test_export_zip_selected_node(client, course_id):
    root = Path(tempfile.mkdtemp(prefix="coursegenie_zip_node_"))
    _bind(client, course_id, root)
    nodes = _plan_and_content(client, course_id)
    node = next(n for n in nodes if n["key"] == "1")
    client.post("/api/image/generate", json={
        "course_id": course_id, "prompt": "叶绿体示意图", "size": "512x512", "node_id": node["id"],
    })

    resp = client.post("/api/publish/export-notes-zip", json={
        "course_id": course_id, "scope": "nodes", "node_ids": [node["id"]],
    })
    assert resp.status_code == 200, resp.text
    from app.core.config import get_settings
    actual = get_settings().export_dir / resp.json()["file"]
    with zipfile.ZipFile(actual) as zf:
        names = zf.namelist()
        node_files = [n for n in names if "01-" in n]
        assert node_files, f"应包含第 1 章目录文件：{names}"
        assert any(n.endswith(".png") and "assets" in n for n in names), "应包含依赖配图"
        assert not any("02-" in n for n in names), "不应打包第 2 章内容"
        assert len(names) < 8, "单节点打包应只含该节点相关文件"


def test_export_zip_selected_node_without_content(client, course_id):
    client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "数据可视化入门", "num_chapters": 1,
    })
    resp = client.post("/api/publish/export-notes-zip", json={
        "course_id": course_id, "scope": "nodes", "node_ids": ["no-such-id"],
    })
    assert resp.status_code == 400, resp.text
