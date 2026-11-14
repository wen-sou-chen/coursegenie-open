"""Markdown → Word(.docx) 渲染（任务 49 教案 / 任务 50 学案共用）。

只用 ``python-docx``（纯 Python、无系统依赖），支持教案/学案真正用到的子集：
标题、段落、无序/有序列表、引用、**加粗**/``代码``行内样式、Markdown 表格。
刻意**不**追求通用（不处理嵌套列表、脚注、HTML），够用且可预测。
"""
from __future__ import annotations

import io
import re
import zipfile

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.text import WD_BREAK
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.shared import Pt

CJK_BODY_FONT = "宋体"
CJK_HEAD_FONT = "黑体"
LATIN_BODY_FONT = "Times New Roman"

# 任务 59：ZIP 存的是 MS-DOS 时间戳（**2 秒精度**），python-docx 打包时取的是「生成那一刻」的墙钟
# ⇒ 同一输入隔 ≥2 秒生成两次字节就不同（门禁偶发变红）。导出统一归一化到这个固定值。
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

_RE_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_RE_UL = re.compile(r"^\s*[-*+]\s+(.*)$")
_RE_OL = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")
_RE_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_RE_HR = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
_RE_TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_RE_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_RE_WIKI = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_RE_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_RE_TOKEN = re.compile(r"(\*\*.+?\*\*|`[^`]+`|\[\[[^\]]+\]\]|\[[^\]]+\]\([^)\s]+\))")
# XML / python-docx 不接受的控制字符（\t \n \r 合法）。内容里出现它们多半是
# 「LaTeX 单反斜杠被 JSON 吃掉」的残留（\frac → U+000C），渲染前换成可见占位
# （U+2400 区的"控制图片"，如 U+240C 显示为 ␌），避免整篇 docx 直接渲染失败（任务 48w）。
_RE_XML_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def sanitize_text(text: str) -> str:
    """把 XML 不允许的控制字符换成可见占位（U+2400 + 码位），不静默丢字。"""
    return _RE_XML_CTRL.sub(lambda m: chr(0x2400 + ord(m.group(0))), str(text or ""))


def _split_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _style_runs(paragraph, text: str) -> None:
    """按行内标记切 run：**粗** / `代码` / [[双链]] / [文字](链接)。"""
    for chunk in _RE_TOKEN.split(text):
        if not chunk:
            continue
        if chunk.startswith("**") and chunk.endswith("**") and len(chunk) > 4:
            run = paragraph.add_run(chunk[2:-2])
            run.bold = True
        elif chunk.startswith("`") and chunk.endswith("`") and len(chunk) > 2:
            run = paragraph.add_run(chunk[1:-1])
            run.font.name = "Consolas"
        else:
            wiki = _RE_WIKI.fullmatch(chunk)
            link = _RE_LINK.fullmatch(chunk)
            if wiki:
                paragraph.add_run(wiki.group(2) or wiki.group(1))
            elif link:
                paragraph.add_run(link.group(1))
            else:
                paragraph.add_run(chunk)


def _set_cjk_font(style, *, latin: str, cjk: str, size: float | None = None) -> None:
    style.font.name = latin
    if size is not None:
        style.font.size = Pt(size)
    rpr = style.element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(fonts)
    fonts.set(qn("w:eastAsia"), cjk)
    fonts.set(qn("w:ascii"), latin)
    fonts.set(qn("w:hAnsi"), latin)


def _prepare_styles(doc) -> None:
    _set_cjk_font(doc.styles["Normal"], latin=LATIN_BODY_FONT, cjk=CJK_BODY_FONT, size=10.5)
    for level in range(1, 5):
        try:
            style = doc.styles[f"Heading {level}"]
        except KeyError:  # pragma: no cover —— 标准模板恒有 Heading 1..9
            continue
        style.font.color.rgb = None
        style.font.bold = True
        _set_cjk_font(style, latin=LATIN_BODY_FONT, cjk=CJK_HEAD_FONT,
                      size=max(11.0, 18.0 - 1.5 * level))


def _add_table(doc, rows: list[list[str]]) -> None:
    if not rows:
        return
    width = max(len(row) for row in rows)
    table = doc.add_table(rows=0, cols=width)
    table.style = "Table Grid"
    for index, row in enumerate(rows):
        cells = table.add_row().cells
        for col in range(width):
            text = row[col] if col < len(row) else ""
            paragraph = cells[col].paragraphs[0]
            _style_runs(paragraph, text)
            if index == 0:
                for run in paragraph.runs:
                    run.bold = True


def _add_field(paragraph, code: str) -> None:
    """插入一个 Word 域（如 ``PAGE``）：Word 打开时按真实页数求值。"""
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = f" {code} "
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    for node in (begin, instr, end):
        run._r.append(node)


def _add_footer(doc, footer_text: str) -> None:
    paragraph = doc.sections[0].footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.add_run("第 ")
    _add_field(paragraph, "PAGE")
    paragraph.add_run(" 页")
    if footer_text:
        paragraph.add_run(f"　{footer_text}")


def _normalize_zip_timestamps(data: bytes) -> bytes:
    """把 .docx（ZIP）里每个条目的时间戳固定成 ``ZIP_EPOCH``，让导出字节确定（任务 59）。

    只改 ``date_time``：条目顺序、压缩方式、外部属性与其它字段原样保留；
    正文（styles/document/footer 等 XML 部件）一个字节都不动。
    """
    with zipfile.ZipFile(io.BytesIO(data)) as source:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as target:
            for info in source.infolist():
                info.date_time = ZIP_EPOCH
                target.writestr(info, source.read(info.filename))
        return buffer.getvalue()


def markdown_to_docx(markdown: str, *, title: str = "", page_numbers: bool = False,
                     footer_text: str = "") -> bytes:
    """把 Markdown 渲染成 .docx 字节流（同一输入**恒得同一字节**，不含时间戳）。

    ``page_numbers=True`` 时在页脚加「第 N 页」（Word 的 ``PAGE`` 域，学案要按页发）。
    返回前会把 ZIP 条目的墙钟时间戳归一化成 ``ZIP_EPOCH``（任务 59）——
    否则 python-docx 写包时带上「生成那一刻」的 MS-DOS 时间戳（2 秒精度），
    同一输入隔几秒生成两次字节就不同。
    """
    doc = Document()
    _prepare_styles(doc)
    markdown = sanitize_text(markdown)
    title = sanitize_text(title)
    footer_text = sanitize_text(footer_text)
    if page_numbers:
        _add_footer(doc, footer_text)
    if title:
        doc.add_heading(title, level=1)
    lines = str(markdown or "").split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        if _RE_TABLE_ROW.match(line) and (index + 1 < len(lines)
                                          and _RE_TABLE_SEP.match(lines[index + 1])):
            rows = [_split_row(line)]
            index += 2
            while index < len(lines) and _RE_TABLE_ROW.match(lines[index]):
                rows.append(_split_row(lines[index]))
                index += 1
            _add_table(doc, rows)
            continue
        heading = _RE_HEADING.match(line)
        paragraph = None
        if heading:
            level = min(4, len(heading.group(1)))
            if heading.group(2).strip():
                doc.add_heading(heading.group(2).strip(), level=level)
        elif _RE_HR.match(line):
            doc.add_paragraph().add_run().add_break(WD_BREAK.LINE)
        elif _RE_UL.match(line):
            paragraph = doc.add_paragraph(style="List Bullet")
            _style_runs(paragraph, _RE_UL.match(line).group(1))
        elif _RE_OL.match(line):
            paragraph = doc.add_paragraph(style="List Number")
            _style_runs(paragraph, _RE_OL.match(line).group(2))
        elif _RE_QUOTE.match(line):
            paragraph = doc.add_paragraph(style="Intense Quote")
            _style_runs(paragraph, _RE_QUOTE.match(line).group(1))
        elif line.strip():
            paragraph = doc.add_paragraph()
            _style_runs(paragraph, line.strip())
        index += 1
    buffer = io.BytesIO()
    doc.save(buffer)
    return _normalize_zip_timestamps(buffer.getvalue())


def docx_text(data: bytes) -> str:
    """读回 .docx 的全部文字（表格逐格、段落逐行），用于测试与自检。"""
    doc = Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            parts.append("\t".join(cell.text for cell in row.cells))
    return "\n".join(parts)


def docx_footer_text(data: bytes) -> str:
    """读回 .docx 页脚文字（含 ``PAGE`` 域占位），用于断言页码存在。"""
    doc = Document(io.BytesIO(data))
    return "\n".join(p.text for p in doc.sections[0].footer.paragraphs).strip()
