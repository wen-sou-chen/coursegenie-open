# -*- coding: utf-8 -*-
"""任务 48k：概念库就绪后 ``terminology`` 不再长期 skipped（判定 + 链路 + 前端）。

规格见 HANDOFF「任务 48k 说明」B/C 段：

1. **判定侧**：``qc_outdated()`` 多一条「**概念库非空、但这篇的 terminology 还是
   skipped**」；护栏：概念库仍为空时**不许**判过期（否则每次读质检都白算一遍）；
2. **链路侧**：概念抽取成功后自动 0-LLM 复检该课讲义（``refresh_course``），
   单篇失败不阻断，把失败篇数写进 ``job.result`` 的一句话里；
3. **前端**：``QcBadge`` 的「开始质检」带 ``refresh=true``（另有前端口径断言脚本
   ``frontend/scripts/check-qc-refresh.ts``）。

``QC_VERSION`` / ``CHECK_KEYS`` / ``_WEIGHT`` / 评分分母**一个字节没动**（本文件里显式断言）。
"""
from __future__ import annotations

import time

from app.db.session import SessionLocal
from app.models.course import ContentItem, Course
from app.services import lecture_qc
from app.services.lecture_qc import (
    CHECK_KEYS,
    QC_VERSION,
    SKIPPED_LEVEL,
    concepts_available,
    qc_outdated,
    refresh_course,
    stored_qc,
)

from tests.test_lecture_qc import _add_sections, _node_id, _plan_approve


def _qc_with(terminology_level: str) -> dict:
    """一份「够新」的缓存质检，只有 terminology 一项按参数给。"""
    checks = [{"key": key, "level": "ok", "detail": ""} for key in CHECK_KEYS]
    for check in checks:
        if check["key"] == "terminology":
            check["level"] = terminology_level
    return {"version": QC_VERSION, "score": 100, "checks": checks}


def _course_qc(client, course_id: str) -> dict:
    resp = client.post(f"/api/lecture/course/{course_id}/qc", json={"worst_n": 5, "refresh": False})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _concept_generate(client, course_id: str) -> dict:
    """跑一次概念抽取 job（Mock LLM），返回 job.result。"""
    resp = client.post(f"/api/concept/course/{course_id}/generate")
    assert resp.status_code == 200, resp.text
    queued = resp.json()
    state = None
    for _ in range(200):
        state = client.get(f"/api/concept/jobs/{queued['job_id']}").json()
        if state["status"] in ("success", "failed"):
            break
        time.sleep(0.1)
    assert state is not None and state["status"] == "success", state
    return state["result"]


def _terminology_levels(course_id: str) -> list[str]:
    levels: list[str] = []
    db = SessionLocal()
    try:
        rows = (db.query(ContentItem)
                .filter(ContentItem.course_id == course_id, ContentItem.kind == "lecture")
                .order_by(ContentItem.created_at).all())
        for row in rows:
            qc = stored_qc(row) or {}
            check = next((c for c in qc.get("checks") or []
                          if c.get("key") == "terminology"), {})
            levels.append(str(check.get("level") or ""))
    finally:
        db.close()
    return levels


def _prepare_two_lectures(client, course_id, gen_lecture_row) -> list[dict]:
    _plan_approve(client, course_id)
    _add_sections(course_id, "1", ["光合作用的过程", "光合作用的原料"])
    return [gen_lecture_row(course_id, _node_id(client, course_id, key))
            for key in ("1.1", "1.2")]


# ---------------------------------------------------------------------------
# ① 判定侧：三种情形（HANDOFF C.1）
# ---------------------------------------------------------------------------

def test_qc_outdated_only_when_concepts_are_ready():
    skipped = _qc_with(SKIPPED_LEVEL)
    # ① 概念库为空（concepts_ready=False）+ terminology=skipped ⇒ 仍不过期
    assert qc_outdated(skipped) is False
    assert qc_outdated(skipped, concepts_ready=False) is False
    # ② 概念库非空 + terminology=skipped ⇒ 过期（要重算）
    assert qc_outdated(skipped, concepts_ready=True) is True
    # ③ 概念库非空 + 已有真实结论（ok / warn）⇒ 不过期，不重复算
    assert qc_outdated(_qc_with("ok"), concepts_ready=True) is False
    assert qc_outdated(_qc_with("warn"), concepts_ready=True) is False
    # 老数据（没 checks / 版本落后）照旧过期
    assert qc_outdated(None) is True
    assert qc_outdated({"version": 1, "checks": [{"key": "terminology", "level": "ok"}]}) is True


def test_qc_rule_constants_untouched():
    """48k 只加一种「过期」情形：11 项判据与版本号一个字节不动。"""
    assert len(CHECK_KEYS) == 11
    assert QC_VERSION == 3
    assert "terminology" in CHECK_KEYS


def test_concepts_available_reflects_concept_library(client, course_id, gen_lecture_row):
    _prepare_two_lectures(client, course_id, gen_lecture_row)
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        assert concepts_available(db, course) is False      # 还没抽概念
    finally:
        db.close()

    _concept_generate(client, course_id)

    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        assert concepts_available(db, course) is True       # 抽完概念库非空
    finally:
        db.close()


# ---------------------------------------------------------------------------
# ② 链路侧：概念抽取后该课讲义 terminology 不再是 skipped（真实链路，临时三件套）
# ---------------------------------------------------------------------------

def test_concept_extraction_refreshes_lecture_qc(client, course_id, gen_lecture_row):
    items = _prepare_two_lectures(client, course_id, gen_lecture_row)
    assert len(items) == 2

    before_levels = _terminology_levels(course_id)
    before = _course_qc(client, course_id)
    assert before_levels == [SKIPPED_LEVEL, SKIPPED_LEVEL], before_levels
    assert before["skipped_items"] == 2

    result = _concept_generate(client, course_id)

    after_levels = _terminology_levels(course_id)
    after = _course_qc(client, course_id)
    # 跑 `pytest -s` 时能看到这一行（临时三件套实测：87.0 → 88.0）
    print(f"\n[48k] 复检前 {before['average_score']} 分（terminology={before_levels}）"
          f" → 复检后 {after['average_score']} 分（terminology={after_levels}）")

    # 概念抽取后：terminology 不再是 skipped，且报告里能看到真实判定
    assert after_levels == ["ok", "ok"], after_levels
    assert after["skipped_items"] == 0
    terminology_row = next(c for c in after["checks"] if c["key"] == "terminology")
    assert terminology_row["skipped"] == 0
    assert terminology_row["ok"] + terminology_row["warn"] + terminology_row["fail"] == 2

    # job.result 里的一句话：复检了多少篇
    assert result["qc_recheck"] == {"lectures": 2, "refreshed": 2, "failed": 0}
    assert "2 篇已按当前口径重算" in result["qc_recheck_note"]


def test_stored_qc_stops_being_outdated_after_refresh(client, course_id, gen_lecture_row):
    """复检过的讲义不会被反复重算（否则每次读质检都白算一遍）。"""
    _prepare_two_lectures(client, course_id, gen_lecture_row)
    _concept_generate(client, course_id)

    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        for row in (db.query(ContentItem)
                    .filter(ContentItem.course_id == course_id, ContentItem.kind == "lecture")
                    .all()):
            cached = stored_qc(row)
            assert cached is not None
            assert qc_outdated(cached, concepts_ready=True) is False
            first = lecture_qc.item_qc(db, course, row)
            second = lecture_qc.item_qc(db, course, row)
            assert first["generated_at"] == second["generated_at"]      # 第二次没重算
    finally:
        db.close()


def test_refresh_course_is_idempotent(client, course_id, gen_lecture_row):
    _prepare_two_lectures(client, course_id, gen_lecture_row)
    _concept_generate(client, course_id)

    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        first = refresh_course(db, course)
        scores_first = [int(qc["score"]) for qc in _stored_scores(db, course_id)]
        second = refresh_course(db, course)
        scores_second = [int(qc["score"]) for qc in _stored_scores(db, course_id)]
    finally:
        db.close()
    assert first == {"lectures": 2, "refreshed": 2, "failed": 0}
    assert second == first
    assert scores_first == scores_second        # 0 LLM、幂等：重复复检不改结论


def _stored_scores(db, course_id: str) -> list[dict]:
    rows = (db.query(ContentItem)
            .filter(ContentItem.course_id == course_id, ContentItem.kind == "lecture")
            .order_by(ContentItem.created_at).all())
    return [stored_qc(row) or {} for row in rows]


# ---------------------------------------------------------------------------
# ③ 失败不阻断：复检出错也照旧成功，失败篇数进 job.result
# ---------------------------------------------------------------------------

def test_recheck_failure_does_not_block_concept_job(client, course_id, gen_lecture_row,
                                                    monkeypatch):
    _prepare_two_lectures(client, course_id, gen_lecture_row)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("模拟复检失败")

    monkeypatch.setattr(lecture_qc, "refresh_course", _boom)
    result = _concept_generate(client, course_id)      # job 仍然 success

    assert result["qc_recheck"] == {"error": "模拟复检失败"}
    assert "复检失败" in result["qc_recheck_note"]
    # 概念本身照旧抽到了（不因为复检失败丢结果）
    resp = client.get(f"/api/concept/course/{course_id}")
    assert resp.status_code == 200 and resp.json()["concepts"], resp.text


def test_recheck_counts_failed_lectures_without_raising(client, course_id, gen_lecture_row,
                                                        monkeypatch):
    """单篇复检抛异常 → 计入 failed，其余篇照旧复检（不抛、不阻断）。"""
    _prepare_two_lectures(client, course_id, gen_lecture_row)

    original = lecture_qc.refresh_item

    def _flaky(db, course, item, node=None, *, commit=True):
        if str(item.title).endswith("原料"):
            raise RuntimeError("模拟单篇失败")
        return original(db, course, item, node, commit=commit)

    monkeypatch.setattr(lecture_qc, "refresh_item", _flaky)
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        counts = refresh_course(db, course)
    finally:
        db.close()
    assert counts == {"lectures": 2, "refreshed": 1, "failed": 1}
