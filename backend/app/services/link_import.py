"""任务 118：把「粘贴 YouTube 链接 → 变成课程素材」做进产品（素材导入的第三种来源）。

规格 = ``AI协作区\给Codex-2026-10-01-任务118.md``（本轮开工时 ``HANDOFF.md`` 文末**没有**
「任务 118 说明」，故按任务书正文执行，交付后补录一节）。**0 LLM**：抓字幕走 yt-dlp、
没字幕才下音频走**既有本地 ASR**（``app.services.material`` 的 faster-whisper 链路），
全程不调模型、不花模型钱。

DSH 2026-10-01 手工跑通这条链路时踩出来的**硬事实**（产品化必须处理，本模块逐条落地）：

1. **cookie 必须是 ``.youtube.com`` 域** —— 只给 ``.google.com`` 会被当未登录，报
   ``Sign in to confirm you're not a bot`` ⇒ :func:`inspect_cookies` 先看域对不对，
   :func:`classify_tool_error` 把这类报错翻成「登录态问题」的人话（一眼能看出不是课的问题）；
2. **抓 YouTube 需要 JS 运行时** —— yt-dlp 2026 版默认 deno；本机没 deno 但有 node 时，
   由 :func:`js_runtime_args` 自动补 ``--js-runtimes node``（缺两个都缺才报不可用）；
3. **字幕接口有 429 限流** —— 一份 cookie 大约只能跑一个播放列表 ⇒ :class:`RateLimiter`
   （每请求 ≥4 秒、列表内串行且两条之间 ≥20 秒）+ :func:`backoff_delay`（指数退避，上限 3 次）；
4. **有字幕就不下音频**（快几十倍）—— :func:`run_link_import` 的顺序**写死**：
   ① 抓字幕（en / zh-Hans 等）→ ② 没字幕才下音频走既有本地 ASR → ③ 都不行就明确失败原因，
   素材行保留（可重试）。

安全：cookie **只按路径交给 yt-dlp**（``--cookies``），**不复制、不进日志、不进接口返回、
不写进库** —— ``meta.link`` 里只有 ``cookies: true/false`` 与「域对不对」的结论，
没有路径也没有值。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.course import Course, SourceMaterial
from app.services import material as material_service

logger = logging.getLogger(__name__)

LINK_SOURCE = "youtube"
LINK_META_KEY = "link"

# ── 外部依赖（可选）：yt-dlp 与 JS 运行时 ────────────────────────────────────
YTDLP_ENV_EXE = "YTDLP_EXE"      # 覆盖可执行文件路径（进程环境变量或 `.env` 都能写，见 119）
YTDLP_ENV_CMD = "YTDLP_CMD"      # 覆盖整条命令（如 ``python -m yt_dlp``）
YTDLP_ENV_COOKIES = "YTDLP_COOKIES"   # cookies.txt 的路径（进程环境变量或 `.env`）
YTDLP_VERSION_TIMEOUT = 15.0     # ``--version`` 的上限（只查版本，不联网）
YTDLP_PROBE_CACHE: dict[str, dict] = {}
JS_RUNTIME_CACHE: dict[str, dict] = {}

# ── 抓取口径（顺序与间隔都写死，别随手改）──────────────────────────────────
SUB_LANGS = ("en", "zh-Hans", "zh-Hant", "zh")
"""字幕语种优先级：**英 → 简中 → 繁中 → 中**（DSH 实测：中/英文频道通常有 en / zh-Hans）。"""
RATE_MIN_INTERVAL = 4.0          # 每请求间隔 ≥4 秒
RATE_PLAYLIST_INTERVAL = 20.0    # 同一列表里两条视频之间 ≥20 秒
RATE_MAX_ATTEMPTS = 3            # 429 退避重试上限（含第一次请求）
RATE_BACKOFF_BASE = 8.0          # 退避 8s → 16s → 32s（指数）
RATE_BACKOFF_CAP = 64.0
FETCH_TIMEOUT_S = 300.0          # 单次 yt-dlp 调用的上限（一个视频的字幕通常几秒）
LINK_MAX_ITEMS = 20              # 播放列表一次最多导入多少条（正文有 200k 字上限，见文档「边界」）
TITLE_MAX = 100

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,64}$")
_VERSION_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}")
_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
             "youtu.be", "www.youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com"}
_PATH_ID_PREFIXES = ("shorts", "live", "embed", "v")

# ---------------------------------------------------------------------------
# 外部依赖探测（只查可执行文件 + 版本，**不联网**；照任务 116 的能力口径）
# ---------------------------------------------------------------------------
@dataclass
class ToolRun:
    """一次 yt-dlp 调用的结果（**自己起进程时用 argv 列表，不过 shell**）。

    ``error`` 是「连进程都没起来 / 超时」时的人话；yt-dlp 自己的报错在 ``out`` 里。
    """

    args: list[str]
    code: int
    out: str = ""
    error: str = ""


def run_ytdlp(args: list[str], *, timeout: float = FETCH_TIMEOUT_S) -> ToolRun:
    """默认执行器：真跑 yt-dlp。测试用假执行器替换它（见 ``FetchOptions.runner``）。"""
    try:
        proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return ToolRun(args, -1, error=f"yt-dlp 超过 {int(timeout)} 秒没返回，已中止这一次抓取")
    except OSError as exc:                      # 可执行文件不在 / 没权限
        return ToolRun(args, -1, error=f"yt-dlp 起不来（{exc}）")
    return ToolRun(args, proc.returncode, (proc.stdout or "") + (proc.stderr or ""))


def _env_or_setting(env_name: str, value: str) -> str:
    """任务 119 的**回落链唯一出处**（与任务 104 的 ``ASR_MODEL`` 同款口径）：
    **进程环境变量（优先，既有口径不变）⇒ `.env`（经 pydantic ``Settings``）⇒ 空**。

    为什么不能只读 ``os.environ``：本项目的 `.env` 是给 pydantic ``Settings`` 读的 ——
    pydantic-settings **只把 `.env` 读进 Settings、不写回 `os.environ`** ⇒ 只读 ``os.environ`` 时
    「往 `.env` 里写这一项」**完全不生效**（任务 104 的 ``ASR_MODEL`` 踩过同一个坑，这里是同一个病）。
    """
    return (os.environ.get(env_name) or "").strip() or (value or "").strip()


def ytdlp_cmd_setting() -> str:
    """``YTDLP_CMD``（整条命令）：进程环境变量 ⇒ `.env` ⇒ 空。"""
    return _env_or_setting(YTDLP_ENV_CMD, get_settings().ytdlp_cmd)


def ytdlp_exe_setting() -> str:
    """``YTDLP_EXE``（可执行文件路径）：进程环境变量 ⇒ `.env` ⇒ 空。"""
    return _env_or_setting(YTDLP_ENV_EXE, get_settings().ytdlp_exe)


def _split_cmd(text: str) -> list[str]:
    """把 ``YTDLP_CMD`` 拆成 argv（Windows 上按空白拆，路径含空格请用 ``YTDLP_EXE``）。"""
    return [part for part in text.strip().split() if part]


def ytdlp_candidates() -> list[list[str]]:
    r"""按优先级给出「可能的 yt-dlp 启动方式」（只列，不执行）。

    ① ``YTDLP_CMD``（整条命令，如 ``python -m yt_dlp``）② ``YTDLP_EXE``（可执行文件路径）
    ③ PATH 上的 ``yt-dlp`` ④ 用户目录里常见落地位置。**①②都按任务 119 的回落链读**
    （进程环境变量 ⇒ `.env` ⇒ 空），不是只读进程环境变量。**本机实测**：
    ``...\Python313\Scripts\yt-dlp.exe`` 文件在、但对应解释器已被删 ⇒ 跑不起来（exit 1 无输出），
    而 ``python -m yt_dlp`` 可用 ⇒ 所以这里要逐个**试版本**，不能「文件存在就算可用」。
    """
    out: list[list[str]] = []
    cmd = ytdlp_cmd_setting()
    if cmd:
        out.append(_split_cmd(cmd))
    exe = ytdlp_exe_setting()
    if exe:
        out.append([exe])
    for name in ("yt-dlp", "yt-dlp.exe"):
        found = shutil.which(name)
        if found:
            out.append([found])
            break
    for cand in (Path.home() / ".local" / "bin" / "yt-dlp.exe",
                 Path.home() / "bin" / "yt-dlp.exe"):
        if cand.exists():
            out.append([str(cand)])
    return out


def _probe_version(cmd: list[str]) -> str:
    """跑 ``<cmd> --version``；拿到形如 ``2026.08.19`` 的版本号才算「可用」，否则空串。"""
    if not cmd:
        return ""
    run = run_ytdlp([*cmd, "--version"], timeout=YTDLP_VERSION_TIMEOUT)
    if run.code != 0:
        return ""
    for line in run.out.splitlines():
        line = line.strip()
        if _VERSION_RE.match(line):
            return line
    return ""


def ytdlp_probe(*, refresh: bool = False) -> dict:
    """yt-dlp 能不能用：``{available, cmd, exe, version, reason}``。

    **探测只查可执行文件 + 版本，不发任何网络请求**；结果按进程缓存（``--version`` 每次
    都要起进程，而 ``GET /api/health`` 会读能力清单，不能每读一次起一次）。
    """
    if not refresh and "ytdlp" in YTDLP_PROBE_CACHE:
        return dict(YTDLP_PROBE_CACHE["ytdlp"])
    info: dict = {"key": "ytdlp", "available": False, "cmd": [], "exe": "", "version": "",
                  "reason": YTDLP_MISSING_REASON}
    for cmd in ytdlp_candidates():
        version = _probe_version(cmd)
        if version:
            info.update({"available": True, "cmd": list(cmd), "exe": cmd[0],
                         "version": version, "reason": ""})
            break
        info["exe"] = info["exe"] or cmd[0]
    if not info["available"] and info["exe"]:
        info["reason"] = (f"找到 yt-dlp（{info['exe']}）但跑不起来（`--version` 失败）："
                          "常见原因是它指向的 Python 解释器已不在或文件被截断。"
                          + YTDLP_MISSING_REASON)
    YTDLP_PROBE_CACHE["ytdlp"] = dict(info)
    return dict(info)


YTDLP_MISSING_REASON = (
    "本机没有可用的 yt-dlp：「从链接导入」会跳过并写明原因，其它功能一律照常 —— "
    "这是环境缺组件，不是你的课有问题"
)
YTDLP_INSTALL = (
    "装 yt-dlp（可选依赖，只影响「从链接导入」）：① 下载官方单文件 yt-dlp.exe 放到任意目录，"
    "然后在 backend/.env 里写 YTDLP_EXE=<完整路径>（推荐；也可以改成设同名进程环境变量，"
    "进程环境变量优先）；② 或 pip install yt-dlp 装到当前 venv，再写 YTDLP_CMD=python -m yt_dlp"
    "（YTDLP_CMD 优先级高于 YTDLP_EXE）；两者都不写就找 PATH 上的 yt-dlp"
)

def js_runtime_probe(*, refresh: bool = False) -> dict:
    """JS 运行时（yt-dlp 抓 YouTube 需要）：``{deno, node, available, reason}``。

    只查可执行文件在不在（``shutil.which`` + ``~/.deno/bin``），**不跑进程、不联网**。
    yt-dlp 2026 版默认用 deno；本机有 node 也能跑（由 :func:`js_runtime_args` 补参数）。
    """
    if not refresh and "js" in JS_RUNTIME_CACHE:
        return dict(JS_RUNTIME_CACHE["js"])
    deno = shutil.which("deno") or shutil.which("deno.exe") or ""
    for cand in (Path.home() / ".deno" / "bin" / "deno.exe",
                 Path.home() / ".deno" / "bin" / "deno"):
        if not deno and cand.exists():
            deno = str(cand)
    node = shutil.which("node") or shutil.which("node.exe") or ""
    available = bool(deno or node)
    info = {"deno": deno, "node": node, "available": available,
            "reason": "" if available else JS_RUNTIME_MISSING_REASON}
    JS_RUNTIME_CACHE["js"] = dict(info)
    return dict(info)


JS_RUNTIME_MISSING_REASON = (
    "本机既没有 deno 也没有 Node.js：yt-dlp 抓 YouTube 会报「No supported JavaScript runtime」"
    "⇒「从链接导入」会跳过并写明原因，其它功能一律照常 —— 这是环境缺组件，不是你的课有问题"
)
JS_RUNTIME_INSTALL = (
    "装一个 JS 运行时（可选依赖，只影响「从链接导入」）：deno 用 winget install DenoLand.Deno；"
    "或装 Node.js（本系统探测到 node 后会自动给 yt-dlp 传 --js-runtimes node）"
)


def js_runtime_args(probe: dict | None = None) -> list[str]:
    """给 yt-dlp 的 JS 运行时参数：有 deno ⇒ 不加（那是默认值）；只有 node ⇒ 显式指过去。"""
    info = probe if probe is not None else js_runtime_probe()
    if info.get("deno"):
        return []
    if info.get("node"):
        return ["--js-runtimes", f"node:{info['node']}"]
    return []


# ---------------------------------------------------------------------------
# cookie 的处理口径（安全第一）：只看「域对不对」，**从不读值、从不打印、从不落库**
# ---------------------------------------------------------------------------
def cookie_domains(path: str | Path) -> list[str]:
    """Netscape 格式 cookies.txt 的**域列**（第 1 列）—— 只取域名，不碰 cookie 的值。"""
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    domains: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        first = line.split("\t", 1)[0].strip()
        if first:
            domains.append(first)
    return domains


def inspect_cookies(path: str | Path | None) -> dict:
    """cookies.txt 体检：``{given, exists, is_file, youtube_domain, domains, hint}``。

    硬事实 1（DSH 实测）：**cookie 必须是 ``.youtube.com`` 域** —— 只给 ``.google.com``
    会被当未登录，报 ``Sign in to confirm you're not a bot``。这里只判断「域对不对」，
    **返回里不出现任何 cookie 值**；不存在的路径由路由侧拒收（400 人话）。
    """
    info = {"given": bool(path), "exists": False, "is_file": False, "youtube_domain": False,
            "domains": [], "hint": ""}
    if not path:
        return info
    p = Path(path)
    info["exists"] = p.exists()
    info["is_file"] = p.is_file()
    if not info["is_file"]:
        info["hint"] = "cookie 路径不是一个文件，请给出 cookies.txt 的完整路径"
        return info
    domains = cookie_domains(p)
    info["domains"] = sorted({d for d in domains if d.startswith(".") or "." in d})
    info["youtube_domain"] = any("youtube.com" in d for d in domains)
    if not info["youtube_domain"]:
        info["hint"] = ("这份 cookies.txt 里没有 .youtube.com 域的 cookie（只有 "
                        + "、".join(info["domains"][:4] or ["（空）"]) + "）：YouTube 会把它当未登录，"
                        "导出 cookie 时请选中 youtube.com；cookie 也会过期，失效后重新导出一份即可")
    return info

# ---------------------------------------------------------------------------
# 报错翻译（人话）与礼貌抓取（限流 + 429 退避）
# ---------------------------------------------------------------------------
LOGIN_PATTERNS = ("sign in to confirm you", "sign in to confirm", "use --cookies",
                  "login required", "cookies for the authentication")
RATE_PATTERNS = ("http error 429", "429", "too many requests", "rate limit")
JS_PATTERNS = ("no supported javascript runtime", "javascript runtime", "js runtime",
               "yt-dlp-ejs", "remote components")
GONE_PATTERNS = ("private video", "video unavailable", "this video is unavailable",
                 "members-only", "this video is available",
                 "removed by the uploader", "not available in your country", "geo restricted")


def default_cookies() -> str:
    """cookie 路径的默认值：进程环境变量 ``YTDLP_COOKIES`` → ``.env`` 里的同名项（Settings）。

    规格 §二.5：cookie **不复制、不进日志、不进接口返回、不写进库** ⇒ 路径不落库，
    「重新导入」默认从这里取；想固定用一份 cookie 就写进 ``backend/.env``。
    任务 119 起与 ``YTDLP_EXE`` / ``YTDLP_CMD`` 共用同一条回落链（``_env_or_setting``）。
    """
    return _env_or_setting(YTDLP_ENV_COOKIES, get_settings().ytdlp_cookies)


def classify_tool_error(text: str) -> tuple[str, str]:
    """yt-dlp 的报错 → ``(代码, 人话)``。**一眼能看出是不是登录态问题**（硬事实 1）。

    代码取 ``login`` / ``rate_limit`` / ``js_runtime`` / ``unavailable`` / ``other``，
    前端与 ``meta.link.skipped_reason`` 都直接用这里的话，不另造第二套。
    """
    low = (text or "").lower()
    if any(p in low for p in LOGIN_PATTERNS):
        return ("login", "YouTube 认为这次请求没登录（cookie 不是 .youtube.com 域、或 cookie 已过期）："
                         "重新从浏览器导出一份 youtube.com 的 cookies.txt，再点「重新导入」")
    if any(p in low for p in RATE_PATTERNS):
        return ("rate_limit", "YouTube 限流（429）：已经按指数退避重试过，仍然被挡。"
                              "同一份 cookie 大约只能跑一个播放列表，等一会儿再点「重新导入」")
    if any(p in low for p in JS_PATTERNS):
        return ("js_runtime", "yt-dlp 需要 JS 运行时（deno 或 Node.js）才能抓 YouTube："
                              "装 deno，或装 Node.js（本系统会自动传 --js-runtimes node）")
    if any(p in low for p in GONE_PATTERNS):
        return ("unavailable", "这条 YouTube 视频抓不了（私有 / 会员专属 / 已删除 / 地区限制）")
    return ("other", "抓取失败：" + _first_error_line(text))


def _first_error_line(text: str) -> str:
    """挑一行能贴给人看的报错：**优先** ``ERROR:`` 那一行。

    真机实测：yt-dlp 单视频抓不动时 **stdout 先打一行 ``null``**，
    真正的原因在后面的 ``ERROR: ...``。直接取第一行就会变成
    「抓取失败：null」这种说不出所以然的话。
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    for line in lines:
        if line.upper().startswith("ERROR:"):
            return line.split(":", 1)[1].strip()
    for line in lines:
        if line not in ("null", "{}", "[]"):
            return line
    return "yt-dlp 没有给出原因"


def is_rate_limited(text: str) -> bool:
    """这一趟是不是被 429 挡了（退避重试用）。"""
    return classify_tool_error(text)[0] == "rate_limit"


def backoff_delay(attempt: int) -> float:
    """第 ``attempt`` 次重试前等多久：8s → 16s → 32s（指数，封顶 64s）。"""
    return min(RATE_BACKOFF_CAP, RATE_BACKOFF_BASE * (2 ** max(0, attempt - 1)))


class RateLimiter:
    """礼貌抓取：每请求间隔 ≥4 秒、同一列表内两条视频之间 ≥20 秒，**串行**不并发。

    测试用假时钟/假 sleep 注入（``FetchOptions.clock`` / ``FetchOptions.sleep``），
    这样「等了多久」是断言出来的，不是靠真等 4 秒。
    """

    def __init__(self, *, min_interval: float = RATE_MIN_INTERVAL,
                 playlist_interval: float = RATE_PLAYLIST_INTERVAL,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.min_interval = float(min_interval)
        self.playlist_interval = float(playlist_interval)
        self.clock = clock
        self.sleep = sleep
        self.waits: list[float] = []      # 每次请求前实际等待的秒数（验收贴退避日志用）
        self._last: float | None = None

    def wait(self, *, item_switch: bool = False) -> float:
        """等到「距上次请求 ≥ 间隔」为止，返回这次实际等待的秒数。"""
        need = self.playlist_interval if item_switch else self.min_interval
        if self._last is None:
            self._last = self.clock()
            self.waits.append(0.0)
            return 0.0
        gap = self.clock() - self._last
        delay = max(0.0, need - gap)
        if delay > 0:
            self.sleep(delay)
        self.waits.append(round(delay, 3))
        self._last = self.clock()
        return delay

    def run(self, args: list[str], opts: "FetchOptions", *, item_switch: bool = False) -> ToolRun:
        """限流后执行；被 429 挡了就指数退避重试（上限 ``opts.max_attempts`` 次）。"""
        self.wait(item_switch=item_switch)
        attempt = 0
        while True:
            attempt += 1
            run = (opts.runner or run_ytdlp)(args)
            if not is_rate_limited(run.out) or attempt >= max(1, opts.max_attempts):
                return run
            delay = backoff_delay(attempt)
            logger.warning("YouTube 限流（429）：第 %d/%d 次重试，等 %.0f 秒",
                           attempt, max(1, opts.max_attempts) - 1, delay)
            self.waits.append(round(delay, 3))
            opts.sleep(delay)
            self._last = self.clock()

# ---------------------------------------------------------------------------
# 链接解析（视频 / 播放列表 / 无效链接）
# ---------------------------------------------------------------------------
def parse_link(url: str) -> dict:
    """YouTube 链接 → ``{kind, video_id, playlist_id, url, watch_url}``。

    ``kind`` 只有 ``video`` / ``playlist`` 两种；解析不出来就 ``ValueError`` + 人话
    （路由翻成 400，**不落素材行** —— 输入错不算「降级」，别把脏链接写进库）。
    """
    raw = (url or "").strip()
    if not raw:
        raise ValueError("请粘贴一个 YouTube 视频或播放列表链接")
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    host = (parts.netloc or "").lower().split("@")[-1].split(":")[0]
    if host not in _YT_HOSTS:
        raise ValueError("只支持 YouTube 链接（watch / youtu.be / shorts / playlist 这几种形态）；"
                         f"现在这条的域名是「{host or '空'}」")
    query = parse_qs(parts.query or "")
    segments = [s for s in (parts.path or "").split("/") if s]
    video_id = ""
    playlist_id = ""
    if host.endswith("youtu.be"):
        video_id = (segments[0] if segments else "")
    elif segments and segments[0] == "playlist":
        playlist_id = (query.get("list") or [""])[0]
    elif segments and segments[0] in _PATH_ID_PREFIXES and len(segments) > 1:
        video_id = segments[1]
    video_id = video_id or (query.get("v") or [""])[0]
    playlist_id = playlist_id or (query.get("list") or [""])[0]
    if playlist_id and not _PLAYLIST_ID_RE.match(playlist_id):
        raise ValueError("播放列表 id 看起来不对（list=… 那一段），请复制地址栏里的完整链接")
    if video_id and not _VIDEO_ID_RE.match(video_id):
        video_id = ""
    if not video_id and not playlist_id:
        raise ValueError("这条链接里既没有视频 id 也没有播放列表 id，请复制地址栏里的完整链接")
    watch_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else ""
    url_norm = (f"https://www.youtube.com/playlist?list={playlist_id}" if playlist_id else watch_url)
    return {"kind": "playlist" if playlist_id else "video", "video_id": video_id,
            "playlist_id": playlist_id, "url": url_norm, "watch_url": watch_url}


# ---------------------------------------------------------------------------
# 抓取（顺序写死：① 字幕 → ② 音频 + 既有本地 ASR → ③ 明确失败）
# ---------------------------------------------------------------------------
@dataclass
class FetchOptions:
    """一次导入的参数（测试全用假的 runner / sleep / clock / transcribe，不联网、不真等）。"""

    cookies: str | None = None
    langs: tuple[str, ...] = SUB_LANGS
    max_items: int = LINK_MAX_ITEMS
    max_attempts: int = RATE_MAX_ATTEMPTS
    min_interval: float = RATE_MIN_INTERVAL
    playlist_interval: float = RATE_PLAYLIST_INTERVAL
    enable_asr: bool = True
    work_dir: Path | None = None
    # ``None`` = 用默认执行器；跑之前由 :func:`bind_runner` 把探测到的可执行文件补进 argv。
    runner: Callable[[list[str]], ToolRun] | None = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    transcribe: Callable[..., dict] | None = None
    waits: list[float] = field(default_factory=list)


def ytdlp_runner(cmd: list[str]) -> Callable[[list[str]], ToolRun]:
    """把「探测到的启动方式」绑进执行器：``ytdlp_base()`` 只拼参数，可执行文件在这里补。

    真机反证据：不补 argv[0] 的话，``subprocess.run`` 会拿 ``--ignore-config`` 当程序名，
    真机上直接 ``[WinError 2] 系统找不到指定的文件``（假执行器看不出来，所以单独钉一条用例）。
    """

    def _run(args: list[str]) -> ToolRun:
        return run_ytdlp([*cmd, *args])

    return _run


def bind_runner(opts: FetchOptions, probe: dict) -> FetchOptions:
    """默认执行器 ⇒ 换成「带可执行文件」的那个；假执行器 / 自定义执行器**原样保留**。"""
    if opts.runner is not None:
        return opts
    cmd = list(probe.get("cmd") or [])
    if not cmd:
        return opts
    return replace(opts, runner=ytdlp_runner(cmd))


def ytdlp_base(opts: FetchOptions) -> list[str]:
    """公共参数：不读用户配置文件、少废话；cookie **只按路径**给（``--cookies``）。"""
    args = ["--ignore-config", "--no-warnings", "--no-progress",
            "--sleep-requests", str(opts.min_interval)]
    if opts.cookies:
        args += ["--cookies", str(opts.cookies)]
    args += js_runtime_args()
    return args


def fetch_listing(parsed: dict, opts: FetchOptions, limiter: RateLimiter) -> tuple[dict, ToolRun]:
    """先「只列不抓」：拿到标题 / 视频清单 / 时长（播放列表**只有这一次**列表请求）。"""
    args = [*ytdlp_base(opts), "--flat-playlist", "--dump-single-json", parsed["url"]]
    run = limiter.run(args, opts)
    if run.code != 0:
        return {}, run
    data = _load_json(run.out)
    if not isinstance(data, dict):
        return {}, ToolRun(args, 1, out="ERROR: yt-dlp 没有返回可解析的列表信息")
    entries = data.get("entries") if isinstance(data.get("entries"), list) else [data]
    items: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        vid = str(entry.get("id") or "")
        if not _VIDEO_ID_RE.match(vid):
            continue
        items.append({"id": vid, "title": str(entry.get("title") or "").strip(),
                      "duration_sec": entry.get("duration"),
                      "url": f"https://www.youtube.com/watch?v={vid}"})
    return {"kind": str(parsed.get("kind") or "video"), "title": str(data.get("title") or "").strip(),
            "items": items}, run


def _load_json(text: str):
    """从 yt-dlp 输出里抠出 JSON（它偶尔会在前面带几行提示）。"""
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return None


def fetch_subtitle(item: dict, opts: FetchOptions, limiter: RateLimiter, work_dir: Path,
                   *, item_switch: bool = False) -> tuple[str, str, ToolRun]:
    """抓这一条的字幕：``(字幕原文, 语言, 运行结果)``；没字幕就是空串（交给上层走 ASR）。

    ``item_switch=True`` 表示这是**换了一条视频后的第一次请求** ⇒ 限流按「列表内两条之间
    ≥20 秒」算（硬事实 3）。
    """
    args = [*ytdlp_base(opts), "--skip-download", "--write-subs", "--write-auto-subs",
            "--sub-langs", ",".join(opts.langs), "--sub-format", "vtt/best",
            "--no-playlist", "-o", str(work_dir / "%(id)s.%(ext)s"), item["url"]]
    run = limiter.run(args, opts, item_switch=item_switch)
    if run.code != 0:
        return "", "", run
    for lang in opts.langs:
        for suffix in (".vtt", ".srt", ".json3"):
            cand = work_dir / f"{item['id']}.{lang}{suffix}"
            if cand.exists():
                return cand.read_text(encoding="utf-8", errors="replace"), lang, run
    return "", "", run


def download_audio(item: dict, opts: FetchOptions, limiter: RateLimiter,
                   work_dir: Path) -> tuple[Path | None, ToolRun]:
    """没字幕才走这一步：只抽音频（**既有 ASR 链路吃本地文件**，不上传云端）。"""
    args = [*ytdlp_base(opts), "-x", "--audio-format", "m4a", "--audio-quality", "5",
            "--no-playlist", "-o", str(work_dir / "%(id)s.%(ext)s"), item["url"]]
    run = limiter.run(args, opts)
    if run.code != 0:
        return None, run
    for cand in sorted(work_dir.glob(f"{item['id']}.*")):
        if cand.suffix.lower() in {".m4a", ".mp3", ".opus", ".webm", ".wav", ".aac", ".ogg", ".mp4"}:
            return cand, run
    return None, run

# ---------------------------------------------------------------------------
# 字幕解析（vtt / srt / json3）+ 去重（滚动字幕的重复行）
# ---------------------------------------------------------------------------
_VTT_TIME_RE = re.compile(r"(?P<h>\d{1,3}):(?P<m>\d{2}):(?P<s>\d{2})[.,](?P<ms>\d{1,3})")
_VTT_TAGS_RE = re.compile(r"<[^>]+>")
_ENTITIES = {"&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
             "&#39;": "'"}


def _vtt_seconds(text: str) -> float:
    """``00:01:02.500`` / ``01:02.5`` → 秒（取第一个时间戳，忽略 align: 之类设置）。"""
    hit = _VTT_TIME_RE.search(text or "")
    if hit:
        return (int(hit.group("h")) * 3600 + int(hit.group("m")) * 60 + int(hit.group("s"))
                + int(hit.group("ms").ljust(3, "0")) / 1000.0)
    hit = re.search(r"(?P<m>\d{1,3}):(?P<s>\d{2})(?P<ms>[.,]\d{1,3})?", text or "")
    if hit:
        return int(hit.group("m")) * 60 + int(hit.group("s"))
    return 0.0


def _clean_cue(text: str) -> str:
    """一条字幕 → 纯文本（去 ``<c>`` 之类的行内标签、去实体、压空白）。"""
    out = _VTT_TAGS_RE.sub("", text or "")
    for src, dst in _ENTITIES.items():
        out = out.replace(src, dst)
    return " ".join(out.split())


def _dedupe_cues(rows: list[dict]) -> list[dict]:
    """滚动字幕去重（启发式）：整条重复就并到上一条；上一条是本条前缀就只留新增部分。"""
    out: list[dict] = []
    for row in rows:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        if out:
            prev = out[-1]
            if text == prev["text"]:
                prev["end"] = row["end"]
                continue
            if text.startswith(prev["text"]) and len(text) > len(prev["text"]):
                tail = text[len(prev["text"]):].strip()
                if tail:
                    prev["text"] = tail
                prev["end"] = row["end"]
                continue
        out.append({"start": row["start"], "end": row["end"], "text": text})
    return out


def _parse_json3(text: str) -> list[dict]:
    """YouTube 自动字幕的 json3（``events[].segs[].utf8``）→ 段落。"""
    data = _load_json(text)
    events = (data or {}).get("events") if isinstance(data, dict) else None
    rows: list[dict] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        body = _clean_cue("".join(str(s.get("utf8") or "") for s in (event.get("segs") or [])
                                  if isinstance(s, dict)))
        if not body:
            continue
        start = float(event.get("tStartMs") or 0) / 1000.0
        end = start + float(event.get("dDurationMs") or 0) / 1000.0
        rows.append({"start": start, "end": end, "text": body})
    return _dedupe_cues(rows)


def parse_subtitle_text(text: str, *, name: str = "") -> list[dict]:
    """字幕文件 → ``[{start, end, text}]``（vtt / srt 走同一套，json3 单独解）。"""
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if name.lower().endswith(".json3") or raw.lstrip().startswith("{"):
        return _parse_json3(raw)
    rows: list[dict] = []
    for block in re.split(r"\n\s*\n", raw):
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        if lines[0].strip().upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            lines = lines[1:]
        index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if index is None:
            continue
        left, _, right = lines[index].partition("-->")
        body = " ".join(_clean_cue(line) for line in lines[index + 1:]).strip()
        if not body:
            continue
        rows.append({"start": _vtt_seconds(left), "end": _vtt_seconds(right), "text": body})
    return _dedupe_cues(rows)


def subtitle_to_transcript(text: str, *, name: str = "") -> tuple[str, list[dict]]:
    """字幕 → 可读转写稿 ``(正文, 段落)``。

    段落口径**与任务 102 的音视频转写完全一致**（``merge_asr_segments`` 合并到 ≤120 字、
    每段行首 ``[mm:ss]``、段间空行）—— 讲义侧的时间戳锚点（任务 105）才认得出这条素材。
    """
    rows = parse_subtitle_text(text, name=name)
    segments = material_service.merge_asr_segments(rows)
    return material_service.asr_segments_to_text(segments), segments

# ---------------------------------------------------------------------------
# 主流程（顺序写死：① 字幕 → ② 音频 + 既有本地 ASR → ③ 明确失败并保留素材行）
# ---------------------------------------------------------------------------
def _iso_now() -> str:
    """抓取时间（带时区，照任务 83 的口径：后端出口的时间必须带时区）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def import_one(item: dict, *, opts: FetchOptions, limiter: RateLimiter, work: Path,
               db: Session, row: SourceMaterial, switch: bool = False) -> dict:
    """抓**一条**视频：先字幕、没字幕才下音频走本地 ASR、都不行就明确写原因（不抛异常）。

    返回值里 ``path`` 就是「用了哪条路」：``subtitle`` / ``asr`` / ``none``（+ ``reason``）。
    """
    result: dict = {"id": item["id"], "title": item.get("title") or "", "path": "none",
                    "language": "", "segments": 0, "text": "", "reason": "", "asr": None,
                    "duration_sec": item.get("duration_sec")}
    raw, lang, run = fetch_subtitle(item, opts, limiter, work, item_switch=switch)
    if raw.strip():
        body, segments = subtitle_to_transcript(raw, name=f"{item['id']}.{lang}.vtt")
        if body.strip():
            result.update({"path": "subtitle", "language": lang, "segments": len(segments),
                           "text": body})
            return result
        result["reason"] = "字幕文件解析不出文字（可能是空字幕）"
    elif run.code != 0:
        code, message = classify_tool_error(run.out or run.error)
        if code in ("unavailable", "login", "rate_limit"):
            result.update({"reason": message})          # 这三类再下音频也白搭（下载同样要过这道门）
            return result
        result["reason"] = message
    if not opts.enable_asr:
        result["reason"] = (result["reason"] + "；" if result["reason"] else "") + "已按参数跳过音频转写"
        return result
    if not material_service.asr_available():
        result["reason"] = ((result["reason"] + "；" if result["reason"] else "")
                            + material_service.ASR_SKIP_REASON)
        return result
    audio, run2 = download_audio(item, opts, limiter, work)
    if audio is None:
        code, message = classify_tool_error(run2.out or run2.error)
        result["reason"] = (result["reason"] + "；" if result["reason"] else "") + message
        return result
    transcribe = opts.transcribe or material_service.transcribe_av
    hint = material_service.asr_domain_hint(db, row)
    try:
        out = transcribe(audio, on_progress=None, domain_hint=hint)
    except Exception as exc:  # noqa: BLE001 —— 本地转写失败：素材行与原因都留着，可重试
        result["reason"] = f"本地转写失败（{exc}），可点「重新导入」重试"
        return result
    body = str(out.get("text") or "")
    segments = out.get("segments") or []
    asr = material_service.asr_meta("done")
    asr.update({"model": out.get("model") or material_service.asr_model(),
                "duration_sec": out.get("duration_sec"),
                "segments": len(segments), "language": out.get("language") or "",
                "avg_logprob": out.get("avg_logprob"),
                "initial_prompt": out.get("initial_prompt") or ""})
    result.update({"path": "asr", "language": str(out.get("language") or ""),
                   "segments": len(segments), "text": body, "asr": asr,
                   "duration_sec": out.get("duration_sec") or result["duration_sec"]})
    if not body.strip():
        result["reason"] = "音频里没有识别到语音（可能是纯音乐 / 无声）"
    return result


def run_link_import(db: Session, row: SourceMaterial, *, parsed: dict,
                    opts: FetchOptions | None = None, on_progress=None) -> dict:
    """对一条**已落库**的链接素材执行抓取（首次导入与「重新导入」都走这里）。

    顺序写死（规格 §二.2，反证据用例盯着它）：① 抓字幕 → ② 没字幕下音频走既有 ASR →
    ③ 都不行就明确失败原因，**素材行保留、可重试**。三种情况都写进 ``meta.link``。
    """
    opts = opts or FetchOptions()
    probe = ytdlp_probe()
    if not probe["available"]:
        return _finish_failed(db, row, parsed, opts=opts, reason=probe["reason"], code="no_tool")
    opts = bind_runner(opts, probe)
    cookies = inspect_cookies(opts.cookies) if opts.cookies else {"youtube_domain": False}
    if opts.cookies and not cookies.get("youtube_domain"):
        logger.warning("链接导入：这份 cookies.txt 里没有 .youtube.com 域，YouTube 会当成未登录")
    limiter = RateLimiter(min_interval=opts.min_interval,
                          playlist_interval=opts.playlist_interval,
                          clock=opts.clock, sleep=opts.sleep)
    temp = tempfile.TemporaryDirectory(prefix="cg_link_") if opts.work_dir is None else None
    work = Path(opts.work_dir) if opts.work_dir is not None else Path(temp.name)
    work.mkdir(parents=True, exist_ok=True)
    try:
        listing, run = fetch_listing(parsed, opts, limiter)
        if not listing or not listing.get("items"):
            code, message = classify_tool_error(run.out or run.error)
            return _finish_failed(db, row, parsed, opts=opts, reason=message, code=code,
                                  limiter=limiter, cookies=cookies)
        all_items = listing["items"]
        items = all_items[: max(1, opts.max_items)]
        total = len(items)
        sections: list[str] = []
        item_meta: list[dict] = []
        failed: list[dict] = []
        asr_rows: list[dict] = []
        paths: set[str] = set()
        done = 0
        for index, item in enumerate(items):
            result = import_one(item, opts=opts, limiter=limiter, work=work, db=db, row=row,
                                switch=index > 0)
            item_meta.append({key: result[key] for key in
                              ("id", "title", "duration_sec", "path", "language", "segments")})
            if result["text"].strip():
                done += 1
                paths.add(str(result["path"]))
                header = f"## {index + 1:02d} {result['title'] or result['id']}" if total > 1 else ""
                sections.append("\n\n".join(part for part in (header, result["text"]) if part))
                if result["asr"]:
                    asr_rows.append(result["asr"])
            else:
                failed.append({"title": result["title"] or result["id"],
                               "reason": result["reason"] or "没抓到字幕，也没能转写"})
            if on_progress is not None:
                on_progress(done, total)
        text = "\n\n".join(sections)
        skipped = ""
        if failed:
            skipped = (f"{len(failed)}/{total} 条没抓成：" +
                       "；".join(f"{f['title']}（{f['reason']}）" for f in failed[:3]))
        link = {
            "source": LINK_SOURCE, "url": parsed["url"], "kind": listing.get("kind") or parsed["kind"],
            "video_id": parsed["video_id"], "playlist_id": parsed["playlist_id"],
            "status": "done" if text.strip() else "failed",
            "path": ("mixed" if len(paths) > 1 else (next(iter(paths)) if paths else "none")),
            "language": "、".join(dict.fromkeys(m["language"] for m in item_meta if m["language"])),
            "duration_sec": round(sum(float(m.get("duration_sec") or 0) for m in item_meta), 3) or None,
            "videos": total, "videos_all": len(all_items),
            "items_skipped": max(0, len(all_items) - total),
            "items": item_meta, "failed": failed,
            "fetched_at": _iso_now(), "cookies": bool(opts.cookies),
            "cookies_youtube_domain": bool(cookies.get("youtube_domain")),
            "rate": {"requests": len(limiter.waits), "waits": limiter.waits[:20]},
            "skipped_reason": skipped,
            "notes": _notes(opts, len(all_items), total),
        }
        material_service.set_link_import(db, row, link=link, text=text,
                                        title=listing.get("title") or "",
                                        asr=_merge_asr(asr_rows))
        logger.info("链接导入完成：material=%s kind=%s 视频=%d 成功=%d 路径=%s 字数=%d",
                    row.id, link["kind"], total, done, link["path"], len(text))
        return {"material_id": row.id, "status": link["status"], "path": link["path"],
                "items": total, "items_done": done,
                "segments": sum(int(m["segments"] or 0) for m in item_meta),
                "chars": len(text), "language": link["language"], "skipped_reason": skipped}
    finally:
        if temp is not None:
            temp.cleanup()


def _notes(opts: FetchOptions, listed: int, taken: int) -> list[str]:
    """meta.link.notes：把「这次和规格有出入的地方」写清楚，别人复验不用猜。"""
    notes: list[str] = []
    if not opts.cookies:
        notes.append("这次没给 cookies.txt：YouTube 常要求登录态，抓不到时请导出一份 youtube.com 的 cookie")
    if listed > taken:
        notes.append(f"列表共 {listed} 条，本次只导入前 {taken} 条（正文有 20 万字上限，其余请分批导入）")
    return notes


def _merge_asr(rows: list[dict]) -> dict | None:
    """多条视频都走了 ASR ⇒ 合成一份 ``meta.asr``（字段与任务 102 的骨架**完全一致**）。"""
    if not rows:
        return None
    merged = dict(rows[0])
    total = sum(float(r.get("duration_sec") or 0) for r in rows)
    langs = list(dict.fromkeys(str(r.get("language") or "") for r in rows if r.get("language")))
    merged.update({"duration_sec": round(total, 3) or None,
                   "segments": sum(int(r.get("segments") or 0) for r in rows),
                   "language": "、".join(langs)})
    return merged


def _finish_failed(db: Session, row: SourceMaterial, parsed: dict, *, opts: FetchOptions,
                   reason: str, code: str, limiter: RateLimiter | None = None,
                   cookies: dict | None = None) -> dict:
    """整体失败（缺工具 / 列表就抓不动）：写 ``meta.link`` 的失败态，**素材行保留**。"""
    info = cookies if cookies is not None else (inspect_cookies(opts.cookies) if opts.cookies else {})
    link = {"source": LINK_SOURCE, "url": parsed["url"], "kind": parsed["kind"],
            "video_id": parsed["video_id"], "playlist_id": parsed["playlist_id"],
            "status": "failed", "path": "none", "language": "", "duration_sec": None,
            "videos": 0, "videos_all": 0, "items_skipped": 0, "items": [],
            "failed": [{"title": "", "reason": reason}], "fetched_at": _iso_now(),
            "cookies": bool(opts.cookies), "cookies_youtube_domain": bool(info.get("youtube_domain")),
            "rate": {"requests": len(limiter.waits) if limiter else 0,
                     "waits": limiter.waits[:20] if limiter else []},
            "skipped_reason": reason, "error_code": code,
            "notes": ["素材行已保留：装好依赖 / 换一份 cookie 后点「重新导入」即可重试"]}
    material_service.set_link_import(db, row, link=link)
    logger.warning("链接导入失败：material=%s code=%s 原因=%s", row.id, code, reason)
    return {"material_id": row.id, "status": "failed", "path": "none", "items": 0,
            "items_done": 0, "segments": 0, "chars": 0, "language": "",
            "skipped_reason": reason, "error_code": code}