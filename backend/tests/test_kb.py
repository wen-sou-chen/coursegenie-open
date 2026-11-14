"""素材与笔记系统测试：原始资料、按知识点成页的 Obsidian 笔记系统、防覆盖。"""
from __future__ import annotations

import tempfile
from pathlib import Path


def _root() -> Path:
    return Path(tempfile.mkdtemp(prefix="coursegenie_notes_"))


def _bind(client, course_id: str, root: Path, out_rel: str = "课程/光合作用") -> None:
    resp = client.post(f"/api/kb/course/{course_id}/bind", json={
        "vault_root": str(root), "output_rel": out_rel,
    })
    assert resp.status_code == 200, resp.text


def _upload_material(client, course_id: str, name: str = "光合作用讲义.txt", text: str | None = None) -> dict:
    content = text or "光合作用是植物利用光能合成有机物的过程，发生在叶绿体，包括光反应与卡尔文循环。"
    resp = client.post("/api/material/upload", data={"course_id": course_id},
                       files={"file": (name, content.encode("utf-8"), "text/plain")})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _plan_and_approve(client, course_id: str, goal: str = "让初中生理解光合作用") -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": goal, "num_chapters": 2,
    })
    assert resp.status_code == 200, resp.text
    assert client.post("/api/plan/approve", json={"course_id": course_id, "approved": True}).status_code == 200
    return resp.json()


def _chapter_dirs(out: Path) -> list[Path]:
    return sorted(d for d in out.iterdir() if d.is_dir() and d.name[:2].isdigit())


def test_material_upload_paste_list_delete(client, course_id):
    m1 = _upload_material(client, course_id)
    assert m1["kind"] == "file"
    assert m1["chars"] > 0

    resp = client.post("/api/material/paste", json={
        "course_id": course_id, "title": "课堂观察记录", "text": "学生在实验中观察到叶片变蓝，说明产生了淀粉。",
    })
    assert resp.status_code == 200, resp.text
    m2 = resp.json()
    assert m2["kind"] == "text"

    rows = client.get(f"/api/material/{course_id}").json()
    assert len(rows) == 2

    assert client.delete(f"/api/material/{m1['id']}").status_code == 200
    assert len(client.get(f"/api/material/{course_id}").json()) == 1


def test_export_generates_note_system(client, course_id):
    root = _root()
    _bind(client, course_id, root)
    _upload_material(client, course_id)

    _plan_and_approve(client, course_id)
    img = client.post("/api/image/generate", json={
        "course_id": course_id, "prompt": "光合作用示意图", "size": "512x512",
    }).json()
    job = client.post("/api/content/generate", json={"course_id": course_id}).json()["job_id"]
    assert client.get(f"/api/content/jobs/{job}").json()["status"] == "success"

    resp = client.post(f"/api/kb/course/{course_id}/export")
    assert resp.status_code == 200, resp.text
    assert not resp.json()["preserved"]
    out = root / resp.json()["output_rel"]

    overview = out / "00-课程概览.md"
    assert overview.exists()
    text = overview.read_text(encoding="utf-8")
    assert "光合作用讲义" in text            # 素材库回链
    assert "素材库" in text
    assert "唯一编辑源" in text              # 生成横幅说明

    # 素材整理成一页
    material_pages = list((out / "素材库").glob("*.md"))
    assert len(material_pages) == 1
    assert "叶绿体" in material_pages[0].read_text(encoding="utf-8")

    # 分章成目录：每章一个 01-*/02-* 目录，内含知识点页
    chapters = _chapter_dirs(out)
    assert len(chapters) == 2, f"应有 2 个章节目录：{chapters}"
    node_pages = [p for d in chapters for p in d.glob("*.md")]
    assert len(node_pages) >= 2, "每个章节应至少有一个知识点页"
    all_node_text = "\n".join(p.read_text(encoding="utf-8") for p in node_pages)
    assert "讲解" in all_node_text or "```" in all_node_text
    assert "随堂练习" in all_node_text

    # 习题集：每个有习题的节点一页
    quiz_files = list((out / "习题集").glob("*.md"))
    assert len(quiz_files) >= 2
    assert "解析" in quiz_files[0].read_text(encoding="utf-8")

    # 课程级内容（未绑定节点）独立成页，不混入章节页
    course_level = out / "00-课程级内容.md"
    assert course_level.exists()
    assert "光合作用示意图" in course_level.read_text(encoding="utf-8")

    # 课程级配图归 图表库/
    charts = list((out / "图表库").glob("*.png"))
    assert len(charts) == 1
    assert img["body"]["path"].split("/")[-1] == charts[0].name


def test_export_preserves_manual_edits(client, course_id):
    root = _root()
    _bind(client, course_id, root)
    _plan_and_approve(client, course_id)
    job = client.post("/api/content/generate", json={"course_id": course_id}).json()["job_id"]
    assert client.get(f"/api/content/jobs/{job}").json()["status"] == "success"

    first = client.post(f"/api/kb/course/{course_id}/export").json()
    out = root / first["output_rel"]
    node_page = next(p for d in _chapter_dirs(out) for p in d.glob("*.md"))
    rel = node_page.relative_to(root).as_posix()

    # 教师在笔记里手动修改
    node_page.write_text(node_page.read_text(encoding="utf-8") + "\n<!-- teacher manual edit -->\n",
                         encoding="utf-8")

    # 再次导出：跳过手动改过的文件
    second = client.post(f"/api/kb/course/{course_id}/export").json()
    kept = {(p["file"], p["reason"]) for p in second["preserved"]}
    assert (rel, "manual-edit") in kept, second["preserved"]
    assert "teacher manual edit" in node_page.read_text(encoding="utf-8")

    # force 导出才覆盖
    third = client.post(f"/api/kb/course/{course_id}/export?force=true").json()
    assert not third["preserved"]
    assert "teacher manual edit" not in node_page.read_text(encoding="utf-8")


def test_export_without_binding_uses_default_dir(client, course_id, no_global_vault):
    """教师没有 vault 也不影响：默认导出到应用 storage/kb，仍是完整笔记系统。"""
    client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "数学入门", "num_chapters": 1,
    })
    resp = client.post(f"/api/kb/course/{course_id}/export")
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["vault_root"].endswith("kb")
    assert (Path(result["vault_root"]) / result["output_rel"] / "00-课程概览.md").exists()


def test_export_partial_without_outline_is_ok(client, course_id):
    """教师尚未生成大纲也能先导出：返回提示而非 400，笔记不会空。"""
    root = _root()
    _bind(client, course_id, root)
    resp = client.post(f"/api/kb/course/{course_id}/export")
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert any("大纲" in w for w in result["warnings"])
    out = root / result["output_rel"]
    overview = out / "00-课程概览.md"
    assert overview.exists()
    assert "暂无章节" in overview.read_text(encoding="utf-8")
