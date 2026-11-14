# -*- coding: utf-8 -*-
"""M5a 只读分享链接：令牌 / 范围隔离 / 有效期 / 密码 / 撤销 / 405 / 访问留痕 / 管理侧鉴权。"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from app.db.session import SessionLocal
from app.models.course import ContentItem, Outline, OutlineNode
from app.models.share import ShareAccessLog, ShareLink
from app.services.share import hash_token


def _make_course(client, title: str = "分享测试课") -> str:
    resp = client.post("/api/course/", json={"title": title, "subject": "生物",
                                             "audience": "初中生", "duration_minutes": 90})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _create_link(client, course_id: str, **payload) -> dict:
    body = {"label": "给客户", "base_url": "http://localhost:5173"}
    body.update(payload)
    resp = client.post(f"/api/share/course/{course_id}", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _prepare_course_with_lecture(client) -> str:
    """建课 → 生成大纲 → 通过 → 落一个知识点 + 一篇讲义（公开侧要有真实内容可读）。

    mock 规划器只产章节点（与 tests/test_ppt_v3.py 同口径），知识点直接落库。
    """
    course_id = _make_course(client)
    plan = client.post("/api/plan/generate", json={
        "course_id": course_id, "audience": "初中生", "goal": "理解光合作用",
        "num_chapters": 2, "style": "标准",
    })
    assert plan.status_code == 200, plan.text
    assert client.post("/api/plan/approve",
                       json={"course_id": course_id, "approved": True}).status_code == 200
    db = SessionLocal()
    try:
        outline = db.query(Outline).filter(Outline.course_id == course_id).first()
        node = OutlineNode(outline_id=outline.id, node_key="1.1", node_type="point",
                           title="光合作用", objective="理解光合作用", order_index=1)
        db.add(node)
        db.commit()
        db.refresh(node)
        db.add(ContentItem(course_id=course_id, node_id=node.id, kind="lecture",
                           title="光合作用",
                           body={"markdown": "# 光合作用\n\n- 光反应\n- 暗反应", "tone": "正式"},
                           meta={}))
        db.commit()
    finally:
        db.close()
    return course_id


# ① token 能读到该课；列表不含明文 token
def test_token_reads_its_course(client):
    course_id = _prepare_course_with_lecture(client)
    created = _create_link(client, course_id)
    token = created["token"]
    assert created["url"] == f"http://localhost:5173/share/{token}"
    assert "只显示这一次" in created["notice"]
    assert created["status"] == "active"

    overview = client.get(f"/api/public/{token}")
    assert overview.status_code == 200, overview.text
    data = overview.json()
    assert data["course"]["id"] == course_id
    assert data["share"]["permission"] == "view"
    assert data["concept_count"] >= 0

    content = client.get(f"/api/public/{token}/content")
    assert content.status_code == 200
    kinds = {row["kind"] for row in content.json()}
    assert "lecture" in kinds
    # 公开侧不暴露 vault（笔记）与本机绝对路径等内部字段
    assert all("vault" not in (row.get("meta") or {}) for row in content.json())
    assert all(row["kind"] != "ppt" for row in content.json())   # 课件走 /public/{token}/ppt
    assert "vault_path" not in content.text
    assert "note_path" not in content.text
    assert ".pptx" not in content.text

    plan = client.get(f"/api/public/{token}/plan")
    assert plan.status_code == 200 and plan.json()["nodes"]

    listing = client.get(f"/api/share/course/{course_id}").json()
    assert [i["id"] for i in listing["items"]] == [created["id"]]
    assert "token" not in listing["items"][0]
    assert "password_hash" not in listing["items"][0]


# ② 改一个字符的 token → 403
def test_tampered_token_is_403(client):
    course_id = _make_course(client)
    token = _create_link(client, course_id)["token"]
    bad = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert client.get(f"/api/public/{bad}").status_code == 403
    assert client.get(f"/api/public/{bad}/content").status_code == 403
    assert client.get(f"/api/public/{token}").status_code == 200  # 原 token 仍可用


# ③ 撤销 → 立即 410
def test_revoked_link_is_410(client):
    course_id = _make_course(client)
    created = _create_link(client, course_id)
    token = created["token"]
    resp = client.post(f"/api/share/{created['id']}/revoke")
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert resp.json()["status"] == "revoked"
    assert client.get(f"/api/public/{token}").status_code == 410
    assert client.get(f"/api/public/{token}/content").status_code == 410
    listing = client.get(f"/api/share/course/{course_id}").json()["items"][0]
    assert listing["status"] == "revoked" and listing["revoked_at"]


# ④ 过期 → 410
def test_expired_link_is_410(client):
    course_id = _make_course(client)
    created = _create_link(client, course_id, expires_in_days=1)
    token = created["token"]
    assert created["expires_at"]
    db = SessionLocal()
    try:
        link = db.get(ShareLink, created["id"])
        link.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    finally:
        db.close()
    assert client.get(f"/api/public/{token}").status_code == 410
    assert client.get(f"/api/share/course/{course_id}").json()["items"][0]["status"] == "expired"


# ⑤ 密码：不传/传错 → 401，传对 → 200
def test_password_link(client):
    course_id = _make_course(client)
    created = _create_link(client, course_id, password="s3cret")
    token = created["token"]
    assert created["has_password"] is True
    assert client.get(f"/api/public/{token}").status_code == 401
    assert client.get(f"/api/public/{token}", params={"password": "wrong"}).status_code == 401
    assert client.get(f"/api/public/{token}", headers={"X-Share-Password": "s3cret"}).status_code == 200
    assert client.get(f"/api/public/{token}", params={"password": "s3cret"}).status_code == 200


# ⑥ 公开侧任何写方法 → 405
def test_public_side_is_read_only(client):
    course_id = _make_course(client)
    token = _create_link(client, course_id)["token"]
    paths = [f"/api/public/{token}", f"/api/public/{token}/content",
             f"/api/public/{token}/plan", f"/api/public/{token}/ppt"]
    for path in paths:
        for method in ("post", "put", "patch", "delete"):
            resp = getattr(client, method)(path)
            assert resp.status_code == 405, f"{method.upper()} {path} -> {resp.status_code}"


# ⑦ A 课 token 读 B 课 → 403（范围隔离）
def test_course_scope_isolation(client):
    course_a = _prepare_course_with_lecture(client)
    course_b = _make_course(client, "B 课")
    token_a = _create_link(client, course_a)["token"]
    assert client.get(f"/api/public/{token_a}", params={"course_id": course_b}).status_code == 403
    assert client.get(f"/api/public/{token_a}/content",
                      params={"course_id": course_b}).status_code == 403
    assert client.get(f"/api/public/{token_a}/ppt",
                      params={"course_id": course_b}).status_code == 403
    # 不带 course_id：只返回 A 课自己的数据
    rows = client.get(f"/api/public/{token_a}/content").json()
    assert rows and client.get(f"/api/public/{token_a}").json()["course"]["id"] == course_a


# ⑧ 访问日志写入 + access_count 累加
def test_access_log_and_count(client):
    course_id = _make_course(client)
    created = _create_link(client, course_id)
    token = created["token"]
    assert client.get(f"/api/public/{token}").status_code == 200
    assert client.get(f"/api/public/{token}").status_code == 200
    assert client.get(f"/api/public/{token}/content").status_code == 200
    # 失败请求不计数
    assert client.get("/api/public/bad-token").status_code == 403

    db = SessionLocal()
    try:
        link = db.get(ShareLink, created["id"])
        assert link.access_count == 3
        assert link.last_accessed_at is not None
        logs = (db.query(ShareAccessLog)
                .filter(ShareAccessLog.link_id == created["id"]).all())
        assert len(logs) == 3
        assert {log.path for log in logs} == {
            f"/api/public/{token}", f"/api/public/{token}/content"}
        assert all(log.ts is not None for log in logs)
    finally:
        db.close()

    listing = client.get(f"/api/share/course/{course_id}").json()["items"][0]
    assert listing["access_count"] == 3
    assert listing["last_accessed_at"]


# ⑨ AUTH_ENABLED=true 时管理侧仍需 key（公开侧不受影响）
def test_admin_share_requires_key_when_auth_enabled(client, monkeypatch):
    from app.api import deps

    class _FakeSettings:
        auth_enabled = True
        dev_api_key = "k-test-share"

    course_id = _make_course(client)          # 先建课，再打开鉴权开关
    monkeypatch.setattr(deps, "get_settings", lambda: _FakeSettings())
    assert client.post(f"/api/share/course/{course_id}", json={}).status_code == 401
    assert client.get(f"/api/share/course/{course_id}").status_code == 401

    ok = client.post(f"/api/share/course/{course_id}", json={},
                     headers={"X-API-Key": "k-test-share"})
    assert ok.status_code == 200, ok.text
    token = ok.json()["token"]
    # 公开侧免登录：不带任何 key 也能读
    assert client.get(f"/api/public/{token}").status_code == 200
    assert client.get(f"/api/share/course/{course_id}",
                      headers={"X-API-Key": "k-test-share"}).status_code == 200
    assert client.delete(f"/api/share/{ok.json()['id']}",
                         headers={"X-API-Key": "k-test-share"}).status_code == 200


# 附加：token 只存哈希；删除记录后 token 失效
def test_token_stored_hashed_and_delete(client):
    course_id = _make_course(client)
    created = _create_link(client, course_id)
    token = created["token"]
    db = SessionLocal()
    try:
        link = db.get(ShareLink, created["id"])
        assert link.token_hash == hash_token(token)
        assert token not in (link.token_hash or "")
        assert link.token_hash != token
    finally:
        db.close()
    assert client.delete(f"/api/share/{created['id']}").status_code == 200
    assert client.get(f"/api/public/{token}").status_code == 403
    assert client.delete(f"/api/share/{created['id']}").status_code == 404


# 附加：图片代理挡住目录穿越；PPT 只给页级大纲与缩略图，不给下载直链
def test_image_proxy_and_ppt_payload(client):
    course_id = _prepare_course_with_lecture(client)
    created = _create_link(client, course_id)
    token = created["token"]
    assert client.get(f"/api/public/{token}/images/%2e%2e%2fsecret.txt").status_code == 404
    assert client.get(f"/api/public/{token}/images/missing.svg").status_code == 404

    queued = client.post("/api/ppt/generate", json={
        "course_id": course_id, "scope": "course", "engine": "python-pptx"})
    assert queued.status_code == 200, queued.text
    job_id = queued.json()["job_id"]
    state = None
    for _ in range(200):
        state = client.get(f"/api/ppt/jobs/{job_id}").json()
        if state["status"] in ("success", "failed"):
            break
        time.sleep(0.2)
    assert state is not None and state["status"] == "success", state

    payload = client.get(f"/api/public/{token}/ppt").json()
    assert payload["course"], payload
    deck = payload["course"][0]
    assert deck["slides"], deck
    assert "urls" not in deck                     # 不给 pptx/pdf 下载直链
    assert all("image" not in slide for slide in deck["slides"])