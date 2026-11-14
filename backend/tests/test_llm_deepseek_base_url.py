"""任务 150：`LLM_PROVIDER=openai` 可指向 DeepSeek 官方（OpenAI 兼容协议）。"""
from __future__ import annotations

from app.core.config import get_settings
from app.services import llm as llm_mod
from app.services.llm import MockLLMClient


def _build_with_env(monkeypatch, *, base_url: str | None):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    if base_url is None:
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("OPENAI_BASE_URL", base_url)
    get_settings.cache_clear()
    try:
        return llm_mod.build_llm_client()
    finally:
        get_settings.cache_clear()


def test_openai_base_url_env_points_client_to_deepseek(monkeypatch):
    client = _build_with_env(monkeypatch, base_url="https://api.deepseek.com")
    assert client.base_url.startswith("https://api.deepseek.com")


def test_openai_base_url_defaults_to_openai(monkeypatch):
    client = _build_with_env(monkeypatch, base_url=None)
    assert client.base_url == "https://api.openai.com/v1"


def test_mock_provider_ignores_openai_base_url(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    get_settings.cache_clear()
    try:
        assert isinstance(llm_mod.build_llm_client(), MockLLMClient)
    finally:
        get_settings.cache_clear()
