"""P1b 新增能力测试：大纲重建语义 / AI 补充素材 / 图片中文字叠加。"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

from app.core.config import get_settings


def _generate(client, course_id: str, *, goal: str = "让初中生理解光合作用",
              audience: str = "初中生", chapters: int = 2) -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id,
        "audience": audience,
        "goal": goal,
        "num_chapters": chapters,
        "style": "正式",
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_plan_regenerate_updates_audience_and_replaces(client, course_id):
    first = _generate(client, course_id, goal="第一版课程目标", audience="初中生", chapters=2)
    second = _generate(client, course_id, goal="第二版课程目标", audience="高中生", chapters=3)
    assert second["outline_id"] != first["outline_id"]
    assert len(second["nodes"]) == 3

    course = client.get(f"/api/course/{course_id}").json()
    assert course["audience"] == "高中生"
    plan = client.get(f"/api/plan/{course_id}").json()
    assert plan["outline_id"] == second["outline_id"]

    # 大纲通过（锁定）后不允许直接重新生成
    assert client.post("/api/plan/approve", json={"course_id": course_id, "approved": True}).status_code == 200
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "第三版", "num_chapters": 2,
    })
    assert resp.status_code == 409


def test_material_ai_generate_edit_delete(client, course_id):
    resp = client.post("/api/material/paste", json={
        "course_id": course_id, "title": "讲义", "text": "光合作用的基础内容",
    })
    assert resp.status_code == 200, resp.text

    resp = client.post("/api/material/generate", json={
        "course_id": course_id,
        "directions": ["concept", "examples"],
        "custom_instruction": "控制篇幅，适合初中课堂",
    })
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 2
    assert all(row["source"] == "ai" for row in rows)

    mid = rows[0]["id"]
    full = client.get(f"/api/material/text/{mid}").json()
    assert full["text"]
    upd = client.patch(f"/api/material/{mid}", json={"title": "概念铺垫·修订"})
    assert upd.status_code == 200
    assert upd.json()["title"] == "概念铺垫·修订"

    bad = client.post("/api/material/generate", json={
        "course_id": course_id, "directions": ["nonsense"],
    })
    assert bad.status_code == 400
    assert client.delete(f"/api/material/{mid}").status_code == 200


def test_image_overlay_with_chinese_text(client, course_id):
    plan = _generate(client, course_id, chapters=1)
    assert client.post("/api/plan/approve", json={"course_id": course_id, "approved": True}).status_code == 200
    node_id = plan["nodes"][0]["id"]

    resp = client.post("/api/image/generate", json={
        "course_id": course_id,
        "prompt": "浅绿色底图上画一片叶子，简洁扁平插画风",
        "size": "512x512",
        "node_id": node_id,
        "text_lang": "zh",
        "overlay_text": "光合作用原理",
        "overlay_pos": "top",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()["body"]
    assert body["text_lang"] == "zh"
    assert body["overlay_text"] == "光合作用原理"
    assert body["path"].endswith(".png")

    img_path = Path(get_settings().storage_dir) / body["path"]
    assert img_path.exists()
    with Image.open(img_path) as im:
        im.verify()
    assert img_path.stat().st_size > 0
