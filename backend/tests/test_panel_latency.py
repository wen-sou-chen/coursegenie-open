# -*- coding: utf-8 -*-
r"""任务 48z：两个面板首屏不再被「整库笔记一致性检查」阻塞（判定口径一个字未改）。

对应 `AI协作区\HANDOFF.md`「任务 48z 说明」§B / §C。DSH 实测基线（真库 + 真 vault）：
`GET /api/delivery/course/{id}` **5.03 s**、`GET /api/audit/course/{id}/report` **5.01 s**，
其余 12 个面板接口 ≤0.20 s；根因是 GET 里同步跑 ``kb.verify_course_notes``
（逐项比对整库 150 项，本次实测 4.33 s，占总耗时 94%）。

这里用「把慢函数换成会抛异常的桩」来证明首屏不再碰它 —— 比掐表稳定，且不依赖机器快慢。
"""
from __future__ import annotations

from app.api.routes import delivery as delivery_route
from app.db.session import SessionLocal
from app.models.course import Course
from app.services import audit as audit_service
from app.services import kb as kb_mod


def _boom(*_args, **_kwargs):
    raise AssertionError("首屏不该跑整库笔记一致性检查（任务 48z）")


# --------------------------------------------------------------- 交付包面板
def test_delivery_overview_does_not_run_notes_verification(client, course_id, monkeypatch):
    """首屏（清单预览 + 历史包 + 交付模式）不得调用整库一致性检查。"""
    monkeypatch.setattr(delivery_route.kb_service, "verify_course_notes", _boom)
    resp = client.get(f"/api/delivery/course/{course_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["consistency"] is None
    assert body["consistency_deferred"] is True
    for key in ("preview", "builds", "delivery_mode", "course_title"):
        assert key in body, key


def test_delivery_consistency_endpoint_keeps_the_same_fields(client, course_id, monkeypatch):
    """独立接口与「老口径一次拿齐」逐字段一致，且都真的调了同一个算法。"""
    sentinel = {"ok": True, "reason": "", "checked": 150, "inconsistent": 3, "consistent": 147,
                "vault_root": "V", "nodes": [{"x": 1}]}
    calls = []

    def fake(db, course, node_ids=None):
        calls.append((course.id, node_ids))
        return dict(sentinel)

    monkeypatch.setattr(delivery_route.kb_service, "verify_course_notes", fake)
    row = client.get(f"/api/delivery/course/{course_id}/consistency").json()
    assert {k: row[k] for k in ("ok", "reason", "checked", "inconsistent", "consistent", "vault_root")} == \
           {k: sentinel[k] for k in ("ok", "reason", "checked", "inconsistent", "consistent", "vault_root")}
    assert "不一致" in row["hint"] and "拒绝出包" in row["hint"]

    combined = client.get(f"/api/delivery/course/{course_id}?with_consistency=1").json()
    assert combined["consistency"] == row
    assert combined["consistency_deferred"] is False
    assert len(calls) == 2, calls


def test_delivery_consistency_endpoint_matches_real_algorithm(client, course_id):
    """口径未变：接口数字 == 直接调 ``kb.verify_course_notes`` 的同一批字段。"""
    row = client.get(f"/api/delivery/course/{course_id}/consistency").json()
    db = SessionLocal()
    try:
        raw = kb_mod.verify_course_notes(db, db.get(Course, course_id))
    finally:
        db.close()
    assert row["reason"] == str(raw.get("reason") or "")   # reason 走 str(... or "") 归一
    for key in ("checked", "consistent", "inconsistent", "vault_root"):
        assert row[key] == raw.get(key), key
    assert row["ok"] == bool(raw.get("ok"))


# --------------------------------------------------------------- 体检面板
def test_audit_overview_skips_notes_verification(client, course_id, monkeypatch):
    """首屏轻量概览：五段照算，整库一致性那一段不碰。"""
    monkeypatch.setattr(audit_service.kb, "verify_course_notes", _boom)
    resp = client.get(f"/api/audit/course/{course_id}/overview")
    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["partial"] is True
    assert "consistency" in row["deferred"] and "conclusion" in row["deferred"]
    assert row["consistency"] is None and row["conclusion"] is None
    assert row["actions"] == [] and row["summary"] == "" and row["markdown"] == ""
    for key in ("coverage", "links", "quality", "usage", "export_health"):
        assert key in row, key


def test_audit_overview_numbers_equal_full_report(client, course_id):
    """该快的那几段两条路径同源同值（时间戳字段除外）。"""
    quick = client.get(f"/api/audit/course/{course_id}/overview").json()
    full = client.get(f"/api/audit/course/{course_id}/report").json()
    assert full["consistency"] is not None and full["links"] is not None
    assert full["conclusion"]["score"] >= 0 and full["markdown"]
    for key in ("coverage", "links", "export_health"):
        assert full[key] == quick[key], key
    for key in ("average_score", "checked", "min_score", "max_score"):
        assert full["quality"][key] == quick["quality"][key], key
    for key in ("total_tokens", "llm_calls", "cost_cny"):
        assert full["usage"][key] == quick["usage"][key], key


def test_audit_full_report_unchanged_when_vault_wired(client, course_id, monkeypatch):
    """完整报告仍有八段：conclusion / consistency / actions / markdown 一个不少。"""
    resp = client.get(f"/api/audit/course/{course_id}/report")
    assert resp.status_code == 200, resp.text
    row = resp.json()
    for key in ("conclusion", "coverage", "consistency", "links", "quality", "usage",
                "export_health", "actions", "summary", "markdown"):
        assert key in row, key
    assert row.get("partial") is None  # 完整报告不是「轻量概览」
