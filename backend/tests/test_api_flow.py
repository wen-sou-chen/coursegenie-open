"""API 端到端核心闭环测试：建课 → 规划 → 内容 → 习题 → 版本 → 分析 → 导出。"""
from __future__ import annotations


def test_full_course_flow(client, course_id):
    # 1. 生成大纲
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "让初中生理解光合作用", "num_chapters": 3, "style": "正式",
    })
    assert resp.status_code == 200, resp.text
    plan = resp.json()
    assert plan["nodes"], "大纲应包含章节节点"
    assert plan["graph"]["nodes"], "知识图谱应有节点"
    first_node_id = plan["nodes"][0]["id"]
    assert client.post("/api/plan/approve", json={"course_id": course_id, "approved": True}).status_code == 200

    # 2. 批量生成内容（BackgroundTasks 在 TestClient 中同步执行）
    resp = client.post("/api/content/generate", json={"course_id": course_id, "tone": "正式"})
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]
    status = client.get(f"/api/content/jobs/{job_id}").json()
    assert status["status"] == "success", status

    items = client.get(f"/api/content/{course_id}").json()
    assert any(i["kind"] == "text" for i in items)
    assert any(i["kind"] == "quiz" for i in items)

    # 3. 单点习题生成
    resp = client.post("/api/quiz/generate", json={
        "course_id": course_id, "node_id": first_node_id, "count": 3, "difficulty": "easy",
    })
    assert resp.status_code == 200, resp.text
    quizzes = resp.json()
    assert len(quizzes) == 3
    assert quizzes[0]["answer"]

    # 4. 版本快照与回滚
    resp = client.post(f"/api/version/{course_id}/snapshot")
    assert resp.status_code == 200, resp.text
    versions = client.get(f"/api/version/{course_id}").json()
    assert len(versions) == 1
    version_id = versions[0]["id"]
    assert client.post(f"/api/version/{course_id}/restore/{version_id}").status_code == 200

    # 5. 分析
    stats = client.get(f"/api/analytics/{course_id}").json()
    assert stats["outline_nodes"] >= 3
    assert stats["quizzes"] >= 3

    # 6. 导出 HTML 课程包
    resp = client.post("/api/publish/export", json={"course_id": course_id, "format": "html"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["file"].endswith(".zip")


def test_flowchart_and_image(client, course_id):
    plan = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "光合作用与能量转换", "num_chapters": 2,
    })
    assert plan.status_code == 200, plan.text
    assert client.post("/api/plan/approve", json={"course_id": course_id, "approved": True}).status_code == 200
    resp = client.post("/api/flowchart/generate", json={
        "course_id": course_id, "description": "光合作用的过程", "kind": "flow",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["body"]["mermaid"].startswith("flowchart")

    resp = client.post("/api/image/generate", json={
        "course_id": course_id, "prompt": "光合作用示意图", "size": "1024x1024",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["body"]["path"].endswith(".png")


def test_publish_teachable_stub(client, course_id):
    resp = client.post("/api/publish/", json={"course_id": course_id, "platform": "teachable", "payload": {}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is False  # 无 API Key 时应给出明确提示

    resp = client.post("/api/publish/", json={"course_id": course_id, "platform": "moodle", "payload": {}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is False
