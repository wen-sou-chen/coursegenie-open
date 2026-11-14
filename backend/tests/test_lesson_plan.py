"""任务 49 教案产出物回归测试：栏目齐全、来源标注、docx 可读回、笔记库与交付包落地、幂等。

协作规则第 7 条：DATABASE_URL / STORAGE_DIR / OBSIDIAN_VAULT_ROOT 由 conftest 统一指向
临时目录，本文件不碰真实库与真实知识库。
"""
from __future__ import annotations

import hashlib
import io
import json
import tempfile
import time
import zipfile
from collections import Counter
from pathlib import Path

from app.db.session import SessionLocal
from app.models.course import ContentItem, Course, Outline, OutlineNode
from app.services import delivery as delivery_mod
from app.services import docx_export
from app.services import lesson_plan
from app.services import lecture as lecture_mod
from app.services.lecture_qc import NO_BASIS_MARK
from app.services.llm import MockLLMClient

LECTURE_MD = """## 光合作用的过程

### 引入
- 阳光下的叶片会悄悄把空气中的二氧化碳变成糖（来源: 光合作用导论 #1）

### 核心讲解
- **光反应**发生在类囊体薄膜上，把光能变成 ATP（来源: 光合作用导论 #2）
- **暗反应**在基质中把二氧化碳固定成糖

### 踩过的坑
- 学生常把「光反应」和「暗反应」的场所记反（来源: 光合作用导论 #4）

### 小结
- 光反应供能、暗反应固碳（来源: 光合作用导论 #3）
"""

QUIZZES = [{"qtype": "choice", "difficulty": "easy", "question": "光反应在哪里？",
           "options": ["类囊体薄膜", "基质"], "answer": 0, "explain": "类囊体薄膜"}]

SECTIONS = ("## 一、基本信息", "## 二、教学目标", "## 三、教学重点与难点", "## 四、教学准备",
            "## 五、教学过程", "## 六、评估方式", "## 七、板书设计", "## 八、作业设计",
            "## 九、教学反思")


class _StubLLM(MockLLMClient):
    """讲义返回固定 Markdown；其他调用走 mock 默认。"""

    def complete(self, system: str, user: str, *, max_tokens: int = 2000) -> str:
        if "LECTURE_JSON" in system:
            return json.dumps({"markdown": LECTURE_MD, "summary": "光合作用",
                               "keywords": ["光反应"], "quizzes": QUIZZES}, ensure_ascii=False)
        return super().complete(system, user, max_tokens=max_tokens)


class _RawLLM(MockLLMClient):
    """按用例指定的正文返回讲义（任务 48s 的构造样本用）。"""

    def __init__(self, markdown: str) -> None:
        self.markdown = markdown

    def complete(self, system: str, user: str, *, max_tokens: int = 2000) -> str:
        if "LECTURE_JSON" in system:
            return json.dumps({"markdown": self.markdown, "summary": "光合作用",
                               "keywords": ["光反应"], "quizzes": QUIZZES}, ensure_ascii=False)
        return super().complete(system, user, max_tokens=max_tokens)


class _TaggedLLM(_RawLLM):
    """把固定讲义里的课题换成另一课时 —— 用来生成「两课时的教案」做套话比对。"""

    def __init__(self, title: str) -> None:
        super().__init__(LECTURE_MD.replace("光合作用", title))


def _plan_approve(client, course_id: str) -> None:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "生物光合作用", "num_chapters": 1})
    assert resp.status_code == 200, resp.text
    assert client.post("/api/plan/approve", json={
        "course_id": course_id, "approved": True}).status_code == 200


def _add_section(course_id: str, key: str, title: str, **fields) -> None:
    db = SessionLocal()
    try:
        outline = (db.query(Outline).filter(Outline.course_id == course_id)
                   .order_by(Outline.created_at.desc()).first())
        base = db.query(OutlineNode).filter(OutlineNode.outline_id == outline.id).count()
        db.add(OutlineNode(outline_id=outline.id, node_key=key, node_type="section",
                           title=title, order_index=base + 1, **fields))
        db.commit()
    finally:
        db.close()


def _node_id(client, course_id: str, key: str) -> str:
    plan = client.get(f"/api/plan/{course_id}").json()
    return next(n["id"] for n in plan["nodes"] if n["key"] == key)


def _gen_lecture(client, course_id: str, node_id: str, monkeypatch, stub=None) -> dict:
    monkeypatch.setattr(lecture_mod, "build_llm_client", stub or (lambda: _StubLLM()))
    resp = client.post("/api/lecture/generate",
                       json={"course_id": course_id, "node_id": node_id, "tone": "正式"})
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]
    state = None
    for _ in range(200):
        state = client.get(f"/api/content/jobs/{job_id}").json()
        if state["status"] in ("success", "failed"):
            break
        time.sleep(0.1)
    assert state and state["status"] == "success", state
    return state["result"]["item"]


def _bind(client, course_id: str, root: Path, out_rel: str = "课程/光合作用") -> None:
    resp = client.post(f"/api/kb/course/{course_id}/bind",
                       json={"vault_root": str(root), "output_rel": out_rel})
    assert resp.status_code == 200, resp.text


def _course_with_lecture(client, course_id, monkeypatch) -> dict:
    _plan_approve(client, course_id)
    _add_section(course_id, "1.1", "光合作用的过程",
                 objective="能说出光反应与暗反应的场所；能写出光合作用的原料与产物",
                 teaching_strategy="先用银边天竺葵实验引入；再分光反应与暗反应讲清场所",
                 assessment="随堂提问两个反应的场所，能答对即为达成")
    node_id = _node_id(client, course_id, "1.1")
    item = _gen_lecture(client, course_id, node_id, monkeypatch)
    return {"node_id": node_id, "item": item}


# ---------------------------------------------------------------------------
# 栏目与来源标注
# ---------------------------------------------------------------------------

def test_lesson_plan_has_all_columns(client, course_id, monkeypatch):
    ctx = _course_with_lecture(client, course_id, monkeypatch)
    resp = client.post(f"/api/lecture/course/{course_id}/lesson-plans")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1 and body["created"] == 1, body

    items = body["items"]
    assert len(items) == 1 and items[0]["node_key"] == "1.1", items
    row = client.get(f"/api/lecture/course/{course_id}/lesson-plans").json()["items"][0]
    item = SessionLocal().get(ContentItem, row["item_id"])
    markdown = item.body["markdown"]
    for heading in SECTIONS:
        assert heading in markdown, heading
    # 表格式：基本信息与教学过程都是 Markdown 表格，且教学过程带时间分配
    assert "| 项目 | 内容 |" in markdown
    assert "| 环节 | 时间 | 教师活动 | 学生活动 | 设计意图 | 来源 |" in markdown
    assert "分钟" in markdown
    # 来源标注逐条保留；反思栏留空占位
    assert "(来源: 大纲·课时目标)" in markdown
    assert "(来源: 讲义 1.1「核心讲解」)" in markdown
    assert lesson_plan.REFLECTION_PLACEHOLDER in markdown
    # 难点优先取讲义「踩过的坑」
    assert "(来源: 讲义「踩过的坑」)" in markdown


def test_lesson_plan_marks_missing_sections_with_reason(client, course_id):
    """没有讲义也能出骨架：缺项写原因 + 无依据标记，不留空栏。"""
    _plan_approve(client, course_id)
    _add_section(course_id, "1.1", "还没有讲义的课时")
    db = SessionLocal()
    try:
        node = db.query(OutlineNode).filter(OutlineNode.node_key == "1.1").first()
        course = db.get(Course, course_id)
        markdown = lesson_plan.plan_markdown(db, course, node, "")
    finally:
        db.close()
    assert NO_BASIS_MARK in markdown
    assert "还没有讲义正文" in markdown
    assert "未配置练习" in markdown


# ---------------------------------------------------------------------------
# 任务 48s：教学过程不套话 / 不留空标签
# ---------------------------------------------------------------------------

def _process_cells(markdown: str) -> list[str]:
    """教学过程表格里的每个单元格（含 教师活动 / 学生活动 / 设计意图 / 来源 / 环节 / 时间）。"""
    seg = markdown.split("## 五、教学过程", 1)[1].split("\n## ", 1)[0]
    cells: list[str] = []
    for line in seg.splitlines():
        line = line.strip()
        if not line.startswith("|") or set(line) <= set("|-: "):
            continue
        parts = [cell.strip() for cell in line.strip("|").split("|")]
        if parts[:2] == ["环节", "时间"]:
            continue
        cells += [cell for cell in parts if cell]
    return cells


def test_lesson_plan_process_cells_are_not_boilerplate(client, course_id, monkeypatch):
    """同一门课的多份教案放在一起，教学过程不能逐字重复同一句套话（任务 48s）。"""
    _plan_approve(client, course_id)
    lessons = (("1.1", "细胞呼吸"), ("1.2", "蒸腾作用"))
    for key, title in lessons:
        _add_section(course_id, key, title,
                     objective=f"能说出{title}的要点；能举出一个例子",
                     teaching_strategy=f"用「{title}」的演示实验引入",
                     assessment=f"随堂提问「{title}」，能答对即为达成")
    for _key, title in lessons:
        _gen_lecture(client, course_id, _node_id(client, course_id, _key), monkeypatch,
                     stub=lambda t=title: _TaggedLLM(t))

    body = client.post(f"/api/lecture/course/{course_id}/lesson-plans").json()
    assert body["total"] == 2, body
    cells: list[str] = []
    for row in body["items"]:
        item = SessionLocal().get(ContentItem, row["item_id"])
        cells += _process_cells(item.body["markdown"])
    dupes = sorted({cell for cell, count in Counter(cells).items()
                    if count >= 2 and len(cell) >= 12})
    assert not dupes, dupes[:3]
    # 每句话都带本课时自己的信息：两课时的学生活动必须不同
    titles = {title for _key, title in lessons}
    assert sum(1 for cell in cells if "同桌互相复述一遍" in cell) >= 2
    assert all(any(title in cell for title in titles)
               for cell in cells if "同桌互相复述一遍" in cell)


EMPTY_LABEL_LECTURE = """## 光合作用的过程

### 核心讲解
- **光反应**发生在类囊体薄膜上

### 踩过的坑
- **易错点：**
- 学生常把「光反应」和「暗反应」的场所记反
"""

MULTILINE_STRATEGY = ("1. 教师逐句解释诗歌，借助注释理解大意\n"
                      "2. 学生分组讨论，想象诗中画面\n"
                      "3. 教师引导学生说出诗人当时的心情")


def test_lesson_plan_table_cells_never_break_rows(client, course_id, monkeypatch):
    """任务 48s：大纲的教学策略是多行时，必须压成一行再进单元格，不能把表行拆断。

    现场（K-12 试点课）：``teaching_strategy`` 写成一串「1.…\\n2.…\\n3.…」，原样进单元格后
    Markdown 表格被拆成好几行 —— 老师的 Word 里表格错位，机器按行审还会把拆出来的
    「2. 学生分组讨论，想象诗中画面」当成独立句子，判成跨课时重复套话。
    """
    _plan_approve(client, course_id)
    _add_section(course_id, "1.1", "光合作用的过程",
                 objective="能说出光反应的场所",
                 teaching_strategy=MULTILINE_STRATEGY,
                 assessment="随堂提问")
    _gen_lecture(client, course_id, _node_id(client, course_id, "1.1"), monkeypatch)
    body = client.post(f"/api/lecture/course/{course_id}/lesson-plans").json()
    markdown = SessionLocal().get(ContentItem, body["items"][0]["item_id"]).body["markdown"]

    rows = [line for line in markdown.splitlines() if line.strip().startswith("|")]
    assert rows, "教案本体是表格"
    for line in rows:
        assert line.strip().endswith("|"), f"表行被拆断：{line!r}"
        assert "\n" not in line
    # 三行策略缩成一行进表格：整句（含课题）唯一，且标题与内容都还在
    assert "学生分组讨论，想象诗中画面" in markdown
    lead_in_row = next(line for line in rows if "引出课题" in line)
    assert "光合作用的过程" in lead_in_row and "1. 教师逐句解释诗歌" in lead_in_row


def test_lesson_plan_drops_label_only_cells(client, course_id, monkeypatch):
    """只有标签没有内容的表行整行不写（任务 48s）——「易错点：」不留空标签。"""
    _plan_approve(client, course_id)
    _add_section(course_id, "1.1", "光合作用的过程", objective="能说出光反应的场所")
    monkeypatch.setattr(lecture_mod, "build_llm_client", lambda: _RawLLM(EMPTY_LABEL_LECTURE))
    _gen_lecture(client, course_id, _node_id(client, course_id, "1.1"), monkeypatch)
    body = client.post(f"/api/lecture/course/{course_id}/lesson-plans").json()
    item = SessionLocal().get(ContentItem, body["items"][0]["item_id"])
    markdown = item.body["markdown"]
    assert "易错点：" not in markdown
    assert "学生常把「光反应」和「暗反应」的场所记反" in markdown, "有内容的难点照旧保留"
    assert not [line for line in markdown.splitlines()
                if line.strip().startswith("|") and line.strip().endswith("|") and "： |" in line]


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------

def test_lesson_plan_is_idempotent(client, course_id, monkeypatch):
    _course_with_lecture(client, course_id, monkeypatch)
    first = client.post(f"/api/lecture/course/{course_id}/lesson-plans").json()
    db = SessionLocal()
    try:
        before_markdown = db.get(ContentItem, first["items"][0]["item_id"]).body["markdown"]
        before_meta = dict(db.get(ContentItem, first["items"][0]["item_id"]).meta)
    finally:
        db.close()

    second = client.post(f"/api/lecture/course/{course_id}/lesson-plans").json()
    assert second["total"] == 1 and second["created"] == 0 and second["changed"] == 0, second
    assert second["items"][0]["item_id"] == first["items"][0]["item_id"]
    assert len(second["items"]) == 1, "幂等：不产生第二条教案"

    db = SessionLocal()
    try:
        item = db.get(ContentItem, first["items"][0]["item_id"])
        assert item.body["markdown"] == before_markdown
        assert item.meta["generated_at"] == before_meta["generated_at"], "正文没变不刷新时间戳"
        assert db.query(ContentItem).filter(ContentItem.course_id == course_id,
                                            ContentItem.kind == lesson_plan.KIND).count() == 1
    finally:
        db.close()


def test_lesson_plan_generation_makes_zero_llm_calls(client, course_id, monkeypatch):
    _course_with_lecture(client, course_id, monkeypatch)

    def _boom():
        raise AssertionError("教案骨架不许调用 LLM")

    monkeypatch.setattr(lecture_mod, "build_llm_client", _boom)
    assert client.post(f"/api/lecture/course/{course_id}/lesson-plans").status_code == 200


# ---------------------------------------------------------------------------
# Word 导出
# ---------------------------------------------------------------------------

def test_lesson_plan_docx_round_trip_chinese(client, course_id, monkeypatch):
    _course_with_lecture(client, course_id, monkeypatch)
    item_id = client.post(f"/api/lecture/course/{course_id}/lesson-plans").json()["items"][0]["item_id"]
    resp = client.get(f"/api/lecture/lesson-plan/{item_id}.docx")
    assert resp.status_code == 200, resp.text
    assert resp.content[:2] == b"PK", "必须是真正的 docx 包"
    text = docx_export.docx_text(resp.content)
    assert "《光合作用的过程》教案" in text
    assert "教学反思" in text
    assert "类囊体薄膜" in text          # 表格里的中文能读回


DOCX_DETERMINISM_MARKDOWN = "# 标题\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n- 甲\n- 乙\n"


def test_markdown_to_docx_is_deterministic():
    """同一输入两次生成逐字节相同（任务 49）。"""
    markdown = DOCX_DETERMINISM_MARKDOWN
    assert docx_export.markdown_to_docx(markdown, title="T") == \
        docx_export.markdown_to_docx(markdown, title="T")


def test_markdown_to_docx_zip_timestamps_are_fixed(monkeypatch):
    """任务 59：ZIP 条目时间戳固定 ⇒ 隔几秒、甚至隔一年生成仍逐字节相同。

    起因：python-docx 写包时给每个条目盖上「生成那一刻」的 MS-DOS 时间戳
    （2 秒精度），全量 pytest 跑得慢时两次调用会跨过 2 秒边界 ⇒ 偶发变红。
    """
    markdown = DOCX_DETERMINISM_MARKDOWN
    first = docx_export.markdown_to_docx(markdown, title="T")

    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        infos = archive.infolist()
    assert len(infos) == 17, len(infos)          # 与改造前同一个包：条目数不变
    assert {info.date_time for info in infos} == {docx_export.ZIP_EPOCH}
    assert not any(info.date_time[0] == time.localtime().tm_year for info in infos)
    text = docx_export.docx_text(first)          # 归一化后仍能正常读回
    assert "乙" in text and "甲" in text

    # 「跨秒」不用真 sleep：把墙钟拨走（含跨年），字节必须一模一样
    real_localtime = time.localtime
    base = time.mktime((2026, 9, 19, 12, 0, 0, 0, 0, -1))

    for shift in (2, 3, 365 * 24 * 3600):
        monkeypatch.setattr(time, "localtime",
                            lambda *a, _s=shift, **kw: real_localtime(base + _s))
        later = docx_export.markdown_to_docx(markdown, title="T")
        assert later == first, shift
        assert hashlib.sha1(later).hexdigest() == hashlib.sha1(first).hexdigest()


# ---------------------------------------------------------------------------
# 笔记库与交付包
# ---------------------------------------------------------------------------

def test_lesson_plan_lands_in_notes_library(client, course_id, monkeypatch):
    """笔记库拿可编辑的 markdown 教案页（Word/PDF 是交付件，见下一个用例）。"""
    _course_with_lecture(client, course_id, monkeypatch)
    root = Path(tempfile.mkdtemp(prefix="coursegenie_plan_notes_"))
    _bind(client, course_id, root)
    exported = client.post(f"/api/kb/course/{course_id}/export").json()
    out = root / exported["output_rel"]

    plans = list((out / "教案").rglob("*-教案.md"))
    assert len(plans) == 1, exported["files"]
    assert "## 五、教学过程" in plans[0].read_text(encoding="utf-8")

    verify = client.post(f"/api/kb/course/{course_id}/verify").json()
    plan_rows = [row for row in verify["nodes"] if str(row["part"]).startswith("lesson-plan")]
    assert plan_rows and all(row["status"] == "consistent" for row in plan_rows), plan_rows
    assert verify["inconsistent"] == 0, [row for row in verify["nodes"]
                                         if row["status"] != "consistent"]


def test_delivery_package_ships_lesson_dir(client, course_id, monkeypatch):
    _course_with_lecture(client, course_id, monkeypatch)
    resp = client.post(f"/api/delivery/course/{course_id}/build", json={"pdf": "off"})
    assert resp.status_code == 200, resp.text
    plan = resp.json()
    assert plan["counts"]["lesson_plans"] == 1, plan["counts"]

    out = delivery_mod.deliveries_root() / plan["build_id"]
    md_files = list((out / "教案").rglob("*-教案.md"))
    docx_files = list((out / "教案").rglob("*-教案.docx"))
    assert len(md_files) == 1 and len(docx_files) == 1
    assert "| 环节 | 时间 |" in md_files[0].read_text(encoding="utf-8")

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    paths = {row["path"] for row in manifest["files"]}
    assert any(path.startswith("教案/") and path.endswith(".docx") for path in paths), paths
    readme = (out / "00-交付说明.md").read_text(encoding="utf-8")
    assert "`教案/`" in readme
    accept = (out / "01-验收单.md").read_text(encoding="utf-8")
    assert "教案" in accept


def test_delivery_reports_missing_lesson_dir_without_lectures(client, course_id):
    _plan_approve(client, course_id)
    _add_section(course_id, "1.1", "还没有讲义的课时")
    resp = client.post(f"/api/delivery/course/{course_id}/build", json={"pdf": "off"})
    assert resp.status_code == 200, resp.text
    plan = resp.json()
    assert plan["counts"]["lesson_plans"] == 0
    reasons = {row["item"]: row["reason"] for row in plan["missing"]}
    assert "教案/" in reasons and "讲义" in reasons["教案/"]
