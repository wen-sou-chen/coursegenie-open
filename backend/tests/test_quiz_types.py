# -*- coding: utf-8 -*-
"""任务 55：题型扩展（calc / draw / material / experiment）+ 分层作业 + 开放题评分要点。

规格见 HANDOFF「任务 53–55 说明」§C。四处口径：

1. **题型**：``quiz_rules.QUIZ_TYPES`` 七种；开放题（``draw / material / experiment``）没有
   唯一答案 ⇒ 校验的是**评分要点 rubric**（缺了就就这道题重问一次，仍缺则题面加
   ``【评分要点待人工确认】`` 并记进 ``meta.quiz_issues`` —— 绝不静默丢题）；
2. **分层**：``level ∈ {基础, 提升, 挑战}``（缺省按难度兜底）落在 ``quiz_items.meta``，
   讲义「随堂练习」与交付件按层分组；
3. **时长**：每题 ``minutes``（缺省按题型兜底、夹 1..60），小节底部给总时长；超过
   ``workload_limit(stage)`` 时**显式提示但不是 fail**（不动 10 项判据 / 权重 / QC_VERSION）；
4. **既有库补列**：``ensure_sqlite_columns()`` 幂等补 ``quiz_items.meta``，非 SQLite 交 Alembic。

铁律 #7：全部走临时三件套（``conftest.py`` 已把 DB / STORAGE_DIR / VAULT_ROOT 指向临时目录）。
"""
from __future__ import annotations

import json

from sqlalchemy import create_engine
from sqlalchemy import inspect as sa_inspect

from app.db import session as session_mod
from app.db.session import SessionLocal
from app.models.course import ContentItem, Course, OutlineNode, QuizItem
from app.services import delivery
from app.services import lecture as lecture_mod
from app.services import lecture_qc
from app.services.llm import MockLLMClient
from app.services.quiz_rules import (
    DEFAULT_MINUTES,
    LEVELS,
    MANUAL_RUBRIC_PREFIX,
    OPEN_TYPES,
    QUIZ_TYPES,
    TYPE_LABEL,
    WORKLOAD_LIMITS,
    mark_manual,
    normalize_quiz,
    quiz_minutes,
    quiz_problem,
    resolve_quiz_list,
    row_meta,
    workload_limit,
)

from tests.test_lecture_qc import QUALIFIED, _add_sections, _node_id, _plan_approve, _wait_job

# 四类新题型：分层三层全覆盖，时长各异，开放题带评分要点
NEW_TYPE_QUIZZES = [
    {"qtype": "calc", "difficulty": "easy", "level": "基础", "minutes": 5,
     "question": "已知光反应的能量转换效率，计算 1 mol CO2 固定所需光子数。",
     "options": [], "answer": "代入 n = hc/λ 后按摩尔数换算。", "explanation": "考察公式代入。"},
    {"qtype": "draw", "difficulty": "medium", "level": "提升", "minutes": 8,
     "question": "在方框内画出光反应与暗反应的衔接示意图。", "options": [], "answer": "",
     "rubric": "① 标出两个阶段 ② 画出 ATP/NADPH 的传递 ③ 箭头方向正确",
     "explanation": "作图题看的是结构与标注。"},
    {"qtype": "material", "difficulty": "medium", "level": "提升", "minutes": 6,
     "question": "阅读材料，指出大棚增产措施分别作用于哪一步反应。", "options": [], "answer": "",
     "rubric": "① 指认措施 ② 对上光反应或暗反应 ③ 说明理由",
     "explanation": "材料题看的是证据与结论是否对应。"},
    {"qtype": "experiment", "difficulty": "hard", "level": "挑战", "minutes": 10,
     "question": "设计实验验证光照强度对光合速率的影响。", "options": [], "answer": "",
     "rubric": "① 变量控制 ② 观测指标 ③ 结论与数据一致",
     "explanation": "实验探究题看变量、指标与结论是否自洽。"},
]


class _LectureStub(MockLLMClient):
    """讲义返回指定 markdown + quizzes（订正调用按脚本回放）。"""

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
            payload = (self.fixes.pop(0) if self.fixes else {"item": {}})
            return json.dumps(payload, ensure_ascii=False)
        return super().complete(system, user, max_tokens=max_tokens)


def _generate(client, course_id: str, node_id: str, monkeypatch, quizzes: list[dict],
              markdown: str = QUALIFIED, fixes: list[dict] | None = None):
    stub = _LectureStub(markdown, quizzes, fixes)
    monkeypatch.setattr(lecture_mod, "build_llm_client", lambda: stub)
    resp = client.post("/api/lecture/generate", json={
        "course_id": course_id, "node_id": node_id, "tone": "正式"})
    assert resp.status_code == 200, resp.text
    state = _wait_job(client, resp.json()["job_id"])
    assert state["status"] == "success", state
    return state["result"]["item"], stub


def _prepare(client, course_id: str) -> str:
    _plan_approve(client, course_id)
    _add_sections(course_id, "1", ["光合作用的过程"])
    return _node_id(client, course_id, "1.1")


def _quiz_meta_rows(item_id: str) -> list[tuple[str, dict]]:
    db = SessionLocal()
    try:
        item = db.get(ContentItem, item_id)
        return [(q.qtype, dict(q.meta or {})) for q in lecture_qc.quiz_rows_of(db, item)]
    finally:
        db.close()


def _check_of(result: dict, key: str) -> dict:
    return next(c for c in result["qc"]["checks"] if c["key"] == key)


# ---------------------------------------------------------------------------
# A. 题型：七种都在册；开放题判「评分要点」而不是答案
# ---------------------------------------------------------------------------

def test_seven_qtypes_all_have_labels_and_open_types_declared():
    assert QUIZ_TYPES == ("choice", "blank", "short", "calc", "draw", "material", "experiment")
    assert set(TYPE_LABEL) == set(QUIZ_TYPES)
    assert OPEN_TYPES == ("draw", "material", "experiment")
    assert set(OPEN_TYPES) < set(QUIZ_TYPES)


def test_open_types_need_rubric_not_answer():
    for qtype in OPEN_TYPES:
        bare = normalize_quiz({"qtype": qtype, "question": "按题意作答"})
        assert "没有评分要点" in quiz_problem(bare), qtype
        ok = normalize_quiz({"qtype": qtype, "question": "按题意作答",
                             "answer": "", "rubric": "① 要点一 ② 要点二"})
        assert quiz_problem(ok) == "", qtype
    # 题干为空仍然报（开放题也要有题面）
    assert quiz_problem(normalize_quiz({"qtype": "draw", "rubric": "① 画线"})) == "作图题没有题干"


def test_closed_types_still_need_answer():
    assert "没有答案" in quiz_problem(normalize_quiz({"qtype": "calc", "question": "算一算"}))
    assert quiz_problem(normalize_quiz({"qtype": "calc", "question": "算一算",
                                        "answer": "42"})) == ""
    # 未知题型退回 choice（不是静默丢题）
    fallback = normalize_quiz({"qtype": "essay", "question": "怎么看？", "options": []})
    assert fallback["qtype"] == "choice"
    assert "选择题只有 0 个选项" in quiz_problem(fallback)


# ---------------------------------------------------------------------------
# B. 分层与时长：归一 + 兜底 + 常量表
# ---------------------------------------------------------------------------

def test_level_is_validated_and_falls_back_by_difficulty():
    assert LEVELS == ("基础", "提升", "挑战")
    assert normalize_quiz({"qtype": "choice", "level": "挑战"})["level"] == "挑战"
    assert normalize_quiz({"qtype": "choice", "level": "特难"})["level"] == ""
    assert row_meta({"difficulty": "easy", "qtype": "choice"})["level"] == "基础"
    assert row_meta({"difficulty": "medium", "qtype": "choice"})["level"] == "提升"
    assert row_meta({"difficulty": "hard", "qtype": "choice"})["level"] == "挑战"
    # 显式分层优先于难度兜底
    assert row_meta({"difficulty": "hard", "qtype": "draw",
                     "level": "基础"})["level"] == "基础"


def test_minutes_defaults_per_qtype_and_is_clamped():
    assert DEFAULT_MINUTES["experiment"] == 10
    assert normalize_quiz({"qtype": "experiment"})["minutes"] == 10
    assert normalize_quiz({"qtype": "calc", "minutes": "7"})["minutes"] == 7
    assert normalize_quiz({"qtype": "calc", "minutes": "abc"})["minutes"] == DEFAULT_MINUTES["calc"]
    assert normalize_quiz({"qtype": "calc", "minutes": 0})["minutes"] == DEFAULT_MINUTES["calc"]
    assert normalize_quiz({"qtype": "calc", "minutes": 999})["minutes"] == 60
    # 读侧：meta 优先、缺 meta 按题型兜底
    assert quiz_minutes({"meta": {"minutes": 12}, "qtype": "draw"}) == 12
    assert quiz_minutes(QuizItem(qtype="draw", difficulty="easy")) == DEFAULT_MINUTES["draw"]


def test_workload_limit_table_by_stage():
    assert WORKLOAD_LIMITS == {"小学": 60, "初中": 90, "高中": 90}
    assert workload_limit("小学", "数学") == {"limit": 60, "label": "小学（数学）"}
    assert workload_limit("小学三年级", "语文")["limit"] == 60
    assert workload_limit("初中", "生物") == {"limit": 90, "label": "初中"}
    assert workload_limit("高中", "物理")["limit"] == 90
    assert workload_limit("", "") == {"limit": 90, "label": "通用"}


# ---------------------------------------------------------------------------
# C. 开放题缺 rubric：重试一次 → 仍缺则显式标注（题不丢）
# ---------------------------------------------------------------------------

def test_open_quiz_rubric_repaired_by_single_retry():
    fixes = [{"item": {"qtype": "draw", "question": "在方框内画出示意图。",
                       "rubric": "① 起点 ② 箭头 ③ 结论", "explanation": "解析"}}]
    stub = _LectureStub("", [], fixes)
    fields, issues = resolve_quiz_list([{"qtype": "draw", "question": "在方框内画出示意图。"}],
                                       client=stub)
    assert stub.fix_calls == 1
    assert issues == []
    assert fields[0]["rubric"].startswith("①")
    assert quiz_problem(fields[0]) == ""


def test_open_quiz_rubric_still_missing_is_marked_not_dropped():
    stub = _LectureStub("", [], [{"item": {"qtype": "experiment", "question": "设计实验。"}}])
    fields, issues = resolve_quiz_list([{"qtype": "experiment", "question": "设计实验。"}],
                                       client=stub)
    assert stub.fix_calls == 1
    assert len(fields) == 1                                    # 题目不丢
    assert fields[0]["question"].startswith(MANUAL_RUBRIC_PREFIX)
    assert issues and "评分要点" in issues[0]["problem"]
    # 幂等：再标一次不会叠前缀
    assert mark_manual(fields[0], "没有评分要点")["question"] == fields[0]["question"]


# ---------------------------------------------------------------------------
# D. 质检：开放题缺 rubric 点名到题号；有 rubric 则 ok
# ---------------------------------------------------------------------------

def test_quiz_check_points_at_missing_rubric_only_for_open_types():
    quizzes = [
        {"qtype": "choice", "difficulty": "easy", "answer": "0", "explanation": "解析",
         "rubric": "", "level": "基础"},
        {"qtype": "draw", "difficulty": "hard", "answer": "", "explanation": "解析",
         "rubric": "", "level": "挑战"},
    ]
    result = lecture_qc._quiz_check(quizzes)
    assert result["level"] == "warn"
    assert "第 2 题缺评分要点" in result["detail"]
    assert "第 1 题缺答案" not in result["detail"]              # choice 有答案，不该被点名


def test_quiz_check_ok_when_open_quiz_has_rubric():
    quizzes = [
        {"qtype": "choice", "difficulty": "easy", "answer": "0", "explanation": "解析",
         "rubric": "", "level": "基础"},
        {"qtype": "experiment", "difficulty": "hard", "answer": "", "explanation": "解析",
         "rubric": "① 变量控制 ② 观测指标 ③ 结论", "level": "挑战"},
    ]
    result = lecture_qc._quiz_check(quizzes)
    assert result["level"] == "ok"
    assert "评分要点" not in result["detail"]


# ---------------------------------------------------------------------------
# E. 端到端：四类新题型落库 + 讲义分层渲染 + 质检 ok
# ---------------------------------------------------------------------------

def test_new_types_persist_render_in_layers_and_qc_stays_ok(client, course_id, monkeypatch):
    node = _prepare(client, course_id)
    item, stub = _generate(client, course_id, node, monkeypatch, NEW_TYPE_QUIZZES)
    assert stub.fix_calls == 0                                  # 四道题都合规，不触发订正

    rows = _quiz_meta_rows(item["id"])
    assert [qtype for qtype, _ in rows] == ["calc", "draw", "material", "experiment"]
    assert [meta.get("level") for _, meta in rows] == ["基础", "提升", "提升", "挑战"]
    assert [meta.get("minutes") for _, meta in rows] == [5, 8, 6, 10]
    assert rows[1][1]["rubric"].startswith("① 标出两个阶段")

    md = str(item["body"]["markdown"])
    assert "## 随堂练习" in md
    for heading in ("### 基础", "### 提升", "### 挑战"):
        assert heading in md, heading
    assert md.index("### 基础") < md.index("### 提升") < md.index("### 挑战")
    assert "#### 第 2 题 · 作图题（难度：中，约 8 分钟）" in md
    assert "**评分要点**：① 标出两个阶段" in md
    assert "> **作图区**：请在此作答" in md
    assert "> **实验记录区**：请在此作答" in md
    assert "预计用时 **29 分钟**" in md                          # 5+8+6+10
    assert item["meta"]["quiz_issues"] == []

    db = SessionLocal()
    try:
        row = db.get(ContentItem, item["id"])
        course = db.get(Course, course_id)
        outline_node = db.get(OutlineNode, node)
        result = lecture_qc.build_for_item(db, course, outline_node, row,
                                           lecture_qc.lecture_markdown(row))
    finally:
        db.close()
    quiz_check = _check_of(result, "quiz")
    assert quiz_check["level"] == "ok", quiz_check


def test_missing_rubric_survives_to_lecture_and_qc_names_the_question(client, course_id,
                                                                     monkeypatch):
    node = _prepare(client, course_id)
    quizzes = [{"qtype": "draw", "difficulty": "easy", "level": "基础", "minutes": 4,
                "question": "画出细胞结构简图。", "options": [], "answer": "",
                "explanation": "看图说话。"}]
    item, stub = _generate(client, course_id, node, monkeypatch, quizzes,
                           fixes=[{"item": {"qtype": "draw", "question": "画出细胞结构简图。"}}])
    assert stub.fix_calls == 1                                  # 重试过一次
    assert item["meta"]["quiz_issues"][0]["problem"].endswith("没有评分要点")

    md = str(item["body"]["markdown"])
    assert MANUAL_RUBRIC_PREFIX in md                            # 题面显式标注，题没丢

    db = SessionLocal()
    try:
        row = db.get(ContentItem, item["id"])
        course = db.get(Course, course_id)
        result = lecture_qc.build_for_item(db, course, db.get(OutlineNode, node), row,
                                           lecture_qc.lecture_markdown(row))
    finally:
        db.close()
    assert _check_of(result, "quiz")["level"] == "warn"
    assert "第 1 题缺评分要点" in _check_of(result, "quiz")["detail"]


def test_over_workload_limit_is_only_a_hint_not_a_fail(client, course_id, monkeypatch):
    """4 道题合计 116 分钟（单题夹到 60 上限内）—— 超建议上限只提示、不 fail。"""
    node = _prepare(client, course_id)
    heavy = [dict(q) for q in NEW_TYPE_QUIZZES]
    heavy[0]["minutes"] = 50
    heavy[1]["minutes"] = 50
    item, _stub = _generate(client, course_id, node, monkeypatch, heavy)
    md = str(item["body"]["markdown"])
    assert "预计用时 **116 分钟**" in md                          # 50+50+6+10
    assert "⚠️" in md                                            # 超过建议上限只提示

    db = SessionLocal()
    try:
        row = db.get(ContentItem, item["id"])
        course = db.get(Course, course_id)
        result = lecture_qc.build_for_item(db, course, db.get(OutlineNode, node), row,
                                           lecture_qc.lecture_markdown(row))
    finally:
        db.close()
    assert _check_of(result, "quiz")["level"] == "ok"             # 超时长不参与 10 项判据
    assert _check_of(result, "quiz").get("downgraded") is not True


# ---------------------------------------------------------------------------
# F. 三处一致：讲义 md / 交付 md / 交付 HTML / 交付 PDF
# ---------------------------------------------------------------------------

def test_layers_and_minutes_agree_in_delivery_md_and_html(client, course_id, monkeypatch):
    node = _prepare(client, course_id)
    item, _stub = _generate(client, course_id, node, monkeypatch, NEW_TYPE_QUIZZES)
    lecture_md = str(item["body"]["markdown"])

    db = SessionLocal()
    try:
        row = db.get(ContentItem, item["id"])
        delivery_md = delivery._delivery_lecture_markdown(row)
    finally:
        db.close()
    html = delivery.markdown_to_html(delivery_md)

    for text, where in ((lecture_md, "讲义 md"), (delivery_md, "交付 md"), (html, "交付 HTML")):
        assert "### 基础" in text or "<h3>基础</h3>" in text, where
        assert "29 分钟" in text, where
        assert "评分要点" in text, where
    assert "<h3>挑战</h3>" in html
    assert "预计用时" in html


def test_layers_and_minutes_survive_pdf_print(client, course_id, monkeypatch, tmp_path):
    edge = delivery.find_edge()
    if edge is None:
        import pytest
        pytest.skip("本机没有 Microsoft Edge，跳过 PDF 打印断言")
    from pypdf import PdfReader

    node = _prepare(client, course_id)
    item, _stub = _generate(client, course_id, node, monkeypatch, NEW_TYPE_QUIZZES)
    db = SessionLocal()
    try:
        row = db.get(ContentItem, item["id"])
        delivery_md = delivery._delivery_lecture_markdown(row)
    finally:
        db.close()

    html_path = tmp_path / "quiz.html"
    html_path.write_text(delivery.html_page("随堂练习", delivery.markdown_to_html(delivery_md)),
                         encoding="utf-8", newline="")
    pdf_path = tmp_path / "quiz.pdf"
    result = delivery.print_pdf(html_path, pdf_path, edge=edge)
    assert result["ok"], result
    text = "\n".join((page.extract_text() or "") for page in PdfReader(str(pdf_path)).pages)
    assert "随堂练习" in text
    assert "基础" in text and "挑战" in text                      # 三层标题进 PDF 文本层
    assert "预计用时" in text and "29 分钟" in text
    assert "评分要点" in text


# ---------------------------------------------------------------------------
# G. 既有库补列：幂等；非 SQLite 交 Alembic
# ---------------------------------------------------------------------------

def test_ensure_sqlite_columns_is_idempotent(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    legacy = create_engine(url)
    with legacy.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE quiz_items (id VARCHAR(36) PRIMARY KEY)")
    monkeypatch.setattr(session_mod, "engine", legacy)
    monkeypatch.setattr(session_mod.settings, "database_url", url)

    assert session_mod.ensure_sqlite_columns() == ["quiz_items.meta"]
    assert session_mod.ensure_sqlite_columns() == []              # 第二次：已存在，不重复补
    names = {col["name"] for col in sa_inspect(legacy).get_columns("quiz_items")}
    assert "meta" in names
    with legacy.begin() as conn:
        conn.exec_driver_sql("INSERT INTO quiz_items (id, meta) VALUES ('q1', '{}')")
    legacy.dispose()


def test_ensure_sqlite_columns_skips_non_sqlite(monkeypatch):
    monkeypatch.setattr(session_mod.settings, "database_url", "postgresql://u@localhost/cg")
    assert session_mod.ensure_sqlite_columns() == []


def test_quiz_item_meta_column_exists_with_dict_default():
    """新列 ``quiz_items.meta`` 声明为 JSON + 默认空 dict（旧行缺 meta 不报错）。"""
    column = QuizItem.__table__.c.meta
    assert column.default is not None
    assert isinstance(QuizItem.__table__.c.options.type, type(column.type))
