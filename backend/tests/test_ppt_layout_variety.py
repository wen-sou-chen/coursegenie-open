# -*- coding: utf-8 -*-
"""任务 108 测试：后段 4 页加视觉 + 知识点页多版式（0 LLM，只改版面不改文字）。

钉住四件事：
1. 后段 4 种版式（回顾 / 练习 / 提问 / 结束）**真的画出了版面视觉** —— 卡片 / 图标 / 大字 /
   色块 + 字号层级，不再是「一段横排文字」；封面 / 目标 / 目录也各有版面；
2. 知识点页变体（对比 / 步骤 / 数据）由**内容判据**决定，确定可测，逐页把「为什么」写进
   ``meta.layout_notes``（kind="point-variant"）；
3. 上限护栏：单页要点 ≤ 版式配额、单条字数 ≤ 上限、装饰元素 ≤ ``MAX_DECOR_PER_SLIDE``
   （超上限的不画并记 ``decor-cap``）—— 不为好看堆砌；
4. **一个字都不改**：卡片 / 大字里出现的文字必须能在现有规格里逐字找到。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from pptx import Presentation

from app.services import ppt
from app.services.ppt import (CARD_PREFIX, DECOR_PREFIX, ICON_PREFIX, LAYOUT_COVER,
                              LAYOUT_END, LAYOUT_GOALS, LAYOUT_POINT,
                              LAYOUT_POINT_COMPARE, LAYOUT_POINT_DATA, LAYOUT_POINT_STEPS,
                              LAYOUT_QUESTIONS, LAYOUT_QUIZ, LAYOUT_REVIEW, LAYOUT_TOC,
                              MAX_DECOR_PER_SLIDE, POINT_VARIANTS, _build_prs, _fit_all,
                              _point_slides, _quiz_slides, audit_presentation, compare_sides,
                              deck_palette, point_variant_of)
from app.services.ppt_layout import MAX_TITLE_CHARS, POINT_LIMITS, char_units, point_chars

COURSE = SimpleNamespace(title="光合作用入门", meta={})


# ---------------------------------------------------------------------------
# 夹具 / 小工具
# ---------------------------------------------------------------------------

def _palette() -> dict:
    return deck_palette()


def _spec(layout: str, bullets: list[str], **extra) -> dict:
    spec: dict = {"layout": layout, "title": "一页", "bullets": list(bullets),
                  "notes": "", "media": []}
    spec.update(extra)
    return spec


def _render(specs: list[dict], course=None) -> Presentation:
    return _build_prs(course or COURSE, specs, _palette(), None)


def _named(slide, prefix: str) -> list:
    """按形状名前缀取形状（卡片 / 图标 / 装饰 / 署名都有固定前缀）。"""
    return [sh for sh in slide.shapes if str(sh.name).startswith(prefix)]


def _texts(slide) -> list[str]:
    return [sh.text_frame.text.strip() for sh in slide.shapes
            if getattr(sh, "has_text_frame", False) and sh.text_frame.text.strip()]


def _text_sizes(slide) -> dict[str, set[float]]:
    """每一段文字 → 它用到的字号集合（量「有字号层级」用；多条落在同一个框里就按段落分开）。"""
    out: dict[str, set[float]] = {}
    for sh in slide.shapes:
        if not getattr(sh, "has_text_frame", False):
            continue
        for para in sh.text_frame.paragraphs:
            text = (para.text or "").strip()
            if not text:
                continue
            sizes = {float(run.font.size.pt) for run in para.runs
                     if run.font.size is not None}
            out.setdefault(text, set()).update(sizes)
    return out


def _point_node(title: str) -> SimpleNamespace:
    return SimpleNamespace(node_key="1.1", title=title, objective="", teaching_strategy="")


def _point_items(markdown: str) -> list[SimpleNamespace]:
    return [SimpleNamespace(kind="lecture", body={"markdown": markdown}, meta={})]


# ---------------------------------------------------------------------------
# ① 知识点页版式判据：确定、可测、可复现
# ---------------------------------------------------------------------------

def test_point_variant_criteria_are_deterministic():
    """对比 > 步骤 > 数据 > 默认：同一份内容永远选同一个版式（无随机、无 LLM）。"""
    assert point_variant_of(["甲", "乙"], "确定性输出与创意输出的平衡")[0] == LAYOUT_POINT_COMPARE
    assert point_variant_of(["1. 先核对来源", "2. 再补引用"], "任意标题")[0] == LAYOUT_POINT_STEPS
    assert point_variant_of(["→ 收集素材", "→ 生成初稿"], "任意标题")[0] == LAYOUT_POINT_STEPS
    assert point_variant_of(["准确率提升 23%", "成本降到 0.5 倍"], "任意标题")[0] == LAYOUT_POINT_DATA
    assert point_variant_of(["光反应把光能转成 ATP", "暗反应用 ATP 固定二氧化碳"],
                            "光合作用")[0] == LAYOUT_POINT
    # 只有 1 条命中不算（阈值 ≥2 条，避开巧合）
    assert point_variant_of(["1. 先核对来源", "再补引用"], "任意标题")[0] == LAYOUT_POINT
    assert point_variant_of(["准确率提升 23%", "其余照旧"], "任意标题")[0] == LAYOUT_POINT
    assert point_variant_of([], "任意标题")[0] == LAYOUT_POINT


def test_point_variant_reason_says_why():
    layout, why = point_variant_of(["1. 先做 A", "2. 再做 B"], "任意标题")
    assert layout == LAYOUT_POINT_STEPS
    assert "2/2 条要点以序号或流程词开头" in why and "步骤" in why


def test_point_variant_ignores_citation_digits():
    """引用标注里的段号不许把判据带偏（真库实测：「（来源:《…》第 21 段）」曾被当成数据页）。"""
    bullets = ["（来源:《幻觉治理实战》第 21 段）", "另一条要点"]
    assert point_variant_of(bullets, "任意标题")[0] == LAYOUT_POINT


def test_compare_sides_names_come_from_the_title():
    assert compare_sides("确定性输出与创意输出的平衡") == ("确定性输出", "创意输出")
    assert compare_sides("光合作用与能量转换") == ("", "")   # 不是「A 与 B 的…」式对照


@pytest.mark.parametrize("title,markdown,expected", [
    ("确定性输出与创意输出的平衡", "# 对照\n\n- 甲要点\n- 乙要点\n", LAYOUT_POINT_COMPARE),
    ("治理流程", "# 步骤\n\n- 1. 先核对来源\n- 2. 再补引用\n", LAYOUT_POINT_STEPS),
    ("治理收益", "# 数据\n\n- 准确率提升 23%\n- 返工成本降到 0.5 倍\n", LAYOUT_POINT_DATA),
    ("光合作用", "# 基础\n\n- 光反应把光能转成 ATP\n- 暗反应用 ATP 固定二氧化碳\n", LAYOUT_POINT),
])
def test_point_slides_picks_variant_by_content(title, markdown, expected):
    slides = _point_slides(_point_node(title), _point_items(markdown), media=[],
                           slides_per_point=1)
    assert slides[0]["layout"] == expected
    assert slides[0]["point_variant"]["layout"] == expected
    assert slides[0]["point_variant"]["reason"]             # 为什么选它，必须写下来


def test_point_variant_note_is_recorded_once_from_the_criteria(tmp_path):
    """判据选中的变体要落 meta.layout_notes（kind="point-variant"）；默认版式不刷屏。"""
    slides = _point_slides(_point_node("治理流程"),
                           _point_items("# 步骤\n\n- 1. 先核对来源\n- 2. 再补引用\n"),
                           media=[], slides_per_point=1)
    notes, error = ppt._render_pptx(COURSE, slides, tmp_path / "steps.pptx")
    assert error is None
    hits = [n for n in notes if n["kind"] == "point-variant"]
    assert [(n["slide"], n["layout"]) for n in hits] == [(1, LAYOUT_POINT_STEPS)]
    assert "2/2 条要点以序号或流程词开头" in hits[0]["reason"]

    plain = _point_slides(_point_node("光合作用"),
                          _point_items("# 基础\n\n- 光反应把光能转成 ATP\n- 暗反应用 ATP 固定二氧化碳\n"),
                          media=[], slides_per_point=1)
    other, _ = ppt._render_pptx(COURSE, plain, tmp_path / "plain.pptx")
    assert [n for n in other if n["kind"] == "point-variant"] == []


# ---------------------------------------------------------------------------
# ② 知识点页的三种变体：各自的版面几何
# ---------------------------------------------------------------------------

def test_compare_variant_renders_two_panels_named_from_the_title():
    lines = ["确定性输出要求结论可复现", "创意输出鼓励多种表达", "两者按场景切换"]
    prs = _render([_spec(LAYOUT_POINT_COMPARE, lines, title="确定性输出与创意输出的平衡")])
    slide = prs.slides[0]
    assert len(_named(slide, f"{DECOR_PREFIX}-cmp-panel")) == 2          # 两栏对照
    texts = _texts(slide)
    assert "确定性输出" in texts and "创意输出" in texts                  # 栏名取自标题原文
    for line in lines:
        assert line in texts                                            # 要点原文照搬
    assert slide.placeholders[1].text_frame.text.strip() == ""           # 正文不再走 bullet 列表
    assert audit_presentation(prs) == []


def test_steps_variant_renders_numbered_badges():
    lines = ["1. 先核对来源", "2. 再补引用", "3. 最后交付"]
    prs = _render([_spec(LAYOUT_POINT_STEPS, lines, title="治理流程")])
    slide = prs.slides[0]
    assert len(_named(slide, f"{DECOR_PREFIX}-step-no")) == len(lines)   # 每步一个编号徽章
    assert {"1", "2", "3"} <= set(_texts(slide))
    for line in lines:
        assert line in _texts(slide)
    assert audit_presentation(prs) == []


def test_data_variant_promotes_the_number_to_a_big_stat():
    lines = ["准确率提升 23%", "返工成本降到 0.5 倍"]
    prs = _render([_spec(LAYOUT_POINT_DATA, lines, title="治理收益")])
    slide = prs.slides[0]
    sizes = _text_sizes(slide)
    assert sizes["23%"] == {26.0} and sizes["0.5 倍"] == {26.0}          # 大数字（原文照搬）
    assert len(_named(slide, f"{DECOR_PREFIX}-data-card")) == len(lines)
    for line in lines:
        assert line in sizes                                            # 整句仍在页上做注解
    assert audit_presentation(prs) == []


# ---------------------------------------------------------------------------
# ③ 后段 4 页：每页都有版面视觉（卡片 / 图标 / 大字 / 色块）
# ---------------------------------------------------------------------------

def test_review_page_uses_cards_with_icons_and_verbatim_text():
    lines = ["要点 1：光反应把光能转成 ATP", "误区 2：暗反应不消耗 ATP", "小结 3：两阶段互相依赖"]
    prs = _render([_spec(LAYOUT_REVIEW, lines, title="本章回顾")])
    slide = prs.slides[0]
    assert len(_named(slide, f"{DECOR_PREFIX}-review-card")) == len(lines)   # 一卡一条，不是横排一段
    assert len(_named(slide, f"{DECOR_PREFIX}-review-icon")) == len(lines)
    assert len(_named(slide, ICON_PREFIX)) == len(lines)                    # 每卡一个小图标
    sizes = _text_sizes(slide)
    # 标题 / 卡片有字号层级：内页标题读主题的 title 档（任务 144 起 clean 由 28 涨到 32，
    # 这里改成跟着主题走 —— 断言的是「层级存在」，不是某个写死的磅数）
    assert sizes["本章回顾"] == {float(_palette()["sizes"]["title"])}
    for line in lines:
        assert sizes[line] == {16.0}                                       # 卡片正文原文照搬
    assert slide.placeholders[1].text_frame.text.strip() == ""
    assert audit_presentation(prs) == []


def _quiz_choice(question: str, options: list[str], answer: str = "A",
                 explanation: str = "") -> SimpleNamespace:
    return SimpleNamespace(question=question, options=list(options), answer=answer,
                           explanation=explanation, qtype="choice", meta={})


def test_quiz_page_packs_two_questions_with_option_cards_and_answers_only_in_notes():
    """任务 111：一页 2 题（108 是一页一题）—— 每题自己的题干卡 + 选项卡 + 编号，题间有分隔。"""
    chapter = SimpleNamespace(node_key="1", title="第1章 光合作用")
    quizzes = [
        _quiz_choice("光反应发生在哪里？", ["类囊体薄膜", "叶绿体基质", "细胞质"],
                     "类囊体薄膜", "光反应在类囊体薄膜上进行。"),
        _quiz_choice("暗反应消耗什么？", ["ATP 与 NADPH"], "ATP 与 NADPH"),
    ]
    slides = _quiz_slides(chapter, [], quizzes)
    assert len(slides) == 1                                              # 一页 2 题
    assert slides[0]["quiz_indices"] == [1, 2]
    assert slides[0]["bullets"] == ["光反应发生在哪里？", "A. 类囊体薄膜", "B. 叶绿体基质",
                                    "C. 细胞质", "暗反应消耗什么？", "A. ATP 与 NADPH"]
    assert slides[0]["quiz_pack"]["count"] == 2
    assert len(slides[0]["bullets"]) <= ppt.QUIZ_MAX_POINTS_PER_PAGE      # 页上要点上限
    assert "答案：类囊体薄膜" in slides[0]["notes"]                        # 答案 / 解析只在备注
    assert "答案：ATP 与 NADPH" in slides[0]["notes"]                      # 两题的答案都在备注里

    prs = _render(slides)
    slide = prs.slides[0]
    sizes = _text_sizes(slide)
    assert sizes["光反应发生在哪里？"] == {24.0}                           # 题干大字
    assert sizes["暗反应消耗什么？"] == {24.0}
    assert sizes["A. 类囊体薄膜"] == {15.0}                                # 选项 15 磅（108 口径）
    assert len(_named(slide, f"{DECOR_PREFIX}-quiz-stem")) == 2          # 每题一张题干卡
    assert len(_named(slide, f"{DECOR_PREFIX}-quiz-no")) == 2            # 每题一个编号徽章
    assert len(_named(slide, f"{DECOR_PREFIX}-quiz-option")) == 4        # 3 + 1 张选项卡
    assert len(_named(slide, f"{DECOR_PREFIX}-quiz-split")) == 1         # 题与题之间有分隔
    assert {"1", "2"} <= set(_texts(slide))
    assert len(_named(slide, DECOR_PREFIX)) <= MAX_DECOR_PER_SLIDE       # 装饰上限不许破
    # 学员版正文里不许出现答案区与解析（答案本身若就是某个选项，那是选项原文，不算漏答案）
    texts = " ".join(_texts(slide))
    assert not any(t.startswith("答案：") for t in _texts(slide))
    assert "光反应在类囊体薄膜上进行。" not in texts
    assert audit_presentation(prs) == []


def test_quiz_single_question_page_keeps_the_108_layout():
    """只有一题时仍走 108 的版式（题干卡 1.75 英寸 + 选项两列网格）—— 口径一个字没动。"""
    chapter = SimpleNamespace(node_key="1", title="第1章 光合作用")
    slides = _quiz_slides(chapter, [], [
        _quiz_choice("光反应发生在哪里？", ["类囊体薄膜", "叶绿体基质", "细胞质"], "类囊体薄膜")])
    assert len(slides) == 1 and slides[0]["quiz_indices"] == [1]
    slide = _render(slides).slides[0]
    stems = _named(slide, f"{DECOR_PREFIX}-quiz-stem")
    assert [round(sh.height / 914400, 2) for sh in stems] == [ppt.QUIZ_STEM_H_IN]
    options = _named(slide, f"{DECOR_PREFIX}-quiz-option")
    assert len(options) == 3
    assert len({round(sh.width / 914400, 4) for sh in options}) == 1      # 两列网格：等宽
    assert len({round(sh.top / 914400, 4) for sh in options}) == 2        # 3 个选项排 2 行
    assert len(_named(slide, f"{DECOR_PREFIX}-quiz-split")) == 0          # 一页一题不画分隔线
    assert audit_presentation(_render(slides)) == []


def test_quiz_pack_criterion_is_deterministic_and_checks_every_limit():
    """放得下 / 放不下都是算出来的（题数上限、卡片高度、装饰上限），并钉住 108 的单题口径。"""
    assert ppt.QUIZ_POINTS_PER_QUESTION == 1 + ppt.QUIZ_OPTION_LIMIT == POINT_LIMITS[LAYOUT_QUIZ]
    assert ppt.QUIZ_MAX_POINTS_PER_PAGE == ppt.QUIZ_PER_PAGE * ppt.QUIZ_POINTS_PER_QUESTION
    short = ("题干一句话？", [])
    assert ppt.quiz_pack_fits([short, short])[0] is True
    assert ppt.quiz_pack_fits([short])[0] is True
    assert ppt.quiz_pack_fits([])[0] is False
    too_many = [short] * (ppt.QUIZ_PER_PAGE + 1)
    assert ppt.quiz_pack_fits(too_many) == (False, f"{len(too_many)} 题超过每页上限 "
                                                   f"{ppt.QUIZ_PER_PAGE} 题")
    five = [f"{c}. 选项{c}" for c in "ABCDE"]
    assert ppt.quiz_pack_fits([("题干", five)])[0] is True                # 5 个选项一行放得下
    ok, why = ppt.quiz_pack_fits([("题干一", five), ("题干二", five)])
    assert ok is False and "装饰元素" in why                              # 15 个装饰 > 上限 14
    tall = ("多行题干" + "很长的一句话" * 16, ["A. 选项一", "B. 选项二", "C. 选项三", "D. 选项四"])
    ok, why = ppt.quiz_pack_fits([tall, tall])
    assert ok is False and "英寸" in why                                  # 卡片高度放不下
    long_five = ["A. " + "很长的选项内容" * 4] * 5
    ok, why = ppt.quiz_pack_fits([("题干一", long_five), ("题干二", long_five)])
    assert ok is False and "压小字号" in why                              # 选项太长 ⇒ 不许压字号


def test_quiz_packing_falls_back_when_it_does_not_fit():
    """放不下就回落 1 题：判据给理由，渲染照旧（回落页走 108 的一页一题版式）。"""
    chapter = SimpleNamespace(node_key="1", title="第1章 光合作用")
    long_options = [f"选项内容非常长需要折行显示的第 {i} 条内容，请判断对错并说明理由"
                    for i in range(5)]
    quizzes = [_quiz_choice(f"第 {i} 题 下列说法哪个正确？", long_options) for i in (1, 2)]
    slides = _quiz_slides(chapter, [], quizzes)
    assert [s["quiz_indices"] for s in slides] == [[1], [2]]              # 回落成一页一题
    assert "放不下" in slides[0]["quiz_pack"]["reason"]                    # 理由写清楚
    assert "第 2 题" in slides[0]["quiz_pack"]["reason"]
    prs = _render(slides)
    assert audit_presentation(prs) == []
    assert len(_named(prs.slides[0], f"{DECOR_PREFIX}-quiz-split")) == 0


def test_quiz_packed_page_keeps_every_bullet_and_respects_the_caps():
    """打包页的要点一条都不许被「一列文本」的高度估算砍进备注，上限也不许破。"""
    chapter = SimpleNamespace(node_key="1", title="第1章 光合作用")
    quizzes = [_quiz_choice(f"第 {i} 题 下列说法哪个正确？", [f"{c}. 选项内容 {i}{c}" for c in "ABCD"])
               for i in (1, 2)]
    slides = _quiz_slides(chapter, [], quizzes)
    bullets = list(slides[0]["bullets"])
    assert len(bullets) == 10                                            # 2 题 × (1 题干 + 4 选项)
    notes: list[dict] = []
    _fit_all(slides, _palette(), notes)
    assert slides[0]["bullets"] == bullets                                # 一条都没被移进备注
    assert not [n for n in notes if n.get("kind") == "point"]
    assert len(bullets) <= ppt.QUIZ_MAX_POINTS_PER_PAGE
    for block in ppt._quiz_blocks_of_lines(bullets):
        assert len(block[1]) <= ppt.QUIZ_OPTION_LIMIT                     # 单题选项上限仍在
    assert len(_named(_render(slides).slides[0], DECOR_PREFIX)) <= MAX_DECOR_PER_SLIDE


def test_quiz_pack_reason_lands_in_layout_notes(tmp_path):
    """「这一页几题、为什么」逐页进 meta.layout_notes（kind="quiz-pack"），绝不静默。"""
    chapter = SimpleNamespace(node_key="1", title="第1章 光合作用")
    slides = _quiz_slides(chapter, [], [
        _quiz_choice("光反应发生在哪里？", ["类囊体薄膜", "叶绿体基质", "细胞质"], "类囊体薄膜"),
        _quiz_choice("暗反应消耗什么？", ["ATP 与 NADPH"], "ATP 与 NADPH")])
    notes, error = ppt._render_pptx(COURSE, slides, tmp_path / "quiz.pptx")
    assert error is None
    pack = [n for n in notes if n.get("kind") == "quiz-pack"]
    assert len(pack) == 1 and pack[0]["slide"] == 1 and pack[0]["layout"] == LAYOUT_QUIZ
    assert "本页 2 题" in pack[0]["reason"] and "英寸" in pack[0]["reason"]


def test_quiz_options_are_capped_per_page():
    chapter = SimpleNamespace(node_key="1", title="第1章 光合作用")
    quiz = SimpleNamespace(question="下列哪些说法正确？",
                           options=[f"选项 {i}" for i in range(1, 9)], answer="A",
                           explanation="", qtype="choice", meta={})
    bullets = _quiz_slides(chapter, [], [quiz])[0]["bullets"]
    assert len(bullets) == 1 + ppt.QUIZ_OPTION_LIMIT                     # 题干 + ≤5 个选项
    assert len(bullets) <= POINT_LIMITS[LAYOUT_QUIZ]
    assert len(bullets) <= ppt.QUIZ_MAX_POINTS_PER_PAGE


def test_question_page_is_a_big_centered_ask_not_a_bullet_list():
    lines = ["你怎么判断一条回答是真是假？", "如果模型很自信呢？", "请结合手上的材料举例说明"]
    prs = _render([_spec(LAYOUT_QUESTIONS, lines, title="课堂提问")])
    slide = prs.slides[0]
    sizes = _text_sizes(slide)
    assert sizes[lines[0]] == {28.0} and sizes[lines[1]] == {28.0}       # 居中大字提问
    assert sizes[lines[2]] == {16.0}                                     # 一句提示
    assert len(_named(slide, f"{DECOR_PREFIX}-ask-panel")) == 1
    assert slide.placeholders[1].text_frame.text.strip() == ""
    assert audit_presentation(prs) == []


def test_end_page_is_a_big_closing_page_with_course_name_and_signature(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "ppt_show_footer", False)        # 只看结束页自己画了什么
    spec = _spec(LAYOUT_END, ["回顾：1 第1章 光合作用", "课后完成随堂练习并对照备注中的解析。"],
                 title="谢谢观看")
    prs = _render([spec])
    slide = prs.slides[0]
    sizes = _text_sizes(slide)
    # 大字收尾（与封面同档）：读主题的 cover_title 档（任务 144 起 clean 由 40 涨到 44）
    assert sizes["谢谢观看"] == {float(_palette()["sizes"]["cover_title"])}
    assert sizes["光合作用入门"] == {20.0}                                # 课程名单独一行
    assert any(t.startswith("日期：") or t.startswith("主讲：") for t in _texts(slide))
    # 结束页不许长得像页脚（页脚是「<课程名> · …」）—— 关掉页脚后不允许再有这种行
    assert not any(t.startswith("光合作用入门 · ") for t in _texts(slide))
    assert len(_named(slide, DECOR_PREFIX)) >= 4                         # 分隔线 + 色点
    assert audit_presentation(prs) == []


def test_cover_goal_toc_pages_also_get_a_layout():
    cover = _render([_spec(LAYOUT_COVER, ["受众：初中生"], title="光合作用入门",
                           subtitle="生物 · 初中生")])
    assert len(_named(cover.slides[0], DECOR_PREFIX)) >= 3               # 封面色块
    goals = _render([_spec(LAYOUT_GOALS, ["目标一", "目标二", "目标三"], title="课程目标")])
    slide = goals.slides[0]
    assert len(_named(slide, f"{DECOR_PREFIX}-goal-card")) == 3          # 3 个目标卡
    assert len(_named(slide, ICON_PREFIX)) == 3                          # 每卡一个编号徽章
    toc = _render([_spec(LAYOUT_TOC, ["1 第1章 光合作用", "2 第2章 能量转换"], title="课程目录")])
    slide = toc.slides[0]
    assert len(_named(slide, f"{DECOR_PREFIX}-toc-card")) == 2
    assert {"1", "2"} <= set(_texts(slide))                              # 章节编号大数字
    for prs in (cover, goals, toc):
        assert audit_presentation(prs) == []


def test_back_half_layouts_all_have_page_level_visuals():
    for layout in (LAYOUT_REVIEW, LAYOUT_QUIZ, LAYOUT_QUESTIONS, *POINT_VARIANTS,
                   LAYOUT_GOALS, LAYOUT_TOC):
        assert layout in ppt._VISUAL_RENDERERS
    assert LAYOUT_POINT not in POINT_VARIANTS                            # 默认版式仍走左要点 + 右图


# ---------------------------------------------------------------------------
# ④ 上限护栏：不超配额、不堆砌、一个字都不改
# ---------------------------------------------------------------------------

def test_bullet_quota_and_char_limit_still_hold_after_fitting():
    lines = [f"要点 {i}：这一条写得非常非常长，长到必须被版式精简" for i in range(1, 9)]
    specs = [_spec(LAYOUT_REVIEW, lines, title="本章回顾")]
    notes: list[dict] = []
    _fit_all(specs, _palette(), notes)
    kept = specs[0]["bullets"]
    assert 0 < len(kept) <= POINT_LIMITS[LAYOUT_REVIEW]
    assert all(char_units(b) <= point_chars(LAYOUT_REVIEW) for b in kept)
    assert char_units(specs[0]["title"]) <= MAX_TITLE_CHARS
    assert any(n["kind"] == "point" for n in notes)                      # 精简理由进 layout_notes
    assert specs[0]["notes"]                                             # 放不下的要点进备注，不静默丢


def test_no_page_draws_more_decor_than_the_cap():
    """现在最挤的一页也只有 12 个装饰（目标 / 目录 6 卡 × 2）⇒ 离上限 14 还有余量。"""
    specs = [_spec(LAYOUT_GOALS, [f"目标 {i}" for i in range(1, 7)], title="课程目标"),
             _spec(LAYOUT_TOC, [f"{i} 第{i}章 主题" for i in range(1, 7)], title="课程目录"),
             _spec(LAYOUT_REVIEW, [f"要点 {i}" for i in range(1, 6)], title="本章回顾"),
             _spec(LAYOUT_POINT_STEPS, [f"{i}. 第{i}步" for i in range(1, 6)], title="治理流程")]
    prs = _render(specs)
    for slide in prs.slides:
        assert len(_named(slide, DECOR_PREFIX)) <= MAX_DECOR_PER_SLIDE
    assert audit_presentation(prs) == []


def test_decor_cap_skips_the_extra_shapes_and_is_recorded(monkeypatch):
    monkeypatch.setattr(ppt, "MAX_DECOR_PER_SLIDE", 2)                   # 把上限压到 2 便于触发
    notes: list[dict] = []
    prs = _build_prs(COURSE, [_spec(LAYOUT_REVIEW, ["甲", "乙", "丙"], title="本章回顾")],
                     _palette(), None, tier_notes=notes)
    assert len(_named(prs.slides[0], DECOR_PREFIX)) == 2                 # 超上限的不画
    caps = [n for n in notes if n["kind"] == "decor-cap"]
    assert len(caps) == 1 and "上限 2" in caps[0]["reason"]


def test_decor_quota_counts_what_it_skips():
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    decor = ppt._Decor(slide, limit=3)
    for _ in range(5):
        decor.box(0.0, 0.0, 1.0, 1.0, "FFFFFF")
    assert (decor.used, decor.skipped) == (3, 2)


def test_visual_text_is_verbatim_from_the_specs():
    """卡片 / 大字里的每一段文字都必须能在规格里逐字找到（0 LLM、一个字都不改）。"""
    lines = ["要点 1：光反应把光能转成 ATP", "误区 2：暗反应不消耗 ATP"]
    spec = _spec(LAYOUT_REVIEW, lines, title="本章回顾")
    prs = _render([spec])
    allowed = {*lines, spec["title"]}
    for slide in prs.slides:
        for shape in _named(slide, CARD_PREFIX):
            for para in shape.text_frame.paragraphs:
                text = (para.text or "").strip()
                if text:
                    assert text in allowed
