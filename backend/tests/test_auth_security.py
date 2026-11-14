# -*- coding: utf-8 -*-
"""P0-1 认证加固：dev key 必填非默认、依赖放行/拦截行为。"""
from __future__ import annotations

import pytest
from fastapi import HTTPException


def test_auth_enabled_requires_non_default_dev_api_key():
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError) as exc_info:
        Settings(auth_enabled=True, dev_api_key="dev-key-change-me")
    assert "DEV_API_KEY" in str(exc_info.value)


def test_auth_enabled_accepts_custom_dev_api_key():
    from app.core.config import Settings

    s = Settings(auth_enabled=True, dev_api_key="k-please-change-99")
    assert s.auth_enabled is True
    assert s.dev_api_key == "k-please-change-99"


def test_auth_disabled_by_default_keeps_mvp_zero_config():
    from app.core.config import Settings

    s = Settings(auth_enabled=False, dev_api_key="dev-key-change-me")
    assert s.auth_enabled is False


def test_require_api_key_behavior(monkeypatch):
    from app.api import deps

    class _FakeSettings:
        auth_enabled = True
        dev_api_key = "k-test-key"

    monkeypatch.setattr(deps, "get_settings", lambda: _FakeSettings())
    with pytest.raises(HTTPException) as missing:
        deps.require_api_key(x_api_key=None)
    assert missing.value.status_code == 401
    assert deps.require_api_key(x_api_key="k-test-key") is None


def test_require_api_key_ignored_when_auth_disabled(monkeypatch):
    from app.api import deps

    class _FakeOff:
        auth_enabled = False
        dev_api_key = "dev-key-change-me"

    monkeypatch.setattr(deps, "get_settings", lambda: _FakeOff())
    assert deps.require_api_key(x_api_key=None) is None
