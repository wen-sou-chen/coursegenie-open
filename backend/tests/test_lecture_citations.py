"""任务 48f 回归测试：讲义标注纪律收口（提示词侧）。

对应 HANDOFF「任务 48f 说明」B/C 段：
① 规则 4-A / 4-D 显式点名「### 举例」与操作类小节（操作指南 / 实施步骤 / 操作流程 /
   操作规范 / 关键操作步骤），写明它们不是标注豁免区；
② 规则 4 新增「复述沿用同一条来源」；
③ 禁止只有标签没有内容的空条目 + 规则 4-D 末尾 6 行以内的交稿自查清单；
④ ``PROMPT_TEMPLATE_VERSION`` == ``lecture-v14``（48f → v10；51 → v11；53 公式 → v12；55 题型扩展 + 分层作业 → v13；
   85 使用场景 → v14）。

验收 6① 的反向证明（**没有放水质检**）：一份「### 举例」3 条无标注 + 1 条有标注的
讲义，``unsourced_points`` 照旧逐条算未标来源、``_sources_check`` 照旧 warn 并点名小节。

协作规则第 7 条：本文件只读提示词字符串 + 喂 Markdown 给纯函数，不碰真实库与知识库。
"""
from __future__ import annotations

from app.services import lecture as lecture_mod
from app.services import lecture_qc as qc_mod

PROMPT = lecture_mod.LECTURE_SYSTEM

# 「### 举例」4 条：第 1 条有标注、第 2~4 条没标注（真机 8.2 / 9.2 的形状）
EXAMPLES_3_MISSING = """## 幻觉案例

### 核心讲解
- **幻觉成因**：模型按统计概率生成文本，不查事实库（来源:《破解AI幻觉》第 33 段）

### 举例
- 学员让模型查政策条款，得到一条根本不存在的文件编号（来源:《破解AI幻觉》第 41 段）
- 客服机器人把退货期限说成 90 天，实际只有 7 天
- 医疗问答给出了错误的用药剂量
- 报告里编造了一篇不存在的参考文献

### 小结
- 幻觉会直接造成业务损失（来源:《破解AI幻觉》第 12 段）
"""


def _checklist_block() -> str:
    return PROMPT[PROMPT.index("📋 交稿前逐条数一遍"):PROMPT.index("   E. 表格与图示")]


def test_prompt_names_examples_and_operation_sections():
    """点名清单补全（48e 的 59 条未标注里 47 条落在这些小节）。"""
    for name in ("### 举例", "操作指南", "实施步骤", "操作流程", "操作规范", "关键操作步骤"):
        assert name in PROMPT, name
    assert "不是标注豁免区" in PROMPT
    # 点名落在规则 4 内部，不是甩在系统提示词末尾
    assert PROMPT.index("4) 来源标注") < PROMPT.index("### 举例") < PROMPT.index("6) 提取优先级")


def test_prompt_reuses_the_same_source_when_restating():
    assert "复述" in PROMPT
    assert "沿用同一条来源" in PROMPT
    assert "不许因为「上面已经标过」就留空" in PROMPT
    assert "也不许为复述另编一条" in PROMPT


def test_prompt_forbids_label_only_empty_entries():
    assert "空条目禁令" in PROMPT
    assert "只有标签、后面没有任何内容" in PROMPT
    assert "删掉整条" in PROMPT


def test_checklist_sits_at_end_of_rule_4d_and_stays_six_lines():
    """规格：**6 行以内**，且挂在规则 4-D 末尾（不是系统提示词末尾）。"""
    block = _checklist_block()
    assert block.rstrip("\n").count("\n") <= 5, block
    for mark in ("①", "②", "③", "④", "⑤", "⑥"):
        assert mark in block, mark
    assert "来源" in block and "举例" in block and "mermaid" in block
    assert "动作 + 带量纲信号 + 产出物" in block
    assert PROMPT.index("D. 最容易漏的五处") < PROMPT.index("📋 交稿前逐条数一遍")
    # 空条目禁令在 4-D 里（不是只写在自查清单里）
    assert PROMPT.index("空条目禁令") < PROMPT.index("📋 交稿前逐条数一遍")


def test_prompt_template_version_is_lecture_v10():
    assert lecture_mod.PROMPT_TEMPLATE_VERSION == "lecture-v14"
    assert lecture_mod.QC_RULES_VERSION == "qc-v2"


def test_example_section_marks_are_still_required():
    """验收 6①：提示词改了，**判定一个字节没动** —— 没标的条目照旧算未标来源。"""
    missing = qc_mod.unsourced_points(EXAMPLES_3_MISSING)
    assert len(missing) == 3, missing
    assert any("退货期限" in line for line in missing)
    assert any("用药剂量" in line for line in missing)
    assert any("参考文献" in line for line in missing)
    assert not any("不存在的文件编号" in line for line in missing)


def test_sources_check_still_warns_and_names_the_example_section():
    sources = qc_mod.parse_sources(EXAMPLES_3_MISSING, "9.9", [])
    check = qc_mod._sources_check(EXAMPLES_3_MISSING, sources, [{"title": "破解AI幻觉"}])
    assert check["level"] == "warn"
    assert "3 条关键点" in check["detail"]
    assert "### 举例" in check["detail"]
    # 标注齐全的样本照旧 ok（不是一律 warn）
    fixed = EXAMPLES_3_MISSING.replace(
        "- 客服机器人把退货期限说成 90 天，实际只有 7 天",
        "- 客服机器人把退货期限说成 90 天，实际只有 7 天（来源:《破解AI幻觉》第 41 段）",
    ).replace(
        "- 医疗问答给出了错误的用药剂量",
        "- 医疗问答给出了错误的用药剂量（来源:《破解AI幻觉》第 41 段）",
    ).replace(
        "- 报告里编造了一篇不存在的参考文献",
        "- 报告里编造了一篇不存在的参考文献（来源:《破解AI幻觉》第 41 段）",
    )
    ok = qc_mod._sources_check(fixed, qc_mod.parse_sources(fixed, "9.9", []),
                               [{"title": "破解AI幻觉"}])
    assert ok["level"] == "ok"
