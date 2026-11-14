"""任务 96：交付 PDF 必须**真内嵌** OFL 字体（``/FontFile2``），不是 Chromium 的 Type3 轮廓。

任务 95 把字体族换成 OFL 之后实测出两条代价：① 同一门课同一批 18 份 PDF 从 5.7 MB 涨到
13.0 MB（2.27 倍）；② **字体其实没内嵌** —— Chromium 改用 ``/Subtype /Type3`` 矢量轮廓画字，
字体名只出现在描述符 ``/FontName`` 里，没有字体程序。

根因：本机 ``Noto Sans SC`` 是 Thin 实例（``wght`` 轴退化 100..100）、思源黑体只装了 Regular
一个面 ⇒ 标题 / ``strong`` / ``th`` 全靠 Chromium **合成粗体**，而合成粗体嵌不进 PDF。

解法（本文件即判据）：给**打印用的那份 HTML**注入两个**静态** SIL OFL 面
（``sourcehansans.ttf`` = 400 / ``sourcehansansb.ttf`` = 700）并把字重钉死，
交付给客户的 HTML 一个字节都不动。

真打印那条断言钉的是**结果**（字体程序 / Type3 个数），不是字符串：把注入去掉（回到任务 95
的现状）或把 Bold 面指向 Regular 文件，本文件都会红。
"""
from __future__ import annotations

import hashlib
import re
import struct
from pathlib import Path
from uuid import uuid4

import pytest

from app.db.session import SessionLocal
from app.models.course import ContentItem, Course, Outline, OutlineNode
from app.services import delivery

#: Chromium 给 ``@font-face`` 提供的字体写的名字：子集前缀 + 空 PostScript 名
#: （``/AAAAAA+-`` / ``/BAAAAA+-``）。4 组不同 family 名实测都是这个形状，与 CSS 里的名字无关。
_INJECTED_FACE = re.compile(r"^/[A-Z]{6}\+-$")
#: 任务 96 §B1：Type3 只允许剩 emoji 量级（任务 95 现状 = 325）。
_TYPE3_BUDGET = 80


def _weight_class(font_path: Path) -> int:
    """读字体自己的 ``OS/2.usWeightClass``（400 = Regular / 700 = Bold）。"""
    blob = font_path.read_bytes()
    offset = struct.unpack(">I", blob[12:16])[0] if blob[:4] == b"ttcf" else 0
    count = struct.unpack(">H", blob[offset + 4:offset + 6])[0]
    for i in range(count):
        row = offset + 12 + 16 * i
        if blob[row:row + 4] == b"OS/2":
            table = struct.unpack(">I", blob[row + 8:row + 12])[0]
            return struct.unpack(">H", blob[table + 4:table + 6])[0]
    raise AssertionError(f"{font_path.name} 没有 OS/2 表，读不到 usWeightClass")


def _font_rows(pdf_path: Path) -> list[dict]:
    """列出这份 PDF 用到的字体：``{name, subtype, program, sha, key}``（去重后）。

    ``key`` = 字体描述符里那个字体程序的键名（``/FontFile2`` = TrueType 真内嵌）；
    没有程序时为 ``""`` —— 任务 97 靠它判「非 Type3 字体是不是**真**内嵌」。
    """
    from pypdf import PdfReader

    rows: dict[tuple, dict] = {}
    for page in PdfReader(str(pdf_path)).pages:
        resources = page.get("/Resources")
        for font in ((resources.get("/Font") if resources is not None else None) or {}).values():
            obj = font.get_object()
            descriptor = obj.get("/FontDescriptor")
            if descriptor is None and obj.get("/DescendantFonts"):
                descriptor = obj["/DescendantFonts"][0].get_object().get("/FontDescriptor")
            program, sha, program_key = 0, "", ""
            if descriptor is not None:
                desc = descriptor.get_object()
                for key in ("/FontFile", "/FontFile2", "/FontFile3"):
                    if key in desc:
                        raw = desc[key].get_object().get_data()
                        program, sha = len(raw), hashlib.sha1(raw).hexdigest()
                        program_key = key
                        break
            row = {"name": str(obj.get("/BaseFont")), "subtype": str(obj.get("/Subtype")),
                   "program": program, "sha": sha, "key": program_key}
            rows[(row["name"], row["subtype"])] = row
    return sorted(rows.values(), key=lambda r: r["name"])


# ---------------------------------------------------------------------------
# ① 注入内容：两个**不同文件**的静态面、字重钉死、等宽与 math 不动
# ---------------------------------------------------------------------------

def test_print_fonts_are_two_static_faces_with_matching_weight_classes():
    """打印用的两个面必须是**不同文件**，且字体自己的 ``usWeightClass`` 就是 400 / 700。

    这条是「真 Bold 面」的守门人：把 Bold 指向 Regular 文件（假装有粗体）⇒ 这里立刻红。
    """
    faces = delivery.print_font_paths()
    assert [path.name for path, _weight in faces] == ["sourcehansans.ttf", "sourcehansansb.ttf"]
    assert [weight for _path, weight in faces] == [400, 700]
    assert faces[0][0] != faces[1][0]
    for path, _weight in faces:
        if not path.is_file():
            pytest.skip(f"本机没有 {path}，跳过真读字体断言")
    assert [_weight_class(path) for path, _weight in faces] == [400, 700]


def test_print_font_style_declares_two_faces():
    """``@font-face`` 两条、src 指向各自的字体文件。"""
    style = delivery.print_font_style()
    if style is None:
        pytest.skip("本机没有静态 OFL 字体（sourcehansans.ttf / sourcehansansb.ttf）")
    assert style.count("@font-face") == 2
    for path, weight in delivery.print_font_paths():
        assert f"font-weight:{weight};" in style
        assert path.as_uri() in style
    assert 'format("truetype")' in style


def test_print_font_style_pins_weights_and_keeps_mono_and_math():
    """字重**钉死** 400 / 700（绝不让 Chromium 合成粗体）；等宽与 math 的字体链不动。"""
    style = delivery.print_font_style()
    if style is None:
        pytest.skip("本机没有静态 OFL 字体（sourcehansans.ttf / sourcehansansb.ttf）")
    assert "html body * { font-weight: 400 !important; }" in style
    assert ("html body h1, html body h2, html body h3, html body h4, html body h5, html body h6,\n"
            "html body strong, html body b, html body th { font-weight: 700 !important; }") in style
    assert 'font-family: "Cascadia Mono", Consolas, "Courier New", monospace !important;' in style
    assert 'font-family: "Cambria Math", "Latin Modern Math", serif !important;' in style
    assert "@FAMILY@" not in style                             # 占位符必须已经替换掉


# ---------------------------------------------------------------------------
# ② 只改「打印用的那份 HTML」：注入点唯一，其余逐字不变
# ---------------------------------------------------------------------------

def test_inject_print_fonts_inserts_one_style_block_before_head_end():
    page = delivery.html_page("光合作用的过程", "<p>正文</p>", subtitle="任务 96")
    injected, warn = delivery.inject_print_fonts(page)
    if warn:
        pytest.skip(warn)
    head_end = page.index("</head>")
    assert injected.startswith(page[:head_end])                # </head> 之前逐字不变
    assert injected.endswith(page[head_end:])                  # </head> 之后逐字不变
    assert injected.count(delivery.print_font_style()) == 1     # 只插一段
    assert "file:///" in injected                              # 本地字体路径只出现在打印件里


def test_inject_print_fonts_falls_back_when_font_missing(monkeypatch, tmp_path):
    """字体文件缺失 ⇒ 不注入、原样返回 + 一句 warn（**绝不让出包失败**）。"""
    monkeypatch.setattr(delivery, "print_font_paths",
                        lambda: [(tmp_path / "nope-400.ttf", 400), (tmp_path / "nope-700.ttf", 700)])
    page = delivery.html_page("x", "<p>x</p>")
    assert delivery.print_font_style() is None
    injected, warn = delivery.inject_print_fonts(page)
    assert injected == page and warn and "未内嵌" in warn


def test_inject_print_fonts_falls_back_without_head(monkeypatch):
    monkeypatch.setattr(delivery, "print_font_paths",
                        lambda: [(Path(__file__), 400), (Path(__file__), 700)])
    injected, warn = delivery.inject_print_fonts("<body>没有 head</body>")
    assert injected == "<body>没有 head</body>" and warn and "未内嵌" in warn


def test_print_document_prints_from_a_sibling_temp_file_and_cleans_up(tmp_path, monkeypatch):
    """打印件必须落在 PDF **同目录**（``../_assets/mermaid.min.js`` 是相对引用），且用完即删。"""
    seen: dict = {}

    def fake_print_pdf(html_path, pdf_path, **_kwargs):
        tmp_html = Path(html_path)
        seen["tmp_html"] = tmp_html
        seen["parent"] = tmp_html.parent
        seen["text"] = tmp_html.read_text(encoding="utf-8")
        Path(pdf_path).write_bytes(b"%PDF-1.4 fake")
        return {"ok": True, "reason": ""}

    monkeypatch.setattr(delivery, "print_pdf", fake_print_pdf)
    pdf_path = tmp_path / "讲义" / "01-发酵" / "1.1-多菌共生.pdf"
    html = "<html><head></head><body>中文</body></html>"
    assert delivery.print_document(html, pdf_path, edge="edge")["ok"] is True
    assert seen["parent"] == pdf_path.parent                   # 同目录 ⇒ 相对引用照常解析
    assert seen["text"] == html
    assert not seen["tmp_html"].exists(), "打印用的临时 HTML 必须删掉（不进 manifest / zip）"
    assert not list(pdf_path.parent.glob(".print-*.html"))


# ---------------------------------------------------------------------------
# ③ 真打印：字体程序真的进了 PDF（钉结果，不钉字符串）
# ---------------------------------------------------------------------------

@pytest.mark.skipif(delivery.find_edge() is None, reason="本机没有 Microsoft Edge，跳过 PDF 打印断言")
@pytest.mark.skipif(delivery.print_font_style() is None,
                    reason="本机没有静态 OFL 字体（sourcehansans.ttf / sourcehansansb.ttf）")
def test_printed_pdf_really_embeds_the_print_fonts(tmp_path):
    """真打印：非 Type3 字体**都有字体程序**、Type3 ≤ 80、注入的两个面（400 + 700）都在。"""
    from pypdf import PdfReader

    markdown = ("## 光合作用的过程\n\n"
                "**核心结论**：光反应把光能变成 ATP，暗反应把二氧化碳固定成糖。\n\n"
                "| 阶段 | 场所 | 产物 |\n| --- | --- | --- |\n"
                "| 光反应 | 类囊体薄膜 | ATP 与 NADPH |\n"
                "| 暗反应 | 叶绿体基质 | 三碳糖 |\n\n"
                "```\ncode_block: keep Cascadia Mono\n```\n")
    page, warn = delivery.inject_print_fonts(
        delivery.html_page("光合作用的过程", delivery.markdown_to_html(markdown), subtitle="任务 96"))
    assert warn == ""
    pdf_path = tmp_path / "讲义" / "1.1-光合作用的过程.pdf"
    result = delivery.print_document(page, pdf_path, edge=str(delivery.find_edge()))
    assert result["ok"], result

    rows = _font_rows(pdf_path)
    type3 = [row for row in rows if row["subtype"] == "/Type3"]
    real = [row for row in rows if row["subtype"] != "/Type3"]
    injected = [row for row in real if _INJECTED_FACE.match(row["name"])]

    assert len(type3) <= _TYPE3_BUDGET, f"Type3 还剩 {len(type3)} 个：{type3}"
    missing = [row["name"] for row in real if not row["program"]]
    assert missing == [], f"这些字体没有字体程序（没真内嵌）：{missing}"
    assert len({row["sha"] for row in injected}) >= 2, (
        f"注入的两个面（400 / 700）没有都进 PDF：{injected}")

    text = "".join((pdf_page.extract_text() or "") for pdf_page in PdfReader(str(pdf_path)).pages)
    assert "光合作用" in text and "暗反应" in text
    # 思源黑体把 U+0001 当空格 ⇒ 文本层里空格绝不能变成控制符（copy/搜索都要正常）
    assert " " in text and "\x01" not in text, repr(text[:120])
    assert "file://" not in text                               # 本机路径不进客户 PDF


# ---------------------------------------------------------------------------
# ④ 出包路径**真的**注入了打印字体（任务 97：补 96 的护栏缺口）
# ---------------------------------------------------------------------------
#
# 96 那几条只证明了「**注入了就有效**」：真打印那条断言是**测试自己**先调
# ``inject_print_fonts()``、再把结果交给 ``print_document()`` 的。所以 DSH 把出包路径里
# 两处 ``inject_print_fonts(...)`` 删掉之后，断言**全绿** —— 将来谁删了那两行，门禁不会响，
# 等于悄悄退回「Type3 轮廓 + 体积 2.27 倍」。这里改成**真跑一次出包路径**（不 mock 注入函数），
# 对产出的**每一份** PDF 钉结果；讲义走 ``emit_lecture``、教案 / 学案走 ``emit_document``，
# 删掉任一处注入本用例都会红。

_LECTURE_MD = """## 光合作用的过程

### 引入
- 阳光下的叶片会悄悄把空气中的二氧化碳变成糖（来源: 光合作用导论 #1）

### 核心讲解
- **光反应**发生在类囊体薄膜上，把光能变成 ATP（来源: 光合作用导论 #2）
- **暗反应**在叶绿体基质里把二氧化碳固定成糖

| 阶段 | 场所 | 产物 |
| --- | --- | --- |
| 光反应 | 类囊体薄膜 | ATP 与 NADPH |
| 暗反应 | 叶绿体基质 | 三碳糖 |

```mermaid
flowchart TD
  A[光能] --> B[光反应]
  B --> C[暗反应]
```

### 小结
- 光反应供能、暗反应固碳（来源: 光合作用导论 #3）
"""


def _minimal_course(db) -> Course:
    """临时库里造最小课程：1 章 + 1 节 + 1 篇讲义（教案 / 学案由出包路径自己派生）。"""
    course = Course(title=f"护栏课-{uuid4().hex[:6]}", subject="生物", audience="初中生",
                    duration_minutes=45, status="approved")
    db.add(course)
    db.flush()                       # id 是 Python 侧默认值，flush 之后才拿得到
    outline = Outline(course_id=course.id, title="光合作用", goal="理解光反应与暗反应")
    db.add(outline)
    db.flush()
    db.add_all([
        OutlineNode(outline_id=outline.id, node_key="1", node_type="chapter",
                    title="光合作用", objective="认识光合作用", order_index=0),
        OutlineNode(outline_id=outline.id, node_key="1.1", node_type="section",
                    title="光合作用的过程", objective="掌握光反应与暗反应", order_index=1),
    ])
    db.flush()
    node = (db.query(OutlineNode)
            .filter(OutlineNode.outline_id == outline.id, OutlineNode.node_key == "1.1").one())
    db.add(ContentItem(course_id=course.id, node_id=node.id, kind="lecture",
                       title="光合作用的过程", body={"markdown": _LECTURE_MD},
                       meta={"node_key": "1.1", "sources": []}))
    db.commit()
    return course


def _assert_embedded(pdf_path: Path) -> None:
    """一份产出 PDF 的三个**结果**判据：真字体程序 / Type3 配额 / 文本层干净。"""
    rows = _font_rows(pdf_path)
    assert rows, f"{pdf_path.name} 一个字体都没有，用例本身失效"
    type3 = [row for row in rows if row["subtype"] == "/Type3"]
    real = [row for row in rows if row["subtype"] != "/Type3"]
    assert len(type3) <= _TYPE3_BUDGET, f"{pdf_path.name} 的 Type3 还剩 {len(type3)} 个：{type3}"
    not_embedded = [f"{row['name']}({row['key'] or '无字体程序'})" for row in real
                    if row["key"] != "/FontFile2"]
    assert not_embedded == [], f"{pdf_path.name} 这些非 Type3 字体没真内嵌：{not_embedded}"
    injected = [row for row in real if _INJECTED_FACE.match(row["name"])]
    assert len({row["sha"] for row in injected}) >= 2, (
        f"{pdf_path.name} 里注入的两个静态面（400 / 700）没有都进 PDF：{injected}")

    from pypdf import PdfReader

    text = "".join((page.extract_text() or "") for page in PdfReader(str(pdf_path)).pages)
    assert "光合作用" in text, repr(text[:120])
    assert "\x01" not in text, repr(text[:120])
    assert "file://" not in text, repr(text[:120])


@pytest.mark.skipif(delivery.find_edge() is None, reason="本机没有 Microsoft Edge，跳过 PDF 打印断言")
@pytest.mark.skipif(delivery.print_font_style() is None,
                    reason="本机没有静态 OFL 字体（sourcehansans.ttf / sourcehansansb.ttf）")
def test_build_delivery_really_injects_print_fonts_into_every_pdf():
    """**出包路径**自己必须注入（不 mock 注入函数）：删掉两处注入里任一处都要红。

    两条入口同时覆盖：讲义 PDF 走 ``emit_lecture``，教案 / 学案 PDF 走 ``emit_document``。
    """
    db = SessionLocal()
    try:
        course = _minimal_course(db)
        # allow_inconsistent：这个最小课程从没导出过笔记（出包前的笔记同步会拒），
        # 但本用例考的是**打印路径**，与笔记库无关。
        summary = delivery.build_delivery(db, course, pdf_engine="edge", external=True,
                                         allow_inconsistent=True)
    finally:
        db.close()

    out_dir = Path(summary["dir"])
    lecture_pdfs = sorted((out_dir / delivery.LECTURE_DIR).rglob("*.pdf"))
    doc_pdfs = sorted(path for folder in (delivery.LESSON_DIR, delivery.SHEET_DIR)
                      for path in (out_dir / folder).rglob("*.pdf"))
    assert lecture_pdfs, f"讲义 PDF 没出来，用例本身失效：{summary.get('degraded')}"
    assert doc_pdfs, f"教案 / 学案 PDF 没出来（emit_document 那条入口没被覆盖）：{summary.get('degraded')}"

    for pdf_path in lecture_pdfs + doc_pdfs:
        _assert_embedded(pdf_path)

    # 交付给客户的 HTML 仍然**不许**注入（本机路径不许出现在交付件里）
    for html_path in sorted(out_dir.rglob("*.html")):
        assert "file://" not in html_path.read_text(encoding="utf-8"), html_path
