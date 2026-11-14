"""任务 48m 回归测试：HTML / SCORM 导出包「真能交付」。

覆盖：每课时一页（讲义数 + 1 张目录）、页面含「怎么读」图例与「## 来源」、页面内引用
不悬空（图表走行内 SVG / 数据表）、SCORM 的 <item> / <resource> / <file> 与包内文件逐一
对得上、缺讲义的课时显式降级且仍在册、连续导出两次幂等、包大小相对基线（662 B）上涨。

协作规则第 7 条：DATABASE_URL / STORAGE_DIR / OBSIDIAN_VAULT_ROOT 由 conftest 统一指向
临时目录，本文件不碰真实库与真实知识库。
"""
from __future__ import annotations

import hashlib
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from app.services import publish as publish_mod
from tests.test_delivery import (LECTURE_MD, _add_section, _gen_lecture, _new_course,
                                 _node_id, _plan_approve, _seed_figures, _seed_material)

CP_NS = "http://www.imsglobal.org/xsd/imscp_v1p1"
ADLCP_NS = "http://www.adlnet.org/xsd/adlcp_v1p3"
NS = {"cp": CP_NS}
RE_REF = re.compile(r'(?:src|href)="([^"]+)"')


def _export(client, course_id: str, fmt: str) -> Path:
    resp = client.post("/api/publish/export", json={"course_id": course_id, "format": fmt})
    assert resp.status_code == 200, resp.text
    path = Path(resp.json()["file"])
    assert path.is_file()
    return path


def _entries(path: Path) -> dict:
    with zipfile.ZipFile(path) as zf:
        assert zf.testzip() is None
        return {name: zf.read(name) for name in zf.namelist()}


def _dangling(entries: dict) -> set:
    """页面里指向包外的相对引用（基线：任务 48m 之前图片 / 图表根本不在包里）。"""
    missing = set()
    for name, blob in entries.items():
        if not name.endswith(".html"):
            continue
        for ref in RE_REF.findall(blob.decode("utf-8")):
            if ref.startswith(("http://", "https://", "#", "mailto:", "data:")):
                continue
            target = ref.split("#")[0]
            if target and target not in entries:
                missing.add(f"{name} -> {target}")
    return missing


def _course_with_lessons(client, monkeypatch, keys=("1.1", "1.2", "1.3")) -> dict:
    """一课三课时、每课时一篇讲义；只有第一课时挂了概念图。"""
    course_id = _new_course(client, "导出包探针")
    _plan_approve(client, course_id)
    _seed_material(course_id)
    node_ids = {}
    for key in keys:
        _add_section(course_id, key, f"光合作用 {key}")
        node_ids[key] = _node_id(client, course_id, key)
    diagram_id = _seed_figures(course_id, node_ids[keys[0]], monkeypatch)
    lectures = {}
    for key in keys:
        lectures[key] = _gen_lecture(client, course_id, node_ids[key],
                                     LECTURE_MD.replace("DIAGRAM_ID", diagram_id), monkeypatch)
    return {"course_id": course_id, "node_ids": node_ids, "lectures": lectures}


def test_html_package_page_per_lesson(client, monkeypatch):
    """页面数 == 讲义数 + 1（目录）；每页带图例 / 来源小节 / 表格 / mermaid，且无悬空引用。"""
    data = _course_with_lessons(client, monkeypatch)
    entries = _entries(_export(client, data["course_id"], "html"))
    pages = sorted(name for name in entries if name.endswith(".html"))
    assert len(pages) == len(data["lectures"]) + 1
    assert "index.html" in entries and "manifest.json" in entries
    index = entries["index.html"].decode("utf-8")
    for page in pages:
        if page != "index.html":
            assert f'href="{page}"' in index
            assert page.startswith("01-")
    for page in pages:
        if page == "index.html":
            continue
        body = entries[page].decode("utf-8")
        assert "怎么读" in body                # 与交付包同源的阅读图例
        assert "<h2>来源</h2>" in body         # 来源小节
        assert "<table>" in body               # 讲义里的对比表
        assert 'class="mermaid"' in body       # 讲义里的流程图
    first = next(page for page in pages if page.startswith("01-1.1-"))
    assert "<svg" in entries[first].decode("utf-8")   # 概念图走行内 SVG，不是外链
    assert _dangling(entries) == set()


def test_scorm_package_manifest_matches_files(client, monkeypatch):
    """<item> == 课时数；<resource> / <file> 与实际文件逐一对得上；XML 可解析、命名空间正确。"""
    data = _course_with_lessons(client, monkeypatch)
    entries = _entries(_export(client, data["course_id"], "scorm"))
    root = ElementTree.fromstring(entries["imsmanifest.xml"].decode("utf-8"))
    assert root.tag == f"{{{CP_NS}}}manifest"
    items = root.findall(".//cp:organization/cp:item", NS)
    resources = root.findall(".//cp:resource", NS)
    assert len(items) == len(data["lectures"])
    assert len(resources) == len(data["lectures"]) + 1     # + 目录页资源 res-index
    ids = {res.get("identifier") for res in resources}
    assert {item.get("identifierref") for item in items} <= ids
    files = {node.get("href") for node in root.findall(".//cp:file", NS)}
    assert files == set(entries) - {"imsmanifest.xml"}
    scormtype = f"{{{ADLCP_NS}}}scormtype"
    assert next(res.get(scormtype) for res in resources
                if res.get("identifier") == "res1") == "sco"
    assert next(res.get(scormtype) for res in resources
                if res.get("identifier") == "res-index") == "asset"
    for res in resources:
        assert res.get("href") in entries
    assert _dangling(entries) == set()


def test_packages_are_idempotent(client, monkeypatch):
    """连续导出两次：zip 内条目内容（sha1）逐条一致。"""
    data = _course_with_lessons(client, monkeypatch)
    for fmt in ("html", "scorm"):
        first = {name: hashlib.sha1(blob).hexdigest()
                 for name, blob in _entries(_export(client, data["course_id"], fmt)).items()}
        second = {name: hashlib.sha1(blob).hexdigest()
                  for name, blob in _entries(_export(client, data["course_id"], fmt)).items()}
        assert first == second
        assert first


def test_lesson_without_lecture_is_explicit(client, monkeypatch):
    """没讲义的课时照旧有页面、照旧进目录与 manifest，页面写明「本课时尚未生成讲义」。"""
    course_id = _new_course(client, "缺讲义探针")
    _plan_approve(client, course_id)
    _add_section(course_id, "1.1", "有讲义")
    _add_section(course_id, "1.2", "没有讲义")
    _gen_lecture(client, course_id, _node_id(client, course_id, "1.1"), LECTURE_MD, monkeypatch)
    entries = _entries(_export(client, course_id, "html"))
    pages = sorted(name for name in entries
                   if name.endswith(".html") and name != "index.html")
    assert len(pages) == 2
    empty = [name for name in pages
             if publish_mod.LESSON_MISSING in entries[name].decode("utf-8")]
    assert len(empty) == 1
    assert publish_mod.LESSON_MISSING in entries["index.html"].decode("utf-8")
    scorm = _entries(_export(client, course_id, "scorm"))
    root = ElementTree.fromstring(scorm["imsmanifest.xml"].decode("utf-8"))
    assert len(root.findall(".//cp:item", NS)) == 2
    assert len(root.findall(".//cp:resource", NS)) == 3


def test_package_size_above_old_baseline(client, monkeypatch):
    """基线（任务 48m 之前）：html 包 2 条目、index.html 662 B；scorm 包 1031 + 662 B。"""
    data = _course_with_lessons(client, monkeypatch)
    path = _export(client, data["course_id"], "html")
    entries = _entries(path)
    payload = sum(len(blob) for blob in entries.values())
    assert len(entries) >= len(data["lectures"]) + 3
    assert payload > 662 * 5
    assert path.stat().st_size > 2000
