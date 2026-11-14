"""讲师模式：导图导航 + 逐条揭示 + 点开看展开 + 档位深浅 + 讲者视图（**0 LLM**）。

本模块只新增一条**渲染/交互路径**，不碰 PPTX 生成、交付包、主题与分页：

* 页数据来自课件自身的页级大纲（``recording_kit.deck_pages``）与已有 pptx 的可见文字；
* ``full`` / ``detail`` / ``source`` 只从**本课已有讲义 markdown**里取，不新编内容、不调模型；
* 节点树按「X（接上页） / X · 概念图 / X · 图 N」归并到 ``X``；
* 单文件 HTML 的 CSS / JS / 图片全部内联，离线可开。

口径与样张 ``AI样张与研究/讲师模式/讲师模式-包子第1章.html`` 一致；数据层与渲染层分开，
接口和测试都读同一份 :func:`build_data`。
"""
from __future__ import annotations

import base64
import html as html_mod
import json
import re
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.course import ContentItem, Course, OutlineNode
from app.services import ppt as ppt_mod
from app.services import recording_kit
from app.services.planner import list_nodes

CORE, EXPAND, CASE = "core", "expand", "case"
CASE_MARKS = ("【举例】", "踩过的坑", "类比", "案例", "例如")
CUTTABLE_MARKS = ("本章回顾", "随堂练习", "谢谢观看", "课堂提问")
STRUCTURAL_LAYOUTS = {"cover", "goals", "toc", "chapter", "review", "quiz", "questions", "end"}
STRUCTURAL_MARKS = ("本章回顾", "随堂练习", "谢谢观看", "课堂提问", "课程目录", "课程目标", "本章概览")

_RE_CONT = re.compile(r"（接上页）\s*$")
_RE_CONCEPT = re.compile(r"\s*·\s*概念[图卡]\s*$")
_RE_FIG = re.compile(r"\s*·\s*图\s*\d+\s*$")
_RE_TAG = re.compile(r"^【([^】]+)】")
_RE_BOLD = re.compile(r"\*\*(.+?)\*\*")
_RE_SRC = re.compile(r"[（(]\s*来源\s*[:：]\s*([^）)\n]+)[）)]")
_RE_TABLE = re.compile(r"^\|.*\|$")
_RE_TABLE_SEP = re.compile(r"^\|[\s:|-]+\|$")
_RE_BULLET = re.compile(r"^(✓|-|\*)\s")
_RE_NODE_PREFIX = re.compile(r"^\s*\d+(?:\.\d+)*\s*")


# ---------------------------------------------------------------------------
# 1. 页 / 档位 / 节点
# ---------------------------------------------------------------------------

def base_title(title: object) -> str:
    """去掉续页与图页后缀，得到导图节点名（与样张 ``baseTitle`` 同口径）。"""
    text = str(title or "")
    text = _RE_CONT.sub("", text)
    text = _RE_CONCEPT.sub("", text)
    text = _RE_FIG.sub("", text)
    return text.strip()


def is_cuttable(layout: object, title: object) -> bool:
    """可砍页：回顾 / 练习 / 提问 / 谢谢（讲者视图据此算「还剩几页可砍」）。"""
    return (str(layout or "") in {"review", "quiz", "questions", "end"}
            or any(mark in str(title or "") for mark in CUTTABLE_MARKS))


def is_structural(layout: object, title: object, index: int) -> bool:
    """结构页：封面 / 回顾 / 练习 / 提问 / 谢谢（以及目录、目标等非要点页）。

    这些页不打档位标签、不给展开按钮。旧课件没有 ``meta.layouts`` 时按标题与首页兜底。
    """
    if str(layout or "") in STRUCTURAL_LAYOUTS:
        return True
    if int(index or 0) == 1:
        return True
    return any(mark in str(title or "") for mark in STRUCTURAL_MARKS)


def tier_of(raw: object, index: int) -> str:
    """A2 档位规则：案例标记优先；前两条必讲；其余展开。"""
    text = str(raw or "")
    if any(mark in text for mark in CASE_MARKS):
        return CASE
    return CORE if index < 2 else EXPAND


def _norm_title(text: object) -> str:
    return re.sub(r"\s+", "", str(text or "")).replace("（接上页）", "").replace("（续）", "")


def _match_node(title: str, nodes: list[OutlineNode]) -> OutlineNode | None:
    """把归并后的节点名对回大纲节点（只用于补 node_key / node_id，不参与归并）。"""
    want = {_norm_title(title), _norm_title(_RE_NODE_PREFIX.sub("", title))}
    for node in nodes:
        key = str(node.node_key or "")
        candidates = (str(node.title or ""), f"{key} {node.title or ''}",
                      _RE_NODE_PREFIX.sub("", str(node.title or "")))
        if want & {_norm_title(x) for x in candidates if x}:
            return node
    return None


def build_nodes(pages: list[dict], outline_nodes: list[OutlineNode] | None = None) -> list[dict]:
    """把页按 ``base_title`` 归并成节点，保持首次出现顺序。"""
    groups: dict[str, dict] = {}
    for page_index, page in enumerate(pages):
        title = base_title(page.get("title"))
        group = groups.get(title)
        if group is None:
            group = {"title": title, "base_title": title, "page_indexes": [], "pages": []}
            groups[title] = group
        group["page_indexes"].append(page_index)
        group["pages"].append({
            "index": int(page.get("index") or page_index + 1),
            "title": str(page.get("title") or ""),
            "is_structural": bool(page.get("is_structural")),
        })
    out: list[dict] = []
    for group in groups.values():
        page_rows = [pages[i] for i in group["page_indexes"]]
        node = _match_node(group["title"], outline_nodes or [])
        out.append({
            **group,
            "page_count": len(page_rows),
            "is_structural": all(bool(p.get("is_structural")) for p in page_rows),
            "is_cuttable": all(bool(p.get("is_cuttable")) for p in page_rows),
            "node_key": str(getattr(node, "node_key", "") or "") or None,
            "node_id": str(getattr(node, "id", "") or "") or None,
        })
    return out


# ---------------------------------------------------------------------------
# 2. 讲义 → 每条要点的 full / detail / source
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """去掉标记与标点，只留可比对的字（与样张工具同一口径）。"""
    text = _RE_BOLD.sub(r"\1", text)
    return re.sub(r"[\s*`_（）()【】《》，。、；：:,.;\[\]“”\"']", "", text)


def _clean_detail(lines: list[str]) -> str:
    """展开内容：保留子要点与表格数据行；去掉纯分隔行与只有来源的行。"""
    out: list[str] = []
    for raw in lines:
        line = str(raw or "").strip()
        if not line or _RE_TABLE_SEP.match(line):
            continue
        if _RE_SRC.fullmatch(line):
            continue
        out.append(line)
    return "\n".join(out)


def parse_lecture_markdown(md: str, lecture: str) -> list[dict]:
    """讲义 markdown → 条目树：每个条目 = 一行要点 + 它的子要点 / 表格 / 说明。"""
    entries: list[dict] = []
    section = ""
    current: dict | None = None
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph and current is None:
            body = "".join(paragraph).strip()
            if body:
                entries.append({"section": section, "text": body, "children": [],
                                "level": 0, "lecture": lecture})
        paragraph = []

    for raw in str(md or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("```"):
            continue
        if stripped.startswith("#"):
            flush_paragraph()
            section = stripped.lstrip("# ").strip()
            current = None
            continue
        if stripped.startswith("![") or stripped.startswith("---"):
            continue
        indent = len(line) - len(line.lstrip())
        is_table = bool(_RE_TABLE.match(stripped))
        is_bullet = stripped.startswith(("- ", "* ", "✓ ", "-  ", "-   ")) or bool(
            _RE_BULLET.match(stripped))
        if is_bullet:
            text = re.sub(r"^(✓|-|\*)\s+", "", stripped).strip()
            if indent >= 2 and current is not None:
                current["children"].append(text)
            else:
                current = {"section": section, "text": text, "children": [],
                           "level": 1, "lecture": lecture}
                entries.append(current)
            continue
        if is_table:
            if current is not None:
                current["children"].append(stripped)
            continue
        if indent >= 2 and current is not None:
            current["children"].append(stripped)
        else:
            flush_paragraph()
            paragraph.append(stripped)
            if current is not None:
                current["children"].append(stripped)
            else:
                entries.append({"section": section, "text": stripped, "children": [],
                                "level": 0, "lecture": lecture})
    flush_paragraph()
    return entries


def _source_of(full_text: str, detail: str) -> str:
    match = _RE_SRC.search(full_text) or _RE_SRC.search(detail)
    return f"来源:{match.group(1).strip()}" if match else ""


def pick_detail(entries: list[dict], bullet: str) -> tuple[str, str, str]:
    """给一条课件要点，找它的完整句 / 展开内容 / 来源（只用已有讲义原文）。"""
    tag_match = _RE_TAG.match(str(bullet or ""))
    tag = tag_match.group(1) if tag_match else ""
    body = _RE_TAG.sub("", str(bullet or "")).strip()
    key = _normalize(body)

    best: dict | None = None
    best_score = 0
    for entry in entries:
        entry_key = _normalize(entry["text"])
        if not entry_key or not key:
            continue
        score = 0
        if entry_key.startswith(key) or key.startswith(entry_key):
            score = 60 + min(len(entry_key), len(key))
        elif key[:8] and key[:8] in entry_key:
            score = 40
        elif entry_key[:8] and entry_key[:8] in key:
            score = 30
        if score > best_score:
            best, best_score = entry, score

    alias = {"举例": "举例", "踩过的坑": "踩过的坑", "小结": "小结", "引入": "引入",
             "核心讲解": "核心讲解", "能力边界": "本节不覆盖什么"}
    if best_score < 30 and tag:
        want = alias.get(tag, tag)
        for entry in entries:
            if want and (entry["section"] == want or want in entry["section"]):
                best, best_score = entry, 50
                break

    if best is None:
        return body or str(bullet or ""), "", ""

    full_text = best["text"]
    detail = _clean_detail(list(best.get("children") or []))

    # 叶子要点：用同一个小节 / 同一篇讲义里的同级条目补出「这段讲的是什么」，
    # 但不跨讲义取（避免两章相似句互相串味）。
    if not detail:
        siblings = [e["text"] for e in entries
                    if e is not best and e.get("lecture") == best.get("lecture")
                    and e.get("section") == best.get("section") and e.get("level") == 1
                    and _normalize(e["text"])[:6] != _normalize(best["text"])[:6]]
        if len(siblings) > 1:
            detail = _clean_detail(siblings[:4])
    if not detail and tag:
        want = alias.get(tag, tag)
        section_rows = [e["text"] for e in entries
                        if e.get("section") == want and e.get("level") == 1
                        and e.get("lecture") == best.get("lecture")]
        if section_rows:
            detail = _clean_detail(section_rows[:5])

    return full_text or body, detail, _source_of(full_text, detail)


# ---------------------------------------------------------------------------
# 3. 课件 → 页数据（可见文字 + 图片 + 备注 + 版式）
# ---------------------------------------------------------------------------

def _walk_shapes(shapes):
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    for shape in shapes:
        yield shape
        if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.GROUP:
            yield from _walk_shapes(shape.shapes)


def _image_of(shape) -> dict | None:
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    if getattr(shape, "shape_type", None) != MSO_SHAPE_TYPE.PICTURE:
        return None
    try:
        image = shape.image
        mime = str(getattr(image, "content_type", "") or "")
        if not mime:
            ext = str(getattr(image, "ext", "") or "png").lower()
            mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
        encoded = base64.b64encode(image.blob).decode("ascii")
        return {"src": f"data:{mime};base64,{encoded}", "name": str(getattr(shape, "name", "") or ""),
                "width": int(getattr(shape, "width", 0) or 0),
                "height": int(getattr(shape, "height", 0) or 0)}
    except Exception:  # noqa: BLE001 —— 单张图读不回不该拖垮整页
        return None


def read_pptx_pages(deck: ContentItem) -> tuple[list[dict], str]:
    """从已有 pptx 读「页面上现在显示的句子」与内嵌图片；读不回只返回原因，不写任何东西。"""
    path = Path(str((deck.body or {}).get("pptx") or ""))
    if not path.is_file():
        return [], "课件源文件不存在，已回退页级大纲"
    try:
        from pptx import Presentation

        prs = Presentation(str(path))
        pages: list[dict] = []
        for offset, slide in enumerate(prs.slides):
            title = ""
            if slide.shapes.title is not None and slide.shapes.title.has_text_frame:
                title = slide.shapes.title.text.strip()
            texts: list[str] = []
            images: list[dict] = []
            for shape in _walk_shapes(slide.shapes):
                if getattr(shape, "has_text_frame", False):
                    text = str(shape.text_frame.text or "").strip()
                    if not text or text == title or "· P" in text:
                        continue
                    texts.extend(line.strip() for line in text.split("\n") if line.strip())
                image = _image_of(shape)
                if image:
                    images.append(image)
            notes = ""
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
                notes = str(slide.notes_slide.notes_text_frame.text or "").strip()
            pages.append({"index": offset + 1, "title": title, "texts": texts,
                          "images": images, "notes": notes})
        return pages, ""
    except Exception as exc:  # noqa: BLE001
        return [], f"读取课件源文件失败（已回退页级大纲）：{exc}"


def _deck_pages(deck: ContentItem) -> tuple[list[dict], str]:
    """合并「页级大纲（版式/备注）」与「pptx 可见文字/图片」，页数与顺序以 pptx 为上限。"""
    base_pages = recording_kit.deck_pages(deck)
    pptx_pages, source_error = read_pptx_pages(deck)
    total = max(len(base_pages), len(pptx_pages))
    pages: list[dict] = []
    for offset in range(total):
        base = base_pages[offset] if offset < len(base_pages) else {}
        pptx = pptx_pages[offset] if offset < len(pptx_pages) else {}
        title = str(base.get("title") or pptx.get("title") or "").strip()
        raw_items = list(pptx.get("texts") or []) or list(base.get("bullets") or [])
        # 图注（「图 1 · …」）不是要点：与样张工具同一口径，只留在图注里。
        raw_items = [x for x in raw_items
                     if not str(x).strip().startswith(("图 ", "图·"))]
        index = int(base.get("index") or pptx.get("index") or offset + 1)
        layout = str(base.get("layout") or "")
        pages.append({
            "index": index,
            "title": title,
            "base_title": base_title(title),
            "layout": layout,
            "is_structural": is_structural(layout, title, index),
            "is_cuttable": is_cuttable(layout, title),
            "notes": str(base.get("spoken") or pptx.get("notes") or "").strip(),
            "notes_internal": str(base.get("internal") or "").strip(),
            "images": list(pptx.get("images") or []),
            "_raw": [str(x) for x in raw_items if str(x).strip()],
        })
    return pages, source_error


# ---------------------------------------------------------------------------
# 4. 讲义选择与完整数据
# ---------------------------------------------------------------------------

def _course_lectures(db: Session, course: Course) -> list[ContentItem]:
    return list(db.query(ContentItem).filter(
        ContentItem.course_id == course.id, ContentItem.kind == "lecture",
    ).order_by(ContentItem.created_at).all())


def _lectures_for_deck(db: Session, course: Course, deck: ContentItem,
                       outline_nodes: list[OutlineNode]) -> list[ContentItem]:
    """本 deck 的讲义：优先 ``meta.source.lecture_ids``；老数据按章节点成员兜底。"""
    meta = dict(deck.meta or {})
    ids = [str(x) for x in ((meta.get("source") or {}).get("lecture_ids") or []) if x]
    rows = _course_lectures(db, course)
    by_id = {str(row.id): row for row in rows}
    if ids:
        return [by_id[i] for i in ids if i in by_id]

    node_keys: set[str] = set()
    chapter_key = str(meta.get("chapter_key") or "")
    if chapter_key:
        node_keys = {str(n.node_key) for n in outline_nodes
                     if str(n.node_key) == chapter_key
                     or str(n.node_key).startswith(f"{chapter_key}.")}
    elif deck.node_id:
        node = next((n for n in outline_nodes if n.id == deck.node_id), None)
        if node is not None:
            key = str(node.node_key or "")
            node_keys = {str(n.node_key) for n in outline_nodes
                         if str(n.node_key) == key or str(n.node_key).startswith(f"{key}.")}
    if node_keys:
        node_ids = {n.id for n in outline_nodes if str(n.node_key) in node_keys}
        picked = [row for row in rows if row.node_id in node_ids]
        if picked:
            return picked
    return rows


def build_data(db: Session, course: Course, deck: ContentItem) -> dict:
    """一份课件 → 讲师模式完整数据（页 / 要点 / 节点树 / 章节信息）。"""
    outline_nodes = list_nodes(db, course.id)
    pages, source_error = _deck_pages(deck)
    lectures = _lectures_for_deck(db, course, deck, outline_nodes)
    entries: list[dict] = []
    for lecture in lectures:
        markdown = str((lecture.body or {}).get("markdown") or "")
        if markdown.strip():
            entries.extend(parse_lecture_markdown(markdown, str(lecture.title or lecture.id)))

    for page in pages:
        items: list[dict] = []
        for index, raw in enumerate(page["_raw"]):
            if entries:
                full, detail, source = pick_detail(entries, raw)
            else:
                full, detail, source = raw, "", ""
            items.append({
                "raw": raw,
                "full": full or raw,
                "detail": detail,
                "source": source,
                "tier": tier_of(raw, index),
                # 结构页仍保留可核对上下文（数据验收用），但 can_expand=False：
                # 前端据此**不挂档位标签、不给展开按钮**。
                "can_expand": bool((not page["is_structural"])
                                   and (detail or full != raw)),
            })
        page["items"] = items
        page.pop("_raw", None)

    if not pages:  # 老课件没有页数据：给一页诚实的结构页，不静默空转
        pages = [{
            "index": 1, "title": str(deck.title or "课件"), "base_title": str(deck.title or "课件"),
            "layout": "cover", "is_structural": True, "is_cuttable": False,
            "notes": "", "notes_internal": "", "images": [], "items": [],
        }]

    nodes = build_nodes(pages, outline_nodes)
    meta = dict(deck.meta or {})
    chapter_key = str(meta.get("chapter_key") or "")
    chapter_node = next((n for n in outline_nodes if str(n.node_key) == chapter_key), None)
    if chapter_node is None:
        chapter_node = _match_node(str(deck.title or ""), outline_nodes)
    chapter = {
        "key": chapter_key,
        "title": str(getattr(chapter_node, "title", "") or ("整课" if not chapter_key else deck.title)),
        "node_id": str(getattr(chapter_node, "id", "") or "") or None,
        "deck_id": str(deck.id),
        "deck_title": str(deck.title or ""),
        "scope": str(meta.get("scope") or "course"),
        "nodes": nodes,
    }
    return {
        "course_id": str(course.id),
        "course_title": str(course.title or ""),
        "deck_id": str(deck.id),
        "deck_title": str(deck.title or ""),
        "chapter": chapter,
        "nodes": nodes,
        "page_count": len(pages),
        "pages": pages,
        "source_error": source_error,
    }


def outline_payload(db: Session, course: Course, deck: ContentItem) -> dict:
    """``GET /api/ppt/{course_id}/outline`` 的响应：chapter → point(节点) → pages[]。"""
    data = build_data(db, course, deck)
    return {
        "course_id": data["course_id"],
        "course_title": data["course_title"],
        "deck_id": data["deck_id"],
        "deck_title": data["deck_title"],
        "chapter": data["chapter"],
        "nodes": data["nodes"],
        "page_count": data["page_count"],
        "source_error": data["source_error"],
    }


def resolve_deck(db: Session, course: Course, *, item_id: str | None = None,
                 chapter_key: str | None = None) -> ContentItem:
    """选一份课件：显式 item_id > chapter_key > 第一章；都没有时退回整课课件。"""
    decks = ppt_mod.list_decks(db, course.id)
    if item_id:
        deck = db.get(ContentItem, item_id)
        if deck is None or deck.course_id != course.id or deck.kind != "ppt":
            raise ValueError("课件不存在或不属于该课程")
        return deck
    if chapter_key:
        for deck in decks:
            if str((deck.meta or {}).get("chapter_key") or "") == str(chapter_key):
                return deck
        raise ValueError(f"没有找到第 {chapter_key} 章的课件")

    def _key(deck: ContentItem) -> tuple:
        raw = str((deck.meta or {}).get("chapter_key") or "")
        return (0, int(raw), "") if raw.isdigit() else (1, 0, raw)

    chapters = [d for d in decks if str((d.meta or {}).get("scope") or "course") == "chapter"]
    if chapters:
        return sorted(chapters, key=_key)[0]
    courses = [d for d in decks if str((d.meta or {}).get("scope") or "course") != "chapter"]
    if courses:
        return courses[0]
    raise ValueError("该课程还没有生成课件：请先在 PPT 面板生成整课或章节课件")


def render_presenter(db: Session, course: Course, deck: ContentItem) -> str:
    data = build_data(db, course, deck)
    title = f"{course.title} · {data['chapter'].get('title') or deck.title} · 讲师模式"
    return (_HTML
            .replace("__TITLE__", html_mod.escape(title, quote=True))
            .replace("__DECK__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")))


# ---------------------------------------------------------------------------
# 5. 单文件 HTML（离线；行为以样张为准）
# ---------------------------------------------------------------------------

_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root{--accent:#2563EB;--accent2:#0EA5E9;--panel:#1E293B;--text:#1E293B;--muted:#475569;
        --footer:#94A3B8;--rule:#3B82F6;--bg:#FFFFFF;--case:#8B5CF6}
  *{box-sizing:border-box}
  html,body{margin:0;height:100%;background:#0B1220;font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif;color:var(--text)}
  #stage{position:fixed;inset:0;display:flex;align-items:center;justify-content:center}
  .slide{position:relative;width:min(96vw,1680px);aspect-ratio:16/9;background:var(--bg);border-radius:10px;
         box-shadow:0 18px 60px rgba(0,0,0,.55);overflow:hidden;display:flex;flex-direction:column}
  .band{background:var(--panel);color:#fff;padding:2.1% 3.4% 1.5%;position:relative;flex:0 0 auto}
  .band h1{margin:0;font-size:clamp(20px,2.5vw,42px);line-height:1.22;font-weight:700}
  .band .rule{position:absolute;left:3.4%;bottom:0;height:5px;width:42%;background:var(--rule);border-radius:2px}
  .band .cont{font-size:.55em;opacity:.72;font-weight:400;margin-left:.5em}
  .body{flex:1 1 auto;display:flex;gap:2.6%;padding:2.2% 3.4% 1.6%;min-height:0}
  .bullets{flex:1 1 auto;min-width:0;display:flex;flex-direction:column;justify-content:center;gap:1.1%;
           overflow:auto;scrollbar-width:thin}
  .b{display:flex;gap:.7em;align-items:flex-start;font-size:clamp(12px,1.36vw,23px);line-height:1.5;
     opacity:0;transform:translateY(8px);transition:opacity .3s ease,transform .3s ease;cursor:pointer}
  .b.on{opacity:1;transform:none}
  .b.hide{display:none}
  .b.no-ex{cursor:default}
  .b:hover .line{color:#0F172A}
  .b .dot{flex:0 0 auto;width:.5em;height:.5em;border-radius:50%;background:var(--accent);margin-top:.58em}
  .b.t-expand .dot{background:var(--accent2);opacity:.8}
  .b.t-case .dot{background:var(--case);opacity:.85}
  .b .txt{flex:1 1 auto;min-width:0}
  .b .head{display:flex;align-items:flex-start;gap:.45em;flex-wrap:wrap}
  .b .line{flex:1 1 auto;min-width:12em}
  .tag{font-size:.6em;padding:.12em .5em;border-radius:99px;color:#fff;background:var(--accent2);opacity:.9;
       white-space:nowrap;transform:translateY(-.12em)}
  .b.t-case .tag{background:var(--case)}
  .ex{font-size:.56em;padding:.16em .62em;border-radius:99px;border:1px solid var(--accent);color:var(--accent);
      background:#fff;cursor:pointer;white-space:nowrap;transform:translateY(-.1em);font-family:inherit}
  .b.open .ex{background:var(--accent);color:#fff}
  .detail{display:none;margin:.45em 0 .2em;padding:.7em .9em;border-left:3px solid var(--accent);
          background:#F6F8FC;border-radius:0 8px 8px 0;font-size:.78em;line-height:1.62;color:#334155}
  .b.open .detail{display:block}
  .detail .q{color:#0F172A;font-weight:600}
  .detail ul{margin:.3em 0 0;padding-left:1.1em}
  .detail li{margin:.18em 0}
  .detail .src{margin-top:.5em;color:var(--muted);font-size:.92em}
  .detail .tbl{margin-top:.45em;border-collapse:collapse;font-size:.94em}
  .detail .tbl td{border:1px solid #DCE4F0;padding:.16em .55em}
  .detail .tbl tr:first-child td{background:#EEF2FF;color:#1E3A8A;font-weight:600}
  strong{color:var(--accent)}
  .fig{flex:0 0 38%;display:flex;flex-direction:column;justify-content:center;gap:.5em;min-width:0}
  .fig img{width:100%;height:auto;object-fit:contain;border:1px solid #E2E8F0;border-radius:8px;background:#fff}
  .fig .cap{font-size:clamp(10px,.9vw,14px);color:var(--muted)}
  .foot{flex:0 0 auto;padding:.5% 3.4% 1%;font-size:clamp(9px,.82vw,13px);color:var(--footer);
        display:flex;justify-content:space-between;gap:1em}
  .progress{position:fixed;left:0;top:0;height:3px;background:var(--accent);transition:width .3s;z-index:50}
  #mm{position:fixed;inset:0;background:rgba(6,11,22,.92);z-index:70;display:none;
      padding:34px 46px;overflow:auto;color:#E2E8F0}
  #mm.on{display:block}
  #mm h2{margin:0 0 6px;font-size:20px;color:#fff;font-weight:700}
  #mm .sub{margin:0 0 18px;font-size:12px;color:#94A3B8}
  #mm .root{border-left:2px solid #1E3A8A;padding-left:16px;margin-left:4px}
  #mm .node{display:flex;align-items:center;gap:10px;padding:7px 10px;border-radius:8px;cursor:pointer;
            font-size:15px;line-height:1.4;margin:2px 0}
  #mm .node:hover{background:rgba(37,99,235,.18)}
  #mm .node .idx{flex:0 0 auto;width:22px;height:22px;border-radius:50%;background:#1E293B;color:#93C5FD;
                 font-size:11px;display:flex;align-items:center;justify-content:center}
  #mm .node.cur{background:rgba(37,99,235,.42);color:#fff;font-weight:700}
  #mm .node.cur .idx{background:var(--accent);color:#fff}
  #mm .node.done{color:#64748B}
  #mm .node.struct{color:#94A3B8;font-size:13px}
  #mm .badge{font-size:10px;padding:1px 6px;border-radius:99px;background:#334155;color:#CBD5E1}
  #presenter{position:fixed;left:0;right:0;bottom:0;background:rgba(8,14,26,.96);color:#E2E8F0;
             border-top:2px solid var(--accent);padding:11px 18px;display:none;z-index:60}
  #presenter.on{display:block}
  #presenter .grid{display:grid;grid-template-columns:2.1fr 1fr 1fr;gap:18px}
  #presenter h3{margin:0 0 3px;font-size:12px;letter-spacing:.12em;color:#93C5FD;font-weight:600}
  #presenter .notes{font-size:13px;line-height:1.55;max-height:112px;overflow:auto;color:#CBD5E1}
  #presenter .kpi{font-size:22px;font-weight:700;color:#fff;font-variant-numeric:tabular-nums}
  #presenter .sub{font-size:12px;color:#94A3B8;margin-top:2px}
  #presenter .warn{color:#FBBF24}
  #hint{position:fixed;right:14px;top:10px;font-size:12px;color:#94A3B8;z-index:55;text-align:right;line-height:1.7}
  #hint b{color:#E2E8F0}
</style>
</head>
<body>
<div class="progress" id="progress"></div>
<div id="hint"><b>→</b> 逐条揭示 / 翻页　<b>点要点</b> 展开　<b>E</b> 展开上一条　<b>D</b> 深浅　<b>P</b> 讲者视图　<b>M</b> 导图　<b>F</b> 全屏</div>
<div id="stage"><div class="slide" id="slide"></div></div>
<div id="mm">
  <h2 id="mmTitle">—</h2>
  <p class="sub">点任意节点直接跳过去　·　再按 <b>M</b> 或 Esc 关闭　·　蓝色 = 现在这里</p>
  <div class="root" id="mmTree"></div>
</div>
<div id="presenter">
  <div class="grid">
    <div><h3>本页讲稿（讲者视图）</h3><div class="notes" id="pNotes">—</div></div>
    <div><h3>节奏</h3><div class="kpi" id="pClock">00:00</div>
         <div class="sub" id="pReveal">—</div><div class="sub" id="pPace">—</div></div>
    <div><h3>下一页</h3><div class="sub" id="pNext" style="color:#E2E8F0;font-size:14px">—</div></div>
  </div>
</div>
<script>
const DECK = __DECK__;
const CORE = "core", EXPAND = "expand", CASE = "case";
const TIER_LABEL = {core: "必讲", expand: "展开", case: "案例"};
let page = 0, revealed = 0, depth = "full", presenterOn = false, t0 = Date.now();
const openSet = new Set();
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, ch => (
  {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[ch]));
const inline = s => esc(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
  .replace(/__([^_]+)__/g, "<strong>$1</strong>");
const plain = s => esc(String(s == null ? "" : s).replace(/\*\*/g, ""));

function isStructural(s){ return !!(s && s.is_structural); }
function items(s){ return (s && s.items) ? s.items : []; }
function tierOf(it){ return it && it.tier ? it.tier : EXPAND; }
function canExpand(it){ return !!(it && it.can_expand); }
function visibleSet(s){
  const its = items(s), out = new Set();
  if (isStructural(s)) { its.forEach((_, i) => out.add(i)); return out; }
  const n = depth === "core"
    ? its.filter((it) => tierOf(it) === CORE).length
    : Math.min(revealed, its.length);
  its.forEach((it, i) => {
    if (depth === "core" ? tierOf(it) === CORE : i < n) out.add(i);
  });
  return out;
}
function detailTable(lines){
  const rows = lines.map(line => line.replace(/^\|/, "").replace(/\|$/, "")
    .split("|").map(cell => cell.trim()));
  return '<table class="tbl">' + rows.map(cells => "<tr>" +
    cells.map(cell => `<td>${inline(cell)}</td>`).join("") + "</tr>").join("") + "</table>";
}
function detailHtml(it){
  let html = "";
  if (it.full && it.full !== it.raw) {
    html += '<div class="q">完整句</div><div>' + inline(it.full) + "</div>";
  }
  const lines = String(it.detail || "").split("\n").map(x => x.trim()).filter(Boolean);
  const normal = lines.filter(x => !x.startsWith("|"));
  const table = lines.filter(x => x.startsWith("|"));
  if (normal.length) {
    html += '<div class="q" style="margin-top:.5em">这一段还讲了</div><ul>' +
      normal.map(x => `<li>${inline(x)}</li>`).join("") + "</ul>";
  }
  if (table.length) {
    html += '<div class="q" style="margin-top:.5em">这一段还讲了</div>' + detailTable(table);
  }
  if (it.source) {
    const source = /^来源\s*[:：]/.test(it.source) ? it.source : `来源：${it.source}`;
    html += `<div class="src">${esc(source)}</div>`;
  }
  return html;
}
function render(){
  const s = DECK.pages[page];
  if (!s) return;
  const its = items(s), vis = visibleSet(s), structural = isStructural(s);
  const base = s.base_title || String(s.title || "").replace(/（接上页）$/, "");
  const cont = String(s.title || "").endsWith("（接上页）") ? '<span class="cont">（接上页）</span>' : "";
  const img = (s.images || [])[0];
  const figHtml = (img && img.src)
    ? `<div class="fig"><img src="${esc(img.src)}" alt=""><div class="cap">图 · ${plain(base)}</div></div>` : "";
  const rows = its.map((it, i) => {
    const tier = tierOf(it), on = vis.has(i) ? " on" : "";
    const hidden = (depth === "core" && tier !== CORE) ? " hide" : "";
    const open = openSet.has(i) ? " open" : "";
    const can = !structural && canExpand(it);
    const tag = (!structural && tier !== CORE)
      ? `<span class="tag">${TIER_LABEL[tier] || tier}</span>` : "";
    const btn = can ? `<button class="ex" data-i="${i}">${openSet.has(i) ? "收起 ▴" : "展开 ▾"}</button>` : "";
    const detail = can ? `<div class="detail">${detailHtml(it)}</div>` : "";
    const cls = `b${on}${hidden}${open} t-${tier === CASE ? "case" : tier === EXPAND ? "expand" : "core"}` +
      (can ? "" : " no-ex");
    return `<div class="${cls}" data-i="${i}"><span class="dot"></span><span class="txt">` +
      `<span class="head"><span class="line">${inline(it.raw)}</span>${tag}${btn}</span>${detail}</span></div>`;
  }).join("");
  $("slide").innerHTML = `
    <div class="band"><h1>${plain(base)}${cont}</h1><div class="rule"></div></div>
    <div class="body"><div class="bullets">${rows || '<div class="b on no-ex"><span class="dot"></span><span class="txt" style="color:#94A3B8">（本页为结构页）</span></div>'}</div>${figHtml}</div>
    <div class="foot"><span>${plain(DECK.course_title)} · ${plain(DECK.chapter.title)}</span><span>${page + 1} / ${DECK.pages.length}</span></div>`;
  $("progress").style.width = ((page + 1) / DECK.pages.length * 100) + "%";
  bindRows();
  renderPresenter();
}
function bindRows(){
  document.querySelectorAll(".b").forEach(row => {
    row.addEventListener("click", ev => { ev.stopPropagation(); toggleRow(Number(row.dataset.i)); });
  });
}
function toggleRow(i){
  const s = DECK.pages[page], it = items(s)[i];
  if (!it || isStructural(s) || !canExpand(it)) return;
  if (openSet.has(i)) openSet.delete(i); else openSet.add(i);
  render();
}
function toggleLastDetail(){
  const s = DECK.pages[page], its = items(s);
  if (isStructural(s)) return;
  const n = Math.min(revealed || its.length, its.length);
  for (let i = n - 1; i >= 0; i--) {
    if (canExpand(its[i])) { toggleRow(i); return; }
  }
}
function limitOf(s){
  const its = items(s);
  return depth === "core" ? its.filter(it => tierOf(it) === CORE).length : its.length;
}
function next(){
  const s = DECK.pages[page];
  if (!isStructural(s) && revealed < limitOf(s)) { revealed++; render(); return; }
  if (page < DECK.pages.length - 1) { page++; revealed = 0; openSet.clear(); render(); }
}
function prev(){
  const s = DECK.pages[page];
  if (!isStructural(s) && revealed > 0) { revealed--; render(); return; }
  if (page > 0) { page--; revealed = items(DECK.pages[page]).length; openSet.clear(); render(); }
}
function renderPresenter(){
  const s = DECK.pages[page];
  if (!s) return;
  const its = items(s);
  const shown = isStructural(s) ? its.length
    : (depth === "core" ? its.filter(it => tierOf(it) === CORE).length : visibleSet(s).size);
  const secs = Math.floor((Date.now() - t0) / 1000);
  $("pClock").textContent = String(Math.floor(secs / 60)).padStart(2, "0") + ":" +
    String(secs % 60).padStart(2, "0");
  $("pReveal").innerHTML = isStructural(s) ? "结构页（整页一次出）"
    : `本页已出 <b>${shown}/${its.length}</b> 条` +
      (depth === "core" ? ' · <span class="warn">只讲必讲</span>' : "");
  const left = DECK.pages.length - page - 1;
  const cut = DECK.pages.slice(page + 1).filter(x => x.is_cuttable).length;
  $("pPace").innerHTML = `已讲 ${Math.floor(secs / 60)} 分钟 · 每 7 分钟安排一次互动` +
    `<br>还剩 ${left} 页` + (cut ? `（其中 <span class="warn">${cut} 页可砍</span>）` : "") +
    (secs >= 420 ? ' · <span class="warn">该互动了</span>' : "");
  $("pNotes").textContent = s.notes || "（本页没有备注）";
  const nx = DECK.pages[page + 1];
  $("pNext").textContent = nx ? `${page + 2}. ${nx.title}` : "（已到最后一页）";
}
function renderMap(){
  const nodes = DECK.nodes || [];
  const current = nodes.find(n => (n.page_indexes || []).includes(page));
  $("mmTitle").textContent = `${DECK.course_title} · ${DECK.chapter.title}`;
  $("mmTree").innerHTML = nodes.map((n, k) => {
    const isCur = current === n ? " cur" : "";
    const last = (n.page_indexes || [])[n.page_indexes.length - 1];
    const done = last < page ? " done" : "";
    const cls = n.is_structural ? " struct" : "";
    const badge = n.page_count > 1 ? `<span class="badge">${n.page_count} 页</span>` : "";
    return `<div class="node${isCur}${done}${cls}" data-page="${(n.page_indexes || [0])[0]}">` +
      `<span class="idx">${k + 1}</span><span>${plain(n.base_title)}</span>${badge}</div>`;
  }).join("");
  document.querySelectorAll("#mmTree .node").forEach(node => {
    node.addEventListener("click", ev => {
      ev.stopPropagation();
      page = Number(node.dataset.page); revealed = 0; openSet.clear();
      $("mm").classList.remove("on"); render();
    });
  });
}
function toggleMap(){
  const on = $("mm").classList.toggle("on");
  if (on) renderMap();
}
document.addEventListener("keydown", e => {
  const k = e.key;
  if (["ArrowRight", "PageDown", " ", "Enter"].includes(k)) { e.preventDefault(); next(); }
  else if (["ArrowLeft", "PageUp", "Backspace"].includes(k)) { e.preventDefault(); prev(); }
  else if (k === "e" || k === "E") toggleLastDetail();
  else if (k === "d" || k === "D") { depth = depth === "full" ? "core" : "full"; render(); }
  else if (k === "p" || k === "P") {
    presenterOn = !presenterOn; $("presenter").classList.toggle("on", presenterOn); renderPresenter();
  }
  else if (k === "t" || k === "T") { t0 = Date.now(); renderPresenter(); }
  else if (k === "f" || k === "F") {
    document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen();
  }
  else if (k === "m" || k === "M") toggleMap();
  else if (k === "Escape") { $("mm").classList.remove("on"); openSet.clear(); render(); }
  else if (/^[0-9]$/.test(k)) {
    const n = k === "0" ? 9 : Number(k) - 1;
    if (DECK.pages[n]) { page = n; revealed = 0; openSet.clear(); render(); }
  }
});
setInterval(() => { if (presenterOn) renderPresenter(); }, 1000);
$("stage").addEventListener("click", e => {
  if (e.target.closest(".b")) return;
  if (e.clientX < window.innerWidth / 2) prev(); else next();
});
(function applyHash(){
  const m = new URLSearchParams(location.hash.slice(1));
  if (m.has("p")) { const n = Number(m.get("p")) - 1; if (DECK.pages[n]) page = n; }
  if (m.has("r")) revealed = Number(m.get("r")) || 0;
  if (m.get("t") === "core") depth = "core";
  if (m.get("open")) m.get("open").split(",").forEach(x => openSet.add(Number(x)));
  if (m.get("presenter") === "1") { presenterOn = true; $("presenter").classList.add("on"); }
  if (m.get("map") === "1") { $("mm").classList.add("on"); }
})();
render();
if ($("mm").classList.contains("on")) renderMap();
</script>
</body>
</html>
"""
