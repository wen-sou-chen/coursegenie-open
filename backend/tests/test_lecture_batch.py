# -*- coding: utf-8 -*-
"""课程级「一键生成整门课完整讲义」测试：门禁、批量配图、概念图自动带图。"""
from __future__ import annotations

import time


def _plan_and_approve(client, course_id: str) -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "初中生物光合作用", "num_chapters": 2,
    })
    assert resp.status_code == 200, resp.text
    assert client.post("/api/plan/approve", json={
        "course_id": course_id, "approved": True}).status_code == 200
    return resp.json()


def _wait_job(client, job_id: str, tries: int = 80) -> dict:
    for _ in range(tries):
        s = client.get(f"/api/content/jobs/{job_id}").json()
        if s["status"] in ("success", "failed"):
            return s
        time.sleep(0.1)
    raise AssertionError(f"job timeout: {job_id}")


def test_generate_all_blocked_until_outline_approved(client, course_id):
    resp = client.post("/api/lecture/generate-all", json={
        "course_id": course_id, "tone": "正式",
    })
    assert resp.status_code == 409, resp.text


def test_generate_all_creates_lectures_with_diagrams(client, course_id):
    plan = _plan_and_approve(client, course_id)
    # M3：未设样板锚时必须显式选择「忽略样板，一次性生成整门课」
    resp = client.post("/api/lecture/generate-all", json={
        "course_id": course_id, "tone": "正式", "ignore_style_anchor": True,
    })
    assert resp.status_code == 200, resp.text
    job = _wait_job(client, resp.json()["job_id"])
    assert job["status"] == "success", job
    assert job["result"]["ok"] == len(plan["nodes"])
    assert job["result"]["images"] == 0, "默认零 AI 位图，批量讲义不应生成 PNG"
    assert job["result"]["diagrams"] >= len(plan["nodes"]), "每篇讲义应自动配一张 Excalidraw 概念图"

    rows = client.get(f"/api/content/{course_id}").json()
    node_ids = {n["id"] for n in plan["nodes"]}
    lectures = [r for r in rows if r["kind"] == "lecture"]
    assert len(lectures) == len(plan["nodes"])
    for lec in lectures:
        assert lec["node_id"] in node_ids, "每个大纲节点都应生成讲义"
        md = str(lec["body"]["markdown"])
        assert "/images/" not in md, "讲义默认零 AI 位图，不应含 PNG 配图引用"
        assert "diagram://" in md, "讲义 markdown 应含一图看懂概念图引用"
        assert "gen://" not in md, "占位符应全部替换"

    images = [r for r in rows if r["kind"] == "image"]
    assert len(images) == 0, "批量讲义不应产生 AI 位图条目"


def test_lecture_diagram_when_llm_omits_image_placeholder(client, course_id, monkeypatch, gen_lecture_row):
    """LLM 未写 gen:// 时不再自动兜底 AI 位图，改为自动配一张 Excalidraw 概念图。"""
    plan = _plan_and_approve(client, course_id)
    node = plan["nodes"][0]

    class _NoPlaceholderClient:
        name = "no-placeholder"

        def complete_json(self, system: str, user: str, *, max_tokens: int = 2000) -> dict:
            return {
                "markdown": f"## {node['title']}\n\n纯文字讲义，没有任何配图占位符。\n\n## 小结\n要点已讲完。",
                "summary": "摘要",
                "keywords": [],
                "quizzes": [],
            }

    import app.services.lecture as lecture_mod

    monkeypatch.setattr(lecture_mod, "build_llm_client", lambda: _NoPlaceholderClient())

    row = gen_lecture_row(course_id, node["id"])
    md = str(row["body"]["markdown"])
    assert "gen://" not in md
    assert "/images/" not in md, "LLM 未写 gen:// 时不应再自动兜底 AI 位图"
    assert "![一图看懂](diagram://" in md, "讲义应自动配一张 Excalidraw 概念图"

    rows = client.get(f"/api/content/{course_id}").json()
    images = [r for r in rows if r["kind"] == "image"]
    assert len(images) == 0, "不应再有 AI 位图兜底条目"
    diagrams = [r for r in rows if r["kind"] == "diagram"]
    # 任务 125：图形上限（>6）也生效 ⇒ 一图看懂按容量拆成 2 张
    assert len(diagrams) == 2
    assert diagrams[0]["meta"].get("from_lecture") == row["id"]
    assert diagrams[0]["node_id"] == node["id"]
