"""统一内容操作测试：文本去重替换、重做保留历史、上传导入、就地编辑/删除。"""
from __future__ import annotations


def _plan(client, course_id: str, chapters: int = 2) -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "初中生物光合作用", "num_chapters": chapters,
    })
    assert resp.status_code == 200, resp.text
    approved = client.post("/api/plan/approve", json={"course_id": course_id, "approved": True})
    assert approved.status_code == 200, approved.text
    return resp.json()


def test_text_generation_dedupes_per_node(client, course_id):
    plan = _plan(client, course_id)
    for _ in range(2):
        job = client.post("/api/content/generate", json={"course_id": course_id}).json()["job_id"]
        assert client.get(f"/api/content/jobs/{job}").json()["status"] == "success"

    texts = [i for i in client.get(f"/api/content/{course_id}").json() if i["kind"] == "text"]
    assert texts, "应生成文本"
    # 两次批量生成后仍按知识点去重（node_id 一一对应，不无限堆积）
    assert len({t["node_id"] for t in texts}) == len(texts)
    assert all((t["meta"].get("history") or []) for t in texts), "重跑应保留上一版历史"

    # 就地编辑 Markdown（body 一层合并，不丢其他字段）
    tid = texts[0]["id"]
    r = client.patch(f"/api/content/{tid}", json={"body": {"markdown": "## 改后正文"}})
    assert r.status_code == 200, r.text
    assert r.json()["body"]["markdown"] == "## 改后正文"
    assert r.json()["body"]["summary"] != ""

    # 删除
    assert client.delete(f"/api/content/{tid}").status_code == 200


def test_image_regenerate_and_upload(client, course_id):
    _plan(client, course_id)
    first = client.post("/api/image/generate", json={
        "course_id": course_id, "prompt": "示意图A", "size": "512x512",
    }).json()
    second = client.post("/api/image/generate", json={
        "course_id": course_id, "prompt": "示意图B", "size": "512x512", "item_id": first["id"],
    }).json()
    assert second["id"] == first["id"], "重做应原地替换同一条目"
    assert second["body"]["prompt"] == "示意图B"

    items = [i for i in client.get(f"/api/content/{course_id}").json() if i["kind"] == "image"]
    assert len(items) == 1, "重做不应新增条目"
    assert len(items[0]["meta"].get("history") or []) == 1

    # 上传导入自己的图片
    up = client.post("/api/image/import", data={"course_id": course_id},
                     files={"file": ("my.png", b"\x89PNG\r\n\x1a\n0", "image/png")})
    assert up.status_code == 200, up.text
    assert up.json()["body"]["source"] == "upload"
    assert up.json()["body"]["path"].endswith(".png")


def test_image_generate_without_prompt_uses_outline(client, course_id):
    plan = _plan(client, course_id)
    chapter = next(n for n in plan["nodes"] if "." not in n["key"])
    resp = client.post("/api/image/generate", json={
        "course_id": course_id, "prompt": "", "size": "512x512", "node_id": chapter["id"],
    })
    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert chapter["title"] in row["body"]["prompt"], "自动提示词应包含该章标题"
    assert row["title"].endswith("配图")
    bound = [i for i in client.get(f"/api/content/{course_id}").json() if i["id"] == row["id"]][0]
    assert bound["node_id"] == chapter["id"]


def test_image_generate_without_prompt_course_wide(client, course_id):
    _plan(client, course_id)
    resp = client.post("/api/image/generate", json={
        "course_id": course_id, "prompt": "", "size": "512x512",
    })
    assert resp.status_code == 200, resp.text
    prompt = resp.json()["body"]["prompt"]
    assert prompt.startswith("为课程《") and "覆盖章节" in prompt
    assert "不要出现任何文字" in prompt, "自动配图应默认无文字，规避图片内中文错别字"


def test_flowchart_import_and_regenerate(client, course_id):
    _plan(client, course_id)
    fc = client.post("/api/flowchart/import", json={
        "course_id": course_id, "title": "我画的流程", "mermaid": "flowchart TD\n  A --> B",
    }).json()
    fc2 = client.post("/api/flowchart/import", json={
        "course_id": course_id, "title": "我改的流程", "mermaid": "flowchart LR\n  A --> C",
        "item_id": fc["id"],
    }).json()
    assert fc2["id"] == fc["id"]
    assert "LR" in fc2["body"]["mermaid"]

    g = client.post("/api/flowchart/generate", json={
        "course_id": course_id, "description": "光合作用的过程", "kind": "flow",
        "item_id": fc["id"],
    }).json()
    assert g["id"] == fc["id"]
    flows = [i for i in client.get(f"/api/content/{course_id}").json() if i["kind"] == "flowchart"]
    assert len(flows) == 1


def test_quiz_edit_and_delete(client, course_id):
    plan = _plan(client, course_id)
    node_id = plan["nodes"][0]["id"]
    quizzes = client.post("/api/quiz/generate", json={
        "course_id": course_id, "node_id": node_id, "count": 3, "difficulty": "easy",
    }).json()
    qid = quizzes[0]["id"]

    r = client.patch(f"/api/quiz/{qid}", json={
        "question": "改后问题", "options": ["甲", "乙"], "answer": "1",
    })
    assert r.status_code == 200, r.text
    assert r.json()["question"] == "改后问题"
    assert r.json()["options"] == ["甲", "乙"]

    assert client.delete(f"/api/quiz/{qid}").status_code == 200
    remain = [q for q in client.get(f"/api/content/{course_id}").json() if q["kind"] == "quiz"]
    assert len(remain) == 2


def test_plan_node_edit(client, course_id):
    plan = _plan(client, course_id)
    nid = plan["nodes"][0]["id"]
    r = client.patch(f"/api/plan/node/{nid}", json={"title": "新章节名", "objective": "能复述实验步骤"})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "新章节名"
    assert r.json()["objective"] == "能复述实验步骤"


def test_ppt_upload_import(client, course_id):
    up = client.post("/api/ppt/import", data={"course_id": course_id},
                     files={"file": ("deck.pptx", b"PK\x03\x04fakepptx",
                                     "application/vnd.openxmlformats-officedocument.presentationml.presentation")})
    assert up.status_code == 200, up.text
    assert up.json()["body"]["engine"] == "upload"
    grouped = client.get(f"/api/ppt/{course_id}").json()   # v1：按 整课/各章 分组返回
    rows = grouped["items"]
    assert rows and rows[0]["urls"]["pptx"].startswith("/ppt-files/uploads/")
    assert grouped["course"] and grouped["course"][0]["scope"] == "course"
