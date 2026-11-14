"""任务 117：模型配置（`LLM_PROVIDER` + 对应 Key）的探测、缺 Key 的人话拒绝、Key 不外泄。

覆盖任务书 §二.A 四条：
① 「有没有可用的模型配置」进**既有** `/api/health.capabilities`（不新造第二套端点/判定），
   且**启动时**就打一行结论（不等到第一次生成才炸）；
② 缺 Key 时**不花钱的功能照常可用**，只有**要调模型的动作**给**人话拒绝**（同步路由 503、异步任务写
   `job.error`），⛔ 不是 500、⛔ 也不是静默退回 mock；
③ Key 只从 `backend/.env` 读，**不进日志、不进接口返回**（返回里只有变量名，连前缀都不给）；
④ 多用户各自 Key = 本轮不做（口径写在 `docs/task117-keys-and-backup.md`）。

测试全程用临时五件套（conftest 已把 DATABASE_URL / STORAGE_DIR / OBSIDIAN_VAULT_ROOT /
PPT_WORK_DIR / EXPORT_DIR 全指到临时目录），不碰真库与真知识库，也不发任何网络请求。
"""
from __future__ import annotations

import logging
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.services import capabilities as cap
from app.services import llm as llm_mod
from app.services.llm import LLMConfigError, MockLLMClient

# 只在测试里存在的「金丝雀」Key：任何接口返回、任何日志、任何文案里都不许出现它（含前缀）。
CANARY_KEY = "sk-canary-117-do-not-leak-9f3a"
CANARY_PREFIX = CANARY_KEY[:12]

PROVIDER_KEY_FIELDS = ("openai_api_key", "anthropic_api_key", "openrouter_api_key")


def _set_config(monkeypatch, provider: str,
                key_field: str | None = None, key: str | None = None) -> None:
    """把当前配置改成「provider + 某个 Key」；没点名的 Key 一律清空（不受 .env 影响）。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_provider", provider)
    for name in PROVIDER_KEY_FIELDS:
        monkeypatch.setattr(settings, name, key if name == key_field else None)


def _llm_item() -> dict:
    report = cap.capability_report()
    return next(item for item in report["items"] if item["key"] == cap.LLM_KEY)


def _strings(node):
    """把一段 JSON（dict/list/str）里所有字符串摊平，用于「扫一遍返回值」。"""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield str(key)
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _new_course(client) -> str:
    resp = client.post("/api/course/", json={
        "title": f"模型配置用例-{uuid4().hex[:6]}", "subject": "生物",
        "audience": "初中生", "duration_minutes": 90})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


# ---------------------------------------------------------------- ① 探测

def test_capability_agrees_with_the_decision_really_in_use():
    """探测与 `build_llm_client()` 用的是同一个判定（不另写第二套）。"""
    item = _llm_item()
    assert item["provider"] == llm_mod.normalized_provider()
    assert item["available"] is (llm_mod.llm_config_problem() is None)
    assert (item["reason"] is None) is item["available"]
    assert item["affects"] and item["unaffected"] and item["install"] and item["probe"]
    assert "exe" not in item  # 模型配置没有「可执行文件」这一说


def test_mock_stays_zero_config(monkeypatch):
    """默认 mock 档位：可用、不需要 Key、也不调真模型（这条老路一个字没改）。"""
    _set_config(monkeypatch, "mock")
    item = _llm_item()
    assert item["available"] is True and item["mock"] is True and item["reason"] is None
    assert item["needs_key"] == ""
    assert isinstance(llm_mod.build_llm_client(), MockLLMClient)


def test_missing_key_gives_human_reason_and_where_to_fill(monkeypatch):
    """② 显式选了 openrouter 却没配 Key ⇒ 不可用 + 人话（缺哪个变量、去哪填）。"""
    _set_config(monkeypatch, "openrouter")
    item = _llm_item()
    assert item["available"] is False
    assert item["provider"] == "openrouter"
    assert item["needs_key"] == "OPENROUTER_API_KEY"
    assert "OPENROUTER_API_KEY" in item["reason"]
    assert "不是你的课有问题" in item["reason"]
    assert ".env" in item["install"]
    assert any("讲义" in row for row in item["affects"])
    assert any("交付包" in row for row in item["unaffected"])
    report = cap.capability_report()
    assert cap.LLM_KEY in report["missing"] and report["hint"]


def test_unknown_provider_is_reported_not_silently_mocked(monkeypatch):
    """provider 名打错同样算「配置没弄好」（以前会静默走 mock）。"""
    _set_config(monkeypatch, "opena")
    item = _llm_item()
    assert item["available"] is False
    assert "LLM_PROVIDER" in item["reason"] and "mock" in item["reason"]
    with pytest.raises(LLMConfigError):
        llm_mod.build_llm_client()


def test_missing_key_raises_human_error_instead_of_mock(monkeypatch):
    """② `build_llm_client()` 缺 Key 时**抛人话**，不再静默给 MockLLMClient。"""
    _set_config(monkeypatch, "anthropic")
    with pytest.raises(LLMConfigError) as err:
        llm_mod.build_llm_client()
    message = str(err.value)
    assert "ANTHROPIC_API_KEY" in message and "不是你的课有问题" in message
    payload = err.value.as_payload()
    assert payload["code"] == "llm_not_configured"
    assert payload["env_var"] == "ANTHROPIC_API_KEY"
    assert ".env" in payload["hint"]


# ---------------------------------------------------------------- ② 缺 Key 时的行为

def test_paid_action_is_refused_with_503_not_500(client, monkeypatch):
    """② 同步路由（生成大纲）：缺 Key ⇒ **503 人话**，不是 500。"""
    _set_config(monkeypatch, "openrouter")
    course_id = _new_course(client)
    resp = client.post("/api/plan/generate",
                       json={"course_id": course_id, "goal": "让初中生理解光合作用"})
    assert resp.status_code != 500, resp.text          # 反证据：写成 500 时这条会红
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["code"] == "llm_not_configured"
    assert "OPENROUTER_API_KEY" in body["detail"]
    assert "不是你的课有问题" in body["detail"]
    assert CANARY_KEY not in resp.text


def test_free_features_still_work_without_key(client, monkeypatch):
    """② 不花钱的功能照常：健康检查 / 课程列表 / 课程详情都 200。"""
    _set_config(monkeypatch, "openai")
    course_id = _new_course(client)
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/course/").status_code == 200
    assert client.get(f"/api/course/{course_id}").status_code == 200


def test_background_job_fails_with_the_same_human_message(client, monkeypatch):
    """② 异步路由（生成讲义）：缺 Key ⇒ job 以**同一句人话**失败，不是静默成功出假内容。"""
    course_id = _new_course(client)
    # 先用默认 mock 档位备好「大纲已通过 + 有一个知识点」（这一步不是本轮要测的点）
    ok = client.post("/api/plan/generate",
                     json={"course_id": course_id, "goal": "让初中生理解光合作用"})
    assert ok.status_code == 200, ok.text
    approved = client.post("/api/plan/approve",
                           json={"course_id": course_id, "approved": True})
    assert approved.status_code == 200, approved.text
    nodes = client.get(f"/api/plan/{course_id}").json()["nodes"]
    assert nodes, "大纲里至少要有一个节点才能测「生成讲义」"
    node_id = nodes[0]["id"]

    _set_config(monkeypatch, "openrouter")            # 现在把模型配置弄坏
    resp = client.post("/api/lecture/generate",
                       json={"course_id": course_id, "node_id": node_id, "tone": "正式"})
    assert resp.status_code == 200, resp.text         # 入队本身不拦，拒绝发生在真调模型时
    job_id = resp.json()["job_id"]
    state = None
    for _ in range(200):
        state = client.get(f"/api/content/jobs/{job_id}").json()
        if state["status"] in ("success", "failed"):
            break
        time.sleep(0.1)
    assert state and state["status"] == "failed", state
    assert "OPENROUTER_API_KEY" in state["error"]
    assert "不是你的课有问题" in state["error"]


# ---------------------------------------------------------------- ③ Key 不外泄

def test_key_never_leaks_into_health_response_or_logs(client, monkeypatch, caplog):
    """③ 扫一遍 `/api/health` 返回值与日志：只有变量名，没有 Key（连前缀都没有）。"""
    _set_config(monkeypatch, "openrouter", "openrouter_api_key", CANARY_KEY)
    with caplog.at_level(logging.DEBUG):
        resp = client.get("/api/health")
    assert resp.status_code == 200

    payload = resp.json()
    item = next(i for i in payload["capabilities"]["items"] if i["key"] == cap.LLM_KEY)
    assert item["available"] is True
    assert item["needs_key"] == "OPENROUTER_API_KEY"
    assert "OPENROUTER_API_KEY" in resp.text          # 变量名可以出现

    for value in _strings(payload):                   # 整个返回体逐字段扫
        assert CANARY_KEY not in value, value
        assert CANARY_PREFIX not in value, value
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert CANARY_KEY not in logs and CANARY_PREFIX not in logs


def test_startup_line_states_the_model_config_without_the_key(monkeypatch, caplog):
    """① 启动时就给结论（不是等第一次生成才炸），同样只说变量名。"""
    _set_config(monkeypatch, "openrouter", "openrouter_api_key", CANARY_KEY)
    with caplog.at_level(logging.INFO, logger="app.main"):
        with TestClient(app) as c:
            assert c.get("/api/health").status_code == 200
    messages = " ".join(r.getMessage() for r in caplog.records if r.name == "app.main")
    assert "模型配置" in messages and "OPENROUTER_API_KEY" in messages
    assert CANARY_KEY not in messages and CANARY_PREFIX not in messages


def test_config_summary_is_key_free_in_both_states(monkeypatch):
    """① 同一处文案（摘要/能力/异常共用）：配好与没配好两种情况都不含 Key。"""
    _set_config(monkeypatch, "openrouter", "openrouter_api_key", CANARY_KEY)
    ok_summary = llm_mod.llm_config_summary()
    assert "OPENROUTER_API_KEY" in ok_summary
    assert CANARY_KEY not in ok_summary and CANARY_PREFIX not in ok_summary

    _set_config(monkeypatch, "openrouter")
    bad_summary = llm_mod.llm_config_summary()
    assert "OPENROUTER_API_KEY" in bad_summary and ".env" in bad_summary