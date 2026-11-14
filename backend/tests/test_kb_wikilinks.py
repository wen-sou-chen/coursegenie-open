"""源文档自带 Obsidian 双链的降级处理（``kb.plain_wikilinks``）回归测试（任务 48i）。

48g 现场：这门课新增的 4 本素材是**教师自己的 Obsidian 笔记**，原文里带跨笔记双链
（如「→ 参见 [[PE_输出控制策略]]」）。讲义「来源」小节的**摘录**与素材页**正文**
照抄了这些文字，而 ``[[…]]`` 在课程笔记库 / 交付包里指向不存在的文件：

- ``course_link_health`` 报 27 处坏链（2 处在「来源」小节、25 处在素材页）；
- 交付包被一致性自检拒绝（``verify_course_notes`` → ``link`` 不一致）；
- 读者点开是空的，交付 markdown 里还会漏出 Obsidian 语法。

DSH 的紧急修复只在两处降级（``_source_section`` 的摘录、``_material_page`` 的正文），
**不动我们自己生成的素材双链** ``[[素材库/{stem}|{name}]]``。本文件把这个口径固定下来。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.models.course import Course, SourceMaterial
from app.services import kb

INCIDENT_LINKS = ("PE_输出控制策略", "PE_迭代优化")


def _out_dir() -> Path:
    """临时笔记库根目录（铁律 #7：一切测试数据落在系统临时目录）。"""
    return Path(tempfile.mkdtemp(prefix="coursegenie_48i_"))


@pytest.mark.parametrize("raw, expected", [
    ("参见 [[A|B]] 一节", "参见 B 一节"),
    ("参见 [[A]] 一节", "参见 A 一节"),
    ("![[图-x.svg]]", "图-x.svg"),
    ("（→ 参见 [[PE_输出控制策略]]）", "（→ 参见 PE_输出控制策略）"),
    ("带路径 [[素材库/03-X|标题]]", "带路径 标题"),
    ("普通文本，没有任何链接。", "普通文本，没有任何链接。"),
    ("", ""),
])
def test_plain_wikilinks_four_kinds(raw, expected):
    """① 四类输入：``[[A|B]]``→``B``、``[[A]]``→``A``、``![[A]]``→``A``、无链文本原样。"""
    assert kb.plain_wikilinks(raw) == expected


def test_plain_wikilinks_accepts_none_and_is_idempotent():
    """① 补：``None`` 当空串；已降级的文本再降级不再变化。"""
    assert kb.plain_wikilinks(None) == ""
    once = kb.plain_wikilinks("参见 [[A|B]]、[[C]]、![[D]]")
    assert once == "参见 B、C、D"
    assert kb.plain_wikilinks(once) == once


def test_source_section_keeps_our_material_links():
    """② 我们自己生成的素材双链 ``[[素材库/…|标题]]`` 不被降级。"""
    sources = [{"material_name": "教师笔记", "material_id": "m1",
                "section": "第一部分", "paragraph": 2,
                "material_quote": "光合作用发生在叶绿体。"}]
    text = "\n".join(kb._source_section(sources, {"m1": "01-教师笔记"}))

    assert "[[素材库/01-教师笔记|教师笔记]]" in text
    assert "第一部分" in text and "第 2 段" in text
    assert "叶绿体" in text


def test_source_section_degrades_wikilinks_inside_quote():
    """③ 摘录（``material_quote``）里源文档自带的双链被降级，可见文字保留。"""
    sources = [{"material_name": "教师笔记", "material_id": "m1", "paragraph": 1,
                "material_quote": "（→ 参见 [[PE_输出控制策略]]）"}]
    text = "\n".join(kb._source_section(sources, {"m1": "01-教师笔记"}))

    assert "[[" in text and "[[素材库/01-教师笔记|教师笔记]]" in text  # 我们的链接仍在
    assert "[[PE_输出控制策略]]" not in text
    assert text.count("[[") == 1
    assert "PE_输出控制策略" in text


def test_source_section_uses_quote_fallback_field():
    """③ 补：旧数据只有 ``quote`` 字段时同样降级。"""
    sources = [{"material_name": "教师笔记", "material_id": "m1",
                "quote": "见 [[PE_迭代优化]]"}]
    text = "\n".join(kb._source_section(sources, {"m1": "01-教师笔记"}))

    assert "[[PE_迭代优化]]" not in text
    assert "PE_迭代优化" in text


def test_material_page_degrades_wikilinks_in_body():
    """④ 素材页正文（``SourceMaterial.text``）里的双链被降级，可见文字保留。"""
    course = Course(title="测试课")
    material = SourceMaterial(
        title="教师笔记", kind="text", meta={},
        text="第一段：参见 [[PE_迭代优化]]。\n\n第二段：普通正文。",
    )
    text = "\n".join(kb._material_page(course, material))

    assert "[[" not in text
    assert "PE_迭代优化" in text                       # 可见文字保留
    assert "[1] 第一段：参见 PE_迭代优化。" in text      # 段落序号口径不变
    assert "[2] 第二段：普通正文。" in text


def test_material_page_keeps_plain_text_untouched():
    """④ 补：没有双链的素材页正文一个字不动（避免误伤）。"""
    course = Course(title="测试课")
    material = SourceMaterial(title="讲义", kind="text", meta={},
                              text="光合作用是植物利用光能合成有机物的过程。")
    text = "\n".join(kb._material_page(course, material))

    assert "光合作用是植物利用光能合成有机物的过程。" in text
    assert "[[" not in text


def _dirty_note() -> str:
    """事故当场的「来源」小节：摘录里照抄了源文档自带的跨笔记双链。"""
    return "\n".join([
        "# 2.1 概率机器的本质", "",
        "## 来源", "",
        f"- [[素材库/01-教师笔记|教师笔记]] · 第 1 段：（→ 参见 [[{INCIDENT_LINKS[0]}]]）"
        f"、[[{INCIDENT_LINKS[1]}]]", "",
    ])


def test_verify_note_links_flags_raw_source_wikilinks():
    """⑤ 未降级时：含源文档双链的「来源」小节被坏链检查判 mismatch（事故复现）。"""
    out = _out_dir()
    (out / "课程" / "测试").mkdir(parents=True)
    (out / "课程" / "测试" / "2.1-概率机器的本质.md").write_text("# 2.1\n", encoding="utf-8")
    (out / "素材库").mkdir()
    (out / "素材库" / "01-教师笔记.md").write_text("# 教师笔记\n", encoding="utf-8")

    bad = kb._verify_note_links(out, "课程/测试/2.1-概率机器的本质.md", _dirty_note(),
                                {"course_id": "c1"})

    assert bad is not None
    assert bad["status"] == "mismatch"
    assert bad["fingerprint"] == "broken"
    assert bad["note"] == "课程/测试/2.1-概率机器的本质.md"
    assert "2 处" in bad["diff"][0]
    for name in INCIDENT_LINKS:
        assert name in bad["diff"][0]


def test_verify_note_links_passes_after_degrading():
    """⑤ 降级后：同一份「来源」小节不再报 mismatch —— 本次事故的直接护栏。"""
    out = _out_dir()
    (out / "课程" / "测试").mkdir(parents=True)
    (out / "课程" / "测试" / "2.1-概率机器的本质.md").write_text("# 2.1\n", encoding="utf-8")
    (out / "素材库").mkdir()
    (out / "素材库" / "01-教师笔记.md").write_text("# 教师笔记\n", encoding="utf-8")

    sources = [{"material_name": "教师笔记", "material_id": "m1", "paragraph": 1,
                "material_quote": "（→ 参见 [[PE_输出控制策略]]）、[[PE_迭代优化]]"}]
    clean = "\n".join(["# 2.1", "", *kb._source_section(sources, {"m1": "01-教师笔记"})])

    assert "[[素材库/01-教师笔记|教师笔记]]" in clean      # 我们的链接仍在
    assert "[[PE_" not in clean
    assert kb._verify_note_links(out, "课程/测试/2.1-概率机器的本质.md", clean,
                                 {"course_id": "c1"}) is None


def test_degraded_source_section_survives_full_export_roundtrip():
    """⑤ 补：真实导出管线（``_source_section`` → 落盘 → 坏链复查）端到端无坏链。"""
    out = _out_dir()
    (out / "课程" / "测试").mkdir(parents=True)
    (out / "素材库").mkdir()
    sources = [{"material_name": "教师笔记", "material_id": "m1", "section": "绪论",
                "paragraph": 3,
                "material_quote": "原始笔记：见 [[PE_输出控制策略]] 与 ![[流程.png]]"}]
    lines = ["# 2.1 概率机器的本质", "", *kb._source_section(sources, {"m1": "01-教师笔记"})]
    rel = "课程/测试/2.1-概率机器的本质.md"
    (out / rel).write_text("\n".join(lines), encoding="utf-8")

    # 素材页同样走 ``_material_page`` 落盘
    page = "\n".join(kb._material_page(
        Course(title="测试课"),
        SourceMaterial(title="教师笔记", kind="text", meta={},
                       text="原始笔记：见 [[PE_输出控制策略]] 与 ![[流程.png]]"),
    ))
    (out / "素材库" / "01-教师笔记.md").write_text(page, encoding="utf-8")

    assert "[[" not in page
    assert kb._verify_note_links(out, rel, "\n".join(lines), {"course_id": "c1"}) is None
