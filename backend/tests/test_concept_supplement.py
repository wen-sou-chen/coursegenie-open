"""P6-1 回归测试：概念抽取规模自适应 + 库外关系目标一键补抽。

背景（任务 25 真实素材实战）：27 节只抽出 9 个概念，三条主线方法在图上是孤点；
图谱里「指向库外的关系目标」只能眼看，没有补抽入口。
"""
from __future__ import annotations

import json
import time

from app.db.session import SessionLocal
from app.services.llm import MockLLMClient, record_call
from app.models.course import Course
from app.services import concept as concept_service

from tests.test_concepts import _concepts, _generate, _plan_and_approve


class _StubLLM(MockLLMClient):
    """只返回指定概念的假客户端（实际走的是真实“抽取→合并→落库”链路）。"""

    def __init__(self, concepts: list[dict]) -> None:
        self._rows = concepts
        self.seen: list[str] = []

    def complete(self, system: str, user: str, *, max_tokens: int = 2000) -> str:
        if "CONCEPT_JSON" not in system:
            return super().complete(system, user, max_tokens=max_tokens)
        self.seen.append(user)
        started = time.perf_counter()
        text = json.dumps({"concepts": self._rows}, ensure_ascii=False)
        record_call(provider="stub", model="stub", prompt_tokens=None,
                    completion_tokens=None, system=system, user=user, content=text,
                    latency_ms=int((time.perf_counter() - started) * 1000))
        return text


def _concept(name: str, **kw) -> dict:
    row = {
        "name": name,
        "aliases": [],
        "definition": f"{name}是本课的基础概念。",
        "points": [f"{name}的定义", f"{name}的适用范围"],
        "broader": [],
        "narrower": [],
        "related": [],
        "pitfalls": [f"只记住{name}这个名字而不理解它的作用"],
        "chapters": ["1.1"],
        "sources": [],
    }
    row.update(kw)
    return row


# ---------------------------------------------------------------------------
# ① 规模自适应
# ---------------------------------------------------------------------------

def test_target_concept_count_scales_with_lectures():
    def corpus(lectures: int) -> dict:
        return {"nodes": [{"has_lecture": True} for _ in range(lectures)]}

    assert concept_service.target_concept_count(corpus(0)) == 12      # 下限
    assert concept_service.target_concept_count(corpus(6)) == 12
    assert concept_service.target_concept_count(corpus(27)) == 36     # 任务 25 的 AI 课
    assert concept_service.target_concept_count(corpus(200)) == concept_service.MAX_CONCEPTS


def test_prompt_carries_the_scaled_target(client, course_id):
    _plan_and_approve(client, course_id, num_chapters=3)
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        corpus = concept_service.collect_corpus(db, course)
        prompt = concept_service._prompt(course, corpus)
    finally:
        db.close()
    expected = concept_service.target_concept_count(corpus)
    assert f"请抽出约 {expected} 个基础概念" in prompt
    assert "覆盖率优先" in prompt


# ---------------------------------------------------------------------------
# ② 一键补抽：只动这次提到的，不能删掉概念库其余条目
# ---------------------------------------------------------------------------

def test_supplement_adds_missing_without_pruning_existing(client, course_id, monkeypatch):
    _plan_and_approve(client, course_id, num_chapters=2)
    _generate(client, course_id)
    before = {c["name"] for c in _concepts(client, course_id)["concepts"]}
    assert len(before) >= 2

    monkeypatch.setattr(concept_service, "build_llm_client",
                        lambda: _StubLLM([_concept("幻觉率")]))
    resp = client.post(f"/api/concept/course/{course_id}/supplement",
                       json={"names": ["幻觉率"]})
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["created"] == 1 and result["removed"] == 0, result
    assert result["requested"] == ["幻觉率"]
    assert result["missing"] == []

    after = {c["name"] for c in _concepts(client, course_id)["concepts"]}
    assert before <= after, "补抽不能删掉原有概念"
    assert "幻觉率" in after


def test_supplement_relinks_an_existing_isolated_concept(client, course_id, monkeypatch):
    """孤点（已在库里但没关系）：补抽后关系被填上，且不新建重复条目。"""
    _plan_and_approve(client, course_id, num_chapters=2)
    _generate(client, course_id)
    rows = _concepts(client, course_id)["concepts"]
    target, other = rows[0], rows[1]

    monkeypatch.setattr(concept_service, "build_llm_client",
                        lambda: _StubLLM([_concept(target["name"], related=[other["name"]])]))
    resp = client.post(f"/api/concept/course/{course_id}/supplement",
                       json={"names": [target["name"]]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] == 0 and resp.json()["updated"] >= 1

    after = {c["name"]: c for c in _concepts(client, course_id)["concepts"]}
    assert len(after) == len(rows), "不应新建重复概念"
    assert after[target["name"]]["body"]["relations"]["related"] == [other["name"]]


def test_supplement_names_can_come_from_dropped_relations(client, course_id, monkeypatch):
    """图谱里「指向库外的关系目标」→ 按一下就能补入库。"""
    _plan_and_approve(client, course_id)
    _generate(client, course_id)
    rows = _concepts(client, course_id)["concepts"]
    patched = client.patch(f"/api/concept/{rows[0]['id']}",
                           json={"relations": {"related": ["幻觉率"]}})
    assert patched.status_code == 200, patched.text

    graph = client.get(f"/api/concept/course/{course_id}/graph").json()
    assert [r["target"] for r in graph["dropped_relations"]] == ["幻觉率"]

    monkeypatch.setattr(concept_service, "build_llm_client",
                        lambda: _StubLLM([_concept("幻觉率")]))
    resp = client.post(f"/api/concept/course/{course_id}/supplement", json={"names": []})
    assert resp.status_code == 200, resp.text
    assert resp.json()["requested"] == ["幻觉率"]
    assert "幻觉率" in {c["name"] for c in _concepts(client, course_id)["concepts"]}


def test_supplement_without_names_or_dropped_targets_is_400(client, course_id):
    resp = client.post(f"/api/concept/course/{course_id}/supplement", json={"names": []})
    assert resp.status_code == 400
    assert "补抽" in resp.json()["detail"]
