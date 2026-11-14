"""任务 141：交付包不许漏进 `*.pdf.textfix` 临时文件，且「文本层修复失败」不许静默。

来历（DSH 复测 140 实测）：AI 那个包磁盘上 **797** 个文件、比 manifest 的 **792** 多 5 个 ——
多出来的是 5 个 `*.pdf.textfix`（文本层修复的临时文件），**同时进包目录与 zip、不在 manifest 里**；
机制是 `delivery.py` 的 `tmp_path.replace(...)` 抛异常时**不清理临时文件**，而那一趟的文本层修复
也就**悄悄失败**了（不报错、不进 `degraded`）。

本文件钉的都是**从产物读出来**的事实（不是断言函数返回值）：

① 正常路径：包目录 / `manifest.json` / zip 三处都没有临时文件；磁盘文件集 == 清单 + `manifest.json`
   自己（`图表/` 之外没有多出来的东西）；
② 回写失败（打桩让 `tmp_path.replace` 抛异常）：临时文件**仍不残留**，`degraded`（`manifest.json`
   与 `00-交付说明.md` 两处）**如实点名**这份 PDF，且**原 PDF 仍在**、sha1 与清单一致；
③ 打包前兜底扫描：临时文件漏进目标目录（本文件在写 `.bat` 那一刻塞一个）也会被清掉并记进
   `degraded`（既进清单也进 zip 的通道被堵死）；
④ 兜底扫描只认临时后缀：正常交付件一个都不删。

⚠️ 真出 PDF 需要本机 Microsoft Edge（没有就跳过 ①②③，与 `test_delivery_font_ofl.py` 同一口径）。
"""
from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from app.services import delivery, integrity_check
from tests.test_delivery import _build, _course_with_content, _delivery_dir, _new_course

#: 真出 PDF 的那几条要本机有 Edge（与既有真打印用例同一口径）。
_needs_edge = pytest.mark.skipif(delivery.find_edge() is None,
                                 reason="本机没有 Microsoft Edge，跳过「真出 PDF」的断言")


def _build_with_pdfs(client, title: str, monkeypatch) -> tuple[Path, Path]:
    """真出一包（PDF 开着 ⇒ 走 Edge 打印 + 文本层修复那条路），返回（出包目录, zip 路径）。"""
    course_id = _new_course(client, title)
    _course_with_content(client, course_id, monkeypatch)
    result = _build(client, course_id, {"pdf": "auto"})
    return _delivery_dir(result["build_id"]), Path(result["zip"])


def _manifest(out_dir: Path) -> dict:
    return json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))


def _disk_files(out_dir: Path) -> set[str]:
    return {p.relative_to(out_dir).as_posix() for p in out_dir.rglob("*") if p.is_file()}


def _zip_names(zip_path: Path) -> set[str]:
    with zipfile.ZipFile(zip_path) as zf:
        return set(zf.namelist())


def _force_one_change(monkeypatch) -> dict:
    """让每一趟「文本层修复」都报有改动 —— 这样回写那次 `tmp_path.replace(...)` 一定被走到。

    只替换内部返回值，不改修复算法本身：本文件考的是**失败与清理的处理**，
    算法本身的守门人在 `test_delivery_font_ofl.py`。返回计数器供断言「这条路真的跑到了」。
    """
    seen = {"rounds": 0}

    def fake_repair(writer, source_cps):
        seen["rounds"] += 1
        return (set(), 1, 1, [])

    monkeypatch.setattr(delivery, "_repair_writer_tounicode", fake_repair)
    return seen


# ---------------------------------------------------------------------------
# ① 正常路径：包目录 / 清单 / zip 三处都没有临时文件
# ---------------------------------------------------------------------------

@_needs_edge
def test_package_ships_no_temporary_files(client, monkeypatch):
    """真出 PDF（且强制走到回写那一步）⇒ 一个临时文件都不许留在包里。"""
    seen = _force_one_change(monkeypatch)
    out, zip_path = _build_with_pdfs(client, "包内无临时文件", monkeypatch)
    manifest = _manifest(out)

    assert seen["rounds"] >= 1, "文本层修复那条路根本没跑到 ⇒ 这条等于没考到"
    assert [row["path"] for row in manifest["files"] if row["path"].endswith(".pdf")], \
        "一个 PDF 都没出来 ⇒ 这条等于没考到"
    assert [p.relative_to(out).as_posix() for p in out.rglob("*.textfix")] == []
    assert [row["path"] for row in manifest["files"] if row["path"].endswith(".textfix")] == []
    assert [name for name in _zip_names(zip_path) if name.endswith(".textfix")] == []
    # 磁盘口径 == 清单口径 + manifest.json 自己（两者只差这一个）
    assert _disk_files(out) == {row["path"] for row in manifest["files"]} | {"manifest.json"}
    assert manifest["counts"]["files"] == len(manifest["files"]) + 1
    # 正常路径既不该有「清理临时文件」那条降级，也不该有「文本层修复未完成」那条
    assert [row for row in manifest["degraded"] if "临时文件" in row["reason"]] == []
    assert [row for row in manifest["degraded"] if "文本层修复未完成" in row["reason"]] == []


# ---------------------------------------------------------------------------
# ② 回写失败：不残留 + 不静默 + 原 PDF 仍在
# ---------------------------------------------------------------------------

@_needs_edge
def test_write_failure_is_not_silent_and_leaves_no_temp_file(client, monkeypatch):
    """打桩让 `tmp_path.replace` 抛异常 ⇒ 临时文件不残留、`degraded` 点名、原 PDF 仍在。"""
    seen = _force_one_change(monkeypatch)
    real_replace = Path.replace

    def failing_replace(self, target):
        if self.name.endswith(".textfix"):
            raise OSError("模拟回写失败（任务 141）")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", failing_replace)
    out, zip_path = _build_with_pdfs(client, "回写失败不静默", monkeypatch)
    manifest = _manifest(out)

    # ① 临时文件：目录 / 清单 / zip 三处都不残留
    assert [p.relative_to(out).as_posix() for p in out.rglob("*.textfix")] == []
    assert [row["path"] for row in manifest["files"] if row["path"].endswith(".textfix")] == []
    assert [name for name in _zip_names(zip_path) if name.endswith(".textfix")] == []

    # ② 降级项：从产物里读（manifest.json 的 degraded + 00-交付说明.md 的降级行）
    hits = [row for row in manifest["degraded"]
            if row["item"].endswith(".pdf") and "文本层修复未完成" in row["reason"]]
    assert hits, f"文本层修复失败了却没吭声：{manifest['degraded']}"
    readme = (out / "00-交付说明.md").read_text(encoding="utf-8")
    for row in hits:
        assert row["item"] in readme, f"{row['item']} 没写进 00-交付说明.md"
    # 每一份 PDF 都该被点名（打桩下每份的回写都失败了）——只留一处调用会在这里红
    pdfs = {row["path"] for row in manifest["files"] if row["path"].endswith(".pdf")}
    assert {row["item"] for row in hits} == pdfs, (
        f"漏点名的 PDF：{sorted(pdfs - {row['item'] for row in hits})}")

    # ③ 原 PDF 仍在、仍是合法 PDF、sha1 与清单一致（不许换坏文件、更不许删）
    listed = {row["path"]: row for row in manifest["files"]}
    assert seen["rounds"] >= 1, "文本层修复那条路根本没跑到 ⇒ 这条等于没考到"
    assert [row["path"] for row in manifest["files"] if row["path"].endswith(".pdf")], \
        "一个 PDF 都没出来 ⇒ 这条等于没考到"
    for row in hits:
        pdf = out / row["item"]
        assert pdf.is_file() and pdf.read_bytes()[:5] == b"%PDF-", row["item"]
        assert listed[row["item"]]["sha1"] == hashlib.sha1(pdf.read_bytes()).hexdigest()

    # ④ 清理没把正常文件带走：磁盘口径仍自洽
    assert _disk_files(out) == set(listed) | {"manifest.json"}


# ---------------------------------------------------------------------------
# ③ 打包前兜底扫描：漏进目标目录的临时文件也会被清掉 + 留痕
# ---------------------------------------------------------------------------

@_needs_edge
def test_prepackage_scan_purges_a_leaked_temp_file_and_records_it(client, monkeypatch):
    """在写 .bat 那一刻往包目录塞一个 `*.pdf.textfix` ⇒ 出包前清掉 + 记进 degraded + 不进 zip。"""
    real_write_text = delivery._write_text
    leaked: dict = {}

    def sneaky_write_text(root, rel, text):
        row = real_write_text(root, rel, text)
        if rel == integrity_check.BAT_NAME:      # 这次写入紧挨着兜底扫描
            path = root / "leaked-141.pdf.textfix"
            path.write_bytes(b"temp")
            leaked["path"] = path
        return row

    monkeypatch.setattr(delivery, "_write_text", sneaky_write_text)
    out, zip_path = _build_with_pdfs(client, "兜底扫描", monkeypatch)
    manifest = _manifest(out)

    assert "path" in leaked, "注入没生效 ⇒ 这条等于没考到"
    assert not leaked["path"].exists(), "兜底扫描没清掉漏进来的临时文件"
    rows = [row for row in manifest["degraded"] if row["item"] == "leaked-141.pdf.textfix"]
    assert rows and "已清理" in rows[0]["reason"], manifest["degraded"]
    assert "leaked-141.pdf.textfix" in (out / "00-交付说明.md").read_text(encoding="utf-8")
    assert [row["path"] for row in manifest["files"] if row["path"].endswith(".textfix")] == []
    assert [name for name in _zip_names(zip_path) if name.endswith(".textfix")] == []


# ---------------------------------------------------------------------------
# ⑤ 源头自己就得清：回写失败也不留 `.textfix`（不依赖打包前那道兜底扫描）
# ---------------------------------------------------------------------------

def test_source_cleanup_leaves_no_temp_file_when_write_fails(tmp_path, monkeypatch):
    """变异 m1 的守门人：把 `finally` 里的清理剥掉，这条必须红。

    只走 `repair_pdf_text_layer`（**不经过** `build_delivery` ⇒ 打包前兜底扫描不参与）：
    回写失败时那个 `*.pdf.textfix` 必须由源头的 `finally` 自己删掉。
    """
    from pypdf import PdfWriter

    pdf = tmp_path / "1.1-x.pdf"
    with pdf.open("wb") as fh:
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.write(fh)

    monkeypatch.setattr(delivery, "_repair_writer_tounicode",
                        lambda writer, source_cps: (set(), 1, 1, []))
    real_replace = Path.replace

    def failing_replace(self, target):
        if self.name.endswith(".textfix"):
            raise OSError("模拟回写失败（任务 141）")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", failing_replace)
    result = delivery.repair_pdf_text_layer(pdf, source_text="用户")

    assert result["ok"] is False and result["unrepaired"] is True
    assert not (tmp_path / "1.1-x.pdf.textfix").exists(), \
        "源头没清掉临时文件（打包前兜底不该是唯一防线）"
    assert pdf.read_bytes()[:5] == b"%PDF-" and pdf.stat().st_size > 0


# ④ 兜底扫描的边界：只认临时后缀 / 像备份的 `~`，正常交付件一个都不删（任务 142 收紧了 `~`）
# ---------------------------------------------------------------------------

def test_stray_scan_only_touches_temp_suffixes(tmp_path):
    """正常交付件（.md / .html / .pdf / .excalidraw / .bat / 名字带 `~` 的）一个都不许删。

    任务 142 把 `~` 收紧：旧规则「凡 `~` 结尾就删」，会把正当文件误删；现在只有
    `<已知交付后缀>~`（`讲义.md~`，原件的编辑器备份）才删，`z~` 一律留着。
    """
    (tmp_path / "讲义").mkdir()
    keep = [tmp_path / "00-交付说明.md", tmp_path / "讲义" / "1.1-x.html",
            tmp_path / "a.pdf", tmp_path / "b.excalidraw", tmp_path / integrity_check.BAT_NAME,
            tmp_path / "z~", tmp_path / "客户补充说明~"]
    for path in keep:
        path.write_bytes(b"x")
    drop = [tmp_path / "1.1-x.pdf.textfix", tmp_path / "old.bak", tmp_path / "x.tmp",
            tmp_path / "y.TEXTFIX", tmp_path / "1.1-讲义.md~", tmp_path / "plot.tmp~"]
    for path in drop:
        path.write_bytes(b"x")

    removed = delivery._purge_stray_files(tmp_path)

    assert sorted(row["path"] for row in removed) == sorted(path.name for path in drop)
    assert all(row["kind"] == "file" and row["files"] == 1 for row in removed)
    assert all(path.exists() for path in keep)
    assert not any(path.exists() for path in drop)


def test_stray_scan_sweeps_a_whole_temp_directory(tmp_path):
    """任务 142：`图表/foo.tmp/` 这类**目录**连同里面所有内容一起清（旧规则只扫 `is_file()`）。"""
    nested = tmp_path / "图表" / "foo.tmp"
    (nested / "深层").mkdir(parents=True)
    (nested / "1.1-临时.png").write_bytes(b"x")
    (nested / "深层" / "b.md").write_bytes(b"x")
    (tmp_path / "图表" / "keep.svg").write_bytes(b"x")
    (tmp_path / "z~").write_bytes(b"x")          # `~` 收紧后仍不许删

    removed = delivery._purge_stray_files(tmp_path)

    assert [row["path"] for row in removed] == ["图表/foo.tmp/"]
    assert removed[0]["kind"] == "dir" and removed[0]["files"] == 2
    assert not nested.exists()
    assert (tmp_path / "图表" / "keep.svg").is_file() and (tmp_path / "z~").is_file()


def test_prepackage_scan_purges_a_leaked_temp_dir_and_records_it(client, monkeypatch):
    """真出包：写 `.bat` 那一刻塞一个 `图表/foo.tmp/` 目录 ⇒ 清掉 + 记进 degraded + 不进 zip。

    不需要 Edge（兜底扫描与 PDF 无关，`pdf off` 时同样执行）。
    """
    real_write_text = delivery._write_text
    leaked: dict = {}

    def sneaky_write_text(root, rel, text):
        row = real_write_text(root, rel, text)
        if rel == integrity_check.BAT_NAME:      # 这次写入紧挨着兜底扫描
            path = root / "图表" / "foo.tmp"
            (path / "深层").mkdir(parents=True, exist_ok=True)
            (path / "1.1-临时.png").write_bytes(b"temp")
            (path / "深层" / "b.md").write_bytes(b"temp")
            leaked["path"] = path
        return row

    monkeypatch.setattr(delivery, "_write_text", sneaky_write_text)
    course_id = _new_course(client, "兜底扫目录")
    _course_with_content(client, course_id, monkeypatch)
    result = _build(client, course_id, {"pdf": "off"})
    out_dir = _delivery_dir(result["build_id"])
    manifest = _manifest(out_dir)

    assert "path" in leaked, "注入没生效 ⇒ 这条等于没考到"
    assert not leaked["path"].exists(), "兜底扫描没清掉漏进来的临时目录"
    rows = [row for row in manifest["degraded"] if row["item"] == "图表/foo.tmp/"]
    assert rows, manifest["degraded"]
    assert "已清理" in rows[0]["reason"] and "连同里面的 2 个文件" in rows[0]["reason"]
    assert "图表/foo.tmp/" in (out_dir / "00-交付说明.md").read_text(encoding="utf-8")
    assert [row["path"] for row in manifest["files"] if "foo.tmp" in row["path"]] == []
    assert not any("foo.tmp" in name for name in _zip_names(Path(result["zip"])))


# ---------------------------------------------------------------------------
# ⑤ 任务 143：白名单照**真包实测**补齐（`<已交付后缀>~` 的真备份必须删、且不许静默）
# ---------------------------------------------------------------------------

#: 2026-10-06 实测：29 个真包（真机 `backend\storage\deliveries` 24 个 + 临时环境 5 个）的
#: 文件后缀**并集** —— 用 `rglob` 扫出来的，不是凭印象列的（读数见 `backend/docs/task143-*.md` §1）。
_REAL_PACKAGE_SUFFIXES = {".bat", ".canvas", ".docx", ".excalidraw", ".excalidrawlib",
                          ".html", ".js", ".json", ".md", ".pdf", ".png", ".pptx", ".srt", ".svg"}


def _suffixes(root: Path) -> set[str]:
    return {p.suffix.lower() for p in root.rglob("*") if p.is_file()}


def test_delivered_suffix_whitelist_covers_the_measured_real_suffixes():
    """白名单 ⊇ 真包实测后缀（漏掉的后缀 ⇒ 那种文件的 `~` 备份既不删、也不进 degraded）。

    142 漏的正是 `.excalidrawlib` / `.canvas` / `.js`：`_风格库.excalidrawlib~` 会直接进包。
    """
    missing = _REAL_PACKAGE_SUFFIXES - set(delivery._DELIVERED_SUFFIXES)
    assert not missing, f"白名单缺这些真包后缀：{sorted(missing)}"
    # 143 补上的三个单独钉一次（免得将来被「顺手」删掉）
    assert {".excalidrawlib", ".canvas", ".js"} <= set(delivery._DELIVERED_SUFFIXES)


def test_delivered_suffix_whitelist_covers_the_package_we_just_built(client, monkeypatch):
    """从**现出的真包**扫后缀，断言白名单是它的超集 —— 以后包里多出新后缀，这条会红。"""
    course_id = _new_course(client, "白名单覆盖产物后缀")
    _course_with_content(client, course_id, monkeypatch)
    out_dir = _delivery_dir(_build(client, course_id, {"pdf": "off"})["build_id"])

    found = _suffixes(out_dir)
    assert found, "包内一个文件都没有 ⇒ 这条等于没考到"
    missing = found - set(delivery._DELIVERED_SUFFIXES)
    assert not missing, f"包里有白名单没覆盖的后缀（`<后缀>~` 真备份会漏）：{sorted(missing)}"


def test_stray_scan_deletes_backups_of_every_delivered_suffix(tmp_path):
    """`<已交付后缀>~` 一律算编辑器备份（真包里确实有 `.excalidrawlib` / `.canvas` / `.js`）。

    对照：`z~` / `客户补充说明~`（证不明是备份的）**必须保留** —— 规则不许退化成「见 `~` 就删」。
    """
    keep = [tmp_path / "z~", tmp_path / "客户补充说明~",
            tmp_path / "笔记库" / "图表库" / "_风格库.excalidrawlib",
            tmp_path / "笔记库" / "概念库" / "_图谱.canvas",
            tmp_path / "讲义" / "_assets" / "mermaid.min.js"]
    drop = [tmp_path / "笔记库" / "图表库" / "_风格库.excalidrawlib~",
            tmp_path / "笔记库" / "概念库" / "_图谱.canvas~",
            tmp_path / "讲义" / "_assets" / "mermaid.min.js~",
            tmp_path / "1.1-讲义.md~", tmp_path / "old.bak", tmp_path / "plot.tmp~"]
    for path in keep + drop:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")

    removed = delivery._purge_stray_files(tmp_path)

    assert sorted(row["path"] for row in removed) == sorted(p.relative_to(tmp_path).as_posix()
                                                            for p in drop)
    assert all(path.is_file() for path in keep), [p.name for p in keep if not p.exists()]
    assert not any(path.exists() for path in drop)


def test_prepackage_scan_purges_a_delivered_suffix_backup_and_records_it(client, monkeypatch):
    """真出包：塞一个 `.excalidrawlib~`（真包里有这种文件）⇒ 清掉 + 记进 degraded + 不进 zip。

    `z~` / `客户补充说明~` 同时塞进去：它们必须**留在包目录与 zip 里**（正当文件不许误删）。
    """
    real_write_text = delivery._write_text
    injected: dict = {}

    def sneaky_write_text(root, rel, text):
        row = real_write_text(root, rel, text)
        if rel == integrity_check.BAT_NAME:      # 这次写入紧挨着兜底扫描
            for name in ("z~", "客户补充说明~"):
                (root / name).write_bytes(b"keep")
            backup = root / "笔记库" / "图表库" / "_风格库.excalidrawlib~"
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(b"backup")
            injected["backup"] = backup
        return row

    monkeypatch.setattr(delivery, "_write_text", sneaky_write_text)
    course_id = _new_course(client, "兜底扫已交付后缀备份")
    _course_with_content(client, course_id, monkeypatch)
    result = _build(client, course_id, {"pdf": "off"})
    out_dir = _delivery_dir(result["build_id"])
    manifest = _manifest(out_dir)

    assert "backup" in injected, "注入没生效 ⇒ 这条等于没考到"
    assert not injected["backup"].exists(), "真编辑器备份（.excalidrawlib~）没被清掉"
    rows = [row for row in manifest["degraded"]
            if row["item"] == "笔记库/图表库/_风格库.excalidrawlib~"]
    assert rows, manifest["degraded"]
    assert "已清理" in rows[0]["reason"] and "临时文件" in rows[0]["reason"]
    assert "笔记库/图表库/_风格库.excalidrawlib~" in (out_dir / "00-交付说明.md").read_text(
        encoding="utf-8")
    names = _zip_names(Path(result["zip"]))
    assert not any("_风格库.excalidrawlib~" in name for name in names)
    # 正当 `~` 文件：磁盘在、zip 里也在（不许「见 ~ 就删」）
    for name in ("z~", "客户补充说明~"):
        assert (out_dir / name).is_file(), f"{name} 被误删了"
        assert name in names, f"{name} 没进 zip"


def test_readme_documents_the_tilde_cleanup_rule(client, monkeypatch):
    """客户看得到的规则：`<已交付后缀>~` = 编辑器备份（删）；其它 `~` 结尾 = 原样保留。"""
    course_id = _new_course(client, "交付说明写了波浪号规则")
    _course_with_content(client, course_id, monkeypatch)
    out_dir = _delivery_dir(_build(client, course_id, {"pdf": "off"})["build_id"])
    readme = (out_dir / "00-交付说明.md").read_text(encoding="utf-8")

    assert "编辑器备份" in readme
    assert "讲义.md~" in readme            # 形如「已交付文件 + ~」
    assert "原样保留" in readme
    assert "2.2 降级项" in readme          # 清理记录去哪儿看
