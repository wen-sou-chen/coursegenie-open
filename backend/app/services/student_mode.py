"""学生版：学习地图 → 微单元（含即时自测）→ 复习卡（**0 LLM**）。

只新增一条只读渲染路径；不改讲义正文、题目文字、PPTX 生成、交付包与讲师模式。
数据全部来自已有产物：大纲节点、讲义 markdown、QuizItem。
"""
from __future__ import annotations

import base64
import html as html_mod
import json
import re

from sqlalchemy.orm import Session

from app.models.course import Course, QuizItem
from app.services.lesson_plan import lecture_markdown_of
from app.services.planner import list_nodes

_RE_SOURCE_START = re.compile(r"^\s*来源\s*[:：]")
_RE_TITLE_NUM = re.compile(r"^\s*\d+(?:\.\d+)*[.、)．]?\s+")
_RE_BODY_NUM = re.compile(r"^\s*(?:\d+(?:\.\d+)+\s*|\d+[.、)．]\s+)")
_RE_BULLET = re.compile(r"^\s*(?:[-*+]\s+|[✓✔]\s+)")
_RE_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_RE_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_RE_LEFTOVER_SEG = re.compile(r"》第\s*\d+\s*段[）)]?")
_RE_LEFTOVER = re.compile(r"第\s*\d+\s*段[）)]?")
_RE_EMPTY_PAREN = re.compile(r"[（(]\s*[）)]")

_LEVEL_ORDER = {"基础": 0, "提升": 1, "挑战": 2}
_TYPE_ORDER = {"choice": 0, "blank": 1, "short": 2, "material": 2, "experiment": 2}
_UNIT_TITLES = (
    "先搞清楚：这到底是怎么回事",
    "怎么用：具体怎么做",
    "别踩坑：最容易错在哪",
)


def _matching_close(text: str, start: int) -> int:
    opener = text[start]
    closer = "）" if opener == "（" else ")"
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return index
    return -1


def strip_sources(text: object) -> str:
    """删掉 ``（来源:…）``，支持全角/半角与嵌套括号（任务 151）。

    来源标记里的括号会嵌套，例如 ``（来源:《理论-总览（多菌共生）》第 3 段）``；
    只用非贪婪正则会在第一个 ``）`` 处截断，留下 ``第 3 段）`` / ``》第`` 碎片，
    所以这里按括号深度配平扫描。其余括号原样保留。
    """
    source = str(text or "")
    out: list[str] = []
    index = 0
    while index < len(source):
        if source[index] in "（(":
            end = _matching_close(source, index)
            if end != -1 and _RE_SOURCE_START.match(source[index + 1:end]):
                index = end + 1
                continue
        out.append(source[index])
        index += 1
    cleaned = "".join(out)
    cleaned = _RE_EMPTY_PAREN.sub("", cleaned)
    cleaned = _RE_LEFTOVER_SEG.sub("》", cleaned)
    return _RE_LEFTOVER.sub("", cleaned)


def clean(text: object, *, title: bool = False) -> str:
    """讲义行 → 学生版展示文本：去来源、图片、链接、项目符号与编号。"""
    value = strip_sources(text)
    value = _RE_IMAGE.sub("", value)
    value = _RE_LINK.sub(r"\1", value)
    value = value.replace("✓", "").replace("✔", "")
    value = value.strip()
    value = _RE_BULLET.sub("", value)
    value = (_RE_TITLE_NUM if title else _RE_BODY_NUM).sub("", value)
    return value.strip()


def split_sections(markdown: object) -> dict[str, list[str]]:
    """按 ``###`` 拆讲义小节；``####`` 只作为上一小节的子标题，不单独开小节。"""
    out: dict[str, list[str]] = {}
    current = ""
    for raw in str(markdown or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        match3 = re.match(r"^###\s+(.+?)\s*$", stripped)
        if match3:
            current = match3.group(1).strip()
            out.setdefault(current, [])
            continue
        match4 = re.match(r"^####\s+(.+?)\s*$", stripped)
        if match4:
            if current:
                out.setdefault(current, []).append(match4.group(1).strip())
            continue
        if re.match(r"^##\s+", stripped):
            current = ""
            continue
        if not current or not stripped or stripped.startswith(("---", "!", "|")):
            continue
        out[current].append(stripped)
    return out


def bullets(lines: list[str]) -> list[str]:
    """小节正文 → 去重前的要点列表（保留讲义措辞，不新编）。"""
    got: list[str] = []
    for line in lines:
        value = clean(line)
        if not value or value.startswith("#"):
            continue
        got.append(value)
    return got


def first_sentence(text: object, limit: int = 46, avoid: str = "") -> str:
    """取前 ≤2 句；不把句子切一半；与要点标题相同则返回空串让其顺延。"""
    value = clean(text)
    if not value:
        return ""
    if avoid and value.strip() == clean(avoid, title=True).strip():
        return ""
    parts = [part for part in re.split(r"(?<=[。；！？])", value) if part.strip()]
    if not parts:
        return value
    out = ""
    for index, part in enumerate(parts):
        if index >= 2:
            break
        out += part
        if len(out) >= limit:
            break
    return out.strip()


def _b64(text: object) -> str:
    return base64.b64encode(str(text or "").encode("utf-8")).decode("ascii")


def _qtype(quiz: QuizItem) -> str:
    raw = str(quiz.qtype or "")
    if raw == "choice":
        return "choice"
    if raw == "blank":
        return "fill"
    return "short"


def _quiz_order(quiz: QuizItem) -> tuple[int, int, str]:
    level = str((quiz.meta or {}).get("level") or "")
    return (
        _LEVEL_ORDER.get(level, 9),
        _TYPE_ORDER.get(str(quiz.qtype or ""), 9),
        str(quiz.id or ""),
    )


def _answer_text(quiz: QuizItem, options: list[str]) -> tuple[str, int]:
    raw = str(quiz.answer or "").strip()
    if quiz.qtype == "choice":
        try:
            index = int(raw)
        except (TypeError, ValueError):
            index = -1
        if 0 <= index < len(options):
            return options[index], index
        text = clean(raw)
        try:
            return text, options.index(text)
        except ValueError:
            return text, -1
    if raw:
        return clean(raw), -1
    rubric = clean(str((quiz.meta or {}).get("rubric") or ""))
    return rubric, -1


def _quiz_payload(quiz: QuizItem) -> dict:
    """一道题 → 学生版 JSON；答案/解析只以 base64 存在，初始 HTML 不出现原文。"""
    qtype = _qtype(quiz)
    options = [clean(str(item)) for item in (quiz.options or []) if str(item).strip()]
    answer_text, correct = _answer_text(quiz, options)
    explanation = clean(quiz.explanation)
    return {
        "type": qtype,
        "type_label": {"choice": "选择题", "fill": "填空题", "short": "简答题"}[qtype],
        "stem": clean(quiz.question),
        "options": [{"text": item} for item in options] if qtype == "choice" else [],
        "correct_b64": _b64(str(correct)) if qtype == "choice" and correct >= 0 else "",
        "answer_b64": _b64(answer_text),
        "explain_b64": _b64(explanation),
    }


def _chapter_points(chapters: list, points: list) -> list[tuple[object, list]]:
    """章 → 直接挂在该章下的知识点（按大纲顺序，不跨章重复）。"""
    result: list[tuple[object, list]] = []
    used: set[str] = set()
    for chapter in chapters:
        key = str(chapter.node_key or "")
        prefix = key.split(".")[0]
        attached = [
            point for point in points
            if str(point.node_key or "").split(".")[0] == prefix and point.id not in used
        ]
        if not attached:
            continue
        used.update(point.id for point in attached)
        result.append((chapter, attached))
    return result


def build_course_data(db: Session, course: Course) -> dict:
    """整门课 → 学生版数据（3 章 / 6 知识点 / 18 单元 / 18 题 / 6 卡）。"""
    nodes = list_nodes(db, course.id)
    chapters = [node for node in nodes if node.node_type in ("chapter", "section")]
    points = [node for node in nodes if node.node_type == "point"]
    if not chapters or not points:
        raise ValueError("本课还没有可用的大纲章节与知识点，无法生成学生版")

    grouped = _chapter_points(chapters, points)
    if not grouped:
        raise ValueError("本课大纲里的知识点没有挂到任何章节，无法生成学生版")

    quizzes: dict[str, list[QuizItem]] = {}
    for quiz in (db.query(QuizItem).filter(QuizItem.course_id == course.id).all()):
        if quiz.node_id:
            quizzes.setdefault(quiz.node_id, []).append(quiz)
    for rows in quizzes.values():
        rows.sort(key=_quiz_order)

    data_chapters: list[dict] = []
    cards: list[dict] = []
    for chapter, attached in grouped:
        data_points: list[dict] = []
        for point in attached:
            point_title = clean(point.title, title=True)
            markdown = lecture_markdown_of(db, course, point)
            if not markdown.strip():
                raise ValueError(f"知识点《{point_title}》还没有讲义，无法生成学生版")
            sections = split_sections(markdown)
            intro = bullets(sections.get("引入") or sections.get("引言") or [])
            core = bullets(sections.get("核心讲解") or [])
            example = bullets(sections.get("举例") or [])
            pitfall = bullets(sections.get("踩过的坑") or [])
            summary = bullets(sections.get("小结") or [])
            boundary = (
                bullets(sections.get("能力边界") or [])
                or bullets(sections.get("本节不覆盖什么") or [])
                or bullets(sections.get("边界") or [])
            )

            middle = max(1, len(core) // 2)
            unit_points = [
                (intro[:2] + core[:middle])[:4],
                core[middle:middle + 4] or core[:3],
                (pitfall[:2] + summary[:2])[:4],
            ]
            one_lines = [
                first_sentence(intro[0] if intro else "", avoid=point_title)
                or first_sentence(core[0] if core else "", avoid=point_title)
                or point_title,
                first_sentence(
                    core[middle] if len(core) > middle else (core[0] if core else ""),
                    avoid=point_title,
                ) or point_title,
                first_sentence(pitfall[0] if pitfall else "", avoid=point_title)
                or first_sentence(summary[0] if summary else "", avoid=point_title)
                or point_title,
            ]
            point_quizzes = quizzes.get(point.id, [])
            units: list[dict] = []
            for index, title in enumerate(_UNIT_TITLES):
                quiz = point_quizzes[index] if index < len(point_quizzes) else None
                units.append({
                    "title": title,
                    "one_line": one_lines[index],
                    "points": unit_points[index],
                    "example": example[0] if index == 1 and example else "",
                    "pitfall": boundary[0] if index == 2 and boundary else "",
                    "quiz": _quiz_payload(quiz) if quiz is not None else None,
                })
            data_points.append({"title": point_title, "units": units})

            last_points = units[2]["points"]
            back = first_sentence(
                last_points[0] if last_points else units[2]["one_line"], limit=120
            ) or units[2]["one_line"]
            cards.append({
                "front": f"《{point_title}》——这一节要记住什么？",
                "back": back,
            })

        data_chapters.append({
            "title": clean(chapter.title, title=True),
            "minutes": 6 * len(data_points),
            "points": data_points,
        })

    total_units = sum(len(point["units"]) for chapter in data_chapters
                      for point in chapter["points"])
    total_quizzes = sum(1 for chapter in data_chapters for point in chapter["points"]
                        for unit in point["units"] if unit["quiz"])
    return {
        "course_id": course.id,
        "course": course.title,
        "chapters": data_chapters,
        "cards": cards,
        "total_units": total_units,
        "total_quizzes": total_quizzes,
    }


def render_student_html(db: Session, course: Course) -> str:
    """整门课 → 单文件 HTML（CSS / JS / 数据全内联，离线可开，0 LLM）。

    初始 HTML 里只放 base64 的整份数据；答案与解析要等用户作答后才在 JS 里解码，
    因此答案标记与答案原文都不会出现在原始 HTML 中。
    """
    data = build_course_data(db, course)
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    data_b64 = base64.b64encode(raw).decode("ascii")
    title = html_mod.escape(course.title or "学生版")
    return _HTML.replace("__TITLE__", title).replace("__DATA_B64__", data_b64)


_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ · 学生版</title>
<style>
  :root{--ink:#0F172A;--muted:#64748B;--line:#E2E8F0;--accent:#2563EB;--soft:#EFF4FF;
        --ok:#059669;--bad:#DC2626;--card:#FFFFFF;--bg:#F5F7FB}
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  html,body{margin:0;padding:0;max-width:100%;overflow-x:hidden}
  body{background:var(--bg);color:var(--ink);font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif;line-height:1.65}
  .wrap{width:100%;max-width:760px;margin:0 auto;padding:16px 16px 60px}
  .top{position:sticky;top:0;z-index:20;background:var(--bg);padding:10px 0 8px;border-bottom:1px solid var(--line)}
  .top h1{margin:0 0 6px;font-size:17px;font-weight:800;overflow-wrap:anywhere}
  .top .bar{height:6px;background:#E5EAF3;border-radius:99px;overflow:hidden}
  .top .bar i{display:block;height:100%;width:0;background:var(--accent);transition:width .3s}
  .top .meta{display:flex;justify-content:space-between;align-items:center;gap:8px;
             font-size:12px;color:var(--muted);margin-top:4px}
  .where{display:inline-flex;align-items:center;gap:8px;flex:0 0 auto}
  .chip{border:1px solid var(--line);background:#fff;border-radius:999px;padding:3px 10px;
        font-size:12px;cursor:pointer;font-family:inherit;color:var(--accent)}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin:12px 0;
        box-shadow:0 1px 2px rgba(15,23,42,.04);overflow-wrap:anywhere}
  .ch h2{margin:0 0 4px;font-size:16px}
  .ch .sub{color:var(--muted);font-size:13px;margin-bottom:10px}
  .pt{display:flex;align-items:center;gap:10px;padding:10px 12px;border:1px solid var(--line);border-radius:12px;
      margin:8px 0;cursor:pointer;background:#fff}
  .pt:hover{border-color:var(--accent);background:var(--soft)}
  .pt .n{flex:0 0 auto;width:26px;height:26px;border-radius:50%;background:#EFF4FF;color:var(--accent);
         font-size:12px;font-weight:700;display:flex;align-items:center;justify-content:center}
  .pt.done .n{background:var(--ok);color:#fff}
  .pt .t{flex:1 1 auto;font-size:14px;min-width:0;overflow-wrap:anywhere}
  .pt .s{flex:0 0 auto;font-size:12px;color:var(--muted)}
  .unit .kicker{font-size:12px;color:var(--accent);font-weight:700;letter-spacing:.06em}
  .unit h3{margin:4px 0 10px;font-size:19px;line-height:1.35;overflow-wrap:anywhere}
  .one{margin:0 0 14px;padding:12px 14px;background:var(--soft);border-radius:10px;font-size:14px;overflow-wrap:anywhere}
  .one b{color:var(--accent)}
  ul.points{margin:0 0 6px;padding-left:18px}
  ul.points li{margin:6px 0;font-size:14.5px;overflow-wrap:anywhere}
  .eg{margin:10px 0;padding:10px 12px;border-left:3px solid #8B5CF6;background:#FAF7FF;border-radius:0 8px 8px 0;font-size:14px;overflow-wrap:anywhere}
  .warn{margin:10px 0;padding:10px 12px;border-left:3px solid #F59E0B;background:#FFFBF0;border-radius:0 8px 8px 0;font-size:14px;overflow-wrap:anywhere}
  .q{margin-top:16px;padding-top:14px;border-top:1px dashed var(--line)}
  .q .ask{font-size:15px;font-weight:600;margin-bottom:10px;overflow-wrap:anywhere}
  .opt{display:block;width:100%;text-align:left;padding:11px 13px;margin:7px 0;border:1px solid var(--line);
       border-radius:10px;background:#fff;font-size:14.5px;cursor:pointer;font-family:inherit;color:var(--ink);
       white-space:normal;overflow-wrap:anywhere}
  .opt:hover{border-color:var(--accent)}
  .opt.ok{border-color:var(--ok);background:#ECFDF5}
  .opt.no{border-color:var(--bad);background:#FEF2F2}
  .opt[disabled]{cursor:default}
  .fill{margin:10px 0;display:flex;gap:8px;flex-wrap:wrap}
  .fill input{flex:1 1 160px;min-width:0;padding:10px 12px;border:1px solid var(--line);border-radius:10px;
              font-size:14.5px;font-family:inherit}
  .fill button{padding:0 16px;border:none;border-radius:10px;background:var(--accent);color:#fff;font-size:14px;cursor:pointer}
  .ans{display:none;margin:10px 0;padding:11px 13px;background:#F8FAFF;border-radius:10px;font-size:14px;overflow-wrap:anywhere}
  .ans.on{display:block}
  .ans b{color:var(--accent)}
  .acts{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap}
  .btn{padding:10px 16px;border-radius:10px;border:1px solid var(--line);background:#fff;font-size:14px;cursor:pointer;
       font-family:inherit;color:var(--ink);max-width:100%}
  .btn.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
  .btn.ghost{color:var(--muted)}
  .status{margin-top:10px;font-size:12.5px;color:var(--muted)}
  .status.on{color:var(--ok);font-weight:600}
  .fc{perspective:900px;margin:10px 0;cursor:pointer}
  .fc .in{position:relative;min-height:104px;transition:transform .45s;transform-style:preserve-3d}
  .fc.flip .in{transform:rotateY(180deg)}
  .fc .side{position:absolute;inset:0;backface-visibility:hidden;border:1px solid var(--line);border-radius:12px;
            padding:14px;background:#fff;font-size:14px;display:flex;align-items:center;overflow-wrap:anywhere}
  .fc .back{transform:rotateY(180deg);background:#F8FAFF}
  .hidden{display:none}
  .tip{font-size:12.5px;color:var(--muted);margin-top:8px}
  @media (max-width:420px){
    .wrap{padding:12px 10px 48px}
    .top h1{font-size:16px}
    .card{padding:13px;border-radius:12px}
    .unit h3{font-size:17px}
    .meta{font-size:11.5px}
    .btn{padding:9px 12px}
    .fill button{width:100%}
  }
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <h1 id="title">—</h1>
    <div class="bar"><i id="bar"></i></div>
    <div class="meta">
      <span id="progress">—</span>
      <span class="where"><span id="where">地图</span><button class="chip" onclick="renderCards()">复习卡</button></span>
    </div>
  </div>
  <div id="app"></div>
  <noscript><p class="tip">学生版需要浏览器启用 JavaScript 才能显示地图与自测。</p></noscript>
</div>
<script>
const DATA_B64 = "__DATA_B64__";
function b64(value){
  if (!value) return "";
  const bin = atob(value);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new TextDecoder("utf-8").decode(bytes);
}
const DATA = JSON.parse(b64(DATA_B64));
const KEY = "cg-student-" + DATA.course_id;
let state = {};
try { state = JSON.parse(localStorage.getItem(KEY) || "{}") || {}; } catch (e) { state = {}; }
state.done = state.done || {};
state.cards = state.cards || {};
function save(){
  try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {}
}
const $ = id => document.getElementById(id);
const esc = value => String(value == null ? "" : value)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
const md = value => esc(value).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
function unitKey(ci, pi, ui){ return ci + "-" + pi + "-" + ui; }
function totalUnits(){
  return DATA.chapters.reduce((a, ch) => a + ch.points.reduce((b, p) => b + p.units.length, 0), 0);
}
function doneUnits(){
  let count = 0;
  DATA.chapters.forEach((ch, ci) => ch.points.forEach((p, pi) => p.units.forEach((_, ui) => {
    if (state.done[unitKey(ci, pi, ui)]) count++;
  })));
  return count;
}
function paintChrome(){
  const total = totalUnits();
  const done = doneUnits();
  $("title").textContent = DATA.course;
  $("bar").style.width = (total ? done / total * 100 : 0) + "%";
  $("progress").textContent = `已学 ${done}/${total} 个单元`;
}
function currentUnit(){
  const ch = DATA.chapters[cur.ci];
  const point = ch && ch.points[cur.pi];
  return point && point.units[cur.ui];
}
function showAnswer(quiz){
  const box = $("ans");
  if (!box) return;
  const answer = b64(quiz.answer_b64);
  const explain = b64(quiz.explain_b64);
  const label = quiz.type === "short" ? "参考答案" : "答案";
  let html = "";
  if (answer) html += `<b>${label}：</b>${md(answer)}`;
  if (explain) html += (answer ? "<br>" : "") + `<span>${md(explain)}</span>`;
  box.innerHTML = html || "（本题暂无参考答案）";
  box.classList.add("on");
}
function markDone(){
  if (!cur) return;
  state.done[unitKey(cur.ci, cur.pi, cur.ui)] = true;
  save();
  paintChrome();
  const flag = $("doneFlag");
  if (flag) { flag.textContent = "已学完 · 答对后已记录"; flag.classList.add("on"); }
  const next = $("next");
  if (next) next.classList.add("primary");
}
function renderMap(){
  $("where").textContent = "地图";
  const html = DATA.chapters.map((ch, ci) => {
    const chapterUnits = ch.points.reduce((a, p) => a + p.units.length, 0);
    const chapterDone = ch.points.reduce((a, p, pi) => a + p.units.filter(
      (_, ui) => state.done[unitKey(ci, pi, ui)]).length, 0);
    const points = ch.points.map((p, pi) => {
      const done = p.units.every((_, ui) => state.done[unitKey(ci, pi, ui)]);
      return `<div class="pt ${done ? "done" : ""}" onclick="openUnit(${ci},${pi},0)">
        <span class="n">${done ? "✓" : pi + 1}</span>
        <span class="t">${esc(p.title)}</span>
        <span class="s">${p.units.length} 单元</span></div>`;
    }).join("");
    return `<div class="card ch">
      <h2>${esc(ch.title)}</h2>
      <div class="sub">${ch.points.length} 个知识点 · ${chapterUnits} 个单元 · ${ch.minutes} 分钟
        · 已学 ${chapterDone}/${chapterUnits}</div>
      ${points}
    </div>`;
  }).join("");
  $("app").innerHTML = html +
    `<div class="acts"><button class="btn ghost" onclick="renderCards()">复习卡 →</button></div>
     <p class="tip">学完每个单元都要答对一道题才算过；答错会显示解析，回讲义对应小节重看。</p>`;
  paintChrome();
}
let cur = null;
function openUnit(ci, pi, ui){
  const ch = DATA.chapters[ci], point = ch && ch.points[pi], unit = point && point.units[ui];
  if (!unit) { renderMap(); return; }
  cur = {ci, pi, ui};
  $("where").textContent = `第${ci + 1}章 · 单元 ${ui + 1}/${point.units.length}`;
  const quiz = unit.quiz;
  let quizHtml = "";
  if (quiz && quiz.type === "choice" && quiz.options.length) {
    quizHtml = `<div class="q">
      <div class="ask">${md(quiz.stem)}</div>
      ${quiz.options.map((option, index) =>
        `<button class="opt" onclick="pick(${index})">${esc(option.text)}</button>`).join("")}
      <div class="ans" id="ans"></div></div>`;
  } else if (quiz && quiz.type === "fill") {
    quizHtml = `<div class="q">
      <div class="ask">${md(quiz.stem)}</div>
      <div class="fill"><input id="fill" placeholder="填在这里"><button onclick="judgeFill()">对答案</button></div>
      <div class="ans" id="ans"></div></div>`;
  } else if (quiz) {
    quizHtml = `<div class="q">
      <div class="ask">${md(quiz.stem)}</div>
      <div class="acts"><button class="btn" onclick="revealShort()">先自己写，再看答案</button></div>
      <div class="ans" id="ans"></div></div>`;
  } else {
    quizHtml = `<div class="q"><div class="ask">本节没有随堂练习</div>
      <div class="acts"><button class="btn" onclick="markDone()">我已学完（无自测）</button></div></div>`;
  }
  const done = !!state.done[unitKey(ci, pi, ui)];
  $("app").innerHTML = `<div class="card unit">
    <div class="kicker">单元 ${ui + 1} / ${point.units.length}　·　${esc(point.title)}</div>
    <h3>${esc(unit.title)}</h3>
    <div class="one"><b>一句话：</b>${md(unit.one_line)}</div>
    <ul class="points">${unit.points.map(item => `<li>${md(item)}</li>`).join("")}</ul>
    ${unit.example ? `<div class="eg"><b>例子　</b>${md(unit.example)}</div>` : ""}
    ${unit.pitfall ? `<div class="warn"><b>边界　</b>${md(unit.pitfall)}</div>` : ""}
    ${quizHtml}
    <div class="status ${done ? "on" : ""}" id="doneFlag">${done ? "已学完 · 答对后已记录" : "答对这道题，本单元才算学完"}</div>
    <div class="acts">
      <button class="btn ghost" onclick="renderMap()">← 回地图</button>
      ${ui + 1 < point.units.length
        ? `<button class="btn primary" id="next" onclick="openUnit(${ci},${pi},${ui + 1})">下一单元 →</button>`
        : `<button class="btn primary" id="next" onclick="backToMap()">这一节学完了 ✓</button>`}
    </div>
  </div>`;
  paintChrome();
}
function pick(index){
  const unit = currentUnit();
  if (!unit || !unit.quiz) return;
  const quiz = unit.quiz;
  const correct = quiz.correct_b64 ? Number(b64(quiz.correct_b64)) : -1;
  document.querySelectorAll(".opt").forEach((button, key) => {
    button.disabled = true;
    if (key === correct) button.classList.add("ok");
    else if (key === index) button.classList.add("no");
  });
  showAnswer(quiz);
  if (index === correct) markDone();
}
function judgeFill(){
  const unit = currentUnit();
  if (!unit || !unit.quiz) return;
  const input = $("fill");
  const value = input ? input.value.trim() : "";
  const want = b64(unit.quiz.answer_b64).trim();
  showAnswer(unit.quiz);
  if (value && want && want.includes(value)) markDone();
}
function revealShort(){
  const unit = currentUnit();
  if (!unit || !unit.quiz) return;
  showAnswer(unit.quiz);
  markDone();
}
function backToMap(){
  renderMap();
  window.scrollTo({top: 0, behavior: "smooth"});
}
function renderCards(){
  $("where").textContent = "复习卡";
  const known = DATA.cards.filter((_, index) => !!state.cards[index]).length;
  const cards = DATA.cards.map((card, index) => {
    const isKnown = !!state.cards[index];
    return `<div class="fc ${isKnown ? "hidden" : ""}" id="fc${index}" onclick="flip(${index})">
      <div class="in"><div class="side front">${md(card.front)}</div>
      <div class="side back">${md(card.back)}</div></div></div>`;
  }).join("");
  $("app").innerHTML = `<div class="card">
    <h2 style="margin:0 0 6px;font-size:16px">复习卡（${known}/${DATA.cards.length} 已记住）</h2>
    <div class="tip">点卡片翻面。翻过来能自己说出来，就点「记住了」。</div>
    ${cards}
    <p class="tip ${known === DATA.cards.length ? "" : "hidden"}" id="allDone">全部记住了 🎉 过 3 天再回来刷一遍。</p>
  </div>
  <div class="acts"><button class="btn ghost" onclick="renderMap()">← 回地图</button></div>`;
  paintChrome();
}
function flip(index){
  const card = $("fc" + index);
  if (!card) return;
  card.classList.toggle("flip");
  if (!card.classList.contains("flip")) return;
  setTimeout(() => {
    if (!card.classList.contains("flip")) return;
    if (!confirm("记住了吗？\n\n确定 = 这张不再出现；取消 = 再放回去")) return;
    state.cards[index] = true;
    save();
    card.classList.add("hidden");
    const known = DATA.cards.filter((_, key) => !!state.cards[key]).length;
    if (known === DATA.cards.length) {
      const all = $("allDone");
      if (all) all.classList.remove("hidden");
    }
  }, 500);
}
(function(){
  const hash = location.hash.slice(1);
  if (!hash) { renderMap(); return; }
  const params = new URLSearchParams(hash);
  if (params.get("cards") === "1") { renderCards(); return; }
  const raw = params.get("u");
  if (raw) {
    const parts = raw.split("-").map(Number);
    const chapter = DATA.chapters[parts[0]];
    const point = chapter && chapter.points[parts[1]];
    const unit = point && point.units[parts[2]];
    if (unit) { openUnit(parts[0], parts[1], parts[2]); return; }
  }
  renderMap();
})();
</script>
</body>
</html>
"""
