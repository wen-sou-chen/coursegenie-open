"""任务 132：交付包自带「双击核对文件完整性」（0 LLM，真跑 .bat）。

来历：交付包里只有 `manifest.json`（机器读的），客户要核对自己得敲
`certutil -hashfile ... SHA1` 一条条比 —— 用户 2026-10-05 原话：「客人不可能去敲命令行」。
⇒ 包根目录加一个**双击就能跑**的 `核对文件完整性.bat`：只读、不联网、不装依赖、不要管理员，
几秒给结论「全部一致 / 有 N 个对不上」，结尾 `pause` 停住等按键。

本文件钉住（任务 132 §A / §B）：
① 包里有这个 .bat，且它**自己**也在 `manifest.json` 的 files[] 里（只有 manifest 自身不在），
   两份说明也都写到了它；
② 真跑正常包 ⇒ 输出「全部一致」+ 退出码 0；
③ 真跑**改坏一个文件**（尾部加 1 字节）⇒ 点名报出来 + 退出码非 0
   —— **把 .bat 变异成「永远说一致」这条必须红**（本次的核心交付物之一）；
④ 真跑**同长度改内容**（原位翻一个字节）⇒ 走 sha1 比对那条路，也报出来；
⑤ 真跑**删掉一个文件** ⇒ 报「文件缺失」+ 退出码非 0；
⑥ 真跑放在**没有** manifest.json 的目录 ⇒ 人话提示、不抛异常栈、退出码非 0；
⑦ 结构护栏：cmd 解析的那半边（`#===PS===` 之前）**纯 ASCII**（默认 GBK 控制台不崩的根因）、
   CRLF 换行、无 BOM。
⑧ 任务 134：包里多出 Obsidian 自己生成的 `.obsidian/` ⇒ **仍判一致、退出码仍 0**，
   只是如实报一行多出来的文件（多出来的文件**不算错误**；包内总数现数、清单口径另说）。
   任务 141：那一行**只如实报、不猜原因** —— 原来写死「通常是 Obsidian 自己生成的
   `.obsidian/` 配置」，而 141 抓到的反例是包里的 `.pdf.textfix` 临时文件（归因说反了）。

协作规则第 7 条：DATABASE_URL / STORAGE_DIR / OBSIDIAN_VAULT_ROOT / PPT_WORK_DIR /
EXPORT_DIR 五件套由 conftest 统一指向临时目录，本文件不碰真实库与真实知识库。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from app.services import integrity_check
from tests.test_delivery import _build, _course_with_content, _delivery_dir, _new_course

BAT_NAME = integrity_check.BAT_NAME
#: `.bat` 只在 Windows 上跑（Windows 10/11 自带 PowerShell，不需要装任何东西）。
pytestmark = pytest.mark.skipif(os.name != "nt", reason=".bat 是 Windows 双击脚本")


def _package(client, title: str, monkeypatch) -> Path:
    """真出一包（生产入口，pdf 按测试既有做法关掉），返回解压目录。"""
    course_id = _new_course(client, title)
    _course_with_content(client, course_id, monkeypatch)
    return _delivery_dir(_build(client, course_id)["build_id"])


def _manifest(out_dir: Path) -> dict:
    return json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))


def _run_bat(folder: Path) -> subprocess.CompletedProcess:
    """像客户那样真的跑一次：`pause` 用 DEVNULL 喂，不会挂住。"""
    comspec = os.environ.get("COMSPEC") or "cmd.exe"
    return subprocess.run([comspec, "/c", str(folder / BAT_NAME)], cwd=str(folder),
                          stdin=subprocess.DEVNULL, capture_output=True, timeout=600)


def _text(proc: subprocess.CompletedProcess) -> str:
    return (proc.stdout + proc.stderr).decode("utf-8", "replace")


def _rel_to_path(out_dir: Path, rel: str) -> Path:
    return out_dir / rel.replace("/", os.sep)


# ---------------------------------------------------------------------------
# ① 包里有这个 .bat，且它自己在清单里
# ---------------------------------------------------------------------------

def test_package_ships_the_bat_and_lists_it(client, monkeypatch):
    out = _package(client, "核对脚本清单", monkeypatch)
    bat = out / BAT_NAME
    assert bat.is_file(), f"包根目录应有 {BAT_NAME}"

    manifest = _manifest(out)
    rows = {row["path"]: row for row in manifest["files"]}
    assert BAT_NAME in rows, "这个 .bat 不特殊：它自己也要进 files[]"
    assert rows[BAT_NAME]["bytes"] == bat.stat().st_size
    assert rows[BAT_NAME]["sha1"] == hashlib.sha1(bat.read_bytes()).hexdigest()
    assert manifest["counts"]["files"] == len(manifest["files"]) + 1, \
        "包内文件数 = 清单条目 + manifest.json 自己（本次比 131 的包多 1 个）"

    for name in ("00-交付说明.md", "01-验收单.md"):
        text = (out / name).read_text(encoding="utf-8")
        assert BAT_NAME in text, f"{name} 要写一句「双击它就核对」的人话"
    assert "Mac" in (out / "00-交付说明.md").read_text(encoding="utf-8"), \
        "没附 .sh ⇒ Mac 用户的替代办法必须写在交付说明里"


# ---------------------------------------------------------------------------
# ② 正常包：全一致 + 退出码 0
# ---------------------------------------------------------------------------

def test_bat_reports_all_consistent(client, monkeypatch):
    out = _package(client, "核对脚本正常", monkeypatch)
    proc = _run_bat(out)
    text = _text(proc)
    total = len(_manifest(out)["files"])
    assert proc.returncode == 0, text
    assert "全部一致" in text, text
    # 两个数都要报：包内文件 = 清单条目 + manifest.json 自身（只有它不在清单里）
    assert f"包内 {total + 1} 个文件" in text, text
    assert f"清单列出的 {total} 个" in text, text
    assert "❌" not in text, text


# ---------------------------------------------------------------------------
# ②b 多出 Obsidian 自己生成的 `.obsidian/` ⇒ 仍判一致、仍 0，但如实报出来（任务 134）
# ---------------------------------------------------------------------------

def test_bat_reports_extra_files_without_failing(client, monkeypatch):
    """任务 134：客户拿这包当 Obsidian 仓库打开过 ⇒ Obsidian 自己生成 `.obsidian/` 配置
    ⇒ 磁盘上比清单多文件 ⇒ **仍判「全部一致」、退出码仍是 0**，只是**如实报一行**多出来的文件。

    复测 133 时的实测：加了 `.obsidian/`（2 个文件）后磁盘 202 个、而那句话仍说「包内 200 个
    文件」—— 判定与退出码都是对的，**只是说法不准**。硬要求：多出来的文件**不许当成错误**
    （Obsidian 生成 `.obsidian/` 是正常的）。

    任务 141：那行**不再猜原因**（原文写死「通常是 Obsidian 自己生成的 `.obsidian/` 配置」，
    而真实反例是包里的 `.pdf.textfix` 临时文件）⇒ 断言改成钉「中性说法 + 不许出现旧归因」，
    退出码语义（多出来不算错、仍 0）一个字没动。
    """
    out = _package(client, "核对脚本多出文件", monkeypatch)
    total = len(_manifest(out)["files"])
    obsidian = out / ".obsidian"
    obsidian.mkdir()
    (obsidian / "app.json").write_text("{}\n", encoding="utf-8")
    (obsidian / "workspace.json").write_text("{}\n", encoding="utf-8")

    proc = _run_bat(out)
    text = _text(proc)
    assert proc.returncode == 0, f"多出 .obsidian/ 是正常现象，不许判成错误：{text}"
    assert "全部一致" in text, text
    # 包内总数**现数**（清单 199 条 + manifest.json 自身 + 2 个 Obsidian 配置），清单口径另说
    assert f"包内 {total + 3} 个文件" in text, text
    assert f"清单列出的 {total} 个" in text, text
    assert "另外 2 个文件不在清单里" in text, text
    assert "是包里多出来的文件，不在核对范围内" in text, text
    assert "Obsidian 自己生成" not in text, f"归因不许说死（任务 141）：{text}"
    assert "❌" not in text, text


# ---------------------------------------------------------------------------
# ③ 改坏一个文件（尾部加 1 字节）⇒ 必须点名报出来（变异成「永远说一致」会红）
# ---------------------------------------------------------------------------

def test_bat_reports_a_changed_file(client, monkeypatch):
    out = _package(client, "核对脚本改坏", monkeypatch)
    target = next(row["path"] for row in _manifest(out)["files"]
                  if row["path"].startswith("讲义/") and row["path"].endswith(".md"))
    with _rel_to_path(out, target).open("ab") as fh:
        fh.write(b"X")                      # 尾部加 1 个字节

    proc = _run_bat(out)
    text = _text(proc)
    assert proc.returncode != 0, f"改坏了文件却报成功：{text}"
    assert "对不上" in text and target in text, text


def test_bat_reports_a_file_with_same_size_but_different_bytes(client, monkeypatch):
    """同长度改内容（原位翻 1 个字节）⇒ 走 **sha1** 那条路，也要报出来。"""
    out = _package(client, "核对脚本改内容", monkeypatch)
    target = next(row["path"] for row in _manifest(out)["files"]
                  if row["path"].startswith("讲义/") and row["path"].endswith(".md"))
    path = _rel_to_path(out, target)
    data = bytearray(path.read_bytes())
    idx = next(i for i, b in enumerate(data) if b == 0x23)   # 第一个 '#'
    data[idx] = 0x25                                         # '#' -> '%'，长度不变
    path.write_bytes(bytes(data))

    proc = _run_bat(out)
    text = _text(proc)
    assert proc.returncode != 0, f"内容变了却报成功：{text}"
    assert target in text and "sha1" in text, text


# ---------------------------------------------------------------------------
# ④ 删掉一个文件 ⇒ 报「文件缺失」+ 退出码非 0
# ---------------------------------------------------------------------------

def test_bat_reports_a_missing_file(client, monkeypatch):
    out = _package(client, "核对脚本缺文件", monkeypatch)
    target = next(row["path"] for row in _manifest(out)["files"]
                  if row["path"].startswith("讲义/") and row["path"].endswith(".md"))
    _rel_to_path(out, target).unlink()

    proc = _run_bat(out)
    text = _text(proc)
    assert proc.returncode != 0, f"缺文件却报成功：{text}"
    assert "文件缺失" in text and target in text, text


# ---------------------------------------------------------------------------
# ⑤ 目录里没有 manifest.json ⇒ 人话提示、不崩
# ---------------------------------------------------------------------------

def test_bat_without_manifest_gives_a_human_hint(tmp_path):
    folder = tmp_path / "只有核对脚本"
    folder.mkdir()
    (folder / BAT_NAME).write_bytes(integrity_check.bat_text().encode("utf-8"))

    proc = _run_bat(folder)
    text = _text(proc)
    assert proc.returncode != 0, text
    assert "manifest.json" in text and "同一个文件夹" in text, text
    assert "Traceback" not in text and "At line:" not in text, f"不许抛异常栈：{text}"


# ---------------------------------------------------------------------------
# ⑥ 结构护栏：cmd 那半边纯 ASCII（GBK 控制台不崩的根因）+ CRLF + 无 BOM
# ---------------------------------------------------------------------------

def test_bat_source_is_cmd_safe(tmp_path):
    raw = integrity_check.bat_text().encode("utf-8")
    assert not raw.startswith(b"\xef\xbb\xbf"), "不要 BOM（cmd 解析 BOM 会出怪提示）"
    text = raw.decode("utf-8")
    lines = text.split("\r\n")
    assert text.count("\n") == text.count("\r\n"), "整份文件统一 CRLF"
    assert integrity_check.PS_MARKER in lines, f"要有独立的 {integrity_check.PS_MARKER} 行"
    head = "\r\n".join(lines[:lines.index(integrity_check.PS_MARKER)])
    assert head.isascii(), "cmd 解析的那半边必须纯 ASCII（中文只许出现在 #===PS=== 之后）"
    assert "pause" in head.lower(), "结尾必须停住等按键（否则双击后窗口一闪就关）"
    assert "chcp 65001" in head, "先切 UTF-8 再输出中文"
    # cmd 那半边要**探测**这个标记（IndexOf 的参数字面串），所以它会合法地出现 1 次；
    # 真正的「独立分界行」全文件只许有 1 行。
    assert lines.count(integrity_check.PS_MARKER) == 1, "独立的分界行只许有一行"
    assert head.count(integrity_check.PS_MARKER) == 1, "cmd 那半边只以「探测串」形式出现一次"
