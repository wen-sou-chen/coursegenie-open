"""任务 151：学生版（地图 → 微单元 → 即时自测 → 复习卡）+ 两个答案泄漏。"""
from __future__ import annotations

import base64
import json
import re

from app.db.session import SessionLocal
from app.models.course import ContentItem, Course, Outline, OutlineNode, QuizItem
from app.services import student_mode
from tests.test_deck_html import _seed_deck
from tests.test_delivery import _new_course

POINT_CODE = "学生版探针"


def _lecture_md(code: str, title: str) -> str:
    return f"""## {title}

### 引入
{code} 的引入段：先说清楚这件事为什么重要（来源:《理论-总览（{code}）》第 3 段）。

### 核心讲解
#### {code} 子标题
- **{code} 要点一**：这是第一段核心讲解，带一个可识别的答案词 {code}-核心一。
- **{code} 要点二**：这是第二段核心讲解，带一个可识别的答案词 {code}-核心二。
- **{code} 要点三**：这是第三段核心讲解。

### 举例
- **{code} 例子**：在真实场景里这样用（来源:《{code} 手册（实操）》第 7 段）。

### 踩过的坑
- **{code} 坑一**：最常见的错误是跳过温度检查（来源:《{code} 手册》第 9 段）。
- **{code} 坑二**：第二个错误是忽略时间窗口。

### 小结
- **{code} 小结**：记住核心一和核心二，再看边界。

### 本节不覆盖什么
- **{code} 边界**：本节不覆盖多菌共生的完整体系（来源:《{code} 边界（说明）》第 11 段）。

## 随堂练习

### 基础

#### 第 1 题 · 选择题（难度：易，约 3 分钟）

{code} 的核心一是什么？

- 干扰选项甲
✓ {code}-核心一
- 干扰选项乙

**答案**：1

**解析**：{code}-核心一 就是本节的第一个要点（来源:《{code} 解析（嵌套）》第 12 段）。

---

### 提升

#### 第 2 题 · 填空题（难度：中，约 3 分钟）

{code} 的填空答案是：__________。

**答案**：{code}-填空答案

**解析**：填空答案来自讲义原文（来源:《{code} 解析》第 13 段）。

---

### 挑战

#### 第 3 题 · 简答题（难度：难，约 5 分钟）

请简述 {code} 的核心一和核心二。

**答案**：{code}-简答参考答案，包含核心一与核心二。

**解析**：按要点作答即可（来源:《{code} 解析（延伸）》第 14 段）。

---
"""


def _seed_course(client) -> str:
    """3 章 × 2 知识点 × 3 题；题目故意倒序入库，验证按层级稳定排序。"""
    course_id = _new_course(client, POINT_CODE)
    db = SessionLocal()
    order = 0
    try:
        outline = Outline(course_id=course_id, title="学生版探针大纲", goal="", content={})
        db.add(outline)
        db.flush()
        for chapter_index in range(3):
            chapter = OutlineNode(
                outline_id=outline.id, node_key=str(chapter_index + 1), node_type="chapter",
                title=f"{chapter_index + 1} 第{chapter_index + 1}章", order_index=order,
            )
            db.add(chapter)
            order += 1
            db.flush()
            for point_index in range(2):
                code = f"P{chapter_index + 1}{point_index + 1}"
                point = OutlineNode(
                    outline_id=outline.id, node_key=f"{chapter_index + 1}.{point_index + 1}",
                    node_type="point", title=f"{chapter_index + 1}.{point_index + 1} 知识点{point_index + 1}",
                    order_index=order,
                )
                db.add(point)
                order += 1
                db.flush()
                db.add(ContentItem(
                    course_id=course_id, node_id=point.id, kind="lecture",
                    title=f"知识点{point_index + 1}",
                    body={"markdown": _lecture_md(code, f"知识点{point_index + 1}")}, meta={},
                ))
                rows = [
                    ("short", f"请简述 {code} 的核心一和核心二。", [],
                     f"{code}-简答参考答案，包含核心一与核心二。", "挑战", 2),
                    ("blank", f"{code} 的填空答案是：__________。", [],
                     f"{code}-填空答案", "提升", 1),
                    ("choice", f"{code} 的核心一是什么？",
                     ["干扰选项甲", f"{code}-核心一", "干扰选项乙"], "1", "基础", 0),
                ]
                for qtype, question, options, answer, level, minutes in rows:
                    db.add(QuizItem(
                        course_id=course_id, node_id=point.id, qtype=qtype, question=question,
                        options=options, answer=answer,
                        explanation=f"{code} 解析（来源:《{code} 解析（嵌套）》第 12 段）。",
                        meta={"level": level, "minutes": minutes, "rubric": ""},
                    ))
        db.commit()
    finally:
        db.close()
    return course_id


def _data(course_id: str) -> dict:
    db = SessionLocal()
    try:
        return student_mode.build_course_data(db, db.get(Course, course_id))
    finally:
        db.close()


def _walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def test_student_data_has_expected_scale_and_shape(client):
    course_id = _seed_course(client)
    data = _data(course_id)

    assert len(data["chapters"]) == 3
    assert sum(len(chapter["points"]) for chapter in data["chapters"]) == 6
    assert data["total_units"] == 18
    assert data["total_quizzes"] == 18
    assert len(data["cards"]) == 6

    for chapter in data["chapters"]:
        assert chapter["minutes"] == 12
        for point in chapter["points"]:
            assert len(point["units"]) == 3
            assert [unit["quiz"]["type"] for unit in point["units"]] == ["choice", "fill", "short"]
            assert all(unit["quiz"]["type_label"] in ("选择题", "填空题", "简答题")
                       for unit in point["units"])
            assert all(unit["one_line"] for unit in point["units"])
            assert point["units"][0]["title"] == "先搞清楚：这到底是怎么回事"
            assert point["units"][1]["title"] == "怎么用：具体怎么做"
            assert point["units"][2]["title"] == "别踩坑：最容易错在哪"

    assert data["chapters"][0]["title"] == "第1章"
    assert data["chapters"][0]["points"][0]["title"] == "知识点1"
    assert all("这一节要记住什么" in card["front"] for card in data["cards"])


def test_h4_subheading_stays_inside_core_section(client):
    course_id = _seed_course(client)
    db = SessionLocal()
    try:
        lecture = db.query(ContentItem).filter(ContentItem.course_id == course_id).first()
        sections = student_mode.split_sections(lecture.body["markdown"])
    finally:
        db.close()
    assert "P11 子标题" not in sections
    assert any("P11 子标题" in line for line in sections["核心讲解"])
    data = _data(course_id)
    units = data["chapters"][0]["points"][0]["units"]
    assert len(units) == 3
    assert any("P11 子标题" in item for unit in units for item in unit["points"])


def test_first_sentence_never_cuts_mid_sentence():
    assert student_mode.first_sentence("第一句。第二句。第三句。", limit=5) == "第一句。第二句。"
    assert student_mode.first_sentence("没有标点的一段话") == "没有标点的一段话"
    assert student_mode.first_sentence("要点标题", avoid="要点标题") == ""
    long_text = "这是一个很长很长而且没有任何句号的句子" * 8
    assert student_mode.first_sentence(long_text, limit=10) == long_text


def test_sources_are_stripped_with_nested_parentheses(client):
    assert student_mode.strip_sources("A（来源:《理论-总览（多菌共生）》第 3 段）B") == "AB"
    assert student_mode.strip_sources("A(来源:《X（Y）》第 9 段)B") == "AB"

    course_id = _seed_course(client)
    data = _data(course_id)
    text = "\n".join(_walk_strings(data))
    assert not re.search(r"第\s*\d+\s*段[）)]", text)
    assert "》第" not in text
    assert "来源:" not in text and "来源：" not in text


def test_student_html_hides_answers_and_is_single_file(client):
    course_id = _seed_course(client)
    data = _data(course_id)
    db = SessionLocal()
    try:
        html = student_mode.render_student_html(db, db.get(Course, course_id))
    finally:
        db.close()

    assert "**答案**" not in html and "**解析**" not in html
    for chapter in data["chapters"]:
        for point in chapter["points"]:
            for unit in point["units"]:
                quiz = unit["quiz"]
                answer = base64.b64decode(quiz["answer_b64"]).decode("utf-8")
                explanation = base64.b64decode(quiz["explain_b64"]).decode("utf-8")
                if len(answer) >= 8:
                    assert answer not in html, f"初始 HTML 泄露答案：{answer}"
                if len(explanation) >= 8:
                    assert explanation not in html, f"初始 HTML 泄露解析：{explanation}"

    for needle in ("const DATA_B64", "atob", "TextDecoder", "answer_b64", "correct_b64",
                   "explain_b64", "localStorage", "cg-student-", "function renderMap",
                   "function openUnit", "function renderCards", "function pick", "function judgeFill",
                   "function revealShort", "@media (max-width:420px)", "overflow-x:hidden"):
        assert needle in html, f"学生版模板缺 {needle}"
    assert "<script src=" not in html
    assert '<link rel="stylesheet"' not in html
    assert "http://" not in html and "https://" not in html

    open_unit = html[html.index("function openUnit("):html.index("function pick(")]
    assert "b64(" not in open_unit, "openUnit 初始渲染就解码了答案（答案会默认展开）"
    assert "answer_b64" not in open_unit and "explain_b64" not in open_unit
    show_answer = html[html.index("function showAnswer("):html.index("function markDone(")]
    assert "b64(quiz.answer_b64)" in show_answer and "b64(quiz.explain_b64)" in show_answer
    assert html.count('<div class="ans" id="ans"></div>') == 3

    match = re.search(r'const DATA_B64 = "([^"]*)";', html)
    assert match
    embedded = json.loads(base64.b64decode(match.group(1)).decode("utf-8"))
    assert embedded["total_units"] == 18
    embedded_quiz = embedded["chapters"][0]["points"][0]["units"][0]["quiz"]
    assert "answer_b64" in embedded_quiz
    assert "answer" not in embedded_quiz and "explanation" not in embedded_quiz


def test_student_endpoints_are_zero_llm_and_404_500(client, monkeypatch):
    course_id = _seed_course(client)
    monkeypatch.setattr("app.services.llm.build_llm_client",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("学生版不许调 LLM")))

    resp = client.get(f"/api/ppt/{course_id}/student")
    assert resp.status_code == 200, resp.text
    assert resp.json()["total_units"] == 18

    page = client.get(f"/api/ppt/{course_id}/student-html")
    assert page.status_code == 200, page.text
    assert "text/html" in page.headers["content-type"]
    assert "const DATA_B64" in page.text

    assert client.get("/api/ppt/no-such/student").status_code == 404
    assert client.get("/api/ppt/no-such/student-html").status_code == 404

    empty_id = _new_course(client, "空学生版")
    empty = client.get(f"/api/ppt/{empty_id}/student")
    assert empty.status_code == 404
    assert "大纲" in empty.text

    def boom(*args, **kwargs):
        raise RuntimeError("学生版渲染炸了（测试注入）")

    monkeypatch.setattr(student_mode, "render_student_html", boom)
    broken = client.get(f"/api/ppt/{course_id}/student-html")
    assert broken.status_code == 500
    assert "学生版渲染炸了（测试注入）" in broken.text


def test_student_endpoints_leave_existing_decks_unchanged(client):
    course_id = _seed_course(client)
    deck_id = _seed_deck(
        course_id, title="学生版回归课件", scope="course", chapter_key="",
        slides=[("课程封面", "开场", ["学生版回归"]), ("知识点", "正文", ["核心一"])],
        layouts=["cover", "point"],
    )
    before_html = client.get(f"/api/ppt/{deck_id}/html")
    before_presenter = client.get(f"/api/ppt/{deck_id}/presenter")
    assert before_html.status_code == 200, before_html.text
    assert before_presenter.status_code == 200, before_presenter.text

    assert client.get(f"/api/ppt/{course_id}/student").status_code == 200
    assert client.get(f"/api/ppt/{course_id}/student-html").status_code == 200

    after_html = client.get(f"/api/ppt/{deck_id}/html")
    after_presenter = client.get(f"/api/ppt/{deck_id}/presenter")
    assert before_html.content == after_html.content, "学生版不能改网页版课件的字节口径"
    assert before_presenter.content == after_presenter.content, "学生版不能改讲师模式的字节口径"
