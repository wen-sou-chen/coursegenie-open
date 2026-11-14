"""教案产出物（任务 49）：0 LLM 从大纲三要素 + 讲义正文渲染「老师能直接交」的表格式教案。

设计口径：
- **0 LLM 骨架**：全部栏目由该节点已有的 ``objective`` / ``teaching_strategy`` /
  ``assessment`` + 该节点讲义正文 + 该节点练习派生；可选 LLM 补细节（默认关，
  失败显式降级为骨架，见 :func:`generate_for_course`）。
- **逐条来源标注**：沿用讲义同一套口径 ``(来源: …)`` / ``(无直接依据,建议人工确认)``，
  不另造一套文案（``lecture_qc.NO_BASIS_MARK`` 是唯一真相源）。
- **缺项显式写原因**：栏目录不到内容时写清楚「为什么空、去哪补」，不留白坑老师。
- **幂等**：同一份输入恒得同一份 markdown（无时间戳、无随机数），重复生成不产生新条目。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.course import ContentItem, Course, OutlineNode, QuizItem
from app.services import k12 as k12_mod
from app.services import kb as kb_mod
from app.services import lecture_qc
from app.services.lecture_qc import NO_BASIS_MARK

logger = logging.getLogger(__name__)

KIND = "lesson_plan"
DIR_NAME = "教案"
SUFFIX = "教案"
DEFAULT_MINUTES = 40          # 一节课 40 分钟（小学）；初高中 45 分钟由 meta 覆盖
K12_MINUTES = {"高中": 45}
MAX_STEPS = 4                 # 教学过程最多拆几个讲解环节，避免课堂装不下

REFLECTION_PLACEHOLDER = ("（此栏留白，供授课后手写：目标达成度 / 学生典型问题 / "
                          "下次改进。系统不自动填写。）")

_RE_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_RE_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+?)\s*$")
_RE_TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_RE_EMPHASIS = re.compile(r"\*\*|__|\*|_|`")
# 只有标签没有内容的单元格：「教学难点 | 易错点： | …」这种行不留空标签（任务 48s）
_RE_LABEL_ONLY = re.compile(r"^[^：:，,。;；]{1,12}\s*[：:]\s*$")
_RE_SENT_SPLIT = re.compile(r"[。；;]\s*")
_RE_CELL_SPACE = re.compile(r"\s+")

_PIT_KEYS = ("踩过的坑", "踩坑", "易错", "常见错误", "注意事项", "失败", "返工", "教训")
_SUMMARY_KEYS = ("小结", "总结", "回顾")
_PRACTICE_KEYS = ("随堂练习", "练习", "习题")
_SKIP_KEYS = ("来源", "术语", "本章概念索引", "参考文献")


# ---------------------------------------------------------------------------
# 讲义结构解析
# ---------------------------------------------------------------------------

def _sections(markdown: str) -> list[tuple[str, list[str]]]:
    """讲义 → [(小节标题, 正文行)]，按出现顺序；标题外的正文归到 "" 段。"""
    out: list[tuple[str, list[str]]] = []
    current = ""
    body: list[str] = []
    for line in str(markdown or "").split("\n"):
        match = _RE_HEADING.match(line)
        if match:
            if body or current:
                out.append((current, body))
            current = match.group(2).strip()
            body = []
            continue
        if _RE_TABLE_SEP.match(line):
            continue
        body.append(line)
    if body or current:
        out.append((current, body))
    return out


def _first_sentence(text: str, limit: int = 60) -> str:
    cleaned = _RE_EMPHASIS.sub("", str(text or "")).strip()
    if not cleaned:
        return ""
    # 先按句末标点切；teaching_strategy 常写成「先…；再…」，取第一句即可
    for part in re.split(r"[。；;]\s*", cleaned):
        if part.strip():
            cleaned = part.strip()
            break
    return cleaned[:limit].rstrip("，,、 ") + ("…" if len(cleaned) > limit else "")


def _has_content(text: str) -> bool:
    """栏目正文有没有实际内容 —— 「易错点：」这种只有标签的行不算。"""
    value = str(text or "").strip()
    return bool(value) and not _RE_LABEL_ONLY.match(value)


def _bullets(lines: list[str], limit: int = 4) -> list[str]:
    out: list[str] = []
    for line in lines:
        match = _RE_BULLET.match(line)
        if not match:
            continue
        text = _RE_EMPHASIS.sub("", match.group(1)).strip()
        if _has_content(text) and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _find_section(sections: list[tuple[str, list[str]]], keys: tuple[str, ...]):
    for heading, body in sections:
        if any(key in heading for key in keys):
            return heading, body
    return None


def _prose(lines: list[str], limit: int = 120) -> str:
    """把一段正文压成一句白话（跳过表格行、图片、空行）。"""
    chunks: list[str] = []
    for line in lines:
        text = line.strip()
        if not text or text.startswith("|") or text.startswith("!["):
            continue
        # 去掉列表符号与引用符号：进表格单元格时「- 光反应…」会像一条断掉的列表
        text = _RE_EMPHASIS.sub("", _RE_BULLET.sub(r"\1", text.lstrip("> "))).strip()
        if text:
            chunks.append(text)
        if len("".join(chunks)) >= limit:
            break
    return "".join(chunks)[:limit]


# ---------------------------------------------------------------------------
# 各栏目
# ---------------------------------------------------------------------------

def _basic_rows(course: Course, node: OutlineNode) -> list[list[str]]:
    meta = k12_mod.k12_meta(course)
    textbook = " ".join(part for part in (meta.get("textbook"), meta.get("volume"),
                                          meta.get("unit")) if part)
    title = str(node.title or "").strip()
    lesson_type = "复习课" if any(key in title for key in ("复习", "练习", "习题")) else "新授课"
    minutes = K12_MINUTES.get(str(meta.get("stage") or ""), DEFAULT_MINUTES)
    return [
        ["课题", title or "（大纲未写课题，请人工补充）"],
        ["教材版本册次单元", textbook or "（未填写教材元数据：course.meta 的 textbook/volume/unit）"],
        ["年级", str(meta.get("grade") or "（未填写 course.meta.grade）")],
        ["课时", f"第 1 课时（{minutes} 分钟）"],
        ["授课类型", lesson_type],
        ["对应知识点", f"{node.node_key} {title}".strip()],
    ]


def _objective_rows(node: OutlineNode) -> list[list[str]]:
    raw = str(node.objective or "").strip()
    if not raw:
        return [["—", "（大纲未写课时目标，请人工补充）", NO_BASIS_MARK]]
    parts = [part.strip(" 　-*") for part in _RE_SENT_SPLIT.split(raw.replace("\n", "；"))]
    rows = [[str(idx), text, "(来源: 大纲·课时目标)"]
            for idx, text in enumerate((p for p in parts if p), 1)]
    return rows or [["1", raw, "(来源: 大纲·课时目标)"]]


def _key_diff_rows(lecture: str, objective: str) -> list[list[str]]:
    rows: list[list[str]] = []
    sections = _sections(lecture)
    focus = _find_section(sections, _PIT_KEYS)
    if focus is not None:
        heading, body = focus
        for text in _bullets(body, limit=3) or [_prose(body, 80)]:
            if _has_content(text):
                rows.append(["教学难点", text, f"(来源: 讲义「{heading}」)"])
    if objective:
        first = _first_sentence(objective, 50)
        if _has_content(first):
            rows.append(["教学重点", first, "(来源: 大纲·课时目标)"])
    if not rows:
        rows.append(["教学重点", "（讲义与大纲都没给出重点，请人工补充）", NO_BASIS_MARK])
    return rows


def _prep_rows(course: Course, db: Session, node: OutlineNode) -> list[list[str]]:
    items = (db.query(ContentItem)
             .filter(ContentItem.course_id == course.id, ContentItem.node_id == node.id).all())
    kinds = {str(item.kind) for item in items}
    rows = [["教材", "课本对应课文 / 单元页"],
            ["板书工具", "白板或黑板 + 粉笔 / 白板笔"]]
    if "ppt" in kinds:
        rows.append(["课件", "本课课件（交付包「课件/」目录）"])
    if kinds & {"diagram"}:
        rows.append(["概念图", "本知识点概念图（交付包「图表/」目录）"])
    if kinds & {"chart_bar", "chart_line", "chart_pie", "chart_table",
                "chart_mindmap", "chart_timeline", "chart_quadrant"}:
        rows.append(["数据图表", "本知识点数据图表（交付包「图表/」目录）"])
    if "image" in kinds:
        rows.append(["图片", "本知识点配图（笔记库 assets/ 目录）"])
    return rows


def _process_rows(course: Course, node: OutlineNode, lecture: str,
                  quizzes: list[QuizItem]) -> list[list[str]]:
    """教学过程：导入 → 讲解（按讲义小节）→ 随堂练习 → 课堂小结，时间合计一课时。

    任务 48s：**每一句都带本课时自己的信息**（课时标题 / 小节标题 / 该节正文首句 /
    节点号）。此前「教师活动 / 学生活动 / 设计意图」是三段固定句式，同一门课的几十份
    教案叠在一起时逐字重复 —— 老师一眼看出是机器套的，跨课时也看不出区别。
    """
    meta = k12_mod.k12_meta(course)
    total = K12_MINUTES.get(str(meta.get("stage") or ""), DEFAULT_MINUTES)
    lead_in, wrap_up, practice_min = 5, 5, 10
    rows: list[list[str]] = []
    title = str(node.title or "").strip() or "本课"
    key = str(node.node_key or "").strip()

    strategy = _first_sentence(str(node.teaching_strategy or ""), 70)
    rows.append([
        "导入", f"{lead_in} 分钟",
        f"用一句话引出课题《{title}》" + (f"：{strategy}" if strategy else ""),
        f"说说自己已经知道的与《{title}》有关的内容，把想问的问题提出来",
        f"唤起旧经验、暴露前概念，把学生的注意力引到《{title}》要解决的问题上",
        f"(来源: 大纲 {key} 的教学策略)" if strategy else NO_BASIS_MARK,
    ])

    sections = [(head, body) for head, body in _sections(lecture)
                if head and not any(key in head for key in
                                    _PIT_KEYS + _SUMMARY_KEYS + _PRACTICE_KEYS + _SKIP_KEYS)]
    sections = sections[:MAX_STEPS]
    teaching_min = max(0, total - lead_in - wrap_up - practice_min)
    if sections:
        base, extra = divmod(teaching_min, len(sections))
        for index, (heading, body) in enumerate(sections):
            minutes = base + (1 if index < extra else 0)
            gist = _prose(body, 70) or f"围绕《{title}》的「{heading}」组织讲授与提问"
            rows.append([
                heading, f"{minutes} 分钟",
                f"围绕《{title}》的「{heading}」，先讲清关键结论再用板书/课件呈现要点；{gist}",
                f"边听边在《{title}》讲义上圈画「{heading}」的关键词，同桌互相复述一遍",
                f"把《{title}》的「{heading}」接进本课的知识结构，能用自己的话说出来",
                f"(来源: 讲义 {key}「{heading}」)",
            ])
    else:
        rows.append([
            "核心讲解", f"{teaching_min} 分钟",
            "（本知识点还没有讲义正文，先在第 4 步生成讲义再来补教学过程）",
            "—",
            "—",
            NO_BASIS_MARK,
        ])

    practice = _find_section(_sections(lecture), _PRACTICE_KEYS)
    if quizzes:
        detail = f"用《{title}》的 {len(quizzes)} 道随堂练习当堂检测，逐题讲评"
        student = f"独立完成《{title}》的 {len(quizzes)} 道题，同桌互批后全班订正"
        source = "(来源: 本节练习)"
    elif practice is not None:
        detail = f"用《{title}》讲义「{practice[0]}」里的题目当堂检测，逐题讲评"
        student = f"独立完成《{title}》讲义「{practice[0]}」里的题目，同桌互批后全班订正"
        source = f"(来源: 讲义 {key}「{practice[0]}」)"
    else:
        detail = f"（《{title}》未配置练习，建议按教学目标自行补 1–2 题）"
        student = f"《{title}》暂无现成练习，改为课堂追问与板演，同桌互评"
        source = NO_BASIS_MARK
    rows.append(["随堂练习", f"{practice_min} 分钟", detail,
                 student, f"当堂检测《{title}》的目标是否达成", source])

    summary = _find_section(_sections(lecture), _SUMMARY_KEYS)
    if summary is not None:
        gist = _prose(summary[1], 60) or f"回顾《{title}》的要点"
        rows.append(["课堂小结", f"{wrap_up} 分钟", gist,
                     f"用自己的话复述《{title}》的要点，写下 1 条仍然存疑的问题",
                     f"把《{title}》的零散知识收成一个结构",
                     f"(来源: 讲义 {key}「{summary[0]}」)"])
    else:
        rows.append(["课堂小结", f"{wrap_up} 分钟",
                     f"带着学生一起复述《{title}》的要点",
                     f"用自己的话复述《{title}》的 1 条要点，写下仍然存疑的问题",
                     f"把《{title}》的零散知识收成一个结构，强化记忆、暴露未解决的问题",
                     NO_BASIS_MARK])
    return rows


def _board_lines(db: Session, course: Course, node: OutlineNode, lecture: str) -> list[str]:
    diagrams = (db.query(ContentItem)
                .filter(ContentItem.course_id == course.id, ContentItem.node_id == node.id,
                        ContentItem.kind == "diagram").count())
    lines: list[str] = []
    summary = _find_section(_sections(lecture), _SUMMARY_KEYS)
    points = _bullets(summary[1], limit=4) if summary is not None else []
    if not points:
        points = lecture_qc.key_points(lecture)[:4] if lecture else []
    if points:
        lines += [f"- {text}" for text in points]
    elif node.objective:
        lines += [f"- {text}" for text in
                  (p.strip() for p in _RE_SENT_SPLIT.split(node.objective) if p.strip())][:4]
    if diagrams:
        lines.append(f"- 概念图：图表库「图-概念图-{node.node_key}」（本课板书可直接照此展开）")
    if not lines:
        lines.append(f"- {NO_BASIS_MARK}（讲义与大纲都没有可上板的要点）")
    return lines


def _homework_lines(db: Session, course: Course, node: OutlineNode,
                    quizzes: list[QuizItem]) -> list[str]:
    if not quizzes:
        if any(key in str(node.title or "") for key in _PRACTICE_KEYS):
            return [f"- {NO_BASIS_MARK}（本节标题是练习课，但库里还没有题目，请先补充练习）"]
        return [f"- {NO_BASIS_MARK}（本节未配置练习，建议按教学目标补 1–2 道题后再布置）"]
    lines: list[str] = []
    for idx, quiz in enumerate(quizzes[:8], 1):
        question = _RE_EMPHASIS.sub("", str(quiz.question or "")).strip().replace("\n", " ")
        lines.append(f"- 第 {idx} 题（{quiz.qtype} / {quiz.difficulty}）：{question[:60]}"
                     + ("…" if len(question) > 60 else ""))
    lines.append("(来源: 本节练习)")
    return lines


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

def _cell(value) -> str:
    """表格单元格文本：换行 / 连续空白压成单个空格（任务 48s）。

    大纲的 ``teaching_strategy`` 常写成多行（``1. … \\n 2. …``），原样塞进单元格会把一条
    表行拆成好几行 —— 老师的 Word 里表格会错位，机器按行审也会把里面的句子当成独立句。
    """
    return _RE_CELL_SPACE.sub(" ", str(value if value is not None else "")).strip()


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(_cell(text) for text in headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    out += ["| " + " | ".join(_cell(cell) for cell in row) + " |" for row in rows]
    out.append("")
    return out


def plan_markdown(db: Session, course: Course, node: OutlineNode,
                  lecture_markdown: str = "") -> str:
    """渲染单个知识点的教案（纯函数式：同输入恒得同输出，不写库、不看时间）。"""
    lecture = str(lecture_markdown or "")
    quizzes = (db.query(QuizItem)
               .filter(QuizItem.course_id == course.id, QuizItem.node_id == node.id)
               .order_by(QuizItem.created_at).all())
    parts = [
        f"# 《{node.title}》教案", "",
        f"> 自动骨架（0 LLM）：由大纲的课时目标 / 教学策略 / 评估方式与讲义正文派生，"
        f"栏目录不到内容时已显式写明原因。教师可直接在 Word 里增删。", "",
        "## 一、基本信息", "",
        *_table(["项目", "内容"], _basic_rows(course, node)),
        "## 二、教学目标", "",
        *_table(["序号", "目标", "来源"], _objective_rows(node)),
        "## 三、教学重点与难点", "",
        *_table(["类型", "内容", "来源"], _key_diff_rows(lecture, str(node.objective or ""))),
        "## 四、教学准备", "",
        *_table(["项目", "内容"], _prep_rows(course, db, node)),
        "## 五、教学过程", "",
        *_table(["环节", "时间", "教师活动", "学生活动", "设计意图", "来源"],
                _process_rows(course, node, lecture, quizzes)),
    ]
    assessment = str(node.assessment or "").strip()
    parts += ["## 六、评估方式（怎么知道学生会不会）", "",
              f"- {assessment}" if assessment else f"- {NO_BASIS_MARK}",
              "", ""]
    parts += ["## 七、板书设计", "", *_board_lines(db, course, node, lecture), ""]
    parts += ["## 八、作业设计", "", *_homework_lines(db, course, node, quizzes), ""]
    parts += ["## 九、教学反思", "", REFLECTION_PLACEHOLDER, ""]
    return "\n".join(parts).rstrip() + "\n"


# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------

def lecture_markdown_of(db: Session, course: Course, node: OutlineNode) -> str:
    """该节点最近一篇讲义的正文（没有则空串）。"""
    item = (db.query(ContentItem)
            .filter(ContentItem.course_id == course.id, ContentItem.node_id == node.id,
                    ContentItem.kind == "lecture")
            .order_by(ContentItem.created_at).all())
    if not item:
        return ""
    return lecture_qc.lecture_markdown(item[-1])


def existing(db: Session, course: Course, node: OutlineNode) -> ContentItem | None:
    rows = (db.query(ContentItem)
            .filter(ContentItem.course_id == course.id, ContentItem.node_id == node.id,
                    ContentItem.kind == KIND)
            .order_by(ContentItem.created_at).all())
    return rows[-1] if rows else None


def write_document(db: Session, course: Course, node: OutlineNode, *, kind: str,
                   title: str, markdown: str, meta_extra: dict | None = None) -> dict:
    """落库一份「按知识点一页」的文档条目（教案 / 学案共用）。

    幂等：正文没变则**不新增条目、不刷新 ``generated_at``**；返回
    ``{item_id, created, changed}``。
    """
    body_meta = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "sources": ["outline", "lecture", "quiz"], **(meta_extra or {})}
    rows = (db.query(ContentItem)
            .filter(ContentItem.course_id == course.id, ContentItem.node_id == node.id,
                    ContentItem.kind == kind)
            .order_by(ContentItem.created_at).all())
    item = rows[-1] if rows else None
    if item is None:
        item = ContentItem(course_id=course.id, node_id=node.id, kind=kind, title=title,
                           body={"markdown": markdown}, meta=body_meta)
        db.add(item)
        db.flush()
        return {"item_id": item.id, "created": True, "changed": True}
    previous = str((item.body or {}).get("markdown") or "")
    item.title = title
    item.body = {**(item.body or {}), "markdown": markdown}
    merged = dict(item.meta or {})
    merged.update({key: value for key, value in body_meta.items() if key != "generated_at"})
    merged.setdefault("generated_at", body_meta["generated_at"])
    if previous != markdown:
        merged["generated_at"] = body_meta["generated_at"]
    item.meta = merged
    db.flush()
    return {"item_id": item.id, "created": False, "changed": previous != markdown}


def list_documents(db: Session, course: Course, kind: str) -> list[dict]:
    """已落库的文档条目（按大纲顺序），供网页端列表与自检使用。"""
    from app.services.planner import list_nodes

    nodes = list_nodes(db, course.id)
    order = {node.id: index for index, node in enumerate(nodes)}
    by_id = {node.id: node for node in nodes}
    items = (db.query(ContentItem)
             .filter(ContentItem.course_id == course.id, ContentItem.kind == kind).all())
    rows: list[dict] = []
    for item in items:
        node = by_id.get(item.node_id or "")
        markdown = str((item.body or {}).get("markdown") or "")
        rows.append({
            "item_id": item.id,
            "node_id": item.node_id,
            "node_key": getattr(node, "node_key", "") or "",
            "title": item.title,
            "chars": len(markdown),
            "generated_at": (item.meta or {}).get("generated_at") or "",
        })
    rows.sort(key=lambda row: order.get(row["node_id"] or "", 10 ** 6))
    return rows


def list_plans(db: Session, course: Course) -> list[dict]:
    """已落库的教案条目（kind=lesson_plan）。"""
    return list_documents(db, course, KIND)


def upsert(db: Session, course: Course, node: OutlineNode, *,
           llm_used: bool = False) -> dict:
    """生成/刷新一个知识点的教案条目；内容没变则原地不动（幂等）。"""
    markdown = plan_markdown(db, course, node, lecture_markdown_of(db, course, node))
    title = f"{node.node_key} {node.title} 教案".strip()
    minutes = K12_MINUTES.get(str(k12_mod.k12_meta(course).get("stage") or ""), DEFAULT_MINUTES)
    return write_document(db, course, node, kind=KIND, title=title, markdown=markdown,
                          meta_extra={"llm_used": bool(llm_used), "minutes": minutes})


def generate_for_course(db: Session, course: Course, *,
                        node_ids: list[str] | None = None) -> dict:
    """为整门课（或指定节点）生成教案骨架。"""
    return generate_documents(db, course, upsert, node_ids=node_ids)


def generate_documents(db: Session, course: Course, writer, *,
                       node_ids: list[str] | None = None) -> dict:
    """逐知识点调用 ``writer(db, course, node)``，返回计数与逐条结果。

    单个节点失败不阻断整门课：失败原因写进 ``rows[].error``，老师能一眼看到是哪一个。
    """
    from app.services.kb import _chapter_list, _nodes  # 局部导入避免环

    nodes = _nodes(db, course.id)
    wanted = set(node_ids or [])
    if wanted:
        nodes = [node for node in nodes if node.id in wanted]
    chapter_keys = {chapter.node_key for chapter in _chapter_list(nodes)}
    targets = [node for node in nodes if node.node_key not in chapter_keys]
    created = changed = failed = 0
    rows: list[dict] = []
    for node in targets:
        try:
            result = writer(db, course, node)
        except Exception as exc:  # noqa: BLE001 —— 单个节点失败不阻断整课
            failed += 1
            logger.warning("文档生成失败 node=%s kind=%s: %s",
                           node.node_key, getattr(writer, "__name__", "?"), exc)
            rows.append({"node_id": node.id, "node_key": node.node_key,
                         "ok": False, "error": str(exc)})
            continue
        created += 1 if result["created"] else 0
        changed += 1 if result["changed"] else 0
        rows.append({"node_id": node.id, "node_key": node.node_key, "ok": True, **result})
    db.commit()
    return {"total": len(targets), "created": created, "changed": changed,
            "failed": failed, "rows": rows}
