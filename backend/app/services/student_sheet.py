"""学案（学生版讲义，任务 50 / 48s 收口）：讲义去答案留空 + 作答区 + 页码，**0 LLM**。

口径：
- **练习小节整块不进学案（任务 48s 的根因修复）**：讲义的随堂练习在 markdown 里是
  **嵌套标题**（``## 随堂练习`` → ``### 基础 / 提升 / 挑战`` → ``#### 第 N 题 · 选择题…``），
  子标题本身不带「随堂练习」字样。任务 50 只按「小节自己的标题」判跳过 ⇒ 整块练习
  （``✓`` 正确项标记、``**答案**``、``**解析**``、``**评分要点**``）漏进「学习任务」，
  正确项标记又被 ``**…** → ＿＿＿`` 变成 ``＿＿＿：<答案正文>`` ⇒ **发给学生等于发答案**。
  现在按**祖先标题链**判定：祖先或自身标题命中跳过键的整块排除。
- **答案一律不留**：练习的 ``answer`` / ``explanation`` 整条不进学案；讲义正文里凡是
  会直接泄题的地方（加粗术语、与答案相同的字串）统一挖空成 ``＿＿＿``。候选池是
  **本课时（≥2 字）+ 整门课（≥6 字的成句答案）**两档 —— 同一门课的知识点互相引用很正常，
  但学生手里的学案不该出现**任何一道课**的答案原句。最后再过两道**兜底清洗**
  （答案字串抹掉 + 泄题行整行删掉），保证「用真实 ``quiz_items.answer`` / ``explanation``
  逐条断言搜不到」这条验收能过。
- **作答区**：每道题下面留出书写位置；``choice`` 给括号、``blank``/``short`` 给横线。
- **页码**：Word 页脚「第 N 页」（Word 的 ``PAGE`` 域），发给学生按页发。
- **答案版留在教师侧**：讲义正文与「习题集」（含答案与解析）都属于教师侧，
  学案只是它们的派生视图，不反过来影响教师侧内容。
"""
from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from app.models.course import Course, OutlineNode, QuizItem
from app.services import lesson_plan
from app.services import quiz_rules
from app.services.lecture_qc import NO_BASIS_MARK

logger = logging.getLogger(__name__)

KIND = "student_sheet"
DIR_NAME = "学案"
SUFFIX = "学案"
BLANK = "＿＿＿"
ANSWER_LINES = 3          # 主观题作答横线行数

_RE_EMPHASIS_SPAN = re.compile(r"\*\*(.+?)\*\*")
_RE_TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_RE_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_RE_ANSWER_MARK = re.compile(r"[（(]\s*(?:答案|参考答案|解析)[^）)]*[）)]")
_RE_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
# 泄题行：答案 / 解析 / 评分要点（带不带加粗、带不带列表符号都算）⇒ 整行去掉
_RE_ANSWER_LABEL = re.compile(
    r"^[\s>]*(?:[-*+]\s*)*(?:[*_]{0,2})\s*"
    r"(?:参考答案|标准答案|答案解析|评分要点|评分标准|评分细则|答案|解析|rubric)"
    r"\s*(?:[*_]{0,2})\s*[：:]")
# ``✓`` 正确项标记：只去标记，选项原文照旧列出
_RE_OPTION_MARK = re.compile(r"^([\s>]*(?:[-*+]\s*)?)(?:[*_]{0,2})\s*[✓✔]\s*(?:[*_]{0,2})\s*")
# 遮完只剩「＿＿＿：<正文>」的裸行（任务 50 的「字符串替换式遮标签」残留）⇒ 整行去掉
_RE_BARE_BLANK = re.compile(r"^＿+[：:]\s*\S")
# 任务 151：填空后紧跟的括号。真实形态有两类（真库 19 处）：
#   - ``＿＿＿（来源:《…》第 N 段）`` ⇒ 来源括号直接去掉；
#   - ``＿＿＿（控制模型采样随机性的超参数）`` ⇒ 括号就是答案本身，换成思考提示。
# 统一要求：最终文本里 ``＿{2,}（…）`` 必须 0 处（DSH 会全库复扫）。
_RE_SOURCE_HINT = re.compile(r"来源|第\s*\d+\s*段|》第")
_RE_SOURCE_START = re.compile(r"^\s*来源\s*[:：]")
_BLANK_HINT = "（想想看：它和什么有关？）"
# 这些不是答案，是普通提示；保留原文，但用全角空格与填空隔开，避免命中泄漏正则。
_HINT_PREFIXES = ("例如", "如图", "见图", "见", "约", "即", "如", "提示")
_SKIP_HEADING = ("来源", "术语", "本章概念索引", "参考文献", "答案", "解析")
_ANSWER_MIN_LEN = 2       # 太短的答案（单字/序号）不挖，否则会把正文挖烂
# 任务 48s 验收要求「用真实 answer/explanation 逐条断言学案里搜不到」，而候选池是**整门课**
# 的习题：同一门课的常识性答案（如 AI 课的「多模型交叉验证」）本来就写在别的课时正文里。
# 跨课时只挖 ≥6 字的「成句答案」：2~5 字的短串多是本课核心词汇，挖掉会把学生读的正文挖烂。
_CROSS_NEEDLE_MIN_LEN = 6


def _answer_safe_lines(count: int = ANSWER_LINES) -> list[str]:
    return ["　", "　答：", *["　＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿＿" for _ in range(count)], ""]


def _redact(text: str, needles: list[str]) -> str:
    """把答案字串抹成空白（幂等；只动出现在正文里的那些）。"""
    out = text
    for needle in needles:
        if len(needle) < _ANSWER_MIN_LEN:
            continue
        out = out.replace(needle, BLANK)
    return out


def _drop_source_parens(text: str) -> str:
    """删掉所有 ``（来源:…）``（含嵌套）；连续两个来源括号也一次清完。"""
    source = str(text or "")
    out: list[str] = []
    index = 0
    while index < len(source):
        if source[index] in "（(":
            opener = source[index]
            closer = "）" if opener == "（" else ")"
            depth = 0
            cursor = index
            while cursor < len(source):
                if source[cursor] == opener:
                    depth += 1
                elif source[cursor] == closer:
                    depth -= 1
                    if depth == 0:
                        cursor += 1
                        break
                cursor += 1
            if depth == 0 and _RE_SOURCE_START.match(source[index + 1:cursor - 1]):
                index = cursor
                continue
        out.append(source[index])
        index += 1
    return "".join(out)


def _rewrite_blank_parens(text: str) -> str:
    """把「＿＿＿（…）」改写成不泄题的形态（幂等，任务 151）。

    判据写死在这里：

    * 括号里是来源（含嵌套括号）⇒ 整段去掉；
    * 括号内容 ≥4 字且不是「例如 / 如图 / 约 / 即」这类普通提示 ⇒ 视为答案，
      换成固定思考提示；
    * 其余短提示 ⇒ 保留内容，但用全角空格把括号与填空隔开。

    这样学生仍能看到「这里有提示」，但**括号里不再写着答案**，也不会命中
    ``＿{2,}（…）`` 的全库扫描。
    """
    source = _drop_source_parens(str(text or ""))
    out: list[str] = []
    index = 0
    while index < len(source):
        match = re.match(r"＿{2,}", source[index:])
        if not match:
            out.append(source[index])
            index += 1
            continue
        blank = match.group(0)
        start = index + len(blank)
        if start >= len(source) or source[start] not in "（(":
            out.append(blank)
            index = start
            continue
        opener = source[start]
        closer = "）" if opener == "（" else ")"
        depth = 0
        cursor = start
        while cursor < len(source):
            if source[cursor] == opener:
                depth += 1
            elif source[cursor] == closer:
                depth -= 1
                if depth == 0:
                    cursor += 1
                    break
            cursor += 1
        if depth != 0:  # 括号没配平：不猜，原样保留
            out.append(blank)
            index = start
            continue
        inner = source[start + 1:cursor - 1].strip()
        if not inner or _RE_SOURCE_HINT.search(inner):
            out.append(blank)
        elif len(inner) >= 4 and not inner.startswith(_HINT_PREFIXES):
            out.append(f"{blank}　{_BLANK_HINT}")
        else:
            out.append(f"{blank}　（{inner}）")
        index = cursor
    return "".join(out)


def _answer_needles(quizzes: list[QuizItem], *, min_len: int = _ANSWER_MIN_LEN) -> list[str]:
    """需要从学案里彻底消失的字串：非选择题的答案 + 各题解析。

    选择题的答案是**选项序号**（``quiz_rules`` 归一口径），序号本身没有泄题价值，
    选项列表本来就是题面的一部分；但解析（可能直接写出正确选项）必须整条拿掉。
    ``min_len`` 之上的才收（跨课时口径用 :data:`_CROSS_NEEDLE_MIN_LEN`）。
    """
    needles: list[str] = []
    for quiz in quizzes:
        if str(quiz.qtype) != "choice":
            text = str(quiz.answer or "").strip()
            if len(text) >= min_len:
                needles.append(text)
        explanation = str(quiz.explanation or "").strip()
        if len(explanation) >= min_len:
            needles.append(explanation)
    return needles


def _merge_needles(*groups: list[str]) -> list[str]:
    """合并去重并按**长度从大到小**排：先抹长串，短串才不会把长串切碎留下残句。"""
    seen: dict[str, None] = {}
    for group in groups:
        for needle in group:
            if needle and needle not in seen:
                seen[needle] = None
    return sorted(seen, key=len, reverse=True)


def _sections(markdown: str) -> list[tuple[tuple[str, ...], str, list[str]]]:
    """讲义 → ``[(祖先标题链, 本小节标题, 正文行)]``，按出现顺序。

    标题链里**不含一级文档标题**（``# 1.1 …`` 是页面标题，不是内容小节）：否则节点
    标题里带「练习」「解析」这类词时，整篇正文会被误跳过。
    """
    out: list[tuple[tuple[str, ...], str, list[str]]] = []
    stack: list[tuple[int, str]] = []
    head, body = "", []
    for line in str(markdown or "").split("\n"):
        match = _RE_HEADING.match(line)
        if match:
            if head or body:
                out.append((tuple(text for level, text in stack if level > 1), head, body))
            level = len(match.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            head = match.group(2).strip()
            stack.append((level, head))
            body = []
            continue
        body.append(line)
    if head or body:
        out.append((tuple(text for level, text in stack if level > 1), head, body))
    return out


def _skip_section(chain: tuple[str, ...], head: str) -> bool:
    """练习/来源/术语小节不进学案：祖先标题或自身标题命中跳过键。"""
    keys = (*_SKIP_HEADING, *lesson_plan._PRACTICE_KEYS)
    return any(key in text for text in (*chain, head) if text for key in keys)


def _keep_line(line: str) -> str | None:
    """讲义一行 → 学案一行；属于泄题内容的返回 ``None``（整行丢掉）。"""
    text = _RE_IMAGE.sub("", line)
    text = _RE_ANSWER_MARK.sub("", text)
    if _RE_ANSWER_LABEL.match(text):
        return None
    text = _RE_OPTION_MARK.sub(r"\1", text)
    text = _RE_EMPHASIS_SPAN.sub(lambda m: BLANK, text)
    text = _rewrite_blank_parens(text)
    if _RE_BARE_BLANK.match(text.strip()):
        return None
    return text.rstrip() if text.strip() else None


def _body_lines(markdown: str) -> list[str]:
    """讲义正文 → 学案「学习任务」：去掉练习/来源/术语小节，加粗术语挖空。

    表格分隔行（``| --- |``）只在**上一行确实是表头**时保留 —— 没有分隔行的 Markdown
    表格在 Obsidian / Word 里只是一串竖线文本，学生会读到 ``| 菌群 | 主要产物 |``。
    """
    keep: list[str] = []
    for chain, head, body in _sections(markdown):
        if _skip_section(chain, head):
            continue
        lines: list[str] = []
        after_table_head = False
        for line in body:
            if _RE_TABLE_SEP.match(line):
                if after_table_head:
                    lines.append(line.rstrip())
                continue
            text = _keep_line(line)
            after_table_head = bool(text is not None and text.strip().startswith("|"))
            if text is not None:
                lines.append(text)
        if not lines:
            continue
        if head:
            keep += [f"### {head}", ""]
        keep += [*lines, ""]
    return keep


def strip_leaks(markdown: str) -> str:
    """整篇第二道兜底（结构判定之外）：泄题行整行删掉、``✓`` 标记一律去掉。

    只删不改、幂等；防的是「练习小节用了没见过的标题层级 / 模型自造小节」这类结构
    判定够不着的情况 —— 学案是发给学生的，宁可少一行，不可多一句答案。

    任务 86 起**公开**：学员版讲义 / 公开分享页复用同一套规则（见 ``services/audience.py``）——
    学生填的学案与学生读的讲义，对答案的口径必须一致。
    """
    out: list[str] = []
    for line in str(markdown or "").split("\n"):
        if _RE_ANSWER_LABEL.match(line):
            continue
        if _RE_BARE_BLANK.match(line.strip()):
            continue
        out.append(_rewrite_blank_parens(line.replace("✓", "").replace("✔", "")))
    return "\n".join(out)


def sheet_markdown(db: Session, course: Course, node: OutlineNode,
                   lecture: str = "") -> str:
    """渲染单个知识点的学案（纯函数式：同输入恒得同输出）。"""
    quizzes = (db.query(QuizItem)
               .filter(QuizItem.course_id == course.id, QuizItem.node_id == node.id)
               .order_by(QuizItem.created_at).all())
    # 两道候选池：本课时（≥2 字，含短答案）+ 整门课（≥6 字的成句答案）。
    # 后者防的是「别的课时把答案写进了本课时的正文」—— 同一门课的知识点互相引用很正常，
    # 但学生手里的学案不该出现任何一道课的答案原句（任务 48s 验收口径：整课候选池）。
    course_quizzes = db.query(QuizItem).filter(QuizItem.course_id == course.id).all()
    needles = _merge_needles(_answer_needles(quizzes),
                             _answer_needles(course_quizzes, min_len=_CROSS_NEEDLE_MIN_LEN))
    parts = [
        f"# 《{node.title}》学案", "",
        f"> 学生版（0 LLM 派生）：答案与解析一律移除，术语与关键结论留空待填。"
        f"教师答案版见讲义正文与「习题集」。", "",
        "| 班级 | 姓名 | 日期 | 得分 |",
        "| --- | --- | --- | --- |",
        "|  |  |  |  |",
        "",
    ]
    objective = str(node.objective or "").strip()
    parts += ["## 一、学习目标", ""]
    if objective:
        parts += [f"{idx}. {text}" for idx, text in
                  enumerate((p.strip() for p in re.split(r"[；;。]\s*", objective) if p.strip()), 1)]
    else:
        parts += [f"- {NO_BASIS_MARK}（大纲未写课时目标）"]
    parts += [""]
    parts += ["## 二、学习任务（先读后填）", ""]
    body = _body_lines(lecture)
    parts += body if body else [
        f"- {NO_BASIS_MARK}（本课时还没有讲义正文，先请老师生成讲义）", ""]
    parts += ["## 三、随堂练习", ""]
    if not quizzes:
        parts += [f"- {NO_BASIS_MARK}（本节未配置练习）", ""]
    for idx, quiz in enumerate(quizzes, 1):
        question = _redact(str(quiz.question or "").replace("\n", " ").strip(), needles)
        # 分层与预计用时是「题干该带的信息」（任务 48s 护栏）：练习整块不进学习任务后，
        # 学生版仍要能看出哪题是基础、哪题是挑战、每题大约留多少时间。
        parts += [f"### 第 {idx} 题（{quiz.qtype} · {quiz_rules.quiz_level(quiz)}"
                  f" · 约 {quiz_rules.quiz_minutes(quiz)} 分钟）", "",
                  question, ""]
        options = list(quiz.options or [])
        if str(quiz.qtype) == "choice" and options:
            for label, option in zip("ABCDEFGH", options):
                parts.append(f"- {label}. {_redact(str(option), needles)}")
            parts.append("")
            parts.append("　答：（　　　　）")
            parts.append("")
        else:
            parts += _answer_safe_lines()
    parts += ["## 四、我的疑问", "", "　", "　", ""]
    markdown = strip_leaks(_redact("\n".join(parts).rstrip() + "\n", needles))
    return markdown


def lecture_markdown_of(db: Session, course: Course, node: OutlineNode) -> str:
    return lesson_plan.lecture_markdown_of(db, course, node)


def upsert(db: Session, course: Course, node: OutlineNode) -> dict:
    """生成/刷新一个知识点的学案条目（幂等）。"""
    markdown = sheet_markdown(db, course, node, lecture_markdown_of(db, course, node))
    title = f"{node.node_key} {node.title} 学案".strip()
    quizzes = (db.query(QuizItem)
               .filter(QuizItem.course_id == course.id, QuizItem.node_id == node.id).count())
    return lesson_plan.write_document(db, course, node, kind=KIND, title=title,
                                      markdown=markdown,
                                      meta_extra={"llm_used": False, "quizzes": quizzes,
                                                  "student_version": True})


def generate_for_course(db: Session, course: Course, *,
                        node_ids: list[str] | None = None) -> dict:
    """为整门课（或指定节点）生成学案。"""
    return lesson_plan.generate_documents(db, course, upsert, node_ids=node_ids)


def list_sheets(db: Session, course: Course) -> list[dict]:
    """已落库的学案条目（按大纲顺序）。"""
    return lesson_plan.list_documents(db, course, KIND)
