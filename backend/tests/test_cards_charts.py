# -*- coding: utf-8 -*-
"""概念卡 + 数据图链路：概念卡就近编排/重建不重复；有讲义时柱状图等数据图也可见导出。"""
from __future__ import annotations

import tempfile
from pathlib import Path


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


def _rows(client, course_id: str) -> list[dict]:
    return client.get(f"/api/content/{course_id}").json()


def test_lecture_cards_and_bar_chart_export_visible(client, course_id, gen_lecture_row):
    """讲义 + 柱状图：导出后讲义页就近可见概念卡，柱状图为可见 SVG + 可编辑原件。"""
    plan = _plan_approve(client, course_id)
    node = _leaf(plan)
    root = Path(tempfile.mkdtemp(prefix="coursegenie_cards_"))
    bind = client.post(f"/api/kb/course/{course_id}/bind", json={
        "vault_root": str(root), "output_rel": "课程/卡片课",
    })
    assert bind.status_code == 200, bind.text

    row = gen_lecture_row(course_id, node["id"])
    md = str(row["body"]["markdown"])
    card_markers = md.count("概念卡：")
    assert card_markers >= 1, "讲义 markdown 应含就近的概念卡引用"

    chart = client.post("/api/chart/generate", json={
        "course_id": course_id, "node_id": node["id"],
        "chart_type": "bar", "instruction": "光合作用速率对比",
    })
    assert chart.status_code == 200, chart.text

    exported = client.post(f"/api/kb/course/{course_id}/export").json()
    out = root / exported["output_rel"]
    note = next(p for p in out.rglob("*.md") if p.name.startswith(node["key"] + "-"))
    text = note.read_text(encoding="utf-8")
    chart_svgs = sorted(p.name for p in (out / "图表库").glob("图-柱状图-*.svg"))
    assert chart_svgs, "柱状图应有可见 SVG 预览"
    assert any(f"![[{name}]]" in text for name in chart_svgs), "讲义页应内嵌柱状图"
    assert list((out / "图表库").glob("图-柱状图-*.excalidraw")), "柱状图应有可编辑原件"
    assert not list((out / "图表库").glob("*.png")), "导出不应出现 PNG 位图"
    assert text.index("![[图-柱状图-") < text.index("## 随堂练习"), "数据图应插在随堂练习之前"
    card_svgs = sorted(p.name for p in (out / "图表库").glob("图-radial-*.svg"))
    if card_svgs:
        assert any("![[%s]]" % name in text for name in card_svgs), "概念卡应以可见 SVG 内嵌"


def test_regenerate_concept_cards_replaces_without_duplicate(client, course_id, gen_lecture_row):
    plan = _plan_approve(client, course_id)
    node = _leaf(plan)
    row = gen_lecture_row(course_id, node["id"])

    def _cards() -> list[dict]:
        return [r for r in _rows(client, course_id)
                if r["kind"] == "diagram" and r["node_id"] == node["id"]
                and r["meta"].get("role") == "card"]

    first = _cards()
    assert len(first) >= 1
    md1 = str(row["body"]["markdown"])
    assert md1.count("概念卡：") == len(first)

    resp = client.post(f"/api/lecture/{row['id']}/cards")
    assert resp.status_code == 200, resp.text
    second = _cards()
    md2 = str(resp.json()["body"]["markdown"])
    assert len(second) == len(first), "重建概念卡应原地替换，不重复堆积"
    assert md2.count("概念卡：") == len(second) == len(first)
    for r in second:
        assert r["meta"].get("from_lecture") == row["id"]
