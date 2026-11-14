"""CourseGenie FastAPI 应用入口。"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import inspect

from app.api.routes import (
    analytics,
    audit,
    auth,
    chart,
    concept,
    content,
    courses,
    delivery,
    flowchart,
    image,
    kb,
    lecture,
    maintenance,
    material,
    plan,
    ppt,
    public,
    publish,
    quiz,
    share,
    versions,
)
from app.core.config import Settings, get_settings
from app.db.base import Base
from app.db.session import SessionLocal, engine, ensure_sqlite_columns
from app.services import capabilities, demo_mode
from app.services.llm import (
    LLMConfigError,
    LLMJsonError,
    LLMResponseError,
    llm_config_problem,
    llm_config_summary,
)
from app.services.jobs import reset_stale_jobs

logger = logging.getLogger(__name__)

DEMO_READONLY_HINT = (
    "演示模式（DEMO_READONLY=true）：只开放只读分享接口 /api/public/*，"
    "工作台读写、导出与交付包下载一律拒绝。"
)
# 任务 115：缺前端产物时给的人话（日志、/api/health、根路径提示页共用同一句）
FRONTEND_BUILD_HINT = (
    "前端未构建：请在 frontend 目录下执行 npm install && npm run build"
    "（或设 FRONTEND_DIST_DIR 指向已构建的 dist）"
)

# 任务 115：前端产物里必须按浏览器认的 MIME 出（Windows 注册表会把 .js 猜成 text/plain，
# 那样 <script type="module"> 会被浏览器拒绝执行 ⇒ 界面白屏）
FRONTEND_MEDIA_TYPES = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".html": "text/html",
    ".json": "application/json",
    ".map": "application/json",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".wasm": "application/wasm",
}

# 2026-10-21（演示站实测）：index.html 原来一个缓存头都没有 ⇒ 浏览器按 Last-Modified 启发式缓存，
# 重新部署后旧 index.html 仍指向**已删掉的 hash 资源**，返回的访客看到的是白屏。所以：
# - index.html / SPA 回退：no-store，每次都拿最新那份（它只有 1 KB）；
# - assets/* 带内容 hash：immutable 长缓存，重部署换文件名自然失效。
_NO_STORE_HEADERS = {"Cache-Control": "no-store, must-revalidate"}
_LONG_CACHE_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}


def _asset_cache_headers(full_path: str) -> dict[str, str]:
    """带 hash 的产物长缓存，其它（favicon 之类）短缓存 + 允许重验证。"""
    if full_path.startswith("assets/"):
        return _LONG_CACHE_HEADERS
    return {"Cache-Control": "public, max-age=3600"}

# 任务 115：dist 不存在时根路径给的提示页（不是 500，也不影响任何 API）
FRONTEND_MISSING_PAGE = """<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>CourseGenie · 前端还没构建</title>
    <style>
      body { font-family: system-ui, "Microsoft YaHei", sans-serif; line-height: 1.8;
             color: #333; max-width: 720px; margin: 8vh auto; padding: 0 16px; }
      code { background: #f2f2f2; padding: 2px 6px; border-radius: 4px; }
      a { color: #1677ff; }
    </style>
  </head>
  <body>
    <h1>前端还没构建</h1>
    <p>后端已经在跑了 —— 接口照常可用（<a href="/docs">/docs</a>、
       <a href="/api/health">/api/health</a>），只是没找到前端产物
       <code>frontend/dist</code>，所以这里显示不了界面。</p>
    <ol>
      <li>装一次 Node.js（20 以上）；</li>
      <li><code>cd frontend</code> → <code>npm install</code> → <code>npm run build</code>；</li>
      <li>刷新本页即可。</li>
    </ol>
    <p>已经构建在别处？设 <code>FRONTEND_DIST_DIR=...\\dist</code> 指向它；
       不需要后端托管（例如 Docker 里前端由 nginx 单独跑）就设
       <code>SERVE_FRONTEND=false</code>。</p>
  </body>
</html>
"""



@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # 开发便利：SQLite 启动自动建表（幂等，补齐新表）；生产环境使用 Alembic 迁移
    if settings.database_url.startswith("sqlite"):
        Base.metadata.create_all(bind=engine)
        # 任务 55：给既有 SQLite 库补新列（quiz_items.meta），幂等
        added = ensure_sqlite_columns()
        if added:
            logger.info("为既有 SQLite 库补齐新列：%s", "、".join(added))
    if not settings.auth_enabled:
        logger.warning(
            "AUTH_ENABLED=false：接口未鉴权。单机本机使用可接受；"
            "请勿把服务绑定到 0.0.0.0 暴露到局域网。"
        )
    # 任务 117：模型配置「有没有配好」在**启动时**就给结论（不等到第一次生成才炸）。
    # ⚠️ 只说变量名与后果，**永不打印 Key 的值 / 前缀 / 长度**（llm_config_summary 保证）。
    if llm_config_problem(settings) is None:
        logger.info("模型配置：%s", llm_config_summary(settings))
    else:
        logger.warning("模型配置有问题：%s", llm_config_summary(settings))
    # 任务可靠性：BackgroundTasks 只活在进程内存里，重启后把遗留的
    # pending/running 任务标记为 failed，避免 job 永久 running。
    db = SessionLocal()
    try:
        reset_stale_jobs(db)
    except Exception:  # noqa: BLE001 —— 恢复失败不阻塞启动
        logger.exception("启动时恢复遗留任务状态失败")
    finally:
        db.close()

    # 任务 154：单租户演示模式 —— 启动先清一次过期数据，之后后台周期清理。
    cleanup_task: asyncio.Task | None = None
    if settings.demo_only:
        if settings.demo_readonly:
            logger.warning("DEMO_ONLY=true 与 DEMO_READONLY=true 同时开启：只读档位优先，生成会被 403 挡住。")
        if settings.auth_enabled:
            logger.warning(
                "DEMO_ONLY=true 与 AUTH_ENABLED=true 同时开启：账号体系被关闭，"
                "所有人仍共用同一个匿名演示空间（AUTH_ENABLED 不提供多租户隔离）。"
            )
        logger.warning(
            "演示模式：单租户；课程/素材 %s 小时后自动清理；每 IP 每分钟 %s 次生成、"
            "每天 %s 门课、同时 %s 个生成任务。",
            settings.demo_retention_hours,
            settings.demo_max_generations_per_minute,
            settings.demo_max_courses_per_day,
            settings.demo_max_concurrent_jobs,
        )
        try:
            stats = await asyncio.to_thread(demo_mode.purge_once)
            app.state.demo_last_purge = stats
            if stats["courses"]:
                logger.info("演示模式：启动清理删除 %s 门过期课程", stats["courses"])
        except Exception:  # noqa: BLE001 —— 清理失败不阻塞启动
            logger.exception("演示模式：启动清理失败（不阻塞启动）")
        cleanup_task = asyncio.create_task(demo_mode.cleanup_loop(app))

    try:
        yield
    finally:
        if cleanup_task is not None:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass


def create_app() -> FastAPI:
    settings = get_settings()
    settings.ensure_dirs()

    # 任务 72 §B2：给应用自己的 logger 一个落点 —— uvicorn 只配 ``uvicorn.*``，
    # ``app.*`` 的 INFO（如「补齐素材信息：回填 N 份」「为既有 SQLite 库补齐新列」）
    # 默认没有任何 handler，写了也**看不见** ⇒ 改过库的操作没法事后回查。
    # ``basicConfig`` 在根 logger 已有 handler 时是 no-op（嵌到别人配好日志的环境里不打架）。
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def demo_readonly_guard(request: Request, call_next):
        """任务 48u：DEMO_READONLY=true 时只放行只读分享接口。

        给「内网穿透 / 公网给人看」用：放行面只有 GET/HEAD/OPTIONS 的
        `/api/public/*` 与 `/api/health`，其余（工作台读写、导出、交付包下载）一律 403
        —— 即使反代规则配错，别人也花不了你的额度、也翻不了全库。
        """
        current = get_settings()
        if current.demo_readonly:
            path = request.url.path
            prefix = current.api_prefix
            readable = path == f"{prefix}/health" or path.startswith(f"{prefix}/public/")
            if request.method.upper() not in ("GET", "HEAD", "OPTIONS") or not readable:
                return JSONResponse(status_code=403, content={"detail": DEMO_READONLY_HINT})
        return await call_next(request)

    @app.middleware("http")
    async def demo_mode_guard(request: Request, call_next):
        """任务 154：单租户演示模式的配额闸门。

        - 不提供账号体系（注册/登录 403）；
        - 新建课程：滚动 24 小时最多 M 门；
        - 生成类 POST：每 IP 每分钟 N 次、全实例同时只跑 1 个；
        - 超限一律 429 + 人话 + ``Retry-After``，不是 500。
        """
        current = get_settings()
        if not current.demo_only or current.demo_readonly:
            return await call_next(request)
        prefix = current.api_prefix.rstrip("/")
        path = request.url.path
        if not path.startswith(f"{prefix}/"):
            return await call_next(request)
        rel = path[len(prefix):]
        method = request.method.upper()
        if rel in ("/auth/register", "/auth/login"):
            return JSONResponse(
                status_code=403,
                content={"detail": "演示实例不提供账号注册/登录；所有人共用同一个匿名演示空间。"},
            )

        db = None
        acquired = False
        try:
            if demo_mode.is_course_create(rel, method):
                db = SessionLocal()
                allowed, count, limit = demo_mode.check_daily_course_quota(db, current)
                if not allowed:
                    return demo_mode.daily_quota_response(count, limit)
            if demo_mode.is_generation_request(rel, method):
                if db is None:
                    db = SessionLocal()
                ip = demo_mode.client_ip(request, current)
                ok, detail, retry = demo_mode.acquire_generation_slot(db, current, ip)
                if not ok:
                    return demo_mode.too_many(detail, retry)
                acquired = True
            return await call_next(request)
        finally:
            if acquired:
                demo_mode.release_generation_slot()
            if db is not None:
                db.close()

    # 任务 117：模型没配好（缺 Key / provider 不认识）⇒ **人话拒绝 503** ——
    # 不是 500，也不静默给假内容。`detail` 就是前端直接显示的那句话；
    # 返回体里只有变量名与「去哪填」，**没有 Key**（也不含它的长度/掩码）。
    @app.exception_handler(LLMConfigError)
    async def llm_config_error_handler(_request: Request, exc: LLMConfigError) -> JSONResponse:
        logger.warning("模型配置有问题，已拒绝本次要调模型的动作：%s", exc)
        return JSONResponse(status_code=503, content=exc.as_payload())

    # 任务 151b：模型返回空 content（多为推理 token 吃光预算）⇒ 503 + 可读原因。
    # 与上面「配置没弄好」同档；detail 是前端/连接器直接显示的那句话，
    # 不含 traceback，也不会把异常吞成 200。
    @app.exception_handler(LLMResponseError)
    async def llm_response_error_handler(_request: Request, exc: LLMResponseError) -> JSONResponse:
        logger.warning("模型返回空 content，已拒绝本次动作：%s", exc)
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    # 任务 151c：模型返回的正文不是合法 JSON（常见于截断）⇒ 同为 503 + detail。
    # 只接专属子类，不接 json.JSONDecodeError 整体，免得把别的路由的坏请求也映射成 503。
    @app.exception_handler(LLMJsonError)
    async def llm_json_error_handler(_request: Request, exc: LLMJsonError) -> JSONResponse:
        logger.warning("模型输出不是合法 JSON，已拒绝本次动作：%s", exc)
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    prefix = settings.api_prefix
    for router in (
        auth.router,
        courses.router,
        delivery.router,
        plan.router,
        content.router,
        chart.router,
        concept.router,
        flowchart.router,
        lecture.router,
        kb.router,
        image.router,
        material.router,
        ppt.router,
        quiz.router,
        versions.router,
        publish.router,
        analytics.router,
        audit.router,
        maintenance.router,
        share.router,
        public.router,
    ):
        app.include_router(router, prefix=prefix)

    app.mount("/exports", StaticFiles(directory=str(settings.export_dir)), name="exports")
    app.mount("/images", StaticFiles(directory=str(settings.storage_dir / "images")), name="images")
    app.mount("/ppt-files", StaticFiles(directory=str(settings.ppt_work_dir)), name="ppt-files")

    @app.get(f"{prefix}/health")
    def health() -> dict:
        return {
            "ok": True,
            "app": settings.app_name,
            # 任务 48u：前端据这两个开关决定要不要登录、要不要只开只读分享页
            "auth_enabled": settings.auth_enabled,
            "demo_readonly": settings.demo_readonly,
            # 任务 154：单租户演示模式的保留期与配额（前端/客户可直接看到）
            "demo_only": settings.demo_only,
            "demo": demo_mode.health_payload(
                settings, getattr(app.state, "demo_last_purge", None)
            ),
            # 任务 115：前端托管状态（dist 缺失时 hint 就是那句人话）
            "frontend": {
                "enabled": settings.serve_frontend,
                "dist": str(frontend_dist) if frontend_dist else None,
                "hint": None if (frontend_dist or not settings.serve_frontend) else FRONTEND_BUILD_HINT,
            },
            # 任务 116：外部依赖探测（缺 Edge / PowerPoint 时给「缺什么 + 影响什么 + 怎么装」的人话）
            "capabilities": capabilities.capability_report(),
        }

    # 任务 115：挂在最后注册 —— /api/health、/docs 与已挂载资源都要先匹配到
    frontend_dist = _mount_frontend(app, settings)

    return app


def _mount_frontend(app: FastAPI, settings: Settings) -> Path | None:
    """任务 115：托管前端 dist + SPA 回退（让别人只起一个进程就能用整个应用）。

    - ``SERVE_FRONTEND=false`` ⇒ 一行都不挂（改造前那套纯 API 行为）；
    - dist 存在 ⇒ 静态产物直出，其余非 ``/api/*``、非已挂载资源的 GET 回退 ``index.html``
      （深链 ``/share/<token>`` / ``/courses/...`` 刷新或直链都不再 404）；
    - dist 缺失 ⇒ 根路径给「怎么构建」的人话提示页（503），**API 一律照常**。

    路由注册在**最后**，所以 ``/api/*``、``/docs``、``/openapi.json`` 与 ``/exports`` /
    ``/images`` / ``/ppt-files`` 这些已挂载资源都优先匹配，只有没人认领的路径才落到这里。
    返回实际托管的 dist（没托管 / 没产物时为 None），供 ``/api/health`` 复用。
    """
    if not settings.serve_frontend:
        logger.info("SERVE_FRONTEND=false：后端不托管前端，只提供 API（前端请单独跑）。")
        return None
    dist = settings.resolved_frontend_dist
    if dist is None:
        logger.warning("%s —— 根路径只给提示页，API 不受影响。", FRONTEND_BUILD_HINT)
    else:
        logger.info("前端静态托管：%s（SPA 回退：非 API 的 GET 一律回 index.html）", dist)

    api_prefix = settings.api_prefix.rstrip("/")

    def _is_api_path(path: str) -> bool:
        return bool(api_prefix) and (path == api_prefix or path.startswith(api_prefix + "/"))

    # 只认 GET/HEAD：这样**非 GET 请求仍走改造前那套规则** —— 注册过的路径由 Starlette
    # 回 405（如 POST /api/public/<token>）、没注册的路径回 404，都不受前端托管影响。
    @app.get("/{full_path:path}", include_in_schema=False)
    async def frontend_spa(full_path: str):
        # 没人认领的 /api/* 仍按 API 的规矩回 404 JSON，不许被前端吃掉
        if _is_api_path("/" + full_path):
            raise HTTPException(status_code=404, detail="Not Found")
        if dist is None:
            return HTMLResponse(FRONTEND_MISSING_PAGE, status_code=503)
        target = (dist / full_path).resolve()
        if full_path and target.is_relative_to(dist) and target.is_file():
            return FileResponse(target, media_type=FRONTEND_MEDIA_TYPES.get(target.suffix.lower()),
                                headers=_asset_cache_headers(full_path))
        return FileResponse(dist / "index.html", media_type="text/html",
                            headers=_NO_STORE_HEADERS)

    return dist


app = create_app()




