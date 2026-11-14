"""任务 48b：讲义「表格 / Mermaid」的结构硬要求 + 质检判据 + 0-LLM 确定性兜底。

背景（DSH 实测，不用再复现）：任务 48 把 27 篇重生成后平均分 79.3 → 92.9、
substance 27/27 全 ok，但 **16 篇既没有 Markdown 表格也没有 ```mermaid** ——
根因是 47b/47c 的来源标注纪律让表格变「贵」（规则 4-A 曾要求每行都就地标注），
模型改用纯列表省事。本文件守护三件事：

1. **质检可测量**：正文两样都没有 → ``figures`` 判 warn（并入现有项，不加第 11 项）；
2. **提示词**：结构硬要求写在规则 7 里、表格标注减负写在规则 4-E 里，版本升到 v7；
3. **0-LLM 兜底**：``lecture.ensure_structured_block`` 用节点已有 chart 数据回填表格
   （表下统一一行来源）或按大纲要点画 flowchart，且**幂等**。

协作规则第 7 条：DATABASE_URL / STORAGE_DIR / OBSIDIAN_VAULT_ROOT 由 conftest 统一
指向临时目录，本文件不碰真实知识库。
"""
from __future__ import annotations

import json
import time

from app.db.session import SessionLocal
from app.models.course import ContentItem, Outline, OutlineNode
from app.services import lecture as lecture_mod
from app.services.lecture import ensure_structured_block
from app.services.lecture_qc import (CHECK_KEYS, QC_VERSION, NO_BASIS_MARK, compute_qc,
                                     has_structured_block, last_citation, no_basis_count,
                                     parse_sources, unsourced_points)
from app.services.llm import MockLLMClient

# ---------------------------------------------------------------------------
# 样本：一篇「有概念图、但既没有表格也没有 mermaid」的讲义（任务 48 的真实形态）
# ---------------------------------------------------------------------------

NO_STRUCTURE_MD = """## 概率机器的本质

### 引入
- 大模型逐词预测，只顾句子顺不顺、不管事实真不真（来源: 破解AI幻觉 #12）

### 核心讲解
- **技术本质**：模型按概率挑下一个词，而不是查事实库（来源: 破解AI幻觉 #33）
- **错误累积**：生成到 200 词以后，事实准确率从 90% 掉到 50%（来源: 破解AI幻觉 #18）

![一图看懂](diagram://d1)

### 小结
- 概率生成机制是幻觉的根源（来源: 破解AI幻觉 #33）
"""

CHART_TABLE_OPTION = {
    "id": "c1",
    "kind": "chart_table",
    "title": "错误累积效应",
    "data": {"headers": ["生成长度", "事实准确率"], "rows": [["1-100 词", "90%"], ["201-300 词", "50%"]]},
    "markdown_table": ("| 生成长度 | 事实准确率 |\n|---|---|\n"
                       "| 1-100 词 | 90% |\n| 201-300 词 | 50% |"),
}


def _node(**extra) -> OutlineNode:
    fields = {"outline_id": "o1", "node_key": "2.1", "node_type": "point",
              "title": "概率机器的本质", "objective": "理解逐词预测与幻觉的关系",
              "teaching_strategy": "对比展示正确与幻觉输出",
              "assessment": "课堂小测：辨别真实输出与幻觉输出"}
    fields.update(extra)
    return OutlineNode(**fields)


def _figures(markdown: str, **kw) -> dict:
    checks = {c["key"]: c for c in compute_qc(markdown, **kw)["checks"]}
    return checks["figures"]


# ---------------------------------------------------------------------------
# ① 质检判据：既无表格也无 mermaid → figures warn
# ---------------------------------------------------------------------------

def test_figures_warns_when_no_table_and_no_mermaid():
    """正文有概念图（figures>0）但两样都没有 —— 旧判据是 ok，新判据必须 warn。"""
    check = _figures(NO_STRUCTURE_MD)
    assert check["level"] == "warn"
    assert check["detail"] == "正文里既没有表格也没有 ```mermaid 流程图"
    metrics = compute_qc(NO_STRUCTURE_MD)["metrics"]
    assert metrics["tables"] == 0 and metrics["mermaid"] == 0 and metrics["figures"] > 0


def test_figures_ok_with_table_only_and_with_mermaid_only():
    """两个方向各一例：只有表格、只有 mermaid 都算达标（不是「必须两个都有」）。"""
    with_table = NO_STRUCTURE_MD + "\n| 生成长度 | 准确率 |\n|---|---|\n| 201-300 词 | 50% |\n"
    with_mermaid = NO_STRUCTURE_MD + ("\n```mermaid\nflowchart TD\n  A[词] --> B[事实]\n```\n")
    assert _figures(with_table)["level"] == "ok"
    assert _figures(with_mermaid)["level"] == "ok"


def test_structured_block_criteria_did_not_touch_ten_checks():
    """并入 figures：结构化块判据**自己不新增检查项**（第 11 项 numbers 是任务 106 的，
    见 test_lecture_qc_numbers.py）；权重口径 / 质检版本一律不动。"""
    qc = compute_qc(NO_STRUCTURE_MD)
    assert list(CHECK_KEYS) == ["structure", "fluff", "length", "objective", "terminology",
                                "figures", "quiz", "sources", "numbers", "substance", "actionable"]
    assert len(qc["checks"]) == 11 and qc["total_checks"] == 11
    assert QC_VERSION == 3
    assert lecture_mod.QC_RULES_VERSION == "qc-v2"


# ---------------------------------------------------------------------------
# ② 0-LLM 兜底：回填表格 / 画 flowchart，且幂等
# ---------------------------------------------------------------------------

def test_backfill_table_from_node_chart_and_warn_disappears():
    markdown, info = ensure_structured_block(
        NO_STRUCTURE_MD, node=_node(), charts=[CHART_TABLE_OPTION])
    assert info["applied"] is True and info["source"] == "chart_table"
    assert info["chart_item_id"] == "c1"
    assert "| 生成长度 | 事实准确率 |" in markdown
    # 插在小结之前（不是甩到文末）
    assert markdown.index("生成长度") < markdown.index("### 小结")
    # 表下统一一行来源：沿用正文里就近的真实来源（不编造新出处）
    assert "（来源: 破解AI幻觉 #18）" in markdown.split("| 201-300 词 | 50% |")[1]
    sources = parse_sources(markdown, "2.1", [])
    assert any(s["paragraph"] == 18 for s in sources)
    # 回填后：warn 消失、关键点一条不增（表格行不是列表项）
    assert _figures(markdown, sources=sources)["level"] == "ok"
    assert unsourced_points(markdown) == unsourced_points(NO_STRUCTURE_MD)
    assert has_structured_block(markdown) is True


def test_backfill_is_idempotent():
    """跑两次结果一致：第二次不许再插一块（重生成 / 存量回填都会重复调用）。"""
    once, info1 = ensure_structured_block(
        NO_STRUCTURE_MD, node=_node(), charts=[CHART_TABLE_OPTION])
    twice, info2 = ensure_structured_block(once, node=_node(), charts=[CHART_TABLE_OPTION])
    assert info1["applied"] is True
    assert info2["applied"] is False and info2["source"] == "existing"
    assert twice == once
    assert twice.count("| 生成长度 | 事实准确率 |") == 1


def test_backfill_flowchart_from_outline_when_no_chart():
    """没有可用 chart → 按大纲要点画 flowchart TD（用已有字段，不新增内容）。"""
    markdown, info = ensure_structured_block(NO_STRUCTURE_MD, node=_node(), charts=[])
    assert info["applied"] is True and info["source"] == "flowchart"
    assert "```mermaid" in markdown and "flowchart TD" in markdown
    assert "理解逐词预测与幻觉的关系" in markdown
    assert markdown.index("flowchart TD") < markdown.index("### 小结")
    assert _figures(markdown)["level"] == "ok"
    # 幂等：第二遍不再插
    again, info2 = ensure_structured_block(markdown, node=_node(), charts=[])
    assert again == markdown and info2["applied"] is False


def test_backfill_needs_no_work_when_block_already_present():
    with_table = NO_STRUCTURE_MD + "\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    out, info = ensure_structured_block(with_table, node=_node(), charts=[CHART_TABLE_OPTION])
    assert out == with_table and info["applied"] is False


def test_backfill_uses_no_basis_mark_when_lecture_has_no_citation():
    """正文里一条来源都没有时，表下写「无直接依据」——不编造出处。"""
    bare = "## 概率机器的本质\n\n### 核心讲解\n- 模型按概率挑下一个词\n\n### 小结\n- 收尾\n"
    markdown, _info = ensure_structured_block(bare, node=_node(), charts=[CHART_TABLE_OPTION])
    assert NO_BASIS_MARK in markdown
    assert last_citation(bare) == ""
    assert no_basis_count(markdown) >= 1


def test_backfill_skips_empty_chart_and_uses_next_one():
    """真机里存在全 0 的空数据图（如「适用场景 [0,0,0]」）——不能用它回填。"""
    empty_bar = {"id": "c0", "kind": "chart_bar", "title": "置信度分级标准",
                 "data": {"labels": ["高", "中", "低"],
                          "datasets": [{"label": "适用场景", "data": [0, 0, 0]}]},
                 "markdown_table": ""}
    markdown, info = ensure_structured_block(
        NO_STRUCTURE_MD, node=_node(), charts=[empty_bar, CHART_TABLE_OPTION])
    assert info["source"] == "chart_table" and info["chart_item_id"] == "c1"
    assert "| 生成长度 | 事实准确率 |" in markdown


def test_backfill_renders_bar_and_mindmap_charts():
    bar = {"id": "b1", "kind": "chart_bar", "title": "准确率对比",
           "data": {"labels": ["1-100 词", "201-300 词"],
                    "datasets": [{"label": "事实准确率", "data": [90, 50]}]},
           "markdown_table": ""}
    mindmap = {"id": "m1", "kind": "chart_mindmap", "title": "五大病根",
               "data": {"root": "病根", "branches": [
                   {"title": "训练数据", "subs": ["知识盲区", "杂质污染"]},
                   {"title": "提示词", "subs": ["模糊指令"]}]},
               "markdown_table": ""}
    from_bar, _ = ensure_structured_block(NO_STRUCTURE_MD, node=_node(), charts=[bar])
    assert "| 项目 | 1-100 词 | 201-300 词 |" in from_bar
    assert "| 事实准确率 | 90 | 50 |" in from_bar
    from_map, _ = ensure_structured_block(NO_STRUCTURE_MD, node=_node(), charts=[mindmap])
    assert "| 一级分支 | 要点 |" in from_map
    assert "| 训练数据 | 知识盲区；杂质污染 |" in from_map


# ---------------------------------------------------------------------------
# ③ 落库链路：生成出来的讲义必须带结构块，结论记进 meta
# ---------------------------------------------------------------------------

class _NoStructureLLM(MockLLMClient):
    """讲义返回「既没有表格也没有 mermaid」的正文（其余能力仍走 mock）。"""

    def complete(self, system: str, user: str, *, max_tokens: int = 2000) -> str:
        if "LECTURE_JSON" in system:
            return json.dumps({"markdown": NO_STRUCTURE_MD.replace("![一图看懂](diagram://d1)\n\n", ""),
                               "summary": "概率机器的本质", "keywords": ["幻觉"],
                               "quizzes": []}, ensure_ascii=False)
        return super().complete(system, user, max_tokens=max_tokens)


def _plan_approve(client, course_id: str) -> None:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "AI 幻觉治理", "num_chapters": 1})
    assert resp.status_code == 200, resp.text
    assert client.post("/api/plan/approve", json={
        "course_id": course_id, "approved": True}).status_code == 200


def _add_section(course_id: str, title: str, objective: str) -> str:
    """挂一个知识点节点（mock 大纲只产章，正课里知识点来自真实大纲）。"""
    db = SessionLocal()
    try:
        outline = (db.query(Outline).filter(Outline.course_id == course_id)
                   .order_by(Outline.created_at.desc()).first())
        node = OutlineNode(outline_id=outline.id, node_key="1.1", node_type="point",
                           title=title, objective=objective,
                           teaching_strategy="对比展示正确与幻觉输出",
                           assessment="课堂小测：辨别真实输出与幻觉输出", order_index=1)
        db.add(node)
        db.commit()
        return node.id
    finally:
        db.close()


def test_generation_backfills_structure_block_and_records_meta(client, course_id, monkeypatch):
    _plan_approve(client, course_id)
    node_id = _add_section(course_id, "概率机器的本质", "理解逐词预测与幻觉的关系")
    monkeypatch.setattr(lecture_mod, "build_llm_client", lambda: _NoStructureLLM())
    resp = client.post("/api/lecture/generate", json={
        "course_id": course_id, "node_id": node_id, "tone": "正式"})
    assert resp.status_code == 200, resp.text
    state = None
    for _ in range(200):
        state = client.get(f"/api/content/jobs/{resp.json()['job_id']}").json()
        if state["status"] in ("success", "failed"):
            break
        time.sleep(0.1)
    assert state is not None and state["status"] == "success", state
    item = state["result"]["item"]
    markdown = item["body"]["markdown"]
    assert has_structured_block(markdown) is True, markdown
    info = item["meta"]["structured_block"]
    assert info["applied"] is True
    assert info["source"] in ("chart_table", "chart_bar", "flowchart") or \
        info["source"].startswith("chart_")
    levels = {c["key"]: c["level"] for c in item["meta"]["qc"]["checks"]}
    assert levels["figures"] == "ok"


# ---------------------------------------------------------------------------
# ④ 提示词：硬要求写进规则 7（不是加在末尾）+ 表格标注减负写进规则 4-E + v7
# ---------------------------------------------------------------------------

def test_structured_block_requirement_lives_inside_rule_7():
    system = lecture_mod.LECTURE_SYSTEM
    assert "结构硬要求" in system
    # 必须落在规则 7 里（小节顺序 7 → 8 之间），不是甩在提示词末尾的无效补丁
    assert system.index("7) 小节顺序") < system.index("结构硬要求") < system.index("8) 讲义最后")
    assert "核心讲解」里**必须至少有 1 个 Markdown 表格" in system
    assert "把它们写成纯列表视为不合格" in system
    assert "反面示例" in system


def test_table_labeling_is_cheap_in_rule_4e():
    system = lecture_mod.LECTURE_SYSTEM
    assert "只需在表下统一写一行" in system
    assert "不要每行标" in system
    assert "不需要**标来源" in system
    # 旧口径（每行都标）必须从 system 与 user 两侧都消失，否则表格仍然「贵」
    assert "表格的每一行" not in system
    prompt = lecture_mod._lecture_user_prompt(_node(), "正式")
    assert "表格的每一行" not in prompt
    assert "表格只在表下统一标一行" in prompt


def test_prompt_version_bumped_past_v7():
    """指纹版本必须 +1（57 占 v6、48b 占 v7、任务 58 升到 v8）；
    否则 48c 重生成会按指纹跳过新措辞（任务 48d 的可操作护栏升到 v9；任务 53 公式 → v12；
    任务 55 题型扩展 + 分层作业 → v13）。"""
    assert lecture_mod.PROMPT_TEMPLATE_VERSION == "lecture-v14"
    assert lecture_mod.QC_RULES_VERSION == "qc-v2"
