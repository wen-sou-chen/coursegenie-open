"""任务 87（修订版）：录课包 —— 逐字稿 / 时长表 / 字幕 / 分镜表 / 互动点（**0 LLM**）。

**它是什么**：给「要录成视频课」的课多出一份可直接开工的出料包 —— 把**现有的**课件演讲备注
按页序拼成逐字稿、按 4.5 字/秒算时长（6 分钟闸门）、切字幕 SRT、列分镜/板书、抽课堂互动点。
全部是**纯本地确定性渲染**：不调模型、不改写任何原文、不动默认八件套。

三处口径（本模块是唯一出处，交付包与探针都读这里）：

1. **生成条件**：``录课包/`` 仅当课程场景含 ``recorded`` 时生成（未选场景 / 只含 classroom /
   live / self_study / lms 的课**结构一个字不变**）。``互动点.md`` 还要求场景含
   ``classroom`` / ``live``（它是课堂/直播的料，录课包先立住才轮到它）。
2. **秒数**：每页秒数 = 该页**口播稿**字数 ÷ 4.5 字/秒（向上取整，字数 = 去掉空白后的字符数）。
   备注里第二段起是 ``ppt_layout.compose_notes`` 追加的**诊断 / 内部提示**（如「课件操作提示
   （仅讲师 / 运营可见，勿投屏）」）—— **照抄保留、单独标注、不计时长、不进字幕**。
3. **字幕**：成人阅读速度上限 20 字符/秒、单行 ≤42 字符、每条 ≤2 行；时间轴按每页预算等比切分，
   与前一条的秒数**同源**，因此天然满足 ≤20 字符/秒，且递增不重叠。
"""
from __future__ import annotations

import math
import re
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.course import ContentItem, Course, QuizItem
from app.services import lecture_qc
from app.services import ppt as ppt_mod
from app.services import scenario
from app.services.planner import list_nodes

DIR_NAME = "录课包"
SCRIPT_DIR = "逐字稿"
SRT_DIR = "字幕"
DURATION_NAME = "时长表.md"
STORYBOARD_NAME = "分镜表.md"
INTERACTION_NAME = "互动点.md"

#: 口语速度：中文讲稿约 4.5 字/秒（≈270 字/分钟）。
CHARS_PER_SECOND = 4.5
#: 6 分钟闸门（edX 10 万+ 学习者数据：超过 6 分钟注意力迅速流失）。
SECTION_LIMIT_SECONDS = 360
SECTION_LIMIT_NOTE = "⚠️ 超过 6 分钟，建议在此处切分"
#: 字幕硬约束（HANDOFF 任务 87 §A3）。
MAX_LINE_CHARS = 42
MAX_CUE_LINES = 2
CPS_LIMIT = 20
#: 页间过渡的固定连接句模板（确定性；只改「页与页」这一层，不动备注原文）。
TRANSITION = "下一页我们看「{title}」。"
TRANSITION_NO_TITLE = "下一页我们接着看。"
#: 备注里第二段起属于诊断 / 内部提示（``compose_notes`` 的拼装顺序）；命中这些标记的也一律归到内部。
INTERNAL_MARKS = ("课件操作提示（仅讲师 / 运营可见，勿投屏）：", "（仅讲师可见）")
INTERNAL_NOTE_PREFIX = "> 讲师提示（勿投屏，不计时长）："
TRANSITION_HEAD = "下一页我们看"
_HEADING = "# {title} · 逐字稿"
_RE_WS = re.compile(r"\s+")
_RE_CONT = re.compile(r"\s*（续）\s*$")
_RE_CHAPTER_PREFIX = re.compile(r"^\s*第\s*[0-9一二三四五六七八九十百]+\s*章[\s:：、.．-]*")
_PUNCT = "。！？；!?;，,、：:"


# ---------------------------------------------------------------------------
# 场景开关（唯一出处：services.scenario）
# ---------------------------------------------------------------------------

def _selected(course) -> list[str]:
    return list(scenario.scenario_meta(course).get("selected") or [])


def kit_enabled(course) -> bool:
    """录课包要不要出：**仅当场景含 ``recorded``**（未选场景 = 不出，结构一成不变）。"""
    return "recorded" in _selected(course)


def wants_interactions(course) -> bool:
    """互动点要不要出：场景含 ``classroom`` / ``live``（且录课包本身要出）。"""
    return any(key in _selected(course) for key in ("classroom", "live"))


# ---------------------------------------------------------------------------
# 备注 / 秒数口径
# ---------------------------------------------------------------------------

def split_notes(notes) -> tuple[str, str]:
    """备注 → ``(口播稿, 内部提示)``。

    ``ppt_layout.compose_notes`` 拼装顺序是「口播稿 + 换行 + 诊断信息」，所以第一段是可照念的稿子；
    第二段起是「未嵌入的图 / 要点超长 / 课件操作提示（勿投屏）」这类**给讲师与运营看**的话。
    两段都**原样保留**（本模块不改写任何原文），只是分开摆放、分开计时。
    """
    text = str(notes or "").strip()
    if not text:
        return "", ""
    parts = text.split("\n")
    spoken = parts[0].strip()
    rest = "\n".join(part.strip() for part in parts[1:]).strip()
    if not rest:
        for mark in INTERNAL_MARKS:
            pos = spoken.find(mark)
            if pos > 0:
                rest = spoken[pos:].strip()
                spoken = spoken[:pos].strip()
                break
    return spoken, rest


def count_chars(text) -> int:
    """字数口径：去掉空白后的字符数（与截图 / 备注里的可见字数一致）。"""
    return len(_RE_WS.sub("", str(text or "")))


def seconds_of(text) -> int:
    """字数 ÷ 4.5 字/秒，向上取整（空文本 = 0 秒）。"""
    return int(math.ceil(count_chars(text) / CHARS_PER_SECOND))


# ---------------------------------------------------------------------------
# 页 / 小节
# ---------------------------------------------------------------------------

def _norm_title(text) -> str:
    raw = str(text or "").strip()
    raw = _RE_CONT.sub("", raw)                      # 「（续）」与首页同属一个小节
    return _RE_WS.sub(" ", raw).strip()


def _outline_nodes(db: Session, course: Course) -> list:
    return list_nodes(db, course.id)


def _title_index(nodes: list) -> dict[str, object]:
    """标题 → 节点（同时索引「1.1 标题」与「标题」两种写法）。"""
    index: dict[str, object] = {}
    for node in nodes:
        key = str(getattr(node, "node_key", "") or "")
        title = str(getattr(node, "title", "") or "")
        if not title:
            continue
        for candidate in (title, f"{key} {title}", f"{key} {_RE_CHAPTER_PREFIX.sub('', title).strip()}"):
            name = _norm_title(candidate)
            if name:
                index.setdefault(name, node)
    return index


def _chapter_index(nodes: list) -> dict[str, object]:
    return _title_index([node for node in nodes if "." not in str(getattr(node, "node_key", "") or "")])


def deck_pages(deck: ContentItem) -> list[dict]:
    """一个 deck 的页：``[{index, title, layout, bullets, spoken, internal, chars, seconds}]``。

    页级数据取课件自己的 ``meta.slides``（``ppt.outline_slides`` 在旧课件上会现场读回 pptx），
    版式族取 ``meta.layouts``（与页号对齐；缺失写「未记录」，绝不猜）。
    """
    meta = dict(deck.meta or {})
    layouts = list(meta.get("layouts") or [])
    pages: list[dict] = []
    for offset, slide in enumerate(ppt_mod.outline_slides(deck)):
        notes = str(slide.get("notes_excerpt") or "")
        spoken, internal = split_notes(notes)
        layout = str(layouts[offset]) if offset < len(layouts) else ""
        pages.append({
            "index": int(slide.get("index") or offset + 1),
            "title": str(slide.get("title") or "").strip(),
            "layout": layout or "未记录",
            "bullets": [str(b).strip() for b in (slide.get("bullets") or []) if str(b).strip()],
            "spoken": spoken,
            "internal": internal,
            "chars": count_chars(spoken),
            "seconds": seconds_of(spoken),
        })
    return pages


def deck_scope(deck: ContentItem) -> tuple[str, str]:
    """``(scope, chapter_key)``：``scope`` = ``chapter`` / ``course``。"""
    meta = dict(deck.meta or {})
    scope = str(meta.get("scope") or "course")
    return scope, str(meta.get("chapter_key") or "")


def ordered_decks(db: Session, course: Course) -> list[ContentItem]:
    """整课课件在前，其后按章号排（同一课里章课件顺序稳定，不受生成时间影响）。"""
    decks = list(ppt_mod.list_decks(db, course.id))

    def sort_key(deck: ContentItem):
        scope, chapter_key = deck_scope(deck)
        try:
            chapter_no = int(re.sub(r"\D", "", chapter_key) or 0)
        except ValueError:
            chapter_no = 0
        return (0 if scope != "chapter" else 1, chapter_no, str(deck.title or ""))

    return sorted(decks, key=sort_key)


def sections_of(deck: ContentItem, nodes: list) -> list[dict]:
    """把该 deck 的页按**小节**聚合：``[{key, title, pages}]``。

    小节 key = ``{章}-{节}``：知识点页 ⇒ ``1-1``；章内其余页（封面 / 回顾 / 练习 / 提问 / 末页）
    与整课里的章概览页 ⇒ ``1-0``；整课封面 / 目标 / 目录 / 结尾 ⇒ ``0-0``。
    """
    pages = deck_pages(deck)
    scope, chapter_key = deck_scope(deck)
    is_chapter = scope == "chapter"
    titles = _title_index(nodes)
    chapters = _chapter_index(nodes)
    chapter_node = chapters.get(_norm_title(str(chapter_key))) or None
    chapter_no = str(getattr(chapter_node, "node_key", "") or chapter_key or "0")
    general_key = f"{chapter_no}-0" if is_chapter else "0-0"
    general_title = ("本章其余页（封面 / 回顾 / 练习 / 提问 / 末页）" if is_chapter
                     else "课程通用页（封面 / 目标 / 目录 / 结尾）")
    groups: list[dict] = []
    seen: dict[str, dict] = {}
    for page in pages:
        layout = page["layout"]
        node = titles.get(_norm_title(page["title"])) if layout in ("point", "chapter") else None
        if is_chapter and layout == "point" and node is not None:
            key = f"{chapter_no}-{str(getattr(node, 'node_key', '') or '').split('.')[-1]}"
            title = str(getattr(node, "title", "") or "")
        elif (not is_chapter) and layout == "chapter" and node is not None:
            key = f"{str(getattr(node, 'node_key', '') or '')}-0"
            title = f"第{str(getattr(node, 'node_key', '') or '')}章 " \
                    f"{_RE_CHAPTER_PREFIX.sub('', str(getattr(node, 'title', '') or '')).strip()}"
        else:
            key, title = general_key, general_title
        group = seen.get(key)
        if group is None:
            group = {"key": key, "title": title, "pages": []}
            seen[key] = group
            groups.append(group)
        group["pages"].append(page)
    return groups


def section_seconds(section: dict) -> tuple[int, int]:
    """``(页数, 秒数)`` —— 小节合计（页数、字数、秒数都按页求和）。"""
    return len(section["pages"]), sum(page["seconds"] for page in section["pages"])


def section_chars(section: dict) -> int:
    return sum(page["chars"] for page in section["pages"])


# ---------------------------------------------------------------------------
# 1. 逐字稿
# ---------------------------------------------------------------------------

def script_markdown(deck: ContentItem, pages: list[dict]) -> str:
    """逐字稿：页序拼装演讲备注，**不做任何改写**；页间加固定连接句。"""
    lines = [
        _HEADING.format(title=str(deck.title or "课件")),
        "",
        "> 本稿由该课件的**演讲备注**按页序拼装，**未做任何改写**"
        "（任务 87 · 纯本地渲染，0 次 LLM 调用）。",
        "> 页标记里的秒数 = 该页口播稿字数 ÷ 4.5 字/秒（向上取整），与 `录课包/时长表.md` 同一口径。",
        "> 备注里「课件操作提示（仅讲师 / 运营可见，勿投屏）」这类**内部提示**照抄保留、单独标注："
        "**不要念**，也不计时长、不进字幕。",
        "",
    ]
    for offset, page in enumerate(pages):
        lines += [f"【第 {page['index']} 页 · ≈{page['seconds']} 秒】", ""]
        lines += [page["spoken"] or "（本页没有演讲备注，请在课件里补上）", ""]
        if page["internal"]:
            lines += [INTERNAL_NOTE_PREFIX + page["internal"].replace("\n", " "), ""]
        if offset + 1 < len(pages):
            title = pages[offset + 1]["title"]
            lines += [TRANSITION.format(title=title) if title else TRANSITION_NO_TITLE, ""]
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# 2. 时长表（6 分钟闸门）
# ---------------------------------------------------------------------------

def duration_line(section: dict) -> str:
    """时长表的一行（格式与探针逐字约定）：``- {章}-{节} {小节标题}：共 N 页、正文 M 字，约 S 秒（≈X 分钟）``。"""
    pages, seconds = section_seconds(section)
    line = (f"- {section['key']} {section['title']}：共 {pages} 页、"
            f"正文 {section_chars(section)} 字，约 {seconds} 秒（≈{seconds / 60:.1f} 分钟）")
    if seconds > SECTION_LIMIT_SECONDS:
        line += f" {SECTION_LIMIT_NOTE}"
    return line


def duration_markdown(entries: list[dict]) -> str:
    """时长表：逐 deck 列小节行 + 末尾总计。``entries`` = ``[{deck, sections}]``。"""
    lines = [
        "# 时长表（录课包）",
        "",
        f"> 口径：每页秒数 = 该页演讲备注的**口播稿**字数 ÷ {CHARS_PER_SECOND} 字/秒（向上取整）；"
        "字数 = 去掉空白后的字符数；内部提示不计时长。",
        f"> 闸门：一个小节合计超过 **{SECTION_LIMIT_SECONDS} 秒（6 分钟）** ⇒ 该行末尾追加"
        f"「{SECTION_LIMIT_NOTE}」（只提示，不改内容）。",
        "> 小节口径：章课件里一个知识点一行；封面 / 回顾 / 练习 / 提问 / 末页归到该章的"
        "`{章}-0`，整课的封面 / 目标 / 目录 / 结尾归到 `0-0`。",
        "",
    ]
    total = 0
    for entry in entries:
        lines += [f"## {entry['deck'].title}", ""]
        for section in entry["sections"]:
            lines.append(duration_line(section))
            total += section_seconds(section)[1]
        lines.append("")
    lines += [f"全 deck 合计 约 {total} 秒 / ≈{total / 60:.1f} 分钟", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3. 字幕 SRT
# ---------------------------------------------------------------------------

def wrap_lines(text: str, limit: int = MAX_LINE_CHARS) -> list[str]:
    """按 ≤42 字符断行：优先断在句读标点后，其次断在空格处，都没有才硬断。"""
    rest = str(text or "").strip()
    out: list[str] = []
    while rest:
        if len(rest) <= limit:
            out.append(rest)
            break
        window = rest[:limit]
        cut = max((window.rfind(ch) for ch in _PUNCT), default=-1) + 1
        if cut <= 0:
            cut = window.rfind(" ") + 1
        if cut <= 0:
            cut = limit
        head = rest[:cut].strip()
        rest = rest[cut:].strip()
        if head:
            out.append(head)
    return out


def cues_of(text: str) -> list[list[str]]:
    """一页文本 → 字幕条（每条 ≤2 行、每行 ≤42 字符）。"""
    lines = wrap_lines(text)
    return [lines[i:i + MAX_CUE_LINES] for i in range(0, len(lines), MAX_CUE_LINES)]


def _stamp(ms: int) -> str:
    ms = max(0, int(ms))
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def srt_text(deck: ContentItem, pages: list[dict]) -> str:
    """字幕：从逐字稿的**口播稿**切句；时间轴按每页秒数等比切分，页与页连续、递增不重叠。"""
    blocks: list[str] = []
    clock_ms = 0
    serial = 0
    for page in pages:
        seconds = page["seconds"]
        groups = cues_of(page["spoken"]) if page["spoken"] else []
        if not groups or seconds <= 0:
            clock_ms += max(0, seconds) * 1000
            continue
        # 分母用**字幕文本**的字数（与逐条 cue 同一口径）⇒ 比例必然收敛到 1，页末时间 == 该页预算
        total_chars = sum(len(line) for group in groups for line in group)
        bounds: list[int] = []
        used = 0
        for group in groups:
            used += sum(len(line) for line in group)
            bounds.append(clock_ms + round(seconds * used / total_chars * 1000))
        start = clock_ms
        for group, end in zip(groups, bounds):
            if end <= start:            # 保证严格递增（正常比例下不会发生）
                end = start + 1
            serial += 1
            blocks.append("\n".join([str(serial), f"{_stamp(start)} --> {_stamp(end)}",
                                     *group]) + "\n")
            start = end
        clock_ms = start if bounds else clock_ms + seconds * 1000
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# 4. 分镜 / 板书表
# ---------------------------------------------------------------------------

def _clip_text(text: str, limit: int) -> str:
    text = _RE_WS.sub(" ", str(text or "")).strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def media_counts(deck: ContentItem) -> list[int] | None:
    """该 deck 每页**内嵌图片数**（读真实 pptx；源文件不在 / 读不回 ⇒ ``None``，写「未核对」）。"""
    path = str((deck.body or {}).get("pptx") or "")
    if not path or not Path(path).is_file():
        return None
    try:
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE

        prs = Presentation(path)
        counts: list[int] = []
        for slide in prs.slides:
            total = 0
            for shape in slide.shapes:
                try:
                    if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                        total += 1
                except Exception:      # noqa: BLE001 —— 单个形状读不回不影响整页计数
                    continue
            counts.append(total)
        return counts
    except Exception:                  # noqa: BLE001 —— 读不回一律降级，绝不让出包失败
        return None


def _board_points(db: Session, course: Course, node) -> str:
    """该页的板书要点：同课时讲义的关键点（与教案「板书设计」同一函数来源）。"""
    if node is None:
        return "—"
    item = (db.query(ContentItem)
            .filter(ContentItem.course_id == course.id, ContentItem.node_id == node.id,
                    ContentItem.kind == "lecture")
            .order_by(ContentItem.created_at).all())
    markdown = lecture_qc.lecture_markdown(item[-1]) if item else ""
    points = [_clip_text(text, 24) for text in lecture_qc.key_points(markdown)[:2]]
    return "；".join(points) if points else "—"


def storyboard_markdown(db: Session, course: Course, entries: list[dict], nodes: list) -> str:
    """分镜 / 板书表：每页一行（页号 + 讲什么 / 画面是什么 / 板书要点三列）。"""
    titles = _title_index(nodes)
    lines = [
        "# 分镜 / 板书表（录课包）",
        "",
        "> 口径：「讲什么」取自该页**要点**（没有要点时取口播稿首句）；「画面是什么」= 该页版式族"
        "（课件 `meta.layouts`）+ 该页内嵌图片数（从 pptx 读回；源文件不在写「未核对」）；"
        "「板书要点」取自该课时讲义的关键点（与 `教案/` 的板书设计同一函数来源），非知识点页写「—」。",
        "",
    ]
    for entry in entries:
        deck = entry["deck"]
        counts = media_counts(deck)
        lines += [f"## {deck.title}", "", "| 页 | 讲什么 | 画面是什么 | 板书要点 |",
                  "| --- | --- | --- | --- |"]
        for offset, page in enumerate(entry["pages"]):
            spoken_head = re.split(r"[。！？；]", page["spoken"])[0] if page["spoken"] else ""
            what = "；".join(page["bullets"]) or spoken_head or "—"
            media = (f"内嵌图 {counts[offset]} 张"
                     if counts is not None and offset < len(counts)
                     else "未核对（源 pptx 不在 / 读不回）")
            node = titles.get(_norm_title(page["title"]))
            board = _board_points(db, course, node) if page["layout"] == "point" else "—"
            lines.append(f"| 第 {page['index']} 页 | {_clip_text(what, 60)} | "
                         f"{page['layout']} · {media} | {board} |")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 5. 课堂互动点
# ---------------------------------------------------------------------------

def interaction_markdown(db: Session, course: Course, nodes: list) -> str:
    """互动点：每个知识点 1~2 条（讲义小节标题 + 随堂练习题干，确定性模板）。"""
    lines = [
        "# 课堂互动点（录课包）",
        "",
        "> 只有当课程场景含「线下课堂面授 / 线上直播」时生成；从讲义小节标题与随堂练习**确定性抽取**，"
        "每个知识点 1~2 条（0 次 LLM 调用）。",
        "",
    ]
    quizzes: dict[str, list] = {}
    rows = (db.query(QuizItem).filter(QuizItem.course_id == course.id)
            .order_by(QuizItem.created_at).all())
    for quiz in rows:
        quizzes.setdefault(str(quiz.node_id or ""), []).append(quiz)
    for node in nodes:
        key = str(getattr(node, "node_key", "") or "")
        if "." not in key:
            continue
        title = str(getattr(node, "title", "") or "")
        lines.append(f"## {key} {title}")
        lines.append(f"- 提问点 1（概念复述）：先请学员用自己的话说明「{title}」，"
                     "再对照讲义里的定义补齐遗漏。")
        quiz = (quizzes.get(str(getattr(node, "id", "") or "")) or [None])[0]
        if quiz is not None:
            lines.append("- 提问点 2（先答后讲）：先独立作答随堂练习第 1 题："
                         f"{_clip_text(str(quiz.question or ''), 60)}"
                         "（作答后再对照答案解析）。")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 组装：交付包要写的文件（0 LLM）
# ---------------------------------------------------------------------------

def _safe_name(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", str(text or "")).strip() or "未命名"


def collect_entries(db: Session, course: Course) -> list[dict] | None:
    """录课包的原料（不出 ⇒ ``None``）：逐 deck 的页与小节。"""
    if not kit_enabled(course):
        return None
    nodes = _outline_nodes(db, course)
    return [{"deck": deck, "pages": deck_pages(deck), "sections": sections_of(deck, nodes)}
            for deck in ordered_decks(db, course)]


def build_files(db: Session, course: Course) -> list[dict]:
    """录课包要写进交付包的全部文件：``[{path, text}]``（未启用场景 ⇒ ``[]``）。"""
    entries = collect_entries(db, course)
    if not entries:
        return []
    nodes = _outline_nodes(db, course)
    files: list[dict] = []
    for entry in entries:
        name = _safe_name(entry["deck"].title)
        files.append({"path": f"{DIR_NAME}/{SCRIPT_DIR}/{name}-逐字稿.md",
                      "text": script_markdown(entry["deck"], entry["pages"])})
        files.append({"path": f"{DIR_NAME}/{SRT_DIR}/{name}.srt",
                      "text": srt_text(entry["deck"], entry["pages"])})
    files.append({"path": f"{DIR_NAME}/{DURATION_NAME}", "text": duration_markdown(entries)})
    files.append({"path": f"{DIR_NAME}/{STORYBOARD_NAME}",
                  "text": storyboard_markdown(db, course, entries, nodes)})
    if wants_interactions(course):
        files.append({"path": f"{DIR_NAME}/{INTERACTION_NAME}",
                      "text": interaction_markdown(db, course, nodes)})
    return files


def summary(entries: list[dict] | None) -> dict:
    """清单预览 / 说明文件用的数字（未启用场景 ⇒ 全 0）。"""
    if not entries:
        return {"decks": 0, "scripts": 0, "subtitles": 0, "pages": 0, "seconds": 0, "sections": 0}
    pages = sum(len(entry["pages"]) for entry in entries)
    return {
        "decks": len(entries),
        "scripts": len(entries),
        "subtitles": len(entries),
        "pages": pages,
        "seconds": sum(page["seconds"] for entry in entries for page in entry["pages"]),
        "sections": sum(len(entry["sections"]) for entry in entries),
    }
