"""按章导出测试（新结构）：每章一个目录、知识点成页，配图入本章 assets/，不串章。"""
from __future__ import annotations

import tempfile
from pathlib import Path


def _root() -> Path:
    return Path(tempfile.mkdtemp(prefix="coursegenie_chapter_"))


def test_export_embeds_bound_chapter_assets(client, course_id):
    root = _root()
    resp = client.post(f"/api/kb/course/{course_id}/bind", json={
        "vault_root": str(root), "output_rel": "课程/光合作用",
    })
    assert resp.status_code == 200, resp.text

    plan = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "初中光合作用", "num_chapters": 2,
    }).json()
    chapter1 = next(n for n in plan["nodes"] if n["key"] == "1")
    assert client.post("/api/plan/approve", json={"course_id": course_id, "approved": True}).status_code == 200

    img = client.post("/api/image/generate", json={
        "course_id": course_id, "prompt": "叶绿体结构示意", "size": "512x512", "node_id": chapter1["id"],
    })
    assert img.status_code == 200, img.text
    fc = client.post("/api/flowchart/generate", json={
        "course_id": course_id, "description": "光合作用的过程", "kind": "flow", "node_id": chapter1["id"],
    })
    assert fc.status_code == 200, fc.text

    # 只生成第 1 章的正文+习题（按章作业，不串到第 2 章）
    job = client.post("/api/content/generate", json={
        "course_id": course_id, "node_ids": [chapter1["id"]], "include_quiz": True,
    }).json()["job_id"]
    assert client.get(f"/api/content/jobs/{job}").json()["status"] == "success"

    res = client.post(f"/api/kb/course/{course_id}/export").json()
    out = root / res["output_rel"]

    dirs = sorted(d for d in out.iterdir() if d.is_dir() and d.name[:2].isdigit())
    assert len(dirs) == 1, f"只有第 1 章有内容，应只生成 1 个章节目录：{dirs}"
    dir1 = dirs[0]
    page1 = next(p for p in dir1.glob("*.md"))
    text1 = page1.read_text(encoding="utf-8")
    assert "](assets/" in text1, "第 1 章页应内嵌本章配图（相对路径 assets/）"
    assert "![[图-流程图-" in text1, "第 1 章页应内嵌可见的流程图 SVG"
    assert "本节习题见" in text1, "知识点页末尾应双链到习题集"

    assets1 = list((dir1 / "assets").glob("*.png"))
    assert len(assets1) == 1, "第 1 章配图应复制到本章 assets/"
    assert assets1[0].name in text1

    xfiles = list((out / "图表库").glob("*.excalidraw"))
    assert len(xfiles) >= 1, "流程图应生成可编辑 .excalidraw 进 图表库/"
    svg_files = list((out / "图表库").glob("*.svg"))
    assert len(svg_files) >= 1, "流程图应生成可见 .svg 预览进 图表库/"

    quiz_files = list((out / "习题集").glob("*.md"))
    assert len(quiz_files) == 1, "只有第 1 章生成习题"
    assert "解析" in quiz_files[0].read_text(encoding="utf-8")

    # 第 2 章无内容：不生成任何目录/页面/资源，自然不串章
    all_text = text1
    assert assets1[0].name in all_text
