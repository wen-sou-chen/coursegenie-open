"""任务 83：时间口径统一 —— 库里 naive（SQLite 不存时区）的 datetime，出口一律带时区。

起因（DSH 2026-09-23 复验任务 81）：`course_versions.created_at` 是 `utcnow()` 落库的**裸 UTC**，
`versions.py` 原样 `isoformat()` ⇒ 出来是 `2026-09-21T06:24:46.708384`（没有 `Z` / `+00:00`），
而前端 `new Date(...)` 按 ES 规范把没有时区标记的串当**本地时间** ⇒ 版本面板比真实本地时间
少 8 小时（真机显示 `06:24`，真实是 `14:24`）。这里把「出口恒带时区」钉成断言：

① `as_utc_iso` 的行为（naive 补 `+00:00` / 带时区的原样 / `None` 仍是 `None`）；
② `GET /version/{id}` 的 `created_at` 带 `+00:00`，且换算回 UTC 与库里那一刻**同一个瞬间**；
③ 分享链接的 `expires_at` / `created_at` / `last_accessed_at` / `revoked_at` 同样（管理侧 + 公开侧）；
④ `POST /plan/{id}/check` 写一行 INFO（与 `material.py` 的同类显式写入口同款）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.db.session import SessionLocal
from app.models.course import CourseVersion
from app.services.timefmt import as_aware, as_utc_iso


# ---------------------------------------------------------------------------
# ① as_utc_iso / as_aware
# ---------------------------------------------------------------------------

def test_as_utc_iso_marks_naive_as_utc():
    """naive（SQLite 读回来的形态）必须补上 `+00:00`，否则前端会当本地时间。"""
    naive = datetime(2026, 9, 21, 6, 24, 46, 708384)
    assert as_utc_iso(naive) == "2026-09-21T06:24:46.708384+00:00"
    assert as_aware(naive).utcoffset() == timedelta(0)


def test_as_utc_iso_keeps_aware_value_and_none():
    """带时区的原样输出（不换时区、不改值）；`None` 仍是 `None`。"""
    aware = datetime(2026, 9, 21, 14, 24, tzinfo=timezone(timedelta(hours=8)))
    assert as_utc_iso(aware) == aware.isoformat()
    assert as_utc_iso(None) is None
    assert as_aware(None) is None


def test_single_implementation_in_timefmt():
    """唯一实现：`share.py` 不再自己写一份 `as_aware`（只允许从 timefmt 引入）。"""
    import inspect

    from app.services import share

    assert not hasattr(share, "as_aware") or share.as_aware is as_aware
    assert "def as_aware(" not in inspect.getsource(share)


# ---------------------------------------------------------------------------
# ② 版本接口
# ---------------------------------------------------------------------------

def test_version_created_at_carries_timezone(client, course_id):
    """版本快照的时间出口必须带时区，且与库里那一刻是同一个瞬间（不再少 8 小时）。"""
    snap = client.post(f"/api/version/{course_id}/snapshot")
    assert snap.status_code == 200, snap.text

    resp = client.get(f"/api/version/{course_id}")
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert rows, "快照之后应该有版本记录"
    row = rows[0]

    at = row["created_at"]
    assert at.endswith("+00:00"), f"版本时间没带时区（前端会当本地时间）：{at}"
    assert "T" in at and "." in at, f"出口应仍是 ISO 格式（只多一个时区）：{at}"

    db = SessionLocal()
    try:
        stored = db.get(CourseVersion, row["id"]).created_at
    finally:
        db.close()
    assert stored.tzinfo is None, "前置：SQLite 读回来就是 naive（本任务的根因）"
    parsed = datetime.fromisoformat(at)
    assert parsed.utcoffset() == timedelta(0)
    assert parsed.replace(tzinfo=None) == stored, "出口值与库里那一刻不是同一个瞬间"


# ---------------------------------------------------------------------------
# ③ 分享链接（管理侧 + 公开侧）
# ---------------------------------------------------------------------------

def test_share_link_times_carry_timezone(client, course_id):
    """`expires_at` / `created_at` / `revoked_at` / `last_accessed_at` 全部带时区。"""
    created = client.post(f"/api/share/course/{course_id}", json={
        "label": "给客户", "base_url": "http://localhost:5173", "expires_in_days": 7,
    })
    assert created.status_code == 200, created.text
    body = created.json()
    token = body["token"]

    for key in ("created_at", "expires_at"):
        assert str(body[key]).endswith("+00:00"), f"{key} 没带时区：{body[key]}"
    assert body["revoked_at"] is None and body["last_accessed_at"] is None

    # 公开侧（客户看的那个页面）同样带时区
    overview = client.get(f"/api/public/{token}")
    assert overview.status_code == 200, overview.text
    expires = overview.json()["share"]["expires_at"]
    assert str(expires).endswith("+00:00"), f"公开侧的 expires_at 没带时区：{expires}"

    # 访问一次 → last_accessed_at 出现且带时区
    assert client.get(f"/api/public/{token}/plan").status_code == 200
    listed = client.get(f"/api/share/course/{course_id}").json()["items"][0]
    assert str(listed["last_accessed_at"]).endswith("+00:00"), listed["last_accessed_at"]

    # 撤销 → revoked_at 出现且带时区
    revoked = client.post(f"/api/share/{body['id']}/revoke")
    assert revoked.status_code == 200, revoked.text
    assert str(revoked.json()["revoked_at"]).endswith("+00:00"), revoked.json()["revoked_at"]


def test_share_expires_at_is_not_shifted(client, course_id):
    """「有效期天数」按 UTC 计算：出口值 = 库里的裸 UTC 值补 `+00:00`（不许被再挪 8 小时）。"""
    created = client.post(f"/api/share/course/{course_id}", json={
        "label": "对账", "base_url": "http://localhost:5173", "expires_in_days": 30,
    })
    assert created.status_code == 200, created.text
    link_id = created.json()["id"]

    from app.models.share import ShareLink

    db = SessionLocal()
    try:
        stored = db.get(ShareLink, link_id).expires_at
    finally:
        db.close()
    assert stored.tzinfo is None
    parsed = datetime.fromisoformat(created.json()["expires_at"])
    assert parsed.replace(tzinfo=None) == stored


# ---------------------------------------------------------------------------
# ④ 写入口的 INFO 日志
# ---------------------------------------------------------------------------

def test_plan_check_writes_info_log(client, course_id, caplog):
    """`POST /plan/{id}/check` 写一行 INFO（任务 72 的同类显式入口有两条）。"""
    plan = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "光合作用", "num_chapters": 2})
    assert plan.status_code == 200, plan.text

    with caplog.at_level(logging.INFO, logger="app.api.routes.plan"):
        resp = client.post(f"/api/plan/{course_id}/check")
    assert resp.status_code == 200, resp.text
    hits = [r for r in caplog.records if "大纲自检补跑入口被调用" in r.getMessage()]
    assert hits, [r.getMessage() for r in caplog.records]
    message = hits[0].getMessage()
    assert "course=" in message and "（不调模型、不产生用量）" in message, message
