"""任务 31（M5c 交付包）回归测试：打包结构、manifest sha1、缺项说明、幂等、0 LLM。

协作规则第 7 条：DATABASE_URL / STORAGE_DIR / OBSIDIAN_VAULT_ROOT 由 conftest 统一
指向临时目录，本文件不碰真实库与真实知识库。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.course import (ContentItem, Course, Outline, OutlineNode,
                                   SourceMaterial)
from app.services import audit as audit_mod
from app.services import chart as chart_mod
from app.services import delivery as delivery_mod
from app.services import kb as kb_mod
from app.services import lecture as lecture_mod
from app.services import lecture_qc as lecture_qc_mod
from app.services.llm import MockLLMClient

MINI_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="80">'
            '<rect width="200" height="80" fill="#ffffff" stroke="#333"/>'
            '<text x="10" y="45" font-size="16">光合作用</text></svg>')

QUADRANT_DATA = {
    "x_low": "不紧急", "x_high": "紧急", "y_low": "不重要", "y_high": "重要",
    "quadrants": [{"name": "计划做", "desc": "重要但不紧急，排进日程"},
                  {"name": "马上做", "desc": "又重要又紧急，立刻处理"},
                  {"name": "不要做", "desc": "既不重要也不紧急，直接删掉"},
                  {"name": "委托做", "desc": "紧急但不重要，交给别人"}],
}

LECTURE_MD = """## 光合作用的过程

### 引入
- 阳光下的叶片会悄悄把空气中的二氧化碳变成糖（来源: 光合作用导论 #1）

### 核心讲解
- **光反应**发生在类囊体薄膜上，把光能变成 ATP（来源: 光合作用导论 #2）
- **暗反应**在基质中把二氧化碳固定成糖

| 阶段 | 场所 |
|---|---|
| 光反应 | 类囊体薄膜 |
| 暗反应 | 叶绿体基质 |

```mermaid
flowchart TD
  A[光能] --> B[光反应]
  B --> C[暗反应]
```

![概念卡：光合作用](diagram://DIAGRAM_ID)

### 小结
- 光反应供能、暗反应固碳（来源: 光合作用导论 #3）
"""

QUIZZES = [{"qtype": "choice", "difficulty": "easy", "question": "光反应在哪里？",
           "options": ["类囊体薄膜", "基质"], "answer": 0, "explain": "类囊体薄膜"}]


class _StubLLM(MockLLMClient):
    """讲义返回固定 Markdown；其他调用走 mock 默认。"""

    def __init__(self, markdown: str) -> None:
        self.markdown = markdown

    def complete(self, system: str, user: str, *, max_tokens: int = 2000) -> str:
        if "LECTURE_JSON" in system:
            return json.dumps({"markdown": self.markdown, "summary": "光合作用",
                               "keywords": ["光反应"], "quizzes": QUIZZES}, ensure_ascii=False)
        return super().complete(system, user, max_tokens=max_tokens)


# ---------------------------------------------------------------------------
# 造课
# ---------------------------------------------------------------------------

def _plan_approve(client, course_id: str, num_chapters: int = 1) -> None:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "生物光合作用", "num_chapters": num_chapters})
    assert resp.status_code == 200, resp.text
    assert client.post("/api/plan/approve", json={
        "course_id": course_id, "approved": True}).status_code == 200


def _add_section(course_id: str, key: str, title: str) -> None:
    db = SessionLocal()
    try:
        outline = (db.query(Outline).filter(Outline.course_id == course_id)
                   .order_by(Outline.created_at.desc()).first())
        base = db.query(OutlineNode).filter(OutlineNode.outline_id == outline.id).count()
        db.add(OutlineNode(outline_id=outline.id, node_key=key, node_type="section",
                           title=title, objective="掌握光合作用", order_index=base + 1))
        db.commit()
    finally:
        db.close()


def _node_id(client, course_id: str, key: str) -> str:
    plan = client.get(f"/api/plan/{course_id}").json()
    return next(n["id"] for n in plan["nodes"] if n["key"] == key)


def _gen_lecture(client, course_id: str, node_id: str, markdown: str, monkeypatch) -> dict:
    monkeypatch.setattr(lecture_mod, "build_llm_client", lambda: _StubLLM(markdown))
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


def _seed_figures(course_id: str, node_id: str, monkeypatch) -> str:
    """塞一个概念图（带真 SVG + 可重绘元素）与一个数据表，并让讲义引用前者。"""
    images = get_settings().storage_dir / "images"
    images.mkdir(parents=True, exist_ok=True)
    svg_name = "mini-diagram.svg"
    (images / svg_name).write_text(MINI_SVG, encoding="utf-8")
    db = SessionLocal()
    try:
        diagram = ContentItem(
            course_id=course_id, node_id=node_id, kind="diagram", title="光合作用概念图",
            body={"caption": "光合作用概念图", "svg_preview": f"images/{svg_name}",
                  "excalidraw_json": {"elements": [
                      {"id": "r1", "type": "rectangle", "x": 0, "y": 0, "width": 160,
                       "height": 60, "isDeleted": False},
                      {"id": "t1", "type": "text", "x": 12, "y": 18, "width": 120,
                       "height": 24, "text": "光合作用", "isDeleted": False}]}},
            meta={"node_key": "1.1"})
        table = ContentItem(
            course_id=course_id, node_id=node_id, kind="chart_table", title="阶段对比",
            body={"chart_type": "table", "title": "阶段对比",
                  "markdown_table": "| 阶段 | 场所 |\n|---|---|\n| 光反应 | 类囊体薄膜 |"},
            meta={"node_key": "1.1"})
        db.add_all([diagram, table])
        db.commit()
        return diagram.id
    finally:
        db.close()


def _seed_deck(course_id: str) -> Path:
    """造一份「整课课件」：只要源 pptx 文件真的存在即可（打包只做复制）。"""
    settings = get_settings()
    deck_dir = settings.storage_dir / "ppt" / f"{course_id}_course"
    deck_dir.mkdir(parents=True, exist_ok=True)
    pptx = deck_dir / f"{course_id}_deck.pptx"
    pptx.write_bytes(b"PK\x03\x04 pretend-pptx")
    db = SessionLocal()
    try:
        db.add(ContentItem(course_id=course_id, node_id=None, kind="ppt",
                           title="光合作用入门 · 整课课件",
                           body={"pptx": str(pptx), "engine": "python-pptx"},
                           meta={"scope": "course", "slide_count": 8}))
        db.commit()
    finally:
        db.close()
    return pptx


def _seed_material(course_id: str) -> None:
    db = SessionLocal()
    try:
        db.add(SourceMaterial(course_id=course_id, title="光合作用导论", kind="text",
                              text="光反应发生在类囊体薄膜上。" * 20,
                              meta={"ext": ".text", "chars": 260, "original": ""}))
        db.commit()
    finally:
        db.close()


def _seed_quadrant_chart(course_id: str, node_id: str) -> dict:
    """塞一张**象限图**（任务 122 的新元素）：走真渲染链路、0 LLM，``body.data`` 齐全可重跑。"""
    db = SessionLocal()
    try:
        item = chart_mod.persist_chart_from_data(
            db, db.get(Course, course_id), db.get(OutlineNode, node_id), "quadrant",
            "重要 × 紧急：任务怎么排", QUADRANT_DATA, title="重要 × 紧急（象限图）",
            source="test-seed")
        return {"id": item.id, "kind": str(item.kind)}
    finally:
        db.close()


def _build(client, course_id: str, payload: dict | None = None) -> dict:
    resp = client.post(f"/api/delivery/course/{course_id}/build", json=payload or {"pdf": "off"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _delivery_dir(build_id: str) -> Path:
    return delivery_mod.deliveries_root() / build_id


def _course_with_content(client, course_id, monkeypatch) -> dict:
    _plan_approve(client, course_id)
    _add_section(course_id, "1.1", "光合作用的过程")
    node_id = _node_id(client, course_id, "1.1")
    diagram_id = _seed_figures(course_id, node_id, monkeypatch)
    _seed_deck(course_id)
    _seed_material(course_id)
    item = _gen_lecture(client, course_id, node_id,
                        LECTURE_MD.replace("DIAGRAM_ID", diagram_id), monkeypatch)
    return {"node_id": node_id, "item": item, "diagram_id": diagram_id}


# ---------------------------------------------------------------------------
# ⓪ 只读笔记库：构建（含体检读取）不得在 vault 里新建或删除任何条目（硬要求）
# ---------------------------------------------------------------------------

def _new_course(client, title: str) -> str:
    """测试自带唯一课名：共享临时 vault 里不会与别的用例互相干扰（顺序无关）。"""
    resp = client.post("/api/course/", json={
        "title": f"{title}-{uuid4().hex[:6]}", "subject": "生物",
        "audience": "初中生", "duration_minutes": 90})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def test_build_delivery_reads_vault_only(client):
    """任务 38 起：不一致时允许「出包前先同步一次笔记」，但同步之后的构建过程必须只读。

    所以这里考两次构建：① 第一次（可能顺带同步）② 快照 ③ 第二次必须一个文件都不动。
    """
    course_id = _new_course(client, "只读探针")
    _build(client, course_id)          # 第一次：不一致就顺带同步（任务 38 的例外）
    db = SessionLocal()
    try:
        from app.models.course import Course

        course = db.get(Course, course_id)
        vault = Path(str(kb_mod._configured_vault_root(course)))
        before = ({p.relative_to(vault).as_posix() for p in vault.rglob("*")}
                  if vault.is_dir() else set())
        second = _build(client, course_id)
        after = {p.relative_to(vault).as_posix() for p in vault.rglob("*")}
    finally:
        db.close()
    assert second["notes_sync"]["exported"] is False, "已一致就不该再动笔记库"
    assert after == before, \
        f"交付包构建只读笔记库：新增 {sorted(after - before)} / 删除 {sorted(before - after)}"


# ---------------------------------------------------------------------------
# ① 结构 + manifest sha1（含「0 LLM 调用」硬校验）
# ---------------------------------------------------------------------------

def test_build_delivery_structure_and_manifest_sha1(client, course_id, monkeypatch):
    _course_with_content(client, course_id, monkeypatch)
    # 构建期间任何一次 LLM 调用都视为失败（规格：交付包 = 0 LLM 调用）
    monkeypatch.setattr("app.services.llm.build_llm_client",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("交付包不许调 LLM")))
    summary = _build(client, course_id)

    out_dir = _delivery_dir(summary["build_id"])
    for rel in ("00-交付说明.md", "01-验收单.md", "素材清单.md", "manifest.json",
                "讲义", "课件", "图表"):
        assert (out_dir / rel).exists(), rel
    assert list((out_dir / "讲义").rglob("*.md")), "每节要有 markdown 源"
    assert list((out_dir / "课件").glob("*.pptx")), "整课 + 各章 pptx"
    assert list((out_dir / "图表" / "svg").glob("*.svg"))
    assert list((out_dir / "图表" / "png").glob("*.png"))

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["llm_calls"] == 0
    assert summary["llm_calls"] == 0
    # 清单里每个文件都能落到磁盘，且 sha1 / 字节数一致
    for row in manifest["files"]:
        path = out_dir / row["path"]
        assert path.is_file(), row["path"]
        digest = hashlib.sha1(path.read_bytes()).hexdigest()
        assert digest == row["sha1"], row["path"]
        assert path.stat().st_size == row["bytes"], row["path"]
    on_disk = {p.relative_to(out_dir).as_posix() for p in out_dir.rglob("*") if p.is_file()}
    assert on_disk - {"manifest.json"} == {row["path"] for row in manifest["files"]}

    zip_path = Path(summary["zip"])
    assert zip_path.is_file()
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.testzip() is None
        names = set(zf.namelist())
    assert "manifest.json" in names and "01-验收单.md" in names


def test_delivery_lecture_carries_source_section_and_read_legend(client, course_id, monkeypatch):
    """任务 57：交付讲义（md 源 + 同源 html/pdf）必须带「## 来源」小节与「怎么读」图例。

    起因：交付包里 27 篇讲义全含 `#N` 来源标记，却**一篇都没有「## 来源」小节**
    （只有 Obsidian 笔记库那份有，走 kb._source_section）⇒ 客户拿着 PDF 无从解读「#33」。
    """
    info = _course_with_content(client, course_id, monkeypatch)
    summary = _build(client, course_id)
    out_dir = _delivery_dir(summary["build_id"])

    db = SessionLocal()
    try:
        item = db.get(ContentItem, info["item"]["id"])
        sources = [s for s in ((item.meta or {}).get("sources") or []) if isinstance(s, dict)]
    finally:
        db.close()
    assert len(sources) == 3, sources      # 引入 / 核心讲解 / 小结 各一条来源标注

    md_files = sorted((out_dir / "讲义").rglob("*.md"))
    assert len(md_files) == 1, md_files
    text = md_files[0].read_text(encoding="utf-8")
    assert kb_mod.READ_LEGEND in text, "讲义顶部要有「怎么读」图例"
    assert "## 来源" in text, "讲义必须带来源小节"
    assert text.index("## 来源") > text.index("## 光合作用的过程"), "来源小节在正文之后"

    section = text.split("## 来源", 1)[1]
    bullets = [line for line in section.splitlines() if line.startswith("- ")]
    assert len(bullets) == len(sources), (bullets, sources)
    assert all("光合作用导论" in line for line in bullets)
    assert "第 1 段" in section and "第 3 段" in section
    assert "光反应发生在类囊体薄膜上" in section, "要带素材原文摘要，读者才核得动"

    html = sorted((out_dir / "讲义").rglob("*.html"))[0].read_text(encoding="utf-8")
    assert "<h2" in html and "光合作用导论" in html, "来源小节要进 html（PDF 同一份渲染）"


def test_package_docs_explain_how_to_open_the_vault(client, course_id, monkeypatch):
    """任务 133：客户拿到包要知道**怎么打开** `笔记库/`，且「不装插件也能看图」要说清。

    起因（用户 2026-10-05 问「分享 Obsidian 知识库怎么操作」）：包内只有一句含糊的
    「放进自己的 vault」，而 `图表库/` 里的 `.excalidraw` 不装 Excalidraw 插件就是一串 JSON。
    """
    _course_with_content(client, course_id, monkeypatch)
    out_dir = _delivery_dir(_build(client, course_id)["build_id"])

    notes = delivery_mod.NOTES_DIR
    overview = (out_dir / notes / "00-课程概览.md").read_text(encoding="utf-8")
    readme = (out_dir / "00-交付说明.md").read_text(encoding="utf-8")
    # 一律钉**字面串**：不拿 kb 的常量来断言 —— 那样改掉常量两边一起变，断言永远绿（132 复测
    # 指出的「同义反复」就是这个毛病）。
    for label, text in ((f"{notes}/00-课程概览.md", overview), ("00-交付说明.md", readme)):
        assert "打开本地仓库" in text, f"{label} 要写清「怎么打开这个笔记库」（官方简体中文按钮名）"
        assert "Open folder as vault" in text, f"{label} 要同时给英文按钮名（照哪个都能找到）"
        assert "Excalidraw" in text, f"{label} 要写明 `.excalidraw` 要装 Excalidraw 插件"
        assert "不装任何插件就能直接看" in text, f"{label} 要写明 `.svg` 免插件就能看"
        assert "不需要任何插件" in text, f"{label} 要写明 Mermaid / Canvas 是 Obsidian 自带"
    # 同一口径：两处「图怎么看」里逐字相同的那半句（只改一处会在这里红）
    shared = "里每张图有**两份**：`.svg` **不装任何插件就能直接看**"
    assert shared in overview and shared in readme, "两处口径必须一致"
    assert "## 怎么用这个笔记库" in overview, "这一节要真在首页里，不只是散句"


def test_package_docs_pin_the_three_steps(client, course_id, monkeypatch):
    """任务 135：三步就要是**真的三步** —— 两份文本里都要有显式的 ①②③（③ = 图怎么看）。

    来历：任务 134 把交付说明 §2 第 7 条写成「① 装 → ② 打开（含先看概览）」，第三段却是裸的
    「；**图怎么看**：」⇒ **产物其实只有 ①②**，而且此前**没有任何用例守编号**（复测点出的）。
    这里把 ①②③ 一起钉住：去掉 ③（或退回挂尾从句）就红。

    照既有的规矩钉**字面串**（不拿 kb 的常量自比 —— 那样两边一起变，断言永远绿）。
    """
    _course_with_content(client, course_id, monkeypatch)
    out_dir = _delivery_dir(_build(client, course_id)["build_id"])

    notes = delivery_mod.NOTES_DIR
    overview = (out_dir / notes / "00-课程概览.md").read_text(encoding="utf-8")
    readme = (out_dir / "00-交付说明.md").read_text(encoding="utf-8")
    for label, text in ((f"{notes}/00-课程概览.md", overview), ("00-交付说明.md", readme)):
        for num in ("①", "②", "③"):
            assert num in text, f"{label} 三步要显式编号，缺 {num}"
        assert "③ **图怎么看**" in text, f"{label} 第 ③ 步要就是「图怎么看」，不许退成挂尾从句"
    item7 = next(l for l in readme.splitlines() if l.startswith("7. "))
    assert "①" in item7 and "②" in item7 and "③" in item7, item7


def test_package_docs_offer_the_ai_helper_shortcut_and_the_workdir_warning(
        client, course_id, monkeypatch):
    """任务 139 / 140：客户可见的两份文本都要有 ㈠「不想自己装？可以让 AI 办公助手（例如 WorkBuddy）
    代劳」与 ㈡「**别把笔记库放进 AI 助手的工作目录**，请放独立目录」这条硬提醒。

    来历（用户 2026-10-06 提的路子）：别让客户自己啃 Obsidian 的安装步骤 —— 装个 AI 办公助手，让它
    把 Obsidian 装好、把 `笔记库/` 导入进去；以后不用它了、卸掉也不影响笔记库。但**笔记目录不能放在
    那个助手的工作目录里**（那是它自己的地盘 —— 140 只读实测：那目录里按时间戳命名的子目录**只增不减**、
    一个都没少，所以文案**只说「建」**、不提「删」）。

    140 收口（都由下面这段断言钉住）：
    ⚠️ 只钉「**可以让它帮你**」这层意思，**不**钉「一定能装好」—— 我们没端到端验过，不替第三方打包票；
    对冲句「以它自己的说法为准」也钉着（删了就红）。路径要写清**把 `<你的用户名>` 换成你自己的用户名**，
    且 Windows / macOS 都要有一句。**两处那段直接逐字比对**（139 只钉了首句 ⇒ 只改一处那段的后半句
    照样绿，那正是复测 m3 的漏洞；140 补上 ⇒ 只改一处就红）。
    照既有规矩钉**字面串**（不拿 kb 常量自比 —— 那样两边一起变，断言永远绿）。
    """
    _course_with_content(client, course_id, monkeypatch)
    out_dir = _delivery_dir(_build(client, course_id)["build_id"])

    notes = delivery_mod.NOTES_DIR
    overview = (out_dir / notes / "00-课程概览.md").read_text(encoding="utf-8")
    readme = (out_dir / "00-交付说明.md").read_text(encoding="utf-8")
    for label, text in ((f"{notes}/00-课程概览.md", overview), ("00-交付说明.md", readme)):
        assert "不想自己装" in text, f"{label} 要有「不想自己装？」这条省事路子"
        assert "WorkBuddy" in text, f"{label} 要举出 AI 办公助手的例子（WorkBuddy）"
        assert "装好" in text and "导入进去" in text, f"{label} 要说清它代劳的是哪两步"
        assert "卸掉也不影响笔记库" in text, f"{label} 要说清「它不是必需的」"
        assert "工作目录" in text, f"{label} 要写「别把笔记库放进 AI 助手的工作目录」"
        assert "独立目录" in text, f"{label} 要给出正面做法（放独立目录）"
        # 140 §A.2：占位符要说清怎么替换；平台不许只给 Windows（上一句刚说 Windows / macOS 都有）
        assert "<你的用户名>" in text and "换成你自己的用户名" in text, \
            f"{label} 要写清「把 <你的用户名> 换成你自己的用户名」"
        assert "macOS" in text, f"{label} 要补一句 macOS 的位置（不许只给 Windows 形式）"
        # 140 §A.9：对冲句也钉住 —— 免得被删掉、只剩看起来像承诺的话
        assert "以它自己的说法为准" in text, f"{label} 要保留「以它自己的说法为准」这层对冲"
        # §A.2：不许替第三方打包票 —— 这几句一旦写进包内文案就红
        for banned in ("一定能装好", "保证能导入", "自动就会"):
            assert banned not in text, f"{label} 不许写「{banned}」这类没验过的承诺"
    # 140 §A.3.7：**直接比对两处文本**（139 只钉了首句 ⇒ 只改一处那段的后半句仍绿，复测 m3 就是这条漏洞）。
    # 交付说明第 7 条 = "7. 看 `笔记库/`（Obsidian 版）：" + 这段 + " ① 到官网 …" ⇒ 去掉这两个已知前后缀；
    # 概览页 = 这段**自成一段**（`vault_getting_started()` 里 note 后面接空串）。
    item7 = next(l for l in readme.splitlines() if l.startswith("7. "))
    assert "（Obsidian 版）：" in item7 and " ① 到官网" in item7, item7
    readme_note = item7.split("（Obsidian 版）：", 1)[1].split(" ① 到官网", 1)[0]
    overview_notes = [l for l in overview.splitlines() if l.startswith("**不想自己装？**")]
    assert len(overview_notes) == 1, f"概览页这段应自成一段，实得 {len(overview_notes)} 段"
    overview_note = overview_notes[0]
    # 两边都得是有头有尾的那一整段（防「两边一起被截空 / 截短」这种同向退化）
    for tag, seg in (("交付说明 §2 第 7 条", readme_note), ("概览页", overview_note)):
        assert seg.startswith("**不想自己装？**") and seg.endswith("vault 里。"), \
            f"{tag} 段落切分异常：{seg!r}"
        assert "⚠️" in seg and "工作目录" in seg and "独立目录" in seg, f"{tag} 这段不完整：{seg!r}"
        # 140 §A.1：只说证过的行为 —— 这段里不许出现「删除」（只读实测：那目录只增不减）
        assert "删除" not in seg, f"{tag} 不许说助手会「删除」（没验过的事不许写）"
    assert readme_note == overview_note, (
        "两处这段必须**逐字**相等（只改一处那段就红）\n" + "  交付说明 §2 第 7 条："
        + repr(readme_note) + "\n  概览页：" + repr(overview_note)
    )


def test_package_docs_style_library_has_no_svg_and_plugin_first_step(client, course_id, monkeypatch):
    """任务 135：`_风格库.excalidrawlib` **没有 `.svg` 预览版**（别让客户去找不存在的文件）；
    装插件还差官方要求的**第一步**「先关掉受限模式」。

    来历：134 那句收尾写着「不装的话看 `.svg` 那一份就行」—— 事实是 `图表库/` 里除
    `.excalidraw` / `.svg` 就只有这 **1 个** `.excalidrawlib`，**根本没有它的 svg 版**
    （错话源自 134 任务书给的示例措辞，复测时被抓出来）。
    """
    _course_with_content(client, course_id, monkeypatch)
    out_dir = _delivery_dir(_build(client, course_id)["build_id"])

    notes = delivery_mod.NOTES_DIR
    charts = out_dir / notes / "图表库"
    assert charts.is_dir(), "包内应有 笔记库/图表库/"
    style = charts / "_风格库.excalidrawlib"
    assert style.is_file(), "包内应有 图表库/_风格库.excalidrawlib（这段文案就是解释它的）"
    sibs = sorted(p.name for p in charts.iterdir() if p.is_file() and "_风格库" in p.name)
    assert sibs == ["_风格库.excalidrawlib"], f"风格库只有这一个文件、没有 .svg 对应物：{sibs}"

    overview = (out_dir / notes / "00-课程概览.md").read_text(encoding="utf-8")
    readme = (out_dir / "00-交付说明.md").read_text(encoding="utf-8")
    for label, text in ((f"{notes}/00-课程概览.md", overview), ("00-交付说明.md", readme)):
        assert "它没有 `.svg` 预览版" in text, f"{label} 要说清风格库没有 svg 版本"
        assert "不影响其它任何内容" in text, f"{label} 要说清不影响其它内容"
        assert "受限模式" in text, f"{label} 要补「先关掉受限模式」这第一步"
        assert "Turn on community plugins" in text, f"{label} 界面按钮名要给英文"


# 任务 138：这条护栏从「扫 `.svg` token」升级成**逐字相等**。
# 137 那版是**围绕 `.svg` 这个字面 token、作用域 = 锚点所在的物理行**的扫描 —— 复测 137 实测：
# ㈤b 段内换行、把错话写在断点之后 ⇒ 6 条用例全绿；㈥ 写大写 `.SVG` ⇒ 绿；㈦ 干脆不写 `.svg`
# （「打开那份矢量图」）⇒ 绿。所以**别再说它「结构上不可能违反」**。
# 本版把「客户可见的那一段」冻成**唯一期望值**逐字比对：段内**多一句、少一句、换行、大小写、
# 同义词、指向 svg 全都红**。它的真实作用面 = 「这一段与核准原文逐字相等」，**不是**「任何表述错误
# 都抓得住」。**已知能绕过、只能靠人复核的情形**（交付说明里也列了）：
#   ㈠ 期望值本身被改错（改测试 = 改判据 ⇒ 必须让 DSH 复核事实，别只把测试改绿）；
#   ㈡ 生成器之外的链路（有人手改包内文件、或别的 renderer 写字）不经过本断言；
#   ㈢ 锚点行**之后**另起一块、且**不点名**风格库 / `excalidrawlib` 的表述（本测试只钉「下一行必须是新块」）。
_STYLE_LIB_MARK = "_风格库.excalidrawlib"
_STYLE_LIB_NAME = "风格库"
_STYLE_LIB_ANCHOR = "同一层还有"
_STYLE_LIB_PREFIX = "笔记库/"          # 只有 `00-交付说明.md` 那处带这个前缀（概览是空串）
# 锚点行的**下一行**必须是「新块」的开始（空行 / 标题 / 列表项 / 引用 / 表行）：生成器把 ③ 步与
# 第 7 条各写成**一整行**，所以这段后面不该有**同段续行**；「另起一行接着说」是复测用过的绕过姿势。
_STYLE_LIB_NEW_BLOCK = re.compile(r"^(#|[-*+>|]|[0-9]+[.、])")
# 讲风格库那一段的**核准原文**（唯一期望值）；`{p}` = 只有交付说明才有的前缀。
# ⚠️ 这份文本**是给客户看的**：改它 = 改产品文案 ⇒ 必须同步改这里**并让 DSH 复核事实**。
_STYLE_LIB_EXPECTED = (
    "同一层还有 `{p}图表库/_风格库.excalidrawlib`，那是给 Excalidraw 插件用的**素材库**"
    "（角色、卡片、连线、色卡这些可拖拽素材）—— **它没有 `.svg` 预览版**（只有这一个文件），"
    "**同样是「装了插件才有用」**：不装插件就用不上它，**但不影响其它任何内容**"
    "（每张图的 `.svg` 都照常能看）；知识图谱（`{p}概念库/_图谱.md` 里的 Mermaid 图）"
    "与 `{p}概念库/_图谱.canvas`（画布）**都是 Obsidian 自带能力，不需要任何插件**。"
)


def _style_library_zone(text: str, label: str) -> str:
    """切出「讲 `_风格库.excalidrawlib` 的那一段」（任务 137 的切法，138 未改）。

    只作用于这一段：图上那段（`图表库/` 每张图成对）**本来就该**提 `.svg`，与这段无关。
    任务 138：切不出来时**给人话断言**（原来抛的是 `ValueError: substring not found`）。
    """
    lines = text.splitlines()
    hits = [i for i, line in enumerate(lines) if _STYLE_LIB_MARK in line]
    assert len(hits) == 1, f"{label}：讲风格库的行应恰好 1 行，实际 {len(hits)} 行（{hits}）"
    line = lines[hits[0]]
    cut = line.find(_STYLE_LIB_ANCHOR)
    assert cut >= 0, (
        f"{label}：包内文本结构变了 —— 讲风格库的那一段**切不出来**："
        f"`_STYLE_LIB_MARK` 与锚点「{_STYLE_LIB_ANCHOR}」不在同一行（任务 138 把人话写在这里，"
        f"不再抛 substring not found）")
    return line[cut:]


def _first_diff(actual: str, expected: str) -> str:
    """逐字比对失败时给人看的「首处不同」（任务 138）。"""
    n = min(len(actual), len(expected))
    at = next((k for k in range(n) if actual[k] != expected[k]), n)
    lo = max(0, at - 20)
    return ("\n　　首处不同 = 第 " + str(at + 1) + " 个字符："
            "\n　　实际：…" + actual[lo:at + 40] + "…（共 " + str(len(actual)) + " 字）"
            "\n　　核准：…" + expected[lo:at + 40] + "…（共 " + str(len(expected)) + " 字）")


def test_package_docs_style_library_paragraph_is_exactly_the_approved_text(
        client, course_id, monkeypatch):
    """任务 138：讲风格库那一段必须与**核准原文**（`_STYLE_LIB_EXPECTED`）逐字相等。

    来历（复测 137 证伪 137 自述的「结构上不可能违反」）：㈤b 段内换行、把错话写在断点之后 ⇒
    6 条用例全绿；㈥ 大写 `.SVG` ⇒ 绿；㈦ 不写 `.svg`（「打开那份矢量图」）⇒ 绿。⇒ 不再扫 token，
    直接**冻住期望值**：换行 / 大小写 / 同义词 / 指向 svg / 多一句少一句，**全都红**。

    **别把这句读成「不可能违反」**：它钉的是「这一段与核准原文逐字相等」，不是「任何表述错误都
    抓得住」；能绕过它的情形见上面常量区的注释（期望值被改错 / 生成器之外手改 / 锚点行之后的新块）。

    照既有规矩钉**字面串**（不拿 kb 常量自比 —— 那样两边一起变，断言永远绿）。
    """
    _course_with_content(client, course_id, monkeypatch)
    out_dir = _delivery_dir(_build(client, course_id)["build_id"])

    notes = delivery_mod.NOTES_DIR
    overview = (out_dir / notes / "00-课程概览.md").read_text(encoding="utf-8")
    readme = (out_dir / "00-交付说明.md").read_text(encoding="utf-8")

    # 先决条件①（137 留下的，别删）：图上那段**本来就该**提 `.svg`，而它**不在**断言范围内
    # ⇒ 既证明「切分真的分开了两段」，也防「一刀切」式切分把图上那段也扫进去。
    assert ".svg" in overview[overview.index("每张图有**两份**"):overview.index(_STYLE_LIB_ANCHOR)], (
        "图上那段本来就该提 `.svg`（它在断言范围外，这条同时钉住「只作用于风格库那一段」）")

    for label, text, prefix in ((f"{notes}/00-课程概览.md", overview, ""),
                                ("00-交付说明.md", readme, _STYLE_LIB_PREFIX)):
        lines = text.splitlines()
        # 先切段：结构坏掉（锚点与 `_风格库.excalidrawlib` 被断成两行）时，这里给人话断言
        zone = _style_library_zone(text, label)
        # 先决条件②：锚点只准出现 1 行；讲风格库的内容（`_风格库.excalidrawlib` / 「风格库」）
        # 也只准出现在这一行 —— 「另起一行接着说」是复测用过的绕过逐字比对的姿势。
        at = [i for i, line in enumerate(lines) if _STYLE_LIB_ANCHOR in line]
        assert len(at) == 1, (
            f"{label}：锚点「{_STYLE_LIB_ANCHOR}」应恰好出现在 1 行，实际 {len(at)} 行（{[i + 1 for i in at]}）")
        row = at[0]
        for key in (_STYLE_LIB_MARK, _STYLE_LIB_NAME):
            rows_key = [i + 1 for i, line in enumerate(lines) if key in line]
            assert rows_key == [row + 1], (
                f"{label}：讲风格库的内容只准出现在锚点那一行（第 {row + 1} 行），"
                f"「{key}」却出现在第 {rows_key} 行 —— 另起一行接着说会绕过逐字比对")
        # 先决条件③：锚点行的**下一行**必须是「新块」的开始（否则算是这一段的续行）
        if row + 1 < len(lines):
            nxt = lines[row + 1]
            assert not nxt.strip() or _STYLE_LIB_NEW_BLOCK.match(nxt), (
                f"{label}：第 {row + 2} 行像是「风格库这段」的**续行**（{nxt[:40]!r}）——"
                f"这段必须自成一行，后面直接接新块（空行 / 标题 / 列表项 / 引用 / 表行）")
        # 判据本身：切出来的那一段 == 核准原文
        expected = _STYLE_LIB_EXPECTED.replace("{p}", prefix)
        assert zone == expected, (
            f"{label}：**讲风格库那一段与核准原文不逐字相等** —— 这段是**客户可见文本**"
            f"（事实必须对：`_风格库.excalidrawlib` 没有 svg 版、只有这一个文件）。"
            f"改了这段，就必须**同步更新 `_STYLE_LIB_EXPECTED`**（本文件），**并让 DSH 复核事实**"
            f"（别只把测试改绿）；拿不准就先改回核准原文。"
            + _first_diff(zone, expected))


def test_package_docs_never_point_the_style_library_at_svg(client, course_id, monkeypatch):
    """任务 136：**否定断言** —— 包内客户可见文本**不许再把风格库引向 `.svg`**。

    来历（复测 135 自造 m4）：把**对的话留着**、同时把**错话**「不装的话看 `.svg` 那一份就行」
    加回去 ⇒ 135 的两条用例**仍全绿**（它们只钉「对的话在不在」，钉不住「错话有没有又冒出来」）；
    这里补上另一半：**「错话在不在」也要钉**。照既有规矩钉**字面串**（不拿 kb 常量自比）。

    任务 137：① **只管风格库那一段**（原来扫全文 ⇒ 误报：图上那段本来就该说「看 `.svg` 那一份」，
    写回去会红、报错却指向风格库）；② 它现在**只是多一层**（真正的不变量是上面那条
    `test_package_docs_style_library_paragraph_is_exactly_the_approved_text`，任务 138 起），留着是因为它把 135 那句错话
    **逐字**写出来、报错文案最直白。
    """
    _course_with_content(client, course_id, monkeypatch)
    out_dir = _delivery_dir(_build(client, course_id)["build_id"])

    notes = delivery_mod.NOTES_DIR
    overview = (out_dir / notes / "00-课程概览.md").read_text(encoding="utf-8")
    readme = (out_dir / "00-交付说明.md").read_text(encoding="utf-8")
    # 这些说法都把 `_风格库.excalidrawlib` 引向一个**不存在**的 svg 版
    # （`图表库/` 里根本没有它的 svg）。正向断言（135 那两条）管不到这一面。
    forbidden = (
        "看 `.svg` 那一份",
        "不装的话看",
        "不装的话可以看",
    )
    for label, text in ((f"{notes}/00-课程概览.md", overview), ("00-交付说明.md", readme)):
        zone = _style_library_zone(text, label)
        for bad in forbidden:
            assert bad not in zone, f"{label} 风格库那段不许把风格库引向不存在的 svg 版：出现「{bad}」"


def test_package_docs_explain_the_style_library(client, course_id, monkeypatch):
    """任务 134：`图表库/_风格库.excalidrawlib` 与 `.excalidraw` **同口径**——装了插件才有用。

    起因（DSH 复测 133 挖出的遗漏）：`图表库/` 里除了 `.excalidraw` 与 `.svg`，还有那个
    40,526 B 的 `_风格库.excalidrawlib`（Excalidraw 插件的素材库）；不装插件点开它同样是
    一串看不懂的 JSON，而当时包内全部 Markdown 搜 `excalidrawlib` / `风格库` = 命中 0。
    同时补客户真正需要的两样：**插件装在哪**、**英文界面下的按钮名**。

    照 133 的规矩钉**字面串**（不拿 kb 常量自比 —— 那样两边一起变，断言永远绿）。
    """
    _course_with_content(client, course_id, monkeypatch)
    out_dir = _delivery_dir(_build(client, course_id)["build_id"])

    notes = delivery_mod.NOTES_DIR
    overview = (out_dir / notes / "00-课程概览.md").read_text(encoding="utf-8")
    readme = (out_dir / "00-交付说明.md").read_text(encoding="utf-8")
    for label, text in ((f"{notes}/00-课程概览.md", overview), ("00-交付说明.md", readme)):
        assert "_风格库.excalidrawlib" in text, f"{label} 要解释 `.excalidrawlib` 是什么"
        assert "装了插件才有用" in text, f"{label} 要把它与 `.excalidraw` 归到同一类"
        assert "第三方插件" in text, f"{label} 要给出插件安装路径（设置 → 第三方插件 → 浏览）"
        assert "Open folder as vault" in text, f"{label} 要给英文界面下的按钮名"
        assert "搜「Obsidian」" in text or "搜 Obsidian" in text, f"{label} 别只留一个裸域名"
    # 同一口径：两处解释 `.excalidrawlib` 的句子逐字相同（只改一处会在这里红）
    # 任务 135 改了这半句的措辞（原来收尾在「看 `.svg` 那一份就行」，而风格库没有 svg 版）⇒
    # 共享串跟着换，仍然是**前缀无关**的一整句（只改一处照样红）。
    shared = "**素材库**（角色、卡片、连线、色卡这些可拖拽素材）—— **它没有 `.svg` 预览版**"
    assert shared in overview and shared in readme, "两处口径必须一致（复用同一 helper）"


def test_zero_citation_lecture_keeps_source_section(client, course_id, monkeypatch):
    """任务 48d：0 条引用时「## 来源」小节不许整段消失（md / html / pdf 三处都要在）。

    起因：48c 重生成后 10.1 变成 0 条引用 + 13 处「无直接依据」⇒ 交付包里它
    **没有「## 来源」小节**，读者会以为交付漏了文件（旧版的引用是牵强引用，删掉是对的，
    但小节该留着并说清楚）。小节里改成一句显式说明，N = 正文里「无直接依据」的处数。
    """
    _plan_approve(client, course_id)
    _add_section(course_id, "1.1", "光合作用的过程")
    node_id = _node_id(client, course_id, "1.1")
    diagram_id = _seed_figures(course_id, node_id, monkeypatch)
    _seed_material(course_id)
    zero_cite = re.sub(r"（来源: [^）]*）", "（无直接依据,建议人工确认）",
                       LECTURE_MD.replace("DIAGRAM_ID", diagram_id))
    item = _gen_lecture(client, course_id, node_id, zero_cite, monkeypatch)

    db = SessionLocal()
    try:
        stored = db.get(ContentItem, item["id"])
        assert [s for s in ((stored.meta or {}).get("sources") or [])] == []
        marks = lecture_qc_mod.no_basis_count(zero_cite)
    finally:
        db.close()
    assert marks >= 3, marks

    summary = _build(client, course_id, {"pdf": "auto"})
    out_dir = _delivery_dir(summary["build_id"])
    note = f"本讲没有可溯源的素材段落：正文中 {marks} 处已标注"

    md_text = sorted((out_dir / "讲义").rglob("*.md"))[0].read_text(encoding="utf-8")
    assert "## 来源" in md_text, "0 引用也必须留「## 来源」小节"
    assert md_text.count(note) == 1, md_text
    assert "请人工核对后再用于对外交付" in md_text

    html_text = sorted((out_dir / "讲义").rglob("*.html"))[0].read_text(encoding="utf-8")
    assert note in html_text, "来源小节要进 html"

    pdfs = sorted((out_dir / "讲义").rglob("*.pdf"))
    if pdfs:                      # 没装 Edge 时交付显式降级为 HTML（test_pdf_generated_… 已覆盖）
        pdf_reader = pytest.importorskip("pypdf").PdfReader
        reader = pdf_reader(str(pdfs[0]))
        pdf_text = re.sub(r"\s+", "", "".join(page.extract_text() or "" for page in reader.pages))
        assert re.sub(r"\s+", "", note) in pdf_text, "来源小节要进 pdf（同一份 html 打印）"


def test_acceptance_numbers_match_course_report(client, course_id, monkeypatch):
    _course_with_content(client, course_id, monkeypatch)
    summary = _build(client, course_id)
    out_dir = _delivery_dir(summary["build_id"])
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))

    db = SessionLocal()
    try:
        from app.models.course import Course

        report = audit_mod.course_report(db, db.get(Course, course_id))
    finally:
        db.close()
    assert manifest["acceptance"] == {
        "report_score": report["conclusion"]["score"],
        "broken_links": report["links"]["broken"],
        "inconsistent": report["consistency"]["inconsistent"],
        "qc_average": report["quality"]["average_score"],
    }
    text = (out_dir / "01-验收单.md").read_text(encoding="utf-8")
    assert str(report["quality"]["average_score"]) in text
    assert "客户意见" in text
    assert str(report["coverage"]["lectures"]) in text


# ---------------------------------------------------------------------------
# ② 幂等：同一份内容重复构建 = 同一次交付（任务 126 起跨分钟也算同一次）
# ---------------------------------------------------------------------------

class _FrozenClock(datetime):
    """可控时钟：钉死交付包自己的「构建时刻」，消掉「恰好跨分钟」这种墙钟抖动。

    ``delivery`` 里只用 ``datetime.now()``（构建号 / 交付日期 / 数字快照时间 /
    manifest.generated_at）。这里只替换 delivery 模块里的这个名字，别的模块不受影响。
    """

    current: datetime | None = None

    @classmethod
    def now(cls, tz=None):
        base = cls.current or datetime.now()
        return base if tz is None else base.astimezone(tz)


@pytest.fixture
def frozen_clock(monkeypatch):
    # 钉在一个**早就过去**的时刻：与同一会话里走真时钟的用例不会撞到同一分钟
    _FrozenClock.current = datetime(2024, 1, 2, 3, 4, 30)
    monkeypatch.setattr(delivery_mod, "datetime", _FrozenClock)
    yield _FrozenClock
    _FrozenClock.current = None


def _read_files(out_dir: Path) -> dict[str, str]:
    """包内每个文件的 sha1（``manifest.json`` 自身不入清单，这里也不比）。"""
    return {path.relative_to(out_dir).as_posix(): hashlib.sha1(path.read_bytes()).hexdigest()
            for path in sorted(out_dir.rglob("*"))
            if path.is_file() and path.name != "manifest.json"}


def test_delivery_build_is_idempotent(client, course_id, monkeypatch, frozen_clock):
    """同一门课、同一份内容、同一套出包选项连跑两次 ⇒ 同一个构建号 + 逐文件**跨构建**全等。

    任务 126 起这条用例是加固版（**一个字没放宽**）：
      * 断言仍然是「两次 build_id 必须相同」，不是「不要求相同」；
      * 逐文件 sha1 从「文件与自己的 manifest 自洽」升级为「两次构建的 sha1 全等」；
      * 时钟钉死 + 先预热一次 ⇒ 跨不跨分钟都一样绿（旧口径正是死在跨分钟上）；
      * 课程是**含图课**（概念图 + 讲义自动分流的 3 张数据图 + 象限图）⇒ 图渲染的确定性
        也被这条钉住（任务 121/122 的换肤与图例/象限元素都在同一条渲染链路上）。
    """
    info = _course_with_content(client, course_id, monkeypatch)
    _seed_quadrant_chart(course_id, info["node_id"])
    _build(client, course_id)   # 预热：把「笔记一致性 / 旧口径质检」这些一次性状态落定
    first = _build(client, course_id)
    out_dir = _delivery_dir(first["build_id"])
    manifest_first = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    files_first = _read_files(out_dir)
    second = _build(client, course_id)

    assert second["build_id"] == first["build_id"]
    assert second["rebuilt"] is True
    root = delivery_mod.deliveries_root()
    assert len(list(root.glob(f"{first['build_id']}*/"))) == 1
    assert len(list(root.glob(f"{first['build_id']}*.zip"))) == 1
    manifest_second = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert [row["path"] for row in manifest_second["files"]] == \
           [row["path"] for row in manifest_first["files"]]
    assert manifest_second["content_fingerprint"] == manifest_first["content_fingerprint"]
    # 交付内容逐字节一致：第二次构建的每个文件，sha1 都必须与第一次一模一样
    files_second = _read_files(out_dir)
    assert [path for path in files_first if files_first[path] != files_second.get(path)] == []
    assert [path for path in files_second if files_second[path] != files_first.get(path)] == []
    for row in manifest_first["files"]:
        assert {files_first.get(row["path"]), files_second.get(row["path"])} == {row["sha1"]}, \
            row["path"]
    # 图渲染的产物确实被这条覆盖（否则「图是确定性的」只是句空话）
    pngs = sorted(path for path in files_first if path.endswith(".png"))
    assert len(pngs) >= 5, pngs
    assert any("象限" in path for path in pngs), pngs


def test_delivery_build_id_survives_minute_rollover(client, course_id, monkeypatch, frozen_clock):
    """任务 126 的现场：内容一字未改，只跨了一分钟 ⇒ **不许**换构建号。

    DSH 2026-10-03 门禁就死在这：``assert '光合作用入门-20261003-1451' ==
    '光合作用入门-20261003-1450-f1f625'``。当时那两份产物**逐字节相同**（除两份自带构建
    时间戳的说明文件），换号的真正原因是旧口径「同一分钟 = 同一次交付」+「撞名挂课程 id
    短码」。跨分钟重建时只有那两份说明文件会变，其余文件必须逐字节一致。
    """
    info = _course_with_content(client, course_id, monkeypatch)
    _seed_quadrant_chart(course_id, info["node_id"])
    _build(client, course_id)                                    # 预热
    first = _build(client, course_id)
    out_dir = _delivery_dir(first["build_id"])
    files_first = _read_files(out_dir)

    before_stamp = frozen_clock.current.strftime("%Y%m%d-%H%M")
    frozen_clock.current = frozen_clock.current + timedelta(seconds=61)
    assert frozen_clock.current.strftime("%Y%m%d-%H%M") != before_stamp, "前置：确实跨分钟了"
    second = _build(client, course_id)
    out_dir_second = _delivery_dir(second["build_id"])
    files_second = _read_files(out_dir_second)

    assert second["build_id"] == first["build_id"], "跨分钟重出包不许换号（旧口径在这里红）"
    assert out_dir_second == out_dir
    changed = sorted(path for path in files_first if files_first[path] != files_second.get(path))
    assert changed == ["00-交付说明.md", "01-验收单.md"], changed
    assert len(files_first) >= 40, len(files_first)


def test_delivery_build_id_follows_content_not_metadata(client, course_id, monkeypatch,
                                                        frozen_clock):
    """指纹只跟**内容**走（双侧探针：无关的不动号，进包的一处就换号，图内容也算内容）。"""
    info = _course_with_content(client, course_id, monkeypatch)
    _seed_quadrant_chart(course_id, info["node_id"])
    _build(client, course_id)                                    # 预热
    baseline = _build(client, course_id)["build_id"]
    files_baseline = _read_files(_delivery_dir(baseline))

    # ① 改一个**交付流程根本不读**的字段（course.meta 里的陌生键）⇒ 号码不变、字节不变
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        course.meta = {**(course.meta or {}), "delivery_note": "只给老师自己看的一句备注"}
        db.commit()
    finally:
        db.close()
    unchanged = _build(client, course_id)["build_id"]
    assert unchanged == baseline
    assert _read_files(_delivery_dir(unchanged)) == files_baseline, "与交付无关的字段不该动一个字节"

    # ② 改课程元数据里**会进包**的字段（预计课时写在 00-交付说明 里）⇒ 必须换号
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        course.duration_minutes = 120
        db.commit()
    finally:
        db.close()
    after_meta = _build(client, course_id)["build_id"]
    assert after_meta != baseline
    readme = (_delivery_dir(after_meta) / "00-交付说明.md").read_text(encoding="utf-8")
    assert "预计课时：120 分钟" in readme

    # ③ 改一处讲义正文 ⇒ 必须换号，且新包的讲义里能看到那一句
    db = SessionLocal()
    try:
        item = db.get(ContentItem, info["item"]["id"])
        body = dict(item.body or {})
        body["markdown"] = (str(body.get("markdown") or "")
                            + "\n\n### 补充说明\n- 光反应还会释放氧气。\n")
        item.body = body
        db.commit()
    finally:
        db.close()
    after_text = _build(client, course_id)["build_id"]
    assert after_text not in (baseline, after_meta)
    lecture = next(_delivery_dir(after_text).glob("讲义/**/*.md"))
    assert "光反应还会释放氧气" in lecture.read_text(encoding="utf-8")

    # ④ 改一处**图内容**（概念图多一个框）⇒ 也得换号，而且 PNG 必须真的重画
    png_before = {path: sha for path, sha in _read_files(_delivery_dir(after_text)).items()
                  if path.endswith(".png")}
    db = SessionLocal()
    try:
        item = db.get(ContentItem, info["diagram_id"])
        body = dict(item.body or {})
        doc = dict(body.get("excalidraw_json") or {})
        doc["elements"] = [*list(doc.get("elements") or []),
                           {"id": "r2", "type": "rectangle", "x": 200, "y": 96, "width": 160,
                            "height": 60, "isDeleted": False}]
        body["excalidraw_json"] = doc
        item.body = body
        db.commit()
    finally:
        db.close()
    after_figure = _build(client, course_id)["build_id"]
    assert after_figure not in (baseline, after_meta, after_text)
    png_after = {path: sha for path, sha in _read_files(_delivery_dir(after_figure)).items()
                 if path.endswith(".png")}
    assert sorted(png_after) == sorted(png_before)
    changed_png = sorted(path for path in png_before if png_before[path] != png_after[path])
    assert changed_png == [path for path in png_before if "光合作用概念图" in path], changed_png


# ---------------------------------------------------------------------------
# ③ 缺项：显式写「本次未生成 + 原因」
# ---------------------------------------------------------------------------

def test_missing_items_are_stated_with_reason(client):
    summary = _build(client, _new_course(client, "空课"))
    out_dir = _delivery_dir(summary["build_id"])
    readme = (out_dir / "00-交付说明.md").read_text(encoding="utf-8")
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))

    assert "本次未生成" in readme
    reasons = {row["item"]: row["reason"] for row in manifest["missing"]}
    assert "讲义/" in reasons and "还没有讲义" in reasons["讲义/"]
    assert "课件/" in reasons
    assert "笔记库/" in reasons
    for row in manifest["missing"]:
        assert row["item"] in readme and row["reason"] in readme


# ---------------------------------------------------------------------------
# ④ PDF：有 Edge 就打 PDF，没有就显式降级（两种都要写明原因）
# ---------------------------------------------------------------------------

def test_pdf_generated_or_explicitly_degraded(client, course_id, monkeypatch):
    _course_with_content(client, course_id, monkeypatch)
    summary = _build(client, course_id, {"pdf": "auto"})
    out_dir = _delivery_dir(summary["build_id"])
    pdfs = list((out_dir / "讲义").rglob("*.pdf"))
    lectures = list((out_dir / "讲义").rglob("*.md"))
    if delivery_mod.find_edge() is None:
        assert pdfs == []
        assert any("Edge" in row["reason"] for row in summary["degraded"])
    else:
        assert len(pdfs) == len(lectures) == summary["pdf"]["generated"]
        for path in pdfs:
            assert path.read_bytes()[:5] == b"%PDF-", path
    # 降级时一定有可直接打印的 HTML 兜底
    assert len(list((out_dir / "讲义").rglob("*.html"))) == len(lectures)


# ---------------------------------------------------------------------------
# ④b Edge 打印：输出路径与 profile 必须是绝对路径、且 profile 不进交付包
#     （真机实测：settings.storage_dir 默认相对 ./storage → Edge 退出码 21 不产 PDF）
# ---------------------------------------------------------------------------

def test_build_without_edge_degrades_pdf_but_still_ships(client, monkeypatch):
    """任务 116：**缺 Edge ⇒ 整包照常出**，只是讲义 PDF 这一项按既有「跳过 + 原因」口径处理。

    给别人的电脑上大概率没有装 Edge 这一环，用户不该看到「整包失败」，
    返回里要能一眼看出是**环境缺组件**，而不是自己的课有问题。
    这里把 `find_edge` 打桩成「没有 Edge」，其余流程一字不改。
    """
    course_id = _new_course(client, "缺Edge降级")
    _course_with_content(client, course_id, monkeypatch)
    monkeypatch.setattr(delivery_mod, "find_edge", lambda: None)  # 假装这台电脑没装 Edge

    summary = _build(client, course_id, {"pdf": "auto"})  # 200（不是 500），也不抛异常
    out_dir = _delivery_dir(summary["build_id"])

    # ① 包照常出：结构件与讲义源文件都在，还有「可直接打印的 HTML」兜底
    assert summary["pdf"]["generated"] == 0
    assert summary["pdf"]["failed"] == 0
    assert summary["pdf"]["engine"] == "html-only(auto)"
    assert (out_dir / delivery_mod.README_NAME).is_file() and (out_dir / delivery_mod.ACCEPT_NAME).is_file()
    assert (out_dir / "manifest.json").is_file()
    assert len(list((out_dir / "讲义").rglob("*.md"))) >= 1
    assert len(list((out_dir / "讲义").rglob("*.html"))) >= 1
    assert list((out_dir / "讲义").rglob("*.pdf")) == []

    # ② 原因写清楚，而且是「环境缺组件」的口径（返回 / 说明 / manifest 三处同源）
    reason = next(row["reason"] for row in summary["degraded"]
                  if row["item"].startswith("讲义/"))
    assert "Edge" in reason
    readme = (out_dir / delivery_mod.README_NAME).read_text(encoding="utf-8")
    assert "Edge" in readme
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert any("Edge" in row["reason"] for row in manifest["degraded"])
    assert manifest["params"]["pdf_engine"] == "html-only(auto)"
    # 包里其余计数照旧（没有因为缺 Edge 就少出东西）
    assert manifest["counts"]["pdf_ok"] == 0
    assert manifest["counts"]["lectures"] == summary["counts"]["lectures"] >= 1


def test_missing_edge_is_logged_as_environment_not_content(client, monkeypatch, caplog):
    """任务 116：日志里也要说清是「环境缺组件」，别让人以为是课坏了。"""
    import logging

    course_id = _new_course(client, "缺Edge日志")
    _course_with_content(client, course_id, monkeypatch)
    monkeypatch.setattr(delivery_mod, "find_edge", lambda: None)
    with caplog.at_level(logging.WARNING, logger="app.services.delivery"):
        _build(client, course_id, {"pdf": "auto"})
    messages = " ".join(record.getMessage() for record in caplog.records
                        if record.name == "app.services.delivery")
    assert "Edge" in messages and "环境缺组件" in messages
    assert "不是课件有问题" in messages

def test_print_pdf_uses_absolute_paths(tmp_path, monkeypatch):
    seen: dict = {}

    class _Proc:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(cmd, **_kwargs):
        seen["cmd"] = cmd
        target = next(arg.split("=", 1)[1] for arg in cmd if arg.startswith("--print-to-pdf="))
        Path(target).write_bytes(b"%PDF-1.4 fake")
        return _Proc()

    monkeypatch.setattr(delivery_mod.subprocess, "run", fake_run)
    monkeypatch.chdir(tmp_path)  # 真机默认 storage_dir 是相对的 ./storage
    html = tmp_path / "讲义.html"
    html.write_text("<html><body>中文</body></html>", encoding="utf-8")
    out = Path("storage/deliveries/x/讲义/a.pdf")  # 相对路径 —— 真机默认就是这样
    result = delivery_mod.print_pdf(html, out, edge="edge")
    assert result["ok"] is True, result

    cmd = seen["cmd"]
    pdf_arg = Path(next(arg for arg in cmd if arg.startswith("--print-to-pdf=")).split("=", 1)[1])
    profile_arg = Path(next(arg for arg in cmd if arg.startswith("--user-data-dir=")).split("=", 1)[1])
    assert pdf_arg.is_absolute(), pdf_arg
    assert profile_arg.is_absolute(), profile_arg
    # profile 落在系统临时目录，绝不在交付包目录里（中文长路径会让 Edge 直接失败）
    assert not str(profile_arg).startswith(str(delivery_mod.deliveries_root()))
    assert not list(Path(pdf_arg).parent.glob("_edge_profile*"))


# ---------------------------------------------------------------------------
# ⑤ Markdown → HTML（表格 / mermaid / 图卡内嵌）
# ---------------------------------------------------------------------------

def test_markdown_to_html_keeps_table_mermaid_and_figure():
    html_text = delivery_mod.markdown_to_html(
        LECTURE_MD.replace("DIAGRAM_ID", "x"),
        resolve_image=lambda url: "<svg id=\"inline\"></svg>")
    assert "<table>" in html_text and "类囊体薄膜" in html_text
    assert '<pre class="mermaid">' in html_text and "flowchart TD" in html_text
    assert '<svg id="inline">' in html_text
    assert "<h2>" in html_text and "<h3>" in html_text  # 讲义 ## 标题 → h2
    # 取不到图时显式留一行说明，不静默丢
    missing = delivery_mod.markdown_to_html("![图](diagram://nope)", resolve_image=lambda url: None)
    assert "未随包交付" in missing


# ---------------------------------------------------------------------------
# ⑥ 数字过期防护（任务 38）：出包前先同步 / 同步不掉就拒绝 + 指引 / 强制出包要标注
# ---------------------------------------------------------------------------

def _make_notes_stale(client, course_id, monkeypatch) -> dict:
    """造「笔记库落后于网页端」：先生成内容 → 导出笔记 → 网页端再改正文但不重导。

    真机里最常见的成因是「重生成课件 / 改讲义后没重导笔记」；内容生成会顺带增量写 vault，
    所以这里直接改网页端存储（等价于「只在网页端改了，笔记还是旧的」），稳定可复现。
    """
    ctx = _course_with_content(client, course_id, monkeypatch)
    resp = client.post(f"/api/kb/course/{course_id}/export")
    assert resp.status_code == 200, resp.text
    db = SessionLocal()
    try:
        item = db.get(ContentItem, ctx["item"]["id"])
        body = dict(item.body or {})
        body["markdown"] = (str(body.get("markdown") or "")
                            + "\n\n### 网页端补充\n- 这段只在网页端，笔记库还没导出\n")
        item.body = body
        item.meta = {**(item.meta or {}), "notes": {}}      # 等价于「这次没写笔记」
        db.commit()
    finally:
        db.close()
    return ctx


def _inconsistent_now(course_id: str) -> int:
    db = SessionLocal()
    try:
        from app.models.course import Course

        return int(kb_mod.verify_course_notes(db, db.get(Course, course_id))["inconsistent"])
    finally:
        db.close()


def test_preview_shows_consistency_before_build(client, course_id, monkeypatch):
    """③ 生成前就能看到一致性状态（点了才发现数字不对就晚了）。

    任务 48z：一致性从首屏 GET 里拆出去 ⇒ 本用例改从 ``/consistency`` 取，
    断言一个字没改（数字口径与改造前相同）。
    """
    _make_notes_stale(client, course_id, monkeypatch)
    assert client.get(f"/api/delivery/course/{course_id}").json()["consistency"] is None
    data = client.get(f"/api/delivery/course/{course_id}/consistency").json()
    assert data["inconsistent"] > 0
    assert data["ok"] is True
    assert "不一致" in data["hint"]
    assert data["checked"] >= data["inconsistent"]

    db = SessionLocal()
    try:
        from app.models.course import Course

        kb_mod.export_course(db, db.get(Course, course_id))
    finally:
        db.close()
    fresh = client.get(f"/api/delivery/course/{course_id}/consistency").json()
    assert fresh["inconsistent"] == 0
    assert "一致" in fresh["hint"]


def test_build_syncs_notes_before_packaging(client, course_id, monkeypatch):
    """① 不一致 → 先自动同步再打包；② 验收单 / manifest 标注「已同步」+ 快照时间。"""
    _make_notes_stale(client, course_id, monkeypatch)
    assert _inconsistent_now(course_id) > 0, "前置：笔记库确实落后于网页端"

    summary = _build(client, course_id)
    sync = summary["notes_sync"]
    assert sync["exported"] is True
    assert sync["inconsistent_before"] > 0
    assert sync["inconsistent_after"] == 0
    assert sync["synced"] is True
    assert summary["llm_calls"] == 0            # 同步走本地导出，不是 LLM

    out_dir = _delivery_dir(summary["build_id"])
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["notes_sync"]["synced_before_build"] is True
    assert manifest["notes_sync"]["auto_exported"] is True
    assert manifest["notes_sync"]["inconsistent_after"] == 0
    assert manifest["notes_sync"]["snapshot_at"]
    assert manifest["vault"]["read_only"] is False, "本次确实同步过笔记"

    text = (out_dir / "01-验收单.md").read_text(encoding="utf-8")
    assert "构建前已同步笔记：是" in text
    assert "数字快照时间：" in text
    assert "若之后重生成内容，请重新出包" in text
    readme = (out_dir / "00-交付说明.md").read_text(encoding="utf-8")
    assert "构建前已同步笔记：是" in readme

    # 验收单数字与「现在重算」逐项一致 —— 这正是任务 38 要防的「数字过期」
    db = SessionLocal()
    try:
        from app.models.course import Course

        report = audit_mod.course_report(db, db.get(Course, course_id))
    finally:
        db.close()
    assert manifest["acceptance"]["inconsistent"] == report["consistency"]["inconsistent"] == 0
    assert manifest["acceptance"]["report_score"] == report["conclusion"]["score"]


def test_build_refuses_when_sync_cannot_fix(client, course_id, monkeypatch):
    """① 同步不掉 → 拒绝出包 + 明确指引；且磁盘不留半成品。"""
    _make_notes_stale(client, course_id, monkeypatch)
    monkeypatch.setattr(delivery_mod.kb_mod, "export_course",
                        lambda *a, **k: {"files": [], "preserved": [], "warnings": []})
    root = delivery_mod.deliveries_root()
    before = {p.name for p in root.glob("*")} if root.is_dir() else set()

    resp = client.post(f"/api/delivery/course/{course_id}/build", json={"pdf": "off"})
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "拒绝出包" in detail
    assert "不一致" in detail
    assert "allow_inconsistent=true" in detail
    assert "保存到 Obsidian 笔记" in detail

    after = {p.name for p in root.glob("*")} if root.is_dir() else set()
    assert after == before, "拒绝出包时不该留下半成品目录"


def test_build_allows_inconsistent_only_when_forced(client, course_id, monkeypatch):
    """① ?allow_inconsistent=true → 照出，但② 验收单与 manifest 如实标注不一致项数。"""
    _make_notes_stale(client, course_id, monkeypatch)
    monkeypatch.setattr(delivery_mod.kb_mod, "export_course",
                        lambda *a, **k: {"files": [], "preserved": [], "warnings": []})
    stale = _inconsistent_now(course_id)

    resp = client.post(f"/api/delivery/course/{course_id}/build",
                       json={"pdf": "off", "allow_inconsistent": True})
    assert resp.status_code == 200, resp.text
    summary = resp.json()
    sync = summary["notes_sync"]
    assert sync["forced"] is True and sync["synced"] is False
    assert sync["inconsistent_after"] == stale > 0

    out_dir = _delivery_dir(summary["build_id"])
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["notes_sync"]["synced_before_build"] is False
    assert manifest["notes_sync"]["inconsistent_after"] == stale
    assert manifest["notes_sync"]["forced_by_allow_inconsistent"] is True
    text = (out_dir / "01-验收单.md").read_text(encoding="utf-8")
    assert f"构建前已同步笔记：否（不一致 {stale} 项" in text
    assert "若之后重生成内容，请重新出包" in text
    # 现算数字如实反映不一致（没有被美化）
    assert manifest["acceptance"]["inconsistent"] == stale
    assert "检查 " in text and f"不一致 {stale}" in text
