"""任务 88：交付包里的**单文件 HTML 课件**（**0 LLM**，纯本地渲染）。

**它是什么**：`课件/HTML/{deck 标题}.html` —— 每个课件（整课 + 各章）一份**能投影、
能打印、能手机上看的网页版课件**。用户 2026-09-22 拍板「交付包加一份 HTML 课件」，
样板（`AI协作区\\样板-HTML课件\\` v8 规整风）已定稿；本模块把样板变成产品能力。

**数据来源（只读，绝不重跑生成）**：课件自己的页数据 —— `recording_kit.deck_pages(deck)`
（页序 / 标题 / 要点 / 备注 / 版式族，取自 `meta.slides` + `meta.layouts`），配上该课
**已在库里的图**（`图表/` 的 SVG 矢量源 / 数据卡表格）。**不重跑 PPT、不写 pptx、不调模型**。

**单文件口径**（探针 H2 逐字查）：CSS / JS 全内联；图片一律内联成 **base64 data URI** ——
因此 HTML 源码里**一个 `http(s)://` 都没有**（连 SVG 自带的
`xmlns="http://www.w3.org/2000/svg"` 都因为 base64 而看不见）⇒ 断网可开、可拷 U 盘。

**三端口径（同一张画布等比缩放，版式三端一致）**：`1122×793px` = A4 横版 297×210mm @96dpi
的设计画布 —— 投影（视口多大就缩放多大）/ 打印（`@page { size: A4 landscape }`，一页一张
幻灯片）/ 手机（等比缩到 375px 宽以内，不横向溢出）。

**视觉口径照样板 v8 规整风**：眉标 27px / 主句 54px / 全页最小 14px / 页边距 36–118px；
入场动画**默认播一次**（**不**跟随系统「减少动画」禁用，只提示一句）；点图放大；页脚带课程名与页码。
"""
from __future__ import annotations

import base64
import html as html_mod
import re

from sqlalchemy.orm import Session

from app.models.course import ContentItem, Course
from app.services import md_tables
from app.services import ppt as ppt_mod
from app.services import recording_kit
from app.services.planner import list_nodes

#: 交付包里的目录名（`课件/HTML/`；探针按这个位置逐字找文件）。
DIR_NAME = "HTML"

#: 设计画布 = A4 横版（297×210mm）@96dpi。三端共用一块画布，只是缩放比不同。
CANVAS_W, CANVAS_H = 1122, 793
#: 页边距（样板口径 36–118px）：左右 112、上 60、下 36。
PAD_X, PAD_TOP, PAD_BOTTOM = 112, 60, 36
#: 字号（样板口径）：眉标 27 / 主句 54 / 全页最小 14。
FS_EYEBROW, FS_HEADLINE, FS_MIN = 27, 54, 14
#: 每页最多放几张图（与 `ppt.MAX_FIGURES_PER_SLIDE` 同口径，避免一页挤满）。
MAX_PAGE_FIGURES = 2
#: 数据卡表格最多显示多少行（超出在表下写明「共 N 行」——不静默截断）。
MAX_TABLE_ROWS = 8
#: 入场动效（样板口径）：单元素 1.05s 缓出、步进 170ms、起步先静 180ms。
ANIM_SECONDS = 1.05
ANIM_STEP_MS = 170
ANIM_BASE_MS = 180
#: 版式族 → 眉标后缀（页数据里的 `layout`，未知一律按「知识点」处理）。
LAYOUT_LABEL = {
    "cover": "课件首页",
    "goals": "课程目标",
    "toc": "课程目录",
    "chapter": "本章概览",
    "point": "知识点",
    "review": "本章回顾",
    "quiz": "随堂练习",
    "questions": "课堂提问",
    "end": "总结与下一步",
}
#: 文件名里不许出现的字符（Windows）——只有真的出现才替换，正常标题一字不改。
_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RE_WS = re.compile(r"\s+")
_RE_CONT = re.compile(r"\s*（续）\s*$")
_RE_LEAD_NUM = re.compile(r"^\s*(\d+)\s")


# ---------------------------------------------------------------------------
# 1. 文件与页块
# ---------------------------------------------------------------------------

def html_name(deck: ContentItem) -> str:
    """`{deck 标题}.html` —— 标题逐字照用（只把 Windows 非法字符换成下划线）。"""
    title = str(getattr(deck, "title", "") or "").strip() or "课件"
    safe = _BAD_NAME.sub("_", title).strip().rstrip(".")
    return (safe or "课件") + ".html"


def _norm(text: object) -> str:
    """标题比对用归一化：去空白、去「（续）」（与 `recording_kit` 同一口径）。"""
    return _RE_CONT.sub("", _RE_WS.sub("", str(text or "")))


def _node_index(nodes: list) -> tuple[dict[str, object], dict[str, object]]:
    """`(标题 → 节点, 节点 key → 节点)`；章页标题形如「1 发酵原理与微生物基础」也算命中。"""
    by_title: dict[str, object] = {}
    by_key: dict[str, object] = {}
    for node in nodes:
        key = str(getattr(node, "node_key", "") or "")
        title = str(getattr(node, "title", "") or "")
        by_key[key] = node
        for candidate in (title, f"{key} {title}".strip(), key):
            name = _norm(candidate)
            if name:
                by_title.setdefault(name, node)
    return by_title, by_key


def _node_of(page: dict, layout: str, by_title: dict, by_key: dict) -> object | None:
    """页 → 大纲节点：先按标题逐字比；章页再按标题开头的章号兜底。"""
    node = by_title.get(_norm(page.get("title")))
    if node is not None:
        return node
    if layout == "chapter":
        match = _RE_LEAD_NUM.match(str(page.get("title") or ""))
        if match:
            return by_key.get(match.group(1))
    return None


def _members_of_chapter(nodes: list) -> dict[str, list]:
    """`{章 key: [成员节点, ...]}`（成员 = key 形如 `1.1` 的节点，按大纲顺序）。"""
    members: dict[str, list] = {}
    for node in nodes:
        key = str(getattr(node, "node_key", "") or "")
        if "." in key:
            members.setdefault(key.split(".")[0], []).append(node)
    return members


def figure_index(figures: list[dict] | None) -> dict[str, list[dict]]:
    """`[{node_key, caption, kind, svg, table}]` → `{node_key: [图, ...]}`（保序）。"""
    index: dict[str, list[dict]] = {}
    for row in figures or []:
        key = str(row.get("node_key") or "").strip()
        if not key:
            continue
        index.setdefault(key, []).append(dict(row))
    return index


def _pool_of(node, index: dict[str, list[dict]], members: dict[str, list]) -> list[dict]:
    """该节点可用的图：先自己的，再（章节点时）本章各知识点的。"""
    key = str(getattr(node, "node_key", "") or "")
    pool = list(index.get(key) or [])
    for member in members.get(key, []):
        pool += list(index.get(str(getattr(member, "node_key", "") or "")) or [])
    return pool


def page_blocks(deck: ContentItem, pages: list[dict], nodes: list,
                index: dict[str, list[dict]]) -> list[dict]:
    """页 → 渲染块（标题 / 要点 / 备注 / 版式族 / 图）——页数、顺序与 pptx 完全一致。

    同一个知识点有多页（第 2 页起标题带「（续）」）时，图**按页顺次分配**（每页 ≤2 张），
    与 `ppt._point_slides` 同一口径：续页不再重复第 1 页的图。
    """
    by_title, by_key = _node_index(nodes)
    members = _members_of_chapter(nodes)
    used: dict[str, int] = {}
    blocks: list[dict] = []
    for offset, page in enumerate(pages):
        layout = str(page.get("layout") or "").strip() or "point"
        node = _node_of(page, layout, by_title, by_key)
        figures: list[dict] = []
        if node is not None:
            key = str(getattr(node, "node_key", "") or "")
            pool = _pool_of(node, index, members)
            start = used.get(key, 0)
            figures = pool[start:start + MAX_PAGE_FIGURES]
            used[key] = start + len(figures)
        blocks.append({
            "index": int(page.get("index") or offset + 1),
            "layout": layout,
            "title": str(page.get("title") or "").strip(),
            "bullets": [str(b).strip() for b in (page.get("bullets") or []) if str(b).strip()],
            "spoken": str(page.get("spoken") or "").strip(),
            "internal": str(page.get("internal") or "").strip(),
            "figures": figures,
        })
    return blocks


# ---------------------------------------------------------------------------
# 2. 渲染
# ---------------------------------------------------------------------------

def _esc(text: object) -> str:
    return html_mod.escape(str(text or ""), quote=True)


_RE_BOLD = re.compile(r"\*\*(.+?)\*\*|__([^_]+)__")
_RE_MARK = re.compile(r"(?:(?<=\S)|^)\*{1,3}(?=\S)")


def _inline(text: object) -> str:
    """行内标记 → HTML（**粗体** / __粗体__ ⇒ `<strong>`）。

    口径：**页数据一个字不改**（`meta.slides` / pptx / 讲义都不动），只在这一层把
    行内**强调标记**按 markdown 的读法渲染出来 —— 与网页端预览、讲义 HTML 同一读法。
    背景：pptx 正文里带标记的要点不算少（真库 609 条要点里 150 条含 `**`），
    ppt 管线原样落字；HTML 课件若也照抄，客户看到的是 `**糖解途径**`。
    残留的**不平衡**标记符（如 `*解析**`）一并去掉：它是标记碎片，不是正文。
    """
    out = _esc(text)
    for _ in range(2):  # 嵌套一次（***x*** 这类）足够，不做多层
        out = _RE_BOLD.sub(lambda m: "<strong>" + (m.group(1) or m.group(2)) + "</strong>", out)
    return _RE_MARK.sub("", out)


def _data_uri(path) -> str:
    """SVG 源 → base64 data URI（**不落任何 http 字样**，断网可开）。"""
    data = path.read_bytes() if hasattr(path, "read_bytes") else bytes(path)
    return "data:image/svg+xml;base64," + base64.b64encode(data).decode("ascii")


def _table_of(markdown: str) -> tuple[list[str], list[list[str]]] | None:
    """数据卡表格：复用 `md_tables.parse_tables`（全产品唯一一份表格口径）。"""
    tables = md_tables.parse_tables(str(markdown or ""))
    return tables[0] if tables else None


def _figure_html(row: dict) -> str:
    """一张图：有 SVG 源就内联（点开放大），只有数据卡就渲染成表格。"""
    caption = _inline(row.get("caption") or "图")
    svg = row.get("svg")
    if svg is not None:
        return ('<figure class="fig anim">'
                f'<img class="fig-img" src="{_data_uri(svg)}" alt="{caption}" tabindex="0">'
                f'<figcaption>{caption}</figcaption></figure>')
    table = _table_of(row.get("table") or "")
    if table:
        head, body = table
        rows = "".join("<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in line) + "</tr>"
                       for line in body[:MAX_TABLE_ROWS])
        more = (f'<p class="fig-more">共 {len(body)} 行，本页显示前 {MAX_TABLE_ROWS} 行'
                f'（完整表格见 `图表/表格/`）</p>' if len(body) > MAX_TABLE_ROWS else "")
        return ('<figure class="fig anim fig-table">'
                '<table><thead><tr>' + "".join(f"<th>{_inline(c)}</th>" for c in head)
                + "</tr></thead>" + f"<tbody>{rows}</tbody></table>{more}"
                + f"<figcaption>{caption}</figcaption></figure>")
    return ""


def _slide_html(course: Course, deck: ContentItem, block: dict, total: int) -> str:
    deck_label = str(getattr(deck, "title", "") or "").replace("· 课件", "").strip(" ·")
    label = LAYOUT_LABEL.get(block["layout"], "知识点")
    eyebrow = deck_label if block["layout"] == "cover" else f"{deck_label} · {label}"
    points = "".join(f'<li class="anim" style="--step:{3 + i}">{_inline(text)}</li>'
                     for i, text in enumerate(block["bullets"]))
    figures = "".join(_figure_html(row) for row in block["figures"])
    notes: list[str] = []
    if block["spoken"]:
        notes.append('<p class="note-head">本页讲稿摘要（课件演讲备注节选）</p>'
                     f'<p class="note-body">{_inline(block["spoken"]).replace(chr(10), "<br>")}</p>')
    if block["internal"]:
        notes.append('<p class="note-head note-warn">讲师提示（勿投屏）</p>'
                     f'<p class="note-body">{_inline(block["internal"]).replace(chr(10), "<br>")}</p>')
    aside = f'<aside class="notes">{"".join(notes)}</aside>' if notes else ""
    return (f'<section class="slide" id="p{block["index"]}" data-layout="{_esc(block["layout"])}"'
            f' data-n="{block["index"]}">'
            f'<p class="eyebrow anim" style="--step:0">{_esc(eyebrow)}</p>'
            f'<h1 class="headline anim" style="--step:1">{_inline(block["title"])}</h1>'
            + (f'<ul class="points">{points}</ul>' if points else "")
            + (f'<div class="figures">{figures}</div>' if figures else "")
            + '<footer class="foot">'
            + f'<span>{_esc(course.title)}</span>'
            + f'<span class="page">{block["index"]} / {total}</span></footer>'
            + aside + "</section>")


def _toc_items(blocks: list[dict]) -> str:
    return "".join(f'<li><a href="#p{b["index"]}">{b["index"]}. {_inline(b["title"])}</a></li>'
                   for b in blocks)


def render_html(course: Course, deck: ContentItem, blocks: list[dict]) -> str:
    """一份 deck → 单文件 HTML（CSS / JS 内联，图 base64）。"""
    if not blocks:  # 老课件没有页数据：写一份诚实的占位页，绝不静默少文件
        blocks = [{"index": 1, "layout": "cover", "title": str(deck.title or "课件"),
                   "bullets": ["本课件还没有页数据：在「高级工具 → PPT」重新生成课件后，"
                               "重新出包即可看到网页版。"],
                   "spoken": "", "internal": "", "figures": []}]
    total = len(blocks)
    slides = "".join(_slide_html(course, deck, block, total) for block in blocks)
    return (_SKELETON
            .replace("<!--TITLE-->", _esc(f"{deck.title} · HTML 课件"))
            .replace("<!--CSS-->", _CSS)
            .replace("<!--SLIDES-->", slides)
            .replace("<!--TOC-->", _toc_items(blocks))
            .replace("<!--TOTAL-->", str(total))
            .replace("<!--JS-->", _JS))


def _render_row(course: Course, deck: ContentItem, nodes: list, index: dict) -> dict:
    """单个 deck → 一行 `{name, text, deck_id, deck_title, pages, bytes}`（唯一渲染路径）。"""
    pages = recording_kit.deck_pages(deck)
    text = render_html(course, deck, page_blocks(deck, pages, nodes, index))
    return {
        "name": html_name(deck),
        "text": text,
        "deck_id": str(deck.id),
        "deck_title": str(deck.title or ""),
        "pages": len(pages),
        "bytes": len(text.encode("utf-8")),
    }


def render_deck(db: Session, course: Course, deck: ContentItem, *,
                figures: list[dict] | None = None) -> dict:
    """按需渲染**单个**课件的 HTML（**0 LLM**）：一行 `{name, text, deck_id, ...}`。

    与 `build_files` 走**同一条渲染路径**（`page_blocks` → `render_html`）——网页端
    「网页版预览」接口（任务 94）用它按需渲一份，口径与交付包里
    `课件/HTML/{deck 标题}.html` 逐字节一致，**不另写一套渲染**；不落盘、不调模型。
    """
    return _render_row(course, deck, list_nodes(db, course.id), figure_index(figures))


def build_files(db: Session, course: Course, *, figures: list[dict] | None = None,
                decks: list[ContentItem] | None = None) -> list[dict]:
    """本课每个课件的 HTML（**0 LLM**）：`[{name, text, deck_id, deck_title, pages, bytes}]`。

    调用方（`delivery.build_delivery`）逐个写进 `课件/HTML/` 并进 manifest；
    这里不落盘、不碰真库，只做纯函数式渲染。
    """
    nodes = list_nodes(db, course.id)
    index = figure_index(figures)
    return [_render_row(course, deck, nodes, index)
            for deck in (decks if decks is not None else ppt_mod.list_decks(db, course.id))]
# ---------------------------------------------------------------------------
# 3. 单文件模板（CSS / JS / 骨架 —— 全部内联，一个 http 字样都不许有）
# ---------------------------------------------------------------------------

_CSS = r"""
/* ══ HTML 课件视觉规范（照样板 v8 规整风） ══════════════════════════════════════
   ① 层级：眉标 27px / 主句 54px / 正文 20–24px / 全页最小 14px；
   ② 留白：页边距 36–118px（左右 112 / 上 60 / 下 36），海报式居中；
   ③ 每个内容页都有视觉：概念图 / 数据图（矢量源内联 base64）或数据卡表格；
   ④ 三端同一画布：1122×793px = A4 横版 297×210mm @96dpi，按视口等比缩放；
   ⑤ 入场动效：单元素 1.05s 缓出 / 步进 170ms / 起步先静 180ms，默认播一次。 */
:root{
  --ink:#0B1220; --ink-2:#334155; --ink-3:#64748B;
  --paper:#F7F8FA; --card:#FFFFFF; --line:#E2E8F0;
  --brand:#1D4ED8; --brand-2:#0F766E;
  --s:1;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0;height:100%}
body{background:#0B1220;color:var(--ink);overflow:hidden;
  font-family:"Noto Sans SC","思源黑体","Microsoft YaHei","微软雅黑","PingFang SC","Noto Sans CJK SC","Hiragino Sans GB",sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;overflow:hidden}
.fit{position:relative;overflow:hidden;width:calc(1122px * var(--s));height:calc(793px * var(--s))}
.stage{position:absolute;left:0;top:0;width:1122px;height:793px;
  transform:scale(var(--s));transform-origin:top left}
.slide{position:absolute;inset:0;display:none;flex-direction:column;overflow:hidden;
  padding:60px 112px 36px;background:var(--paper)}
.slide.is-current{display:flex}
.slide[data-layout="cover"],.slide[data-layout="end"]{background:#0B1220;color:#F8FAFC}
.slide[data-layout="chapter"]{background:#EEF2FF}
.slide[data-layout="review"]{background:#F0FDF4}
.slide[data-layout="quiz"]{background:#FFF7ED}
.slide[data-layout="questions"]{background:#F0F9FF}
.eyebrow{margin:0 0 14px;font-size:27px;line-height:1.2;font-weight:600;
  letter-spacing:.06em;color:var(--brand-2)}
.slide[data-layout="cover"] .eyebrow,.slide[data-layout="end"] .eyebrow{color:#5EEAD4}
.headline{margin:0 0 22px;font-size:54px;line-height:1.16;font-weight:700;
  letter-spacing:-.01em;max-width:26ch;overflow-wrap:anywhere}
.points{display:flex;flex-wrap:wrap;gap:12px 18px;list-style:none;margin:0;padding:0}
.points li{position:relative;flex:1 1 calc(50% - 9px);min-width:0;padding:14px 18px 14px 46px;
  background:var(--card);border:1px solid var(--line);border-radius:14px;
  font-size:22px;line-height:1.5;color:var(--ink-2);overflow-wrap:anywhere}
.points li::before{content:"";position:absolute;left:18px;top:22px;width:14px;height:14px;
  border-radius:4px;background:var(--brand);opacity:.85}
.slide[data-layout="cover"] .points li,.slide[data-layout="end"] .points li{
  background:rgba(255,255,255,.08);border-color:rgba(255,255,255,.18);color:#CBD5E1}
.slide[data-layout="cover"] .points li::before,.slide[data-layout="end"] .points li::before{
  background:#5EEAD4}
.slide[data-layout="chapter"] .points li::before{background:var(--brand-2)}
.figures{display:flex;align-items:stretch;justify-content:center;gap:24px;
  flex:1 1 0;min-height:0;margin-top:18px;overflow:hidden}
.fig{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;
  flex:1 1 0;min-width:0;min-height:0;margin:0;overflow:hidden}
.fig-img{flex:1 1 auto;min-height:0;max-width:100%;object-fit:contain;cursor:zoom-in;
  background:#FFF;border:1px solid var(--line);border-radius:12px}
.fig-table{justify-content:flex-start}
.fig-table table{border-collapse:collapse;font-size:14px;background:#FFF;
  border-radius:10px;overflow:hidden;max-width:100%}
.fig-table th,.fig-table td{border:1px solid var(--line);padding:8px 12px;text-align:left;
  line-height:1.4;color:var(--ink-2)}
.fig-table th{background:#EEF2FF;color:var(--brand);font-weight:600}
.fig-more{margin:6px 0 0;font-size:14px;color:var(--ink-3)}
figcaption{font-size:14px;color:var(--ink-3);text-align:center}
.slide[data-layout="cover"] figcaption,.slide[data-layout="end"] figcaption{color:#94A3B8}
.foot{display:flex;justify-content:space-between;gap:16px;margin-top:14px;padding-top:12px;
  border-top:1px solid rgba(15,23,42,.08);font-size:14px;color:var(--ink-3)}
.foot .page{font-variant-numeric:tabular-nums}
.slide[data-layout="cover"] .foot,.slide[data-layout="end"] .foot{
  color:#94A3B8;border-top-color:rgba(255,255,255,.14)}
.notes{display:none;position:absolute;left:0;right:0;bottom:0;max-height:46%;overflow:auto;
  padding:20px 112px 26px;background:rgba(11,18,32,.94);color:#E2E8F0;
  border-top:2px solid var(--brand-2)}
body.show-notes .slide.is-current .notes{display:block}
.note-head{margin:0 0 6px;font-size:14px;letter-spacing:.08em;color:#5EEAD4}
.note-warn{color:#FDBA74}
.note-body{margin:0 0 14px;font-size:20px;line-height:1.6;overflow-wrap:anywhere}
.hud{position:fixed;left:50%;bottom:14px;transform:translateX(-50%);z-index:30;
  display:flex;align-items:center;justify-content:center;flex-wrap:wrap;gap:8px;
  max-width:calc(100% - 16px);padding:8px 12px;border-radius:999px;
  background:rgba(15,23,42,.88);color:#E2E8F0;font-size:16px}
.hud-btn{cursor:pointer;border:1px solid rgba(255,255,255,.2);background:rgba(255,255,255,.06);
  color:inherit;font:inherit;padding:6px 12px;border-radius:999px}
.hud-btn:hover{background:rgba(255,255,255,.16)}
.counter{min-width:74px;text-align:center;letter-spacing:.08em;font-variant-numeric:tabular-nums}
.toc{position:fixed;inset:0;z-index:40;display:flex;align-items:center;justify-content:center;
  padding:24px;background:rgba(8,12,20,.72)}
.toc[hidden]{display:none}
.toc-box{width:min(760px,100%);max-height:min(76vh,640px);overflow:auto;padding:22px 26px;
  background:#0F172A;color:#E2E8F0;border:1px solid rgba(255,255,255,.16);border-radius:16px}
.toc-head{margin:0 0 12px;font-size:14px;letter-spacing:.1em;color:#5EEAD4}
.toc-list{margin:0;padding-left:24px;font-size:20px;line-height:1.7}
.toc-list a{color:inherit;text-decoration:none}
.toc-list a:hover{color:#5EEAD4;text-decoration:underline}
.lightbox{position:fixed;inset:0;z-index:50;display:flex;align-items:center;justify-content:center;
  padding:16px;background:rgba(8,12,20,.92);cursor:zoom-out}
.lightbox[hidden]{display:none}
.lightbox img{max-width:96vw;max-height:92vh;background:#FFF;border-radius:12px}
.hint{position:fixed;left:50%;top:12px;transform:translateX(-50%);z-index:35;margin:0;
  padding:8px 14px;border-radius:10px;background:rgba(194,65,12,.94);color:#FFF;
  font-size:14px;line-height:1.4;max-width:calc(100% - 24px)}
.hint[hidden]{display:none}
.anim{animation-name:rise;animation-duration:1.05s;animation-fill-mode:both;
  animation-timing-function:cubic-bezier(.22,.61,.36,1);
  animation-delay:calc(180ms + var(--step,0) * 170ms)}
.eyebrow.anim{animation-name:sweep}
.fig.anim{animation-name:pop}
@keyframes rise{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:none}}
@keyframes sweep{from{opacity:0;transform:translateX(-16px)}to{opacity:1;transform:none}}
@keyframes pop{from{opacity:0;transform:scale(.96)}to{opacity:1;transform:none}}
body.no-anim .anim{animation:none!important;opacity:1!important;transform:none!important}
@media (max-width:520px){
  .hud{bottom:8px;gap:6px;padding:6px 8px;font-size:14px}
  .hud-btn{padding:5px 8px}
  .counter{min-width:56px}
}
@media print{
  @page{size:A4 landscape;margin:0}
  html,body{height:auto;overflow:visible;background:#FFF}
  .wrap{position:static;display:block;overflow:visible}
  .fit{width:297mm;height:210mm;overflow:visible}
  .stage{position:static;width:297mm;height:210mm;transform:none}
  .slide{position:relative;display:flex!important;width:297mm;height:210mm;overflow:hidden;
    page-break-after:always;break-after:page}
  .slide:last-child{page-break-after:auto;break-after:auto}
  .notes,.hud,.toc,.lightbox,.hint{display:none!important}
  .anim{animation:none!important;opacity:1!important;transform:none!important}
  .headline{font-size:46px}
  .points li{font-size:19px}
}
"""

_SKELETON = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title><!--TITLE--></title>
<style><!--CSS--></style>
</head>
<body class="deck">
<div class="wrap" id="wrap"><div class="fit" id="fit"><div class="stage" id="stage" data-total="<!--TOTAL-->">
<!--SLIDES-->
</div></div></div>
<div class="hud">
  <button type="button" class="hud-btn" id="btn-prev" title="上一页（← / PageUp）">‹</button>
  <span class="counter" id="counter">1 / <!--TOTAL--></span>
  <button type="button" class="hud-btn" id="btn-next" title="下一页（→ / 空格 / PageDown）">›</button>
  <button type="button" class="hud-btn" id="btn-toc" title="目录（T）">☰ 目录</button>
  <button type="button" class="hud-btn" id="btn-notes" title="讲稿备注（N）">🗒 讲稿</button>
  <button type="button" class="hud-btn" id="btn-anim" title="动画开关（A）">🎬 动画:开</button>
  <button type="button" class="hud-btn" id="btn-replay" title="重播本页动画（R）">↻ 重播</button>
</div>
<div class="toc" id="toc" hidden><div class="toc-box">
  <p class="toc-head">目录 · 点标题跳转（Esc / T 关闭）</p>
  <ol class="toc-list"><!--TOC--></ol>
</div></div>
<div class="lightbox" id="lightbox" hidden><img id="lightbox-img" alt="放大的图"></div>
<p class="hint" id="hint" hidden></p>
<script><!--JS--></script>
</body>
</html>
"""

_JS = r"""(function(){
  var stage=document.getElementById('stage'),fit=document.getElementById('fit');
  var slides=[].slice.call(document.querySelectorAll('.slide'));
  var counter=document.getElementById('counter'),toc=document.getElementById('toc');
  var lightbox=document.getElementById('lightbox'),zoom=document.getElementById('lightbox-img');
  var hint=document.getElementById('hint'),btnAnim=document.getElementById('btn-anim');
  var total=slides.length,current=0;
  function scale(){
    /* 画布按视口等比缩放；底部留 58px 给 HUD（否则按钮条压在页脚上） */
    var s=Math.min(window.innerWidth/1122,(window.innerHeight-58)/793);
    if(!(s>0)){s=0.05;}
    document.documentElement.style.setProperty('--s',String(s));
  }
  function show(n){
    if(!total){return;}
    current=((n%total)+total)%total;
    slides.forEach(function(s,k){s.classList.toggle('is-current',k===current);});
    if(counter){counter.textContent=(current+1)+' / '+total;}
    if(toc){toc.hidden=true;}
  }
  function replay(){
    var s=slides[current];if(!s){return;}
    var prev=s.style.display;s.style.display='none';void s.offsetWidth;s.style.display=prev;
  }
  function toggleNotes(){document.body.classList.toggle('show-notes');}
  function toggleAnim(){
    var off=document.body.classList.toggle('no-anim');
    if(btnAnim){btnAnim.textContent=off?'🎬 动画:关':'🎬 动画:开';}
    if(!off){replay();}
  }
  function openZoom(src,alt){
    if(!lightbox||!zoom){return;}
    zoom.src=src;zoom.alt=alt||'放大的图';lightbox.hidden=false;
  }
  function closeZoom(){if(lightbox){lightbox.hidden=true;}}
  document.addEventListener('keydown',function(e){
    var k=e.key;
    if(k==='ArrowRight'||k==='PageDown'||k===' '||k==='ArrowDown'){show(current+1);e.preventDefault();}
    else if(k==='ArrowLeft'||k==='PageUp'||k==='ArrowUp'){show(current-1);e.preventDefault();}
    else if(k==='Home'){show(0);}else if(k==='End'){show(total-1);}
    else if(k==='a'||k==='A'){toggleAnim();}
    else if(k==='n'||k==='N'){toggleNotes();}
    else if(k==='r'||k==='R'){replay();}
    else if(k==='t'||k==='T'){if(toc){toc.hidden=!toc.hidden;}}
    else if(k==='Escape'){closeZoom();if(toc){toc.hidden=true;}}
  });
  document.getElementById('btn-prev').addEventListener('click',function(){show(current-1);});
  document.getElementById('btn-next').addEventListener('click',function(){show(current+1);});
  document.getElementById('btn-replay').addEventListener('click',replay);
  document.getElementById('btn-notes').addEventListener('click',toggleNotes);
  document.getElementById('btn-anim').addEventListener('click',toggleAnim);
  document.getElementById('btn-toc').addEventListener('click',function(){
    if(toc){toc.hidden=!toc.hidden;}});
  if(toc){
    toc.addEventListener('click',function(){toc.hidden=true;});
    [].slice.call(toc.querySelectorAll('a')).forEach(function(a){
      a.addEventListener('click',function(e){
        e.stopPropagation();
        var n=parseInt(String(a.getAttribute('href')).replace('#p',''),10);
        if(n){show(n-1);e.preventDefault();}
      });
    });
  }
  if(lightbox){lightbox.addEventListener('click',closeZoom);}
  [].slice.call(document.querySelectorAll('.fig-img')).forEach(function(img){
    img.addEventListener('click',function(e){e.stopPropagation();openZoom(img.src,img.alt);});
    img.addEventListener('keydown',function(e){
      if(e.key==='Enter'||e.key===' '){e.preventDefault();openZoom(img.src,img.alt);}});
  });
  document.getElementById('wrap').addEventListener('click',function(e){
    var t=e.target;
    if(t.closest&&(t.closest('.hud')||t.closest('.toc')||t.closest('figure')||t.closest('a')||
      t.closest('button')||t.closest('.lightbox')||t.closest('.notes'))){return;}
    var x=e.clientX/window.innerWidth;
    if(x<0.22){show(current-1);}else if(x>0.78){show(current+1);}
  });
  window.addEventListener('resize',scale);
  window.addEventListener('hashchange',function(){
    var m=/^#p(\d+)$/.exec(window.location.hash||'');
    if(m){show(parseInt(m[1],10)-1);}
  });
  scale();
  var m0=/^#p(\d+)$/.exec(window.location.hash||'');
  show(m0?parseInt(m0[1],10)-1:0);
  var reduced=false;
  try{reduced=!!(window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches);}
  catch(err){reduced=false;}
  if(hint){
    hint.hidden=false;
    hint.textContent=reduced
      ?'系统开启了「减少动画」：本课件仍按默认播放一次入场动画（点「🎬 动画:开」可随时关掉）。'
      :'→ / 空格 翻页 · T 目录 · N 讲稿备注 · 点图放大 · 打印为 A4 横版';
    window.setTimeout(function(){hint.hidden=true;},reduced?9000:7000);
  }
})();
"""
