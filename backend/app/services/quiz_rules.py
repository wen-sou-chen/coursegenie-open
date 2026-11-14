"""习题答案口径与落库前校验（任务 56）。

背景：模型对「正确答案是第一个选项」的选择题常返回 **JSON 数字 ``0``**，而旧代码写的是
``answer=str(raw.get("answer") or "")`` —— ``0 or ""`` 直接把答案吞成空串。后果是
讲义里那道题**既没有「答案」行、也没给正确选项打 ✓**（真实 AI 课 27 道选择题里 8 道，30%）。

这里统一两条路径（讲义 ``lecture.py`` 与独立出题 ``quiz.py``）的口径：

1. 取值一律走 :func:`text_of` —— 显式判 ``None``，``0`` / ``False`` 也是合法值；
2. ``choice`` 的答案归一成 **0 基序号字符串**（支持数字、字母 A~H、选项原文回查）；
3. 落库前用 :func:`quiz_problem` 校验：不合规先 :func:`revise_quiz_item` **重问一次**，
   仍不合规就 :func:`mark_manual`（题面加显式前缀 ``【答案待人工确认】``）——
   **绝不静默丢答案、也绝不静默丢题**。
"""
from __future__ import annotations

import logging
import re

from app.services import usage as usage_log

logger = logging.getLogger(__name__)

QUIZ_TYPES = ("choice", "blank", "short", "calc", "draw", "material", "experiment")
# 任务 55：题型中文名（渲染 / 报错 / 质检共用同一份）
TYPE_LABEL = {"choice": "选择题", "blank": "填空题", "short": "简答题", "calc": "计算题",
              "draw": "作图题", "material": "材料分析题", "experiment": "实验探究题"}
# 任务 55：开放题（没有唯一正确答案）⇒ 必须有**评分要点 rubric**，而不是 answer
OPEN_TYPES = ("draw", "material", "experiment")
DIFFICULTIES = ("easy", "medium", "hard")
MAX_OPTIONS = 8

# 任务 55：分层作业（基础 / 提升 / 挑战，三层都要有且难度递增）
LEVELS = ("基础", "提升", "挑战")
LEVEL_BY_DIFFICULTY = {"easy": "基础", "medium": "提升", "hard": "挑战"}

# 任务 55：每题预计用时（分钟）——缺省按题型兜底，单题夹到 1..MINUTES_CAP
DEFAULT_MINUTES = {"choice": 2, "blank": 2, "short": 5, "calc": 6,
                   "draw": 8, "material": 8, "experiment": 10}
MINUTES_CAP = 60

# 任务 55：单次练习总时长建议上限（分钟，双减口径）——**只提示，不判 fail**
WORKLOAD_LIMITS = {"小学": 60, "初中": 90, "高中": 90}
WORKLOAD_LIMIT_DEFAULT = 90

# 校验不过且重试仍失败时的显式标注（题干前缀 + QC 会判 warn，老师一眼能看到）
MANUAL_ANSWER_MARK = "答案待人工确认"
MANUAL_PREFIX = f"【{MANUAL_ANSWER_MARK}】"
# 任务 55：开放题缺评分要点时的专用标注（与「缺答案」区分，老师一眼知道该补什么）
MANUAL_RUBRIC_MARK = "评分要点待人工确认"
MANUAL_RUBRIC_PREFIX = f"【{MANUAL_RUBRIC_MARK}】"

# 选项原文回查时忽略的装饰：空白、「A.」「A、」这类标签前缀
_OPTION_LABEL_RE = re.compile(r"^\s*(?:[A-Ha-h]|[0-9]{1,2})\s*[.、:：)）]\s*")
_OPTION_TRIM = " \t\u3000。．,，;；:：、!！?？\"'“”‘’()（）[]【】"

# 订正提示词：沿用 QUIZ_JSON 前缀，便于 mock / 日志按同一类路由与检索
QUIZ_FIX_SYSTEM = """QUIZ_JSON
你负责订正一道习题。只输出 JSON：
{"item": {"qtype": "choice|blank|short|calc|draw|material|experiment",
  "difficulty": "easy|medium|hard", "level": "基础|提升|挑战", "minutes": 3,
  "question": "题干原文", "options": ["选项..."], "answer": "答案",
  "rubric": "开放题的评分要点", "explanation": "解析"}}
要求：① choice 的 answer 必须是**0 基序号字符串**（第一个选项写作 "0"，第二个写作 "1"）；
② 保持题干与选项原意，不要改写内容，只补准确答案；③ blank/short/calc 的 answer 写完整答案文本；
④ **开放题（draw / material / experiment）必须补「评分要点 rubric」**（看什么、给几分），
开放题不要求 answer；⑤ minutes 写这道题的预计用时（分钟）；⑥ 解析要说明为什么。"""


def text_of(value) -> str:
    """把 LLM 返回的任意标量归一成字符串（``None`` → ``""``，但 **0 / False 保留**）。

    不要用 ``value or ""`` —— 那正是本任务要修的根因。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _minutes_of(value, qtype: str) -> int:
    """预计用时（分钟）：解析不出来的按题型兜底，统一夹到 1..MINUTES_CAP。"""
    try:
        minutes = int(float(text_of(value)))
    except (TypeError, ValueError):
        minutes = 0
    if minutes <= 0:
        minutes = DEFAULT_MINUTES.get(qtype, 5)
    return max(1, min(minutes, MINUTES_CAP))


def meta_of(row) -> dict:
    """取题目上的 ``meta``（QuizItem 行与 dict 两种都认）。"""
    if isinstance(row, dict):
        return dict(row.get("meta") or {})
    return dict(getattr(row, "meta", None) or {})


def _field_of(row, name: str) -> str:
    value = row.get(name) if isinstance(row, dict) else getattr(row, name, None)
    return text_of(value)


def quiz_level(row) -> str:
    """题目分层（基础 / 提升 / 挑战）：优先 ``meta.level``，缺省按难度兜底。"""
    level = text_of(meta_of(row).get("level"))
    if level in LEVELS:
        return level
    return LEVEL_BY_DIFFICULTY.get(_field_of(row, "difficulty").lower(), "提升")


def quiz_minutes(row) -> int:
    """每题预计用时（分钟）：优先 ``meta.minutes``，缺省按题型兜底。"""
    return _minutes_of(meta_of(row).get("minutes"), _field_of(row, "qtype") or "choice")


def quiz_rubric(row) -> str:
    """开放题的评分要点（``meta.rubric``）；非开放题返回空串。"""
    return text_of(meta_of(row).get("rubric"))


def row_meta(fields: dict) -> dict:
    """落库用的 ``quiz_items.meta``：分层 / 预计用时 / 评分要点（任务 55）。"""
    return {
        "level": text_of(fields.get("level")) or LEVEL_BY_DIFFICULTY.get(
            text_of(fields.get("difficulty")) or "medium", "提升"),
        "minutes": _minutes_of(fields.get("minutes"), text_of(fields.get("qtype")) or "choice"),
        "rubric": text_of(fields.get("rubric")),
    }


def workload_limit(stage: str = "", subject: str = "") -> dict:
    """单次练习总时长建议上限：``{"limit": 分钟, "label": 学段}``（任务 55，双减口径）。

    **只用于提示**（讲义练习小节底部 / 交付件），不参与 10 项判据、不影响评分。
    """
    text = "{} {}".format(stage or "", subject or "")
    for name in WORKLOAD_LIMITS:
        if name in text:
            if name == "小学":
                label = "小学（{}）".format(subject) if subject else "小学（语文/数学）"
            else:
                label = name
            return {"limit": WORKLOAD_LIMITS[name], "label": label}
    return {"limit": WORKLOAD_LIMIT_DEFAULT, "label": "通用"}


def _option_key(text: str) -> str:
    """选项原文比对用的归一 key（去标签前缀、去首尾标点、压缩空白、忽略大小写）。"""
    body = _OPTION_LABEL_RE.sub("", text_of(text))
    return re.sub(r"\s+", "", body).strip(_OPTION_TRIM).lower()


def answer_index_of(raw_answer, options: list[str]) -> str:
    """把原始答案归一成 0 基序号字符串；解析不出来返回 ``""``。

    支持三种写法：① 数字/数字串（0 基，含全角数字）；② 单个字母 ``A``~``H``（1 基）；
    ③ 选项原文（与某个选项归一后相等）。越界或对不上返回空串，由 :func:`quiz_problem` 报告。
    """
    text = text_of(raw_answer)
    if not text:
        return ""
    if text.isdigit():
        try:
            index = int(text)
        except ValueError:            # 上标数字等 isdigit() 为真但 int() 失败的怪值
            return ""
    elif len(text) == 1 and text.upper() in "ABCDEFGH":
        index = ord(text.upper()) - ord("A")
    else:
        key = _option_key(text)
        for idx, option in enumerate(options):
            if key and key == _option_key(option):
                return str(idx)
        return ""
    return str(index)


def normalize_quiz(raw) -> dict:
    """把一条 LLM 原始题目归一成落库字段（不校验，校验见 :func:`quiz_problem`）。"""
    raw = raw if isinstance(raw, dict) else {}
    qtype = text_of(raw.get("qtype")).lower()
    if qtype not in QUIZ_TYPES:
        qtype = "choice"
    difficulty = text_of(raw.get("difficulty")).lower()
    if difficulty not in DIFFICULTIES:
        difficulty = "medium"
    raw_options = raw.get("options")
    options = ([text_of(item) for item in raw_options]
               if isinstance(raw_options, list) else [])
    options = [item for item in options if item][:MAX_OPTIONS]
    answer_raw = text_of(raw.get("answer"))
    answer = (answer_index_of(answer_raw, options) if qtype == "choice" else answer_raw)
    level = text_of(raw.get("level"))
    if level not in LEVELS:
        level = ""                        # 空着交给 :func:`row_meta` 按难度兜底
    return {
        "qtype": qtype,
        "difficulty": difficulty,
        "level": level,                                   # 任务 55：分层
        "minutes": _minutes_of(raw.get("minutes"), qtype),  # 任务 55：预计用时
        "question": text_of(raw.get("question"))[:500],
        "options": options,
        "answer": answer,
        "answer_raw": answer_raw,      # 只进 meta/日志，不落库
        "rubric": text_of(raw.get("rubric")),   # 任务 55：开放题评分要点
        "explanation": text_of(raw.get("explanation")),
    }


def quiz_problem(item: dict) -> str:
    """落库前校验：返回不合规原因（``""`` = 合规）。

    任务 55：按题型分派 —— 开放题（draw / material / experiment）**必须有评分要点
    ``rubric``**（开放题没有唯一答案，answer 不作要求）；其余题型仍要求 answer。
    """
    qtype = text_of(item.get("qtype")) or "choice"
    answer = text_of(item.get("answer"))
    label = TYPE_LABEL.get(qtype, "习题")
    if qtype == "choice":
        options = list(item.get("options") or [])
        if len(options) < 2:
            return f"选择题只有 {len(options)} 个选项（至少 2 个）"
        if not answer:
            raw = text_of(item.get("answer_raw"))
            return (f"选择题答案「{raw}」对不上任何选项" if raw else "选择题没有答案")
        if not answer.isdigit():
            return f"选择题答案「{answer}」不是选项序号"
        if int(answer) >= len(options):
            return f"选择题答案序号 {answer} 越界（只有 {len(options)} 个选项）"
        return ""
    if qtype in OPEN_TYPES:
        if not text_of(item.get("question")):
            return f"{label}没有题干"
        if not text_of(item.get("rubric")):
            return f"{label}（开放题）没有评分要点"
        return ""
    if not answer:
        return f"{label}没有答案"
    return ""


def mark_manual(item: dict, reason: str) -> dict:
    """仍不合规时：题面加显式前缀（幂等），并返回要写进 meta 的问题记录。

    任务 55：缺评分要点（开放题）与缺答案用**不同前缀**，老师一眼知道该补什么。
    """
    prefix = (MANUAL_RUBRIC_PREFIX if "评分要点" in str(reason or "")
              else MANUAL_PREFIX)
    question = str(item.get("question") or "")
    if not question.startswith(prefix):
        item = {**item, "question": f"{prefix}{question}"}
    return item


def revise_quiz_item(client, item: dict, problem: str) -> dict | None:
    """就一道不合规的题**重问一次**；返回订正后的原始 dict，失败返回 ``None``。"""
    if client is None:
        return None
    options = list(item.get("options") or [])
    user = (
        f"题干：{item.get('question') or ''}\n"
        f"题型：{item.get('qtype')}\n"
        f"选项：{options}\n"
        f"当前答案：{item.get('answer_raw') or item.get('answer') or '（空）'}\n"
        f"现有解析：{item.get('explanation') or '（空）'}\n"
        f"问题：{problem}\n"
        "请订正后重新输出这道题（answer 用 0 基序号）。"
    )
    try:
        data = usage_log.complete_json(client, QUIZ_FIX_SYSTEM, user,
                                       max_tokens=900, purpose="quiz-fix")
    except Exception as exc:  # noqa: BLE001 —— 订正失败不阻断，退回显式标注
        logger.warning("习题订正调用失败：%s", exc)
        return None
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("item"), dict):
        return data["item"]
    items = data.get("items")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0]
    return data if data.get("question") else None


def resolve_quiz(raw, *, client=None) -> tuple[dict, str]:
    """归一 + 校验 + （必要时）重问一次；返回 ``(字段, 遗留问题)``。

    遗留问题非空 = 这道题最终仍不合规，调用方必须 :func:`mark_manual` 并显式上报。
    """
    item = normalize_quiz(raw)
    problem = quiz_problem(item)
    if not problem:
        return item, ""
    fixed = revise_quiz_item(client, item, problem)
    if fixed is not None:
        candidate = normalize_quiz({**fixed,
                                    "qtype": fixed.get("qtype") or item["qtype"],
                                    "difficulty": fixed.get("difficulty") or item["difficulty"],
                                    "level": fixed.get("level") or item.get("level"),
                                    "minutes": fixed.get("minutes") or item.get("minutes"),
                                    "rubric": fixed.get("rubric") or item.get("rubric")})
        if candidate.get("question") or candidate.get("options"):
            retry_problem = quiz_problem(candidate)
            if not retry_problem:
                return candidate, ""
            item, problem = candidate, retry_problem
    return item, problem


def resolve_quiz_list(raw_items, *, client=None, limit: int = 5) -> tuple[list[dict], list[dict]]:
    """整批题目走同一条流水线：归一 → 校验 → （逐题）重问一次 → 仍不合规则显式标注。

    返回 ``(字段列表, 问题列表)``：问题列表里每项含 ``index``（第几题，1 基）、
    ``qtype``、``problem``（原因）、``question``（已带前缀的题面）、``answer_raw``。
    调用方负责落库、把问题写进 meta / 日志 —— 这里不做任何静默丢弃。
    """
    fields_list: list[dict] = []
    issues: list[dict] = []
    for index, raw in enumerate(list(raw_items or [])[:limit], 1):
        fields, problem = resolve_quiz(raw, client=client)
        if problem:
            fields = mark_manual(fields, problem)
            issues.append({
                "index": index,
                "qtype": fields["qtype"],
                "problem": problem,
                "question": fields["question"][:200],
                "answer_raw": fields.get("answer_raw") or "",
            })
        fields_list.append(fields)
    return fields_list, issues
