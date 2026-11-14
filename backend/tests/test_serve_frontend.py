"""任务 115：后端托管前端 dist + SPA 回退（让别人「只起一个进程」就能用整个应用）。

六条覆盖（任务书 §A.5）：
① dist 存在 ⇒ ``/`` 返回 index.html；② 深链（``/share/<token>``）走 SPA 回退 ⇒ index.html；
③ ``/api/*`` 未被影响（含没人认领的 ``/api/*`` 仍是 404 JSON）；
④ 已挂载资源（``/exports``、``/images``、``/ppt-files``）不被回退吃掉；
⑤ dist 缺失 ⇒ 根路径给「怎么构建」的人话，API 照常；
⑥ 关掉开关（``SERVE_FRONTEND=false``）⇒ 回到纯 API 行为。

每个用例都用 ``create_app()`` 现造一个应用、只改前端相关的两个设置，
其它设置沿用 ``conftest.py`` 的临时五件套（真库 / 真 storage / 真 vault 一律不碰）。
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.main import FRONTEND_BUILD_HINT, create_app

MARKER = "SPA-INDEX-MARKER-115"


def _make_dist(root: Path) -> Path:
    """造一份最小「已构建」产物：index.html + 一个 assets/*.js + favicon。"""
    dist = root / "frontend" / "dist"
    (dist / "assets").mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text(
        f'<!doctype html><html><body><div id="root">{MARKER}</div></body></html>',
        encoding="utf-8",
    )
    (dist / "assets" / "app.js").write_text("export const ok = 1;\n", encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    return dist


@contextmanager
def _app(monkeypatch, *, dist: Path, serve: bool = True):
    settings = get_settings()
    monkeypatch.setattr(settings, "serve_frontend", serve)
    monkeypatch.setattr(settings, "frontend_dist_dir", str(dist))
    with TestClient(create_app()) as client:
        yield client


def test_root_serves_built_index(tmp_path, monkeypatch):
    """① dist 存在：`/` 直出 index.html；`/assets/*.js` 走真实文件且是浏览器认的 JS MIME。"""
    dist = _make_dist(tmp_path)
    with _app(monkeypatch, dist=dist) as client:
        root = client.get("/")
        assert root.status_code == 200, root.text
        assert MARKER in root.text
        assert root.headers["content-type"].startswith("text/html")

        js = client.get("/assets/app.js")
        assert js.status_code == 200
        assert js.text.startswith("export const ok")
        # Windows 注册表会把 .js 猜成 text/plain ⇒ <script type="module"> 会被浏览器拒绝执行
        assert js.headers["content-type"].split(";")[0] == "text/javascript"

        assert client.get("/favicon.svg").status_code == 200


def test_deep_links_fall_back_to_index(tmp_path, monkeypatch):
    """② 深链刷新/直链不再 404：非 `/api/*`、非已挂载资源的 GET 一律回 index.html。"""
    dist = _make_dist(tmp_path)
    with _app(monkeypatch, dist=dist) as client:
        for path in ("/share/AbC123token", "/share/AbC123token/", "/courses/abc/plan",
                     "/workbench", "/a/b/c/d"):
            resp = client.get(path)
            assert resp.status_code == 200, (path, resp.status_code)
            assert MARKER in resp.text, path
            assert resp.headers["content-type"].startswith("text/html")


def test_api_routes_untouched(tmp_path, monkeypatch):
    """③ 接口照常：`/api/health`、`/docs`、`/openapi.json` 都在；没人认领的 /api 仍是 404 JSON。"""
    dist = _make_dist(tmp_path)
    with _app(monkeypatch, dist=dist) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        body = health.json()
        assert body["ok"] is True and body["auth_enabled"] is False
        assert body["frontend"] == {"enabled": True, "dist": str(dist), "hint": None}

        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200

        missing = client.get("/api/no-such-route")
        assert missing.status_code == 404
        assert missing.headers["content-type"].startswith("application/json")
        assert missing.json() == {"detail": "Not Found"}
        assert MARKER not in missing.text
        # 非 GET 也不许被前端接管（拿到的不是 index.html，也绝不能是 200 的页面）
        not_get = client.post("/api/no-such-route")
        assert not_get.status_code in (404, 405)
        assert MARKER not in not_get.text


def test_non_get_on_registered_route_still_405(tmp_path, monkeypatch):
    """回归护栏：兜底路由**只认 GET/HEAD**。

    一开始写成「吃下所有方法」，结果把 Starlette 的 405 顶掉了 ——
    `POST /api/public/<token>` 从 405 变 404，打红了既有用例
    `tests/test_share.py::test_public_side_is_read_only`。这条钉住这个坑。
    """
    dist = _make_dist(tmp_path)
    with _app(monkeypatch, dist=dist) as client:
        for method, path in (("put", "/api/health"), ("post", "/openapi.json"),
                             ("delete", "/api/course/"), ("patch", "/api/health")):
            resp = getattr(client, method)(path)
            assert resp.status_code == 405, (method, path, resp.status_code)
            assert MARKER not in resp.text


def test_mounted_dirs_not_eaten_by_spa_fallback(tmp_path, monkeypatch):
    """④ 已挂载的三个资源目录照旧出文件（不是 HTML），缺失文件仍 404。"""
    dist = _make_dist(tmp_path)
    settings = get_settings()
    (settings.storage_dir / "images").mkdir(parents=True, exist_ok=True)
    settings.export_dir.mkdir(parents=True, exist_ok=True)
    settings.ppt_work_dir.mkdir(parents=True, exist_ok=True)
    (settings.export_dir / "task115-proof.zip").write_bytes(b"PK\x03\x04export-file")
    (settings.storage_dir / "images" / "task115-proof.svg").write_text("<svg>img</svg>", encoding="utf-8")
    (settings.ppt_work_dir / "task115-proof.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    with _app(monkeypatch, dist=dist) as client:
        exported = client.get("/exports/task115-proof.zip")
        assert exported.status_code == 200
        assert exported.content == b"PK\x03\x04export-file"
        assert MARKER not in exported.text

        image = client.get("/images/task115-proof.svg")
        assert image.status_code == 200 and "img" in image.text and MARKER not in image.text

        slide = client.get("/ppt-files/task115-proof.png")
        assert slide.status_code == 200 and slide.content.startswith(b"\x89PNG")

        # 目录里没有的文件 ⇒ 还是 404（没被 SPA 回退接管成 index.html）
        for path in ("/exports/no-such-file.zip", "/images/no-such-image.png",
                     "/ppt-files/no-such-slide.png"):
            resp = client.get(path)
            assert resp.status_code == 404, path
            assert MARKER not in resp.text, path


def test_missing_dist_gives_human_hint_and_keeps_api(tmp_path, monkeypatch):
    """⑤ dist 缺失：根路径给人话（不是 500），API 一律照常。"""
    with _app(monkeypatch, dist=tmp_path / "not-built-yet") as client:
        root = client.get("/")
        assert root.status_code == 503  # 不是 500：前端没构建不该看起来像「服务炸了」
        assert "npm run build" in root.text
        assert "frontend/dist" in root.text

        deep = client.get("/share/whatever")
        assert deep.status_code == 503 and "npm run build" in deep.text

        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["ok"] is True
        assert health.json()["frontend"] == {
            "enabled": True, "dist": None, "hint": FRONTEND_BUILD_HINT,
        }
        assert "npm run build" in health.json()["frontend"]["hint"]


def test_switch_off_returns_pure_api(tmp_path, monkeypatch):
    """⑥ SERVE_FRONTEND=false ⇒ 回到改造前那套纯 API 行为（一行都不挂）。"""
    dist = _make_dist(tmp_path)
    with _app(monkeypatch, dist=dist, serve=False) as client:
        for path in ("/", "/share/tok", "/assets/app.js"):
            resp = client.get(path)
            assert resp.status_code == 404, (path, resp.status_code)
            assert MARKER not in resp.text, path
        assert client.get("/api/health").json()["frontend"] == {
            "enabled": False, "dist": None, "hint": None,
        }


def test_switch_is_a_real_config_item(monkeypatch):
    """开关与目录都走配置（.env / 环境变量），不是写死的常量。"""
    monkeypatch.setenv("SERVE_FRONTEND", "false")
    assert Settings().serve_frontend is False
    monkeypatch.setenv("SERVE_FRONTEND", "true")
    assert Settings().serve_frontend is True
    assert Settings().frontend_dist_dir == ""
    monkeypatch.setenv("FRONTEND_DIST_DIR", "C:/somewhere/dist")
    assert Settings().frontend_dist_dir == "C:/somewhere/dist"


def test_serve_frontend_default_is_on(monkeypatch):
    """任务 116：默认值必须是**开**，「只起一个后端就能用整个界面」才成立。

    这是**产品口径**，不是随手默认：DSH 2026-10-01 复验 115 时把实现里的 `True` 改成 `False`，
    9 条用例照样全绿 ⇒ 说明没人看着这个默认值。这条就是那个看门人：
    ① 模型字段的默认值本身是 True；② 没有环境变量 / `.env` 覆盖时，解析出来的也是 True。
    """
    monkeypatch.delenv("SERVE_FRONTEND", raising=False)
    assert Settings.model_fields["serve_frontend"].default is True
    assert Settings(_env_file=None).serve_frontend is True


def test_static_cache_headers(tmp_path, monkeypatch):
    """⑦ 缓存头：index.html 必须 no-store，带 hash 的 assets/* 才长缓存。

    2026-10-21 演示站实测的坑：index.html 原来**一个缓存头都没有**，浏览器按
    Last-Modified 启发式缓存；重新部署（换 hash 资源名）后旧 index.html 仍指向已删掉的
    JS ⇒ 返回的访客直接白屏（我自己就踩了一次），所以这条钉住它。
    """
    dist = _make_dist(tmp_path)
    with _app(monkeypatch, dist=dist) as client:
        root = client.get("/")
        assert "no-store" in root.headers["cache-control"]
        # SPA 回退出来的 index.html 同样 no-store（深链刷新也拿新的）
        deep = client.get("/share/tok123")
        assert "no-store" in deep.headers["cache-control"]

        js = client.get("/assets/app.js")
        cache = js.headers["cache-control"]
        assert "immutable" in cache and "31536000" in cache

        # 非 assets 的静态文件（favicon 等）短缓存，但不许 immutable
        fav = client.get("/favicon.svg")
        assert "immutable" not in fav.headers["cache-control"]


def test_explicit_dist_dir_wins_and_no_guessing(tmp_path, monkeypatch):
    """显式指了 dist 目录就以它为准：那里没产物就按「前端未构建」处理，不回头去猜仓库里的。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "frontend_dist_dir", str(tmp_path / "empty"))
    assert settings.resolved_frontend_dist is None
