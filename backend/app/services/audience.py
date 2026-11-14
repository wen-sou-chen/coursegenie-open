"""学员版 / 教师版（答案可见性，任务 86）：**0-LLM 的确定性文本处理**。

用途：公开分享页（``GET /api/public/{token}/content?view=student``）与交付包里的
``学员版讲义/`` —— 老师把同一门课给到学生时，答案与解析不该跟着走。

剥离规则**与 ``学案/`` 共用同一套**（:func:`app.services.student_sheet.strip_leaks`）：
讲义正文里 ``**答案**：`` / ``**解析**：`` / ``**评分要点**：`` 这类泄题行**整行删掉**、
选择题正确项前的 ``✓`` 标记去掉（退回普通列表项）；**题目与作答区保留**。

任务 89 补一处实测漏点：讲义第 3 步的引用表会把随堂练习的解析行当成「来源」记进
``meta.sources[].quote`` ⇒ 学员版整条去掉（:func:`student_sources`，真库实测 49 处）。

任务 90 补第二处实测漏点：习题行原先只把 ``answer`` / ``explanation`` **清成空串**，
留下「键在、值为空」的中间态 —— 前端无条件渲染的「答案：」标签就露出一个空壳
（分享页学员版实测每门课 6 处可见）。改用 :func:`student_quiz_body`：**键一起去掉**。

⚠️ 只做确定性文本处理，**不调模型**；``teacher``（默认）= 现状，一字不改。
"""
from __future__ import annotations

import re

from app.services import student_sheet

#: 选择题正确项标记 ``✓ x``（可能带列表符 / 加粗）⇒ **普通列表项** ``- x``。
#: 规格口径：「✓ 改回普通列表项」，所以这里补 ``- ``，而不是只把 ✓ 抹掉（那样会掉出列表）。
_RE_TICK_OPTION = re.compile(r"^([\s>]*)(?:[-*+]\s*)?(?:[*_]{0,2})\s*[✓✔]\s*(?:[*_]{0,2})?",
                             re.MULTILINE)

AUDIENCE_TEACHER = "teacher"
AUDIENCE_STUDENT = "student"
VIEWS: tuple[str, ...] = (AUDIENCE_TEACHER, AUDIENCE_STUDENT)
UNSET_VIEW = AUDIENCE_TEACHER


def normalize_view(raw) -> str:
    """归一化视图：非法 / 空 ⇒ ``teacher``（**默认 = 现状**，绝不默认去答案）。"""
    return AUDIENCE_STUDENT if str(raw or "").strip().lower() == AUDIENCE_STUDENT else AUDIENCE_TEACHER


def is_student(raw) -> bool:
    return normalize_view(raw) == AUDIENCE_STUDENT


def student_markdown(markdown: str) -> str:
    """讲义正文 → 学员版正文（只删不改、幂等）。

    ``学案/`` 那套 :func:`app.services.student_sheet.strip_leaks` 只管「泄题行整行删 + ✓ 抹掉」；
    学员版讲义还要把正确项的 ``✓ `` **变回普通列表项**（否则抹掉 ✓ 后这一行会掉出列表）。
    """
    return student_sheet.strip_leaks(_RE_TICK_OPTION.sub(r"\1- ", str(markdown or "")))


def student_sources(sources) -> list[dict]:
    """引用表去泄题：``quote`` 整行就是「答案 / 解析 / 评分要点」的条目**整条不进载荷**。

    讲义第 3 步的引用表会把**随堂练习的解析行**也当成「来源」记进 ``meta.sources[].quote``
    （``section`` 形如「第 1 题 · 选择题」）—— 教师版是自查线索，学员版等于把答案发给学生
    （任务 89 实测：真库两门课 **49** 处，全部落在讲义行，且只在讲义行 ⇒ 只处理讲义行）。
    规则复用学案同一套 :func:`student_sheet.strip_leaks`（泄题行整行删）；删完不剩实质内容
    的条目（本来只为引用这道答案而存在）**整条去掉**，而不是留一条空引用。
    """
    if not isinstance(sources, list):
        return sources
    out: list[dict] = []
    for item in sources:
        if not isinstance(item, dict):
            out.append(item)
            continue
        quote = str(item.get("quote") or "")
        cleaned = student_sheet.strip_leaks(quote)
        if quote.strip() and not cleaned.strip():
            continue
        out.append({**item, "quote": cleaned})
    return out


def student_meta(meta) -> dict:
    """学员版 ``meta``：只清引用表里的泄题行；**保留哪些键**是 ``share.py`` 白名单的事。"""
    data = dict(meta) if isinstance(meta, dict) else {}
    if "sources" in data:
        data["sources"] = student_sources(data["sources"])
    return data


#: 学员版习题行**连键一起去掉**的字段（任务 90 §A3 方案 A）。
#: 键不存在 ⇒ 前端按真值渲染时整行不渲染，载荷里也不再出现「有答案这么个字段」的暗示。
_QUIZ_HIDDEN_KEYS: tuple[str, ...] = ("answer", "explanation")


def student_quiz_body(body: dict) -> dict:
    """习题行 body → 学员版：去掉答案 / 解析（键一起去掉），其余字段原样、不就地改。"""
    return {k: v for k, v in body.items() if k not in _QUIZ_HIDDEN_KEYS}


def student_rows(rows: list[dict]) -> list[dict]:
    """内容行批量转学员版：只动讲义正文 / 引用表与习题行的答案字段，其余原样返回。

    习题行走 :func:`student_quiz_body`：``answer`` / ``explanation`` **键一起去掉**（任务 90），
    不留「键在、值为空」的空壳。
    """
    out: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            out.append(row)
            continue
        kind = str(row.get("kind") or "")
        body = row.get("body")
        if kind == "lecture":
            updated = {**row, "meta": student_meta(row.get("meta"))}
            if isinstance(body, dict) and body.get("markdown"):
                updated["body"] = {**body, "markdown": student_markdown(str(body["markdown"]))}
            out.append(updated)
        elif kind == "quiz" and isinstance(body, dict):
            out.append({**row, "body": student_quiz_body(body)})
        else:
            out.append(row)
    return out
