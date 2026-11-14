# -*- coding: utf-8 -*-
"""P2 概念图（diagram）链路测试：生成、讲义自动带图、Obsidian 导出、删除联动。"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.course import Course, OutlineNode
from app.services.diagram import diagram_to_svg, generate_diagram, infer_layout
from app.services.diagram import LAYOUTS
from app.services.excalidraw_layout import validate_elements


def _plan_approve(client, course_id: str) -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "初中生物光合作用", "num_chapters": 2,
    })
    assert resp.status_code == 200, resp.text
    assert client.post("/api/plan/approve", json={
        "course_id": course_id, "approved": True,
    }).status_code == 200
    return resp.json()


def _leaf(plan: dict) -> dict:
    node = next((n for n in plan["nodes"] if "." in n["key"]), None)
    return node or plan["nodes"][0]


def _rows(client, course_id: str) -> list[dict]:
    return client.get(f"/api/content/{course_id}").json()


def test_layout_inference_rules():
    """学习目标关键词 → 布局映射（用户规则）。"""
    assert infer_layout("掌握光合作用的反应流程") == "chain"
    assert infer_layout("分析发酵成功的条件因素") == "radial"
    assert infer_layout("对比老面与干酵母的异同") == "compare"
    assert infer_layout("梳理植物演化的发展时间线") == "timeline"
    assert infer_layout("给常见面粉做分类") == "ring"
    assert infer_layout("判断面团是否发酵到位") == "decision"
    assert infer_layout("掌握一个普通概念") == "radial"


def test_generate_diagram_all_layouts_persist_clean_excalidraw(client, course_id):
    """mock LLM 生成各布局 diagram：kind 落库、excalidraw_json 合法、text∩text=0。"""
    plan = _plan_approve(client, course_id)
    node = _leaf(plan)
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        node_row = db.get(OutlineNode, node["id"])
        for layout in sorted(LAYOUTS):
            item = generate_diagram(
                db, course, node_row, diagram_type=layout,
                instruction=f"围绕{node_row.title}梳理{layout}布局内容",
            )
            assert item.kind == "diagram"
            assert item.node_id == node_row.id
            assert (item.meta or {}).get("node_key") == node_row.node_key
            body = item.body or {}
            assert body.get("layout") == layout
            doc = body["excalidraw_json"]
            assert doc["type"] == "excalidraw" and doc["version"] == 2
            assert doc["elements"], f"{layout} 未生成任何元素"
            assert json.loads(json.dumps(doc, ensure_ascii=False)) == doc
            issues = validate_elements(doc["elements"])
            assert not issues["text_overlaps"], f"{layout} text∩text 重叠：{issues['text_overlaps']}"
            assert not issues["overflow"], f"{layout} 文字越界：{issues['overflow']}"
            svg_rel = str(body.get("svg_preview") or "")
            assert svg_rel.startswith("images/") and svg_rel.endswith(".svg")
            svg_path = get_settings().storage_dir / svg_rel
            assert svg_path.exists(), f"{layout} 的 SVG 预览未写入 storage/images"
            svg = svg_path.read_text(encoding="utf-8")
            assert svg.startswith("<svg") and "viewBox" in svg
            assert "diagram://" not in json.dumps(body, ensure_ascii=False)
    finally:
        db.close()


def test_diagram_to_svg_outputs_shapes_and_text():
    """SVG 转换器：矩形/文字/连线都能画出（无外部库的粗糙预览）。"""
    svg = diagram_to_svg([
        {"type": "rectangle", "id": "b1", "x": 10, "y": 10, "width": 200, "height": 80,
         "backgroundColor": "#ffe4e6", "strokeColor": "#f43f5e", "strokeWidth": 2,
         "strokeStyle": "solid", "opacity": 100, "isDeleted": False},
        {"type": "arrow", "id": "a1", "x": 210, "y": 50, "width": 60, "height": 0,
         "points": [[0, 0], [60, 0]], "strokeColor": "#94a3b8", "strokeWidth": 2,
         "strokeStyle": "solid", "endArrowhead": "arrow", "isDeleted": False},
        {"type": "text", "id": "t1", "x": 40, "y": 30, "width": 120, "height": 24,
         "text": "要点一", "fontSize": 20, "textAlign": "center",
         "strokeColor": "#374151", "isDeleted": False},
    ])
    assert svg.startswith("<svg")
    assert "<rect" in svg and "<text" in svg and "<polyline" in svg
    assert "要点一" in svg


def test_lecture_auto_diagram_when_mock_returns_no_gen(client, course_id, monkeypatch, gen_lecture_row):
    """mock LLM 不返回 gen:// 时：讲义 markdown 有 diagram://，无位图兜底。"""
    plan = _plan_approve(client, course_id)
    node = _leaf(plan)

    class _NoGenClient:
        name = "no-gen"

        def complete_json(self, system: str, user: str, *, max_tokens: int = 2000) -> dict:
            return {
                "markdown": f"## {node['title']}\n\n正文讲解……\n\n## 小结\n本节要点回顾。",
                "summary": "一句话摘要",
                "keywords": ["要点"],
                "quizzes": [],
            }

    import app.services.lecture as lecture_mod

    monkeypatch.setattr(lecture_mod, "build_llm_client", lambda: _NoGenClient())
    lecture = gen_lecture_row(course_id, node["id"])
    md = str(lecture["body"]["markdown"])
    assert "diagram://" in md and "![一图看懂](diagram://" in md
    assert md.index("![一图看懂](diagram://") < md.index("## 小结"), "概念图引用应插在「## 小结」前"
    assert "/images/" not in md, "不应再自动生成 AI 位图"
    assert "gen://" not in md

    diagrams = [r for r in _rows(client, course_id)
                if r["kind"] == "diagram" and r["node_id"] == node["id"]]
    # 任务 125：图形上限（>6）也生效 ⇒ mock 的一图看懂按容量拆成 2 张，两张都挂同一篇讲义
    assert len(diagrams) == 2
    assert diagrams[0]["meta"].get("from_lecture") == lecture["id"]
    images = [r for r in _rows(client, course_id) if r["kind"] == "image"]
    assert len(images) == 0


def test_export_obsidian_charts_and_wikilink(client, course_id, gen_lecture_row):
    """导出 Obsidian：讲义页直接内嵌可见的 .svg 概念图，图表库/ 同时有可编辑 .excalidraw。"""
    root = Path(tempfile.mkdtemp(prefix="coursegenie_diagram_"))
    bind = client.post(f"/api/kb/course/{course_id}/bind", json={
        "vault_root": str(root), "output_rel": "课程/光合作用",
    })
    assert bind.status_code == 200, bind.text
    plan = _plan_approve(client, course_id)
    node = _leaf(plan)
    lecture = gen_lecture_row(course_id, node["id"])
    lecture_id = lecture["id"]
    diagrams = [r for r in _rows(client, course_id)
                if r["kind"] == "diagram" and r["meta"].get("from_lecture") == lecture_id]
    concepts = [r for r in diagrams if (r["meta"].get("role") or "concept") != "card"]
    assert len(concepts) == 2, "一图看懂超限（图形 > 6）⇒ 按容量拆成 2 张，两张都在同一篇讲义下"
    assert len(diagrams) >= 3, "讲义除概念图外还应就近生成概念卡"
    layout = concepts[0]["body"]["layout"]

    exported = client.post(f"/api/kb/course/{course_id}/export").json()
    out = root / exported["output_rel"]
    chart_files = list((out / "图表库").glob("*.excalidraw"))
    assert chart_files, "图表库/ 应有 .excalidraw 概念图原件"
    diagram_file = next(
        (f for f in chart_files if f.name.startswith(f"图-{layout}-") and f.stem.endswith("-01")),
        None,
    )
    assert diagram_file is not None, f"未找到 图-{layout}-*-01.excalidraw：{chart_files}"
    raw = diagram_file.read_text(encoding="utf-8")
    assert '"type": "excalidraw"' in raw and '"elements"' in raw

    svg_files = list((out / "图表库").glob("*.svg"))
    assert svg_files, "图表库/ 应有概念图 SVG 预览"
    svg_file = next(
        (f for f in svg_files if f.name.startswith(diagram_file.stem + ".")),
        None,
    )
    assert svg_file is not None, f"未找到与 {diagram_file.name} 同名的 SVG"
    assert svg_file.read_text(encoding="utf-8").lstrip().startswith("<svg")

    note = next(
        (p for p in out.rglob("*.md") if f"![[{svg_file.name}]]" in p.read_text(encoding="utf-8")),
        None,
    )
    assert note is not None, "讲义笔记中应有可见的 ![[图-*.svg]] 图片内嵌"
    note_text = note.read_text(encoding="utf-8")
    assert "diagram://" not in note_text, "导出的讲义不应残留 diagram:// 协议引用"
    assert f"![[{svg_file.name}]]" in note_text
    assert f"[[{diagram_file.name}]]" in note_text, "图片下方应露出 .excalidraw 可编辑原件链接"


def test_delete_lecture_cascades_diagram(client, course_id, gen_lecture_row):
    """删除讲义时，其关联的概念图条目一并删除。"""
    plan = _plan_approve(client, course_id)
    node = _leaf(plan)
    row = gen_lecture_row(course_id, node["id"])
    diagrams = [r for r in _rows(client, course_id)
                if r["kind"] == "diagram" and r["meta"].get("from_lecture") == row["id"]]
    concepts = [d for d in diagrams if (d["meta"].get("role") or "concept") != "card"]
    assert len(concepts) == 2, "一图看懂超限（图形 > 6）⇒ 拆成 2 张"
    assert len(diagrams) >= 3, "讲义应有一图看懂（可能多张）+ 就近概念卡"

    deleted = client.delete(f"/api/lecture/{row['id']}")
    assert deleted.status_code == 200, deleted.text
    rows = _rows(client, course_id)
    assert not [r for r in rows if r["id"] == row["id"]]
    assert not [r for r in rows if r["kind"] == "diagram" and r["meta"].get("from_lecture") == row["id"]]
    assert not [r for r in rows if r["kind"] == "diagram" and r["node_id"] == node["id"]], "概念图应随讲义删除"
