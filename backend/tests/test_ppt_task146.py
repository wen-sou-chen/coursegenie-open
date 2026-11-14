# -*- coding: utf-8 -*-
"""任务 146 的专项断言：续页后缀唯一、图页必须有内容、练习页有排面。

三个判据都是 **0 LLM + 几何确定**，与 ``ppt.py`` 里的常量一一对应：

- ``suffix_count(title) < 2``：任何页标题不许叠「（续）/（续图）/（接上页）」；
- 有图的页至少 1 条要点、正文 >= ``MEDIA_PAGE_MIN_TEXT_CHARS``（12 字），不许「一图 +
  一行图注」；整页可见文字（标题 + 正文 + 页脚）仍须 >= 30 字，由 146b 的端到端用例核；
- 练习页可见文字 >= ``QUIZ_MIN_TEXT_CHARS``（40 字），无选项简答题要有作答提示。
"""
from __future__ import annotations

from types import SimpleNamespace

from pptx import Presentation

from app.services import ppt, ppt_layout
from app.services.ppt_layout import char_units


def _bitmap(name: str, px: tuple[int, int] = (1000, 400)) -> dict:
    return {"type": "image", "path": name, "px": list(px), "caption": f"图-{name}",
            "legibility": {"min_px": 20.0}}


def test_page_suffix_has_one_source_and_never_stacks():
    base = "概率机器的本质"
    legacy = f"{base}（续）（续图）"
    assert ppt.strip_page_suffix(legacy) == base
    assert ppt.suffix_count(legacy) == 2
    assert ppt.continue_title(legacy) == f"{base}{ppt.CONT_SUFFIX}"
    assert ppt.suffix_count(ppt.continue_title(legacy)) == 1
    assert ppt.figure_page_title(base, "过度治理影响", 2) == "过度治理影响"
    assert ppt.suffix_count(ppt.figure_page_title(base, "过度治理影响", 2)) == 0


def test_point_pages_with_media_always_carry_enough_visible_text():
    node = SimpleNamespace(
        title="概率机器的本质",
        objective="理解模型为什么只顾语言流畅而牺牲事实准确。",
        teaching_strategy="先用一个逐词预测的例子，再让学员复述因果链。",
    )
    media = [_bitmap("a.png"), _bitmap("b.png")]
    slides = ppt._point_slides(node, [], media, slides_per_point=2)

    assert slides
    for page in slides:
        assert ppt.suffix_count(page["title"]) < 2, page["title"]
        if page.get("media"):
            bullets = [str(b) for b in (page.get("bullets") or []) if str(b).strip()]
            visible = sum(len(b) for b in bullets)
            assert bullets, page
            assert visible >= ppt.MEDIA_PAGE_MIN_TEXT_CHARS, page
        assert page["layout"] != ppt.LAYOUT_FIGURE or not page.get("media")


def test_short_text_does_not_trigger_a_split_into_low_information_pages():
    spec = {"layout": ppt.LAYOUT_POINT, "title": "训练数据的裂纹",
            "bullets": ["知识盲区", "杂质污染"],
            "media": [_bitmap("a.png"), _bitmap("b.png"), _bitmap("c.png")]}
    specs = [spec]
    notes: list[dict] = []
    ppt.split_multi_media_slides(specs, notes)

    assert len(specs) == 1
    assert len(specs[0]["media"]) == 1
    assert notes and notes[0]["kind"] == "media-split"
    assert "不拆" in notes[0]["reason"]


def test_open_quiz_page_has_visible_answer_hints_and_keeps_answers_in_notes():
    palette = ppt.deck_palette()
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    stem = "13 为什么AI模型在训练过程中会被优化为生成自信的口吻？"
    assert ppt._render_quiz_single(slide, {}, palette, [stem], ppt._Decor(slide))

    texts = [shape.text_frame.text for shape in slide.shapes if shape.has_text_frame]
    visible = " ".join(texts)
    assert len(visible) >= ppt.QUIZ_MIN_TEXT_CHARS
    assert stem in visible
    assert all(hint in visible for hint in ppt.QUIZ_OPEN_HINTS)
    assert "答案：" not in visible
    assert "解析：" not in visible


def test_packed_open_quiz_page_also_has_visible_answer_hints():
    """一页两道无选项简答题时，提示也必须上页，不能只剩 36 字题干。"""
    palette = ppt.deck_palette()
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    blocks = [("请简述死面不发起的主要诊断方法。", []),
              ("设计一个实验，验证水温对酵母活性的影响。", [])]
    spec = {"quiz_indices": [5, 6]}
    assert ppt.quiz_pack_fits(blocks)[0]
    assert ppt._render_quiz_packed(slide, spec, palette, blocks, ppt._Decor(slide))

    texts = [shape.text_frame.text for shape in slide.shapes if shape.has_text_frame]
    visible = " ".join(texts)
    assert sum(len(stem) for stem, _options in blocks) < ppt.QUIZ_MIN_TEXT_CHARS
    assert len(visible) >= ppt.QUIZ_MIN_TEXT_CHARS
    assert all(hint in visible for hint in ppt.QUIZ_OPEN_HINTS)
    assert "答案：" not in visible
    assert "解析：" not in visible


def test_continued_title_keeps_full_words_and_fits_one_line():
    """任务 146 A1：20 字标题加「（接上页）」后不许被旧 24 字上限截成「…」。"""
    title = ppt.continue_title("提示词与角色陷阱（模糊指令诱发自由发挥）")
    spec = {"title": title, "bullets": ["要点一", "要点二"]}
    notes: list[dict] = []
    ppt_layout.fit_spec(spec, layout=ppt.LAYOUT_POINT, width_in=ppt.TITLE_BOX[2],
                        height_in=ppt.TITLE_BOX[3], size_pt=32, notes=notes, slide_no=1)

    assert spec["title"] == title
    assert "…" not in spec["title"]
    assert char_units(spec["title"]) <= ppt_layout.MAX_TITLE_CHARS
