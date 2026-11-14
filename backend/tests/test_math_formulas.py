"""任务 53：公式支持（LaTeX → 前端 KaTeX / 交付件 MathML）。

规格见 HANDOFF「任务 53–55 说明」§A。三层口径：

1. **提示词**：正文公式一律 LaTeX（行内 `$…$`、独立 `$$…$$`，含正反例）；
   `PROMPT_TEMPLATE_VERSION` `lecture-v11 → lecture-v12`（任务 55 再升到 `lecture-v13`；任务 85 再升到 `lecture-v14`；
   只影响以后新生成/重生成的讲义）；
2. **前端**：`remark-math` + `rehype-katex` + `katex`，渲染成 `<span class="katex">`
   （断言脚本 `frontend/scripts/check-math-render.ts` 用同一份插件配置，已接入 verify.ps1）；
3. **交付件 HTML/PDF**：`delivery.markdown_to_html` 把公式转 **MathML**（Chromium 原生渲染）；
   **不许静默**——坏 LaTeX / 引擎缺失时退化成 `<code>` 里的 LaTeX 原文，并在该篇顶部写
   「本篇有 N 处公式未能渲染」。

笔记库（Obsidian）不动：它原生支持 `$…$`。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services import delivery
from app.services import lecture as lecture_mod

MATH_MD = "\n".join([
    "## 二次方程",
    "",
    "行内公式 $x^2+y^2=r^2$ 与求根公式：",
    "",
    "$$x=\\frac{-b\\pm\\sqrt{b^2-4ac}}{2a}$$",
    "",
    "| 量 | 取值 |",
    "| --- | --- |",
    "| 判别式 | $\\Delta \\ge 0$ |",
])

BAD_MD = "\n".join([
    "## 写坏的公式",
    "",
    "这里 $\\frac{1}{$ 少了一个右括号（坏 LaTeX），正文照旧要能出包。",
])


# ---------------------------------------------------------------- A. LaTeX → MathML

def test_engine_available_and_display_attribute():
    """行内 → display=inline；独立 → display=block（Chromium 靠它单独占一行）。"""
    assert delivery.math_engine_available() is True
    inline = delivery.tex_to_mathml("x^2+y^2=r^2")
    assert inline and "<math" in inline and 'display="inline"' in inline
    block = delivery.tex_to_mathml(r"\frac{-b\pm\sqrt{b^2-4ac}}{2a}", display=True)
    assert block and 'display="block"' in block
    assert delivery.tex_to_mathml("") is None


def test_bad_latex_returns_none_without_raising():
    assert delivery.tex_to_mathml(r"\frac{1}{") is None
    assert delivery.tex_to_mathml(r"\begin{unknownenv} x") is None


def test_engine_missing_degrades_explicitly(monkeypatch):
    """引擎缺失（可选依赖没装）也不许静默：原样保留 LaTeX + 顶部计数说明。"""
    monkeypatch.setattr(delivery, "_MATH_ENGINE_OK", False)
    html = delivery.markdown_to_html(MATH_MD)
    assert "<math" not in html
    assert html.count('class="math-src') >= 3
    assert "本篇有 3 处公式未能渲染" in html
    assert "$x^2+y^2=r^2$" in html                      # 原文仍在，老师能看出是什么


# ---------------------------------------------------------------- B. 讲义 → HTML

def test_markdown_to_html_renders_inline_display_and_table_cells():
    """行内、独立、表格单元格三处都转成 MathML；正文里不留 `$` 原文。"""
    html = delivery.markdown_to_html(MATH_MD)
    assert html.count("<math") == 3
    assert 'display="block"' in html and 'display="inline"' in html
    assert "<td><math" in html                          # 表格单元格里的公式也认
    assert "$" not in html
    assert "math-note" not in html                       # 没有失败就不该出现提示条


def test_math_survives_inline_markup_escapes():
    """公式里的 `*` `_` `\\` 不能被当成强调/斜体语法吃掉（先摘公式再转义）。"""
    html = delivery.markdown_to_html("速度 $v = a_1 * t_2$ 与 $\\alpha$ 系数")
    assert html.count("<math") == 2
    assert "<em>" not in html and "<strong>" not in html


def test_formula_inside_code_fence_is_untouched():
    """```代码块里的 `$x$` 是源码，不许渲染成公式（交付件里要能照着抄）。"""
    html = delivery.markdown_to_html("## 源码\n\n```text\n$x^2$\n```\n")
    assert "<math" not in html
    assert "$x^2$" in html


def test_bad_latex_is_reported_on_top_with_original_source():
    """验收 ③：坏 LaTeX → 显式降级文案 + 不抛异常（原 LaTeX 放在 <code> 里）。"""
    html = delivery.markdown_to_html(BAD_MD)
    assert "<math" not in html
    assert 'class="math-src"' in html
    assert "本篇有 1 处公式未能渲染" in html
    assert html.index("math-note") < html.index("写坏的公式")   # 提示在该篇顶部
    assert "坏 LaTeX" in html                                   # 正文没被吞掉


def test_money_and_lone_dollar_stay_plain_text():
    """护栏：金额、单个孤立的 `$` 不是公式（提示词也要求金额写「30 元」）。"""
    html = delivery.markdown_to_html("本课成本 30 元；正文里出现一个孤立的 $ 符号也不该转公式。")
    assert "<math" not in html
    assert "30 元" in html
    assert "$" in html                      # 没配对的 $ 原样保留


def test_html_page_carries_mathml():
    """交付件整页（html_page）里同样是 MathML —— PDF 就是它打印出来的。"""
    page = delivery.html_page("二次方程", delivery.markdown_to_html(MATH_MD))
    assert "<!doctype html>" in page and page.count("<math") == 3
    assert "math[display=" in page                       # 打印 CSS 里有公式排版规则


# ---------------------------------------------------------------- C. 提示词与版本

def test_lecture_prompt_requires_latex_and_version_bumped():
    system = lecture_mod.LECTURE_SYSTEM
    assert "12) 数学 / 物理 / 化学的公式一律写" in system
    assert "$x^2+y^2=r^2$" in system                     # 正例：行内
    assert "$$\\frac{-b\\pm\\sqrt{b^2-4ac}}{2a}$$" in system   # 正例：独立
    assert "用 `^2` 或 `x2` 表示平方" in system            # 反例
    assert "用图片/截图代替公式" in system                  # 反例
    assert lecture_mod.PROMPT_TEMPLATE_VERSION == "lecture-v14"
    assert lecture_mod.QC_RULES_VERSION == "qc-v2"        # 质检 10 项口径不动


def test_latex2mathml_is_a_declared_dependency():
    req = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(encoding="utf-8")
    assert "latex2mathml" in req


# ---------------------------------------------------------------- D. PDF 文本层

@pytest.mark.skipif(delivery.find_edge() is None, reason="本机没有 Microsoft Edge，跳过 PDF 打印断言")
def test_pdf_text_layer_has_prose_not_latex(tmp_path):
    """验收 ②：打印出来的 PDF 里抽得到公式附近的正文，且抽不到原始 `$` 文本。"""
    from pypdf import PdfReader

    html_path = tmp_path / "math.html"
    html_path.write_text(delivery.html_page("二次方程", delivery.markdown_to_html(MATH_MD)),
                         encoding="utf-8", newline="")
    pdf_path = tmp_path / "math.pdf"
    result = delivery.print_pdf(html_path, pdf_path, edge=str(delivery.find_edge()))
    assert result["ok"], result
    text = "\n".join((page.extract_text() or "") for page in PdfReader(str(pdf_path)).pages)
    assert "二次方程" in text
    assert "判别式" in text
    assert "$" not in text                    # 抽到原始 $ 文本 = 公式没转 MathML，算失败
