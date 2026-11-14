"""CourseGenie MCP Server —— 把「课程生成应用」封装成 WorkBuddy 连接器（stdio / streamable-http）。

设计边界（与项目既有口径一致）：

- **本服务不实现任何课程生成逻辑**：解析资料、写大纲、写讲义、画图、导出 Obsidian
  全部发生在用户自己的 CourseGenie 后端，本文件只做 HTTP 桥接。
- **凭据只从环境变量读**，永不写日志、永不进异常信息、永不落盘。
- **每个工具 30 秒内必须返回**：批量生成与笔记导出一律「立即返回 task_id + 后台线程 +
  轮询查询」，绝不让调用方干等。
- **错误一律是可读中文**：不把 Python 异常栈抛给 AI，而是返回
  ``{"ok": false, "error": "...", "hint": "..."}``。
"""

from __future__ import annotations

import io
import json
import logging
import mimetypes
import os
import re
import stat
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from hmac import compare_digest
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

# --------------------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------------------

SERVER_NAME = "CourseGenerator"
CONNECTOR_ID = "coursegenie"
# 1.0.3：默认指向我们托管的演示服务（固定 IP + 端口，暂无域名/证书 ⇒ 走 HTTP），用户**无需自建后端**。
# 想连自己的后端，在连接器表单里把地址改掉即可（本机开发一般填 http://127.0.0.1:8000）。
# 本地址只用于接口调用，不下载可执行文件；程序本体来自 PyPI。
DEFAULT_BACKEND_URL = "http://127.0.0.1:8000"
DEFAULT_API_PREFIX = "/api"

# 单个工具的总预算（秒）。留出 5 秒余量给 WorkBuddy 进程调度。
TOOL_BUDGET_S = 25.0
# 大纲生成在「资料多 / 章节多」时会超过 30 秒：先等这么久，超时就转后台，工具立即返回
OUTLINE_WAIT_S = 22.0

# 单次 HTTP 调用的读写超时（秒）—— 除导出、上传外都用这套
# 连接超时 8s：托管服务在 Cloudflare 后面，冷启动/排队时 5s 会偶发误判（2026-10-20
# 审核员链路实测 5.03s 连接超时）。读超时仍留 18s，保证单工具 25s 预算。
HTTP_TIMEOUT = httpx.Timeout(connect=8.0, read=18.0, write=30.0, pool=5.0)
# 导出（同步接口，27 知识点可能跑几分钟）与上传大文件走更宽的写/读超时；
# 连接阶段同样给 8s，避免 Cloudflare 冷启动时导出下载先卡在 5s。
HTTP_TIMEOUT_LONG = httpx.Timeout(connect=8.0, read=1800.0, write=300.0, pool=5.0)

# 下载/解压上限：笔记包是后端自己打的，正常只有几 MB；这些上限只防「坏包/恶意包」
# 把连接器内存撑爆。超过上限一律给可读中文错误，不静默截断。
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
MAX_ZIP_FILES = 2000
MAX_ZIP_TOTAL_BYTES = 512 * 1024 * 1024
MAX_ZIP_FILE_BYTES = 64 * 1024 * 1024
MAX_ZIP_PATH_CHARS = 180

# Windows 保留设备名：即使带扩展名（CON.md）也不能作为文件名。
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

# DeepSeek 凭据自检（只读接口，不消耗 token）
DEEPSEEK_MODELS_URL = "https://api.deepseek.com/models"
DEEPSEEK_PROBE_TIMEOUT = 8.0
_DS_PROBE_TTL_S = 600.0

logger = logging.getLogger("coursegenie.mcp")


# --------------------------------------------------------------------------------------
# 错误：一切对 AI 的报错都从这里走，保证是中文、可执行、不含栈
# --------------------------------------------------------------------------------------


class ToolError(Exception):
    """可读中文错误。``hint`` 是「用户下一步该做什么」，``status`` 是后端 HTTP 状态（有的话）。"""

    def __init__(self, message: str, hint: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.status = status

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"ok": False, "error": self.message}
        if self.hint:
            data["hint"] = self.hint
        return data


def _fail(message: str, hint: str = "") -> dict[str, Any]:
    return ToolError(message, hint).as_dict()


# --------------------------------------------------------------------------------------
# 配置：只从 os.environ 读，缺 Key 给可读中文错误
# --------------------------------------------------------------------------------------


class Settings:
    """运行时配置。**只从环境变量读取**，不读文件、不接受参数注入。"""

    __slots__ = ("api_key", "backend_url", "backend_api_key", "api_prefix")

    def __init__(self) -> None:
        # 1.0.1：Key 改成**可选**。课程生成用的是后端自己的模型凭据；
        # 这个 Key 只用于连接器的「凭据自检」。没填就跳过自检，不阻断任何工具
        # （否则审核员/新用户拿不到 DeepSeek Key 就完全用不了）。
        self.api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()

        raw_url = os.environ.get("COURSE_BACKEND_URL", "").strip() or DEFAULT_BACKEND_URL
        self.backend_url = _normalize_backend_url(raw_url)
        # 可选：后端开了 AUTH_ENABLED 时用；不在连接器表单里，不影响占位符一一对应
        self.backend_api_key = os.environ.get("COURSE_BACKEND_API_KEY", "").strip()
        self.api_prefix = DEFAULT_API_PREFIX

    @property
    def api_base(self) -> str:
        return f"{self.backend_url}{self.api_prefix}"


def _require_key_hint() -> str:
    """没填 Key 时给的自检说明（不报错，只说明跳过）。"""
    return (
        "未配置 DeepSeek API Key：已跳过凭据自检（不影响课程生成——"
        "生成用的是后端自己的模型凭据）。若要自检，可在连接器配置里填一个 Key。"
    )


def _normalize_backend_url(raw: str) -> str:
    """容忍「带 /api」「带尾斜杠」「只写端口」几种写法，统一成 origin。"""
    url = raw.strip().rstrip("/")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = f"http://{url}"
    if url.lower().endswith(DEFAULT_API_PREFIX):
        url = url[: -len(DEFAULT_API_PREFIX)]
    return url.rstrip("/")


# --------------------------------------------------------------------------------------
# HTTP 桥接
# --------------------------------------------------------------------------------------


def _trust_env() -> bool:
    """默认**不读系统代理**。

    Windows「Internet 选项」里残留的代理会经 urllib 读注册表被 httpx 采纳，
    打到 ``127.0.0.1`` 上会变成超时/连接重置（本机后端根本不需要代理）。
    后端确实在远端且必须走公司代理时，设 ``COURSE_BACKEND_USE_SYSTEM_PROXY=true`` 打开。
    """
    return os.environ.get("COURSE_BACKEND_USE_SYSTEM_PROXY", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )

def _headers(settings: Settings) -> dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": "coursegenie-mcp/1.0"}
    if settings.backend_api_key:
        headers["X-API-Key"] = settings.backend_api_key
    return headers


def _extract_detail(response: httpx.Response) -> str:
    """FastAPI 的报错在 detail 里；拿不到就退回状态短语。"""
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 —— 非 JSON 响应（如 SPA 回退页）不算错误
        return ""
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("error") or payload.get("message")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        if detail is not None:
            return json.dumps(detail, ensure_ascii=False)[:500]
    return ""


def _translate_http_error(status: int, detail: str, path: str) -> ToolError:
    """把后端状态码翻译成「中文 + 下一步」。"""
    tail = f"（后端原话：{detail}）" if detail else ""
    if status == 401:
        return ToolError(
            f"后端拒绝访问：缺少或无效的 API Key{tail}",
            "你的 CourseGenie 后端开了 AUTH_ENABLED；"
            "请把 DEV_API_KEY 或用户 API Key 通过环境变量 COURSE_BACKEND_API_KEY 传给连接器，"
            "或把后端 AUTH_ENABLED 设为 false（仅本机使用时可以）。",
        )
    if status == 403:
        return ToolError(
            f"后端拒绝该操作：当前是只读演示模式{tail}",
            "后端设了 DEMO_READONLY=true，只放行只读分享接口；"
            "要生成课程请把它设为 false 后重启后端。",
        )
    if status == 404:
        return ToolError(
            f"后端找不到对应资源{tail}",
            "请确认 task_id（课程）确实存在：可先调用 check_task_status 或 list 课程。",
        )
    if status == 409:
        return ToolError(
            f"当前阶段不允许这个操作{tail}",
            "这是后端的状态校验（例如大纲还没通过就生成讲义、已有任务在跑）。"
            "请按提示先完成上一步，或等正在跑的任务结束。",
        )
    if status == 413:
        return ToolError(
            f"文件太大，后端拒收{tail}",
            "后端上限由 MAX_UPLOAD_MB 控制（默认 50MB）；"
            "请压缩文件、拆分成多份，或调高后端配置后重启。",
        )
    if status == 400:
        return ToolError(
            f"后端拒绝了这次请求{tail}",
            "看括号里的后端原话：常见原因是这一步的前提还没完成"
            "（例如大纲还在生成中就点了「通过」、目标字数太短、素材为空）。"
            "如果刚调用过 generate_outline 且返回 status=running，请先用 check_task_status 取到结果再继续。",
        )
    if status == 429:
        retry = ""
        try:
            payload = json.loads(detail) if isinstance(detail, str) and detail.startswith("{") else {}
        except Exception:  # noqa: BLE001
            payload = {}
        if isinstance(payload, dict) and payload.get("retry_after"):
            retry = f"，建议 {payload['retry_after']} 秒后重试"
        return ToolError(
            f"服务忙或超出限额{tail}{retry}",
            "演示服务同一时刻只跑 1 个生成任务、每人每分钟最多 6 次、每天最多 20 门课。"
            "等当前任务结束后再试；进度用 check_task_status 查看。",
        )
    if status == 422:
        return ToolError(
            f"参数没通过后端校验{tail}",
            "检查必填项是否缺失（例如大纲需要 goal ≥ 3 个字符）。",
        )
    if status == 503:
        return ToolError(
            f"后端暂不可用{tail}",
            "常见原因：后端没配好模型凭据（LLM_PROVIDER + 对应 API Key）。"
            "请看后端启动日志里「模型配置」那行，或先设 LLM_PROVIDER=mock 跑通流程。",
        )
    if status >= 500:
        return ToolError(
            f"后端内部错误（HTTP {status}）{tail}",
            "请看后端控制台／uvicorn 日志里的异常；本连接器没有拿到可用结果。",
        )
    return ToolError(f"后端返回了未预期的状态码 {status}{tail}", "请稍后重试或查看后端日志。")


def _request(
    settings: Settings,
    method: str,
    path: str,
    *,
    json_body: Any = None,
    params: dict[str, Any] | None = None,
    files: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    long_running: bool = False,
) -> Any:
    """一次后端调用；所有失败都转成中文 ToolError。日志只记方法/路径/状态/耗时。"""
    url = f"{settings.api_base}{path}"
    timeout = HTTP_TIMEOUT_LONG if long_running else HTTP_TIMEOUT
    started = time.monotonic()
    status: Any = "-"
    response: httpx.Response | None = None
    # GET 是幂等的：连接阶段抖动（Cloudflare 边缘排队）重试一次；POST 不重试，避免重复入队。
    attempts = 2 if method.upper() == "GET" else 1
    for attempt in range(attempts):
        try:
            with httpx.Client(
                timeout=timeout,
                headers=_headers(settings),
                trust_env=_trust_env(),
                follow_redirects=False,
            ) as client:
                response = client.request(
                    method.upper(),
                    url,
                    json=json_body,
                    params=params,
                    files=files,
                    data=data,
                )
            status = response.status_code
            break
        except (httpx.ConnectError, httpx.ConnectTimeout):
            if attempt + 1 < attempts:
                logger.info(
                    "%s %s -> 连接失败，0.5s 后重试一次 (%.0f ms)",
                    method.upper(), path, (time.monotonic() - started) * 1000,
                )
                time.sleep(0.5)
                continue
            logger.info(
                "%s %s -> 连接失败 (%.0f ms)",
                method.upper(), path, (time.monotonic() - started) * 1000,
            )
            raise ToolError(
                f"连不上课程生成后端：{settings.backend_url}",
                "1) 确认后端已启动（在 backend 目录跑 uvicorn app.main:app --port 8000）；"
                "2) 确认连接器里的 COURSE_BACKEND_URL 与后端实际地址一致；"
                "3) 确认本机防火墙没有拦截；4) 若本机开着系统代理/加速器，确认它没有劫持 127.0.0.1（连接器默认不走系统代理）。",
            ) from None
        except httpx.TimeoutException:
            logger.info("%s %s -> 超时 (%.0f ms)", method.upper(), path, (time.monotonic() - started) * 1000)
            raise ToolError(
                f"后端响应超时（{path}）",
                "后端可能正在处理大文件或排队生成。少量重试即可；"
                "持续超时请检查后端日志是否卡在模型调用上。",
            ) from None
        except httpx.HTTPError as exc:  # 传输层其它问题
            logger.info("%s %s -> 传输错误 %s", method.upper(), path, type(exc).__name__)
            raise ToolError(
                f"与后端通信失败：{type(exc).__name__}",
                "请检查 COURSE_BACKEND_URL 是否写错（例如 http/https、端口号）。",
            ) from None
    if response is None:  # 理论上到不了；防止未赋值被当成成功
        raise ToolError(f"与后端通信失败（{path}）", "请稍后重试。")

    elapsed_ms = (time.monotonic() - started) * 1000
    ok = 200 <= status < 300
    logger.info("%s %s -> %s (%.0f ms)", method.upper(), path, status, elapsed_ms)

    if not ok:
        error = _translate_http_error(status, _extract_detail(response), path)
        error.status = status
        raise error

    if response.status_code == 204 or not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        raise ToolError(
            f"后端返回的不是 JSON（{path}）",
            "可能是 COURSE_BACKEND_URL 指向了前端页面或代理。请确认它指向 CourseGenie 后端本身。",
        ) from None


# --------------------------------------------------------------------------------------
# 下载笔记包 + 解压到本机（v1.0.2：让「写进我的 Obsidian」真的落在用户自己的盘上）
# --------------------------------------------------------------------------------------


def _download(settings: Settings, path: str) -> bytes:
    """下载后端静态文件（/exports/xxx.zip 这类挂在根路径、不带 /api 前缀）。"""
    url = f"{settings.backend_url}{path}" if path.startswith("/") else path
    started = time.monotonic()
    status: Any = "-"
    chunks: list[bytes] = []
    total = 0
    try:
        with httpx.Client(
            timeout=HTTP_TIMEOUT_LONG,
            headers=_headers(settings),
            trust_env=_trust_env(),
            follow_redirects=True,
        ) as client:
            with client.stream("GET", url) as response:
                status = response.status_code
                if status != 200:
                    raise ToolError(
                        f"下载笔记包失败（HTTP {status}）",
                        "后端可能还在导出，稍等几秒再调 export_to_obsidian；持续失败请看后端日志。",
                    )
                length = response.headers.get("content-length", "")
                if length.isdigit() and int(length) > MAX_DOWNLOAD_BYTES:
                    raise ToolError(
                        f"笔记包太大（超过 {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB）",
                        "为避免撑爆内存，已停止下载；请确认后端导出的确实是课程笔记包。",
                    )
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise ToolError(
                            f"笔记包太大（超过 {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB）",
                            "为避免撑爆内存，已停止下载；请确认后端导出的确实是课程笔记包。",
                        )
                    chunks.append(chunk)
    except ToolError:
        raise
    except httpx.TimeoutException:
        logger.info("GET %s -> 下载超时 (%.0f ms)", path, (time.monotonic() - started) * 1000)
        raise ToolError(
            "下载笔记包超时",
            "后端可能还在导出。稍等几秒再调 export_to_obsidian；持续超时请看后端日志。",
        ) from None
    except httpx.ConnectError:
        logger.info("GET %s -> 连接失败 (%.0f ms)", path, (time.monotonic() - started) * 1000)
        raise ToolError(
            f"连不上课程生成后端：{settings.backend_url}",
            "请检查连接器里的服务地址是否可访问、网络是否正常。",
        ) from None
    except httpx.HTTPError as exc:
        logger.info("GET %s -> 下载失败 %s", path, type(exc).__name__)
        raise ToolError(
            f"下载笔记包失败（{type(exc).__name__}）",
            "请检查连接器里的服务地址是否可访问、网络是否正常。",
        ) from None
    logger.info("GET %s -> %s (%.0f ms, %s bytes)", path, status,
                (time.monotonic() - started) * 1000, total)
    return b"".join(chunks)


def _sanitize_zip_name(name: str) -> str | None:
    """把 zip 内部路径逐段清洗成 Windows 可落盘的相对路径；越界/非法返回 None。"""
    raw = (name or "").replace("\\", "/")
    if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", raw):
        return None
    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", part).rstrip(" .")
        if not cleaned:
            cleaned = "_"
        if cleaned.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
            cleaned = f"_{cleaned}"
        if len(cleaned) > 120:
            cleaned = cleaned[:120]
        parts.append(cleaned)
    if not parts:
        return None
    rel = "/".join(parts)
    return rel if len(rel) <= MAX_ZIP_PATH_CHARS else None


def _safe_dirname(name: str) -> str:
    r"""把课程名变成合法目录名（Windows 不允许 <>:"/\|?*、控制字符、保留设备名与结尾点/空格）。"""
    cleaned = (_sanitize_zip_name(name or "") or "course").replace("/", "_")
    return cleaned[:80] or "course"


def _extract_zip(data: bytes, target: Path) -> list[str]:
    """解压到 target：拒绝路径穿越、软链、加密包、文件名非法字符与 zip 炸弹。"""
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    written: list[str] = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ToolError("后端返回的笔记包不是合法 zip", "请重试一次；持续失败请看后端日志。") from None
    with archive as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_FILES:
            raise ToolError(
                f"笔记包内含文件过多（{len(infos)} 个，上限 {MAX_ZIP_FILES}）",
                "这不是正常的课程笔记包；请重试，持续失败请看后端日志。",
            )
        declared = sum(max(0, int(info.file_size or 0)) for info in infos)
        if declared > MAX_ZIP_TOTAL_BYTES:
            raise ToolError(
                f"笔记包解压后过大（声明 {declared // (1024 * 1024)} MB，"
                f"上限 {MAX_ZIP_TOTAL_BYTES // (1024 * 1024)} MB）",
                "为避免撑爆内存，已拒绝解压；请重试或联系服务方。",
            )
        total = 0
        for info in infos:
            if info.is_dir():
                continue
            if info.flag_bits & 0x1:
                raise ToolError("笔记包是加密 zip，连接器无法解压", "请让后端导出不加密的笔记包。")
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                # 不还原软链；直接跳过，避免写出指向 vault 之外的文件
                continue
            name = _sanitize_zip_name(info.filename)
            if name is None:
                continue
            dest = (root / name).resolve()
            try:
                dest.relative_to(root)
            except ValueError:
                continue
            if len(str(dest)) >= 260:
                raise ToolError(
                    "笔记包里的文件路径太长（Windows 上限 260 字符）",
                    "请把 vault_path 换成更短的目录，或减少课程名/章节名长度后重试。",
                )
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src:
                chunk = src.read(MAX_ZIP_FILE_BYTES + 1)
            if len(chunk) > MAX_ZIP_FILE_BYTES:
                raise ToolError(
                    f"笔记包内单个文件过大（{name}，上限 {MAX_ZIP_FILE_BYTES // (1024 * 1024)} MB）",
                    "为避免撑爆内存，已停止解压；请重试或联系服务方。",
                )
            total += len(chunk)
            if total > MAX_ZIP_TOTAL_BYTES:
                raise ToolError(
                    f"笔记包解压后过大（已超过 {MAX_ZIP_TOTAL_BYTES // (1024 * 1024)} MB）",
                    "为避免撑爆内存，已停止解压；请重试或联系服务方。",
                )
            dest.write_bytes(chunk)
            written.append(name)
    return written

# --------------------------------------------------------------------------------------
# DeepSeek 凭据自检：这是 DEEPSEEK_API_KEY 的真实用途（只读，不消耗 token，不落日志）
# --------------------------------------------------------------------------------------

_ds_probe: dict[str, Any] = {"at": 0.0, "result": None}
_ds_probe_lock = threading.Lock()


def _deepseek_probe(settings: Settings, force: bool = False) -> dict[str, Any]:
    """用表单里的 Key 调 DeepSeek 只读接口，确认凭据可用。**绝不回传 Key 本身**。"""
    with _ds_probe_lock:
        fresh = (time.monotonic() - float(_ds_probe["at"] or 0)) < _DS_PROBE_TTL_S
        if _ds_probe["result"] is not None and fresh and not force:
            return dict(_ds_probe["result"])

        result: dict[str, Any]
        try:
            with httpx.Client(timeout=httpx.Timeout(DEEPSEEK_PROBE_TIMEOUT), trust_env=_trust_env()) as client:
                response = client.get(
                    DEEPSEEK_MODELS_URL,
                    headers={"Authorization": f"Bearer {settings.api_key}"},
                )
        except httpx.HTTPError as exc:
            logger.info("DeepSeek 凭据自检：网络不可达（%s）", type(exc).__name__)
            result = {
                "checked": False,
                "reason": f"连不上 DeepSeek 接口（{type(exc).__name__}），本次未校验凭据",
            }
        else:
            if response.status_code == 200:
                models: list[str] = []
                try:
                    payload = response.json()
                    for item in payload.get("data") or []:
                        model_id = item.get("id") if isinstance(item, dict) else None
                        if model_id:
                            models.append(str(model_id))
                except Exception:  # noqa: BLE001
                    payload = {}
                logger.info("DeepSeek 凭据自检：通过（HTTP 200，%d 个模型）", len(models))
                result = {
                    "checked": True,
                    "valid": True,
                    "models": models[:10],
                }
            elif response.status_code in (401, 403):
                logger.info("DeepSeek 凭据自检：未通过（HTTP %s）", response.status_code)
                result = {
                    "checked": True,
                    "valid": False,
                    "reason": "DeepSeek 拒绝了这个 Key（HTTP 401/403）：请确认没有多余空格、没有过期",
                }
            else:
                logger.info("DeepSeek 凭据自检：异常状态 HTTP %s", response.status_code)
                result = {
                    "checked": False,
                    "reason": f"DeepSeek 返回 HTTP {response.status_code}，本次未得出结论",
                }

        _ds_probe["at"] = time.monotonic()
        _ds_probe["result"] = result
        return dict(result)


# --------------------------------------------------------------------------------------
# 本地异步任务表：长任务（批量生成、笔记导出）立即返回 task_id，由此处轮询
# --------------------------------------------------------------------------------------

_STATE_DIR = Path(tempfile.gettempdir()) / "coursegenie-mcp"
_STATE_FILE = _STATE_DIR / "tasks.json"
_tasks: dict[str, dict[str, Any]] = {}
# 必须可重入：_new_op / _finish_op 在**持锁状态**下还会经过 _task()，
# 非重入锁会自锁死（2026-10-19 临时环境实测：export_to_obsidian 直接把请求线程卡住）。
_tasks_lock = threading.RLock()
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="coursegenie-task")


def _load_tasks() -> None:
    """把上次进程留下的任务状态读回来（后端 job 本身是持久的，这里只做尽力而为）。"""
    try:
        raw = _STATE_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 —— 状态文件坏了不该影响启动
        return
    if isinstance(data, dict):
        with _tasks_lock:
            _tasks.update({str(k): v for k, v in data.items() if isinstance(v, dict)})


def _persist_tasks() -> None:
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        with _tasks_lock:
            snapshot = json.dumps(_tasks, ensure_ascii=False, indent=2)
        _STATE_FILE.write_text(snapshot, encoding="utf-8")
    except Exception:  # noqa: BLE001 —— 落盘失败只影响「重启后还能查到」，不影响本次调用
        logger.info("任务状态落盘失败（不影响本次调用）")


def _task(task_id: str) -> dict[str, Any]:
    with _tasks_lock:
        entry = _tasks.setdefault(task_id, {"course_id": task_id, "ops": {}})
        entry.setdefault("ops", {})
        return entry


def _pending_op(task_id: str, kind: str) -> dict[str, Any] | None:
    """这门课还有没有指定类型的本地长任务在跑（大纲生成就是这种）。"""
    with _tasks_lock:
        ops = dict((_tasks.get(task_id) or {}).get("ops") or {})
    running = [(op_id, op) for op_id, op in ops.items()
               if op.get("kind") == kind and op.get("status") == "running"]
    if not running:
        return None
    op_id, op = running[-1]
    return {"op_id": op_id, "started_at": op.get("started_at")}


def _wait_outline(task_id: str, timeout_s: float = 15.0) -> dict[str, Any]:
    """等后台大纲生成跑完（最多 timeout_s 秒），避免「刚生成就点通过」被后端 400。

    返回 {"waited": bool, "still_running": bool, "op_id": str, "error": str}
    """
    pending = _pending_op(task_id, "generate_outline")
    if pending is None:
        return {"waited": False, "still_running": False, "op_id": "", "error": ""}
    op_id = str(pending.get("op_id") or "")
    deadline = time.monotonic() + max(0.0, timeout_s)
    while time.monotonic() < deadline:
        with _tasks_lock:
            op = dict((_tasks.get(task_id) or {}).get("ops", {}).get(op_id) or {})
        status = str(op.get("status") or "")
        if status == "success":
            return {"waited": True, "still_running": False, "op_id": op_id, "error": ""}
        if status == "failed":
            return {"waited": True, "still_running": False, "op_id": op_id,
                    "error": str(op.get("error") or "大纲生成失败")}
        time.sleep(1.0)
    return {"waited": True, "still_running": True, "op_id": op_id, "error": ""}

def _new_op(task_id: str, kind: str) -> str:
    op_id = f"{kind}-{int(time.time())}-{os.getpid()}"
    with _tasks_lock:
        _task(task_id)["ops"][op_id] = {
            "kind": kind,
            "status": "running",
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": "",
            "result": None,
            "error": "",
        }
    _persist_tasks()
    return op_id


def _finish_op(task_id: str, op_id: str, *, result: Any = None, error: str = "") -> None:
    with _tasks_lock:
        op = _task(task_id)["ops"].get(op_id)
        if op is None:
            return
        op["status"] = "failed" if error else "success"
        op["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        op["result"] = result
        op["error"] = error
    _persist_tasks()


def _start_op(
    task_id: str, kind: str, func, wait_s: float  # noqa: ANN001
) -> tuple[str, bool, dict[str, Any]]:
    """起一个后台长任务，但**先等 wait_s 秒**：等到了就直接把结果交回去（同步快路径）。

    工具因此可以「快的当场给结果，慢的立刻转轮询」，而不必区分两套接口。
    """
    op_id = _new_op(task_id, kind)
    box: dict[str, Any] = {}

    def _worker() -> None:
        try:
            result = func()
        except ToolError as exc:
            _finish_op(task_id, op_id, error=exc.message)
            box["error"] = exc
        except Exception as exc:  # noqa: BLE001 —— 后台线程绝不把栈漏给用户
            logger.info("长任务 %s 异常：%s", op_id, type(exc).__name__)
            _finish_op(
                task_id, op_id,
                error=f"长任务执行失败（{type(exc).__name__}），请查看后端日志后重试。",
            )
            box["error"] = ToolError("长任务执行失败", "请查看后端日志后重试。")
        else:
            _finish_op(task_id, op_id, result=result)
            box["result"] = result

    future = _pool.submit(_worker)
    try:
        future.result(timeout=wait_s)
    except FuturesTimeoutError:
        return op_id, False, box
    return op_id, True, box


def _run_async(task_id: str, op_id: str, func) -> None:  # noqa: ANN001
    def _wrapped() -> None:
        try:
            _finish_op(task_id, op_id, result=func())
        except ToolError as exc:
            logger.info("长任务 %s 失败：%s", op_id, exc.message)
            _finish_op(task_id, op_id, error=exc.message)
        except Exception as exc:  # noqa: BLE001 —— 后台线程绝不能把栈漏给用户
            logger.info("长任务 %s 异常：%s", op_id, type(exc).__name__)
            _finish_op(
                task_id,
                op_id,
                error=f"长任务执行失败（{type(exc).__name__}），请查看后端日志后重试。",
            )

    _pool.submit(_wrapped)


def _poll_job(settings: Settings, job_id: str, budget_s: float) -> dict[str, Any]:
    """在后端 job 上短轮询；预算内没结束就如实返回 running。"""
    deadline = time.monotonic() + max(0.0, budget_s)
    interval = 1.2
    while True:
        data = _request(settings, "GET", f"/content/jobs/{job_id}")
        status = str((data or {}).get("status") or "")
        if status in ("success", "failed"):
            return data or {}
        if time.monotonic() >= deadline:
            return data or {}
        time.sleep(interval)
def _lecture_items(settings: Settings, course_id: str) -> list[dict[str, Any]]:
    """这门课已有的讲义条目（后端返回的顺序即生成顺序）。"""
    data = _request(settings, "GET", f"/content/{course_id}")
    if not isinstance(data, list):
        return []
    return [i for i in data if isinstance(i, dict) and str(i.get("kind") or "") == "lecture"]


def _ensure_style_anchor(
    settings: Settings, course_id: str, tone: str, figure_style: str = ""
) -> dict[str, Any]:
    """确保这门课有「样板锚」—— 网页端点「以这篇为样板」就是这件事。

    有了样板锚，批量生成才会沿用同一套语气/插图风格；没有就 409。
    顺序：已经有 → 直接用；否则拿本次（或最近一节）讲义当样板并自动加锁。
    """
    state = _request(settings, "GET", "/lecture/style-anchor", params={"course_id": course_id})
    if isinstance(state, dict) and state.get("style_anchor"):
        return {
            "created": False,
            "anchor": state.get("style_anchor"),
            "summary": state.get("summary") or "",
        }

    with _tasks_lock:
        remembered = str((_tasks.get(course_id) or {}).get("sample_item_id") or "")
    candidates: list[str] = []
    if remembered:
        candidates.append(remembered)
    for item in reversed(_lecture_items(settings, course_id)):
        item_id = str(item.get("id") or "")
        if item_id and item_id not in candidates:
            candidates.append(item_id)

    last_error = ""
    for item_id in candidates:
        try:
            written = _request(
                settings,
                "POST",
                "/lecture/style-anchor",
                json_body={
                    "course_id": course_id,
                    "source_item_id": item_id,
                    "tone": (tone or "正式").strip(),
                    "figure_style": (figure_style or "").strip() or None,
                },
            )
        except ToolError as exc:
            last_error = exc.message
            continue
        with _tasks_lock:
            _task(course_id)["sample_item_id"] = item_id
        _persist_tasks()
        return {
            "created": True,
            "anchor": (written or {}).get("style_anchor"),
            "summary": (written or {}).get("summary") or "",
            "source_item_id": item_id,
        }

    raise ToolError(
        "这门课还没有可以当样板的讲义，后端拒绝批量生成",
        "先调用 generate_sample 生成一节样课（连接器会自动把它设成「样板」），再做批量；"
        "确实要跳过样板、一次性生成整门课，就显式传 ignore_style_anchor=true。"
        + (f"（后端原话：{last_error}）" if last_error else ""),
    )


# --------------------------------------------------------------------------------------
# 课程相关的取数小工具
# --------------------------------------------------------------------------------------


def _course(settings: Settings, course_id: str) -> dict[str, Any]:
    data = _request(settings, "GET", f"/course/{course_id}")
    if not isinstance(data, dict):
        raise ToolError("后端返回的课程数据格式异常", "请确认 COURSE_BACKEND_URL 指向 CourseGenie 后端。")
    return data


def _plan_or_none(settings: Settings, course_id: str) -> dict[str, Any] | None:
    try:
        data = _request(settings, "GET", f"/plan/{course_id}")
    except ToolError as exc:
        if exc.status == 404:
            return None
        raise
    return data if isinstance(data, dict) else None


def _point_nodes(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """大纲里的叶子节点（node_type=point），按后端返回顺序 —— 即「第 N 课」。"""
    nodes = [n for n in (plan.get("nodes") or []) if isinstance(n, dict)]
    points = [n for n in nodes if str(n.get("node_type") or n.get("type") or "") == "point"]
    return points or nodes


def _outline_view(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """给 AI 看的精简大纲：章 + 知识点，带 id 以便后续直接指定。"""
    nodes = [n for n in (plan.get("nodes") or []) if isinstance(n, dict)]
    view: list[dict[str, Any]] = []
    for node in nodes:
        view.append(
            {
                "id": node.get("id"),
                "key": node.get("node_key") or node.get("key"),
                "type": node.get("node_type") or node.get("type"),
                "title": node.get("title"),
                "objective": node.get("objective") or "",
            }
        )
    return view


def _stage_of(settings: Settings, course_id: str, course: dict[str, Any] | None = None) -> dict[str, Any]:
    """推断课程当前阶段，给 check_task_status 用。任何一步失败都不影响整体。"""
    course = course or _course(settings, course_id)
    stage: dict[str, Any] = {
        "course_id": course_id,
        "title": course.get("title"),
        "status": course.get("status"),
        "materials": None,
        "outline": None,
        "lectures": None,
        "vault": None,
        "stage": "未知",
    }
    try:
        materials = _request(settings, "GET", f"/material/{course_id}")
        stage["materials"] = len(materials) if isinstance(materials, list) else None
    except ToolError:
        pass
    try:
        plan = _plan_or_none(settings, course_id)
        if plan:
            points = _point_nodes(plan)
            stage["outline"] = {
                "outline_id": plan.get("outline_id"),
                "nodes": len(plan.get("nodes") or []),
                "points": len(points),
            }
    except ToolError:
        pass
    try:
        items = _request(settings, "GET", f"/content/{course_id}")
        if isinstance(items, list):
            kinds: dict[str, int] = {}
            for item in items:
                kind_key = str((item or {}).get("kind") or "unknown")
                kinds[kind_key] = kinds.get(kind_key, 0) + 1
            stage["content_items"] = len(items)
            stage["content_kinds"] = kinds
            stage["lectures"] = kinds.get("lecture", 0)
    except ToolError:
        pass
    try:
        vault = _request(settings, "GET", f"/kb/course/{course_id}")
        if isinstance(vault, dict):
            kb = vault.get("vault") or {}
            stage["vault"] = {
                "configured": kb.get("configured"),
                "vault_root": kb.get("vault_root"),
                "exists": kb.get("exists"),
                "synced_at": (vault.get("kb") or {}).get("synced_at"),
            }
    except ToolError:
        pass

    approved = str(course.get("status") or "") == "approved"
    if not stage["materials"]:
        stage["stage"] = "未上传资料（下一步：upload_source）"
    elif not stage["outline"]:
        stage["stage"] = "资料已上传（下一步：generate_outline）"
    elif not approved:
        stage["stage"] = "大纲已生成但未通过（下一步：confirm_outline approved=true）"
    elif not stage["lectures"]:
        stage["stage"] = "大纲已通过（下一步：generate_sample 生成样课）"
    elif stage["vault"] and stage["vault"].get("synced_at"):
        stage["stage"] = "已写入 Obsidian 笔记库（可继续修改插图后重新 export_to_obsidian）"
    else:
        stage["stage"] = "讲义已生成（下一步：export_to_obsidian）"
    return stage


# --------------------------------------------------------------------------------------
# MCP Server
# --------------------------------------------------------------------------------------

# 远程 MCP（streamable-http）配置。默认仍是 stdio，连接器包行为不变；
# 只有显式启动远程模式时才读这些变量。
DEFAULT_MCP_HOST = "127.0.0.1"
DEFAULT_MCP_PORT = 8001
DEFAULT_MCP_PATH = "/mcp"
MIN_MCP_TOKEN_CHARS = 32
DEFAULT_MCP_ALLOWED_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*", "mcp.example.com")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} 必须是整数，当前是 {raw!r}") from None


def _transport_security() -> TransportSecuritySettings:
    """允许 Cloudflare 隧道带来的 Host（默认只放本机与 mcp.example.com）。"""
    raw = os.environ.get("MCP_ALLOWED_HOSTS", "").strip()
    hosts = [item.strip() for item in raw.split(",") if item.strip()] if raw else list(DEFAULT_MCP_ALLOWED_HOSTS)
    origins = []
    for host in hosts:
        name = host[1:].split("]", 1)[0] if host.startswith("[") else host.split(":", 1)[0]
        origins.append(f"https://{name}")
    origins.extend(["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"])
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


mcp = FastMCP(
    SERVER_NAME,
    host=os.environ.get("MCP_HOST", DEFAULT_MCP_HOST).strip() or DEFAULT_MCP_HOST,
    port=_env_int("MCP_PORT", DEFAULT_MCP_PORT),
    streamable_http_path=os.environ.get("MCP_PATH", DEFAULT_MCP_PATH).strip() or DEFAULT_MCP_PATH,
    transport_security=_transport_security(),
)

SOURCE_TYPES = {
    "pdf": ".pdf",
    "ppt": ".pptx",
    "pptx": ".pptx",
    "video": "",
    "audio": "",
    "text": "",
    "auto": "",
}

_AV_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".m4a", ".mp3", ".wav", ".m4s"}


def _resolve_file(file_path: str) -> Path:
    raw = (file_path or "").strip().strip('"').strip("'")
    if not raw:
        raise ToolError("file_path 不能为空", "请给一个本机文件的绝对路径，例如 D:\\素材\\讲义.pdf。")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        raise ToolError(
            f"找不到文件：{path}",
            "请确认路径拼写、盘符，以及该文件确实在本机（连接器与被上传文件必须在同一台机器上）。",
        )
    if not path.is_file():
        raise ToolError(f"这不是一个文件：{path}", "请提供一个文件路径（不是文件夹）。")
    return path


@mcp.tool()
def check_backend_status() -> dict[str, Any]:
    """开工前自检：后端是否在线、可写、Obsidian 库是否就绪、DeepSeek 凭据是否有效。

    建议每个课程会话的第一步都调用一次；返回里不含任何凭据内容。
    """
    try:
        settings = Settings()
        health = _request(settings, "GET", "/health")
        vault: dict[str, Any] = {}
        try:
            vault = _request(settings, "GET", "/kb/vault") or {}
        except ToolError as exc:
            vault = {"error": exc.message}

        # 1.0.1：没填 Key 就跳过自检（不阻断），并如实说明
        if settings.api_key:
            deepseek = _deepseek_probe(settings)
        else:
            deepseek = {"checked": False, "reason": _require_key_hint()}
        return {
            "ok": True,
            "backend_url": settings.backend_url,
            "backend_online": True,
            "backend_app": (health or {}).get("app"),
            "auth_enabled": (health or {}).get("auth_enabled"),
            "demo_readonly": (health or {}).get("demo_readonly"),
            "llm": (health or {}).get("capabilities", {}),
            "vault": vault,
            "deepseek_key": deepseek,
            "next_step": "先 upload_source 上传资料；已有课程可直接传 task_id 进行后续步骤。",
        }
    except ToolError as exc:
        return exc.as_dict()


@mcp.tool()
def upload_source(
    file_path: str,
    source_type: str = "auto",
    course_title: str = "",
    course_id: str = "",
) -> dict[str, Any]:
    """把一份原始资料（PDF/PPT/视频/音频/文本/Markdown/Word）上传到课程生成后端。

    返回的 task_id 就是这门课的 id，后续每个工具都传它。
    音视频会走本机转写，后端会额外返回 job_id（可用 check_task_status 跟进）。
    """
    try:
        settings = Settings()
        path = _resolve_file(file_path)
        kind = (source_type or "auto").strip().lower()
        if kind not in SOURCE_TYPES:
            raise ToolError(
                f"不认识的 source_type：{source_type}",
                "可用值：auto / pdf / ppt / pptx / video / audio / text。留空或 auto 表示按扩展名自动识别。",
            )
        ext = path.suffix.lower()
        if kind in ("pdf", "ppt", "pptx") and ext not in (
            {".pdf"} if kind == "pdf" else {".ppt", ".pptx"}
        ):
            raise ToolError(
                f"source_type={kind} 与文件扩展名 {ext} 不匹配",
                "把 source_type 改成 auto，或换一个对应类型的文件。",
            )

        target_course = (course_id or "").strip()
        created = False
        if target_course:
            _course(settings, target_course)
        else:
            title = (course_title or "").strip() or path.stem
            created_course = _request(
                settings,
                "POST",
                "/course/",
                json_body={"title": title, "subject": "", "audience": "", "duration_minutes": 60},
            )
            target_course = str((created_course or {}).get("id") or "")
            if not target_course:
                raise ToolError("后端创建课程后没有返回 id", "请查看后端日志确认 /api/course/ 是否正常。")
            created = True

        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as handle:
            payload = handle.read()
        result = _request(
            settings,
            "POST",
            "/material/upload",
            data={"course_id": target_course, "title": path.stem},
            files={"file": (path.name, payload, mime)},
        )

        body = result if isinstance(result, dict) else {}
        summary_bits = [f"已上传《{path.name}》（{len(payload)} 字节）"]
        if body.get("chars") or (body.get("meta") or {}).get("chars"):
            chars = body.get("chars") or (body.get("meta") or {}).get("chars")
            summary_bits.append(f"解析出约 {chars} 字")
        if body.get("job_id"):
            summary_bits.append("音视频已入队转写，可用 check_task_status 跟进")
        if created:
            summary_bits.append(f"已新建课程（题目「{course_title or path.stem}」）")
        summary = "；".join(summary_bits)

        return {
            "ok": True,
            "task_id": target_course,
            "course_id": target_course,
            "course_created": created,
            "material": body,
            "summary": summary,
            "next_step": "继续 upload_source 补资料，或调用 generate_outline 生成大纲（需要 goal）。",
        }
    except ToolError as exc:
        return exc.as_dict()


@mcp.tool()
def generate_outline(
    task_id: str,
    goal: str = "",
    num_chapters: int = 4,
    style: str = "正式",
    audience: str = "",
) -> dict[str, Any]:
    """根据已上传资料生成课程大纲。

    资料多、章节多时可能超过 30 秒：本工具先等约 18 秒，超时就返回
    ``status=running`` + ``op_id``，用 check_task_status(task_id) 取结果，**不要**重复调用。
    生成后**必须**把大纲念给用户看、等用户明确同意，再调用 confirm_outline。
    """
    try:
        settings = Settings()
        course = _course(settings, task_id)
        final_goal = (goal or "").strip() or f"讲清《{course.get('title') or '本课程'}》的核心内容"
        if len(final_goal) < 3:
            raise ToolError(
                "课程目标（goal）太短，后端要求至少 3 个字",
                "请用一句人话说清这门课要让学员学会什么。",
            )
        if not 1 <= int(num_chapters) <= 12:
            raise ToolError(
                f"num_chapters={num_chapters} 超出范围",
                "后端允许 1~12 章，请给一个整数。",
            )
        body = {
            "course_id": task_id,
            "goal": final_goal,
            "audience": (audience or course.get("audience") or "").strip(),
            "num_chapters": int(num_chapters),
            "style": (style or "正式").strip(),
        }

        def _call() -> Any:
            return _request(settings, "POST", "/plan/generate", json_body=body, long_running=True)

        op_id, done, box = _start_op(task_id, "generate_outline", _call, OUTLINE_WAIT_S)
        if not done:
            return {
                "ok": True,
                "task_id": task_id,
                "status": "running",
                "op_id": op_id,
                "progress": "大纲还在生成（资料较大或章节较多）。",
                "next_step": f"调用 check_task_status(task_id='{task_id}') 取结果；"
                "**不要**重复调用 generate_outline。",
            }
        error = box.get("error")
        if isinstance(error, ToolError):
            return error.as_dict()
        return _outline_payload(task_id, box.get("result"), final_goal)
    except ToolError as exc:
        return exc.as_dict()


def _outline_payload(task_id: str, data: Any, final_goal: str) -> dict[str, Any]:
    """把后端大纲摊成给 AI 看得懂的形状（章 + 知识点，都带 id）。"""
    if not isinstance(data, dict):
        raise ToolError("后端返回的大纲格式异常", "请确认 COURSE_BACKEND_URL 指向 CourseGenie 后端。")
    outline = _outline_view(data)
    return {
        "ok": True,
        "task_id": task_id,
        "status": "success",
        "outline_id": data.get("outline_id"),
        "goal": data.get("goal") or final_goal,
        "outline": outline,
        "chapters": sum(1 for n in outline if n["type"] == "chapter"),
        "points": sum(1 for n in outline if n["type"] == "point"),
        "self_check": data.get("check"),
        "next_step": "把大纲给用户看；用户同意后调用 confirm_outline(task_id, approved=true)。"
        "用户要改就先调 confirm_outline(approved=false) 或让用户直接说改哪里。",
    }


@mcp.tool()
def generate_concepts(task_id: str) -> dict[str, Any]:
    """从整门课抽取「概念库」（定义 / 要点 / 概念间关系）。

    **导出笔记前调它**：Obsidian 里概念之间的双链就是靠这份概念库生成的；
    不抽就只有图链、没有概念链。异步：立刻返回 job_id，用 check_task_status 跟进。
    """
    try:
        settings = Settings()
        course = _course(settings, task_id)
        if str(course.get("status") or "") != "approved":
            raise ToolError(
                "大纲还没通过，后端不允许抽取概念",
                "先让用户确认大纲，再调用 confirm_outline(task_id, approved=true)。",
            )
        queued = _request(settings, "POST", f"/concept/course/{task_id}/generate")
        job_id = str((queued or {}).get("job_id") or "")
        if not job_id:
            raise ToolError(
                "后端没有返回 job_id",
                "请查看后端日志确认 /api/concept/course/{id}/generate 是否正常。",
            )
        job = _poll_job(settings, job_id, budget_s=max(5.0, TOOL_BUDGET_S - 9.0))
        status = str((job or {}).get("status") or "running")
        payload: dict[str, Any] = {
            "ok": True,
            "task_id": task_id,
            "job_id": job_id,
            "status": status,
        }
        if status == "success":
            result = (job or {}).get("result") or {}
            payload["concept_report"] = result
            payload["next_step"] = (
                "概念库已就绪：接着 export_to_obsidian 导出，笔记之间会自动生成概念双链。"
            )
        elif status == "failed":
            payload["ok"] = False
            payload["error"] = f"概念抽取失败：{(job or {}).get('error') or '后端没有给出原因'}"
            payload["hint"] = "请检查后端日志（多半是模型调用问题），修好后重试。"
        else:
            payload["next_step"] = (
                f"概念抽取还在跑：调用 check_task_status(task_id='{task_id}', job_id='{job_id}') 跟进，"
                "**不要**重复调用 generate_concepts。"
            )
        return payload
    except ToolError as exc:
        return exc.as_dict()
@mcp.tool()
def confirm_outline(task_id: str, approved: bool = True) -> dict[str, Any]:
    """用户看一眼大纲、点头（或摇头）之后调用。通过后大纲被锁定，成为生成依据。

    1.0.4：如果 `generate_outline` 当时返回的是 `status=running`（大纲还在后台生成），
    本工具会**先等最多 15 秒**再提交，避免后端回「还没有大纲，请先生成大纲」这种看不懂的 400。
    """
    try:
        settings = Settings()
        gate = _wait_outline(task_id)
        if gate["still_running"]:
            raise ToolError(
                f"大纲还在生成中（op_id={gate['op_id']}），现在通过会失败",
                f"先用 check_task_status(task_id='{task_id}') 取回大纲、给用户看，"
                "用户同意后再调用 confirm_outline。",
            )
        if gate["error"]:
            raise ToolError(
                f"大纲生成失败，无法通过：{gate['error']}",
                "请先重新调用 generate_outline（或让用户改一下资料/目标）再继续。",
            )
        data = _request(
            settings, "POST", "/plan/approve", json_body={"course_id": task_id, "approved": bool(approved)}
        )
        status = str((data or {}).get("status") or "")
        return {
            "ok": True,
            "task_id": task_id,
            "status": status,
            "approved": bool(approved),
            "next_step": (
                "调用 generate_sample(task_id, lesson_index=1) 生成第一节样课，"
                "让用户确认插图样式后再 generate_all。"
                if approved
                else "大纲已退回可编辑状态：改完（或重新 generate_outline）后再次 confirm_outline(approved=true)。"
            ),
        }
    except ToolError as exc:
        return exc.as_dict()


@mcp.tool()
def generate_sample(
    task_id: str,
    lesson_index: int = 1,
    tone: str = "正式",
    figure_style: str = "",
) -> dict[str, Any]:
    """生成「样课」：某一个大纲知识点（第 lesson_index 课，1 起）的完整讲义与插图样式。

    讲义生成通常要几十秒：本工具会等最多约 18 秒，若还没好就返回 job_id 与 running，
    请用 check_task_status(task_id, job_id) 继续跟进，别重复下单。
    """
    try:
        settings = Settings()
        course = _course(settings, task_id)
        if str(course.get("status") or "") != "approved":
            raise ToolError(
                "大纲还没通过，后端不允许生成讲义",
                "先让用户确认大纲，再调用 confirm_outline(task_id, approved=true)。",
            )
        plan = _plan_or_none(settings, task_id)
        if not plan:
            raise ToolError("这门课还没有大纲", "先调用 generate_outline。")
        points = _point_nodes(plan)
        if not points:
            raise ToolError("大纲里没有可生成的知识点", "请让用户在大纲里加至少一个知识点节点。")
        index = int(lesson_index)
        if not 1 <= index <= len(points):
            raise ToolError(
                f"lesson_index={index} 超出范围（这门课共 {len(points)} 个知识点）",
                f"请给 1~{len(points)} 之间的整数。",
            )
        node = points[index - 1]
        node_id = str(node.get("id") or "")

        if (figure_style or "").strip():
            try:
                _request(
                    settings,
                    "POST",
                    "/lecture/style-anchor",
                    json_body={
                        "course_id": task_id,
                        "tone": (tone or "正式").strip(),
                        "figure_style": figure_style.strip(),
                    },
                )
            except ToolError as exc:
                logger.info("样板锚设置失败（继续生成）：%s", exc.message)

        queued = _request(
            settings,
            "POST",
            "/lecture/generate",
            json_body={
                "course_id": task_id,
                "node_id": node_id,
                "tone": (tone or "正式").strip(),
                "allow_overwrite_locked": False,
            },
        )
        job_id = str((queued or {}).get("job_id") or "")
        if not job_id:
            raise ToolError("后端没有返回 job_id", "请查看后端日志确认 /api/lecture/generate 是否正常。")

        job = _poll_job(settings, job_id, budget_s=max(5.0, TOOL_BUDGET_S - 7.0))
        status = str((job or {}).get("status") or "running")
        result = (job or {}).get("result") or {}
        sample = result.get("item") if isinstance(result, dict) else None

        payload: dict[str, Any] = {
            "ok": True,
            "task_id": task_id,
            "job_id": job_id,
            "status": status,
            "lesson_index": index,
            "lesson": {"node_id": node_id, "key": node.get("node_key"), "title": node.get("title")},
        }
        if status == "failed":
            payload["ok"] = False
            payload["error"] = f"样课生成失败：{(job or {}).get('error') or '后端没有给出原因'}"
            payload["hint"] = "请检查后端日志（多半是模型调用或素材解析问题），修好后重试。"
            return payload
        if sample:
            item_id = str(sample.get("id") or "")
            anchor_info: dict[str, Any] = {}
            if item_id:
                with _tasks_lock:
                    _task(task_id)["sample_item_id"] = item_id
                _persist_tasks()
                try:
                    anchor_info = _ensure_style_anchor(settings, task_id, tone, figure_style)
                except ToolError as exc:
                    logger.info("样课已生成，但样板锚没设上：%s", exc.message)
                    anchor_info = {"error": exc.message}
            payload["sample_content"] = sample
            payload["style_anchor"] = anchor_info
            payload["next_step"] = (
                "把样课与插图样式给用户确认；用户认可后再调用 "
                "generate_all(task_id, sample_approved=true)。"
                "（这节样课已自动设成「样板」，批量生成会沿用它的风格；"
                "用户要换风格就再 generate_sample(figure_style=...)。）"
            )
        else:
            payload["next_step"] = (
                f"样课还在生成。请调用 check_task_status(task_id='{task_id}', job_id='{job_id}') 查看进度，"
                "**不要**重复调用 generate_sample。"
            )
        return payload
    except ToolError as exc:
        return exc.as_dict()


@mcp.tool()
def generate_all(
    task_id: str,
    sample_approved: bool = False,
    tone: str = "正式",
    force: bool = False,
    ignore_style_anchor: bool = False,
) -> dict[str, Any]:
    """批量生成整门课的全部讲义（异步）。

    立刻返回 task_id + job_id，不阻塞；用 check_task_status 跟进。
    ``sample_approved`` 必须为 true —— 用户没确认样课样式之前不允许批量生成。
    """
    try:
        settings = Settings()
        if not sample_approved:
            raise ToolError(
                "用户还没确认样课样式，不能批量生成",
                "先 generate_sample 生成样课，把插图样式给用户看；用户确认后"
                "再调用 generate_all(task_id, sample_approved=true)。",
            )
        course = _course(settings, task_id)
        if str(course.get("status") or "") != "approved":
            raise ToolError(
                "大纲还没通过，后端不允许批量生成",
                "先 confirm_outline(task_id, approved=true)。",
            )
        anchor_info: dict[str, Any] = {"skipped": True}
        if not ignore_style_anchor:
            anchor_info = _ensure_style_anchor(settings, task_id, tone)
        queued = _request(
            settings,
            "POST",
            "/lecture/generate-all",
            json_body={
                "course_id": task_id,
                "tone": (tone or "正式").strip(),
                "force": bool(force),
                "ignore_style_anchor": bool(ignore_style_anchor),
                # 用户已确认样课（sample_approved=true）本身就是「允许覆盖样板节」的明确同意
                "allow_anchor_overwrite": bool(sample_approved),
            },
        )
        job_id = str((queued or {}).get("job_id") or "")
        if not job_id:
            raise ToolError("后端没有返回 job_id", "请查看后端日志确认 /api/lecture/generate-all。")
        return {
            "ok": True,
            "task_id": task_id,
            "job_id": job_id,
            "progress": f"已入队（job {job_id}）。整门课通常需要几分钟，请用 check_task_status 轮询。",
            "style_anchor": anchor_info,
            "next_step": (
                f"调用 check_task_status(task_id='{task_id}', job_id='{job_id}') 查看进度；"
                "status=success 后再请用户确认，最后 export_to_obsidian。"
            ),
        }
    except ToolError as exc:
        return exc.as_dict()


@mcp.tool()
def export_to_obsidian(
    task_id: str,
    vault_path: str,
    output_rel: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """把整门课写进**你本机**的 Obsidian 笔记库（每节一篇笔记，概念间自动生成双链）。

    v1.0.2 起：笔记由后端生成后**打包下载到你本机**并解压到 ``vault_path/<课程名>/``
    —— 后端在云端时也一样，文件真的落在你自己的盘上。
    后台线程执行，**立刻返回**；用 check_task_status(task_id) 跟进。

    Args:
        task_id: 课程 ID。
        vault_path: 本机目标文件夹（必须已存在）。
        output_rel: **兼容参数，v1.0.2 起已忽略**；导出目录固定为 ``vault_path/<课程名>``。
        force: **兼容参数，v1.0.2 起已忽略**；下载解压会覆盖同名文件。
    """
    try:
        settings = Settings()
        raw = (vault_path or "").strip().strip('"').strip("'")
        if not raw:
            raise ToolError(
                "vault_path 不能为空",
                "请给 Obsidian 库（或任意目标文件夹）的绝对路径，例如 D:\\我的课程。",
            )
        vault = Path(raw).expanduser()
        if not vault.exists():
            raise ToolError(
                f"这个目录不存在：{vault}",
                "请先创建该文件夹（或在 Obsidian 里打开它），再重试；"
                "路径必须是连接器所在机器上的真实路径。",
            )
        if not vault.is_dir():
            raise ToolError(f"这不是一个文件夹：{vault}", "vault_path 要指向目录。")

        course = _course(settings, task_id)
        title = str(course.get("title") or task_id)
        target = vault / _safe_dirname(title)

        op_id = _new_op(task_id, "export_obsidian")

        def _do_export() -> dict[str, Any]:
            # 1) 后端「导出 + 打包」（服务端先落一份，再打 zip）
            packed = _request(
                settings,
                "POST",
                "/publish/export-notes-zip",
                json_body={"course_id": task_id, "scope": "all"},
                long_running=True,
            )
            url = str((packed or {}).get("url") or "")
            if not url.startswith("/"):
                raise ToolError(
                    "后端没有返回笔记包地址",
                    "请查看后端日志确认 /api/publish/export-notes-zip 是否正常。",
                )
            # 2) 下载 zip 到内存
            data = _download(settings, url)
            # 3) 解压到本机 vault_path/<课程名>/
            written = _extract_zip(data, target)
            return {
                "vault_path": str(vault),
                "local_dir": str(target),
                "files_written": len(written),
                "files": written[:200],
                "zip": (packed or {}).get("file"),
            }

        _run_async(task_id, op_id, _do_export)
        return {
            "ok": True,
            "task_id": task_id,
            "op_id": op_id,
            "export_status": "running",
            "vault_path": str(vault),
            "local_dir": str(target),
            "progress": "后台正在生成笔记包并下载到本机；导出期间**不要**重复调用。",
            "next_step": f"调用 check_task_status(task_id='{task_id}') 查看 export 状态与写盘文件数。",
        }
    except ToolError as exc:
        return exc.as_dict()


@mcp.tool()
def check_task_status(task_id: str, job_id: str = "") -> dict[str, Any]:
    """查进度：课程阶段（资料/大纲/样课/批量/导出）+ 指定后端 job + 本地长任务。

    长任务（generate_all / export_to_obsidian）一律靠这个工具跟进。
    """
    try:
        settings = Settings()
        course = _course(settings, task_id)
        stage = _stage_of(settings, task_id, course)

        payload: dict[str, Any] = {"ok": True, "task_id": task_id, "course": stage}

        if job_id:
            try:
                job = _request(settings, "GET", f"/content/jobs/{job_id}")
                payload["job"] = {
                    "job_id": (job or {}).get("job_id"),
                    "status": (job or {}).get("status"),
                    "error": (job or {}).get("error") or "",
                    "result_keys": sorted(((job or {}).get("result") or {}).keys())
                    if isinstance((job or {}).get("result"), dict)
                    else [],
                    "result": (job or {}).get("result"),
                }
            except ToolError as exc:
                payload["job"] = {"job_id": job_id, "error": exc.message}

        with _tasks_lock:
            ops = dict((_tasks.get(task_id) or {}).get("ops") or {})
        payload["local_tasks"] = [
            {
                "op_id": op_id,
                "kind": op.get("kind"),
                "status": op.get("status"),
                "started_at": op.get("started_at"),
                "finished_at": op.get("finished_at"),
                "error": op.get("error") or "",
                "result": op.get("result"),
            }
            for op_id, op in sorted(ops.items(), key=lambda kv: str(kv[1].get("started_at")), reverse=True)
        ]
        running = any(op.get("status") == "running" for op in ops.values())
        payload["still_running"] = running or str((payload.get("job") or {}).get("status") or "") in (
            "running",
            "pending",
            "queued",
        )
        return payload
    except ToolError as exc:
        return exc.as_dict()


# --------------------------------------------------------------------------------------
# 远程 HTTP 传输（streamable-http）+ Bearer 鉴权
# --------------------------------------------------------------------------------------

MCP_HEALTH_PATH = "/healthz"


def _header_value(scope: dict[str, Any], name: str) -> str:
    for key, value in scope.get("headers") or []:
        if key.decode("latin-1").lower() == name:
            return value.decode("latin-1").strip()
    return ""


def _presented_token(scope: dict[str, Any]) -> str:
    raw = _header_value(scope, "authorization")
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    for name in ("x-workbuddy-token", "x-api-key"):
        value = _header_value(scope, name)
        if value:
            return value
    return ""


async def _send_json(
    send,
    status: int,
    payload: dict[str, Any],
    headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    base = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    await send({"type": "http.response.start", "status": status, "headers": base + (headers or [])})
    await send({"type": "http.response.body", "body": body})


class TokenAuthMiddleware:
    """公开端点的门禁：/healthz 与 / 免鉴权，其余一律要求令牌。"""

    def __init__(self, app, token: str) -> None:
        self.app = app
        self._token = token

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path") in (MCP_HEALTH_PATH, "/"):
            await _send_json(
                send,
                200,
                {
                    "ok": True,
                    "server": SERVER_NAME,
                    "transport": "streamable-http",
                    "mcp_path": mcp.settings.streamable_http_path,
                },
            )
            return
        presented = _presented_token(scope)
        if not presented or not compare_digest(presented, self._token):
            await _send_json(
                send,
                401,
                {
                    "ok": False,
                    "error": "缺少或错误的访问令牌",
                    "hint": "在连接设置里填平台下发的 MCP 令牌（Authorization: Bearer …）。",
                },
                [(b"www-authenticate", b'Bearer realm="coursegenie-mcp"')],
            )
            return
        await self.app(scope, receive, send)


def _run_http(token: str) -> None:
    import uvicorn

    settings = mcp.settings
    logger.info(
        "远程 MCP：http://%s:%s%s（Bearer 鉴权，健康检查 %s）",
        settings.host,
        settings.port,
        settings.streamable_http_path,
        MCP_HEALTH_PATH,
    )
    uvicorn.run(
        TokenAuthMiddleware(mcp.streamable_http_app(), token),
        host=settings.host,
        port=settings.port,
        log_level="info",
        access_log=False,
    )


# --------------------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------------------


def configure_logging() -> None:
    """stdio 传输下 stdout 属于协议通道，日志一律走 stderr。"""
    if logger.handlers:
        return
    handler = logging.StreamHandler()  # 默认 stderr
    handler.setFormatter(logging.Formatter("[coursegenie-mcp] %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # httpx 自己也会打 INFO（"HTTP Request: ..."）—— 降到 WARNING，避免刷屏
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _parse_cli(argv: list[str]) -> Any:
    import argparse

    parser = argparse.ArgumentParser(prog="coursegenie-mcp", add_help=True)
    parser.add_argument("--transport", default=os.environ.get("MCP_TRANSPORT", "stdio"), help="stdio（默认）或 streamable-http")
    parser.add_argument("--host", default="", help="HTTP 监听地址，默认读 MCP_HOST 或 127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="HTTP 监听端口，默认读 MCP_PORT 或 8001")
    parser.add_argument("--path", default="", help="MCP 路径，默认读 MCP_PATH 或 /mcp")
    args, _unknown = parser.parse_known_args(argv)
    return args


def main(argv: list[str] | None = None) -> None:
    configure_logging()
    _load_tasks()
    args = _parse_cli(sys.argv[1:] if argv is None else argv)
    transport = str(args.transport or "stdio").strip().lower().replace("_", "-")
    if transport == "streamablehttp":
        transport = "streamable-http"
    # 只报「有没有配」，绝不打印凭据内容
    logger.info(
        "启动：transport=%s，后端=%s，DeepSeek 凭据=%s",
        transport,
        _safe_backend_url(),
        "已配置" if os.environ.get("DEEPSEEK_API_KEY", "").strip() else "缺失",
    )
    if transport == "stdio":
        mcp.run()
        return
    if transport not in ("streamable-http", "http"):
        raise SystemExit(f"不支持的传输方式：{transport}（只支持 stdio / streamable-http）")
    token = os.environ.get("MCP_AUTH_TOKEN", "").strip()
    if len(token) < MIN_MCP_TOKEN_CHARS:
        raise SystemExit(
            f"远程 MCP 必须设置 MCP_AUTH_TOKEN（至少 {MIN_MCP_TOKEN_CHARS} 个字符）："
            "公开端点不鉴权，谁拿到地址都能烧掉后端模型额度。"
        )
    if args.host:
        mcp.settings.host = args.host
    if args.port:
        mcp.settings.port = args.port
    if args.path:
        mcp.settings.streamable_http_path = args.path
    # 远程模式追加第 10 个工具；stdio 连接器（PyPI 1.0.5）仍是 9 个，审核口径不变。
    from course_generator.remote_tools import register_remote_tools

    register_remote_tools(mcp)
    _run_http(token)


def _safe_backend_url() -> str:
    try:
        return _normalize_backend_url(os.environ.get("COURSE_BACKEND_URL", "").strip() or DEFAULT_BACKEND_URL)
    except Exception:  # noqa: BLE001
        return DEFAULT_BACKEND_URL


if __name__ == "__main__":
    main()
