"""图表 API 测试：生成（mock LLM）→ 列表 → 编辑 → 删除。"""
from __future__ import annotations


def _approve(client, course_id: str) -> dict:
    resp = client.post("/api/plan/approve", json={"course_id": course_id, "approved": True})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_chart_generate_blocked_until_approved(client, course_id):
    plan = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "近五年气温变化趋势", "num_chapters": 2,
    })
    assert plan.status_code == 200, plan.text
    node_id = plan.json()["nodes"][0]["id"]

    blocked = client.post("/api/chart/generate", json={
        "course_id": course_id, "node_id": node_id, "chart_type": "bar",
        "instruction": "近五年全球气温变化",
    })
    assert blocked.status_code == 409  # 大纲未通过时不允许生成

    _approve(client, course_id)
    ok = client.post("/api/chart/generate", json={
        "course_id": course_id, "node_id": node_id, "chart_type": "bar",
        "instruction": "近五年全球气温变化",
    })
    assert ok.status_code == 200, ok.text


def test_chart_generate_types(client, course_id):
    plan = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "数据可视化入门", "num_chapters": 2,
    })
    assert plan.status_code == 200, plan.text
    _approve(client, course_id)
    node_id = plan.json()["nodes"][0]["id"]

    for ctype in ("bar", "line", "pie", "mindmap", "timeline", "table"):
        resp = client.post("/api/chart/generate", json={
            "course_id": course_id, "node_id": node_id, "chart_type": ctype,
            "instruction": "示例数据",
        })
        assert resp.status_code == 200, resp.text
        row = resp.json()
        assert row["kind"] == f"chart_{ctype}"
        assert row["node_id"] == node_id

    rows = client.get(f"/api/chart/{course_id}").json()
    # 任务 125：图形上限（>6）也生效 ⇒ mock 的 3 步时间轴拆成 2 张，所以是 7 条不是 6 条
    assert len(rows) == 7
    assert {r["kind"] for r in rows} == {f"chart_{t}" for t in ("bar", "line", "pie", "mindmap", "timeline", "table")}

    # 编辑数据
    target = next(r for r in rows if r["kind"] == "chart_bar")
    patched = client.patch(f"/api/chart/{target['id']}", json={"title": "改标题",
                                                               "data": {"labels": ["A", "B"], "datasets": [{"label": "x", "data": [1, 2]}]}})
    assert patched.status_code == 200, patched.text
    assert patched.json()["title"] == "改标题"
    assert patched.json()["body"]["data"]["labels"] == ["A", "B"]

    # 删除
    assert client.delete(f"/api/chart/{target['id']}").status_code == 200
    assert len(client.get(f"/api/chart/{course_id}").json()) == 6

    # 内容列表也可看到 chart 条目（供前端整课渲染使用）
    content = client.get(f"/api/content/{course_id}").json()
    assert any(i["kind"] == "chart_mindmap" for i in content)


def test_chart_incremental_vault_write(client, course_id):
    """图表生成后自动增量写入绑定 Vault 的知识点笔记（数据图=可见 SVG+可编辑原件）。"""
    import tempfile
    from pathlib import Path

    root = Path(tempfile.mkdtemp(prefix="coursegenie_chart_"))
    resp = client.post(f"/api/kb/course/{course_id}/bind", json={
        "vault_root": str(root), "output_rel": "课程/数据图表",
    })
    assert resp.status_code == 200, resp.text

    plan = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "图表数据入门", "num_chapters": 1,
    })
    assert plan.status_code == 200, plan.text
    _approve(client, course_id)
    node_id = plan.json()["nodes"][0]["id"]

    bar = client.post("/api/chart/generate", json={
        "course_id": course_id, "node_id": node_id, "chart_type": "bar", "instruction": "示例数据",
    }).json()
    assert client.post("/api/chart/generate", json={
        "course_id": course_id, "node_id": node_id, "chart_type": "table", "instruction": "示例数据",
    }).status_code == 200

    rows = client.get(f"/api/chart/{course_id}").json()
    assert len(rows) == 2
    # 条目状态已回显到内容列表 meta.vault
    content = client.get(f"/api/content/{course_id}").json()
    row = next(r for r in content if r["id"] == bar["id"])
    assert row["meta"]["vault"]["vault_write"] == "saved"

    out = root / "课程" / "数据图表"
    chapter_dirs = [d for d in out.iterdir() if d.is_dir() and d.name[:2].isdigit()]
    assert chapter_dirs, "应按新结构生成 01-* 章节目录"
    notes = [p for d in chapter_dirs for p in d.glob("*.md")]
    assert len(notes) >= 1
    note_text = notes[0].read_text(encoding="utf-8")
    assert "```json" not in note_text, "柱状图不再以 JSON 副本导出"
    assert "图-柱状图-" in note_text and "可编辑原件" in note_text, "柱状图应可见 SVG + 可编辑原件"
    assert "| 指标 | 数值 | 说明 |" in note_text   # 表格以 Markdown 写入笔记
    chart_dir = out / "图表库"
    assert list(chart_dir.glob("图-柱状图-*.svg")), "柱状图应生成可见 SVG 进 图表库/"
    assert list(chart_dir.glob("图-柱状图-*.excalidraw")), "柱状图应生成可编辑 .excalidraw 进 图表库/"
