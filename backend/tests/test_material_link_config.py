"""任务 119：``YTDLP_EXE`` / ``YTDLP_CMD``（连 ``YTDLP_COOKIES``）走 ``.env`` —— 与任务 104 的 ``ASR_MODEL`` 同一个病。

病灶：``link_import`` 原来**只读 ``os.environ``**，而本项目的 ``.env`` 是给 pydantic ``Settings``
读的（pydantic-settings **只把 `.env` 读进 Settings、不写回 `os.environ`**）⇒ 往 ``.env`` 里写
``YTDLP_EXE`` **完全不生效**（真机实测：能力块一直显示 ytdlp 不可用）。
现在回落链写死：**进程环境变量（优先，既有口径不变）⇒ `.env`（经 ``Settings``）⇒ 空 / PATH**。

判据（全部在**新进程**里跑）：``Settings`` 有 ``lru_cache``，且 ``env_file=".env"`` 是**相对 cwd**
解析的 ⇒ 同进程里换 ``.env`` 不生效，必须另起子进程、以临时目录为 cwd（与 ``test_material_asr.py``
里的任务 104 用例同款）。假 yt-dlp 是**真能跑**的 ``.bat``（打印一行版本号）—— 探测只认
``YYYY.MM.DD`` 形状的版本行，所以这条链是真跑出来的，不是打桩。

协作区铁律：五件套由 ``conftest`` 指向临时目录；本文件**不碰真实库、不改真机 `.env`**（只读）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

WIN_ONLY = pytest.mark.skipif(os.name != "nt", reason="假 yt-dlp 用 .bat 启动器，只在 Windows 上可执行")

_PROBE = """
import json, shutil

from app.services import link_import as L
from app.services.capabilities import ytdlp_capability

cap = ytdlp_capability()
print("CGPROBE " + json.dumps({
    "available": bool(cap["available"]),
    "version": cap.get("version") or "",
    "candidates": L.ytdlp_candidates()[:3],
    "which": shutil.which("yt-dlp") or "",
    "cookies": L.default_cookies(),
}, ensure_ascii=True))
"""


def _probe_env() -> dict[str, str]:
    """子进程环境：把本轮这三项从继承环境里**摘掉**（否则门禁/真机的环境变量会污染判定），
    并把 ``backend/`` 加进 ``PYTHONPATH``（子进程的 cwd 是临时目录，不是仓库）。"""
    env = {k: v for k, v in os.environ.items()
           if k not in ("YTDLP_EXE", "YTDLP_CMD", "YTDLP_COOKIES")}
    backend = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(backend), env.get("PYTHONPATH")]))
    env.pop("PYTHONIOENCODING", None)
    return env


def _probe(workdir: Path, env: dict[str, str]) -> dict:
    """在 ``workdir`` 里新起一个进程读配置（``.env`` 相对 cwd 解析 ⇒ 必须换 cwd 才测得准）。"""
    proc = subprocess.run([sys.executable, "-c", _PROBE], cwd=workdir, env=env,
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, f"子进程跑不通：{proc.stderr[-500:]}"
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("CGPROBE ")]
    assert lines, f"探针没输出：{proc.stdout[-300:]}"
    return json.loads(lines[-1][len("CGPROBE "):])


def _fake_ytdlp(where: Path, version: str, name: str = "fake-yt-dlp.bat") -> Path:
    """造一个**真能跑**的假 yt-dlp：只打印一行版本号（纯 ASCII，避免控制台编码干扰）。"""
    path = where / name
    path.write_text("@echo off\r\necho " + version + "\r\n", encoding="utf-8")
    return path


@WIN_ONLY
def test_dotenv_ytdlp_exe_is_read_and_capability_turns_available(tmp_path):
    """验收①：临时 `.env` 写 `YTDLP_EXE` ⇒ 能力块 `available=True`，且候选命令第一个就是它。"""
    exe = _fake_ytdlp(tmp_path, "2026.08.19")
    (tmp_path / ".env").write_text(f"YTDLP_EXE={exe}\n", encoding="utf-8")

    got = _probe(tmp_path, _probe_env())

    assert got["available"] is True, "`.env` 里的 YTDLP_EXE 没被读到（又变成只认进程环境变量了）"
    assert got["version"] == "2026.08.19", got
    assert got["candidates"][0] == [str(exe)], got["candidates"]


@WIN_ONLY
def test_process_env_still_wins_over_dotenv(tmp_path):
    """验收②：`.env` 指 A、进程环境变量指 B ⇒ 取 **B**（任务 102 / 104 的「环境变量优先」口径不变）。"""
    a = _fake_ytdlp(tmp_path, "2026.08.19", "a.bat")
    b = _fake_ytdlp(tmp_path, "2027.01.01", "b.bat")
    (tmp_path / ".env").write_text(f"YTDLP_EXE={a}\n", encoding="utf-8")

    got = _probe(tmp_path, dict(_probe_env(), YTDLP_EXE=str(b)))

    assert got["candidates"][0] == [str(b)], got["candidates"]
    assert got["version"] == "2027.01.01", got


@WIN_ONLY
def test_ytdlp_cmd_still_takes_precedence_over_exe(tmp_path):
    """验收④：`YTDLP_CMD` 的优先级**不变**（整条命令排在 `YTDLP_EXE` 前面）。"""
    exe = _fake_ytdlp(tmp_path, "2026.08.19", "exe.bat")
    cmd = _fake_ytdlp(tmp_path, "2030.12.31", "cmd.bat")
    (tmp_path / ".env").write_text(f"YTDLP_EXE={exe}\nYTDLP_CMD={cmd}\n", encoding="utf-8")

    got = _probe(tmp_path, _probe_env())

    assert got["candidates"][:2] == [[str(cmd)], [str(exe)]], got["candidates"]
    assert got["version"] == "2030.12.31", got


def test_without_any_config_falls_back_to_path(tmp_path):
    """验收③：`.env` 与进程环境变量都没有 ⇒ 回落 **PATH 上的 yt-dlp**（临时目录里的假货一个都不算）。"""
    got = _probe(tmp_path, _probe_env())

    if got["which"]:
        assert got["candidates"][0] == [got["which"]], got["candidates"]
    assert all(str(tmp_path) not in cand[0] for cand in got["candidates"]), got["candidates"]
    assert got["cookies"] == "", "没配 YTDLP_COOKIES 时不该凭空冒出一个 cookie 路径"


def test_cookie_path_uses_the_same_chain(tmp_path):
    """顺手统一同类（任务书 §二.3）：`YTDLP_COOKIES` 走**同一条**回落链 —— `.env` 能读到、环境变量优先。"""
    jar = tmp_path / "cookies.txt"
    jar.write_text("# Netscape HTTP Cookie File\n"
                   ".youtube.com\tTRUE\t/\tTRUE\t2000000000\t__Secure-3PSID\tx\n", encoding="utf-8")
    other = tmp_path / "other.txt"
    other.write_text("", encoding="utf-8")
    (tmp_path / ".env").write_text(f"YTDLP_COOKIES={jar}\n", encoding="utf-8")

    assert _probe(tmp_path, _probe_env())["cookies"] == str(jar)
    assert _probe(tmp_path, dict(_probe_env(), YTDLP_COOKIES=str(other)))["cookies"] == str(other)
