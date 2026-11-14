"""M4-PPT v1 测试：整课/每章课件链路（标题顺序、备注、占位、原地替换、回退、409）。"""
from __future__ import annotations

import time


def _plan_and_approve(client, course_id: str, num_chapters: int = 2) -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "让初中生理解光合作用", "num_chapters": num_chapters,
    })
    assert resp.status_code == 200, resp.text
    assert client.post("/api/plan/approve",
                       json={"course_id": course_id, "approved": True}).status_code == 200
    return client.get(f"/api/plan/{course_id}").json()


def _chapters(client, course_id: str) -> list[dict]:
    nodes = client.get(f"/api/plan/{course_id}").json()["nodes"]
    return [n for n in nodes if n["type"] in ("chapter", "section")] or nodes


def _wait(client, job_id: str, timeout: float = 30.0) -> dict:
    state: dict | None = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = client.get(f"/api/ppt/jobs/{job_id}").json()
        if state["status"] in ("success", "failed"):
            return state
        time.sleep(0.1)
    raise AssertionError(f"任务超时未结束：{state}")


def _gen(client, course_id: str, **payload) -> dict:
    """发一次生成请求 → 等任务结束；成功返回结果，失败返回整份 job。"""
    body = {"course_id": course_id, **payload}
    resp = client.post("/api/ppt/generate", json=body)
    assert resp.status_code == 200, resp.text
    state = _wait(client, resp.json()["job_id"])
    return state


def _decks(client, course_id: str) -> dict:
    resp = client.get(f"/api/ppt/{course_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _slides(pptx_path: str) -> list[dict]:
    """用 python-pptx 重新打开产物，读回每页标题 / 正文 / 备注。"""
    from pptx import Presentation

    prs = Presentation(pptx_path)
    out: list[dict] = []
    for slide in prs.slides:
        title = slide.shapes.title.text if slide.shapes.title is not None else ""
        body = slide.placeholders[1].text_frame.text if len(slide.placeholders) > 1 else ""
        out.append({"title": title, "body": body,
                    "notes": slide.notes_slide.notes_text_frame.text})
    return out


def test_course_deck_page_order_and_titles(client, course_id):
    """① 整课 PPT 页面顺序：封面 → 课程目标 → 目录 → 每章概览 → 末页。"""
    _plan_and_approve(client, course_id, num_chapters=2)
    state = _gen(client, course_id, scope="course")
    assert state["status"] == "success", state
    deck = _decks(client, course_id)["course"][0]
    slides = _slides(deck["body"]["pptx"])
    chapters = [f"{n['key']} {n['title']}" for n in _chapters(client, course_id)]
    assert [s["title"] for s in slides] == [
        "光合作用入门", "课程目标", "课程目录", *chapters, "总结与下一步",
    ]
    assert all(s["title"].strip() for s in slides)


def test_every_slide_has_notes(client, course_id):
    """② 每页备注非空（讲义原文 / 答案解析 / 讲稿提示）。"""
    _plan_and_approve(client, course_id, num_chapters=1)
    assert _gen(client, course_id, scope="course")["status"] == "success"
    node = _chapters(client, course_id)[0]
    assert _gen(client, course_id, scope="chapter",
                chapter_node_id=node["id"])["status"] == "success"
    for deck in (_decks(client, course_id)["items"]):
        for slide in _slides(deck["body"]["pptx"]):
            assert slide["notes"].strip(), f"{deck['title']} 的「{slide['title']}」没有备注"


def test_python_pptx_reopens_and_slide_count_matches(client, course_id):
    """③ python-pptx 能重新打开产物，页数与 meta.slide_count 吻合。"""
    from pptx import Presentation

    _plan_and_approve(client, course_id, num_chapters=2)
    assert _gen(client, course_id, scope="course")["status"] == "success"
    deck = _decks(client, course_id)["course"][0]
    assert deck["body"]["engine"] == "python-pptx"
    prs = Presentation(deck["body"]["pptx"])
    assert len(prs.slides) == deck["meta"]["slide_count"] == len(_slides(deck["body"]["pptx"]))
    assert deck["meta"]["generated_at"]


def test_chapter_deck_scope_and_binding(client, course_id):
    """④ scope / 章节绑定正确；GET 按 整课 / 各章 分组。"""
    _plan_and_approve(client, course_id, num_chapters=2)
    node = _chapters(client, course_id)[0]
    state = _gen(client, course_id, scope="chapter", chapter_node_id=node["id"])
    assert state["status"] == "success", state
    grouped = _decks(client, course_id)
    assert grouped["course"] == []
    assert len(grouped["chapters"]) == 1
    deck = grouped["chapters"][0]
    assert deck["scope"] == "chapter"
    assert deck["meta"]["chapter_node_id"] == node["id"]
    assert deck["meta"]["chapter_key"] == node["key"]
    assert deck["title"].endswith("· 课件")
    slides = _slides(deck["body"]["pptx"])
    assert slides[0]["title"] == f"{node['key']} {node['title']}"
    assert slides[-1]["title"] == "谢谢观看"
    assert any(s["title"] == "随堂练习" for s in slides)
    # 整课课件独立存在，互不覆盖
    assert _gen(client, course_id, scope="course")["status"] == "success"
    grouped = _decks(client, course_id)
    assert len(grouped["course"]) == 1 and len(grouped["chapters"]) == 1


def test_redo_replaces_in_place_and_keeps_history(client, course_id):
    """⑤ 重做 = 原地替换（每课/每章最多 1 个），旧版进 meta.history（≤5）。"""
    _plan_and_approve(client, course_id, num_chapters=1)
    node = _chapters(client, course_id)[0]
    first = _gen(client, course_id, scope="chapter", chapter_node_id=node["id"])
    assert first["status"] == "success"
    deck = _decks(client, course_id)["chapters"][0]
    old_id, old_pptx = deck["id"], deck["body"]["pptx"]

    state = _gen(client, course_id, scope="chapter", chapter_node_id=node["id"])
    assert state["status"] == "success"
    assert state["result"]["content_id"] == old_id          # 原地替换，不新建条目
    decks = _decks(client, course_id)["chapters"]
    assert len(decks) == 1
    history = decks[0]["meta"]["history"]
    assert len(history) == 1 and history[0]["body"]["pptx"] == old_pptx
    assert decks[0]["body"]["pptx"] != old_pptx

    # 整课同理：连续两次只留 1 个
    assert _gen(client, course_id, scope="course")["status"] == "success"
    assert _gen(client, course_id, scope="course")["status"] == "success"
    assert len(_decks(client, course_id)["course"]) == 1


def test_quiz_answers_go_to_notes(client, course_id):
    """每章 PPT 的随堂练习：题干+选项在正文，答案与解析只在备注。"""
    _plan_and_approve(client, course_id, num_chapters=1)
    node = _chapters(client, course_id)[0]
    resp = client.post("/api/quiz/generate", json={
        "course_id": course_id, "node_id": node["id"], "count": 2,
    })
    assert resp.status_code == 200, resp.text
    assert _gen(client, course_id, scope="chapter",
                chapter_node_id=node["id"])["status"] == "success"
    deck = _decks(client, course_id)["chapters"][0]
    quiz = next(s for s in _slides(deck["body"]["pptx"]) if s["title"] == "随堂练习")
    assert "答案" in quiz["notes"]
    assert "解析" in quiz["notes"]
    assert "答案" not in quiz["body"]


def test_lecture_points_go_to_slides_and_paragraphs_to_notes(client, course_id, gen_lecture_row):
    """知识点页取讲义「标题 + 要点」，长段落只进备注（去长段落）。"""
    _plan_and_approve(client, course_id, num_chapters=1)
    node = _chapters(client, course_id)[0]
    gen_lecture_row(course_id, node["id"])
    assert _gen(client, course_id, scope="chapter",
                chapter_node_id=node["id"])["status"] == "success"
    deck = _decks(client, course_id)["chapters"][0]
    slides = _slides(deck["body"]["pptx"])
    point = slides[1]
    assert point["title"] == node["title"]
    assert "【概念引入】" in point["body"] or "定义" in point["body"]
    assert "先从贴近生活的现象切入" in point["notes"]
    assert deck["meta"]["source"]["lecture_ids"]


def test_engine_failure_falls_back_and_records_error(client, course_id, monkeypatch):
    """⑥ 显式选择 dashi 但失败：回退 python-pptx、任务仍成功、原因记进 meta.engine_error。"""
    from app.services import ppt as ppt_service

    _plan_and_approve(client, course_id, num_chapters=1)
    monkeypatch.setattr(ppt_service, "_dashi_project", lambda: "C:/fake/dashi-ppt")
    monkeypatch.setattr(ppt_service, "_generate_with_dashi",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("npm 不可用")))
    state = _gen(client, course_id, scope="course", engine="dashi")
    assert state["status"] == "success", state
    assert state["result"]["engine"] == "python-pptx"
    deck = _decks(client, course_id)["course"][0]
    assert deck["body"]["engine"] == "python-pptx"
    assert "dashi-ppt" in deck["meta"]["engine_error"]
    assert "npm 不可用" in deck["meta"]["engine_error"]
    assert len(_slides(deck["body"]["pptx"])) == deck["meta"]["slide_count"]


def test_generate_requires_approved_outline(client, course_id):
    """⑦ 未通过大纲 → 409；每章课件缺 chapter_node_id → 400。"""
    resp = client.post("/api/ppt/generate", json={"course_id": course_id, "scope": "course"})
    assert resp.status_code == 409, resp.text
    _plan_and_approve(client, course_id, num_chapters=1)
    resp = client.post("/api/ppt/generate", json={"course_id": course_id, "scope": "chapter"})
    assert resp.status_code == 400, resp.text


def test_invalid_chapter_binding_reports_failure(client, course_id):
    """章节不归属该课程：任务失败并显式上报原因（不静默成功）。"""
    _plan_and_approve(client, course_id, num_chapters=1)
    state = _gen(client, course_id, scope="chapter", chapter_node_id="not-a-node")
    assert state["status"] == "failed"
    assert "章节不存在" in (state["error"] or "")
    assert _decks(client, course_id)["items"] == []
