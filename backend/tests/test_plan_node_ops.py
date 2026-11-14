# -*- coding: utf-8 -*-
"""P3-3 回归测试：大纲节点增 / 删 / 移（node_key 自动重排且不破坏绑定）。

背景：任务 25 真实素材实战里老师的动作是「素材里漏了一条 → 我想加一节」，
当时只有「改节点字段」与「整份重新生成」（还会给出完全不同的大纲）。
本组用例覆盖：键连续、绑定不破（node_id 不动、meta.node_key 同步）、
删除/移动的二次确认、以及重排后导出仍与现算一致。
"""
from __future__ import annotations

from app.db.session import SessionLocal
from app.models.course import ContentItem, OutlineNode
from app.services.planner import plan_keys


class _Row:
    """plan_keys 只需要 node_key / order_index 两个字段。"""

    def __init__(self, key: str, order: int) -> None:
        self.node_key = key
        self.order_index = order


def _plan(client, course_id: str, chapters: int = 3) -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "掌握核心概念",
        "num_chapters": chapters, "style": "正式"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _nodes(client, course_id: str) -> list[dict]:
    resp = client.get(f"/api/plan/{course_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()["nodes"]


def _approve(client, course_id: str) -> None:
    resp = client.post("/api/plan/approve", json={"course_id": course_id, "approved": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"


def _keys(nodes: list[dict]) -> list[str]:
    return [n["key"] for n in nodes]


def _by_key(nodes: list[dict], key: str) -> dict:
    return next(n for n in nodes if n["key"] == key)


def _assert_contiguous(nodes: list[dict]) -> None:
    """章 1/2/3…；第 N 章的知识点必须是 N.1/N.2…（不跳号、不重号）。"""
    chapter_no = 0
    point_no = 0
    for node in nodes:
        key = node["key"]
        if "." not in key:
            chapter_no += 1
            point_no = 0
            assert key == str(chapter_no), (key, _keys(nodes))
        else:
            point_no += 1
            assert key == f"{chapter_no}.{point_no}", (key, _keys(nodes))


def test_plan_keys_renumbers_contiguously():
    """纯计算口径：删掉中间一节后 2.3 补位成 2.2；新章插到最前面时知识点整块后移。"""
    rows = [_Row("1", 0), _Row("1.1", 1), _Row("1.3", 2), _Row("2", 3), _Row("2.1", 4)]
    assert [key for _row, key in plan_keys(rows)] == ["1", "1.1", "1.2", "2", "2.1"]
    rows.insert(0, _Row("", -1))
    assert [key for _row, key in plan_keys(rows)] == ["1", "2", "2.1", "2.2", "3", "3.1"]


def test_add_node_keeps_keys_contiguous(client, course_id):
    _plan(client, course_id, 3)
    nodes = _nodes(client, course_id)
    chapter_one = _by_key(nodes, "1")
    assert _keys(nodes) == ["1", "2", "3"]

    added = client.post("/api/plan/node", json={
        "course_id": course_id, "parent_id": chapter_one["id"], "title": "方法一：反思校对"})
    assert added.status_code == 200, added.text
    assert added.json()["node"]["key"] == "1.1"
    _assert_contiguous(_nodes(client, course_id))

    # 插到第 1 位：原来的 1.1 让位成 1.2
    inserted = client.post("/api/plan/node", json={
        "course_id": course_id, "parent_id": chapter_one["id"], "position": 1,
        "title": "方法零：先看日志"})
    assert inserted.status_code == 200, inserted.text
    assert inserted.json()["renumbered"] == {"1.1": "1.2"}
    nodes = _nodes(client, course_id)
    assert _keys(nodes) == ["1", "1.1", "1.2", "2", "3"]
    assert _by_key(nodes, "1.1")["title"] == "方法零：先看日志"
    assert _by_key(nodes, "1.2")["title"] == "方法一：反思校对"

    # 顶层插一整章到最前面：它的知识点跟着整块后移
    chapter = client.post("/api/plan/node", json={
        "course_id": course_id, "position": 1, "title": "第零章 先导"})
    assert chapter.status_code == 200, chapter.text
    nodes = _nodes(client, course_id)
    assert _keys(nodes) == ["1", "2", "2.1", "2.2", "3", "4"], _keys(nodes)
    assert _by_key(nodes, "1")["title"] == "第零章 先导"
    assert _by_key(nodes, "2.1")["title"] == "方法零：先看日志"
    _assert_contiguous(nodes)


def test_add_node_rejects_bad_parent(client, course_id):
    _plan(client, course_id, 2)
    point = client.post("/api/plan/node", json={
        "course_id": course_id, "parent_id": _by_key(_nodes(client, course_id), "1")["id"],
        "title": "知识点"}).json()["node"]
    nested = client.post("/api/plan/node", json={
        "course_id": course_id, "parent_id": point["id"], "title": "孙节点"})
    assert nested.status_code == 400, nested.text
    assert "章节点" in nested.json()["detail"]


def test_added_node_can_generate_lecture(client, course_id, gen_lecture_row):
    _plan(client, course_id, 2)
    chapter = _by_key(_nodes(client, course_id), "1")
    node = client.post("/api/plan/node", json={
        "course_id": course_id, "parent_id": chapter["id"], "title": "新增知识点"}).json()["node"]
    _approve(client, course_id)
    item = gen_lecture_row(course_id, node["id"])
    assert item["meta"]["node_key"] == "1.1"


def test_move_node_renumbers_and_keeps_content_bound(client, course_id, gen_lecture_row):
    _plan(client, course_id, 2)
    chapter_one = _by_key(_nodes(client, course_id), "1")
    chapter_two = _by_key(_nodes(client, course_id), "2")
    for title in ("方法一", "方法二"):
        client.post("/api/plan/node", json={
            "course_id": course_id, "parent_id": chapter_one["id"], "title": title})
    _approve(client, course_id)
    nodes = _nodes(client, course_id)
    target = _by_key(nodes, "1.2")
    item = gen_lecture_row(course_id, target["id"])

    blocked = client.post(f"/api/plan/node/{target['id']}/move", json={"direction": "up"})
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["impact"]["key_change_count"] == 2

    moved = client.post(f"/api/plan/node/{target['id']}/move",
                        json={"direction": "up", "confirm": True})
    assert moved.status_code == 200, moved.text
    assert moved.json()["node"]["key"] == "1.1"
    nodes = _nodes(client, course_id)
    assert _keys(nodes) == ["1", "1.1", "1.2", "2"]
    assert _by_key(nodes, "1.2")["title"] == "方法一"
    _assert_contiguous(nodes)

    db = SessionLocal()
    try:
        row = db.get(ContentItem, item["id"])
        assert row.node_id == target["id"], "绑定点必须还是同一个节点 id"
        assert row.meta["node_key"] == "1.1", row.meta
        assert db.get(OutlineNode, target["id"]).node_key == "1.1"
    finally:
        db.close()

    crossed = client.post(f"/api/plan/node/{target['id']}/move", json={
        "reparent": True, "to_parent_id": chapter_two["id"], "position": 1, "confirm": True})
    assert crossed.status_code == 200, crossed.text
    nodes = _nodes(client, course_id)
    assert _keys(nodes) == ["1", "1.1", "2", "2.1"], _keys(nodes)
    assert _by_key(nodes, "2.1")["title"] == "方法二"
    assert _by_key(nodes, "2.1")["type"] == "point"
    _assert_contiguous(nodes)
    db = SessionLocal()
    try:
        assert db.get(ContentItem, item["id"]).node_id == target["id"]
    finally:
        db.close()


def test_move_chapter_with_children_is_blocked(client, course_id):
    _plan(client, course_id, 2)
    chapter_one = _by_key(_nodes(client, course_id), "1")
    client.post("/api/plan/node", json={
        "course_id": course_id, "parent_id": chapter_one["id"], "title": "方法一"})
    resp = client.post(f"/api/plan/node/{chapter_one['id']}/move",
                       json={"reparent": True, "position": 1, "confirm": True})
    assert resp.status_code == 409, resp.text
    assert "知识点" in resp.json()["detail"]["message"]


def test_delete_node_needs_confirmation_and_renumbers(client, course_id, gen_lecture_row):
    _plan(client, course_id, 2)
    chapter_one = _by_key(_nodes(client, course_id), "1")
    for title in ("方法一", "方法二", "方法三"):
        client.post("/api/plan/node", json={
            "course_id": course_id, "parent_id": chapter_one["id"], "title": title})
    nodes = _nodes(client, course_id)
    _approve(client, course_id)
    first = _by_key(nodes, "1.1")
    third = _by_key(nodes, "1.3")
    item = gen_lecture_row(course_id, first["id"])

    blocked = client.delete(f"/api/plan/node/{first['id']}")
    assert blocked.status_code == 409, blocked.text
    impact = blocked.json()["detail"]["impact"]
    # 一次讲义生成会连带产出配图/概念卡等条目，全部挂在同一个节点上
    assert impact["content_total"] >= 1 and impact["content_counts"]["lecture"] == 1, impact

    # 没有绑内容的节点：直接删（不需要 cascade）
    plain = client.delete(f"/api/plan/node/{third['id']}")
    assert plain.status_code == 200, plain.text
    assert _keys(_nodes(client, course_id)) == ["1", "1.1", "1.2", "2"]

    confirmed = client.delete(f"/api/plan/node/{first['id']}?cascade=true")
    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert body["removed_content"] == impact["content_total"]
    assert body["renumbered"] == {"1.2": "1.1"}
    nodes = _nodes(client, course_id)
    assert _keys(nodes) == ["1", "1.1", "2"], _keys(nodes)
    assert _by_key(nodes, "1.1")["title"] == "方法二"
    _assert_contiguous(nodes)

    db = SessionLocal()
    try:
        assert db.get(ContentItem, item["id"]) is None
        assert db.get(OutlineNode, first["id"]) is None
    finally:
        db.close()


def test_deleted_node_notes_are_reported(client, course_id, gen_lecture_row):
    """删除前的影响面要能报出笔记库里的文件（不静默丢文件）。"""
    _plan(client, course_id, 2)
    chapter_one = _by_key(_nodes(client, course_id), "1")
    node = client.post("/api/plan/node", json={
        "course_id": course_id, "parent_id": chapter_one["id"], "title": "方法一"}).json()["node"]
    _approve(client, course_id)
    gen_lecture_row(course_id, node["id"])
    assert client.post(f"/api/kb/course/{course_id}/export").status_code == 200

    impact = client.get(f"/api/plan/node/{node['id']}/impact")
    assert impact.status_code == 200, impact.text
    body = impact.json()
    assert body["content_total"] >= 1 and body["content_counts"]["lecture"] == 1, body
    assert body["notes"]["bound"] is True
    assert any("1.1-" in rel for rel in body["notes"]["files"]), body["notes"]


def test_renumbered_outline_still_exports_consistent(client, course_id, gen_lecture_row):
    """重排编号后导出，笔记与网页端必须仍然逐项一致（不产生坏链/陈旧文件）。"""
    _plan(client, course_id, 2)
    chapter_one = _by_key(_nodes(client, course_id), "1")
    for title in ("方法一", "方法二"):
        client.post("/api/plan/node", json={
            "course_id": course_id, "parent_id": chapter_one["id"], "title": title})
    _approve(client, course_id)
    nodes = _nodes(client, course_id)
    gen_lecture_row(course_id, _by_key(nodes, "1.1")["id"])
    gen_lecture_row(course_id, _by_key(nodes, "1.2")["id"])
    assert client.post(f"/api/kb/course/{course_id}/export").status_code == 200

    target = _by_key(nodes, "1.2")
    assert client.post(f"/api/plan/node/{target['id']}/move",
                       json={"direction": "up", "confirm": True}).status_code == 200
    assert client.post(f"/api/kb/course/{course_id}/export").status_code == 200

    report = client.post(f"/api/kb/course/{course_id}/verify").json()
    assert report["ok"] is True, report
    assert report["checked"] > 0
    assert report["inconsistent"] == 0, [r for r in report["nodes"]
                                         if r["status"] != "consistent"]