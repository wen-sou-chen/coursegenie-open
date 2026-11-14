"""任务 48d 回归测试：可操作判据的两处口径收敛 + 空来源小节说明。

对应 HANDOFF「任务 48d 说明」C 段验收 6：
① 只有「术语定义型条目」的样本 → 不再因它们判 actionable warn（完全没步骤时护栏②仍 warn）；
② 步骤抽象但表格里有量纲参数 → 不再 warn；
③ 0 引用讲义的「## 来源」小节在交付 md / html / pdf 三处都存在且带那句说明
   （端到端在 ``test_delivery.py::test_zero_citation_lecture_keeps_source_section``）。

三条护栏：
① 以动作动词开头的条目照旧算候选；
② 候选为 0 且表里也找不到参数 → 仍判 warn（不许借此放宽）；
③ 候选里一条带信号的都没有 → 仍判 warn。

协作规则第 7 条：DATABASE_URL / STORAGE_DIR / OBSIDIAN_VAULT_ROOT 由 conftest 统一
指向临时目录，本文件只喂 Markdown / 纯函数，不碰真实库与知识库。
"""
from __future__ import annotations

from app.services import kb as kb_mod
from app.services import lecture as lecture_mod
from app.services import lecture_qc as qc_mod

# ---------------------------------------------------------------------------
# 样例：术语定义型条目（真机 1.2 的形状：名词 + 定义，含动词却不是给人照做的步骤）
# ---------------------------------------------------------------------------

DEFINITION_ONLY = """## 幻觉的成因

### 核心讲解
- **概率机器本质**：AI 生成文本时只按统计概率输出下一个词，不查事实库（来源:《破解AI幻觉》第 33 段）
- **温度参数**：控制模型采样随机性的超参数，高温度可能导致更多错误（来源:《破解AI幻觉》第 71 段）
- **核查成本**：为降低幻觉率而引入事实核查与多模型交叉验证，会推高开发与运行成本（来源:《降幻五法》第 80 段）

### 小结
- 幻觉来自概率机制（来源:《破解AI幻觉》第 33 段）
"""

# 定义条目 + 一条真步骤（动作 + 量纲 + 产出物）—— 真步骤要能把这篇救回 ok
DEFINITION_PLUS_STEP = DEFINITION_ONLY.replace(
    "### 小结",
    "- 当幻觉率超过 5% 时，先把温度锁到 0.2 以下，并把三次输出整理成《对比表》"
    "（来源:《降幻五法》第 75 段）\n\n### 小结")

# 步骤抽象（没数字/条件词），参数全写在表格里 —— 验收 6②
TABLE_CARRIES_PARAMS = """## 温度与幻觉

### 核心讲解
- **温度参数**：控制模型采样随机性的超参数（来源:《破解AI幻觉》第 71 段）
- 按表格里的阈值调整采样参数，并把对比结果输出成《参数记录表》（来源:《破解AI幻觉》第 71 段）

| 场景 | 温度阈值 | 处理 |
| --- | --- | --- |
| 事实问答 | 低于 0.2 | 输出前逐条核对来源 |
| 创意写作 | 0.9 以上 | 输出草稿后人工复核 |

### 小结
- 温度越低越保守（来源:《破解AI幻觉》第 71 段）
"""

# 同样的步骤，但表格里只有纯文字（没有数字 / 条件词）→ 不算判断信号（护栏：不许放宽）
TABLE_WITHOUT_PARAMS = ("""## 温度与幻觉

### 核心讲解
- **温度参数**：控制模型采样随机性的超参数（来源:《破解AI幻觉》第 71 段）
- 按表格里的阈值调整采样参数，并把对比结果输出成《参数记录表》（来源:《破解AI幻觉》第 71 段）

| 场景 | 处理 |
| --- | --- |
| 事实问答 | 输出前逐条核对来源 |
| 创意写作 | 输出草稿后人工复核 |

### 小结
- 温度越低越保守（来源:《破解AI幻觉》第 71 段）
""")

# 术语定义条目里**带着参数**（低于 12℃）—— 术语解释条目不算步骤，救不了这一项
DEFINITION_WITH_PARAM = """## 阈值

### 核心讲解
- **温度阈值**：当温度低于 12℃ 时应先预热设备再投料（来源:《破解AI幻觉》第 71 段）

### 小结
- 温度决定产量（来源:《破解AI幻觉》第 71 段）
"""


def _pre_48d_candidates(markdown: str) -> list[str]:
    """48d 之前的口径：列表项里含动作动词就算步骤候选（用来证明口径真的收了）。"""
    body, _quiz = qc_mod.split_quiz(markdown)
    body = qc_mod._RE_FENCE.sub("", body)
    lines = [m.group(1).strip() for m in qc_mod._RE_LIST_ITEM.finditer(body)
             if m.group(1).strip()]
    return [line for line in lines if any(v in line for v in qc_mod._ACTION_VERB)]


def _check(markdown: str) -> dict:
    return qc_mod._actionable_check(markdown)


# ---------------------------------------------------------------------------
# ① 术语定义型条目不算步骤
# ---------------------------------------------------------------------------

def test_definition_entries_are_no_longer_step_candidates():
    """旧口径下这三条都是「步骤」（含动作动词），新口径一条都不留 —— 口径真的收了。"""
    old = _pre_48d_candidates(DEFINITION_ONLY)
    assert len(old) == 3, old
    assert all(qc_mod._is_definition_entry(line) for line in old), old
    assert qc_mod._step_candidates(DEFINITION_ONLY) == []


def test_definition_only_sample_warns_without_steps():
    """护栏②：候选为 0 且表里也没有参数 → 仍判 warn（但 detail 点名「术语解释」）。"""
    check = _check(DEFINITION_ONLY)
    assert check["level"] == "warn", check
    assert check["steps"] == 0
    assert "没有可照做的步骤" in check["detail"]
    assert "术语解释" in check["detail"]


def test_one_real_step_plus_definitions_is_ok():
    """定义条目 + 1 条真步骤（动作 + 量纲 + 产出物）→ ok：定义条目不参与、也不阻碍。"""
    check = _check(DEFINITION_PLUS_STEP)
    assert check["level"] == "ok", check
    assert check["steps"] == 1, check          # 候选只有那条真步骤


def test_definition_entry_with_parameter_cannot_rescue():
    """定义条目里写着「低于 12℃」也不算步骤（护栏③：一条带信号的候选都没有 → warn）。"""
    old = _pre_48d_candidates(DEFINITION_WITH_PARAM)
    assert len(old) == 1 and qc_mod._has_signal(old[0]) is True, old
    check = _check(DEFINITION_WITH_PARAM)
    assert check["level"] == "warn", check
    assert check["steps"] == 0 and "没有可照做的步骤" in check["detail"]


# ---------------------------------------------------------------------------
# 护栏①：以动作动词开头的条目照旧算候选
# ---------------------------------------------------------------------------

def test_action_verb_entries_stay_candidates():
    """「先配置…（占30%窗口）」不算定义条目；「**模型选择**：选择…」标签含动词也不算。"""
    plain = "先配置法案关键条款（占30%窗口），再让模型逐条引用（来源:《降幻五法》第 51 段）"
    labelled = "**模型选择**：选择多个训练背景不同的模型（来源:《降幻五法》第 65 段）"
    assert qc_mod._is_definition_entry(plain) is False
    assert qc_mod._is_definition_entry(labelled) is False
    md = f"## 交叉验证\n\n### 核心讲解\n- {plain}\n- {labelled}\n"
    assert len(qc_mod._step_candidates(md)) == 2, qc_mod._step_candidates(md)


def test_source_annotation_colon_is_not_a_definition_marker():
    """「…推荐（来源:《X》第 26 段）」不能被当成「名词：定义」（冒号在括号里不算）。"""
    line = "某医疗 AI 把民间偏方作为治疗方案推荐（来源:《降幻五法》第 26 段）"
    assert qc_mod._is_definition_entry(line) is False


# ---------------------------------------------------------------------------
# ② 判断信号也认表格行（带护栏）
# ---------------------------------------------------------------------------

def test_table_rows_count_as_signal():
    """步骤抽象但表格里有「低于 0.2」这类量纲 → 不再 warn（验收 6②）。"""
    check = _check(TABLE_CARRIES_PARAMS)
    assert check["level"] == "ok", check
    assert check["steps"] >= 1, check
    # 拿掉表格就退回 warn：救它的是表格里的参数，不是别的
    no_table = "\n".join(line for line in TABLE_CARRIES_PARAMS.splitlines()
                         if not line.startswith("|"))
    assert _check(no_table)["level"] == "warn"
    assert "判断信号" in _check(no_table)["detail"]


def test_table_without_numbers_is_not_a_signal():
    """表格里只有纯文字、没有数字/条件词 → 不算信号，仍 warn（护栏：不许放宽）。"""
    assert qc_mod._table_rows(TABLE_WITHOUT_PARAMS)          # 表格本身认得出来
    assert not any(qc_mod._has_signal(row)
                   for row in qc_mod._table_rows(TABLE_WITHOUT_PARAMS))
    check = _check(TABLE_WITHOUT_PARAMS)
    assert check["level"] == "warn", check
    assert "判断信号" in check["detail"]


def test_stray_pipe_lines_are_not_tables():
    """散装的 `| … |` 行（没有 `| --- |` 分隔行）不是表格，不贡献信号。"""
    md = "## 随手记\n\n- 调整参数（无直接依据,建议人工确认）\n\n| 随手写的 | 一行 | 98% |\n"
    assert qc_mod._table_rows(md) == []
    assert qc_mod.has_structured_block(md) is False
    assert _check(md)["level"] == "warn"


def test_mermaid_only_body_still_warns_when_no_steps():
    """只有 mermaid、没有可照做步骤 → 仍 warn（护栏②：mermaid 不提供判断信号）。"""
    md = "## 流程\n\n### 核心讲解\n\n```mermaid\nflowchart TD\n  A --> B\n```\n"
    assert qc_mod.has_structured_block(md) is True
    assert qc_mod._table_rows(md) == []
    check = _check(md)
    assert check["level"] == "warn" and check["steps"] == 0, check


def test_table_params_without_any_step_still_says_no_steps():
    """表格里有参数但正文一条可照做的操作都没有 → 仍 warn，detail 说清是「没有步骤」。"""
    md = """## 参数表

### 核心讲解

| 场景 | 温度阈值 |
| --- | --- |
| 事实问答 | 低于 0.2 |

### 小结
- 温度越低越稳妥
"""
    check = _check(md)
    assert check["level"] == "warn" and check["steps"] == 0, check
    assert "表格里有参数" in check["detail"]


# ---------------------------------------------------------------------------
# ③ 0 引用讲义的来源小节说明（渲染层；端到端见 test_delivery.py）
# ---------------------------------------------------------------------------

def test_empty_sources_note_counts_no_basis_marks():
    md = ("## 本讲目标\n\n- 先看成因再选方法（无直接依据,建议人工确认）\n"
          "- 高风险场景先人工复核（无直接依据,建议人工确认）\n")
    note = qc_mod.empty_sources_note(md)
    assert "本讲没有可溯源的素材段落" in note
    assert "正文中 2 处已标注" in note
    assert qc_mod.NO_BASIS_MARK in note
    assert "请人工核对后再用于对外交付" in note
    assert qc_mod.no_basis_count(md) == 2


def test_source_section_renders_note_for_zero_citations():
    """笔记库与交付讲义共用同一份文案：0 条引用时小节留着，且带那句说明。"""
    md = "## 本讲目标\n\n- 先看成因再选方法（无直接依据,建议人工确认）\n"
    assert kb_mod._source_section([], {}, md) == ["", "## 来源", "",
                                                  qc_mod.empty_sources_note(md), ""]
    # 老调用方（不传正文）保持原样：没正文可数就不硬造小节
    assert kb_mod._source_section([], {}) == []
    # 有引用时口径不变（仍走条目列表）
    section = kb_mod._source_section([{"material_name": "光合作用导论", "paragraph": 3}], {})
    assert "## 来源" in section and any("光合作用导论" in line for line in section)


# ---------------------------------------------------------------------------
# 提示词侧的「可操作护栏」必须真的进了 system prompt（事前说清，不是事后抓）
# ---------------------------------------------------------------------------

def test_prompt_carries_actionable_guard_and_version():
    system = lecture_mod.LECTURE_SYSTEM
    for word in ("必须至少保留", "术语解释条目不算步骤", "动作动词 + 判断信号/参数",
                 "产出物", "参数、阈值、判定条件优先写进表格"):
        assert word in system, word
    assert lecture_mod.PROMPT_TEMPLATE_VERSION == "lecture-v14"
    assert lecture_mod.QC_RULES_VERSION == "qc-v2"      # 10 项口径与权重不动
