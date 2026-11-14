"""任务 50 学案（学生版）回归测试：答案全部消失、作答区与页码、docx 可读回、笔记库与交付包同步。

协作规则第 7 条：DATABASE_URL / STORAGE_DIR / OBSIDIAN_VAULT_ROOT 由 conftest 统一指向
临时目录，本文件不碰真实库与真实知识库。
"""
from __future__ import annotations

import json
import re
import tempfile
import time
from pathlib import Path

from app.db.session import SessionLocal
from app.models.course import ContentItem, Course, Outline, OutlineNode, QuizItem
from app.services import delivery as delivery_mod
from app.services import docx_export
from app.services import kb as kb_mod
from app.services import lesson_plan
from app.services import lecture as lecture_mod
from app.services import quiz_rules
from app.services import student_sheet
from app.services.llm import MockLLMClient

# 讲义正文里直接写着答案（「类囊体薄膜」「二氧化碳和水」），学案必须把它们挖空
LECTURE_MD = """## 光合作用的过程

### 引言
光合作用是绿色植物利用光能把二氧化碳和水变成有机物并释放氧气的过程。

### 核心讲解
- **光反应**发生在类囊体薄膜上，把光能变成 ATP
- **暗反应**在叶绿体基质中把二氧化碳固定成糖
- 反应的总产物是葡萄糖和氧气

### 随堂练习
1. 光反应发生的场所在哪里？（答案：类囊体薄膜）
2. 光合作用的原料是什么？（答案：二氧化碳和水）

### 小结
- 光反应供能、暗反应固碳
"""

QUIZZES = [
    {"qtype": "blank", "difficulty": "easy", "question": "光反应发生的场所在哪里？",
     "answer": "类囊体薄膜", "explanation": "光反应在类囊体薄膜上进行。"},
    {"qtype": "short", "difficulty": "medium", "question": "光合作用的原料是什么？",
     "answer": "二氧化碳和水", "explanation": "原料是二氧化碳和水。"},
    {"qtype": "choice", "difficulty": "hard", "question": "光合作用的产物之一是？",
     "options": ["氧气", "氮气", "甲烷"], "answer": 0, "explanation": "光合作用释放氧气。"},
]


class _StubLLM(MockLLMClient):
    def complete(self, system: str, user: str, *, max_tokens: int = 2000) -> str:
        if "LECTURE_JSON" in system:
            return json.dumps({"markdown": LECTURE_MD, "summary": "光合作用",
                               "keywords": ["光反应"], "quizzes": QUIZZES}, ensure_ascii=False)
        return super().complete(system, user, max_tokens=max_tokens)


def _course_with_lecture(client, course_id, monkeypatch) -> dict:
    assert client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "生物光合作用", "num_chapters": 1}).status_code == 200
    assert client.post("/api/plan/approve", json={
        "course_id": course_id, "approved": True}).status_code == 200
    db = SessionLocal()
    try:
        outline = (db.query(Outline).filter(Outline.course_id == course_id)
                   .order_by(Outline.created_at.desc()).first())
        node = OutlineNode(outline_id=outline.id, node_key="1.1", node_type="section",
                           title="光合作用的过程",
                           objective="能说出光反应与暗反应的场所；能写出光合作用的原料",
                           teaching_strategy="先用实验引入", assessment="随堂提问", order_index=1)
        db.add(node)
        db.commit()
        node_id = node.id
    finally:
        db.close()
    monkeypatch.setattr(lecture_mod, "build_llm_client", lambda: _StubLLM())
    resp = client.post("/api/lecture/generate",
                       json={"course_id": course_id, "node_id": node_id, "tone": "正式"})
    job_id = resp.json()["job_id"]
    state = None
    for _ in range(200):
        state = client.get(f"/api/content/jobs/{job_id}").json()
        if state["status"] in ("success", "failed"):
            break
        time.sleep(0.1)
    assert state and state["status"] == "success", state
    return {"node_id": node_id, "item": state["result"]["item"]}


def _bind(client, course_id: str, root: Path, out_rel: str = "课程/光合作用") -> None:
    assert client.post(f"/api/kb/course/{course_id}/bind",
                       json={"vault_root": str(root), "output_rel": out_rel}).status_code == 200


def _answers(course_id: str, node_id: str) -> list[str]:
    db = SessionLocal()
    try:
        quizzes = (db.query(QuizItem)
                   .filter(QuizItem.course_id == course_id, QuizItem.node_id == node_id).all())
        return [(str(q.answer or ""), str(q.explanation or "")) for q in quizzes]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 答案必须全部消失
# ---------------------------------------------------------------------------

# 任务 48s 的现场形态：讲义的练习小节是**嵌套标题**（## 随堂练习 → ### 基础/挑战 →
# #### 第 N 题），子标题本身不带「随堂练习」字样 —— 只看小节自己的标题就会整块漏进学案。
NESTED_PRACTICE_LECTURE = """# 1.1 光合作用的过程

## 光合作用的过程

### 核心讲解
- **光反应**发生在类囊体薄膜上，把光能变成 ATP

## 随堂练习

### 基础

#### 第 1 题 · 选择题（难度：易，约 2 分钟）

光反应发生的场所是？
✓  类囊体薄膜
-  线粒体基质
**答案**：类囊体薄膜
**解析**：光反应在类囊体薄膜上进行。

### 挑战

#### 第 2 题 · 简答题（难度：难，约 5 分钟）

说说暗反应在哪里进行。
**评分要点**：答出叶绿体基质即得满分，漏答一半。
**答案**：叶绿体基质

## 来源

- （来源:《光合作用导论》第 1 段）
"""


def _render_sheet(course_id: str, node_id: str, lecture: str) -> str:
    db = SessionLocal()
    try:
        return student_sheet.sheet_markdown(db, db.get(Course, course_id),
                                            db.get(OutlineNode, node_id), lecture)
    finally:
        db.close()


def test_student_sheet_drops_nested_practice_block(client, course_id, monkeypatch):
    """任务 48s：嵌套在练习小节里的 ✓ / 答案 / 解析 / 评分要点整块不进学案。"""
    ctx = _course_with_lecture(client, course_id, monkeypatch)
    sheet = _render_sheet(course_id, ctx["node_id"], NESTED_PRACTICE_LECTURE)

    assert "✓" not in sheet
    for leaked in ("**答案**", "**解析**", "**评分要点**",
                   "线粒体基质",                       # 练习选项
                   "光反应在类囊体薄膜上进行",          # 解析正文
                   "答出叶绿体基质即得满分"):          # 开放题评分要点
        assert leaked not in sheet, leaked
    assert not [line for line in sheet.splitlines() if line.strip().startswith("＿＿＿：")
                and line.strip() not in ("＿＿＿：", "＿＿＿：＿＿＿")], "残留「＿＿＿：答案正文」"
    assert "光反应" in sheet, "讲义正文本身照旧保留"

    # 反向断言：教师侧讲义照旧带答案与 ✓（学案只是派生视图）
    db = SessionLocal()
    try:
        item = (db.query(ContentItem)
                .filter(ContentItem.course_id == course_id, ContentItem.node_id == ctx["node_id"],
                        ContentItem.kind == "lecture").one())
        lecture_md = item.body["markdown"]
    finally:
        db.close()
    assert "**答案**" in lecture_md and "✓" in lecture_md


def test_student_sheet_shows_level_minutes_and_answer_slot(client, course_id, monkeypatch):
    """任务 48s 护栏：分层（基础/提升/挑战）与预计用时照旧显示，每题都有作答位。"""
    ctx = _course_with_lecture(client, course_id, monkeypatch)
    sheet = _render_sheet(course_id, ctx["node_id"], NESTED_PRACTICE_LECTURE)
    db = SessionLocal()
    try:
        quizzes = (db.query(QuizItem)
                   .filter(QuizItem.course_id == course_id, QuizItem.node_id == ctx["node_id"])
                   .order_by(QuizItem.created_at).all())
        expected = [(str(q.qtype), quiz_rules.quiz_level(q), quiz_rules.quiz_minutes(q))
                    for q in quizzes]
    finally:
        db.close()
    assert expected, "前置条件：本节点必须有练习"
    assert {level for _, level, _ in expected} == set(quiz_rules.LEVELS), expected
    for index, (qtype, level, minutes) in enumerate(expected, 1):
        header = f"### 第 {index} 题（{qtype} · {level} · 约 {minutes} 分钟）"
        assert header in sheet, header
    blocks = re.split(r"^#{2,4}\s*第\s*\d+\s*题", sheet, flags=re.M)[1:]
    assert len(blocks) == len(expected), blocks
    for index, block in enumerate(blocks, 1):
        assert re.search(r"[　\s]*答[：:]", block), f"第 {index} 题缺作答位"
TABLE_LECTURE = """# 1.1 光合作用的过程

## 光合作用的过程

### 核心讲解

| 阶段 | 场所 | 产物 |
| --- | --- | --- |
| 光反应 | 类囊体薄膜 | ATP |
| 暗反应 | 叶绿体基质 | 糖 |

### 小结

- 光反应供能、暗反应固碳
"""


def test_student_sheet_keeps_table_separator(client, course_id, monkeypatch):
    """任务 48s 相邻修复：正文表格要留住分隔行。

    没有 ``| --- |`` 的 Markdown 表格在 Obsidian / Word 里只是一串竖线文本，
    学生读到的是 ``| 阶段 | 场所 | 产物 |``（真实包子课学案就是这样）。
    """
    ctx = _course_with_lecture(client, course_id, monkeypatch)
    sheet = _render_sheet(course_id, ctx["node_id"], TABLE_LECTURE)
    lines = sheet.splitlines()
    assert "| 阶段 | 场所 | 产物 |" in lines, "表头丢了"
    for index, line in enumerate(lines):
        if set(line.strip()) <= set("|-: ") and line.strip().startswith("|"):
            assert lines[index - 1].strip().startswith("|"), "分隔行必须紧跟表头"
            break
    else:
        raise AssertionError("学案丢了表格分隔行")
    assert "| 暗反应 | 叶绿体基质 | 糖 |" in sheet, "表格数据行照旧保留"


def test_student_sheet_redacts_other_lessons_answers(client, course_id, monkeypatch):
    """任务 48s：候选池是**整门课** —— 别的课时的成句答案也不许出现在本课时学案里。

    同一门课的知识点互相引用很正常（AI 课的「多模型交叉验证」写在多篇讲义正文里），
    但学案是发给学生的，任何一道课的答案原句都不该出现在里面（验收口径 = 整课候选池）。
    """
    ctx = _course_with_lecture(client, course_id, monkeypatch)
    leaked = "把光能变成 ATP"
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        node = db.get(OutlineNode, ctx["node_id"])
        other = OutlineNode(outline_id=node.outline_id, node_key="1.2", node_type="section",
                            title="另一个课时", order_index=2)
        db.add(other)
        db.flush()
        db.add(QuizItem(course_id=course_id, node_id=other.id, qtype="blank",
                        difficulty="medium", question="光反应把光能变成了什么？",
                        answer=leaked, explanation="先变成活跃化学能，再转成稳定的化学能。"))
        db.commit()
        sheet = student_sheet.sheet_markdown(db, course, node, NESTED_PRACTICE_LECTURE)
    finally:
        db.close()
    assert leaked not in sheet, "别的课时的答案原句漏进了本课时学案"
    assert "＿＿＿" in sheet, "该处应挖空成待填横线"


def test_student_sheet_hides_every_answer_and_explanation(client, course_id, monkeypatch):
    ctx = _course_with_lecture(client, course_id, monkeypatch)
    rows = _answers(course_id, ctx["node_id"])
    assert rows, "前置条件：本节点必须有练习"
    resp = client.post(f"/api/lecture/course/{course_id}/student-sheets")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1 and body["created"] == 1, body

    db = SessionLocal()
    try:
        item = db.get(ContentItem, body["items"][0]["item_id"])
        sheet = item.body["markdown"]
    finally:
        db.close()
    assert "《光合作用的过程》学案" in sheet
    checked = 0
    for answer, explanation in rows:
        if answer and not answer.isdigit():
            assert answer not in sheet, f"学案里泄露答案：{answer}"
            checked += 1
        if explanation:
            assert explanation not in sheet, f"学案里泄露解析：{explanation}"
            checked += 1
    assert checked >= 4, "至少校验了 4 条答案/解析"
    # 作答区与留空都在
    assert "答：" in sheet and "＿＿＿" in sheet
    assert "## 四、我的疑问" in sheet
    # 讲义里夹带的答案标记（「（答案：…）」）与解析行都被清掉
    assert "（答案：" not in sheet and "解析：" not in sheet


def test_student_sheet_docx_reads_back_and_has_page_numbers(client, course_id, monkeypatch):
    ctx = _course_with_lecture(client, course_id, monkeypatch)
    item_id = client.post(f"/api/lecture/course/{course_id}/student-sheets").json()["items"][0]["item_id"]
    resp = client.get(f"/api/lecture/student-sheet/{item_id}.docx")
    assert resp.status_code == 200, resp.text
    assert resp.content[:2] == b"PK"
    text = docx_export.docx_text(resp.content)
    assert "《光合作用的过程》学案" in text
    assert "姓名" in text
    for answer, explanation in _answers(course_id, ctx["node_id"]):
        if answer and not answer.isdigit():
            assert answer not in text, f"Word 里泄露答案：{answer}"
        if explanation:
            assert explanation not in text
    assert "第" in docx_export.docx_footer_text(resp.content), "学案 Word 必须带页码"


def test_student_sheet_idempotent_and_zero_llm(client, course_id, monkeypatch):
    ctx = _course_with_lecture(client, course_id, monkeypatch)

    def _boom():
        raise AssertionError("学案不许调用 LLM")

    monkeypatch.setattr(lecture_mod, "build_llm_client", _boom)
    first = client.post(f"/api/lecture/course/{course_id}/student-sheets").json()
    second = client.post(f"/api/lecture/course/{course_id}/student-sheets").json()
    assert first["created"] == 1 and second["created"] == 0 and second["changed"] == 0, second
    assert len(second["items"]) == 1
    db = SessionLocal()
    try:
        assert db.query(ContentItem).filter(ContentItem.course_id == course_id,
                                            ContentItem.kind == student_sheet.KIND).count() == 1
    finally:
        db.close()


def test_student_sheet_without_lecture_marks_reason(client, course_id):
    """学生版是讲义的派生视图：没有讲义时写清原因，不留空坑。"""
    assert client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "生物光合作用", "num_chapters": 1}).status_code == 200
    assert client.post("/api/plan/approve", json={
        "course_id": course_id, "approved": True}).status_code == 200
    db = SessionLocal()
    try:
        outline = (db.query(Outline).filter(Outline.course_id == course_id)
                   .order_by(Outline.created_at.desc()).first())
        node = OutlineNode(outline_id=outline.id, node_key="1.1", node_type="section",
                           title="还没有讲义的课时", order_index=1)
        db.add(node)
        db.commit()
        course = db.get(Course, course_id)
        sheet = student_sheet.sheet_markdown(db, course, node, "")
    finally:
        db.close()
    assert "还没有讲义正文" in sheet
    assert "未配置练习" in sheet


# ---------------------------------------------------------------------------
# 笔记库与交付包
# ---------------------------------------------------------------------------

def test_student_sheet_lands_in_notes_library(client, course_id, monkeypatch):
    _course_with_lecture(client, course_id, monkeypatch)
    root = Path(tempfile.mkdtemp(prefix="coursegenie_sheet_notes_"))
    _bind(client, course_id, root)
    exported = client.post(f"/api/kb/course/{course_id}/export").json()
    out = root / exported["output_rel"]
    sheets = list((out / "学案").rglob("*-学案.md"))
    assert len(sheets) == 1, exported["files"]
    assert "## 三、随堂练习" in sheets[0].read_text(encoding="utf-8")

    verify = client.post(f"/api/kb/course/{course_id}/verify").json()
    rows = [row for row in verify["nodes"] if row["part"] == "student-sheet"]
    assert rows and all(row["status"] == "consistent" for row in rows), rows
    assert verify["inconsistent"] == 0, [r for r in verify["nodes"] if r["status"] != "consistent"]


def test_delivery_package_ships_student_sheet_dir(client, course_id, monkeypatch):
    _course_with_lecture(client, course_id, monkeypatch)
    resp = client.post(f"/api/delivery/course/{course_id}/build", json={"pdf": "off"})
    assert resp.status_code == 200, resp.text
    plan = resp.json()
    assert plan["counts"]["student_sheets"] == 1, plan["counts"]
    assert plan["counts"]["sheet_docx_ok"] == 1

    out = delivery_mod.deliveries_root() / plan["build_id"]
    md_files = list((out / "学案").rglob("*-学案.md"))
    docx_files = list((out / "学案").rglob("*-学案.docx"))
    assert len(md_files) == 1 and len(docx_files) == 1
    text = docx_export.docx_text(docx_files[0].read_bytes())
    assert "《光合作用的过程》学案" in text
    assert "类囊体薄膜" not in text, "交付的学案里不许出现答案"

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    paths = {row["path"] for row in manifest["files"]}
    assert any(path.startswith("学案/") and path.endswith(".docx") for path in paths), paths
    assert "`学案/`" in (out / "00-交付说明.md").read_text(encoding="utf-8")
    assert "学案" in (out / "01-验收单.md").read_text(encoding="utf-8")
    # 答案版留在教师侧：讲义（含答案与解析）仍在本包里
    assert any(path.startswith("讲义/") for path in paths), paths


def test_delivery_reports_missing_student_sheet_dir(client, course_id):
    assert client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "生物光合作用", "num_chapters": 1}).status_code == 200
    assert client.post("/api/plan/approve", json={
        "course_id": course_id, "approved": True}).status_code == 200
    plan = client.post(f"/api/delivery/course/{course_id}/build", json={"pdf": "off"}).json()
    assert plan["counts"]["student_sheets"] == 0
    reasons = {row["item"]: row["reason"] for row in plan["missing"]}
    assert "学案/" in reasons and "讲义" in reasons["学案/"]


# ---------------------------------------------------------------------------
# 任务 151：填空后的括号不许写答案
# ---------------------------------------------------------------------------

BLANK_PAREN_LECTURE = """# 1.1 填空括号探针

## 填空括号探针

### 核心讲解
- **控制模型采样随机性的超参数**（控制模型采样随机性的超参数）越高，输出越发散。
- **AI 幻觉**（AI 生成的看似流畅却与事实不符的内容）会降低信任。
- **知识盲区**（来源:《破解AI幻觉（成因）》第 49 段）需要补素材。
- **连续引用**（来源:《破解AI幻觉》第 49 段）（来源:《AI降幻五法》第 56 段）也要清干净。
- **温度**（℃）要控制在合理范围。

### 小结
- 先看温度，再看采样参数。
"""


def test_student_sheet_never_puts_answers_in_blank_parentheses(client, course_id, monkeypatch):
    """任务 151：真库 19 处 ``＿＿＿（…）`` 必须在派生学案里归零。

    两类真实形态都要覆盖：括号就是答案（定义/解释），以及嵌套来源括号。
    """
    ctx = _course_with_lecture(client, course_id, monkeypatch)
    sheet = _render_sheet(course_id, ctx["node_id"], BLANK_PAREN_LECTURE)

    assert not re.search(r"＿{2,}[（(]", sheet), "填空后仍紧跟括号"
    for leaked in ("控制模型采样随机性的超参数",
                   "AI 生成的看似流畅却与事实不符的内容",
                   "来源:", "第 49 段", "第 56 段"):
        assert leaked not in sheet, f"填空括号里仍泄露：{leaked}"
    assert "＿＿＿　（想想看：它和什么有关？）" in sheet
    assert "＿＿＿　（℃）" in sheet, "短提示应保留，但不能紧贴填空"
    assert student_sheet.strip_leaks(sheet) == sheet, "兜底清洗必须幂等"
