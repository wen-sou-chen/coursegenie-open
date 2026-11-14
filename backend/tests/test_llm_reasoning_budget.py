"""任务 151：推理 token 计入 max_tokens 时，DeepSeek 官方直连的空 content 守卫。"""
from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import get_settings
from app.services import llm as llm_mod
from app.services.lecture import _failure_reason


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _build_with_env(monkeypatch, **env):
    for key, value in {
        "LLM_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-test-not-a-real-key",
        **env,
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    return llm_mod.build_llm_client()


def _capture_payload(monkeypatch, *, content: str = "ok",
                     finish_reason: str = "stop", completion_tokens: int = 1):
    payloads: list[dict] = []

    def fake_post(url, *, payload, headers, timeout, retries=4, on_retry=None):
        payloads.append(payload)
        return httpx.Response(200, json={
            "choices": [{
                "message": {"content": content},
                "finish_reason": finish_reason,
            }],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": completion_tokens,
            },
        }), []

    monkeypatch.setattr(llm_mod, "_post_retry", fake_post)
    return payloads


def test_disable_thinking_adds_official_deepseek_field(monkeypatch):
    client = _build_with_env(monkeypatch, OPENAI_DISABLE_THINKING="true")
    payloads = _capture_payload(monkeypatch)

    assert client.complete("system", "user") == "ok"
    assert payloads[0]["thinking"] == {"type": "disabled"}


def test_thinking_field_is_opt_in_and_never_sent_to_openrouter(monkeypatch):
    payloads = _capture_payload(monkeypatch)
    default_client = _build_with_env(monkeypatch, OPENAI_DISABLE_THINKING="false")
    default_client.complete("system", "user")
    assert "thinking" not in payloads[0]

    openrouter_client = _build_with_env(
        monkeypatch,
        LLM_PROVIDER="openrouter",
        OPENROUTER_API_KEY="sk-test-not-a-real-key",
        OPENAI_DISABLE_THINKING="true",
    )
    openrouter_client.complete("system", "user")
    assert "thinking" not in payloads[1]


def test_empty_content_raises_readable_reasoning_error(monkeypatch):
    client = _build_with_env(monkeypatch, OPENAI_DISABLE_THINKING="false")
    _capture_payload(monkeypatch, content="", finish_reason="length",
                     completion_tokens=4000)

    with pytest.raises(RuntimeError) as exc_info:
        client.complete_json("OUTLINE_JSON", "长讲义", max_tokens=4000)

    message = str(exc_info.value)
    assert "finish_reason=length" in message
    assert "completion_tokens=4000" in message
    assert "max_tokens=4000" in message
    assert "推理" in message
    assert "JSON" not in message

def _serve_content(monkeypatch, *, content: str, finish_reason: str = "stop",
                   completion_tokens: int = 1):
    """让 OpenAI 客户端拿到指定的 content（不触网）。"""
    def fake_post(url, *, payload, headers, timeout, retries=4, on_retry=None):
        return httpx.Response(200, json={
            "choices": [{
                "message": {"content": content},
                "finish_reason": finish_reason,
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": completion_tokens},
        }), []

    monkeypatch.setattr(llm_mod, "_post_retry", fake_post)


def test_empty_content_surfaces_as_503_detail(client, course_id, monkeypatch):
    """任务 151b：空 content 的可读原因必须走 HTTP 503 + detail，而不是裸 500。"""
    _build_with_env(monkeypatch, OPENAI_DISABLE_THINKING="false")
    _serve_content(monkeypatch, content="", finish_reason="length",
                   completion_tokens=3000)

    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "让初中生理解光合作用",
        "num_chapters": 3, "style": "正式",
    })

    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    for needle in ("finish_reason=", "completion_tokens=", "推理"):
        assert needle in detail, detail
    assert "Traceback" not in detail


def test_truncated_json_surfaces_as_503_detail(client, course_id, monkeypatch):
    """任务 151c：截断的 JSON 也要 503 + 可读 detail，不能是裸 500。"""
    _build_with_env(monkeypatch, OPENAI_DISABLE_THINKING="false")
    truncated = ('{"title": "光合作用", "nodes": [{"key": "1", '
                 '"node_type": "chapter", "title": "第1章')
    _serve_content(monkeypatch, content=truncated, finish_reason="length",
                   completion_tokens=3000)

    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "让初中生理解光合作用",
        "num_chapters": 3, "style": "正式",
    })

    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    for needle in ("LLM 返回了坏 JSON", "位置", "现场",
                   "finish_reason=length", "OPENAI_DISABLE_THINKING=true"):
        assert needle in detail, detail
    assert "Traceback" not in detail


def test_normal_content_still_returns_200(client, course_id, monkeypatch):
    """空 content 的 handler 不能把正常路径也改成 503。"""
    _build_with_env(monkeypatch, OPENAI_DISABLE_THINKING="true")
    outline = {
        "title": "光合作用入门",
        "goal": "让初中生理解光合作用",
        "nodes": [{
            "key": "1", "node_type": "chapter", "title": "第1章 光合作用基础",
            "points": [{
                "key": "1.1", "node_type": "point",
                "title": "光合作用的过程",
            }],
        }],
    }
    _serve_content(monkeypatch, content=json.dumps(outline, ensure_ascii=False),
                   finish_reason="stop", completion_tokens=120)

    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "让初中生理解光合作用",
        "num_chapters": 3, "style": "正式",
    })

    assert resp.status_code == 200, resp.text
    assert len(resp.json()["nodes"]) == 2


def test_truncated_json_keeps_llm_json_classification():
    """151c 护栏：专属子类仍按 JSONDecodeError 归类，讲义失败原因还是 llm-json。"""
    with pytest.raises(llm_mod.LLMJsonError) as exc_info:
        llm_mod.extract_json('{"title": "光合作用", "nodes": [')

    exc = exc_info.value
    assert isinstance(exc, json.JSONDecodeError)
    stage, reason = _failure_reason(exc)
    assert stage == "llm-json"
    assert "位置" in reason and "现场" in reason
