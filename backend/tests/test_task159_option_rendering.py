"""任务 159：随堂练习正确项渲染 —— 正确项留在列表里、文本层不留裸 ✓、数值区间用短横线。"""
from __future__ import annotations

import re

import pytest

from app.services import audience, delivery


QUIZ_MD = """#### 第 1 题 · 选择题（难度：易，约 3 分钟）

酵母发酵的最适温度是多少？

-  20~25℃
✓  25~30℃
-  30~35℃
-  35~40℃

**答案**：25~30℃
"""


def _items(html_text: str) -> list[tuple[str, str]]:
    """取 (属性, 可见文本) 列表；正文里没有行内标签时需要剥掉的标签。"""
    return [(attrs, re.sub(r"<[^>]+>", "", body))
            for attrs, body in re.findall(r"<li([^>]*)>(.*?)</li>", html_text, re.S)]


def test_correct_option_stays_in_the_same_list_and_is_marked_once():
    items = _items(delivery.markdown_to_html(QUIZ_MD))
    assert len(items) == 4
    assert [bool(re.search(r'class="correct"', attrs)) for attrs, _ in items] == [
        False, True, False, False]
    assert [text for _, text in items] == ["20–25℃", "25–30℃", "30–35℃", "35–40℃"]


def test_visible_text_has_no_bare_check_mark():
    body = delivery.markdown_to_html(QUIZ_MD)
    page = delivery.html_page("随堂练习", body)
    for where, html_text in (("正文", body), ("整页", page)):
        text = "".join(html_text.split(">"))
        for mark in ("✓", "✔", "√"):
            assert mark not in text, f"{where}文本层里还留着 {mark}"


def test_correct_style_is_defined_in_the_print_stylesheet():
    assert "li.correct { font-weight: 600; color: #0b6b3a; }" in delivery.PRINT_CSS
    assert 'li.correct::before { content: "";' in delivery.PRINT_CSS
    assert "border-right: 2pt solid currentColor" in delivery.PRINT_CSS
    assert "✓" not in delivery.PRINT_CSS


def test_number_ranges_become_en_dashes_but_other_tildes_stay():
    assert delivery._inline_html("20~25℃ / 25 ~ 30℃") == "20–25℃ / 25–30℃"
    assert delivery._inline_html("约~3 天，a~b") == "约~3 天，a~b"


def test_student_view_keeps_four_plain_options():
    student_md = audience.student_markdown(QUIZ_MD)
    assert "✓" not in student_md
    html_text = delivery.markdown_to_html(student_md)
    items = _items(html_text)
    assert len(items) == 4
    assert all('class="correct"' not in attrs for attrs, _ in items)
    assert [text for _, text in items] == ["20–25℃", "25–30℃", "30–35℃", "35–40℃"]


def test_indented_correct_option_keeps_list_balance():
    html_text = delivery.markdown_to_html("- 甲\n  ✓ 乙\n")
    assert '<li class="correct">乙</li>' in html_text
    assert html_text.count("<ul>") == html_text.count("</ul>")


def test_unrelated_markdown_renders_byte_for_byte_as_before():
    markdown = "# 标题\n\n正文 30 元与 **粗体**。\n\n- 甲\n- 乙\n"
    assert delivery.markdown_to_html(markdown) == (
        "<h1>标题</h1>\n"
        "<p>正文 30 元与 <strong>粗体</strong>。</p>\n"
        "<ul>\n"
        "<li>甲</li>\n"
        "<li>乙</li>\n"
        "</ul>"
    )


def test_pdf_text_layer_copies_ranges_with_en_dashes(tmp_path):
    edge = delivery.find_edge()
    if edge is None:
        pytest.skip("本机没有 Microsoft Edge，跳过 PDF 打印断言")
    from pypdf import PdfReader

    page = delivery.html_page("随堂练习", delivery.markdown_to_html(QUIZ_MD))
    pdf_path = tmp_path / "quiz.pdf"
    result = delivery.print_document(page, pdf_path, edge=edge, source_text=page)
    assert result["ok"], result
    text = "".join((page.extract_text() or "") for page in PdfReader(str(pdf_path)).pages)
    compact = re.sub(r"\s+", "", text)
    assert "20–25℃" in compact
    assert "2025℃" not in compact
    assert "✓" not in text
