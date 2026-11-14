"""任务 22（讲义质量提升包 v1）回归测试：质检器 + 来源锚定 + 术语与概念库对齐。

覆盖（对应 HANDOFF「任务 22 说明」D 段硬标准）：
① 合格讲义 score 高且 checks 全 ok；② 缺小结 → structure fail；③ 套话堆砌 → fluff warn；
④ 用别名 → terminology warn 且给建议标准名；⑤ 无来源 → sources warn；
⑥ 「按质检建议重生成」的提示词含质检问题文本；⑦ 整课汇总统计口径正确；
外加：来源锚定落 meta.sources、导出笔记有「来源」/「术语」小节且 verify 无坏链。

协作规则第 7 条：DATABASE_URL / STORAGE_DIR / OBSIDIAN_VAULT_ROOT 由 conftest 统一
指向临时目录，本文件不碰真实知识库。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

import pytest

from app.db.session import SessionLocal
from app.models.course import ContentItem, Outline, OutlineNode
from app.services import lecture as lecture_mod
from app.services.concept import concept_id_for
from app.services.lecture import _lecture_user_prompt
from app.services.lecture_qc import (CHECK_KEYS, QC_VERSION, compute_qc, key_points,
                                     muddy_sentences, open_issues, parse_sources, prompt_section,
                                     source_key_points, substance_hits, terminology_issues,
                                     unsourced_groups, unsourced_points, unsourced_sentences)
from app.services.llm import MockLLMClient

# ---------------------------------------------------------------------------
# 样例讲义：一份「合格稿」（11 项检查全 ok）与三种问题稿。
# 任务 41 起「合格」的标准提高了：除了定义→原理→举例→小结，还要有
# ① 踩过的坑（失败教训）② 能力边界 ③ 能被人照着独立执行的步骤。
# ---------------------------------------------------------------------------

QUALIFIED = """## 光合作用的过程

### 引入
- 阳光下的叶片会悄悄把空气中的二氧化碳变成糖，这个过程就是本节要弄清的光合作用（来源: 光合作用导论 #1）
- 农谚说「庄稼一枝花，全靠肥当家」，但真正把光能变成粮食的，是叶片里这套精密的反应链条（来源: 光合作用导论 #1）

### 核心讲解
- **光反应**：发生在叶绿体的类囊体薄膜上，色素分子吸收光能后把水分解，释放氧气并产生 ATP 与 NADPH，把光能暂时存进化学键里（来源: 光合作用导论 #2）
- **暗反应**：发生在叶绿体基质中，不需要光直接参与，但必须消耗光反应送来的 ATP 与 NADPH，把二氧化碳固定成三碳化合物，再还原成糖类（来源: 光合作用导论 #3）
- **两步衔接**：光反应负责供电，暗反应负责加工，供电一停加工立刻停摆，所以两步在时间上必须紧挨着发生，不能分开理解（来源: 光合作用导论 #4）
- **影响因素**：光照强度决定光反应的速率，温度影响酶的活性，二氧化碳浓度决定暗反应的原料，三者中任意一个偏小，整条链的产量都会被拖住（来源: 光合作用导论 #5）
- **生产应用**：大棚夜间补光、增施气肥、控制夜温，本质都是分别给这三处瓶颈加料，让两步衔接得更顺畅（来源: 光合作用导论 #6）
- **常见误区**：暗反应并不是只在黑暗中进行，它只是不直接需要光，白天同样在持续运转（无直接依据,建议人工确认）

```mermaid
flowchart TD
  A[光能] --> B[光反应]
  B --> C[ATP 与 NADPH]
  C --> D[暗反应固碳]
  D --> E[有机物]
```

### 举例
- 举例：把光反应比作发电站、暗反应比作工厂，电站停工工厂立刻停线，这正好解释了阴雨天为什么会减产（来源: 光合作用导论 #7）

### 踩过的坑
- 2023 年冬天有 3 个大棚照搬白天的做法，夜里连着补光 8 小时，夜温压不下去，叶片净光合速率反而掉了 12%，后来改成只在 18:00-22:00 补光才恢复（来源: 光合作用导论 #9）

### 可照做的操作
1. 每天 18:00 记录棚内温度与光照，把夜温控制在 12~15℃（来源: 光合作用导论 #10）
2. 当连续两天夜温低于 12℃ 时，把补光时段缩短到 2 小时，并把当天的读数写入《夜温记录表》（来源: 光合作用导论 #11）

### 小结
- 光反应供能、暗反应固碳，两步通过 ATP 与 NADPH 扣在一起，抓住这条主线就能解释影响因素与生产措施（来源: 光合作用导论 #8）
- 不覆盖 C4 / CAM 植物的特殊固碳途径；遇到这类作物或产量异常，请找农技专家现场确认（无直接依据,建议人工确认）
"""

QUIZZES = [
    {"qtype": "choice", "difficulty": "easy", "question": "光反应发生的场所是？",
     "options": ["类囊体薄膜", "叶绿体基质", "细胞质基质", "线粒体"], "answer": "0",
     "explanation": "光反应在类囊体薄膜上进行。"},
    {"qtype": "short", "difficulty": "medium", "question": "说明光反应与暗反应如何衔接。",
     "options": [], "answer": "光反应为暗反应提供 ATP 与 NADPH。", "explanation": "考查两步衔接。"},
]


def _drop_section(markdown: str, keyword: str) -> str:
    """删掉含关键字的小节（用来构造「缺小结」这类问题稿）。"""
    lines = markdown.splitlines()
    out: list[str] = []
    skipping = False
    for line in lines:
        if line.startswith("#"):
            skipping = keyword in line
        if not skipping:
            out.append(line)
    return "\n".join(out)


def _fluff_markdown() -> str:
    """在合格稿上加一段套话（③）。"""
    return QUALIFIED + "\n### 补充说明\n" + (
        "- 总之，光反应与暗反应的衔接非常重要（来源: 光合作用导论 #8）\n"
        "- 综上所述，理解两步关系至关重要（来源: 光合作用导论 #8）\n"
        "- 值得注意的是，影响因素决定最终产量（来源: 光合作用导论 #8）\n"
    )


def _no_source_markdown() -> str:
    """把标注全部去掉（⑤）。"""
    import re
    return re.sub(r"（来源: [^）]*）", "", QUALIFIED)


# ---------------------------------------------------------------------------
# 夹具：课程 / 大纲 / 小节 / 讲义生成
# ---------------------------------------------------------------------------

class _StubLLM(MockLLMClient):
    """讲义走指定 Markdown，概念图/概念卡沿用 mock 默认实现。"""

    def __init__(self, markdown: str, quizzes: list[dict] | None = None) -> None:
        self.markdown = markdown
        self.quizzes = quizzes if quizzes is not None else QUIZZES
        self.seen: list[str] = []

    def complete(self, system: str, user: str, *, max_tokens: int = 2000) -> str:
        if "LECTURE_JSON" in system:
            self.seen.append(user)
            return json.dumps({
                "markdown": self.markdown,
                "summary": "光合作用的过程",
                "keywords": ["光反应", "暗反应"],
                "quizzes": self.quizzes,
            }, ensure_ascii=False)
        return super().complete(system, user, max_tokens=max_tokens)


class _Recorder(MockLLMClient):
    """记录讲义提示词（断言「按质检建议重生成」注入用）。"""

    def __init__(self, markdown: str) -> None:
        self.markdown = markdown
        self.prompts: list[str] = []

    def complete(self, system: str, user: str, *, max_tokens: int = 2000) -> str:
        if "LECTURE_JSON" in system:
            self.prompts.append(user)
            return json.dumps({"markdown": self.markdown, "summary": "重生成稿",
                               "keywords": [], "quizzes": QUIZZES}, ensure_ascii=False)
        return super().complete(system, user, max_tokens=max_tokens)


def _plan_approve(client, course_id: str, num_chapters: int = 2) -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "生物光合作用", "num_chapters": num_chapters})
    assert resp.status_code == 200, resp.text
    assert client.post("/api/plan/approve", json={
        "course_id": course_id, "approved": True}).status_code == 200
    return resp.json()


def _add_sections(course_id: str, chapter_key: str, titles: list[str],
                  objective: str = "掌握光合作用的过程") -> list[str]:
    db = SessionLocal()
    try:
        outline = (db.query(Outline).filter(Outline.course_id == course_id)
                   .order_by(Outline.created_at.desc()).first())
        base = db.query(OutlineNode).filter(OutlineNode.outline_id == outline.id).count()
        keys = []
        for index, title in enumerate(titles, 1):
            key = f"{chapter_key}.{index}"
            db.add(OutlineNode(outline_id=outline.id, node_key=key, node_type="section",
                               title=title, objective=objective, order_index=base + index))
            keys.append(key)
        db.commit()
        return keys
    finally:
        db.close()


def _node_id(client, course_id: str, key: str) -> str:
    plan = client.get(f"/api/plan/{course_id}").json()
    return next(n["id"] for n in plan["nodes"] if n["key"] == key)


def _wait_job(client, job_id: str, tries: int = 200) -> dict:
    state = None
    for _ in range(tries):
        state = client.get(f"/api/content/jobs/{job_id}").json()
        if state["status"] in ("success", "failed"):
            return state
        time.sleep(0.1)
    raise AssertionError(f"job timeout: {job_id} {state}")


def _generate(client, course_id: str, node_id: str, markdown: str,
              monkeypatch, quizzes: list[dict] | None = None) -> dict:
    monkeypatch.setattr(lecture_mod, "build_llm_client",
                        lambda: _StubLLM(markdown, quizzes))
    resp = client.post("/api/lecture/generate", json={
        "course_id": course_id, "node_id": node_id, "tone": "正式"})
    assert resp.status_code == 200, resp.text
    state = _wait_job(client, resp.json()["job_id"])
    assert state["status"] == "success", state
    return state["result"]["item"]


def _levels(qc: dict) -> dict:
    return {c["key"]: c["level"] for c in qc["checks"]}


# ---------------------------------------------------------------------------
# ① 合格讲义：score 高、11 项全 ok（任务 41 起含 substance / actionable；任务 106 起含 numbers）
# ---------------------------------------------------------------------------

def test_qualified_lecture_passes_all_checks(client, course_id, monkeypatch):
    _plan_approve(client, course_id)
    _add_sections(course_id, "1", ["光合作用的过程"])
    node = _node_id(client, course_id, "1.1")
    item = _generate(client, course_id, node, QUALIFIED, monkeypatch)

    qc = item["meta"]["qc"]
    assert [c["key"] for c in qc["checks"]] == list(CHECK_KEYS)
    # P5-1：本课程没有概念库 → terminology 记为「未执行」，其余 10 项全 ok
    levels = _levels(qc)
    assert {k: v for k, v in levels.items() if k != "terminology"} == \
        {k: "ok" for k in CHECK_KEYS if k != "terminology"}, qc["checks"]
    assert levels["terminology"] == "skipped"
    assert qc["skipped_keys"] == ["terminology"] and qc["skipped_count"] == 1
    assert qc["executed_checks"] == 10 and qc["total_checks"] == 11
    assert qc["score"] == 100
    assert qc["metrics"]["sources"] == 11
    assert qc["metrics"]["quizzes"] == 2
    assert item["meta"]["sources"][0]["material_name"] == "光合作用导论"


# ---------------------------------------------------------------------------
# ② 缺小结 → structure fail
# ---------------------------------------------------------------------------

def test_missing_summary_fails_structure(client, course_id, monkeypatch):
    _plan_approve(client, course_id)
    _add_sections(course_id, "1", ["光合作用的过程"])
    node = _node_id(client, course_id, "1.1")
    item = _generate(client, course_id, node, _drop_section(QUALIFIED, "小结"), monkeypatch)

    qc = item["meta"]["qc"]
    assert _levels(qc)["structure"] == "fail"
    assert "小结" in next(c for c in qc["checks"] if c["key"] == "structure")["detail"]
    assert qc["score"] < 100


# ---------------------------------------------------------------------------
# ③ 套话堆砌 → fluff warn
# ---------------------------------------------------------------------------

def test_fluff_pileup_warns(client, course_id, monkeypatch):
    _plan_approve(client, course_id)
    _add_sections(course_id, "1", ["光合作用的过程"])
    node = _node_id(client, course_id, "1.1")
    item = _generate(client, course_id, node, _fluff_markdown(), monkeypatch)

    qc = item["meta"]["qc"]
    assert _levels(qc)["fluff"] == "warn"
    detail = next(c for c in qc["checks"] if c["key"] == "fluff")["detail"]
    assert "套话" in detail


# ---------------------------------------------------------------------------
# ④ 用别名 → terminology warn 且给建议标准名
# ---------------------------------------------------------------------------

def test_alias_usage_warns_with_standard_name():
    md = QUALIFIED.replace("光反应", "光阶段")
    concepts = [{"name": "光反应", "aliases": ["光阶段"]}]
    qc = compute_qc(md, objective="掌握光合作用的过程", quizzes=QUIZZES,
                    sources=parse_sources(md, "1.1", []), concepts=concepts, catalog=[])
    check = next(c for c in qc["checks"] if c["key"] == "terminology")
    assert check["level"] == "warn"
    assert "光阶段" in check["detail"] and "光反应" in check["detail"]
    assert check["issues"][0]["standard"] == "光反应"
    assert terminology_issues(md, concepts)[0]["alias"] == "光阶段"


def test_terminology_ignores_parenthetical_alias():
    """「标准名（别名）」这种首次括号注明不算问题。"""
    md = QUALIFIED.replace("光反应", "光反应（光阶段）", 1)
    assert terminology_issues(md, [{"name": "光反应", "aliases": ["光阶段"]}]) == []


# ---------------------------------------------------------------------------
# ⑤ 无来源 → sources warn
# ---------------------------------------------------------------------------

def test_missing_sources_warns():
    md = _no_source_markdown()
    qc = compute_qc(md, objective="掌握光合作用的过程", quizzes=QUIZZES,
                    sources=parse_sources(md, "1.1", []), concepts=[], catalog=[])
    check = next(c for c in qc["checks"] if c["key"] == "sources")
    assert check["level"] == "warn"
    assert "来源" in check["detail"]


# ---------------------------------------------------------------------------
# ⑥ 「按质检建议重生成」的提示词含质检问题文本
# ---------------------------------------------------------------------------

def test_regenerate_prompt_carries_qc_issues(client, course_id, monkeypatch):
    _plan_approve(client, course_id)
    _add_sections(course_id, "1", ["光合作用的过程"])
    node = _node_id(client, course_id, "1.1")
    item = _generate(client, course_id, node, _drop_section(QUALIFIED, "小结"), monkeypatch)
    issue_detail = next(c for c in item["meta"]["qc"]["checks"]
                        if c["key"] == "structure")["detail"]
    assert prompt_section(item["meta"]["qc"]).find(issue_detail) > 0

    node_row = SessionLocal()
    try:
        node_obj = node_row.get(OutlineNode, node)
        prompt = _lecture_user_prompt(node_obj, "正式", qc=item["meta"]["qc"])
    finally:
        node_row.close()
    assert issue_detail in prompt

    recorder = _Recorder(QUALIFIED)
    monkeypatch.setattr(lecture_mod, "build_llm_client", lambda: recorder)
    resp = client.post(f"/api/lecture/{item['id']}/regenerate-qc", json={"tone": "正式"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["qc_issues"], resp.text
    state = _wait_job(client, resp.json()["job_id"])
    assert state["status"] == "success", state
    assert issue_detail in recorder.prompts[0]
    assert state["result"]["item"]["meta"]["qc"]["score"] == 100


def test_regenerate_rejects_lecture_without_node(client, course_id):
    db = SessionLocal()
    try:
        row = ContentItem(course_id=course_id, node_id=None, kind="lecture",
                          title="孤立讲义", body={"markdown": "## x"}, meta={})
        db.add(row)
        db.commit()
        item_id = row.id
    finally:
        db.close()
    resp = client.post(f"/api/lecture/{item_id}/regenerate-qc", json={})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# ⑦ 整课汇总统计口径正确
# ---------------------------------------------------------------------------

def test_course_qc_report_counts_and_worst(client, course_id, monkeypatch):
    _plan_approve(client, course_id)
    _add_sections(course_id, "1", ["光合作用的过程", "影响光合作用的因素"])
    good = _node_id(client, course_id, "1.1")
    bad = _node_id(client, course_id, "1.2")
    _generate(client, course_id, good, QUALIFIED, monkeypatch)
    _generate(client, course_id, bad, _drop_section(QUALIFIED, "小结"), monkeypatch)

    report = client.post(f"/api/lecture/course/{course_id}/qc", json={"worst_n": 1}).json()
    assert report["checked"] == 2
    scores = sorted(row["score"] for row in report["items"])
    # P5-1：本课程没有概念库 → terminology 未执行，扣分按 8/7 归一到全项口径
    assert report["average_score"] == round(sum(scores) / 2, 1)
    assert report["skipped_items"] == 2, "两篇都应记一档「未执行项」"
    assert all(row["skipped_keys"] == ["terminology"] for row in report["items"])
    assert report["min_score"] == scores[0] and report["max_score"] == scores[1]
    structure = next(row for row in report["failure_distribution"] if row["key"] == "structure")
    assert structure == {"key": "structure", "warn": 0, "fail": 1, "failed": 1, "skipped": 0}
    assert len(report["worst"]) == 1
    assert report["worst"][0]["score"] == scores[0]
    assert report["worst"][0]["node_key"] == "1.2"
    assert report["checks"][0]["key"] == "structure"

    single = client.get(f"/api/lecture/{report['worst'][0]['item_id']}/qc").json()
    assert single["qc"]["score"] == single["qc"]["score"]
    assert single["issues"] == [c for c in open_issues(single["qc"])]
    assert single["node_key"] == "1.2"


# ---------------------------------------------------------------------------
# 来源锚定：meta.sources + 笔记「来源」小节 + verify 坏链检查
# ---------------------------------------------------------------------------

def test_sources_are_exported_to_notes_without_broken_links(client, course_id, monkeypatch):
    root = Path(tempfile.mkdtemp(prefix="coursegenie_qc_"))
    assert client.post(f"/api/kb/course/{course_id}/bind", json={
        "vault_root": str(root), "output_rel": "课程/光合作用"}).status_code == 200
    material = client.post("/api/material/paste", json={
        "course_id": course_id, "title": "光合作用导论",
        "text": "第一段：光合作用把光能变成化学能。\n第二段：光反应在类囊体薄膜上产生 ATP。\n"
                "第三段：暗反应在叶绿体基质里固定二氧化碳。\n第四段：两步衔接决定产量。\n"
                "第五段：温度与二氧化碳浓度都会影响速率。\n第六段：大棚补光就是应用。\n"
                "第七段：发电站与工厂的类比。\n第八段：小结两步关系。",
    }).json()

    _plan_approve(client, course_id)
    _add_sections(course_id, "1", ["光合作用的过程"])
    node = _node_id(client, course_id, "1.1")
    item = _generate(client, course_id, node, QUALIFIED, monkeypatch)

    sources = item["meta"]["sources"]
    assert len(sources) == 11
    assert {s["material_id"] for s in sources} == {material["id"]}
    assert sources[1]["paragraph"] == 2
    assert "类囊体薄膜" in sources[1]["material_quote"]
    assert sources[1]["section"] == "核心讲解"

    export = client.post(f"/api/kb/course/{course_id}/export", params={"force": True})
    assert export.status_code == 200, export.text
    pages = [p for p in (root / "课程/光合作用").rglob("1.1-*.md")
             if "习题集" not in p.parts and "教案" not in p.parts and "学案" not in p.parts]
    assert len(pages) == 1
    text = pages[0].read_text(encoding="utf-8")
    assert "## 来源" in text
    assert f"[[素材库/01-{material['title']}|{material['title']}]]" in text
    # 每条来源一个条目：素材双链 + 小节 + 段落号 + 该段摘要
    assert text.count(f"[[素材库/01-{material['title']}|{material['title']}]]") == 11
    assert "· 第 2 段：第二段：光反应在类囊体薄膜上产生 ATP。" in text
    assert "· 引入 · 第 1 段：" in text

    verify = client.post(f"/api/kb/course/{course_id}/verify").json()
    assert verify["inconsistent"] == 0, verify["nodes"]
    link_rows = [row for row in verify["nodes"] if row.get("part") == "link"]
    assert link_rows == []
# ---------------------------------------------------------------------------
# 术语与概念库对齐：提示词注入术语表 + 笔记页末「术语」小节
# ---------------------------------------------------------------------------

def _add_concept(course_id: str, name: str, aliases: list[str]) -> str:
    """直接落一条概念库条目（概念抽取走 LLM，这里为可比对性手工造）。"""
    db = SessionLocal()
    try:
        row = ContentItem(
            course_id=course_id, node_id=None, kind="concept", title=name,
            body={"definition": f"{name}的定义", "points": [], "relations": {},
                  "pitfalls": [], "sources": []},
            meta={"concept_id": concept_id_for(name), "aliases": aliases,
                  "chapters": [], "edited": False},
        )
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def test_terminology_empty_library_degrades_gracefully():
    """概念库为空 → 术语比对**未执行**（P5-1：不再按合格计分）。"""
    qc = compute_qc(QUALIFIED, objective="掌握光合作用的过程", quizzes=QUIZZES,
                    sources=parse_sources(QUALIFIED, "1.1", []), concepts=[], catalog=[])
    check = next(c for c in qc["checks"] if c["key"] == "terminology")
    assert check["level"] == "skipped"
    assert "概念库为空" in check["detail"]
    assert qc["skipped_keys"] == ["terminology"] and qc["skipped_count"] == 1


def test_qc_skipped_flips_to_real_result_after_concepts_extracted(client, course_id,
                                                                  monkeypatch):
    """P5-1：概念库为空 → 术语比对「未执行」；抽完概念复检 → 转成真实结论。"""
    _plan_approve(client, course_id)
    _add_sections(course_id, "1", ["光合作用的过程"])
    node = _node_id(client, course_id, "1.1")
    item = _generate(client, course_id, node, QUALIFIED, monkeypatch)

    qc = item["meta"]["qc"]
    assert _levels(qc)["terminology"] == "skipped"
    assert qc["skipped_count"] == 1 and qc["executed_checks"] == 10
    assert "1 项未执行" in qc["note"]

    # 抽完概念（这里手工落一条等价于抽取结果）→ 复检：未执行项归零、结论变真
    _add_concept(course_id, "光反应", ["光阶段"])
    refreshed = client.get(f"/api/lecture/{item['id']}/qc",
                           params={"refresh": True}).json()
    qc2 = refreshed["qc"]
    assert qc2["skipped_count"] == 0 and qc2["skipped_keys"] == []
    assert "note" not in qc2
    assert _levels(qc2)["terminology"] == "ok"
    assert qc2["executed_checks"] == qc2["total_checks"] == len(CHECK_KEYS)
    assert refreshed["issues"] == open_issues(qc2)


def test_terminology_injected_in_prompt_and_listed_in_notes(client, course_id, monkeypatch):
    root = Path(tempfile.mkdtemp(prefix="coursegenie_qc_cpt_"))
    assert client.post(f"/api/kb/course/{course_id}/bind", json={
        "vault_root": str(root), "output_rel": "课程/光合作用"}).status_code == 200
    _add_concept(course_id, "光反应", ["光阶段"])
    _plan_approve(client, course_id)
    _add_sections(course_id, "1", ["光合作用的过程"])
    node = _node_id(client, course_id, "1.1")

    stub = _StubLLM(QUALIFIED.replace("光反应", "光阶段"))
    monkeypatch.setattr(lecture_mod, "build_llm_client", lambda: stub)
    resp = client.post("/api/lecture/generate", json={
        "course_id": course_id, "node_id": node, "tone": "正式"})
    state = _wait_job(client, resp.json()["job_id"])
    assert state["status"] == "success", state
    item = state["result"]["item"]

    # 生成前：概念库标准名 + 别名进了术语表
    assert "课程术语表" in stub.seen[0]
    assert "标准名「光反应」" in stub.seen[0] and "光阶段" in stub.seen[0]
    # 生成后：用别名 → terminology warn，并给出建议标准名
    check = next(c for c in item["meta"]["qc"]["checks"] if c["key"] == "terminology")
    assert check["level"] == "warn"
    assert "光阶段 → 建议「光反应」" in check["detail"]

    assert client.post(f"/api/kb/course/{course_id}/export",
                       params={"force": True}).status_code == 200
    page = next(p for p in (root / "课程/光合作用").rglob("1.1-*.md")
                if "习题集" not in p.parts)
    text = page.read_text(encoding="utf-8")
    assert "## 术语" in text
    assert "正文用了别名「光阶段」→ 建议改用标准名 [[光反应]]" in text
    verify = client.post(f"/api/kb/course/{course_id}/verify").json()
    assert verify["inconsistent"] == 0, verify["nodes"]


# ---------------------------------------------------------------------------
# P5-2：缺来源标注不只给条数，还要给「是哪几句」+上下文+行号
# ---------------------------------------------------------------------------

UNSOURCED_MD = """## 光合作用的过程

### 光反应
- **光反应**发生在类囊体薄膜上，把光能变成 ATP（来源: 光合作用导论 #1）
- **暗反应**在基质中把二氧化碳固定成糖，需要光反应供电

### 暗反应
- 光反应与暗反应必须紧挨着发生

## 随堂练习
1. 下面哪个说法对？
- 选项 A（来源: 光合作用导论 #9）
"""


def test_unsourced_sentences_give_text_context_line_and_section():
    rows = unsourced_sentences(UNSOURCED_MD)
    assert [row["text"] for row in rows] == [
        "**暗反应**在基质中把二氧化碳固定成糖，需要光反应供电",
        "光反应与暗反应必须紧挨着发生",
    ]
    assert rows[0]["line"] == 5 and rows[1]["line"] == 8
    assert rows[0]["section"] == "光反应" and rows[1]["section"] == "暗反应"
    # 上下文：能看到上一行已经标了来源，便于对照
    assert "来源: 光合作用导论 #1" in rows[0]["context"]
    # 「随堂练习」后面的选项不算关键点
    assert all("选项 A" not in row["text"] for row in rows)


def test_qc_payload_carries_unsourced_sentences(client, course_id, monkeypatch):
    """质检里的 sources 检查项要带出具体句子，供前端跳转。"""
    _plan_approve(client, course_id)
    _add_sections(course_id, "1", ["光合作用的过程"])
    node = _node_id(client, course_id, "1.1")
    md = re.sub(r"（来源[^）]*）", "", QUALIFIED)
    assert md != QUALIFIED and "来源:" not in md.split("随堂练习")[0]
    item = _generate(client, course_id, node, md, monkeypatch)
    check = next(c for c in item["meta"]["qc"]["checks"] if c["key"] == "sources")
    assert check["level"] == "warn"
    assert check["sentences"], check
    assert all(row["text"] and row["context"] for row in check["sentences"])
    assert all(isinstance(row["line"], int) and row["line"] > 0 for row in check["sentences"])

def _downgrade_qc_to_v1(item_id: str) -> None:
    """把质检退回「上线前的老格式」：version 1 且 checks 里没有 sentences。"""
    db = SessionLocal()
    try:
        row = db.get(ContentItem, item_id)
        qc = json.loads(json.dumps(row.meta["qc"]))
        qc["version"] = 1
        for check in qc["checks"]:
            check.pop("sentences", None)
        row.meta = {**row.meta, "qc": qc}
        db.commit()
    finally:
        db.close()


def test_outdated_qc_gets_sentences_backfilled_on_read(client, course_id, monkeypatch):
    """老数据（meta.qc 缺 sentences）**不刷新**读取也要带出具体句子（任务 30）。

    真机缺口：AI 课 27 篇讲义的 meta.qc 都是上线前存的 version 1，
    GET /api/lecture/{id}/qc 不刷新时 issues[].sentences 为空 ——
    用户看到「26 篇缺来源标注」，却看不到是哪几句。
    """
    _plan_approve(client, course_id)
    _add_sections(course_id, "1", ["光合作用的过程"])
    node = _node_id(client, course_id, "1.1")
    md = re.sub(r"（来源[^）]*）", "", QUALIFIED)
    item = _generate(client, course_id, node, md, monkeypatch)
    _downgrade_qc_to_v1(item["id"])

    # 不刷新：直接读质检明细
    resp = client.get(f"/api/lecture/{item['id']}/qc")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["qc"]["version"] == QC_VERSION, payload["qc"]["version"]
    check = next(c for c in payload["issues"] if c["key"] == "sources")
    assert check["sentences"], check
    assert all(row["text"] and row["context"] for row in check["sentences"])
    assert all(isinstance(row["line"], int) and row["line"] > 0 for row in check["sentences"])

    # 补算结果已落库，第二次读不会再退回老口径
    db = SessionLocal()
    try:
        stored = db.get(ContentItem, item["id"]).meta["qc"]
    finally:
        db.close()
    assert stored["version"] == QC_VERSION
    assert next(c for c in stored["checks"] if c["key"] == "sources")["sentences"]


def test_fresh_qc_is_not_recomputed(client, course_id, monkeypatch):
    """口径够新时读取不能白算一遍：generated_at 不变。"""
    _plan_approve(client, course_id)
    _add_sections(course_id, "1", ["光合作用的过程"])
    node = _node_id(client, course_id, "1.1")
    md = re.sub(r"（来源[^）]*）", "", QUALIFIED)
    item = _generate(client, course_id, node, md, monkeypatch)
    first = client.get(f"/api/lecture/{item['id']}/qc").json()["qc"]
    again = client.get(f"/api/lecture/{item['id']}/qc").json()["qc"]
    assert again["generated_at"] == first["generated_at"]


# ---------------------------------------------------------------------------
# 任务 41：substance（含金量）+ actionable（可操作）+ 矛盾护栏
# ---------------------------------------------------------------------------

# 「通篇通用观点」稿：没有一手案例 / 失败教训 / 边界声明，也没有可照做的步骤
GENERIC_ONLY = """## 光合作用的过程

### 引入
- 光合作用对生命活动非常重要，是生物圈运转的能量基础（来源: 光合作用导论 #1）

### 核心讲解
- **光反应**：把光能转成化学能，是这套机制的第一步（来源: 光合作用导论 #2）
- **暗反应**：把二氧化碳固定成有机物，是这套机制的第二步（来源: 光合作用导论 #3）

### 小结
- 综上，理解光合作用对后续学习至关重要（来源: 光合作用导论 #8）
"""

# 「步骤只有抽象描述」稿：有动作动词，但没有判断信号/参数
ABSTRACT_STEPS = """## 光合作用的过程

### 核心讲解
- **光反应**：把光能转成化学能（来源: 光合作用导论 #2）

### 可照做的操作
- 记录棚内温度与光照，注意做好关键环节的把控（来源: 光合作用导论 #10）

### 小结
- 抓住两步衔接这条主线（来源: 光合作用导论 #8）
"""


def test_substance_warns_when_only_generic_points():
    """通篇通用观点 → substance warn（任务 41：训练数据里就有的话不算含金量）。"""
    qc = compute_qc(GENERIC_ONLY, objective="掌握光合作用的过程", quizzes=QUIZZES,
                    sources=parse_sources(GENERIC_ONLY, "1.1", []), concepts=[], catalog=[])
    check = next(c for c in qc["checks"] if c["key"] == "substance")
    assert check["level"] == "warn", check
    assert check["hits"] == {"case": 0, "failure": 0, "boundary": 0}, check
    assert "一手案例" in check["detail"] and "失败教训" in check["detail"]


def test_substance_ok_with_failure_case_and_boundary():
    """有失败教训 + 边界声明 → substance ok，并分别报出命中数。"""
    qc = compute_qc(QUALIFIED, objective="掌握光合作用的过程", quizzes=QUIZZES,
                    sources=parse_sources(QUALIFIED, "1.1", []), concepts=[], catalog=[])
    check = next(c for c in qc["checks"] if c["key"] == "substance")
    assert check["level"] == "ok", check
    assert check["hits"]["failure"] >= 1 and check["hits"]["boundary"] >= 1, check
    assert substance_hits(QUALIFIED)["failure"] >= 1


def test_substance_flags_muddy_sentences():
    """「各有优劣」式和稀泥句 → substance warn 并点名（矛盾护栏）。"""
    md = QUALIFIED + "\n- 两种补光方案各有优劣，具体怎么选要因情况而异（来源: 光合作用导论 #9）\n"
    qc = compute_qc(md, objective="掌握光合作用的过程", quizzes=QUIZZES,
                    sources=parse_sources(md, "1.1", []), concepts=[], catalog=[])
    check = next(c for c in qc["checks"] if c["key"] == "substance")
    assert check["level"] == "warn", check
    assert "和稀泥" in check["detail"]
    assert muddy_sentences(md)[:2] == ["各有优劣", "因情况而异"]


def test_actionable_warns_without_steps():
    qc = compute_qc(GENERIC_ONLY, objective="掌握光合作用的过程", quizzes=QUIZZES,
                    sources=parse_sources(GENERIC_ONLY, "1.1", []), concepts=[], catalog=[])
    check = next(c for c in qc["checks"] if c["key"] == "actionable")
    assert check["level"] == "warn", check
    assert "没有可照做的步骤" in check["detail"]


def test_actionable_warns_when_steps_are_abstract():
    qc = compute_qc(ABSTRACT_STEPS, objective="掌握光合作用的过程", quizzes=QUIZZES,
                    sources=parse_sources(ABSTRACT_STEPS, "1.1", []), concepts=[], catalog=[])
    check = next(c for c in qc["checks"] if c["key"] == "actionable")
    assert check["level"] == "warn", check
    assert "判断信号" in check["detail"]


def test_actionable_ok_for_executable_steps():
    """含动作 + 判断信号（12~15℃ / 低于 12℃）+ 产出物（写入《夜温记录表》）→ ok。"""
    qc = compute_qc(QUALIFIED, objective="掌握光合作用的过程", quizzes=QUIZZES,
                    sources=parse_sources(QUALIFIED, "1.1", []), concepts=[], catalog=[])
    check = next(c for c in qc["checks"] if c["key"] == "actionable")
    assert check["level"] == "ok", check
    # 步骤候选 = 列表项里带动作动词的行（含「可照做的操作」两条）
    assert check["steps"] >= 2


def test_lecture_prompt_carries_cangjie_priority():
    """四条要求必须真的进了提示词（任务 41 A 段）：事前说清，而不是事后抓。"""
    system = lecture_mod.LECTURE_SYSTEM
    for word in ("一手案例", "失败教训", "能力边界", "内在矛盾", "素材未提及，建议补充采访",
                 "各有优劣", "什么情况要找专家", "产出物"):
        assert word in system, word
    db = SessionLocal()
    try:
        node = OutlineNode(outline_id="x", node_key="1.1", node_type="section",
                           title="光合作用的过程", objective="掌握过程")
        prompt = lecture_mod._lecture_user_prompt(node, "正式")
    finally:
        db.close()
    assert "踩过的坑" in prompt and "各有优劣" in prompt


def test_qc_payload_carries_new_checks(client, course_id, monkeypatch):
    """接口层：meta.qc 里 11 项齐备，前端徽标按 CHECK_KEYS 自动跟随。"""
    _plan_approve(client, course_id)
    _add_sections(course_id, "1", ["光合作用的过程"])
    node = _node_id(client, course_id, "1.1")
    item = _generate(client, course_id, node, GENERIC_ONLY, monkeypatch)

    qc = item["meta"]["qc"]
    assert [c["key"] for c in qc["checks"]] == list(CHECK_KEYS)
    assert qc["total_checks"] == 11 and qc["executed_checks"] == 10
    levels = _levels(qc)
    assert levels["substance"] == "warn" and levels["actionable"] == "warn"
    # 评分仍按已执行项归一（任务 30 的 skipped 机制没被回退）
    assert qc["skipped_keys"] == ["terminology"]
    penalty = sum({"warn": 6, "fail": 20}.get(c["level"], 0) for c in qc["checks"])
    assert penalty >= 12, qc["checks"]          # 两项新检查真的进了扣分
    # 已执行项扣分按「全部 / 已执行」等比放大（分母 11 / 分子 10 已在上面的断言钉死）
    assert qc["score"] == max(0, round(100 - penalty * qc["total_checks"]
                                       / qc["executed_checks"])), qc["checks"]


# ---------------------------------------------------------------------------
# ⑪ 任务 47：新增小节（踩过的坑 / 执行步骤）的关键点也必须标来源
# ---------------------------------------------------------------------------

# 正文（引入/核心讲解）标了来源，新增小节（执行步骤 / 踩过的坑）漏标 ——
# 任务 41 的口径缺口就是这一类：要求变多了，但没告诉模型新小节也要标。
UNLABELED_NEW_SECTIONS = """## 出处标注的标准格式

### 引入

出处标注把结论和来源绑在一起，方便复核（来源: 降低幻觉五法 #42）。

### 核心讲解

- 来源标识：写清素材标题（来源: 降低幻觉五法 #42）
- 位置锚点：写到段落号

### 执行步骤

1. 打开素材清单，找到目标段落
2. 记录段落前的 [序号]
3. 在句末补上来源标注

### 踩过的坑

- 一开始只给正文标来源，新增的小节整节漏标
- 表格行被当成标注豁免区，结果整张表查不到出处

### 小结

- 每条关键点都要能追溯到素材
"""


def test_sources_warn_for_unlabeled_new_sections():
    """任务 47：执行步骤 / 踩过的坑里的关键点没标来源 → sources 仍判 warn（不放宽）。"""
    qc = compute_qc(UNLABELED_NEW_SECTIONS, objective="掌握出处标注格式", quizzes=[],
                    sources=parse_sources(UNLABELED_NEW_SECTIONS, "4.1", []),
                    concepts=[], catalog=[])
    check = next(c for c in qc["checks"] if c["key"] == "sources")
    assert check["level"] == "warn", check
    # 缺的是新小节的条目，不是正文里已标来源的那条
    assert len(unsourced_points(UNLABELED_NEW_SECTIONS)) == 7, check


def test_sources_detail_names_sections():
    """任务 47：detail 要点名「哪个小节、第几条」，老师才知道去哪补标注。"""
    qc = compute_qc(UNLABELED_NEW_SECTIONS, objective="掌握出处标注格式", quizzes=[],
                    sources=parse_sources(UNLABELED_NEW_SECTIONS, "4.1", []),
                    concepts=[], catalog=[])
    check = next(c for c in qc["checks"] if c["key"] == "sources")
    detail = check["detail"]
    assert "### 执行步骤" in detail and "### 踩过的坑" in detail, detail
    # 整节都没标 → 直接说「整节未标来源」，不用逐个列序号
    assert "整节未标来源" in detail, detail
    # 核心讲解里有 1 条漏标 → 列成「第 2 条（共 2 条）」
    assert "### 核心讲解 第 2 条（共 2 条）" in detail, detail
    # sentences 维持原样（前端在用，本次不动）
    assert [s["section"] for s in check["sentences"]].count("执行步骤") == 3


def test_no_basis_mark_is_not_a_boundary_claim():
    """守护（任务 41 行为，别回归）：来源口径的「无直接依据」不算能力边界声明。"""
    md = """## 通用说明

### 引入

这件事很重要（无直接依据,建议人工确认）。

### 核心讲解

- 做好规划
- 注意细节（无直接依据,建议人工确认）

### 小结

- 多练习
"""
    qc = compute_qc(md, objective="掌握这件事", quizzes=[],
                    sources=parse_sources(md, "1.1", []), concepts=[], catalog=[])
    check = next(c for c in qc["checks"] if c["key"] == "substance")
    assert check["level"] == "warn", check
    assert check["hits"] == {"case": 0, "failure": 0, "boundary": 0}, check
    # 标了「无直接依据」的条目不算「漏标来源」（来源口径与含金量口径互不冒用）
    missing = unsourced_points(md)
    assert "注意细节（无直接依据,建议人工确认）" not in missing, missing
    assert len(missing) == 2, missing          # 另两条确实没标 → 仍会被 sources 抓出来


# ---------------------------------------------------------------------------
# ⑫ 任务 47b：分组标签行不计入来源覆盖（四条护栏 + 一致性）
# ---------------------------------------------------------------------------

# DSH 实测的写法：父标签本身没有断言、来源都在子项里 → 不该算「漏标」
GROUP_LABEL_MD = """## 温度参数怎么调

### 核心讲解

- **行为阈值**：
  - 温度=0时：模型必然选择最高概率词汇，输出稳定但机械（来源: 《AI降幻五法》#60）
  - 温度>1.0时：低概率词汇显著增多，可能触发非常规但错误的组合（来源: 《破解AI幻觉》#73）
"""

# 护栏(a)：悬空标签（后面没有子项）→ 仍算关键点
DANGLING_LABEL_MD = """## 标题

### 核心讲解

- **悬空标签**：
- 另一条（来源: 《AI降幻五法》#1）
"""

# 护栏(b)：标签行含实质断言（「成本会翻三倍」是断言不是分组）→ 仍算关键点
ASSERTION_LABEL_MD = """## 标题

### 核心讲解

- **成本会翻三倍**：
  - 子项（来源: 《AI降幻五法》#2）
"""

# 护栏(c)：子项各自仍算关键点、仍要各自标来源（整组不豁免）
CHILD_UNSOURCED_MD = """## 标题

### 核心讲解

- **分层记忆策略**：
  - 子项没标来源
  - 子项二（来源: 《AI降幻五法》#3）
"""

# 护栏(d)：普通句子 / 顶层无缩进条目 / **有内容的编号步骤**一律照旧
# （编号**父标签**另有测试：它与 `- **行为阈值**：` 只是列表标记不同）
PLAIN_AND_NUMBERED_MD = """## 标题

### 核心讲解

1. 打开配置文件并保存副本
2. **编号标签**：
   - 子项（来源: 《AI降幻五法》#4）
- 顶层无缩进条目没标来源
"""

# 真机 5.1 的写法：编号父标签 + 带来源的子项（内容与来源都在子项里）
NUMBERED_PARENT_LABEL_MD = """## 标题

### 核心讲解

1. **技术本质**：基于逐词生成（来源: 《AI降幻五法》#33）
2. **动态调整**：
   - 事实查询类任务缩小窗口至原配置的70%（来源: 《AI降幻五法》#53）
   - 创意生成类任务可增加20%余量（无直接依据,建议人工确认）
"""


def _sources_check(md: str) -> dict:
    qc = compute_qc(md, objective="掌握温度参数怎么调", quizzes=[],
                    sources=parse_sources(md, "2.4", []), concepts=[], catalog=[])
    return next(c for c in qc["checks"] if c["key"] == "sources")


def test_group_label_rows_excluded_from_source_coverage():
    """分组标签行不计入来源覆盖：只有子项算关键点，整段因此 sources=ok。"""
    assert unsourced_points(GROUP_LABEL_MD) == []
    assert _sources_check(GROUP_LABEL_MD)["level"] == "ok"
    # 标签行仍在「所有列表行」里（篇幅/复杂度统计的口径没被连带改动）
    assert "**行为阈值**：" in key_points(GROUP_LABEL_MD)
    assert "**行为阈值**：" not in source_key_points(GROUP_LABEL_MD)
    # 两个子项都算关键点
    assert len(source_key_points(GROUP_LABEL_MD)) == 2


def test_group_label_guard_dangling_label_still_counts():
    """护栏(a)：标签后面没有子项 → 仍算关键点、照旧 warn。"""
    missing = unsourced_points(DANGLING_LABEL_MD)
    assert missing == ["**悬空标签**："], missing
    assert _sources_check(DANGLING_LABEL_MD)["level"] == "warn"


def test_group_label_guard_assertion_label_still_counts():
    """护栏(b)：标签行含实质断言（「成本会翻三倍」）→ 仍算关键点。"""
    assert unsourced_points(ASSERTION_LABEL_MD) == ["**成本会翻三倍**："]
    assert _sources_check(ASSERTION_LABEL_MD)["level"] == "warn"


# 护栏(b) 真实补漏：9.1 的这两行与「成本会翻三倍」同类（都是带数字的量化断言），
# 只因为用了「提升／增长」而不是「提高／增加」就被豁免 → 属于口径漏网。
QUANT_ASSERTION_LABEL_MD = """## 标题

### 核心讲解

- **上下文窗口占用率提升30%**：
  - 子项（来源: 《AI降幻五法》#5）
- **成本线性增长**：
  - 子项（来源: 《AI降幻五法》#6）
"""


def test_group_label_guard_quantitative_assertion_still_counts():
    """护栏(b)：带量化断言的标签行（提升30%／线性增长）仍算关键点、仍需来源。"""
    assert unsourced_points(QUANT_ASSERTION_LABEL_MD) == [
        "**上下文窗口占用率提升30%**：", "**成本线性增长**："]
    assert _sources_check(QUANT_ASSERTION_LABEL_MD)["level"] == "warn"


def test_group_label_guard_children_still_need_sources():
    """护栏(c)：子项各自仍算关键点、仍要各自标来源（整组不豁免）。"""
    assert unsourced_points(CHILD_UNSOURCED_MD) == ["子项没标来源"]
    assert _sources_check(CHILD_UNSOURCED_MD)["level"] == "warn"


def test_group_label_guard_plain_and_numbered_items_unchanged():
    """护栏(d)：普通句子 / 顶层条目 / 有内容的编号步骤照旧计入。

    编号步骤（``1. 打开配置文件并保存副本``）是**非标签条目**，护栏 (d) 要求它照旧；
    编号**父标签**（``2. **编号标签**：`` 后面挂着子项）属于「标签条目」，与
    ``- **行为阈值**：`` 只是列表标记不同，因此同样豁免（见下一条测试）。
    """
    missing = unsourced_points(PLAIN_AND_NUMBERED_MD)
    assert missing == ["打开配置文件并保存副本", "顶层无缩进条目没标来源"], missing


def test_group_label_numbered_parent_label_also_exempt():
    """编号父标签也豁免（真机 5.1 `3. **动态调整**：`）；子项照旧各自要来源。"""
    assert unsourced_points(NUMBERED_PARENT_LABEL_MD) == []
    assert _sources_check(NUMBERED_PARENT_LABEL_MD)["level"] == "ok"
    # 子项仍各自算关键点
    assert len(source_key_points(NUMBERED_PARENT_LABEL_MD)) == 3


@pytest.mark.parametrize("md", [GROUP_LABEL_MD, DANGLING_LABEL_MD, ASSERTION_LABEL_MD,
                                QUANT_ASSERTION_LABEL_MD, CHILD_UNSOURCED_MD,
                                PLAIN_AND_NUMBERED_MD, NUMBERED_PARENT_LABEL_MD,
                                UNLABELED_NEW_SECTIONS], ids=[
                                    "group_label", "dangling", "assertion", "child",
                                    "quant_assertion", "plain_numbered", "numbered_label",
                                    "task47_sample"])
def test_unsourced_groups_match_unsourced_points(md):
    """一致性：分组条数之和 == unsourced_points 条数（否则 detail 点名的条号会错位）。"""
    groups = unsourced_groups(md)
    assert sum(len(group["indexes"]) for group in groups) == len(unsourced_points(md))
    # sentences（前端跳转用）与 unsourced_points 也用同一套条目集合
    assert len(unsourced_sentences(md)) == len(unsourced_points(md))


def test_unsourced_groups_match_unsourced_points_on_real_course():
    """真实课讲义上的一致性（需要 ``CG_REAL_DB`` 指向真实库副本，否则跳过）。

    真库是只读打开、零 LLM：只验证「同一套条目集合」在真实正文上不打架。
    """
    real_db = os.environ.get("CG_REAL_DB", "")
    if not real_db or not Path(real_db).exists():
        pytest.skip("未设置 CG_REAL_DB（真实库副本路径）")
    from sqlalchemy import create_engine, text as sql_text

    engine = create_engine(f"sqlite:///{real_db}")
    with engine.connect() as conn:
        rows = conn.execute(sql_text(
            "SELECT body FROM content_items WHERE kind = 'lecture'")).fetchall()
    assert rows, "真实库里没有讲义"
    checked = 0
    for (body,) in rows:
        # 裸 SQL 读回来的是 JSON 文本（没走 ORM 的 JSON 类型），先解一下
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError:
                body = None
        md = (body or {}).get("markdown") if isinstance(body, dict) else None
        if not md:
            continue
        checked += 1
        assert (sum(len(g["indexes"]) for g in unsourced_groups(md))
                == len(unsourced_points(md)))
    assert checked >= 10, f"样本太少（{checked} 篇）"


# ---------------------------------------------------------------------------
# ⑬ 任务 47c：来源括号纪律（靠提示词说清楚，解析器不放宽）
# ---------------------------------------------------------------------------

# 两种「写歪」形态：其实想标，但两个正则都不认（任务 47b 采样里那 4/10 条就是它们）。
MISWRITTEN_SOURCE_MARKS_MD = """## 标题

### 核心讲解

- 涉及人身安全的决策（来源: 无直接依据,建议人工确认）
- 高风险场景要点（来源: 破解AI幻觉 #82；改进方法：建立场景分级速查表）
"""


def test_lecture_prompt_states_source_parentheses_discipline():
    """两条硬纪律必须真的进了提示词（system + user 两侧；事前说清，而不是事后抓）。"""
    system = lecture_mod.LECTURE_SYSTEM
    for phrase in ("硬纪律一", "硬纪律二", "不夹带", "短名词短语", "会被当成普通条目",
                   "（来源: 无直接依据,建议人工确认）"):
        assert phrase in system, phrase
    # 半角标点会把分组标签降级成普通条目 —— 这条也要在提示词里说清
    assert "不加逗号、句号、分号" in system
    db = SessionLocal()
    try:
        node = OutlineNode(outline_id="x", node_key="1.2", node_type="section",
                           title="为什么幻觉是 AI 应用的头号顽疾", objective="掌握成因")
        prompt = lecture_mod._lecture_user_prompt(node, "正式")
    finally:
        db.close()
    for phrase in ("不夹带", "不要写「来源:」前缀"):
        assert phrase in prompt, phrase


def test_miswritten_source_marks_still_count_as_missing():
    """两种混写形态**仍算缺标注**（解析器一个都没放宽：我们靠提示词解决，不靠放宽）。"""
    assert unsourced_points(MISWRITTEN_SOURCE_MARKS_MD) == [
        "涉及人身安全的决策（来源: 无直接依据,建议人工确认）",
        "高风险场景要点（来源: 破解AI幻觉 #82；改进方法：建立场景分级速查表）"]
    # `_CITE_RE` / `_NO_BASIS_RE` 原样：这两种写法一个都认不出来（所以只能靠提示词写对）
    assert parse_sources(MISWRITTEN_SOURCE_MARKS_MD, "1.2", []) == []
    assert _sources_check(MISWRITTEN_SOURCE_MARKS_MD)["level"] == "warn"


def test_lecture_prompt_version_bumped_to_v8():
    """指纹版本必须 +1（48b → v7；任务 58 的术语表带定义 + 首现给解释 → v8），
    否则 48c 会把新措辞误跳过（任务 48d 的可操作护栏 → v9；任务 51 的 K-12 课时口径 → v11；
    任务 53 的公式口径 → v12；任务 55 的题型扩展 + 分层作业（level/minutes/rubric）→ v13）；
    质检规则版本（10 项口径）不动。"""
    assert lecture_mod.PROMPT_TEMPLATE_VERSION == "lecture-v14"
    assert lecture_mod.QC_RULES_VERSION == "qc-v2"
