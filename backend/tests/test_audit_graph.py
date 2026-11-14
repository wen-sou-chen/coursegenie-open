"""任务 24（特色三件套）测试：知识图谱 / excalidrawlib 风格库 / 课程资产体检报告。

对应 HANDOFF「任务 24 说明」D 段硬标准：
① Mermaid 节点数 = 概念数、边数 = 关系数（去重后），Canvas 与之一致，孤点显式列出；
② excalidrawlib JSON 合法、type 正确、每组非空、字段完整、重复导出幂等；
③ 报告数字与 verify / qc / usage / 导出统计交叉一致；人为造坏链 / 缺来源时报告标出来
   并给建议动作；重复生成幂等；报告内链接可解析。

协作规则第 7 条：DATABASE_URL / STORAGE_DIR / OBSIDIAN_VAULT_ROOT 由 conftest 统一
指向临时目录，本文件不碰真实知识库。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from app.db.session import SessionLocal
from app.models.course import ContentItem, Course, Outline, OutlineNode
from app.services import kb as kb_service
from app.services.style_library import GROUPS

OUT_REL = "课程/光合作用"


# ---------------------------------------------------------------------------
# 夹具与流程助手（概念抽取 / 大纲 / 讲义 / 绑定 Vault）
# ---------------------------------------------------------------------------

def _bind(client, course_id: str, root: Path) -> None:
    resp = client.post(f"/api/kb/course/{course_id}/bind", json={
        "vault_root": str(root), "output_rel": OUT_REL,
    })
    assert resp.status_code == 200, resp.text


def _plan_and_approve(client, course_id: str, num_chapters: int = 2) -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "让初中生理解光合作用", "num_chapters": num_chapters,
    })
    assert resp.status_code == 200, resp.text
    approved = client.post("/api/plan/approve", json={"course_id": course_id, "approved": True})
    assert approved.status_code == 200, approved.text
    return resp.json()


def _generate_concepts(client, course_id: str) -> dict:
    resp = client.post(f"/api/concept/course/{course_id}/generate")
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]
    state: dict | None = None
    deadline = time.time() + 30
    while time.time() < deadline:
        state = client.get(f"/api/concept/jobs/{job_id}").json()
        if state["status"] in ("success", "failed"):
            break
        time.sleep(0.1)
    assert state is not None and state["status"] == "success", state
    return state["result"]


def _concepts(client, course_id: str) -> dict:
    resp = client.get(f"/api/concept/course/{course_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _add_points(course_id: str, chapter_key: str, titles: list[str],
                objective: str = "掌握光合作用的过程") -> list[str]:
    """mock 大纲只有章节点，这里补几个知识点节点（讲义/质检按知识点走）。"""
    db = SessionLocal()
    try:
        outline = (db.query(Outline).filter(Outline.course_id == course_id)
                   .order_by(Outline.created_at.desc()).first())
        base = db.query(OutlineNode).filter(OutlineNode.outline_id == outline.id).count()
        keys = []
        for index, title in enumerate(titles, 1):
            key = f"{chapter_key}.{index}"
            db.add(OutlineNode(outline_id=outline.id, node_key=key, node_type="section",
                               title=title, objective=objective, order_index=base + index))
            keys.append(key)
        db.commit()
        return keys
    finally:
        db.close()


def _node_id(client, course_id: str, key: str) -> str:
    plan = client.get(f"/api/plan/{course_id}").json()
    return next(node["id"] for node in plan["nodes"] if node["key"] == key)


def _add_concept(course_id: str, name: str, definition: str = "测试用孤立概念") -> str:
    """直接落一条没有任何关系的概念，用来验证「孤点必须显式列出」。"""
    db = SessionLocal()
    try:
        row = ContentItem(course_id=course_id, kind="concept", title=name,
                          body={"definition": definition, "points": [], "relations": {}},
                          meta={"chapters": [], "aliases": []})
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _add_item(course_id: str, kind: str, title: str = "测试条目") -> str:
    """直接落一条指定 kind 的条目（用来复算报告覆盖度里的分类计数）。"""
    db = SessionLocal()
    try:
        row = ContentItem(course_id=course_id, kind=kind, title=title, body={}, meta={})
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _add_lecture(course_id: str, node_id: str, markdown: str, title: str = "测试讲义") -> str:
    db = SessionLocal()
    try:
        row = ContentItem(course_id=course_id, node_id=node_id, kind="lecture", title=title,
                          body={"markdown": markdown, "summary": "", "keywords": []}, meta={})
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _norm(name: str) -> str:
    return re.sub(r"\s+", "", str(name or "").strip().lower())


def _expected_edges(rows: list[dict]) -> int:
    """独立复算边数：两端都在概念库的关系，按 (方向, 源, 目标) 去重。"""
    by_name: dict[str, str] = {}
    for row in rows:
        by_name.setdefault(_norm(row["name"]), row["name"])
        for alias in row.get("aliases") or []:
            by_name.setdefault(_norm(alias), row["name"])
    seen: set[tuple] = set()
    kinds = (("broader", "parent"), ("narrower", "child"), ("related", "related"))
    for row in rows:
        relations = (row.get("body") or {}).get("relations") or {}
        for field, direction in kinds:
            for raw in relations.get(field) or []:
                other = by_name.get(_norm(raw))
                if other is None or other == row["name"]:
                    continue
                if direction == "parent":
                    dedup = (other, row["name"])
                elif direction == "child":
                    dedup = (row["name"], other)
                else:
                    dedup = tuple(sorted([row["name"], other]))
                seen.add((direction, dedup))
    return len(seen)


def _mermaid_nodes(text: str) -> set[str]:
    return set(re.findall(r'^\s*(c\d+)\["', text, re.M))


def _mermaid_edges(text: str) -> list[tuple[str, str, str]]:
    return re.findall(r'^\s*(c\d+) (?:-->|-\.->)\|([^|]+)\| (c\d+)\s*$', text, re.M)


# ---------------------------------------------------------------------------
# A. 知识图谱
# ---------------------------------------------------------------------------

def test_graph_mermaid_and_canvas_match_concepts(client, course_id, tmp_path):
    """Mermaid 节点数 = 概念数、边数 = 关系数（去重后）；Canvas 节点/边数与之一致。"""
    root = tmp_path / "vault"
    _bind(client, course_id, root)
    _plan_and_approve(client, course_id)
    _generate_concepts(client, course_id)
    payload = _concepts(client, course_id)
    assert payload["count"] > 0, payload

    resp = client.get(f"/api/concept/course/{course_id}/graph", params={"format": "mermaid"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["stats"]["nodes"] == payload["count"]
    assert data["stats"]["edges"] == _expected_edges(payload["concepts"])

    node_ids = _mermaid_nodes(data["mermaid"])
    assert len(node_ids) == payload["count"]
    assert node_ids == {node["id"] for node in data["nodes"]}
    edges = _mermaid_edges(data["mermaid"])
    assert len(edges) == data["stats"]["edges"] == _expected_edges(payload["concepts"])
    for source, label, target in edges:
        assert source in node_ids and target in node_ids and label
    assert data["mermaid"].startswith("%% CourseGenie") and "graph TD" in data["mermaid"]
    assert "subgraph" in data["mermaid"] and "classDef" in data["mermaid"]
    assert "```mermaid" in data["text"]

    canvas_resp = client.get(f"/api/concept/course/{course_id}/graph", params={"format": "canvas"})
    assert canvas_resp.status_code == 200, canvas_resp.text
    canvas = canvas_resp.json()["canvas"]
    assert {node["id"] for node in canvas["nodes"]} == node_ids
    assert len(canvas["edges"]) == len(edges)
    for node in canvas["nodes"]:
        assert node["type"] == "text" and node["text"].startswith("**")
        assert node["width"] > 0 and node["height"] > 0
    for edge in canvas["edges"]:
        assert edge["fromNode"] in node_ids and edge["toNode"] in node_ids and edge["label"]

    # 孤点显式列出（名单 + 计数与边数口径一致）
    connected = {eid for source, _label, target in edges for eid in (source, target)}
    assert set(data["isolated"]) == {node["name"] for node in data["nodes"]
                                     if node["id"] not in connected}
    assert data["stats"]["isolated"] == len(data["isolated"])
    if data["isolated"]:
        assert "孤立概念" in data["mermaid"]

    bad = client.get(f"/api/concept/course/{course_id}/graph", params={"format": "dot"})
    assert bad.status_code == 400, bad.text


def test_graph_export_is_idempotent_and_marks_isolated(client, course_id, tmp_path):
    """孤立概念必须进名单和 md；导出写两个文件且重复导出字节一致（无副本）。"""
    root = tmp_path / "vault"
    _bind(client, course_id, root)
    _plan_and_approve(client, course_id)
    _generate_concepts(client, course_id)
    _add_concept(course_id, "孤立概念-Alpha")

    first = client.post(f"/api/concept/course/{course_id}/graph")
    assert first.status_code == 200, first.text
    body = first.json()
    assert "孤立概念-Alpha" in body["isolated"]
    assert [Path(name).name for name in body["files"]] == ["_图谱.md", "_图谱.canvas"]
    assert all(name.startswith(f"{OUT_REL}/概念库/") for name in body["files"])

    out = root / body["output_rel"] / "概念库"
    md_path, canvas_path = out / "_图谱.md", out / "_图谱.canvas"
    assert md_path.exists() and canvas_path.exists()
    md_text = md_path.read_text(encoding="utf-8")
    canvas_text = canvas_path.read_text(encoding="utf-8")
    assert "```mermaid" in md_text
    assert "孤立概念-Alpha" in md_text
    assert json.loads(canvas_text)["nodes"]

    again = client.post(f"/api/concept/course/{course_id}/graph")
    assert again.status_code == 200, again.text
    assert md_path.read_text(encoding="utf-8") == md_text
    assert canvas_path.read_text(encoding="utf-8") == canvas_text
    assert len(list(out.glob("_图谱*"))) == 2  # 同名覆盖，不产生副本


def test_graph_rejects_empty_concept_library(client, course_id, tmp_path):
    """概念库为空 → 400 且给出去哪补数据的提示，不写空图。"""
    root = tmp_path / "vault"
    _bind(client, course_id, root)
    _plan_and_approve(client, course_id)
    resp = client.post(f"/api/concept/course/{course_id}/graph")
    assert resp.status_code == 400, resp.text
    assert "概念库为空" in resp.json()["detail"]
    assert not (root / OUT_REL / "概念库" / "_图谱.md").exists()


# ---------------------------------------------------------------------------
# B. .excalidrawlib 风格库
# ---------------------------------------------------------------------------

def test_style_library_format_and_groups(client, course_id, tmp_path):
    """JSON 合法、type/version 正确、四组非空、元素字段完整、预览与库一致。"""
    root = tmp_path / "vault"
    _bind(client, course_id, root)

    resp = client.get(f"/api/kb/course/{course_id}/style-library")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    library = body["library"]
    assert library["type"] == "excalidrawlib"
    assert int(library["version"]) == 2
    assert body["file"] == "图表库/_风格库.excalidrawlib"

    grouped: dict[str, list[dict]] = {}
    elements_total = 0
    for item in library["libraryItems"]:
        assert item["id"] and item["name"] and item["created"], item
        assert isinstance(item["elements"], list) and item["elements"], item
        grouped.setdefault(item["name"].split(" ", 1)[0], []).append(item)
        for element in item["elements"]:
            assert {"id", "type", "x", "y", "width", "height"} <= set(element), element
            assert element["type"] in ("rectangle", "ellipse", "diamond", "text", "arrow", "line")
            assert element["width"] >= 0 and element["height"] >= 0
            assert element["width"] or element["height"]
            elements_total += 1
    assert set(grouped) == set(GROUPS), grouped.keys()
    assert all(grouped[group] for group in GROUPS)

    stats = body["stats"]
    assert stats["items"] == len(library["libraryItems"]) == sum(len(v) for v in grouped.values())
    assert stats["elements"] == elements_total
    assert set(stats["groups"]) == set(GROUPS)
    assert stats["theme"] and stats["theme_name"]

    from app.core.config import get_settings
    assert get_settings().export_style_library is True


def test_style_library_export_is_idempotent(client, course_id, tmp_path):
    """写盘落点正确、重复导出字节一致且不产生副本，自检通过。"""
    root = tmp_path / "vault"
    _bind(client, course_id, root)

    first = client.post(f"/api/kb/course/{course_id}/style-library")
    assert first.status_code == 200, first.text
    assert first.json()["file"] == "图表库/_风格库.excalidrawlib"
    charts = root / first.json()["output_rel"] / "图表库"
    path = charts / "_风格库.excalidrawlib"
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    library = json.loads(text)
    assert library["type"] == "excalidrawlib" and len(library["libraryItems"]) == 17

    second = client.post(f"/api/kb/course/{course_id}/style-library")
    assert second.status_code == 200, second.text
    assert path.read_text(encoding="utf-8") == text
    assert len(list(charts.glob("_风格库*"))) == 1
    assert second.json()["stats"] == first.json()["stats"]


# ---------------------------------------------------------------------------
# C. 课程资产体检报告
# ---------------------------------------------------------------------------

def test_audit_report_cross_checks_modules(client, course_id, tmp_path, gen_lecture_row):
    """报告八段齐全，数字与 verify / qc / usage / 导出统计逐项一致；重复生成幂等。"""
    root = tmp_path / "vault"
    _bind(client, course_id, root)
    _plan_and_approve(client, course_id)
    _generate_concepts(client, course_id)
    _add_points(course_id, "1", ["光反应", "暗反应"])
    gen_lecture_row(course_id, _node_id(client, course_id, "1.1"))
    exported = client.post(f"/api/kb/course/{course_id}/export")
    assert exported.status_code == 200, exported.text

    report = client.get(f"/api/audit/course/{course_id}/report")
    assert report.status_code == 200, report.text
    data = report.json()
    assert {"conclusion", "coverage", "consistency", "links", "quality",
            "usage", "export_health", "actions"} <= set(data)

    # 覆盖度 ↔ 概念库 / DB 容器真实计数（另一条通路复算）
    concepts = _concepts(client, course_id)
    assert data["coverage"]["concepts"] == concepts["count"]
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        items = db.query(ContentItem).filter(ContentItem.course_id == course_id).all()
        assert data["coverage"]["lectures"] == len([i for i in items if i.kind == "lecture"])
        assert data["coverage"]["ppt"] == len([i for i in items if i.kind == "ppt"])
        # 链接健康 / 导出健康 ↔ 导出模块的同一份统计
        assert data["links"] == kb_service.course_link_health(db, course)
        assert data["export_health"] == kb_service.course_asset_health(db, course)
    finally:
        db.close()

    # 一致性 ↔ /api/kb/course/{id}/verify
    verify = client.post(f"/api/kb/course/{course_id}/verify").json()
    for key in ("checked", "consistent", "inconsistent", "ok"):
        assert data["consistency"][key] == verify[key], key

    # 质检 ↔ /api/lecture/course/{id}/qc
    qc = client.post(f"/api/lecture/course/{course_id}/qc", json={"worst_n": 3}).json()
    assert data["quality"]["checked"] == qc["checked"] > 0
    assert data["quality"]["average_score"] == qc["average_score"]
    assert data["quality"]["min_score"] == qc["min_score"]
    assert data["quality"]["max_score"] == qc["max_score"]
    assert data["quality"]["failure_distribution"] == qc["failure_distribution"]
    assert [row["item_id"] for row in data["quality"]["worst"]] == \
        [row["item_id"] for row in qc["worst"]]

    # 用量 ↔ /api/analytics/{id}/usage
    usage = client.get(f"/api/analytics/{course_id}/usage").json()
    assert data["usage"]["items_measured"] == usage["items_measured"] > 0
    assert data["usage"]["total_tokens"] == usage["total_tokens"]
    assert data["usage"]["prompt_tokens"] == usage["prompt_tokens"]
    assert data["usage"]["completion_tokens"] == usage["completion_tokens"]
    assert data["usage"]["cost_cny"] == usage["cost_cny"]
    assert data["usage"]["avg_duration_ms"] == usage["avg_duration_ms"]
    assert [row["item_id"] for row in data["usage"]["top_items"]] == \
        [row["item_id"] for row in usage["top_items"][:3]]

    # 结论卡分数 = 100 − 扣分（唯一自算部分，扣分项来自上面七段）
    assert data["conclusion"]["score"] == max(
        0, 100 - sum(row["cut"] for row in data["conclusion"]["deductions"]))
    assert data["conclusion"]["level"] in ("健康", "待改进", "有风险")
    # 干净课程：报告所依赖的链接全部可解析
    assert data["links"]["broken"] == 0, data["links"]["broken_links"]

    written = client.post(f"/api/audit/course/{course_id}/report")
    assert written.status_code == 200, written.text
    path = root / written.json()["output_rel"] / "00-课程体检报告.md"
    text = path.read_text(encoding="utf-8")
    for heading in ("## 1. 结论卡", "## 2. 覆盖度", "## 3. 一致性", "## 4. 链接健康",
                    "## 5. 质检", "## 6. 用量与成本", "## 7. 导出健康", "## 8. 建议动作"):
        assert heading in text, heading
    assert str(data["conclusion"]["score"]) in text
    assert str(data["usage"]["total_tokens"]) in text

    again = client.post(f"/api/audit/course/{course_id}/report")
    assert again.status_code == 200, again.text
    assert path.read_text(encoding="utf-8") == text  # 正文无时间戳 → 幂等
    assert len(list((path.parent).glob("00-课程体检报告*"))) == 1


def test_audit_coverage_counts_chart_kinds_by_prefix(client, course_id, tmp_path):
    """P9-2：数据图表按 `chart_*` 前缀统计（回归：原先写死 kind='chart' → 恒为 0）。

    真实库里的子类型是 chart_bar / chart_line / chart_table / chart_mindmap /
    chart_timeline，没有裸 "chart"。「概念图/流程图（条目）」仍取容器里的 diagram
    条目数；讲义正文的 mermaid 流程图段数是另一套口径（任务 27），这里不改写它。
    """
    root = tmp_path / "vault"
    _bind(client, course_id, root)
    _plan_and_approve(client, course_id)
    for kind in ("chart_bar", "chart_bar", "chart_line", "chart_table",
                 "chart_mindmap", "chart_timeline"):
        _add_item(course_id, kind)
    _add_item(course_id, "chart")  # 裸 "chart" 不是真实 kind，不该被算进来
    _add_item(course_id, "diagram")
    _add_item(course_id, "diagram")

    data = client.get(f"/api/audit/course/{course_id}/report").json()
    assert data["coverage"]["charts"] == 6, data["coverage"]
    assert data["coverage"]["diagrams"] == 2, data["coverage"]

    db = SessionLocal()
    try:
        items = db.query(ContentItem).filter(ContentItem.course_id == course_id).all()
        assert data["coverage"]["charts"] == len(
            [i for i in items if str(i.kind or "").startswith("chart_")])
        assert data["coverage"]["diagrams"] == len([i for i in items if i.kind == "diagram"])
    finally:
        db.close()
    assert "| 数据图表（条目） | 6 |" in data["markdown"]
    assert "| 概念图/流程图（条目） | 2 |" in data["markdown"]


def test_audit_report_flags_bad_link_and_missing_sources(client, course_id, tmp_path):
    """人为造坏链 + 缺来源讲义 → 报告标出来并给出对应建议动作。"""
    root = tmp_path / "vault"
    _bind(client, course_id, root)
    _plan_and_approve(client, course_id)
    _add_points(course_id, "1", ["光反应"])
    _add_lecture(course_id, _node_id(client, course_id, "1.1"), (
        "## 缺来源讲义\n\n### 引入\n- 一句话引入，但没有来源标注\n\n"
        "### 核心讲解\n- 关键点一，没有任何依据\n- 关键点二，也没有依据\n\n"
        "### 小结\n- 小结一句话\n"
    ))
    assert client.post(f"/api/kb/course/{course_id}/export").status_code == 200
    out = root / OUT_REL
    (out / "99_坏链测试.md").write_text("这是一条坏链：[[根本不存在的概念页]]\n", encoding="utf-8")

    data = client.get(f"/api/audit/course/{course_id}/report").json()
    assert data["links"]["broken"] >= 1
    assert any(row["target"] == "根本不存在的概念页" for row in data["links"]["broken_links"])
    assert data["conclusion"]["score"] < 100
    assert data["conclusion"]["level"] != "健康"

    markdown = data["markdown"]
    assert "❌" in markdown
    actions = " ".join(row["text"] + row["action"] for row in data["actions"])
    assert "坏链" in actions
    assert "来源" in actions
    assert any(row["level"] == "fail" and "坏链" in row["text"] for row in data["actions"])
    assert any("来源" in row["text"] for row in data["actions"])


def test_audit_report_without_vault_is_explicit(client, course_id, no_global_vault):
    """没绑定笔记库时不报假数字：链接/导出健康显式说明原因。"""
    _plan_and_approve(client, course_id)
    data = client.get(f"/api/audit/course/{course_id}/report").json()
    assert data["links"]["ok"] is False and data["links"]["reason"]
    assert data["export_health"]["ok"] is False and data["export_health"]["reason"]
    assert data["conclusion"]["level"] in ("健康", "待改进", "有风险")
    assert any("绑定" in row["action"] for row in data["actions"])
    assert any("还没有任何讲义" in row["text"] for row in data["actions"])


@pytest.mark.parametrize("fmt", ["mermaid", "canvas"])
def test_graph_requires_bound_course(client, fmt):
    """课程不存在 → 404（接口契约）。"""
    assert client.get(f"/api/concept/course/nope/graph", params={"format": fmt}).status_code == 404
    assert client.get("/api/kb/course/nope/style-library").status_code == 404
    assert client.get("/api/audit/course/nope/report").status_code == 404
