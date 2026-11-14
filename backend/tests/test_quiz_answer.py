# -*- coding: utf-8 -*-
"""任务 56：选择题 ``answer=0`` 不再被吞 + 落库前校验 + 质检点名缺答案。

真机事故：模型对「正确答案是第一个选项」的选择题返回 JSON 数字 ``0``，
旧代码 ``str(raw.get("answer") or "")`` 把 ``0 or ""`` 变成空串 —— 讲义里那道题
既没有「**答案**」行、也没给正确选项打 ✓（AI 课 27 道选择题里 8 道，30%）。
"""
from __future__ import annotations

import json

from app.db.session import SessionLocal
from app.models.course import ContentItem
from app.schemas.quiz import QuizOut
from app.services import lecture as lecture_mod
from app.services import quiz as quiz_mod
from app.services.lecture import rerender_quiz_section
from app.services.lecture_qc import compute_qc, quiz_rows_of
from app.services.llm import MockLLMClient
from app.services.quiz_rules import (
    MANUAL_PREFIX,
    answer_index_of,
    mark_manual,
    normalize_quiz,
    quiz_problem,
    text_of,
)

from tests.test_lecture_qc import QUALIFIED, _add_sections, _node_id, _plan_approve, _wait_job

OPTIONS = ["输出高度确定，重复率高", "输出更随机", "与温度无关", "一定更准确"]

# 正确答案是第一个选项 → 模型给的是 JSON 数字 0（不是字符串 "0"）
QUIZ_ZERO = [{
    "qtype": "choice", "difficulty": "easy",
    "question": "温度参数调到 0 时，模型输出的主要特征是什么？",
    "options": OPTIONS, "answer": 0,
    "explanation": "温度=0 时模型选择概率最高的词，输出确定但容易重复。",
}]

BAD_QUIZZES = [
    {"qtype": "choice", "difficulty": "easy", "question": "只有一个选项？",
     "options": [], "answer": "0", "explanation": "解析一"},
    {"qtype": "choice", "difficulty": "medium", "question": "答案越界？",
     "options": ["甲", "乙", "丙"], "answer": "9", "explanation": "解析二"},
    {"qtype": "choice", "difficulty": "hard", "question": "答案空？",
     "options": ["甲", "乙"], "answer": "", "explanation": "解析三"},
]


class _QuizStub(MockLLMClient):
    """讲义返回指定 markdown/quizzes；订正调用（QUIZ_FIX）按脚本依次回放。"""

    def __init__(self, markdown: str, quizzes: list[dict],
                 fixes: list[dict] | None = None) -> None:
        self.markdown = markdown
        self.quizzes = quizzes
        self.fixes = list(fixes or [])
        self.fix_calls = 0

    def complete(self, system: str, user: str, *, max_tokens: int = 2000) -> str:
        if "LECTURE_JSON" in system:
            return json.dumps({"markdown": self.markdown, "summary": "一句话摘要",
                               "keywords": [], "quizzes": self.quizzes}, ensure_ascii=False)
        if "QUIZ_JSON" in system:
            self.fix_calls += 1
            payload = (self.fixes.pop(0) if self.fixes
                       else {"item": {"qtype": "choice", "question": "订正后的题",
                                      "options": [], "answer": "", "explanation": ""}})
            return json.dumps(payload, ensure_ascii=False)
        return super().complete(system, user, max_tokens=max_tokens)


def _generate(client, course_id, node_id, monkeypatch, quizzes, fixes=None) -> tuple[dict, _QuizStub]:
    stub = _QuizStub(QUALIFIED, quizzes, fixes)
    monkeypatch.setattr(lecture_mod, "build_llm_client", lambda: stub)
    resp = client.post("/api/lecture/generate", json={
        "course_id": course_id, "node_id": node_id, "tone": "正式"})
    assert resp.status_code == 200, resp.text
    state = _wait_job(client, resp.json()["job_id"])
    assert state["status"] == "success", state
    return state["result"]["item"], stub


def _quiz_rows(item_id: str) -> list[dict]:
    db = SessionLocal()
    try:
        item = db.get(ContentItem, item_id)
        return [{"qtype": q.qtype, "answer": q.answer, "options": list(q.options or []),
                 "explanation": q.explanation, "question": q.question}
                for q in quiz_rows_of(db, item)]
    finally:
        db.close()


def _prepare(client, course_id) -> str:
    _plan_approve(client, course_id)
    _add_sections(course_id, "1", ["光合作用的过程"])
    return _node_id(client, course_id, "1.1")


# ---------------------------------------------------------------------------
# ① 取值归一：0 / False 是合法值，不能被 ``or`` 吞掉
# ---------------------------------------------------------------------------

def test_text_of_keeps_falsy_values():
    assert text_of(0) == "0"
    assert text_of(0.0) == "0.0"
    assert text_of(False) == "false"
    assert text_of(None) == ""
    assert text_of("  ") == ""


def test_answer_index_normalization():
    assert answer_index_of(0, OPTIONS) == "0"
    assert answer_index_of("0", OPTIONS) == "0"
    assert answer_index_of("2", OPTIONS) == "2"
    assert answer_index_of("A", OPTIONS) == "0"
    assert answer_index_of("b", OPTIONS) == "1"
    assert answer_index_of("输出更随机", OPTIONS) == "1"
    assert answer_index_of("B. 输出更随机", OPTIONS) == "1"
    assert answer_index_of("", OPTIONS) == ""
    assert answer_index_of("对不上的话", OPTIONS) == ""


def test_quiz_problem_reports_three_broken_forms():
    """choice 无 options / 答案越界 / 答案空 —— 三种形态都要报出来（不静默）。"""
    no_options = quiz_problem(normalize_quiz(BAD_QUIZZES[0]))
    overflow = quiz_problem(normalize_quiz(BAD_QUIZZES[1]))
    empty = quiz_problem(normalize_quiz(BAD_QUIZZES[2]))
    assert "至少 2 个" in no_options
    assert "越界" in overflow
    assert "没有答案" in empty
    assert quiz_problem(normalize_quiz(QUIZ_ZERO[0])) == ""

    blank = quiz_problem(normalize_quiz({"qtype": "blank", "question": "填 ______",
                                         "answer": ""}))
    assert "没有答案" in blank
    assert quiz_problem(normalize_quiz({"qtype": "blank", "question": "填 ______",
                                        "answer": "ATP"})) == ""


def test_mark_manual_is_idempotent():
    once = mark_manual({"question": "题干"}, "没有答案")
    twice = mark_manual(once, "没有答案")
    assert once["question"] == f"{MANUAL_PREFIX}题干"
    assert twice["question"] == once["question"]


# ---------------------------------------------------------------------------
# ② 端到端：answer=0 落库为 "0"，讲义正文有「答案」行与 ✓
# ---------------------------------------------------------------------------

def test_choice_answer_zero_survives_to_db_and_markdown(client, course_id, monkeypatch):
    node = _prepare(client, course_id)
    item, stub = _generate(client, course_id, node, monkeypatch, QUIZ_ZERO)

    rows = _quiz_rows(item["id"])
    assert len(rows) == 1
    assert rows[0]["answer"] == "0"                     # ← 修复前是空串
    assert stub.fix_calls == 0                          # 合规题不该触发订正调用

    markdown = str(item["body"]["markdown"])
    assert f"**答案**：{OPTIONS[0]}" in markdown
    marked = [line.strip()[1:].strip() for line in markdown.splitlines()
              if line.strip().startswith("✓")]
    assert marked == [OPTIONS[0]]
    assert item["meta"]["quiz_issues"] == []


def test_unresolved_quizzes_are_retried_then_marked_not_dropped(client, course_id, monkeypatch):
    """三种不合规形态：各重试一次；仍不合规则显式标注 + 记 meta，题目不丢。"""
    node = _prepare(client, course_id)
    echoes = [{"item": {k: v for k, v in quiz.items()}} for quiz in BAD_QUIZZES]
    item, stub = _generate(client, course_id, node, monkeypatch, BAD_QUIZZES, fixes=echoes)

    assert stub.fix_calls == 3                          # 每道题都重试了一次
    rows = _quiz_rows(item["id"])
    assert len(rows) == 3                               # 不许默默丢题
    assert all(row["question"].startswith(MANUAL_PREFIX) for row in rows)

    issues = item["meta"]["quiz_issues"]
    assert [issue["index"] for issue in issues] == [1, 2, 3]
    assert "至少 2 个" in issues[0]["problem"]
    assert "越界" in issues[1]["problem"]
    assert "没有答案" in issues[2]["problem"]
    assert rows[0]["answer"] == "0" and rows[1]["answer"] == "9" and rows[2]["answer"] == ""


def test_quiz_retry_can_rescue_a_broken_item(client, course_id, monkeypatch):
    """重试成功时不留「待人工确认」：订正结果直接落库。"""
    node = _prepare(client, course_id)
    fixed = {"item": {"qtype": "choice", "difficulty": "medium",
                      "question": "答案越界？", "options": ["甲", "乙", "丙"],
                      "answer": "1", "explanation": "订正后的解析"}}
    item, stub = _generate(client, course_id, node, monkeypatch, [BAD_QUIZZES[1]],
                           fixes=[fixed])

    assert stub.fix_calls == 1
    rows = _quiz_rows(item["id"])
    assert rows[0]["answer"] == "1"
    assert not rows[0]["question"].startswith(MANUAL_PREFIX)
    assert rows[0]["explanation"] == "订正后的解析"
    assert item["meta"]["quiz_issues"] == []


def test_independent_quiz_path_normalizes_answer(client, course_id, monkeypatch):
    """独立出题路径（/api/quiz/generate）同样归一，且不再以 int 落库。"""
    payload = {"items": [{"qtype": "choice", "difficulty": "easy",
                          "question": "温度=0 时输出？", "options": OPTIONS,
                          "answer": 0, "explanation": "选概率最高的词。"}]}
    stub = _QuizStub("", [], fixes=[payload])

    class _Client:
        def complete_json(self, system, user, *, max_tokens=2000):
            return json.loads(stub.complete(system, user, max_tokens=max_tokens))

    monkeypatch.setattr(quiz_mod, "build_llm_client", lambda: _Client())
    node_id = _prepare(client, course_id)
    db = SessionLocal()
    try:
        from app.models.course import Course, OutlineNode

        course = db.get(Course, course_id)
        node = db.get(OutlineNode, node_id)
        rows = quiz_mod.generate_quizzes(db, course, node, 3, "medium")
        assert [row.answer for row in rows] == ["0"]
        assert isinstance(rows[0].answer, str)
        QuizOut.model_validate({**{k: getattr(rows[0], k) for k in
                                   ("id", "qtype", "difficulty", "question",
                                    "options", "answer", "explanation")},
                                "course_id": course_id, "node_id": node.id})
    finally:
        db.close()


# ---------------------------------------------------------------------------
# ③ 质检：每道题都要有答案与解析，缺则 warn 且点名题号
# ---------------------------------------------------------------------------

def test_quiz_check_flags_missing_answer_and_explanation():
    quizzes = [
        {"qtype": "choice", "difficulty": "easy", "answer": "0", "explanation": "解析一"},
        {"qtype": "choice", "difficulty": "medium", "answer": "", "explanation": "解析二"},
        {"qtype": "choice", "difficulty": "hard", "answer": "1", "explanation": ""},
    ]
    qc = compute_qc(QUALIFIED, objective="掌握光合作用的过程", quizzes=quizzes)
    check = next(c for c in qc["checks"] if c["key"] == "quiz")
    assert check["level"] == "warn"
    assert "第 2 题缺答案" in check["detail"]
    assert "第 3 题缺解析" in check["detail"]


def test_quiz_check_ok_when_every_quiz_has_answer_and_explanation():
    quizzes = [
        {"qtype": "choice", "difficulty": "easy", "answer": "0", "explanation": "解析一"},
        {"qtype": "short", "difficulty": "hard", "answer": "答案文本", "explanation": "解析二"},
    ]
    qc = compute_qc(QUALIFIED, objective="掌握光合作用的过程", quizzes=quizzes)
    check = next(c for c in qc["checks"] if c["key"] == "quiz")
    assert check["level"] == "ok", check


def test_quiz_check_does_not_judge_payloads_without_answer_fields():
    """旧调用方只传 qtype/difficulty（没有答案数据可判）→ 不误报（不凭空判 warn）。"""
    quizzes = [{"qtype": "choice", "difficulty": "easy"},
               {"qtype": "short", "difficulty": "hard"}]
    qc = compute_qc(QUALIFIED, objective="掌握光合作用的过程", quizzes=quizzes)
    check = next(c for c in qc["checks"] if c["key"] == "quiz")
    assert check["level"] == "ok", check


def test_quiz_check_treats_numeric_zero_answer_as_present():
    """答案以数字 0 上报（正确答案是第一个选项）→ 是「有答案」，不能被 falsy 吞成缺答案。"""
    quizzes = [{"qtype": "choice", "difficulty": "easy", "answer": 0, "explanation": "解析一"},
               {"qtype": "choice", "difficulty": "hard", "answer": "1", "explanation": "解析二"}]
    qc = compute_qc(QUALIFIED, objective="掌握光合作用的过程", quizzes=quizzes)
    check = next(c for c in qc["checks"] if c["key"] == "quiz")
    assert check["level"] == "ok", check


# ---------------------------------------------------------------------------
# ④ 存量重渲染：只替换「随堂练习」小节，正文一字不动、幂等
# ---------------------------------------------------------------------------

def test_rerender_quiz_section_replaces_only_practice_block():
    class _Row:
        qtype, difficulty = "choice", "easy"
        question = "温度=0 时输出？"
        options = list(OPTIONS)
        answer = "0"
        explanation = "选概率最高的词。"

    markdown = ("## 引入\n\n正文第一段。\n\n## 随堂练习\n\n"
                "### 第 1 题 · 选择题（难度：易）\n\n温度=0 时输出？\n\n"
                "- 输出高度确定，重复率高\n- 输出更随机\n- 与温度无关\n- 一定更准确\n\n"
                "**解析**：选概率最高的词。\n\n---\n")
    once = rerender_quiz_section(markdown, [_Row()])
    twice = rerender_quiz_section(once, [_Row()])
    assert twice == once                                  # 幂等
    assert once.startswith("## 引入\n\n正文第一段。\n\n## 随堂练习")   # 正文没动
    assert f"**答案**：{OPTIONS[0]}" in once
    assert f"✓  {OPTIONS[0]}" in once


def test_rerender_quiz_section_without_practice_block_is_noop():
    assert rerender_quiz_section("## 只有正文\n\n没有练习。\n", []) == "## 只有正文\n\n没有练习。\n"
