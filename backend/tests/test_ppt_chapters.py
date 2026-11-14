# -*- coding: utf-8 -*-
"""任务 26：章节课件一键补齐 + 章节编号口径统一 + node 绑定（老数据兜底）。

覆盖：
1. 「补齐全部章节课件」后 chapters 数量 = 大纲章数，每份都有 chapter_key 与 node_id；
2. 只补缺：第二次调用不重复生成（generated 为空、skipped 列全、DB 条目数不变）；
3. redo=true 时全部重做，且仍是「每章 1 条」（原地替换，不新增条目）；
4. 课件 DB title 与展示标签同一口径（形如「第1章 …」），整课课件名不受影响；
5. 老数据（node_id=NULL、只有 meta.chapter_key）读取时按章号兜底，重做不生成第二条。
"""
from __future__ import annotations

import time

from app.db.session import SessionLocal
from app.models.course import ContentItem, OutlineNode
from app.services.deck_label import chapter_label, node_label


class _Stub:
    """把 /api/plan 的节点字典包成 deck_label.chapter_label 能吃的对象。"""

    def __init__(self, node: dict) -> None:
        self.node_key = str(node["key"])
        self.title = str(node["title"])
        self.node_type = str(node["type"])


def _plan_and_approve(client, course_id: str, num_chapters: int = 3) -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "让初中生理解光合作用", "num_chapters": num_chapters,
    })
    assert resp.status_code == 200, resp.text
    assert client.post("/api/plan/approve",
                       json={"course_id": course_id, "approved": True}).status_code == 200
    return client.get(f"/api/plan/{course_id}").json()


def _chapters(client, course_id: str) -> list[dict]:
    nodes = client.get(f"/api/plan/{course_id}").json()["nodes"]
    return [n for n in nodes if n["type"] in ("chapter", "section")]


def _wait(client, job_id: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    state: dict | None = None
    while time.time() < deadline:
        state = client.get(f"/api/ppt/jobs/{job_id}").json()
        if state["status"] in ("success", "failed"):
            return state
        time.sleep(0.1)
    raise AssertionError(f"任务超时未结束：{state}")


def _fill_all(client, course_id: str, **payload) -> dict:
    resp = client.post("/api/ppt/generate-chapters", json={"course_id": course_id, **payload})
    assert resp.status_code == 200, resp.text
    return _wait(client, resp.json()["job_id"])


def _deck_rows(course_id: str) -> list[ContentItem]:
    db = SessionLocal()
    try:
        return list(db.query(ContentItem).filter(
            ContentItem.course_id == course_id, ContentItem.kind == "ppt").all())
    finally:
        db.close()


def _gen_course_deck(client, course_id: str) -> dict:
    resp = client.post("/api/ppt/generate", json={
        "course_id": course_id, "scope": "course", "engine": "python-pptx",
    })
    assert resp.status_code == 200, resp.text
    state = _wait(client, resp.json()["job_id"])
    assert state["status"] == "success", state
    return state


def test_fill_all_chapters_covers_outline_and_binds_nodes(client, course_id):
    """① 各章齐全：chapters 数 = 大纲章数，每份都有 chapter_key 与 node_id（DB 也写上了）。"""
    _plan_and_approve(client, course_id, num_chapters=3)
    chapters = _chapters(client, course_id)
    assert len(chapters) == 3, chapters
    _gen_course_deck(client, course_id)

    state = _fill_all(client, course_id)
    assert state["status"] == "success", state
    result = state["result"]
    assert result["total"] == 3
    assert len(result["generated"]) == 3, result
    assert not result["failed"], result["failed"]
    assert {g["chapter_key"] for g in result["generated"]} == {c["key"] for c in chapters}

    listed = client.get(f"/api/ppt/{course_id}").json()
    assert len(listed["chapters"]) == len(chapters), listed["chapters"]
    assert listed["course"], "整课课件应独立存在"
    for deck in listed["chapters"]:
        assert deck["chapter_key"], deck
        assert deck["node_id"], deck
        assert deck["chapter_node_id"] == deck["node_id"]
        assert deck["scope"] == "chapter"

    db = SessionLocal()
    try:
        rows = db.query(ContentItem).filter(
            ContentItem.course_id == course_id, ContentItem.kind == "ppt").all()
        chapter_rows = [r for r in rows if (r.meta or {}).get("scope") == "chapter"]
        assert len(chapter_rows) == 3
        assert all(r.node_id for r in chapter_rows), [r.id for r in chapter_rows]
        for row in chapter_rows:
            node = db.get(OutlineNode, row.node_id)
            assert node is not None
            assert node.node_key == (row.meta or {}).get("chapter_key")
    finally:
        db.close()


def test_fill_all_skips_existing_and_redo_replaces_in_place(client, course_id):
    """② 只补缺：第二次调用全部 skipped、不重复生成；③ redo 原地替换不新增条目。"""
    _plan_and_approve(client, course_id, num_chapters=3)
    chapters = _chapters(client, course_id)

    first = _fill_all(client, course_id)["result"]
    assert len(first["generated"]) == 3
    before = _deck_rows(course_id)
    assert len([r for r in before if (r.meta or {}).get("scope") == "chapter"]) == 3

    second = _fill_all(client, course_id)["result"]
    assert not second["generated"], second
    assert len(second["skipped"]) == len(chapters), second
    assert {s["chapter_key"] for s in second["skipped"]} == {c["key"] for c in chapters}
    assert len(_deck_rows(course_id)) == len(before), "只补缺不应新增条目"

    third = _fill_all(client, course_id, redo=True)["result"]
    assert len(third["generated"]) == len(chapters), third
    assert not third["skipped"], third
    after = _deck_rows(course_id)
    assert len(after) == len(before), "重做必须原地替换（同 scope 复用同一条目）"
    assert {r.id for r in after} == {r.id for r in before}


def test_deck_titles_use_same_label_as_ui(client, course_id):
    """④ 课件 title 与展示标签同一口径（形如「第1章 …」），整课课件名不受影响。"""
    _plan_and_approve(client, course_id, num_chapters=2)
    chapters = _chapters(client, course_id)
    _gen_course_deck(client, course_id)
    _fill_all(client, course_id)

    listed = client.get(f"/api/ppt/{course_id}").json()
    for deck in listed["chapters"]:
        node = next(c for c in chapters if c["key"] == deck["chapter_key"])
        assert deck["title"] == f"{chapter_label(_Stub(node))} · 课件", deck["title"]
        assert deck["title"].startswith(f"第{node['key']}章 "), deck["title"]
        assert deck["label"] == node_label(node["key"], node["title"], node["type"])
        assert "第2章 第2章" not in deck["title"]
    assert listed["course"][0]["title"].endswith("· 整课课件")


def test_legacy_chapter_deck_without_node_id_reads_and_redoes_in_place(client, course_id):
    """⑤ 老数据（node_id=NULL，只靠 meta.chapter_key）读取时兜底，重做不产生第二条。"""
    _plan_and_approve(client, course_id, num_chapters=2)
    chapters = _chapters(client, course_id)
    target = chapters[0]

    db = SessionLocal()
    try:
        legacy = ContentItem(
            course_id=course_id, node_id=None, kind="ppt",
            title=f"{target['key']} {target['title']} · 课件",
            body={"pptx": "", "engine": "python-pptx", "source": "generate"},
            meta={"scope": "chapter", "chapter_node_id": None, "chapter_key": target["key"],
                  "slide_count": 7, "generated_at": "2026-09-14T00:00:00+00:00"},
        )
        db.add(legacy)
        db.commit()
        db.refresh(legacy)
        legacy_id = legacy.id
    finally:
        db.close()

    listed = client.get(f"/api/ppt/{course_id}").json()
    deck = listed["chapters"][0]
    assert deck["id"] == legacy_id
    assert deck["node_id"] == target["id"], "读取时按 meta.chapter_key 兜底成大纲节点 id"

    result = _fill_all(client, course_id)["result"]
    assert [s["chapter_key"] for s in result["skipped"]] == [target["key"]], result
    assert len(result["generated"]) == 1, result

    resp = client.post("/api/ppt/generate", json={
        "course_id": course_id, "scope": "chapter", "chapter_node_id": target["id"],
        "engine": "python-pptx",
    })
    assert resp.status_code == 200, resp.text
    assert _wait(client, resp.json()["job_id"])["status"] == "success"

    rows = [r for r in _deck_rows(course_id) if (r.meta or {}).get("scope") == "chapter"]
    assert len(rows) == 2, [r.title for r in rows]
    redone = next(r for r in rows if r.id == legacy_id)
    assert redone.node_id == target["id"], "重做后应写上 node_id"
    assert redone.title.startswith(f"第{target['key']}章 "), redone.title
