"""远程 MCP（streamable-http）的鉴权与握手回归。

把 `TokenAuthMiddleware` 的校验改松（例如去掉 compare_digest 或直接放行），
`test_initialize_requires_token` / `test_initialize_rejects_wrong_token` 必须变红。
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from course_generator import server as srv

TOKEN = "unit-test-token-0123456789abcdef0123456789"

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "0"},
    },
}


@pytest.fixture(scope="module")
def client():
    app = srv.TokenAuthMiddleware(srv.mcp.streamable_http_app(), TOKEN)
    with TestClient(app, base_url="http://127.0.0.1:8001") as test_client:
        yield test_client


def _post(client: TestClient, headers: dict[str, str] | None = None):
    return client.post(
        "/mcp",
        json=INITIALIZE,
        headers={"Accept": "application/json, text/event-stream", **(headers or {})},
    )


def test_healthz_needs_no_token(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_initialize_requires_token(client):
    response = _post(client)
    assert response.status_code == 401
    assert response.json()["ok"] is False


def test_initialize_rejects_wrong_token(client):
    response = _post(client, {"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401


def test_initialize_with_bearer_token(client):
    response = _post(client, {"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert srv.SERVER_NAME in response.text


def test_initialize_with_workbuddy_header(client):
    response = _post(client, {"X-WorkBuddy-Token": TOKEN})
    assert response.status_code == 200


def test_bad_authorization_scheme_is_ignored():
    scope = {"headers": [(b"authorization", b"Basic abc")]}
    assert srv._presented_token(scope) == ""


def test_remote_mode_refuses_short_token(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", "too-short")
    with pytest.raises(SystemExit) as excinfo:
        srv.main(["--transport", "streamable-http"])
    assert "MCP_AUTH_TOKEN" in str(excinfo.value)
