"""任务 74（并入原 73 B6）：actionable 提示里的例句必须来自**本篇正文**。

用户 / DSH 实测（2026-09-21，真机 :5173 只读）：27 篇讲义里 **17 篇**的 actionable 提示带着同一句
**别的课**（面点课）的例句 —— 根因是 `lecture_qc.py` 把例句硬编码在文案里；
`lecture.py:233/260` 的提示词示例同源（提示词按纪律**只给建议、本次不改**）。

这里钉三件事：
① 摘句来自本篇（去强调、去「（来源:…）」尾巴、超长截断）；
② 摘不出时退回**课程无关**的中性例子；
③ 这段文案**不参与判分** —— 把 `_abstract_example` 换成空串，score 与 10 项 level 逐字不变
（任务 74 §C6「修前后同一批讲义分数与 level 完全一致」的单测版护栏）。
"""

from app.services import lecture_qc as qc_mod

ABSTRACT = """## 引入
幻觉治理是这门课的核心议题。

## 关键点
- 整理幻觉治理清单
- 确认治理流程

## 小结
本节介绍了幻觉治理的要点。

## 随堂练习
1. 幻觉的常见成因有哪些？
"""


def _actionable(markdown: str) -> dict:
    qc = qc_mod.compute_qc(markdown, objective="掌握幻觉治理")
    return next(c for c in qc["checks"] if c["key"] == "actionable")


def test_detail_quotes_sentence_from_this_lecture():
    """例句必须摘自**本篇**正文，不再是别的课的温度阈值。"""
    check = _actionable(ABSTRACT)
    assert check["level"] == "warn", check
    assert "整理幻觉治理清单" in check["detail"], check
    assert "温度" not in check["detail"], check


def test_excerpt_strips_emphasis_and_citation_tail():
    out = qc_mod._abstract_example(["**建立幻觉监测机制**（来源:《某素材》第 3 段）"])
    assert out == "建立幻觉监测机制", out


def test_excerpt_truncates_and_falls_back_to_neutral_example():
    long = "整理" + "幻觉治理要点" * 5
    out = qc_mod._abstract_example([long])
    assert out.endswith("…") and len(out) <= 21, out
    assert qc_mod._abstract_example([]) == qc_mod._ACTIONABLE_NEUTRAL_EXAMPLE
    assert "检查数据质量" in qc_mod._ACTIONABLE_NEUTRAL_EXAMPLE


def test_excerpt_skips_label_only_and_too_short_candidates():
    """表头 / 字段标签（真机 5.1 的「输入 Prompt：」）与过短候选不配当例句，跳到下一条。"""
    out = qc_mod._abstract_example(["**输入 Prompt**：", "短", "整理幻觉治理清单并复核"])
    assert out == "整理幻觉治理清单并复核", out
    # 全是标签 ⇒ 退回课程无关的中性例子
    assert qc_mod._abstract_example(["输入 Prompt：", "输出："]) == qc_mod._ACTIONABLE_NEUTRAL_EXAMPLE


def test_example_text_never_affects_score_or_levels(monkeypatch):
    """文案照旧不参与判分：抹掉例句后 score 与 10 项 level / key 顺序逐字不变。"""
    before = qc_mod.compute_qc(ABSTRACT, objective="掌握幻觉治理")
    monkeypatch.setattr(qc_mod, "_abstract_example", lambda *a, **k: "")
    after = qc_mod.compute_qc(ABSTRACT, objective="掌握幻觉治理")
    assert after["score"] == before["score"]
    assert [c["key"] for c in after["checks"]] == [c["key"] for c in before["checks"]]
    assert [c["level"] for c in after["checks"]] == [c["level"] for c in before["checks"]]
