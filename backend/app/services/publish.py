"""发布部署：HTML5 课程包 / SCORM 结构导出 + 平台适配器接口。

任务 48m（2026-09-17）：导出包从「只有课程首页 + 每章目标」升级为**真能交付**的形态
—— 每个课时一个页面（讲义正文 + 「怎么读」图例 + 「## 来源」小节 + 表格 / mermaid / 图片），
图表以行内 SVG / 数据表落地（页面里不留悬空引用），mermaid 运行时随包（相对路径）。
SCORM 1.2 的 ``imsmanifest.xml`` 每课时一个 ``<item>`` + ``<resource>``。
Markdown 渲染复用 ``services.delivery``（同一套渲染，不写第二套），
因此离线包与交付包（八件套）的图例 / 来源小节口径同源。
"""
from __future__ import annotations

import html
import json
import math
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.course import ContentItem, Course
from app.services import delivery as delivery_mod
from app.services import kb as kb_mod
from app.services import lecture_qc
from app.services import md_tables
from app.services.recording_kit import CHARS_PER_SECOND
from app.services.planner import list_nodes

_RE_WS = re.compile(r"\s+")

ASSETS_DIR = "assets"
MERMAID_REL = f"{ASSETS_DIR}/mermaid.min.js"
LESSON_MISSING = "本课时尚未生成讲义"

MANIFEST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<manifest identifier="{course_id}" version="1.0" xmlns="http://www.imsglobal.org/xsd/imscp_v1p1"
 xmlns:adlcp="http://www.adlnet.org/xsd/adlcp_v1p3">
  <metadata><schema>ADL SCORM</schema><schemaversion>1.2</schemaversion></metadata>
  <organizations default="org1">
    <organization identifier="org1">
      <title>{title}</title>
      {items}
    </organization>
  </organizations>
  <resources>
{resources}
  </resources>
</manifest>"""


# ---------------------------------------------------------------------------
# 采集：这一门课有哪些课时、每课时有没有讲义
# ---------------------------------------------------------------------------

def _course_items(db: Session, course: Course) -> list[ContentItem]:
    return (db.query(ContentItem)
            .filter(ContentItem.course_id == course.id)
            .order_by(ContentItem.created_at).all())


def _lesson_rows(db: Session, course: Course, items: list[ContentItem]) -> list[dict]:
    """每个**课时**一行：``{key, stem, file, title, node, chapter_title, chapter_no, item}``。

    ``item`` 是该课时的讲义条目（没有则 ``None`` —— 页面显式降级，不许静默少页）。
    同一课时有多篇讲义时取最后生成的那篇（与交付包「一课时一页」口径一致）。
    """
    all_nodes = list_nodes(db, course.id)
    chapters = {str(node.node_key or ""): node for node in all_nodes
                if "." not in str(node.node_key or "")}
    nodes = [node for node in all_nodes if "." in str(node.node_key or "")]
    nodes.sort(key=lambda node: delivery_mod._key_order(str(node.node_key or "")))
    lectures: dict[str, ContentItem] = {}
    for item in items:
        if item.kind == "lecture" and item.node_id:
            lectures[item.node_id] = item
    chapter_no: dict[str, int] = {}
    rows: list[dict] = []
    for node in nodes:
        key = str(node.node_key or "")
        chapter_key = key.split(".")[0]
        chapter_no.setdefault(chapter_key, len(chapter_no) + 1)
        serial = chapter_no[chapter_key]
        stem = f"{serial:02d}-{key}-{kb_mod._slug(node.title or '', 40)}"
        rows.append({
            "key": key,
            "stem": stem,
            "file": f"{stem}.html",
            "title": f"{key} {node.title or ''}".strip(),
            "node": node,
            "chapter_title": getattr(chapters.get(chapter_key), "title", "") or "未分章",
            "chapter_no": serial,
            "item": lectures.get(node.id),
        })
    return rows


def _figure_resolver(items: list[ContentItem]):
    """``diagram://id`` / ``chart://id`` → 行内 SVG / 数据表（取数口径与交付包一致）。"""
    figures = {item.id: item for item in items if item.kind in delivery_mod.FIGURE_KINDS}

    def resolve_image(url: str) -> str | None:
        kind, _, ident = str(url or "").partition("://")
        item = figures.get(ident)
        if item is None:
            return None
        if kind == "chart" and item.kind == "chart_table":
            table = str((item.body or {}).get("markdown_table") or "").strip()
            rows = [line.strip().strip("|").split("|")
                    for line in table.splitlines() if line.strip()]
            if len(rows) >= 2:
                head = "".join(f"<th>{delivery_mod._inline_html(cell.strip())}</th>"
                               for cell in rows[0])
                body = "".join("<tr>" + "".join(
                    f"<td>{delivery_mod._inline_html(cell.strip())}</td>" for cell in row)
                    + "</tr>" for row in rows[2:])
                return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
        svg_src = delivery_mod._svg_source_of(item)
        if svg_src is None:
            return None
        text = svg_src.read_text(encoding="utf-8", errors="ignore")
        return re.sub(r"<\?xml[^>]*\?>", "", text).strip()

    return resolve_image


def _asset_bytes() -> dict[str, bytes]:
    """随包资源：本地 mermaid 运行时（没装则不写，页面里的流程图退化为源码块）。"""
    src = delivery_mod.find_mermaid_js()
    return {MERMAID_REL: src.read_bytes()} if src is not None else {}


def _course_head(course: Course, lesson_count: int) -> str:
    return (f"受众：{course.audience} · 计划时长：{course.duration_minutes} 分钟 · "
            f"共 {lesson_count} 课时")


def _index_html(rows: list[dict], pages: list[dict]) -> str:
    """目录页：章节 → 课时链接（顺序与讲义一致；缺讲义的课时显式标注）。"""
    parts: list[str] = []
    serial = 0
    for row, page in zip(rows, pages):
        if row["chapter_no"] != serial:
            if serial:
                parts.append("</ul>")
            serial = row["chapter_no"]
            parts.append(f"<h2>第 {serial} 章 · {html.escape(row['chapter_title'])}</h2>")
            parts.append("<ul>")
        note = "" if row["item"] is not None else f"（{LESSON_MISSING}）"
        parts.append(f'<li><a href="{html.escape(page["file"], quote=True)}">'
                     f'{html.escape(page["title"])}</a>{note}</li>')
    if serial:
        parts.append("</ul>")
    else:
        parts.append("<p>本课程还没有课时：请先在「大纲」里生成并批准大纲。</p>")
    return "\n".join(parts)


def build_pages(db: Session, course: Course) -> dict:
    """打包前的通用产物：``{rows, pages, index, assets}``（HTML / SCORM 两种包共用）。

    ``pages`` 每课时一页，正文 = ``delivery`` 的交付讲义 markdown（讲义缺失时显式降级）；
    ``assets`` 是 ``{包内相对路径: 字节}``。两种包都从这里取，口径不会漂移。
    """
    items = _course_items(db, course)
    rows = _lesson_rows(db, course, items)
    resolve_image = _figure_resolver(items)
    # 任务 64：与交付包同一套去重 —— 正文已有同数据表格时不再渲染该数据卡
    chart_markdowns = delivery_mod.chart_table_markdowns(items)
    mermaid_js = MERMAID_REL if delivery_mod.find_mermaid_js() is not None else None
    pages: list[dict] = []
    for row in rows:
        item = row["item"]
        markdown = (delivery_mod._delivery_lecture_markdown(item)
                    if item is not None else None)
        if markdown:
            markdown, _skipped = md_tables.strip_duplicate_chart_refs(
                markdown, chart_markdowns)
        body = (delivery_mod.markdown_to_html(markdown, resolve_image=resolve_image)
                if markdown else f'<p class="lesson-missing">{LESSON_MISSING}</p>')
        body += '\n<p class="course-nav"><a href="index.html">返回目录</a></p>'
        subtitle = f"{course.title} · {row['chapter_title']} · {row['key']}"
        pages.append({
            "file": row["file"],
            "title": row["title"],
            "html": delivery_mod.html_page(row["title"], body, subtitle=subtitle,
                                           mermaid_js=mermaid_js),
        })
    index = delivery_mod.html_page(course.title, _index_html(rows, pages),
                                   subtitle=_course_head(course, len(rows)))
    return {"rows": rows, "pages": pages, "index": index, "assets": _asset_bytes()}


# ---------------------------------------------------------------------------
# 导出：HTML5 课程包 / SCORM 1.2 包
# ---------------------------------------------------------------------------

def export_html_package(db: Session, course: Course) -> Path:
    """导出 HTML5 课程包（zip：目录页 + 每课时一页 + 随包资源 + manifest.json）。"""
    settings = get_settings()
    settings.export_dir.mkdir(parents=True, exist_ok=True)
    out = settings.export_dir / f"{course.id}_html.zip"
    bundle = build_pages(db, course)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.html", bundle["index"])
        for page in bundle["pages"]:
            zf.writestr(page["file"], page["html"])
        for rel, data in sorted(bundle["assets"].items()):
            zf.writestr(rel, data)
        zf.writestr("manifest.json", _manifest_json(course, bundle))
    return out


def lesson_seconds(markdown: str) -> int:
    """课时**预计时长**（秒）：讲义正文字数 ÷ 4.5 字/秒，向上取整。

    与录课包 ``时长表.md`` 同一口径（同一常量 ``recording_kit.CHARS_PER_SECOND``）——
    同一个数在两处出现，不各写一份。
    """
    return int(math.ceil(len(_RE_WS.sub("", str(markdown or ""))) / CHARS_PER_SECOND))


def _hms(seconds: int) -> str:
    """秒 → SCORM 1.2 的 ``HH:MM:SS``（``adlcp:maxtimeallowed`` 的格式要求）。"""
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _scorm_items(db: Session, course: Course, bundle: dict) -> list[str]:
    """每课时一个 ``<item>``：标题 + **预计时长** + **知识点 key**。

    SCORM 1.2 没有「预计时长」字段，最贴近的是 ``adlcp:maxtimeallowed``（限时，HH:MM:SS）
    —— 这里用它承载「预计时长」；知识点 key 走 ``adlcp:datafromlms``（LMS 会原样传给 SCO 的
    启动数据，是这门课在平台侧定位知识点的合法落点）。顺序按 SCORM 1.2 的 ADL 扩展序列
    （datafromlms → maxtimeallowed）。
    """
    items: list[str] = []
    for number, (row, page) in enumerate(zip(bundle["rows"], bundle["pages"]), start=1):
        item = row["item"]
        markdown = lecture_qc.lecture_markdown(item) if item is not None else ""
        lines = [f'<item identifier="item{number}" identifierref="res{number}">',
                 f'<title>{html.escape(page["title"])}</title>',
                 f'<adlcp:datafromlms>node_key={html.escape(str(row["key"]), quote=True)}</adlcp:datafromlms>']
        if markdown:
            lines.append(f'<adlcp:maxtimeallowed>{_hms(lesson_seconds(markdown))}</adlcp:maxtimeallowed>')
        lines.append("</item>")
        items.append("".join(lines))
    return items


def scorm_selfcheck(manifest: str, names: list[str], sizes: dict[str, int]) -> dict:
    """导出前的自检：``imsmanifest.xml`` 可解析 / item 指向的资源都在 / 文件齐 / 总大小。

    只做「这份包能不能被 LMS 读」的机器判定；不通过时**不落盘**（宁可报错，也不交一份坏包）。
    """
    problems: list[str] = []
    root: ElementTree.Element | None = None
    try:
        root = ElementTree.fromstring(manifest)
    except ElementTree.ParseError as exc:
        problems.append(f"imsmanifest.xml 无法解析：{exc}")
    items = references = 0
    if root is not None:
        ns = {"cp": "http://www.imsglobal.org/xsd/imscp_v1p1"}
        item_nodes = root.findall(".//cp:item", ns)
        items = len(item_nodes)
        resource_ids = {str(node.get("identifier") or "")
                        for node in root.findall(".//cp:resource", ns)}
        references = len(resource_ids)
        for node in item_nodes:
            ref = str(node.get("identifierref") or "")
            if ref and ref not in resource_ids:
                problems.append(f"item {node.get('identifier')} 指向未声明的资源 {ref}")
        for node in root.findall(".//cp:file", ns):
            href = str(node.get("href") or "")
            if href and href not in names:
                problems.append(f"资源声明的文件不在包里：{href}")
    return {"ok": not problems, "items": items, "resources": references,
            "files": len(names), "total_bytes": int(sum(sizes.values())),
            "problems": problems}


def export_scorm_package_with_report(db: Session, course: Course) -> tuple[Path, dict]:
    """导出 SCORM 1.2 包：每课时一个 ``<item>``（带标题 / 预计时长 / 知识点 key）+ ``<resource>``，
    **写盘前先自检**（XML 可解析、item 指向的资源都在、声明文件都在包里、总大小）。

    ``index.html``（目录页）作为单独的 ``res-index`` 资源打进包、不占 ``<item>``，
    课时页里都有「返回目录」链接 —— LMS 里从任一课时都能回到目录。
    自检不通过 ⇒ 抛 ``ValueError``（不落盘），绝不静默交一份读不进去的包。
    """
    settings = get_settings()
    settings.export_dir.mkdir(parents=True, exist_ok=True)
    out = settings.export_dir / f"{course.id}_scorm.zip"
    bundle = build_pages(db, course)
    items = _scorm_items(db, course, bundle)
    resources: list[str] = []
    for number, page in enumerate(bundle["pages"], start=1):
        href = html.escape(page["file"], quote=True)
        resources.append(f'<resource identifier="res{number}" type="webcontent" '
                         f'adlcp:scormtype="sco" href="{href}">'
                         f'<file href="{href}"/></resource>')
    index_files = ["index.html", *sorted(bundle["assets"])]
    resources.append('<resource identifier="res-index" type="webcontent" '
                     'adlcp:scormtype="asset" href="index.html">'
                     + "".join(f'<file href="{html.escape(rel, quote=True)}"/>'
                               for rel in index_files)
                     + "</resource>")
    manifest = MANIFEST_TEMPLATE.format(
        course_id=html.escape(course.id, quote=True), title=html.escape(course.title),
        items="\n      ".join(items),
        resources="\n".join("    " + row for row in resources))
    payload: list[tuple[str, bytes]] = [("imsmanifest.xml", manifest.encode("utf-8")),
                                        ("index.html", bundle["index"].encode("utf-8"))]
    payload += [(page["file"], page["html"].encode("utf-8")) for page in bundle["pages"]]
    payload += [(rel, data) for rel, data in sorted(bundle["assets"].items())]
    names = [name for name, _ in payload]
    sizes = {name: len(data) for name, data in payload}
    report = scorm_selfcheck(manifest, names, sizes)
    if not report["ok"]:
        raise ValueError("SCORM 自检未通过，未写盘：" + "；".join(report["problems"][:3]))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in payload:
            zf.writestr(name, data)
    report["file"] = out.name
    report["lessons"] = len(bundle["pages"])
    report["estimated_seconds"] = sum(
        lesson_seconds(lecture_qc.lecture_markdown(row["item"]))
        for row in bundle["rows"] if row["item"] is not None)
    return out, report


def export_scorm_package(db: Session, course: Course) -> Path:
    """导出 SCORM 1.2 包（返回路径；自检报告见 :func:`export_scorm_package_with_report`）。"""
    return export_scorm_package_with_report(db, course)[0]


def export_notes_zip(
    db: Session,
    course: Course,
    scope: str = "all",
    node_ids: list[str] | None = None,
) -> Path:
    """把课程笔记打包为 ZIP：scope=all 整门课；scope=nodes 只打所选知识点。

    all：先按当前内容重新导出（no-clobber）再压缩整个课程目录；
    nodes：直接按所选节点收集 知识点页 + assets 配图 + 习题页 + Excalidraw。
    """
    from app.services.kb import collect_node_notes, export_course as export_course_kb

    settings = get_settings()
    settings.export_dir.mkdir(parents=True, exist_ok=True)
    if scope == "all":
        result = export_course_kb(db, course, force=False)
        base = Path(result["vault_root"]) / result["output_rel"]
        if not base.is_dir():
            raise ValueError("课程笔记目录不存在，请先导出")
        out = settings.export_dir / f"{course.id}_notes_all.zip"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in sorted(base.rglob("*")):
                if file.is_file():
                    zf.write(file, file.relative_to(base).as_posix())
        return out

    entries = collect_node_notes(db, course, node_ids or [])
    if not entries:
        raise ValueError("所选节点没有可导出的笔记，请先为该节点生成内容或讲义")
    out = settings.export_dir / f"{course.id}_notes_nodes.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, data in entries:
            zf.writestr(arcname.replace("\\", "/"), data)
    return out


def _manifest_json(course: Course, bundle: dict | None = None) -> str:
    """包清单：课程信息 + 逐课时的页面与讲义状态（便于客户与我们逐项核对）。"""
    data: dict = {
        "title": course.title,
        "subject": course.subject,
        "audience": course.audience,
        "duration_minutes": course.duration_minutes,
    }
    if bundle is not None:
        data["lessons"] = [
            {"key": row["key"], "title": row["title"],
             "file": page["file"], "has_lecture": row["item"] is not None}
            for row, page in zip(bundle["rows"], bundle["pages"])
        ]
    return json.dumps(data, ensure_ascii=False, indent=2)


class PlatformAdapter:
    """平台适配器接口：子类实现 publish()。MVP 阶段以示例/桩实现为主。"""

    name = "base"

    def publish(self, course: Course, payload: dict) -> dict:
        raise NotImplementedError


class TeachableAdapter(PlatformAdapter):
    """Teachable 示例适配器：需要真实 API Key 与课程数据，未对真实实例验证。"""

    name = "teachable"

    def publish(self, course: Course, payload: dict) -> dict:
        api_key = payload.get("api_key") or ""
        if not api_key:
            return {"ok": False, "message": "缺少 Teachable API Key（TEACHABLE_API_KEY）"}
        # 真实实现：POST /v1/courses + /v1/lectures 上传内容
        return {
            "ok": True,
            "message": "Teachable 适配器已就绪（示例）：真实发布需配置 API Key 与课程映射。",
            "platform": self.name,
            "course_id": course.id,
        }


ADAPTERS: dict[str, PlatformAdapter] = {"teachable": TeachableAdapter()}


def publish(db: Session, course: Course, platform: str, payload: dict) -> dict:
    adapter = ADAPTERS.get(platform)
    if adapter is None:
        return {"ok": False, "message": f"未支持的平台：{platform}（可用：{', '.join(ADAPTERS)}）"}
    return adapter.publish(course, payload)
