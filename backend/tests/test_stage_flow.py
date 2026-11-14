"""P1 阶段门禁与章节绑定测试：大纲通过后才能自动生成；图片/流程图可绑定 node_id。"""
from __future__ import annotations


def _plan(client, course_id: str, chapters: int = 2, goal: str = "初中科学入门") -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": goal, "num_chapters": chapters,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


def _approve(client, course_id: str, approved: bool = True) -> dict:
    resp = client.post("/api/plan/approve", json={"course_id": course_id, "approved": approved})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_auto_generation_blocked_until_outline_approved(client, course_id):
    plan = _plan(client, course_id)
    node_id = plan["nodes"][0]["id"]

    blocked = client.post("/api/content/generate", json={"course_id": course_id})
    assert blocked.status_code == 409, blocked.text
    quiz = client.post("/api/quiz/generate", json={
        "course_id": course_id, "node_id": node_id, "count": 1, "difficulty": "easy"})
    assert quiz.status_code == 409
    img = client.post("/api/image/generate", json={
        "course_id": course_id, "prompt": "示意图", "size": "512x512"})
    assert img.status_code == 409
    fc = client.post("/api/flowchart/generate", json={
        "course_id": course_id, "description": "实验步骤", "kind": "flow"})
    assert fc.status_code == 409

    _approve(client, course_id)
    job = client.post("/api/content/generate", json={"course_id": course_id}).json()["job_id"]
    assert client.get(f"/api/content/jobs/{job}").json()["status"] == "success"

    # 已通过时不允许直接重新生成大纲（避免静默推翻已产出内容的依据）
    again = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "改版目标", "num_chapters": 3})
    assert again.status_code == 409


def test_unapprove_allows_regenerate_outline(client, course_id):
    _plan(client, course_id)
    _approve(client, course_id, False)
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "改版大纲", "num_chapters": 3})
    assert resp.status_code == 200, resp.text
    course = client.get(f"/api/course/{course_id}").json()
    assert course["status"] == "planned"


def test_edit_approved_node_unapproves_course(client, course_id):
    plan = _plan(client, course_id)
    _approve(client, course_id)
    node_id = plan["nodes"][0]["id"]
    r = client.patch(f"/api/plan/node/{node_id}", json={"title": "改名后的章节"})
    assert r.status_code == 200, r.text
    course = client.get(f"/api/course/{course_id}").json()
    assert course["status"] == "planned"


def test_image_and_flowchart_bind_to_node(client, course_id):
    plan = _plan(client, course_id)
    _approve(client, course_id)
    node_id = plan["nodes"][0]["id"]

    img = client.post("/api/image/generate", json={
        "course_id": course_id, "prompt": "章节示意图", "size": "512x512", "node_id": node_id,
    })
    assert img.status_code == 200, img.text
    row = next(r for r in client.get(f"/api/content/{course_id}").json() if r["id"] == img.json()["id"])
    assert row["node_id"] == node_id

    fc = client.post("/api/flowchart/generate", json={
        "course_id": course_id, "description": "本章学习流程", "kind": "flow", "node_id": node_id,
    })
    assert fc.status_code == 200, fc.text
    row2 = next(r for r in client.get(f"/api/content/{course_id}").json() if r["id"] == fc.json()["id"])
    assert row2["node_id"] == node_id

    # 跨课程绑定节点 → 400（防串课）
    other = client.post("/api/course/", json={"title": "另一门课", "subject": "数学"}).json()
    other_node = _plan(client, other["id"])["nodes"][0]["id"]
    bad = client.post("/api/image/generate", json={
        "course_id": course_id, "prompt": "示意图", "size": "512x512", "node_id": other_node,
    })
    assert bad.status_code == 400
