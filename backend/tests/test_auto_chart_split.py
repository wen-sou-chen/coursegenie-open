# -*- coding: utf-8 -*-
"""讲义自动配图按内容分流（概念→diagram / 数据类→chart_*）的链路测试。"""
from __future__ import annotations

import json
import time


def _plan_approve(client, course_id: str, goal: str = "初中生物光合作用",
                  chapters: int = 2) -> dict:
    plan = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": goal, "num_chapters": chapters,
    })
    assert plan.status_code == 200, plan.text
    assert client.post("/api/plan/approve", json={
        "course_id": course_id, "approved": True}).status_code == 200
    return plan.json()


def _leaf(plan: dict) -> dict:
    return next((n for n in plan["nodes"] if "." in n["key"]), None) or plan["nodes"][0]


def _rows(client, course_id: str) -> list[dict]:
    return client.get(f"/api/content/{course_id}").json()


def _lecture_md(topic: str) -> str:
    return (
        f"## {topic}\n\n"
        f"### 核心概念\n{topic}的定义与机制是本节重点。\n\n"
        f"### 数值对比\n不同方案的效果对比：A 组 3、B 组 5、C 组 8。\n\n"
        f"### 体系分类\n{topic}可按类别划分成几个组成部分。\n\n"
        f"### 阶段进程\n整个过程分几步依次推进。\n\n"
        f"## 小结\n{topic}要点已讲清。\n\n## 随堂练习\n题目。\n"
    )


def _cards_mix() -> list[dict]:
    return [
        {"anchor": "核心概念", "caption": "核心概念",
         "chart_type": "concept", "layout": "radial",
         "nodes": [{"emoji": "⭐", "title": "定义", "subs": ["划清边界"]},
                   {"emoji": "🔧", "title": "机制", "subs": ["讲清原理"]}]},
        {"anchor": "数值对比", "caption": "效果对比", "chart_type": "bar",
         "title": "三组效果对比",
         "data": {"labels": ["A 组", "B 组", "C 组"],
                  "datasets": [{"label": "效果值", "data": [3, 5, 8]}]}},
        {"anchor": "体系分类", "caption": "体系分类", "chart_type": "mindmap",
         "title": "体系构成",
         "data": {"root": "体系",
                  "branches": [{"title": "甲类", "subs": ["要素一"]},
                               {"title": "乙类", "subs": ["要素二"]},
                               {"title": "丙类", "subs": ["要素三"]}]}},
        {"anchor": "阶段进程", "caption": "阶段进程", "chart_type": "timeline",
         "title": "推进阶段",
         "data": {"steps": [{"title": "准备", "subs": ["定目标"]},
                            {"title": "执行", "subs": ["推进"]},
                            {"title": "收尾", "subs": ["校验"]}]}},
    ]


class _MixCardsClient:
    """讲义用同一实例：LECTURE_JSON 给讲义，CARDS_JSON 给四类混合卡。"""

    name = "mix-cards"

    def __init__(self, cards=None, md=None):
        self._cards = cards or _cards_mix()
        self._md = md

    def complete_json(self, system: str, user: str, *, max_tokens: int = 2000) -> dict:
        if "LECTURE_JSON" in system:
            topic = (user.splitlines()[0] if user.splitlines() else "知识点").strip()
            return {"markdown": self._md or _lecture_md(topic),
                    "summary": "摘要", "keywords": ["要点"], "quizzes": []}
        if "CARDS_JSON" in system:
            return {"cards": self._cards}
        return {"cards": []}


def test_lecture_auto_splits_concept_and_data_charts(client, course_id, monkeypatch, gen_lecture_row):
    """讲义自动配图按 chart_type 分流：diagram 概念卡 + bar/mindmap/timeline 数据卡。"""
    plan = _plan_approve(client, course_id)
    node = _leaf(plan)

    import app.services.lecture as lecture_mod

    monkeypatch.setattr(lecture_mod, "build_llm_client",
                        lambda: _MixCardsClient())
    lecture = gen_lecture_row(course_id, node["id"])
    md = str(lecture["body"]["markdown"])
    assert "![概念卡：核心概念](diagram://" in md
    assert "![数据卡：效果对比](chart://" in md
    assert "![数据卡：体系分类](chart://" in md
    # 任务 125：图形上限也生效 ⇒ 3 步时间轴卡（7 个图形 > 6）拆成 2 张，引用行标分册
    assert "![数据卡：阶段进程（图 1/2）](chart://" in md
    assert "![数据卡：阶段进程（图 2/2）](chart://" in md

    rows = _rows(client, course_id)
    node_rows = [r for r in rows if r["node_id"] == node["id"]]
    diagrams = [r for r in node_rows if r["kind"] == "diagram"]
    concepts = [r for r in diagrams if (r["meta"].get("role") or "concept") == "concept"]
    concept_cards = [r for r in diagrams if r["meta"].get("role") == "card"]
    assert len(concepts) == 2, "一图看懂超限（图形 > 6）⇒ 按容量拆成 2 张，不是丢掉"
    assert len(concept_cards) == 1, "概念卡走 diagram 服务"
    chart_kinds = {r["kind"] for r in node_rows if r["kind"].startswith("chart_")}
    assert chart_kinds == {"chart_bar", "chart_mindmap", "chart_timeline"}
    for r in node_rows:
        if r["kind"].startswith("chart_"):
            assert r["meta"].get("role") == "card"
            assert r["meta"].get("from_lecture") == lecture["id"]
    bar = next(r for r in node_rows if r["kind"] == "chart_bar")
    assert bar["body"].get("excalidraw_json"), "bar 数据图应带 Excalidraw 原件"
    mind = next(r for r in node_rows if r["kind"] == "chart_mindmap")
    assert mind["body"].get("excalidraw_json") and mind["body"].get("svg_preview"), "思维导图应为 Excalidraw 版"
    assert mind["body"].get("mermaid", "").startswith("mindmap"), "思维导图保留 mermaid 备用"
    tl = next(r for r in node_rows if r["kind"] == "chart_timeline")
    assert tl["body"].get("excalidraw_json") and tl["body"].get("svg_preview")
    assert tl["body"].get("mermaid", "").startswith("timeline")

    # 导出后：正文内嵌 SVG、图表库有概念图+数据图原件，且无 PNG
    import tempfile
    from pathlib import Path

    root = Path(tempfile.mkdtemp(prefix="coursegenie_split_"))
    bind = client.post(f"/api/kb/course/{course_id}/bind", json={
        "vault_root": str(root), "output_rel": "课程/分流课",
    })
    assert bind.status_code == 200, bind.text
    exported = client.post(f"/api/kb/course/{course_id}/export").json()
    out = root / exported["output_rel"]
    note = next(p for p in out.rglob("*.md") if p.name.startswith(node["key"] + "-"))
    text = note.read_text(encoding="utf-8")
    assert "![[图-思维导图-" in text, "思维导图应以可见 SVG 内嵌"
    assert "![[图-时间轴-" in text, "时间轴应以可见 SVG 内嵌"
    assert "![[图-柱状图-" in text, "柱状图应以可见 SVG 内嵌"
    assert text.count("可编辑原件") >= 4
    chart_dir = out / "图表库"
    assert list(chart_dir.glob("图-思维导图-*.svg")), "思维导图应生成可见 SVG"
    assert list(chart_dir.glob("图-思维导图-*.excalidraw")), "思维导图应有可编辑原件"
    assert list(chart_dir.glob("图-时间轴-*.excalidraw"))
    assert not list(chart_dir.glob("*.png")), "导出不应出现 PNG"

    # 重建卡片（只给概念卡）应清掉旧数据卡；删除讲义应连带清理 diagram 与 chart_*
    monkeypatch.setattr(lecture_mod, "build_llm_client", lambda: _MixCardsClient(
        cards=[{"anchor": "核心概念", "caption": "核心概念", "chart_type": "concept",
                "layout": "chain",
                "nodes": [{"emoji": "⭐", "title": "定义", "subs": ["划清边界"]}]}]
    ))
    rebuild = client.post(f"/api/lecture/{lecture['id']}/cards")
    assert rebuild.status_code == 200, rebuild.text
    rows = _rows(client, course_id)
    node_charts = [r for r in rows if r["node_id"] == node["id"] and r["kind"].startswith("chart_")]
    assert node_charts == [], "重建概念卡应清理上一版自动数据卡"

    deleted = client.delete(f"/api/lecture/{lecture['id']}")
    assert deleted.status_code == 200, deleted.text
    rows = _rows(client, course_id)
    leftovers = [r for r in rows if r["node_id"] == node["id"]
                 and (r["kind"] == "diagram" or r["kind"].startswith("chart_"))]
    assert leftovers == [], "删除讲义应连带清理 diagram 与 chart_*"


def test_generate_all_mixes_chart_types_across_lectures(client, course_id, monkeypatch):
    """一键生成整门课：不同讲义按内容自动配不同类型的图（不全是概念图）。"""
    plan = _plan_approve(client, course_id, goal="数据与过程入门", chapters=2)
    nodes = plan["nodes"]
    assert len(nodes) >= 2

    variants = [
        (_cards_mix(), None),
        ([{"anchor": "核心概念", "caption": "核心概念", "chart_type": "concept",
           "layout": "ring",
           "nodes": [{"emoji": "⭐", "title": "定义", "subs": ["边界"]}]},
          {"anchor": "体系分类", "caption": "分类表", "chart_type": "table",
           "data": {"headers": ["类别", "说明"], "rows": [["甲", "说明一"], ["乙", "说明二"]]}},
          {"anchor": "阶段进程", "caption": "阶段", "chart_type": "timeline",
           "data": {"steps": [{"title": "开始", "subs": ["起点"]},
                              {"title": "过程", "subs": ["推进"]},
                              {"title": "收尾", "subs": ["校验"]}]}}], None),
    ]

    import app.services.lecture as lecture_mod

    calls = {"i": 0}

    def _client():
        # 每篇讲义会新建两次客户端：偶数次 = LECTURE，奇数次 = CARDS 规划
        idx = calls["i"]
        calls["i"] += 1
        if idx % 2 == 0:
            return _MixCardsClient(cards=[])
        cards, md = variants[(idx // 2) % len(variants)]
        return _MixCardsClient(cards=cards, md=md)

    monkeypatch.setattr(lecture_mod, "build_llm_client", _client)
    resp = client.post("/api/lecture/generate-all", json={
        "course_id": course_id, "tone": "正式", "ignore_style_anchor": True,
    })
    assert resp.status_code == 200, resp.text
    job = _wait_job(client, resp.json()["job_id"])
    assert job["status"] == "success", job
    result = job["result"]
    assert result["ok"] == len(nodes)
    assert result["images"] == 0
    assert result["diagrams"] >= len(nodes)
    assert result["charts"] >= 2, "整门课应自动产生数据图（不全是概念图）"

    rows = _rows(client, course_id)
    kinds = {r["kind"] for r in rows if r["kind"].startswith("chart_")}
    assert kinds == {"chart_bar", "chart_mindmap", "chart_timeline", "chart_table"}, kinds
    lectures = [r for r in rows if r["kind"] == "lecture"]
    assert len(lectures) == len(nodes)
    for lec in lectures:
        md = str(lec["body"]["markdown"])
        assert "diagram://" in md, "每篇讲义都应保留一图看懂概念图"
        assert "gen://" not in md
        assert (len(lec["meta"].get("chart_ids") or []) > 0
                or "数据卡" in md), "应存在数据卡分流标记"


def _wait_job(client, job_id: str, tries: int = 120) -> dict:
    for _ in range(tries):
        s = client.get(f"/api/content/jobs/{job_id}").json()
        if s["status"] in ("success", "failed"):
            return s
        time.sleep(0.1)
    raise AssertionError(f"job timeout: {job_id}")
