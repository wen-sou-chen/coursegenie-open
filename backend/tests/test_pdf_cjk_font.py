"""任务 48n 回归测试：交付 PDF 文本层不再出现康熙部首码位。

三条防线各测一条：
① 打印 CSS 把 SVG（图形 / mermaid）里的文字锁到 CJK 字体（只动字体族，不动字号 / 字重 / 行距）；
② mermaid 初始化显式给 fontFamily（HTML 标签走 foreignObject 时也吃这一条）；
③ Edge 打印时关掉浏览器自己的页眉页脚 —— 那是**我们管不到**的字体栈（每页 1 个码位），
   顺带不让客户的 PDF 里出现 ``file:///C:/Users/wen/...`` 这类本机路径。

最后一条是真打印（无头 Edge + pypdf 抽文本层），验收标准：非标准码位 == 0。
协作规则第 7 条：DATABASE_URL / STORAGE_DIR / OBSIDIAN_VAULT_ROOT 由 conftest 指向临时目录。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services import delivery

# 康熙部首 U+2F00–2FDF / CJK 部首补充 U+2E80–2EFF / CJK 兼容汉字 U+F900–FAFF
_CJK_FAMILIES = ("Microsoft YaHei", "微软雅黑", "PingFang SC", "SimSun")
_SVG_SELECTORS = ("svg, svg text, svg tspan, .mermaid", ".mermaid text", ".mermaid tspan",
                  ".mermaid foreignObject")


def _rule_block(css: str) -> str:
    """取出含 ``svg text`` 的那条规则（选择器 + 声明块）。"""
    start = css.index("svg, svg text")
    end = css.index("}", start) + 1
    return css[start:end]


def test_print_css_locks_cjk_font_for_svg_text():
    """规则覆盖全部 SVG 文字（图形 / mermaid），且只改字体族 —— 字号 / 字重 / 行距一个字没动。"""
    block = _rule_block(delivery.PRINT_CSS)
    for selector in _SVG_SELECTORS:
        assert selector in block, selector
    for family in _CJK_FAMILIES:
        assert family in block, family
    assert "!important" in block
    for prop in ("font-size", "font-weight", "line-height", "letter-spacing"):
        assert prop not in block, f"48n 只允许动字体族，不许改 {prop}"


def test_html_page_mermaid_init_declares_cjk_font():
    """mermaid 自己也要拿到 CJK 字体（foreignObject 里的 HTML 标签不吃 CSS 选择器）。"""
    html = delivery.html_page("光合作用", "<p>x</p>", mermaid_js="assets/mermaid.min.js")
    assert "fontFamily:'Noto Sans SC, 思源黑体, Microsoft YaHei, SimSun, sans-serif'" in html
    assert "startOnLoad:true" in html and "theme:'neutral'" in html      # 其余配置不变
    assert 'src="assets/mermaid.min.js"' in html
    assert delivery.html_page("x", "<p>x</p>") == delivery.html_page("x", "<p>x</p>")


def test_print_pdf_disables_browser_header_footer(tmp_path, monkeypatch):
    """浏览器页眉页脚（标题 / 日期 / file:// 路径）必须关掉：它的字体栈我们改不了。"""
    seen: dict = {}

    class _Proc:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(cmd, **_kwargs):
        seen["cmd"] = cmd
        target = next(arg.split("=", 1)[1] for arg in cmd if arg.startswith("--print-to-pdf="))
        Path(target).write_bytes(b"%PDF-1.4 fake")
        return _Proc()

    monkeypatch.setattr(delivery.subprocess, "run", fake_run)
    html = tmp_path / "x.html"
    html.write_text("<html><body>中文</body></html>", encoding="utf-8")
    assert delivery.print_pdf(html, tmp_path / "x.pdf", edge="edge")["ok"] is True
    assert "--no-pdf-header-footer" in seen["cmd"]


@pytest.mark.skipif(delivery.find_edge() is None, reason="本机没有 Microsoft Edge，跳过 PDF 打印断言")
@pytest.mark.skipif(delivery.find_mermaid_js() is None, reason="本机没有 mermaid.min.js，跳过打印断言")
def test_printed_pdf_text_has_no_kangxi_radicals(tmp_path):
    """真打印：mermaid 图 + 内联 SVG 图 + 正文，PDF 文本层非标准码位 0、且没有 file:// 泄漏。"""
    from pypdf import PdfReader

    mermaid_js = delivery.find_mermaid_js()
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="240" height="90">'
           '<rect width="240" height="90" fill="#fff" stroke="#333"/>'
           '<text x="12" y="52" font-size="16" font-family="trebuchet ms, verdana, arial">'
           '光合作用 产生 用</text></svg>')
    markdown = ("## 光合作用的过程\n\n"
                "### 核心讲解\n"
                "- 光反应把光能变成 ATP，暗反应把二氧化碳固定成糖（来源: 素材料 #1）\n\n"
                "```mermaid\nflowchart TD\n  A[阳光] --> B[光反应: 产生 ATP]\n"
                "  B --> C[暗反应: 固定二氧化碳]\n```\n")
    body = delivery.markdown_to_html(markdown) + f"<figure>{svg}</figure>"
    html_path = tmp_path / "lesson.html"
    html_path.write_text(delivery.html_page("光合作用的过程", body, subtitle="48n",
                                            mermaid_js=mermaid_js.as_uri()),
                         encoding="utf-8", newline="")
    pdf_path = tmp_path / "lesson.pdf"
    result = delivery.print_pdf(html_path, pdf_path, edge=str(delivery.find_edge()),
                                budget_ms=9000)
    assert result["ok"], result
    text = "".join((page.extract_text() or "") for page in PdfReader(str(pdf_path)).pages)
    bad = sorted({f"{ch} U+{ord(ch):04X}" for ch in text
                  if 0x2E80 <= ord(ch) <= 0x2FDF or 0xF900 <= ord(ch) <= 0xFAFF})
    assert bad == [], f"PDF 文本层还有非标准码位：{bad}"
    assert "光合作用" in text and "光反应" in text        # 图里 / 正文里的中文都抽得到
    assert "file://" not in text                          # 本机路径不进客户 PDF
