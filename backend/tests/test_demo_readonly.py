"""任务 48u：只读演示模式（DEMO_READONLY）—— 对外演示/穿透的默认安全档位。

给「内网穿透 / 公网给人看」用：开了 DEMO_READONLY 后，放行面只有
GET/HEAD/OPTIONS 的 `/api/public/*`（只读分享）与 `/api/health`，
其余接口（工作台读写、导出、交付包下载、静态目录）一律 403。
**不依赖反代规则配置正确** —— 反代配错时兜底的那一层。

健康检查另外带 `auth_enabled` / `demo_readonly` 两个开关，前端据此决定
「要不要登录页」「要不要只渲染分享页」。
"""
from __future__ import annotations

from app.core.config import get_settings


def _demo_on(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "demo_readonly", True)


def _make_course(client, title: str = "演示模式探针课") -> str:
    resp = client.post("/api/course/", json={"title": title, "subject": "生物",
                                             "audience": "初中生", "duration_minutes": 90})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _share_token(client, course_id: str) -> str:
    resp = client.post(f"/api/share/course/{course_id}",
                       json={"label": "给客户", "base_url": "http://localhost:5173"})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def test_health_reports_both_switches(client):
    """默认单机档位：不鉴权、不是演示模式（前端据此直接进工作台）。"""
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["auth_enabled"] is False
    assert body["demo_readonly"] is False


def test_demo_mode_blocks_workspace_reads_and_writes(client, monkeypatch):
    """演示模式下工作台读写全挡：GET 列表 / POST 建课 / POST 生成 都是 403。"""
    course_id = _make_course(client)
    _demo_on(monkeypatch)

    blocked = [
        client.get("/api/course/"),
        client.post("/api/course/", json={"title": "演示模式不该建出来的课"}),
        client.post("/api/lecture/generate", json={"course_id": course_id, "node_ids": []}),
        client.get(f"/api/delivery/course/{course_id}"),
        client.get("/api/analytics/course-not-exist/usage"),
    ]
    for resp in blocked:
        assert resp.status_code == 403, (resp.request.url, resp.status_code, resp.text[:120])
    assert "只读分享" in client.get("/api/course/").json()["detail"]


def test_demo_mode_blocks_course_creation_persists_nothing(client, monkeypatch):
    """403 是真的没进业务层：演示模式下建课请求过后，库里课程数不变。"""
    before = len(client.get("/api/course/").json())
    _demo_on(monkeypatch)
    assert client.post("/api/course/", json={"title": "不该落库"}).status_code == 403
    monkeypatch.setattr(get_settings(), "demo_readonly", False)
    assert len(client.get("/api/course/").json()) == before


def test_demo_mode_blocks_other_api_and_static_downloads(client, monkeypatch):
    """其它路径（含静态导出目录）也挡：演示模式只认分享页那两条。"""
    _demo_on(monkeypatch)
    assert client.get("/api/auth/x").status_code == 403
    assert client.get("/exports/演示包.zip").status_code == 403
    assert client.get("/images/x.png").status_code == 403
    assert client.get("/ppt-files/x.png").status_code == 403


def test_demo_mode_public_share_still_works(client, monkeypatch):
    """演示模式 + 分享链接：share 页与 /api/public/* 必须照常可读。"""
    course_id = _make_course(client)
    token = _share_token(client, course_id)
    _demo_on(monkeypatch)

    assert client.get("/api/health").status_code == 200
    overview = client.get(f"/api/public/{token}")
    assert overview.status_code == 200, overview.text
    assert overview.json()["course"]["id"] == course_id
    assert client.get(f"/api/public/{token}/content").status_code == 200
    # 公开共享的图片前缀仍能取（分享页要显示幻灯片图）
    assert client.get(f"/api/public/{token}/images/nope.png").status_code == 404

    # 无效 token 给的是「分享自己」的错误，不是中间件的 403 ⇒ 说明这条路径真的放行了
    bad = client.get("/api/public/not-a-real-token")
    assert bad.status_code == 403
    assert "只读分享" not in bad.text


def test_demo_mode_off_by_default_keeps_everything_working(client):
    """不开开关时一行代码都不影响（默认单机使用）。"""
    course_id = _make_course(client)
    assert client.get("/api/course/").status_code == 200
    assert client.get(f"/api/course/{course_id}").status_code == 200
