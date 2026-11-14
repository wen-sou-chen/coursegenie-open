"""K-12 备课元数据（任务 51）：学段 / 年级 / 教材版本册次 / 单元 / 课时数。

数据落在 ``course.meta``（**不改表结构**），两种写法都认、**嵌套块优先**：

    {"stage": "小学", "grade": "二年级", "textbook": "部编版",
     "volume": "上册", "unit": "第一单元", "class_hours": 3}   # ① 顶层散写（试点脚本的写法）
    {"k12": {"stage": "小学", ...}}                             # ② 嵌套块

只要拿到 ``stage`` / ``grade`` / ``textbook`` / ``volume`` / ``unit`` 中任意一项，就算
「K-12 课」；否则 :func:`outline_prompt_section` / :func:`lecture_prompt_section` 返回空串 ——
既有职场课（AI 课 / 包子课）的提示词一字不加，生成口径不变。
"""
from __future__ import annotations

META_BLOCK_KEY = "k12"
K12_KEYS = ("stage", "grade", "textbook", "volume", "unit", "class_hours")

# 讲义正文（不含代码块）的建议字数区间：(下限, 上限)；上限 None = 不限。
_WORD_RANGE_DEFAULT = (900, None)
_WORD_RANGE_LOW_PRIMARY = (900, 1500)
_WORD_RANGE_PRIMARY = (1000, 1800)
_WORD_RANGE_JUNIOR = (1200, 2200)
_WORD_RANGE_SENIOR = (1400, 2600)
_LOW_GRADE_MARKS = ("一年级", "二年级", "三年级")

_STAGE_TONE = {
    "小学": "用词浅白、句子短，多用朗读 / 举例 / 图示；不要堆学科专业术语",
    "初中": "语言清楚，可以引入本学科的基本术语，但出现时随手给一句白话解释",
    "高中": "可以用学科术语，但每个术语都要给定义与来由，别只报名词",
}


def _meta_of(source) -> dict:
    """从 Course 对象或 dict 里取 meta（两种都接受，便于纯函数测试）。"""
    if isinstance(source, dict):
        return dict(source)
    return dict(getattr(source, "meta", None) or {})


def k12_meta(source) -> dict:
    """归一化后的 K-12 元数据（只保留非空字段；嵌套块 ``meta["k12"]`` 优先）。"""
    meta = _meta_of(source)
    block = meta.get(META_BLOCK_KEY)
    merged = dict(meta)
    if isinstance(block, dict):
        merged.update(block)
    out: dict = {}
    for key in K12_KEYS:
        value = merged.get(key)
        if value not in (None, "", [], {}):
            out[key] = value
    return out


def is_k12(source) -> bool:
    """是不是 K-12 课：拿到学段 / 年级 / 教材 / 册次 / 单元 任意一项即为真。"""
    k12 = k12_meta(source)
    return any(k12.get(key) for key in ("stage", "grade", "textbook", "volume", "unit"))


def describe(source) -> str:
    """一句话描述（如「小学 二年级 · 部编版 上册 · 第一单元」），缺项自动跳过。"""
    k12 = k12_meta(source)
    bits: list[str] = []
    stage_grade = " ".join(str(k12[k]) for k in ("stage", "grade") if k12.get(k))
    if stage_grade:
        bits.append(stage_grade)
    book = " ".join(str(k12[k]) for k in ("textbook", "volume") if k12.get(k))
    if book:
        bits.append(book)
    if k12.get("unit"):
        bits.append(str(k12["unit"]))
    return " · ".join(bits)


def word_range(source) -> tuple[int, int | None]:
    """本课讲义正文的建议字数区间（下限, 上限）；非 K-12 课沿用既有「不少于 900 字」。"""
    k12 = k12_meta(source)
    if not is_k12(k12):
        return _WORD_RANGE_DEFAULT
    stage = str(k12.get("stage") or "")
    grade = str(k12.get("grade") or "")
    if "小学" in stage or (not stage and grade):
        if any(mark in grade for mark in _LOW_GRADE_MARKS) or not grade:
            return _WORD_RANGE_LOW_PRIMARY
        return _WORD_RANGE_PRIMARY
    if "初中" in stage:
        return _WORD_RANGE_JUNIOR
    if "高中" in stage:
        return _WORD_RANGE_SENIOR
    return _WORD_RANGE_LOW_PRIMARY


def _tone_line(k12: dict) -> str:
    stage = str(k12.get("stage") or "")
    for name, line in _STAGE_TONE.items():
        if name in stage:
            if name == "小学" and any(m in str(k12.get("grade") or "") for m in _LOW_GRADE_MARKS):
                return "小学低年级 —— 用词浅白、句子短，多用朗读 / 举例 / 图示；不出现专业术语堆砌"
            return line
    return "按学段调整用词深浅：低学段浅白短句，高学段可用术语但必须给解释"


def _hours_line(k12: dict) -> str:
    hours = k12.get("class_hours")
    if not hours:
        return ""
    return f"本单元素材写到共 **{hours} 课时** ⇒ 知识点总数按 {hours} 个左右安排（分配到各章）。"


def outline_prompt_section(source) -> str:
    """大纲提示词里的 K-12 段（非 K-12 课返回空串）。"""
    k12 = k12_meta(source)
    if not is_k12(k12):
        return ""
    hours = _hours_line(k12)
    return (
        "\n【K-12 教材口径（本次必须遵守）】\n"
        f"本课信息：{describe(k12)}。\n"
        "- 顶层节点 = **单元（或课文）**：一篇课文 / 一个单元一章（`章节数` 参数优先）；\n"
        "- 知识点的口径 = **一节课（40/45 分钟）**，不是「一个知识点」这种笼统切法"
        + (f"；{hours}" if hours else "；") + "\n"
        "- 每个知识点的三个字段都按**课时**写：\n"
        "  · objective = 本节课的课时目标（可观测、可评价：能用「说出 / 区分 / 朗读 / 复述 / 算出」检验）；\n"
        "  · teaching_strategy = 这节课怎么上（环节 + 时间分配 + 学生活动）；\n"
        "  · assessment = 怎么知道学生会不会（当堂可做的检查：朗读、口头复述、随堂练习…）。\n"
        f"- 学段语气：{_tone_line(k12)}。\n"
    )


def lecture_prompt_section(source) -> str:
    """讲义提示词里的 K-12 段（非 K-12 课返回空串）。"""
    k12 = k12_meta(source)
    if not is_k12(k12):
        return ""
    low, high = word_range(k12)
    limit = f"控制在 **{low}–{high} 字**" if high else f"不少于 {low} 字"
    return (
        "\n【K-12 课时口径（本次必须遵守）】\n"
        f"本课时面向：{describe(k12)}。\n"
        f"- 篇幅：正文（不含代码块）{limit} —— 这是一节课讲得完的量；"
        "超了就砍到本节课的范围，不要写成 5000 字长文。\n"
        f"- 语气：{_tone_line(k12)}。\n"
        "- 取材：例子优先用这个年龄段学生身边的场景；抽象概念先用生活类比，再给术语。"
    )