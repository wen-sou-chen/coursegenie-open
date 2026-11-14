"""任务 23 回归测试：LLM 用量与成本可见 + 输入指纹去重 + 并发可配。

覆盖 D 段 7 条硬标准：
① 三级（单篇/任务/课程）汇总数字与 cost 换算正确
② 未知模型 → tokens 有、cost=0 且标注「未配置单价」
③ 同一输入连跑两次 → 第二次全部 skipped、无新调用、内容未变
④ 改大纲目标 / 换样板锚 → 指纹变化 → 正常重生成
⑤ llm_concurrency 生效（fake LLM 记录并发峰值）
⑥ 「强制重生成」忽略指纹
⑦ 「按质检建议重生成」不被去重跳过
"""
from __future__ import annotations

import json
import threading
import time

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.db.session import SessionLocal
from app.models.course import Course, Outline, OutlineNode
from app.services import usage as usage_log
from app.services.llm import MockLLMClient, record_call

import app.services.lecture as lecture_mod
import app.services.chart as chart_mod
import app.services.concept as concept_mod
import app.services.diagram as diagram_mod
import app.services.planner as planner_mod
import app.services.quiz as quiz_mod

PRICE_CHAT_IN = 2.0   # 元 / 百万 tokens（DeepSeek 官方价，与 usage.DEFAULT_PRICES 一致）
PRICE_CHAT_OUT = 3.0


class _MeteredLLM(MockLLMClient):
    """带 usage 的假客户端：模拟真实客户端「调用内部自己计量」的行为。

    - 每次调用按固定 token 数记一条（prompt=1000 / completion=500）；
    - 记录调用次数与并发峰值（线程安全），供断言去重与并发。
    """

    model = "deepseek-chat"

    def __init__(self, markdown: str | None = None, delay: float = 0.0) -> None:
        self.markdown = markdown
        self.delay = delay
        self.calls = 0
        self.prompts: list[str] = []
        self.peak = 0
        self._live = 0
        self._lock = threading.Lock()

    def _enter(self) -> None:
        with self._lock:
            self.calls += 1
            self._live += 1
            self.peak = max(self.peak, self._live)

    def _exit(self) -> None:
        with self._lock:
            self._live -= 1

    def complete(self, system: str, user: str, *, max_tokens: int = 2000) -> str:
        self._enter()
        try:
            if self.delay:
                time.sleep(self.delay)
            self.prompts.append(user)
            if "LECTURE_JSON" in system:
                body = self.markdown or f"## 讲义\n\n正文（{len(self.prompts)}）"
                text = json.dumps({"markdown": body, "summary": "摘要",
                                   "keywords": [], "quizzes": []}, ensure_ascii=False)
            else:
                # 走 _route 而不是 super().complete()：父类内部也会记账，避免一次调用记两条
                text = self._route(system, user)
        finally:
            self._exit()
        # 真实客户端在这里记账（provider 返回 usage）；假客户端同样走 record_call
        record_call(provider="openai", model=self.model, prompt_tokens=1000,
                    completion_tokens=500, system=system, user=user,
                    content=text, latency_ms=5)
        return text


def _expected_cost(calls: int) -> float:
    per_call = 1000 / 1_000_000 * PRICE_CHAT_IN + 500 / 1_000_000 * PRICE_CHAT_OUT
    return round(per_call * calls, 6)


def _use(monkeypatch, client_llm) -> None:
    """把讲义链路里所有会自己造客户端的模块都指向同一个假客户端（否则会漏计量）。"""
    for module in (lecture_mod, diagram_mod, chart_mod, quiz_mod):
        monkeypatch.setattr(module, "build_llm_client", lambda: client_llm)


def _approve(client, course_id: str) -> None:
    """改大纲节点会把课程状态打回 planned，重新「大纲通过」才能继续生成。"""
    assert client.post("/api/plan/approve", json={
        "course_id": course_id, "approved": True}).status_code == 200


def _plan_approve(client, course_id: str, num_chapters: int = 2) -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "生物光合作用", "num_chapters": num_chapters})
    assert resp.status_code == 200, resp.text
    assert client.post("/api/plan/approve", json={
        "course_id": course_id, "approved": True}).status_code == 200
    return resp.json()


def _add_sections(course_id: str, chapter_key: str, titles: list[str],
                  objective: str = "掌握光合作用的过程") -> list[str]:
    db = SessionLocal()
    try:
        outline = (db.query(Outline).filter(Outline.course_id == course_id)
                   .order_by(Outline.created_at.desc()).first())
        base = db.query(OutlineNode).filter(OutlineNode.outline_id == outline.id).count()
        keys = []
        for index, title in enumerate(titles, 1):
            key = f"{chapter_key}.{index}"
            db.add(OutlineNode(outline_id=outline.id, node_key=key, node_type="section",
                               title=title, objective=objective, order_index=base + index))
            keys.append(key)
        db.commit()
        return keys
    finally:
        db.close()


def _node_id(client, course_id: str, key: str) -> str:
    plan = client.get(f"/api/plan/{course_id}").json()
    return next(n["id"] for n in plan["nodes"] if n["key"] == key)


def _wait_job(client, job_id: str, tries: int = 300) -> dict:
    state = None
    for _ in range(tries):
        state = client.get(f"/api/content/jobs/{job_id}").json()
        if state["status"] in ("success", "failed"):
            return state
        time.sleep(0.1)
    raise AssertionError(f"job timeout: {job_id} {state}")


def _chapter_job(client, course_id: str, chapter_node_id: str,
                 force: bool = False) -> dict:
    resp = client.post("/api/lecture/generate-chapter", json={
        "course_id": course_id, "chapter_node_id": chapter_node_id, "tone": "正式",
        "force": force})
    assert resp.status_code == 200, resp.text
    return _wait_job(client, resp.json()["job_id"])


def _single(client, course_id: str, node_id: str, **extra) -> dict:
    payload = {"course_id": course_id, "node_id": node_id, "tone": "正式", **extra}
    resp = client.post("/api/lecture/generate", json=payload)
    assert resp.status_code == 200, resp.text
    return _wait_job(client, resp.json()["job_id"])


def _lecture_rows(client, course_id: str) -> list[dict]:
    return [r for r in client.get(f"/api/content/{course_id}").json() if r["kind"] == "lecture"]


def _prepared_course(client, course_id: str, titles: list[str] | None = None) -> dict:
    """建课 → 大纲通过 → 第 1 章插若干知识点，返回第 1 章节点。"""
    plan = _plan_approve(client, course_id)
    chapter1 = next(n for n in plan["nodes"] if n["key"] == "1")
    _add_sections(course_id, "1", titles or ["多菌共生体系", "酵母发酵"])
    return chapter1


# ---------------------------------------------------------------------------
# ① 三级汇总 + cost 换算
# ---------------------------------------------------------------------------

def test_three_level_usage_and_cost_math(client, course_id, monkeypatch):
    chapter1 = _prepared_course(client, course_id)
    llm = _MeteredLLM()
    _use(monkeypatch, llm)
    job = _chapter_job(client, course_id, chapter1["id"])
    assert job["status"] == "success", job

    result = job["result"]
    usage = result["usage"]
    assert usage["llm_calls"] == llm.calls > 0
    assert usage["total_tokens"] == 1500 * llm.calls
    assert abs(usage["cost_cny"] - _expected_cost(llm.calls)) < 1e-9, usage
    assert usage["per_node"] and len(usage["per_node"]) == result["ok"]
    assert {row["node_key"] for row in usage["per_node"]} == {"1.1", "1.2"}
    assert usage["by_purpose"].get("lecture"), usage["by_purpose"]

    # 单篇：meta.usage 与 per_node 自洽
    rows = _lecture_rows(client, course_id)
    items = {row["meta"]["node_key"]: row for row in rows}
    for node_key in ("1.1", "1.2"):
        per_item = items[node_key]["meta"]["usage"]
        assert per_item["llm_calls"] >= 1
        assert abs(per_item["cost_cny"] - _expected_cost(per_item["llm_calls"])) < 1e-9
        assert per_item["duration_ms"] >= 0
    total_tokens = sum(row["meta"]["usage"]["total_tokens"] for row in rows)
    assert usage["total_tokens"] == total_tokens

    # 课程级：讲义逐篇合计 + 课程级辅助用量（大纲生成，P3-4 起不再漏报）
    report = client.get(f"/api/analytics/{course_id}/usage").json()
    aux = {row["item_id"]: row for row in report["items"]
           if str(row["item_id"]).startswith("aux:")}
    assert "aux:outline" in aux, report["items"]
    aux_tokens = sum(row["total_tokens"] for row in aux.values())
    aux_cost = sum(row["cost_cny"] for row in aux.values())
    assert report["items_measured"] == len(rows) + len(aux)
    assert report["llm_calls"] == llm.calls + sum(row["llm_calls"] for row in aux.values())
    assert report["total_tokens"] == total_tokens + aux_tokens
    assert abs(report["cost_cny"] - (_expected_cost(llm.calls) + aux_cost)) < 1e-9, report
    assert report["avg_duration_ms"] >= 0
    assert {b["kind"] for b in report["by_kind"]} == {"lecture"} | {
        row["kind"] for row in aux.values()}
    assert abs(sum(b["cost_cny"] for b in report["by_kind"]) - report["cost_cny"]) < 1e-9
    assert len(report["top_items"]) == min(5, report["items_measured"])
    assert report["top_items"][0]["cost_cny"] >= report["top_items"][-1]["cost_cny"]


# ---------------------------------------------------------------------------
# ② 未知模型：记 tokens、cost=0、标注未配置单价
# ---------------------------------------------------------------------------

def test_unknown_model_records_tokens_without_price(client, course_id, monkeypatch):
    chapter1 = _prepared_course(client, course_id)
    llm = _MeteredLLM()
    llm.model = "some-vendor/unknown-model"
    _use(monkeypatch, llm)
    job = _chapter_job(client, course_id, chapter1["id"])
    assert job["status"] == "success", job

    usage = job["result"]["usage"]
    assert usage["total_tokens"] > 0
    assert usage["cost_cny"] == 0
    assert "some-vendor/unknown-model" in usage["unpriced_models"]
    assert "未配置单价" in usage["note"]

    report = client.get(f"/api/analytics/{course_id}/usage").json()
    assert report["total_tokens"] > 0 and report["cost_cny"] == 0
    assert report["priced"] is False
    assert "未配置单价" in report["note"]


def test_mock_client_records_estimated_usage():
    """mock 客户端没有 usage 字段：按文本估算记录（estimated 标注），且未配置单价。"""
    client_llm = MockLLMClient()
    with usage_log.collect() as collector:
        client_llm.complete("QUIZ_JSON", "光合作用\n题量：3", max_tokens=100)
    summary = collector.summary()
    assert summary["llm_calls"] == 1
    assert summary["total_tokens"] > 0
    assert summary["estimated_calls"] == 1
    assert summary["cost_cny"] == 0
    assert summary["note"] and "未配置单价" in summary["note"]


def test_custom_model_prices_override(monkeypatch):
    """MODEL_PRICES 可覆盖单价：按自定义价换算。"""
    monkeypatch.setattr(get_settings(), "model_prices",
                        json.dumps({"some-vendor/unknown-model": {"input": 1, "output": 4}}))
    with usage_log.collect() as collector:
        record_call(provider="openai", model="some-vendor/unknown-model",
                    prompt_tokens=1_000_000, completion_tokens=500_000,
                    system="s", user="u", content="c", latency_ms=1)
    summary = collector.summary()
    assert abs(summary["cost_cny"] - 3.0) < 1e-9, summary
    assert summary["unpriced_models"] == []


# ---------------------------------------------------------------------------
# ③ 同输入连跑两次 → 第二次全部跳过、无新调用、内容不变
# ---------------------------------------------------------------------------

def test_second_identical_batch_run_skips_without_new_calls(client, course_id, monkeypatch):
    chapter1 = _prepared_course(client, course_id)
    llm = _MeteredLLM()
    _use(monkeypatch, llm)
    first = _chapter_job(client, course_id, chapter1["id"])
    assert first["status"] == "success" and first["result"]["ok"] == 2, first
    calls_after_first = llm.calls
    bodies = {row["meta"]["node_key"]: row["body"]["markdown"]
              for row in _lecture_rows(client, course_id)}

    second = _chapter_job(client, course_id, chapter1["id"])
    result = second["result"]
    assert result["ok"] == 0, result
    assert [s["node_key"] for s in result["skipped"]] == ["1.1", "1.2"], result
    assert all(s["reason"] == "输入未变" for s in result["skipped"]), result
    assert llm.calls == calls_after_first, "第二次不应产生新的 LLM 调用"
    assert result["usage"]["llm_calls"] == 0 and result["usage"]["cost_cny"] == 0

    after = {row["meta"]["node_key"]: row["body"]["markdown"]
             for row in _lecture_rows(client, course_id)}
    assert after == bodies, "输入未变时内容不得变化"


def test_fingerprint_ignores_only_input_core(client, course_id, monkeypatch):
    """指纹只看输入：改正文（教师手改）不影响「是否跳过」的判定口径。"""
    chapter1 = _prepared_course(client, course_id, titles=["单节点"])
    llm = _MeteredLLM()
    _use(monkeypatch, llm)
    _chapter_job(client, course_id, chapter1["id"])
    row = _lecture_rows(client, course_id)[0]
    before = (row["meta"] or {}).get("input_fingerprint")
    assert before
    # 同一输入再跑：指纹不变
    _chapter_job(client, course_id, chapter1["id"])
    row2 = _lecture_rows(client, course_id)[0]
    assert (row2["meta"] or {}).get("input_fingerprint") == before


# ---------------------------------------------------------------------------
# ④ 改大纲目标 / 换样板锚 → 正常重生成
# ---------------------------------------------------------------------------

def test_outline_objective_change_triggers_regeneration(client, course_id, monkeypatch):
    chapter1 = _prepared_course(client, course_id, titles=["多菌共生体系"])
    llm = _MeteredLLM()
    _use(monkeypatch, llm)
    _chapter_job(client, course_id, chapter1["id"])
    node = _node_id(client, course_id, "1.1")

    assert client.patch(f"/api/plan/node/{node}", json={
        "objective": "说出多菌共生体系里酵母与乳酸菌的分工"}).status_code == 200
    _approve(client, course_id)

    calls_before = llm.calls
    again = _chapter_job(client, course_id, chapter1["id"])
    assert again["result"]["ok"] == 1, again["result"]
    assert again["result"]["skipped"] == [], again["result"]
    assert llm.calls > calls_before, "改大纲后应真正重生成"


def test_style_anchor_change_triggers_regeneration(client, course_id, monkeypatch):
    """换样板锚 → 指纹变化 → 其它知识点重生成；样板节本身受保护（P4-1）。"""
    chapter1 = _prepared_course(client, course_id, titles=["多菌共生体系", "酵母发酵"])
    llm = _MeteredLLM()
    _use(monkeypatch, llm)
    sample = _single(client, course_id, _node_id(client, course_id, "1.1"))["result"]["item"]
    assert client.post("/api/lecture/style-anchor", json={
        "course_id": course_id, "source_item_id": sample["id"]}).status_code == 200
    first = _chapter_job(client, course_id, chapter1["id"])
    # 「以这篇为样板」会自动给 1.1 加锁 → 本章只重做 1.2
    assert first["result"]["ok"] == 1, first["result"]
    assert [row["node_key"] for row in first["result"]["locked_skipped"]] == ["1.1"]
    calls_before = llm.calls

    # 换样板锚（改深度/讲法）→ 指纹变化 → 其它节必须重生成
    assert client.post("/api/lecture/style-anchor", json={
        "course_id": course_id, "depth": "深入", "tone": "幽默"}).status_code == 200
    again = _chapter_job(client, course_id, chapter1["id"])
    assert again["result"]["ok"] == 1, again["result"]
    assert [row["node_key"] for row in again["result"]["ok"] and
            again["result"].get("usage", {}).get("per_node", []) or []] == ["1.2"], again["result"]
    assert [row["node_key"] for row in again["result"]["locked_skipped"]] == ["1.1"]
    assert llm.calls > calls_before


# ---------------------------------------------------------------------------
# ⑤ llm_concurrency 生效（fake LLM 记录并发峰值）
# ---------------------------------------------------------------------------

def test_concurrency_setting_limits_parallel_calls(client, course_id, monkeypatch):
    chapter1 = _prepared_course(client, course_id,
                                titles=["多菌共生体系", "酵母发酵", "醋酸菌"])
    llm = _MeteredLLM(delay=0.2)
    _use(monkeypatch, llm)

    monkeypatch.setattr(get_settings(), "llm_concurrency", 1)
    _chapter_job(client, course_id, chapter1["id"])
    assert llm.peak == 1, f"并发 1 时不应并行（peak={llm.peak}）"

    # 改输入（否则第二次会被指纹去重跳过），并把并发调到 2
    for key in ("1.1", "1.2", "1.3"):
        client.patch(f"/api/plan/node/{_node_id(client, course_id, key)}",
                     json={"objective": f"复述{key}的关键机制"})
    _approve(client, course_id)
    llm.peak = 0
    monkeypatch.setattr(get_settings(), "llm_concurrency", 2)
    again = _chapter_job(client, course_id, chapter1["id"])
    assert again["result"]["ok"] == 3, again["result"]
    assert llm.peak == 2, f"并发 2 时应出现两路并行（peak={llm.peak}）"


def test_concurrency_and_timeout_config_range():
    """llm_concurrency 只接受 1~4，llm_timeout_s 默认 180。"""
    assert Settings(_env_file=None, llm_concurrency=1).llm_concurrency == 1
    assert Settings(_env_file=None, llm_concurrency=4).llm_concurrency == 4
    for bad in (0, 5):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, llm_concurrency=bad)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_timeout_s=0)
    assert Settings(_env_file=None).llm_timeout_s == 180


# ---------------------------------------------------------------------------
# ⑥ 强制重生成忽略指纹
# ---------------------------------------------------------------------------

def test_force_regenerate_ignores_fingerprint(client, course_id, monkeypatch):
    chapter1 = _prepared_course(client, course_id, titles=["多菌共生体系"])
    llm = _MeteredLLM()
    _use(monkeypatch, llm)
    _chapter_job(client, course_id, chapter1["id"])
    calls_before = llm.calls

    forced = _chapter_job(client, course_id, chapter1["id"], force=True)
    assert forced["result"]["ok"] == 1, forced["result"]
    assert forced["result"]["skipped"] == [], forced["result"]
    assert llm.calls > calls_before, "强制重生成必须真的重做"


# ---------------------------------------------------------------------------
# ⑦ 按质检建议重生成永不被跳过
# ---------------------------------------------------------------------------

def test_qc_regenerate_is_never_skipped(client, course_id, monkeypatch):
    _prepared_course(client, course_id, titles=["多菌共生体系"])
    llm = _MeteredLLM()
    _use(monkeypatch, llm)
    item = _single(client, course_id, _node_id(client, course_id, "1.1"))["result"]["item"]
    calls_before = llm.calls

    resp = client.post(f"/api/lecture/{item['id']}/regenerate-qc", json={})
    assert resp.status_code == 200, resp.text
    state = _wait_job(client, resp.json()["job_id"])
    assert state["status"] == "success", state
    assert llm.calls > calls_before, "质检重生成不应被去重跳过"
    assert state["result"]["usage"]["llm_calls"] >= 1, state["result"].get("usage")


# ---------------------------------------------------------------------------
# 用量采集的边界：嵌套生成不重复记账
# ---------------------------------------------------------------------------

def test_nested_generation_does_not_double_count(client, course_id, monkeypatch):
    """讲义内部的概念图/卡片调用计入讲义这一篇，不额外挂到图表条目上。"""
    _prepared_course(client, course_id, titles=["多菌共生体系"])
    llm = _MeteredLLM()
    _use(monkeypatch, llm)
    state = _single(client, course_id, _node_id(client, course_id, "1.1"))
    lecture = state["result"]["item"]
    assert lecture["meta"]["usage"]["llm_calls"] >= 1

    rows = client.get(f"/api/content/{course_id}").json()
    diagrams = [r for r in rows if r["kind"] in ("diagram", "chart_bar", "chart_table")]
    assert diagrams, "讲义应带出概念图/卡片"
    assert all(not (r["meta"] or {}).get("usage") for r in diagrams), \
        "讲义内部生成的图不应单独挂用量（避免重复计费）"


# ---------------------------------------------------------------------------
# P9-3 / P3-4 / P6-2：大纲与概念抽取两段用量不再被静默丢弃
# ---------------------------------------------------------------------------

def test_outline_and_concept_usage_reach_course_tab(client, course_id, monkeypatch):
    """大纲生成 / 概念抽取原先没有采集器 → 页签漏报这两段（实测少报 18.5%）。

    断言：① 大纲这次调用立刻出现在「用量与成本」页签（llm_calls/tokens 不再为 0）；
    ② 概念抽取继续累加；③ 两段都带 job/接口级 `usage`（与讲义链路口径一致）。
    """
    llm = _MeteredLLM()
    monkeypatch.setattr(planner_mod, "build_llm_client", lambda: llm)
    monkeypatch.setattr(concept_mod, "build_llm_client", lambda: llm)

    plan = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "光合作用", "num_chapters": 2})
    assert plan.status_code == 200, plan.text
    assert plan.json()["usage"]["llm_calls"] == 1, plan.json().get("usage")

    usage = client.get(f"/api/analytics/{course_id}/usage").json()
    assert usage["llm_calls"] == 1, usage
    assert usage["total_tokens"] == 1500, usage
    assert usage["cost_cny"] == _expected_cost(1), usage
    outline_row = next(row for row in usage["items"] if row["item_id"] == "aux:outline")
    assert outline_row["kind"] == "outline"
    assert outline_row["by_purpose"]["outline"]["calls"] == 1

    _approve(client, course_id)
    resp = client.post(f"/api/concept/course/{course_id}/generate")
    assert resp.status_code == 200, resp.text
    state = _wait_job(client, resp.json()["job_id"])
    assert state["status"] == "success", state
    assert state["result"]["usage"]["llm_calls"] == 1, state["result"].get("usage")

    after = client.get(f"/api/analytics/{course_id}/usage").json()
    assert after["llm_calls"] == 2, after
    assert after["total_tokens"] == 3000, after
    assert after["cost_cny"] == _expected_cost(2), after
    assert {row["item_id"] for row in after["items"]} == {"aux:outline", "aux:concept"}
    concept_row = next(row for row in after["items"] if row["item_id"] == "aux:concept")
    assert concept_row["by_purpose"]["concept"]["calls"] == 1


# ---------------------------------------------------------------------------
# P9-4：aux（大纲/概念抽取）是「重跑覆盖」，页签要同时给当前版本与历史累计
# ---------------------------------------------------------------------------

def test_aux_rerun_history_keeps_overwritten_versions(client, course_id, monkeypatch):
    """重跑 3 次 → 当前版本 1 次（覆盖）+ 历史累计 3 次/三份费用。"""
    llm = _MeteredLLM()
    monkeypatch.setattr(planner_mod, "build_llm_client", lambda: llm)

    for _ in range(3):
        resp = client.post("/api/plan/generate", json={
            "course_id": course_id, "goal": "光合作用", "num_chapters": 2})
        assert resp.status_code == 200, resp.text

    usage = client.get(f"/api/analytics/{course_id}/usage").json()
    # 当前版本：只有最后一次（aux_usage 是覆盖写）
    assert usage["llm_calls"] == 1, usage
    assert {row["item_id"] for row in usage["items"]} == {"aux:outline"}
    # 历史累计：三次重跑都记着，费用是三份
    assert usage["history_aux_runs"] == 3, usage["aux_history"]
    assert usage["history_aux_tokens"] == 4500, usage["aux_history"]
    assert usage["history_aux_cost_cny"] == _expected_cost(3), usage["aux_history"]
    assert usage["history_aux_estimated"] is False
    row = usage["aux_history"]["by_purpose"][0]
    assert row["purpose"] == "outline" and row["runs"] == 3
    assert row["first_at"] and row["last_at"]


def test_aux_history_estimated_for_legacy_courses(client, course_id, monkeypatch):
    """没有历史账本的老课程：按「当前版本 = 至少 1 次」给下限并标 estimated。"""
    llm = _MeteredLLM()
    monkeypatch.setattr(planner_mod, "build_llm_client", lambda: llm)
    assert client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "光合作用", "num_chapters": 2}
    ).status_code == 200

    # 模拟本页签上线前的老数据：只有 aux_usage，没有历史账本
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        meta = dict(course.meta or {})
        meta.pop("aux_usage_history", None)
        course.meta = meta
        db.commit()
    finally:
        db.close()

    usage = client.get(f"/api/analytics/{course_id}/usage").json()
    assert usage["history_aux_runs"] == 1, usage["aux_history"]
    assert usage["history_aux_cost_cny"] == _expected_cost(1)
    assert usage["history_aux_estimated"] is True
