"""任务 58：陌生术语必须解释 + 入库 + 可链接（含大纲字段）。

真机问题：用户在大纲里看到「**教学策略：马尔可夫链预测演示**」——全课没有任何地方解释它
（概念库 9 条里没有、术语表为空、正文也没出现过），因为**概念抽取只读讲义正文**，
大纲字段里自造的术语必然漏网。本文件守护四件事：

1. **质检可测量**：策略/评估字段里出现术语嫌疑词（拉丁缩写 / 专业词缀）且既不在概念库、
   也不在术语表（带 definition）→ ``terminology`` 判 warn，detail 点名「词 + 字段」
   （**并入现有项**，不新增第 11 项、不动 CHECK_KEYS/权重/评分分母）；
2. **抽取范围**：``teaching_strategy`` / ``assessment`` 进语料与准入（跨 ≥2 知识点 → 概念页；
   只出现一次 → ``meta.terminology``），条目必须带 definition；
3. **渲染**：术语表注入带定义 + 正文术语首次出现给括号解释；笔记「## 术语」列标准名 +
   一句话定义 + 双链；
4. **0-LLM 存量处理**：``scripts/backfill_outline_terms.py`` 只把「自带括号解释」的术语入表，
   光秃术语只报告、不编解释。

协作规则第 7 条：DATABASE_URL / STORAGE_DIR / OBSIDIAN_VAULT_ROOT 由 conftest 统一
指向临时目录，本文件不碰真实知识库。
"""
from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from app.db.session import SessionLocal
from app.models.course import ContentItem, Course, Outline, OutlineNode
from app.services import concept as concept_service
from app.services import kb as kb_service
from app.services import lecture as lecture_mod
from app.services import planner as planner_mod
from app.services.lecture_qc import (CHECK_KEYS, QC_VERSION, _WEIGHT, compute_qc,
                                     jargon_suspects, terminology_prompt_section,
                                     unexplained_jargon)

MARKDOWN = """## 概率机器的本质

### 核心讲解
- 模型按概率挑下一个词，而不是查事实库（来源: 破解AI幻觉 #33）
"""

CONCEPTS = [{"name": "概率机器本质", "aliases": ["概率生成机制"],
             "body": {"definition": "模型逐词按概率生成，不查事实库"}}]


def _terminology(**kw) -> dict:
    qc = compute_qc(MARKDOWN, concepts=CONCEPTS, **kw)
    return next(c for c in qc["checks"] if c["key"] == "terminology")


def _course_with_nodes(db, rows) -> Course:
    """造一门课 + 一个大纲 + 若干知识点（策略/评估字段就是被测对象）。"""
    course = Course(title="AI 幻觉治理实战", subject="AI", audience="职场人",
                    duration_minutes=120)
    db.add(course)
    db.flush()
    outline = Outline(course_id=course.id, title="AI 幻觉治理实战", goal="", content={})
    db.add(outline)
    db.flush()
    for index, row in enumerate(rows):
        db.add(OutlineNode(outline_id=outline.id, node_key=row["key"], node_type="point",
                           title=row["title"], objective=row.get("objective", ""),
                           teaching_strategy=row.get("strategy", ""),
                           assessment=row.get("assessment", ""), order_index=index))
    db.commit()
    return course


def _load_backfill_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "backfill_outline_terms.py"
    spec = spec_from_file_location("backfill_outline_terms_under_test", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _first_node(db, course) -> OutlineNode:
    """本课程的第一个知识点（测试库是共享的，必须按课程过滤）。"""
    return (db.query(OutlineNode)
            .join(Outline, OutlineNode.outline_id == Outline.id)
            .filter(Outline.course_id == course.id)
            .order_by(OutlineNode.order_index).first())


# ---------------------------------------------------------------------------
# ① 质检：策略/评估里的陌生术语 → terminology warn 且点名「词 + 字段」
# ---------------------------------------------------------------------------

def test_strategy_jargon_warns_and_names_the_field():
    """验收 5：策略里塞「马尔可夫链预测演示」→ warn 且点名该词与字段。"""
    check = _terminology(strategy="马尔可夫链预测演示")
    assert check["level"] == "warn"
    assert "马尔可夫链" in check["detail"] and "教学策略" in check["detail"]
    assert check["jargon"] == [{"term": "马尔可夫链", "field": "教学策略"}]


def test_assessment_jargon_warns_with_its_own_field():
    check = _terminology(assessment="设计一个三模型验证流程")
    assert check["level"] == "warn"
    assert "三模型" in check["detail"] and "评估方式" in check["detail"]


def test_jargon_covered_by_concept_library_is_ok():
    qc = compute_qc(MARKDOWN,
                    concepts=CONCEPTS + [{"name": "RAG", "body": {"definition": "检索增强生成"}}],
                    strategy="用 RAG 检索素材后再回答")
    check = next(c for c in qc["checks"] if c["key"] == "terminology")
    assert check["level"] == "ok"


def test_jargon_needs_a_defined_terminology_entry_to_count_as_covered():
    """术语表条目**必须带 definition** 才算「查得到」：只有名字不算。"""
    with_definition = [{"name": "马尔可夫链", "definition": "只根据当前状态预测下一步的概率模型"}]
    assert _terminology(strategy="马尔可夫链预测演示",
                        terminology=with_definition)["level"] == "ok"
    assert _terminology(strategy="马尔可夫链预测演示",
                        terminology=[{"name": "马尔可夫链", "definition": ""}])["level"] == "warn"


def test_parenthetical_explanation_is_not_reported_as_jargon():
    """括号里的解释本身就是白话，不该被当成新的术语嫌疑词。"""
    assert "马尔可夫链" in jargon_suspects("马尔可夫链预测演示")
    suspects = jargon_suspects("马尔可夫链预测演示（只根据当前状态预测下一步的概率模型）")
    assert suspects == ["马尔可夫链"], suspects


def test_jargon_suspects_cover_dsh_real_scan():
    """DSH 真机扫到的 11 处形态：词缀（链/梯度/矩阵/模型）+ 拉丁缩写。"""
    cases = {
        "马尔可夫链预测演示": "马尔可夫链",
        "参数梯度对比实验": "参数梯度",
        "决策矩阵使用演示": "决策矩阵",
        "成本效益计算模型教学": "成本效益计算模型",
        "用 MCMC 采样对比": "MCMC",
        "n8n流程搭建实操": "n8n",
    }
    for text, term in cases.items():
        assert term in jargon_suspects(text), (text, jargon_suspects(text))
    assert jargon_suspects("辅以气球类比演示CO₂与面筋的关系") == []   # 化学式下标不误报
    # 真机采样补的误报：「体会模型是按概率接词」里的「模型」是日常词，不该报
    assert jargon_suspects("只看上一个词猜下一个词，体会模型是按概率接词而不是完全随机") == []


def test_jargon_joined_the_existing_terminology_item():
    """并入现有 terminology 项：11 项口径 / 权重 / QC_VERSION 一行未动。"""
    qc = compute_qc(MARKDOWN, concepts=CONCEPTS, strategy="马尔可夫链预测演示")
    assert [c["key"] for c in qc["checks"]] == list(CHECK_KEYS)
    assert len(qc["checks"]) == 11 and qc["total_checks"] == 11
    assert QC_VERSION == 3 and qc["version"] == 3
    assert _WEIGHT == {"warn": 6, "fail": 20}


def test_alias_path_still_reports_alias_and_standard_name():
    """老的「正文用了别名」判据不受影响（jargon 为空时走原路径）。"""
    md = MARKDOWN.replace("概率生成机制", "概率生成机制")  # 正文不含别名，这里只断言结构
    check = _terminology()
    assert check["level"] == "ok" and "比对 1 个概念" in check["detail"]
    assert md


# ---------------------------------------------------------------------------
# ② 提示词：术语表带定义 + 正文首次出现给解释；大纲提示词要白话
# ---------------------------------------------------------------------------

def test_terminology_prompt_section_carries_definitions_and_first_use_rule():
    section = terminology_prompt_section(CONCEPTS)
    assert "标准名「概率机器本质」" in section
    assert "模型逐词按概率生成，不查事实库" in section
    assert "第一次出现" in section and "白话解释" in section
    assert "概率生成机制" in section      # 别名照旧保留


def test_lecture_prompt_requires_first_use_explanation_and_bumped_version():
    # 任务 48d 把可操作护栏写进规则 7 / 10 → 顺延到 v9（否则定向重生成会被指纹跳过）
    # 任务 53 公式口径 → v12；任务 55 题型扩展 + 分层作业 → v13
    assert lecture_mod.PROMPT_TEMPLATE_VERSION == "lecture-v14"
    assert "术语第一次出现时必须给读者一个括号解释" in lecture_mod.LECTURE_SYSTEM


def test_planner_prompt_prefers_plain_language_with_inline_explanation():
    system = planner_mod.SYSTEM_PROMPT
    assert "优先用大白话" in system
    assert "同一行里紧跟一句白话解释" in system
    assert "马尔可夫链预测演示" in system      # 反面示例就写在提示词里


# ---------------------------------------------------------------------------
# ③ 抽取范围：大纲字段进语料 + 同一套准入 + 条目带 definition
# ---------------------------------------------------------------------------

def test_collect_corpus_includes_strategy_and_assessment():
    db = SessionLocal()
    try:
        course = _course_with_nodes(db, [{
            "key": "2.1", "title": "自信语气掩盖的认知盲区",
            "strategy": "马尔可夫链预测演示",
            "assessment": "解释为什么概率生成会导致事实偏差"}])
        corpus = concept_service.collect_corpus(db, course)
        unit = corpus["nodes"][0]
        assert unit["strategy"] == "马尔可夫链预测演示"
        assert unit["assessment"].startswith("解释为什么概率")
        assert "teaching_strategy=马尔可夫链预测演示" in concept_service._prompt(course, corpus)
    finally:
        db.close()


def test_outline_field_terms_go_through_the_same_admission():
    """策略/评估里出现的术语和正文一样走准入：跨 2 个知识点 → 概念页。"""
    db = SessionLocal()
    try:
        course = _course_with_nodes(db, [
            {"key": "2.1", "title": "AI 也会一本正经地胡说", "strategy": "马尔可夫链预测演示"},
            {"key": "2.2", "title": "训练数据的裂纹", "assessment": "用马尔可夫链做一次预测"},
        ])
        corpus = concept_service.collect_corpus(db, course)
        dropped: list[dict] = []
        out = concept_service.merge_raw(
            [{"name": "马尔可夫链", "definition": "只根据当前状态预测下一步的概率模型",
              "chapters": ["2.1"]}], corpus, dropped)
        assert [c["name"] for c in out] == ["马尔可夫链"]
        assert out[0]["chapters"] == ["2.1", "2.2"]      # 证据来自大纲字段
        assert out[0]["definition"]
    finally:
        db.close()


def test_single_point_outline_term_lands_in_terminology_with_definition():
    """只出现一次的术语 → meta.terminology，且 definition 从大纲自带的括号解释里补。"""
    db = SessionLocal()
    try:
        course = _course_with_nodes(db, [{
            "key": "2.1", "title": "自信语气掩盖的认知盲区",
            "strategy": "马尔可夫链预测演示（只看上一个词猜下一个词的概率模型）"}])
        corpus = concept_service.collect_corpus(db, course)
        dropped: list[dict] = []
        assert concept_service.merge_raw([{"name": "马尔可夫链"}], corpus, dropped) == []
        entries = concept_service.terminology_entries(dropped)
        assert entries[0]["name"] == "马尔可夫链"
        assert entries[0]["definition"] == "只看上一个词猜下一个词的概率模型"
        concept_service.record_terminology(db, course, entries)
        assert concept_service.terminology_only(course)[0]["definition"].startswith("只看上一个词")
    finally:
        db.close()


def test_concept_definition_reads_both_shapes():
    from app.services.lecture_qc import concept_definition

    assert concept_definition({"name": "X", "body": {"definition": "定义 A"}}) == "定义 A"
    assert concept_definition({"name": "X", "definition": "定义 B"}) == "定义 B"
    assert concept_definition({"name": "X"}) == ""


# ---------------------------------------------------------------------------
# ④ 渲染：笔记「## 术语」= 标准名 + 一句话定义 + 双链
# ---------------------------------------------------------------------------

def test_note_terms_section_lists_definition_and_link():
    db = SessionLocal()
    try:
        course = _course_with_nodes(db, [{
            "key": "2.1", "title": "自信语气掩盖的认知盲区",
            "strategy": "马尔可夫链预测演示", "assessment": "用决策矩阵评分"}])
        node = _first_node(db, course)
        db.add(ContentItem(course_id=course.id, node_id=None, kind="concept",
                           title="马尔可夫链",
                           body={"definition": "只根据当前状态预测下一步的概率模型",
                                 "points": [], "relations": {}, "pitfalls": [], "sources": []},
                           meta={"aliases": [], "chapters": ["2.1"]}))
        db.commit()
        concept_service.record_terminology(db, course, [{
            "name": "决策矩阵", "aliases": [], "chapters": ["2.1"],
            "definition": "把选项按多条标准逐项打分后比较"}])
        lines = kb_service._node_term_lines(db, course, node, None, {"马尔可夫链": "马尔可夫链"})
        text = "\n".join(lines)
        assert "[[马尔可夫链|马尔可夫链]]：只根据当前状态预测下一步的概率模型" in text
        assert "决策矩阵：把选项按多条标准逐项打分后比较（术语表，尚未建概念页）" in text
        section = "\n".join(kb_service._terminology_section([], {}, lines))
        assert "## 术语" in section
    finally:
        db.close()


# ---------------------------------------------------------------------------
# ⑤ 0-LLM 存量处理脚本：只入表「自带解释」的术语，光秃术语只报告
# ---------------------------------------------------------------------------

def test_backfill_script_reports_and_registers_only_explained_terms():
    module = _load_backfill_module()
    db = SessionLocal()
    try:
        course = _course_with_nodes(db, [
            {"key": "2.1", "title": "AI 也会一本正经地胡说",
             "strategy": "马尔可夫链预测演示"},
            {"key": "9.2", "title": "速查决策工具",
             "strategy": "决策矩阵使用演示（把选项按多条标准逐项打分后比较）"},
        ])
        rows = module.scan_course(db, course)
        by_term = {row["term"]: row for row in rows}
        assert by_term["马尔可夫链"]["definition"] == ""            # 光秃术语：不编解释
        assert by_term["决策矩阵"]["definition"] == "把选项按多条标准逐项打分后比较"

        entries = [{"name": row["term"], "aliases": [], "chapters": [row["node_key"]],
                    "definition": row["definition"]} for row in rows if row["definition"]]
        concept_service.record_terminology(db, course, entries)
        left = {row["term"] for row in module.scan_course(db, course)}
        assert "决策矩阵" not in left and "马尔可夫链" in left

        concept_service.record_terminology(db, course, entries)      # 幂等：不重复登记
        assert [row["name"] for row in concept_service.terminology_only(course)] == ["决策矩阵"]
    finally:
        db.close()
