# -*- coding: utf-8 -*-
"""任务 91 回归测试：单篇「重跑这一节」看锁、覆盖即摘徽标。

现象：`POST /api/lecture/generate` 通篇没有 `_locked_of` 检查，已「审核通过」的
一节照样被覆盖；更麻烦的是覆盖后 meta 是合并写法 ⇒ `locked` / `locked_reason`
被原样保留 ⇒ 界面继续显示「审核通过」而正文已经是没人看过的那一版。
修法：单篇在目标已锁时默认 409（除非显式 `allow_overwrite_locked=True`）；
任何一次正文被重新生成都会清掉 `locked` / `locked_reason`（让徽标只贴真被审过的那一版）。
批量路径的 `skip_locked=True` 语义不动（仍然跳过、不摘徽标）。
"""
from __future__ import annotations

import hashlib
import time

from app.db.session import SessionLocal
from app.models.course import ContentItem


def _plan_and_approve(client, course_id: str) -> None:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "初中生物光合作用", "num_chapters": 2})
    assert resp.status_code == 200, resp.text
    assert client.post("/api/plan/approve", json={
        "course_id": course_id, "approved": True}).status_code == 200


def _nodes(client, course_id: str) -> list[dict]:
    return client.get(f"/api/plan/{course_id}").json()["nodes"]


def _wait_job(client, job_id: str, tries: int = 120) -> dict:
    for _ in range(tries):
        state = client.get(f"/api/content/jobs/{job_id}").json()
        if state["status"] in ("success", "failed"):
            return state
        time.sleep(0.1)
    raise AssertionError(f"job timeout: {job_id}")


def _lecture_of(client, course_id: str, node_id: str) -> dict:
    rows = client.get(f"/api/content/{course_id}").json()
    return next(r for r in rows if r["kind"] == "lecture" and r["node_id"] == node_id)


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _reviewed_lecture(client, course_id: str, gen_lecture_row, markdown: str):
    """造一篇「老师改过并审核通过」的讲义（模拟真库包子课 1.1 的状态）。

    刷新出的 mock 正文与手改的正文必定不同 ⇒ “是否被覆盖”有一个可比的 sha1。
    """
    _plan_and_approve(client, course_id)
    node = _nodes(client, course_id)[0]
    item = gen_lecture_row(course_id, node["id"])
    resp = client.patch(f"/api/lecture/{item['id']}", json={"markdown": markdown})
    assert resp.status_code == 200, resp.text
    locked = client.post(f"/api/lecture/{item['id']}/lock", json={
        "locked": True, "reason": "审核通过"})
    assert locked.status_code == 200, locked.text
    assert locked.json()["locked"] is True
    row = _lecture_of(client, course_id, node["id"])
    assert row["body"]["markdown"] == markdown
    assert row["meta"]["locked_reason"] == "审核通过"
    return node, row


def test_single_rerun_of_locked_lecture_is_blocked(client, course_id, gen_lecture_row):
    """已锁定的单篇：默认 409，正文逐字不动、锁与历史都不动。"""
    node, before = _reviewed_lecture(client, course_id, gen_lecture_row,
                                     "## 只有这一句是老师审过的版本\n")
    blocked = client.post("/api/lecture/generate", json={
        "course_id": course_id, "node_id": node["id"], "tone": "正式",
        "item_id": before["id"]})
    assert blocked.status_code == 409, blocked.text
    detail = blocked.json()["detail"]
    assert "已锁定" in detail and "解锁" in detail and "审核通过" in detail, detail

    after = _lecture_of(client, course_id, node["id"])
    assert _sha1(after["body"]["markdown"]) == _sha1(before["body"]["markdown"]), "被拦截时正文不该动"
    assert after["meta"]["locked"] is True
    assert after["meta"]["locked_reason"] == "审核通过"
    assert len((after["meta"] or {}).get("history") or []) == 0


def test_single_rerun_without_item_id_is_also_blocked(client, course_id, gen_lecture_row):
    """不带 item_id 也一样：按节点找到的就是那篇已锁定的讲义。"""
    node, before = _reviewed_lecture(client, course_id, gen_lecture_row,
                                     "## 老师审过的版本（不带 item_id）\n")
    blocked = client.post("/api/lecture/generate", json={
        "course_id": course_id, "node_id": node["id"], "tone": "正式"})
    assert blocked.status_code == 409, blocked.text
    after = _lecture_of(client, course_id, node["id"])
    assert _sha1(after["body"]["markdown"]) == _sha1(before["body"]["markdown"])


def test_explicit_overwrite_replaces_body_and_clears_lock(client, course_id, gen_lecture_row):
    """确认覆盖：正文换成新版 ⇒ `locked` 摘掉、`locked_reason` 清空，并在结果里说清。"""
    node, before = _reviewed_lecture(client, course_id, gen_lecture_row,
                                     "## 老师审过的版本（即将被覆盖）\n")
    resp = client.post("/api/lecture/generate", json={
        "course_id": course_id, "node_id": node["id"], "tone": "正式",
        "item_id": before["id"], "allow_overwrite_locked": True})
    assert resp.status_code == 200, resp.text
    state = _wait_job(client, resp.json()["job_id"])
    assert state["status"] == "success", state
    assert state["result"]["unlocked"] is True
    assert "已摘除" in state["result"]["unlock_message"]

    after = state["result"]["item"]
    assert _sha1(after["body"]["markdown"]) != _sha1(before["body"]["markdown"]), "正文应已被新版覆盖"
    assert after["meta"]["locked"] is False
    assert "locked_reason" not in after["meta"]
    assert (after["meta"].get("history") or [])[0]["body"]["markdown"] == before["body"]["markdown"]

    row = _lecture_of(client, course_id, node["id"])
    assert row["meta"]["locked"] is False and "locked_reason" not in row["meta"]
    assert _sha1(row["body"]["markdown"]) == _sha1(after["body"]["markdown"])


def test_batch_still_skips_locked_and_keeps_the_badge(client, course_id, gen_lecture_row):
    """批量路径（含 force=true）照旧跳过已锁节点，且**不摘徽标**（语义不动）。"""
    node, before = _reviewed_lecture(client, course_id, gen_lecture_row,
                                     "## 老师审过的版本（批量不许碰）\n")
    resp = client.post("/api/lecture/generate-all", json={
        "course_id": course_id, "tone": "正式", "force": True,
        "ignore_style_anchor": True})
    assert resp.status_code == 200, resp.text
    state = _wait_job(client, resp.json()["job_id"])
    assert state["status"] == "success", state
    assert node["key"] in [r["node_key"] for r in state["result"]["locked_skipped"]], state["result"]

    after = _lecture_of(client, course_id, node["id"])
    assert _sha1(after["body"]["markdown"]) == _sha1(before["body"]["markdown"])
    assert after["meta"]["locked"] is True
    assert after["meta"]["locked_reason"] == "审核通过"


def test_unlocked_single_rerun_still_overwrites(client, course_id, gen_lecture_row):
    """不回归：没锁的单篇照旧能重跑（不需要任何新开关）。"""
    _plan_and_approve(client, course_id)
    node = _nodes(client, course_id)[0]
    item = gen_lecture_row(course_id, node["id"])
    client.patch(f"/api/lecture/{item['id']}", json={"markdown": "## 手改但没加锁\n"})
    assert _lecture_of(client, course_id, node["id"])["meta"]["locked"] is True  # 手改自动加锁
    unlocked = client.post(f"/api/lecture/{item['id']}/lock", json={"locked": False})
    assert unlocked.status_code == 200 and unlocked.json()["locked"] is False

    resp = client.post("/api/lecture/generate", json={
        "course_id": course_id, "node_id": node["id"], "tone": "正式", "item_id": item["id"]})
    assert resp.status_code == 200, resp.text
    state = _wait_job(client, resp.json()["job_id"])
    assert state["status"] == "success", state
    assert state["result"]["unlocked"] is False and state["result"]["unlock_message"] == ""
    after = state["result"]["item"]
    assert "手改但没加锁" not in after["body"]["markdown"]
    assert after["meta"]["locked"] is False
    assert "locked_reason" not in after["meta"]


def test_quality_regenerate_also_drops_the_badge(client, course_id, gen_lecture_row):
    """同族路径：「按质检建议重生成」也是覆盖 ⇒ 徽标同样不能留着。"""
    node, before = _reviewed_lecture(client, course_id, gen_lecture_row,
                                     "## 老师审过的版本（按质检重生成）\n")
    resp = client.post(f"/api/lecture/{before['id']}/regenerate-qc", json={"tone": "正式"})
    assert resp.status_code == 200, resp.text
    state = _wait_job(client, resp.json()["job_id"])
    assert state["status"] == "success", state
    row = _lecture_of(client, course_id, node["id"])
    assert row["meta"]["locked"] is False
    assert "locked_reason" not in row["meta"]
    assert _sha1(row["body"]["markdown"]) != _sha1(before["body"]["markdown"])


def test_lock_row_is_written_in_base_meta(client, course_id, gen_lecture_row):
    """锁的语义本身不变：`set_lecture_locked` 的写法与读法仍然一致。"""
    _plan_and_approve(client, course_id)
    node = _nodes(client, course_id)[0]
    item = gen_lecture_row(course_id, node["id"])
    assert client.post(f"/api/lecture/{item['id']}/lock", json={
        "locked": True, "reason": "审核通过"}).json()["locked"] is True
    db = SessionLocal()
    try:
        row = db.get(ContentItem, item["id"])
        assert row.meta["locked"] is True and row.meta["locked_reason"] == "审核通过"
    finally:
        db.close()
    off = client.post(f"/api/lecture/{item['id']}/lock", json={"locked": False}).json()
    assert off["locked"] is False and "locked_reason" not in off["meta"]