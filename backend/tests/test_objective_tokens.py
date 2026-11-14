"""任务 48h 回归测试：`objective` 目标对齐判定的分词假象收口。

对应 HANDOFF「任务 48h 说明」B/C 段：
① 滑窗切出的**虚词碎片**不进分母（整条 token 含虚词 / 结构字就丢）；
② 阈值与判定口径**一个字节不许动**（``ratio < 0.5 → warn`` / 0 命中 → fail / 无目标 → ok）；
③ 三条护栏：完全跑题仍 fail、5.1·7.2 不许由 warn 变 ok、滤完为空时 ok + 说明（不崩不 fail）。

真机锚点（AI 课 27 篇，48e 之后）：1.2 / 2.2 / 4.2 由 warn 转 ok，5.1 / 7.2 仍 warn。
协作规则第 7 条：本文件只喂字符串给纯函数，不碰真实库与知识库。
"""
from __future__ import annotations

from app.services import lecture_qc as qc_mod

# 真机 5 篇 warn 的学习目标（原文照抄）
OBJ_1_2 = "理解幻觉对应用落地的严重影响"
OBJ_2_2 = "理解数据质量问题如何诱发幻觉"
OBJ_4_2 = "学习为锚定准备知识库"
OBJ_5_1 = "掌握上下文的高效组织方法"
OBJ_7_2 = "掌握基础实现方案"


def _level(markdown: str, objective: str) -> tuple[str, str]:
    check = qc_mod._objective_check(markdown, objective)
    return check["level"], check["detail"]


# ---------------------------------------------------------------------------
# ① 虚词碎片不进分母
# ---------------------------------------------------------------------------

def test_fake_fragments_drop_out_of_the_objective_tokens():
    """1.2 的滑窗碎片「地的」「的严」必须消失，实词照旧保留。"""
    old = qc_mod._OBJECTIVE_STOP
    tokens = qc_mod._objective_tokens(OBJ_1_2)
    for fake in ("地的", "的严"):
        assert fake not in tokens, (fake, tokens, old)
    for real in ("落地", "严重", "影响"):
        assert real in tokens, (real, tokens)


def test_fragments_are_dropped_for_every_real_warning_objective():
    dropped = {
        OBJ_2_2: ("题如", "如何", "何诱"),
        OBJ_4_2: ("为锚",),
        OBJ_5_1: ("文的", "的高"),
    }
    for objective, fragments in dropped.items():
        tokens = qc_mod._objective_tokens(objective)
        for fragment in fragments:
            assert fragment not in tokens, (objective, fragment, tokens)
    # 7.2「掌握基础实现方案」是**反例**：它的假词（础实 / 现方）里没有虚词或结构字，
    # 按判据**不丢**（这正是它没有被顺手放宽的原因）—— 5.1 / 7.2 仍 warn，见护栏②。
    assert qc_mod._objective_tokens(OBJ_7_2) == ["基础", "础实", "实现", "现方", "方案"]


def test_real_content_characters_are_never_dropped():
    """反过宽证明：实义字（上下 / 关系 / 问题 / 基础 …）一个都不许进虚词表。"""
    for char in ("上", "下", "关", "系", "问", "题", "基", "础", "文", "字", "数", "据"):
        assert char not in qc_mod._OBJECTIVE_FUNC, char
    assert "上下" in qc_mod._objective_tokens(OBJ_5_1)
    assert "下文" in qc_mod._objective_tokens(OBJ_5_1)
    assert "问题" in qc_mod._objective_tokens(OBJ_2_2)
    assert "知识" in qc_mod._objective_tokens(OBJ_4_2)


# ---------------------------------------------------------------------------
# ② 阈值与判定口径没动
# ---------------------------------------------------------------------------

def test_half_hit_ratio_is_still_ok_not_warn():
    """``ratio < 0.5 → warn`` 是严格小于：命中率正好 0.5 照旧 ok（口径不许被顺手改）。"""
    objective = "掌握 RAG 检索"
    tokens = qc_mod._objective_tokens(objective)
    assert tokens == ["检索", "RAG"], tokens
    level, detail = _level("这篇只讲检索流程（检索）。", objective)
    assert level == "ok", detail


def test_no_objective_still_skips():
    for blank in ("", "   ", None):
        level, detail = _level("随便什么正文", blank)
        assert level == "ok" and "没有填学习目标" in detail, detail


# ---------------------------------------------------------------------------
# ③ 三条护栏
# ---------------------------------------------------------------------------

def test_guardrail_off_topic_body_still_fails():
    """护栏①：完全跑题的正文仍然是 fail（0 命中），不是 warn、不是 ok。"""
    objective = "掌握古诗鉴赏的基本方法"
    tokens = qc_mod._objective_tokens(objective)
    assert tokens, tokens           # 有可判词 ⇒ 走正常比例判定
    body = ("### 核心讲解\n数据清洗要去重、补缺失值，再切分成段落并建立索引。\n")
    level, detail = _level(body, objective)
    assert level == "fail", detail
    assert "命中 0/" in detail


def test_guardrail_5_1_stays_warn():
    """护栏②：5.1 的分母全是实词（词汇面真的偏低）⇒ 不许由 warn 变 ok。"""
    body = "### 核心讲解\n上下文越长越贵，把上下文按优先级排序能省一半成本。\n"
    level, detail = _level(body, OBJ_5_1)
    assert level == "warn", detail


def test_guardrail_7_2_stays_warn():
    body = "### 核心讲解\n基础版本先跑通，再考虑把它实现成可复用的流程。\n"
    level, detail = _level(body, OBJ_7_2)
    assert level == "warn", detail


def test_guardrail_all_function_word_objective_is_ok_with_note():
    """护栏③：目标句全是虚词 / 结构字 ⇒ 滤完为空 ⇒ ok + 说明，不崩、不判 fail。"""
    for objective in ("的了着是在和", "是在和与或", "之为把被这那"):
        assert qc_mod._objective_tokens(objective) == []
        level, detail = _level("### 核心讲解\n正文随便写点什么。\n", objective)
        assert level == "ok", (objective, detail)
        assert "没有可判定的关键词" in detail, detail


# ---------------------------------------------------------------------------
# 真机锚点：1.2 / 2.2 / 4.2 转 ok
# ---------------------------------------------------------------------------

def test_real_objectives_that_flipped_to_ok():
    cases = {
        OBJ_1_2: "### 核心讲解\n幻觉造成的严重影响会随应用落地被放大。\n",
        OBJ_2_2: "### 核心讲解\n训练数据的质量问题会诱发幻觉，杂质与盲区都是诱因。\n",
        OBJ_4_2: "### 核心讲解\n先为锚定准备好知识库，把文档切分后入库。\n",
    }
    for objective, body in cases.items():
        level, detail = _level(body, objective)
        assert level == "ok", (objective, detail)
