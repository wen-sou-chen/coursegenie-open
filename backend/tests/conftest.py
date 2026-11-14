"""测试夹具：独立临时 SQLite + Mock LLM + 强制 PPT 兜底。"""
from __future__ import annotations

import os
import tempfile
import time

_tmp = tempfile.mkdtemp(prefix="coursegenie_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["STORAGE_DIR"] = f"{_tmp}/storage"
os.environ["EXPORT_DIR"] = f"{_tmp}/storage/exports"
os.environ["PPT_WORK_DIR"] = f"{_tmp}/storage/ppt"
os.environ["LLM_PROVIDER"] = "mock"
os.environ["IMAGE_PROVIDER"] = "mock"  # 隔离 .env 的真实图像 provider，测试不触网
os.environ["AUTH_ENABLED"] = "false"
os.environ["DASHI_PPT_FORCE_FALLBACK"] = "true"
os.environ["PPT_SLIDE_IMAGES"] = "false"  # 测试里不启动 PowerPoint；图片用例自行 monkeypatch
# 协作规则第 7 条：测试期间 DATABASE_URL / STORAGE_DIR / OBSIDIAN_VAULT_ROOT
# 三者必须全部指向临时目录（历史上漏了 STORAGE_DIR 导致真实配图被连带删除）。
os.environ["OBSIDIAN_VAULT_ROOT"] = f"{_tmp}/vault"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db.base import Base  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _init_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def no_global_vault(monkeypatch):
    """把全局 vault 也清空（平时指向临时目录）：用于测「没绑定 → 默认目录」这类路径。"""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "obsidian_vault_root", "")


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def course_id(client) -> str:
    resp = client.post("/api/course/", json={"title": "光合作用入门", "subject": "生物",
                                             "audience": "初中生", "duration_minutes": 90})
    assert resp.status_code == 200
    return resp.json()["id"]


@pytest.fixture()
def gen_lecture_row(client):
    """POST /api/lecture/generate 已入队化：发请求 → 轮询 job → 返回生成的行。"""

    def _gen(course_id: str, node_id: str, tone: str = "正式",
             item_id: str | None = None, **extra) -> dict:
        payload = {"course_id": course_id, "node_id": node_id, "tone": tone}
        if item_id:
            payload["item_id"] = item_id
        payload.update(extra)
        resp = client.post("/api/lecture/generate", json=payload)
        assert resp.status_code == 200, resp.text
        queued = resp.json()
        assert "job_id" in queued and queued.get("status") == "running", queued
        state = None
        for _ in range(120):
            state = client.get(f"/api/content/jobs/{queued['job_id']}").json()
            if state["status"] in ("success", "failed"):
                break
            time.sleep(0.1)
        assert state is not None and state["status"] == "success", state
        return state["result"]["item"]

    return _gen
