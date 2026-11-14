"""任务 88 回归测试：交付包里的**单文件 HTML 课件**（**0 LLM**）。

钉四组：
① **文件与位置**：`课件/HTML/{deck 标题}.html` 每个 deck 一份（标题逐字，只替非法字符）；
② **单文件口径**：无 `http(s)://`、无外部 `<link rel=stylesheet>` / `<script src>`、有
   `viewport`、有 `@media print` + `size: A4 landscape`、有 `@keyframes`、图内联 base64；
③ **页数据忠实**：页数 / 页序 / 标题 / 要点与课件页一致；行内 `**粗体**` 渲染成 `<strong>`
   （页数据本身不动）；备注拆成「讲稿摘要」与「讲师提示（勿投屏）」；
④ **交付包接线**：每个 HTML 都进 manifest（含 sha1 / 字节数）、`llm_calls == 0`、
   出包页「本次将包含」与交付说明 / 验收单里都写明网页版；两次构建逐字节一致（确定性）。

协作规则第 7 条：五件套由 conftest 统一指向临时目录；本文件不碰真实库、真实知识库、
真实 storage/deliveries，也不调任何模型。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from app.db.session import SessionLocal
from app.models.course import ContentItem, Course
from app.services import deck_html as deck_html_mod
from app.services import delivery as delivery_mod
from tests.test_delivery import _add_section, _new_course, _plan_approve

#: 故意的「带 http 的 SVG」：内联走 base64 之后，HTML 源码里不该再出现 http 字样。
MINI_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="240" height="90">'
            '<rect width="240" height="90" fill="#fffbf2" stroke="#374151"/>'
            '<text x="12" y="52" font-size="18">老面发酵</text></svg>')
INTERNAL_NOTE = "课件操作提示（仅讲师 / 运营可见，勿投屏）：每章课件在工作台第 3 步选中该章生成。"
SPOKEN = "这一页我们看老面发酵：先看体积，再看蜂窝，最后闻酸香。"


# ---------------------------------------------------------------------------
# 造课（课件页数据是 meta.slides + meta.layouts，与真库同一形状）
# ---------------------------------------------------------------------------

def _seed_figure(course_id: str, node_id: str, node_key: str, *, name: str, caption: str,
                 kind: str = "diagram", table: str = "") -> None:
    images = None
    body: dict = {"caption": caption}
    if kind == "diagram":
        from app.core.config import get_settings

        images = get_settings().storage_dir / "images"
        images.mkdir(parents=True, exist_ok=True)
        (images / name).write_text(MINI_SVG, encoding="utf-8")
        body["svg_preview"] = f"images/{name}"
    if table:
        body["markdown_table"] = table
    db = SessionLocal()
    try:
        db.add(ContentItem(course_id=course_id, node_id=node_id, kind=kind, title=caption,
                           body=body, meta={"node_key": node_key}))
        db.commit()
    finally:
        db.close()


def _seed_deck(course_id: str, *, title: str, scope: str, chapter_key: str,
               slides: list[tuple[str, str, list[str]]], layouts: list[str],
               pptx: bool = True) -> str:
    """一份带页级备注的课件；`pptx=False` 造「没有页数据的老课件」（meta.slides 为空）。"""
    from app.core.config import get_settings

    deck_dir = get_settings().storage_dir / "ppt" / f"{course_id}_{scope or 'course'}"
    deck_dir.mkdir(parents=True, exist_ok=True)
    path = deck_dir / f"{course_id}-{scope}-{chapter_key or 'all'}.pptx"
    body: dict = {"engine": "python-pptx", "pptx": str(path)}
    if pptx:
        from pptx import Presentation

        prs = Presentation()
        for _ in slides:
            prs.slides.add_slide(prs.slide_layouts[5])
        prs.save(str(path))
        meta = {"scope": scope, "chapter_key": chapter_key, "layouts": list(layouts),
                "slides": [{"index": index, "title": page_title, "bullets": list(bullets),
                            "notes_excerpt": note, "image": ""}
                           for index, (page_title, note, bullets) in enumerate(slides, 1)]}
    else:
        path.write_bytes(b"not-a-real-pptx")
        meta = {"scope": scope, "chapter_key": chapter_key}
    db = SessionLocal()
    try:
        item = ContentItem(course_id=course_id, kind="ppt", title=title, body=body, meta=meta)
        db.add(item)
        db.commit()
        return item.id
    finally:
        db.close()


def _course_with_decks(client, title: str, *, empty_deck: bool = False) -> str:
    """一门「整课课件 + 章课件 + 两个知识点 + 概念图/数据卡」的课（0 真实调用）。"""
    course_id = _new_course(client, title)
    _plan_approve(client, course_id)
    _add_section(course_id, "1.1", "老面发酵")
    _add_section(course_id, "1.2", "温度窗口")
    from app.services.planner import list_nodes

    db = SessionLocal()
    try:
        nodes = {str(n.node_key): n for n in list_nodes(db, course_id)}
        point_a, point_b = nodes["1.1"], nodes["1.2"]
        _seed_deck(course_id, title="整课课件", scope="course", chapter_key="",
                   slides=[("课程封面", SPOKEN, ["受众：初中生"]),
                           ("课程目标", SPOKEN, ["理解发酵原理"])],
                   layouts=["cover", "goals"])
        _seed_deck(course_id, title="第1章课件", scope="chapter", chapter_key="1",
                   slides=[("1 老面发酵", f"{SPOKEN}\n{INTERNAL_NOTE}", ["本章课件"]),
                           ("老面发酵", f"**发酵判断**：体积膨胀约 2 倍。\n{INTERNAL_NOTE}",
                            ["**体积**：膨胀约 2 倍", "**蜂窝**：均匀细密"]),
                           ("温度窗口", "**温度**：25~30℃ 最稳。", ["**温度**：25~30℃"]),
                           ("本章回顾", SPOKEN, ["要点回顾"])],
                   layouts=["cover", "point", "point", "review"])
        if empty_deck:
            _seed_deck(course_id, title="第2章课件", scope="chapter", chapter_key="2",
                       slides=[], layouts=[], pptx=False)
        point_a_id, point_b_id = point_a.id, point_b.id
    finally:
        db.close()
    _seed_figure(course_id, point_a_id, "1.1", name="t88-a.svg", caption="老面发酵概念图")
    _seed_figure(course_id, point_a_id, "1.1", name="t88-b.svg", caption="体积与蜂窝")
    _seed_figure(course_id, point_b_id, "1.2", name="t88-c.svg", caption="温度窗口概念图")
    _seed_figure(course_id, point_b_id, "1.2", name="", caption="温度对照", kind="chart_table",
                 table="| 温度 | 状态 |\n|---|---|\n| 25~30℃ | 正常 |\n| 35℃+ | 过酸 |")
    return course_id


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _html_files(out_dir: Path) -> list[Path]:
    return sorted((out_dir / "课件" / "HTML").glob("*.html"))


def _build_no_llm(client, course_id: str, monkeypatch) -> dict:
    """构建期间任何一次 LLM 调用都算失败（任务 88 的硬约束：0 LLM）。"""
    monkeypatch.setattr("app.services.llm.build_llm_client",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("HTML 课件不许调 LLM")))
    resp = client.post(f"/api/delivery/course/{course_id}/build", json={"pdf": "off"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _audit_html(text: str) -> list[str]:
    """单文件 HTML 的独立检查器（与前端 scripts/check-html-deck.ts 同一套判据）。"""
    bad: list[str] = []
    if re.search(r"https?://", text):
        bad.append("HTML 里有 http(s)://（外部依赖）")
    if re.search(r"<link[^>]+rel=[\"']stylesheet", text):
        bad.append("HTML 里有外部样式表 <link rel=stylesheet>")
    if re.search(r"<script[^>]+src=", text):
        bad.append("HTML 里有外部脚本 <script src>")
    if '<meta name="viewport" content="width=device-width, initial-scale=1">' not in text:
        bad.append("缺 viewport meta（逐字口径）")
    if "@media print" not in text or not re.search(r"size:\s*A4\s+landscape", text):
        bad.append("缺打印 A4 横版规则（@media print + size: A4 landscape）")
    if "@keyframes" not in text:
        bad.append("缺 @keyframes 入场动效")
    if "animation-duration:1.05s" not in text.replace(" ", ""):
        bad.append("入场动效时长不是 1.05s")
    return bad


# ---------------------------------------------------------------------------
# ① 文件与位置 + ② 单文件口径 + ③ 页数据忠实
# ---------------------------------------------------------------------------

def test_html_deck_files_are_single_file_and_faithful(client, monkeypatch):
    course_id = _course_with_decks(client, "HTML 课件探针")
    summary = _build_no_llm(client, course_id, monkeypatch)
    out_dir = delivery_mod.deliveries_root() / summary["build_id"]
    files = _html_files(out_dir)
    assert len(files) == 2, f"应该每个 deck 一份：{files}"

    db = SessionLocal()
    try:
        decks = [d for d in db.query(ContentItem).filter(
            ContentItem.course_id == course_id, ContentItem.kind == "ppt").all()]
        titles = sorted(str(d.title) for d in decks)
    finally:
        db.close()
    assert sorted(p.stem for p in files) == titles, "文件名必须是 deck 标题逐字（只替非法字符）"

    chapter = next(p for p in files if p.stem == "第1章课件")
    text = chapter.read_text(encoding="utf-8")
    assert _audit_html(text) == []
    assert text.count('class="slide"') == 4, "页数与课件页数据一致"
    assert "data:image/svg+xml;base64," in text, "概念图必须内联（不带外链）"
    assert text.count("data:image/svg+xml;base64,") == 3, "1.1 两页 + 1.2 一页 ⇒ 3 张图"
    assert "<strong>发酵判断</strong>" in text, "**粗体** 要渲染成 <strong>"
    assert "**" not in text, "行内标记不许原样落进 HTML"
    assert "老面发酵" in text and "体积" in text
    assert "本页讲稿摘要" in text and INTERNAL_NOTE in text, "备注：口播 + 内部提示都要有"
    assert "讲师提示（勿投屏）" in text, "内部提示必须单独标注"
    assert "1 / 4" in text and "4 / 4" in text, "页脚要有页码"
    # 数据卡（无矢量源）以表格随页呈现
    assert "<table>" in text and "温度对照" in text

    course_deck = next(p for p in files if p.stem == "整课课件")
    assert course_deck.read_text(encoding="utf-8").count('class="slide"') == 2


def test_html_deck_enters_manifest_and_plan(client, monkeypatch):
    course_id = _course_with_decks(client, "HTML 清单探针")
    summary = _build_no_llm(client, course_id, monkeypatch)
    out_dir = delivery_mod.deliveries_root() / summary["build_id"]
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    declared = {row["path"]: row for row in manifest["files"]}
    for path in _html_files(out_dir):
        rel = path.relative_to(out_dir).as_posix()
        assert rel.startswith("课件/HTML/"), rel
        assert rel in declared, f"{rel} 没进 manifest"
        assert declared[rel]["sha1"] == hashlib.sha1(path.read_bytes()).hexdigest()
        assert declared[rel]["bytes"] == path.stat().st_size
    assert manifest["llm_calls"] == 0 and summary["llm_calls"] == 0
    assert summary["counts"]["html_decks"] == 2
    on_disk = {p.relative_to(out_dir).as_posix() for p in out_dir.rglob("*") if p.is_file()}
    assert on_disk - {"manifest.json"} == set(declared)

    # 出包页「本次将包含」+ 交付说明 + 验收单：三处都要写明网页版（任务 88 §A3）
    db = SessionLocal()
    try:
        plan = delivery_mod.collect_plan(db, db.get(Course, course_id))
    finally:
        db.close()
    row = next(item for item in plan["items"] if item["key"] == "decks")
    assert "课件/HTML/" in row["label"] and "可投影" in row["label"], row["label"]
    readme = (out_dir / "00-交付说明.md").read_text(encoding="utf-8")
    assert "`课件/HTML/`" in readme and "网页版课件" in readme
    accept = (out_dir / "01-验收单.md").read_text(encoding="utf-8")
    assert "网页版 HTML" in accept


def test_html_deck_is_deterministic_and_handles_empty_pages(client, monkeypatch):
    course_id = _course_with_decks(client, "HTML 确定性探针", empty_deck=True)
    first = _build_no_llm(client, course_id, monkeypatch)
    second = _build_no_llm(client, course_id, monkeypatch)
    dir_a = delivery_mod.deliveries_root() / first["build_id"]
    dir_b = delivery_mod.deliveries_root() / second["build_id"]
    names = sorted(p.name for p in _html_files(dir_a))
    assert names == sorted(p.name for p in _html_files(dir_b))
    for name in names:
        assert (dir_a / "课件" / "HTML" / name).read_bytes() == \
            (dir_b / "课件" / "HTML" / name).read_bytes(), f"{name} 两次构建不一致（非确定性）"
    # 没有页数据的老课件：照样一份文件 + 一句诚实的提示（绝不静默少文件）
    stub = (dir_a / "课件" / "HTML" / "第2章课件.html").read_text(encoding="utf-8")
    assert "还没有页数据" in stub and stub.count('class="slide"') == 1
    assert _audit_html(stub) == []


# ---------------------------------------------------------------------------
# ④ 渲染层单元口径
# ---------------------------------------------------------------------------

def test_html_name_and_inline_markup_rules():
    from types import SimpleNamespace

    assert deck_html_mod.html_name(SimpleNamespace(title="第1章 发酵 · 课件")) \
        == "第1章 发酵 · 课件.html"
    assert deck_html_mod.html_name(SimpleNamespace(title='a/b:c*d?e"f')) == "a_b_c_d_e_f.html"
    assert deck_html_mod.html_name(SimpleNamespace(title="")) == "课件.html"
    inline = deck_html_mod._inline
    assert inline("**糖解途径**：糖 → CO₂") == "<strong>糖解途径</strong>：糖 → CO₂"
    assert inline("常见误区：*解析**：干酵母不耐低温") == "常见误区：解析：干酵母不耐低温"
    assert inline("__粗体__ 与 **粗体**") == "<strong>粗体</strong> 与 <strong>粗体</strong>"
    assert inline("2 * 3 = 6 与 1_2 下标") == "2 * 3 = 6 与 1_2 下标"
    assert inline('a<b & "c"') == "a&lt;b &amp; &quot;c&quot;"
    # 常量（与样板口径一致，改了就说明视觉口径被动了）
    assert (deck_html_mod.DIR_NAME, deck_html_mod.CANVAS_W, deck_html_mod.CANVAS_H) == ("HTML", 1122, 793)
    assert (deck_html_mod.FS_EYEBROW, deck_html_mod.FS_HEADLINE, deck_html_mod.FS_MIN) == (27, 54, 14)
    assert deck_html_mod.ANIM_SECONDS == 1.05
    assert deck_html_mod.ANIM_STEP_MS == 170
    assert "A4 landscape" in deck_html_mod._CSS and "@keyframes" in deck_html_mod._CSS
    assert "http" not in deck_html_mod._CSS


def test_html_deck_checker_catches_mutations():
    """把三种「不该发生」的样子喂给检查器 —— 必须逐条判红（反证据的单元版）。"""
    text = "<html><head><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">" \
           "<style>@media print{@page{size:A4 landscape}}@keyframes rise{from{opacity:0}}" \
           ".a{animation-duration:1.05s}</style></head><body></body></html>"
    assert _audit_html(text) == []
    assert _audit_html(text.replace("</style>", "@import url(https://cdn.example.com/x.css);</style>"))
    assert _audit_html(text.replace("size:A4 landscape", "size:A4"))
    assert _audit_html(text.replace("@keyframes rise{from{opacity:0}}", ""))
    assert _audit_html(text.replace(
        '<meta name="viewport" content="width=device-width, initial-scale=1">', ""))
    assert _audit_html(text.replace(
        "</head>", '<script src="app.js"></script></head>'))