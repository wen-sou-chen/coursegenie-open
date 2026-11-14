"""任务 86 §B4：学员版 / 教师版（答案可见性）测试 —— **0 LLM，只做确定性文本处理**。

四条口径（只加不改）：

1. **默认 = 现状**：`normalize_view` / 公开路由 / `public_content` 三处缺省都是 `teacher`
   —— 任何一处「顺手默认去答案」，老师自己看到的讲义就少一段而且没有任何提示；
2. **去答案的规则与学案同源**：复用 `student_sheet.strip_leaks`（讲义正文里
   `**答案**：` / `**解析**：` / `**评分要点**：` 整行删掉、选择题正确项的 `✓` 变回普通列表项），
   **题目与作答区保留**、处理**幂等**；
3. `student_rows`：讲义正文换成学员版、习题行的 `answer` / `explanation` **连键一起去掉**
   （任务 90 §A3 方案 A：清成空串会留下「键在但为空」的空壳 ⇒ 分享页学员版每张随堂练习卡
   都露出一个空的「答案：」标签，实测每门课 6 处可见），其余行原样返回；
4. **不调模型**：源码级护栏 —— `audience.py` / `student_sheet.py` 里不许出现任何模型入口。

样本用**真的渲染器**产出（`lecture._quiz_section`），不是手写的假 markdown ——
否则「按 `_quiz_section` 的渲染规则反向剥离」这句话就没有被真正验证。
"""
from __future__ import annotations

import pathlib

import pytest

from app.models.course import QuizItem
from app.services import audience, scenario
from app.services import student_sheet
from app.services.lecture import _quiz_section

#: 泄题标记（与 `student_sheet` 的规则同一套口径）。
LEAK_MARKS = ("**答案**", "**解析**", "**评分要点**", "✓")


def _quiz(**kw) -> QuizItem:
    """构造一个游离的习题对象（渲染器只读属性，不进库）。"""
    base = dict(qtype="choice", difficulty="easy", question="下列哪一项是发酵的主角？",
                options=["乳酸菌", "酵母菌", "大肠杆菌"], answer="1",
                explanation="酵母菌在无氧条件下把糖变成酒精与二氧化碳。",
                meta={"level": "基础", "minutes": 2})
    base.update(kw)
    return QuizItem(**base)


def _teacher_section() -> str:
    """教师版随堂练习小节（真渲染器产出）：选择题 + 开放题（带评分要点与作答区）。"""
    rows = [
        _quiz(),
        _quiz(qtype="material", difficulty="medium", question="读材料，说说发酵温度为什么关键。",
              options=[], answer="温度过高会杀死酵母，过低则发酵缓慢。",
              explanation="酵母的最适温度在 30℃ 上下。",
              meta={"level": "提升", "minutes": 5,
                    "rubric": "答出温度区间给 2 分，能解释原因再给 1 分。"}),
    ]
    return _quiz_section(rows)


# ------------------------------------------------------- A. 默认 = 现状（教师版）

def test_normalize_view_defaults_to_teacher():
    """非法 / 空 / 乱七八糟 ⇒ 一律 `teacher`；**绝不默认去答案**。"""
    for raw in (None, "", "  ", "TEACHER", "tEaChEr", "teacher", "unknown", "both",
                0, 1, False, True, [], {}, "student_teacher"):
        assert audience.normalize_view(raw) == audience.AUDIENCE_TEACHER, raw
        assert audience.is_student(raw) is False, raw


def test_only_exact_student_switches_the_view():
    """只有 `student`（大小写 / 空白不敏感）才切到学员版。"""
    for raw in ("student", "STUDENT", " Student ", "student\n"):
        assert audience.normalize_view(raw) == audience.AUDIENCE_STUDENT, raw
        assert audience.is_student(raw) is True, raw


def test_view_constants_are_the_documented_pair():
    assert audience.AUDIENCE_TEACHER == "teacher"
    assert audience.AUDIENCE_STUDENT == "student"
    assert audience.VIEWS == ("teacher", "student")
    assert audience.UNSET_VIEW == audience.AUDIENCE_TEACHER


# ------------------------------------------------------- B. 学员版正文（去答案）

def test_teacher_section_really_contains_leaks():
    """先证明样本**真的**带答案 —— 否则下面的「0 命中」是空的。"""
    teacher = _teacher_section()
    assert "**答案**：" in teacher
    assert "**解析**：" in teacher
    assert "**评分要点**：" in teacher
    # 渲染器的正确项是 `✓` + **两个空格**（`f"{marker} {opt}"`）⇒ 只钉 ✓ 本身
    assert "✓" in teacher
    assert "作答区" in teacher


def test_student_markdown_removes_every_leak_and_keeps_questions():
    teacher = _teacher_section()
    student = audience.student_markdown(teacher)
    for mark in LEAK_MARKS:
        assert mark not in student, f"学员版里还留着 {mark}"
    # 泄题的具体内容也不许留下（整行删掉 = 连答案正文一起消失）。
    # ⚠️ 别拿「酵母菌」这种**同时是选项**的词当判据：正确项本来就在选项表里，
    # 去掉 ✓ 之后学生没法从选项里看出哪个是对的 —— 这正是本任务的口径。
    for leak in ("酵母菌在无氧条件下", "最适温度在 30℃", "答出温度区间给 2 分",
                 "酵母的最适温度在 30℃ 上下"):
        assert leak not in student, f"学员版里还留着答案正文：{leak}"
    # 题目与作答区保留（学生要能答题）
    assert "下列哪一项是发酵的主角？" in student
    assert "乳酸菌" in student and "大肠杆菌" in student
    assert "读材料，说说发酵温度为什么关键。" in student
    assert "作答区" in student
    assert "## 随堂练习" in student


def test_student_markdown_keeps_options_as_a_plain_list():
    """`✓` 那条要变回**普通列表项**（只抹掉 ✓ 会让这一行掉出列表）。"""
    student = audience.student_markdown("- 甲\n✓ 乙\n- 丙\n")
    lines = [line for line in student.split("\n") if line.strip()]
    assert lines == ["- 甲", "- 乙", "- 丙"], lines


def test_student_markdown_is_idempotent():
    """幂等：跑第二遍与第一遍逐字相同（导出 / 分享可能重复过一遍）。"""
    teacher = _teacher_section()
    once = audience.student_markdown(teacher)
    assert audience.student_markdown(once) == once


def test_student_markdown_is_noop_on_clean_text():
    """本来就没有答案的正文**一字不改**（不许顺手改标点 / 空行）。"""
    clean = "# 一、正文\n\n发酵靠的是微生物。\n\n## 二、小结\n\n温度是关键。\n"
    assert audience.student_markdown(clean) == clean


def test_student_markdown_reuses_the_student_sheet_rule():
    """去答案的规则**只有一套**（学案那套）：学员版讲义 = 同一函数 + `✓` 归一。"""
    assert student_sheet.strip_leaks is not None
    sample = "**答案**：甲\n正文\n✓ 乙\n"
    assert audience.student_markdown(sample) == student_sheet.strip_leaks("正文\n- 乙\n")


# ------------------------------------------------------- C. 内容行（分享页 / 导出）

def test_student_rows_rewrites_lecture_and_drops_quiz_answer_keys():
    rows = [
        {"id": "a", "kind": "lecture", "body": {"markdown": _teacher_section(), "tone": "正式"},
         "meta": {}},
        {"id": "b", "kind": "quiz",
         "body": {"question": "发酵的主角是谁？", "answer": "酵母菌", "explanation": "无氧呼吸。"},
         "meta": {}},
    ]
    out = audience.student_rows(rows)
    assert len(out) == 2
    assert out[0]["body"]["tone"] == "正式"                      # 其余字段原样
    assert "**答案**" not in out[0]["body"]["markdown"]
    assert "发酵的主角是谁？" in out[1]["body"]["question"]      # 题面保留
    # 任务 90：**键一起去掉**（不是清成空串）—— 「键在但为空」会让前端渲染出空标签
    assert "answer" not in out[1]["body"]
    assert "explanation" not in out[1]["body"]
    assert out[1]["body"] == {"question": "发酵的主角是谁？"}


def test_student_quiz_body_drops_only_the_two_answer_keys():
    """习题行只去掉答案 / 解析：题面、选项、题型、难度等一律原样，且**不就地改**输入。"""
    row = {"id": "e", "kind": "quiz",
           "body": {"qtype": "choice", "difficulty": "easy", "question": "发酵的主角是谁？",
                    "options": ["乳酸菌", "酵母菌"], "answer": "1", "explanation": "无氧呼吸。"},
           "meta": {}}
    once = audience.student_rows([row])
    assert once[0]["body"] == {"qtype": "choice", "difficulty": "easy", "question": "发酵的主角是谁？",
                               "options": ["乳酸菌", "酵母菌"]}
    assert audience.student_rows(once) == once            # 幂等
    assert row["body"]["answer"] == "1"                  # 教师版那一行一个字没动
    assert audience.student_rows([row])[0] == once[0]


def test_student_rows_leaves_other_kinds_untouched():
    rows = [
        {"id": "c", "kind": "concept", "body": {"markdown": "**答案**：这是概念图"},
         "meta": {"x": 1}},
        {"id": "d", "kind": "ppt", "body": {"pptx": "x.pptx"}, "meta": {}},
    ]
    assert audience.student_rows(rows) == rows       # 概念图 / 课件不参与去答案
    assert audience.student_rows([]) == []
    # 脏数据（非 dict 行）原样带过，不许炸
    assert audience.student_rows([None, 3]) == [None, 3]      # type: ignore[list-item]


# ------------------------------------------------------- D. ⛔ 0 LLM 源码级护栏

@pytest.mark.parametrize("rel", ["app/services/audience.py", "app/services/student_sheet.py"])
def test_audience_path_never_calls_a_model(rel: str):
    """学员版是**确定性文本处理**：这两个文件里不许出现任何模型入口（任务 86 §B4 硬约束）。"""
    root = pathlib.Path(__file__).resolve().parents[1]
    source = (root / rel).read_text(encoding="utf-8")
    for needle in ("complete_json", "build_llm_client", "openai", "LLMClient"):
        assert needle not in source, f"{rel} 里出现了 {needle}（学员版必须 0 LLM）"


# ------------------------------------------------------- E. 与场景口径的联动

def test_auto_student_view_follows_self_study_scenario():
    """交付包「默认是否多出学员版讲义」只看 `self_study`（与 `delivery.py` 同一条规则）。"""
    self_study = scenario.scenario_meta(
        {"scenario": {"primary": "self_study", "selected": ["self_study"]}})
    recorded = scenario.scenario_meta(
        {"scenario": {"primary": "recorded", "selected": ["recorded"]}})
    assert "self_study" in (self_study.get("selected") or [])
    assert "self_study" not in (recorded.get("selected") or [])
    assert "self_study" not in (scenario.scenario_meta({}).get("selected") or [])
