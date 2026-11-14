"""任务 95：交付侧字体族首位必须是 OFL 字体（Noto Sans SC / 思源黑体）。

``Microsoft YaHei``（微软雅黑）是微软授权字体，嵌进要发给客户的 PDF / HTML 有授权风险；
换成 SIL OFL（免费可商用、可嵌入）的 ``Noto Sans SC`` / ``思源黑体``，微软雅黑只留作兜底。

覆盖三条链路 + 一条真加载：① ``delivery.PRINT_CSS``（body / SVG 覆盖）与
``delivery.html_page`` 的 mermaid ``fontFamily``；② ``deck_html._CSS``（HTML 课件）；
③ ``figure_png`` 的字体探测顺序；④ 位图重绘真取到的字重。

判据写的是**顺序**：把任一条链改回「微软雅黑优先」⇒ 本文件必然红。
"""
from __future__ import annotations

import re
import struct
import unicodedata
from pathlib import Path

import pytest

from app.services import deck_html, delivery, figure_png

_OFL = ("Noto Sans SC", "思源黑体")
_MS_FILES = ("msyh.ttc", "msyh.ttf", "simhei.ttf")
_NOTO_VF = Path(r"C:\Windows\Fonts\NotoSansSC-VF.ttf")


def _family_chain(css: str, anchor: str | None = None) -> list[str]:
    """取 ``anchor`` 之后（默认全篇第一条）的 ``font-family`` 声明，切成分族名列表。"""
    start = 0 if anchor is None else css.index(anchor)
    value = re.search(r"font-family\s*:\s*([^;}]+)", css[start:]).group(1)
    parts = (p.replace("!important", "").strip().strip("\"'") for p in value.split(","))
    return [p for p in parts if p]


def _assert_ofl_first(chain: list[str], *, where: str) -> None:
    assert chain[0] == "Noto Sans SC", f"{where} 首位不是 Noto Sans SC：{chain}"
    for family in _OFL:
        assert family in chain, f"{where} 缺 OFL 字体 {family}：{chain}"
    assert chain.index("思源黑体") < chain.index("Microsoft YaHei"), \
        f"{where} 微软雅黑跑到思源黑体前面了：{chain}"
    assert chain[-1] == "sans-serif", f"{where} 兜底不是 sans-serif：{chain}"


def test_print_css_body_and_svg_prefer_ofl():
    """交付 PDF / HTML 的正文字体链与图内文字覆盖链：OFL 优先、雅黑兜底。"""
    _assert_ofl_first(_family_chain(delivery.PRINT_CSS), where="PRINT_CSS body")
    _assert_ofl_first(_family_chain(delivery.PRINT_CSS, "svg, svg text"), where="PRINT_CSS svg")
    assert ".mermaid foreignObject *" in delivery.PRINT_CSS        # 48n 的覆盖范围没变


def test_print_css_mono_and_math_unchanged():
    """§A1：等宽（代码块）与 math 的字体不动；排版口径一个字没改。"""
    assert 'font-family: "Cascadia Mono", Consolas, "Courier New", monospace' in delivery.PRINT_CSS
    assert 'code { font-family: "Cascadia Mono", Consolas, monospace' in delivery.PRINT_CSS
    assert 'math { font-family: "Cambria Math", "Latin Modern Math", serif' in delivery.PRINT_CSS


def test_mermaid_init_prefers_ofl():
    """mermaid 的 ``fontFamily``（foreignObject 里的 HTML 标签吃这一条）也换成 OFL 优先。"""
    html = delivery.html_page("x", "<p>x</p>", mermaid_js="assets/mermaid.min.js")
    assert "fontFamily:'Noto Sans SC, 思源黑体, Microsoft YaHei, SimSun, sans-serif'" in html
    assert "startOnLoad:true" in html and "theme:'neutral'" in html


def test_deck_html_css_prefers_ofl():
    """HTML 课件的 font-family（单文件内联 CSS）。"""
    _assert_ofl_first(_family_chain(deck_html._CSS), where="deck_html._CSS")


def test_figure_png_probe_order_prefers_ofl():
    """位图重绘的字体探测顺序：NotoSansSC → 思源黑体 → msyh → simhei。"""
    candidates = list(figure_png.FONT_CANDIDATES)
    assert candidates[0].endswith("NotoSansSC-VF.ttf"), candidates[:2]
    assert candidates[1].endswith("sourcehansansi.ttf"), candidates[:2]
    first_ms = min(i for i, p in enumerate(candidates) if p.lower().endswith(_MS_FILES))
    assert first_ms == 3, candidates
    assert not any(p.lower().endswith(_MS_FILES) for p in candidates[:first_ms])


@pytest.mark.skipif(not _NOTO_VF.exists(), reason="本机没装 Noto Sans SC，跳过真加载断言")
def test_figure_png_loads_regular_weight():
    """真加载：``NotoSansSC-VF`` 默认实例是 Thin（wght=100），必须钉到 Regular。"""
    figure_png._FONT_CACHE.clear()
    assert figure_png.find_font_path() == str(_NOTO_VF)
    font = figure_png.load_font(24)
    assert font.getname() == ("Noto Sans SC", "Regular")
    assert font.getlength("中文测试") >= 24 * 4          # 真的能量中文（4 个字宽）


# ---------------------------------------------------------------------------
# 文本层：OFL 字体里「兼容区码位」与「正字」同字形 ⇒ Chromium 的 ToUnicode 反查会挑错码位
# （客户在 PDF 里搜不到「二 / 长 / 门」）。``delivery`` 打印后按 _COMPAT_FIX 归一。
# ---------------------------------------------------------------------------


def test_repair_tounicode_cmap_only_touches_destinations():
    """只改目标码位：源码位（CID）哪怕长得像兼容码位也一个字节不许动 —— 动了就串码。"""
    cmap = ("3 beginbfchar\n"
            "<2F06> <4E8C> <0001> <957F>\n"          # src 是兼容码位、dst 已是正字 ⇒ 整行不动
            "<0002> <2ED3>\n"                        # dst 兼容码位（长）⇒ 归一到 957F
            "<0003> <2F06>\n"                        # dst 兼容码位（二）⇒ 归一到 4E8C
            "endbfchar\n"
            "1 beginbfrange\n"
            "<0100> <0102> <4E00>\n"                 # 单值形式：dst 是第 3 个 token
            "<0200> <0201> [<4E01> <2F06>]\n"        # 数组形式：dst 从第 3 个起
            "endbfrange\n")
    fixed, count = delivery.repair_tounicode_cmap(cmap)
    assert count == 3, fixed                              # 两个 bfchar dst + 数组里一个
    assert "<2F06> <4E8C> <0001> <957F>\n" in fixed       # 源码位 / 正字目标一个字没动
    assert "<0002> <957F>" in fixed
    assert "<0003> <4E8C>" in fixed
    assert "<0100> <0102> <4E00>" in fixed                 # bfrange 单值形式：正字目标不动
    assert "<0200> <0201> [<4E01> <4E8C>]" in fixed        # bfrange 数组形式：兼容 → 正字


def test_repair_tounicode_cmap_turns_control_codepoints_into_spaces():
    """思源黑体把 U+0001 当空格 ⇒ 目标码位 U+0001 归一到空格（源码位一个字节不动）。"""
    cmap = ("1 beginbfchar\n"
            "<0001> <0001>\n"                        # dst 是控制符 ⇒ 归一到空格
            "<0002> <0020>\n"                        # dst 已是空格 ⇒ 不动
            "endbfchar\n"
            "1 beginbfrange\n"
            "<0100> <0101> [<0001> <4E00>]\n"        # 数组形式里的控制符也要修
            "endbfrange\n")
    fixed, count = delivery.repair_tounicode_cmap(cmap)
    assert count == 2, fixed
    assert "<0001> <0020>\n" in fixed                # 源码位 <0001> 原样，只有目标动了
    assert "<0002> <0020>" in fixed
    assert "<0100> <0101> [<0020> <4E00>]" in fixed


def test_repair_tounicode_cmap_never_touches_ordinary_characters():
    """普通字符（含全角标点）一个字节不许碰 —— 对全体字符跑 NFKC 会偷偷改客户的文本层。"""
    for ch in "：（）Ⅲ光二":                                   # 全角标点 + 罗马数字 + 汉字 + 兼容区正字
        assert delivery._to_canonical(ch) == ch, ch
    assert delivery._to_canonical("\u2f06") == "\u4e8c"        # 兼容区（康熙部首「二」）才修
    assert delivery._to_canonical("\x01") == " "               # 控制符 → 空格


def test_repair_pdf_text_layer_is_idempotent_and_safe_on_garbage(tmp_path):
    """非 PDF / 坏文件不许抛异常（出包要降级而不是中断），且返回可读原因。"""
    junk = tmp_path / "x.pdf"
    junk.write_bytes(b"not a pdf at all")
    result = delivery.repair_pdf_text_layer(junk)
    assert result["ok"] is False and result["reason"]
    assert junk.read_bytes() == b"not a pdf at all"        # 原文件没被破坏


def _font_glyph_groups(font_path: Path) -> dict[int, list[int]]:
    """字体 cmap（UCS-4 子表）：``gid -> [码位…]``（同一字形的码位归到一组）。"""
    blob = font_path.read_bytes()
    offset = struct.unpack(">I", blob[12:16])[0] if blob[:4] == b"ttcf" else 0
    count = struct.unpack(">H", blob[offset + 4:offset + 6])[0]
    tables = {}
    for i in range(count):
        tag, _sum, start, length = struct.unpack(">4sIII", blob[offset + 12 + 16 * i:offset + 28 + 16 * i])
        tables[tag.decode("latin1")] = (start, length)
    cmap_off, _length = tables["cmap"]
    subtables = struct.unpack(">H", blob[cmap_off + 2:cmap_off + 4])[0]
    best = None
    for i in range(subtables):
        pid, eid, sub = struct.unpack(">HHI", blob[cmap_off + 4 + 8 * i:cmap_off + 12 + 8 * i])
        if struct.unpack(">H", blob[cmap_off + sub:cmap_off + sub + 2])[0] == 12 and (pid, eid) in ((3, 10), (0, 4)):
            best = cmap_off + sub
    groups = struct.unpack(">I", blob[best + 12:best + 16])[0]
    mapping: dict[int, int] = {}
    for g in range(groups):
        start, end, gid = struct.unpack(">III", blob[best + 16 + 12 * g:best + 28 + 12 * g])
        for cp in range(start, end + 1):
            mapping[cp] = gid + (cp - start)
    by_glyph: dict[int, list[int]] = {}
    for cp, gid in mapping.items():
        by_glyph.setdefault(gid, []).append(cp)
    return by_glyph


def _font_compat_pairs(font_path: Path) -> dict[int, int]:
    """在字体文件上重新推导_兼容区_对照表：同一字形 + 兼容码位更小 + NFKC 修不了。"""
    by_glyph = _font_glyph_groups(font_path)
    pairs = {}
    for gid, codepoints in by_glyph.items():
        for cp in codepoints:
            if not delivery._is_compat(chr(cp)):
                continue
            canon = sorted(p for p in codepoints if not delivery._is_compat(chr(p)))
            if not canon or unicodedata.normalize("NFKC", chr(cp)) != chr(cp):
                continue
            if cp < canon[0]:
                pairs[cp] = canon[0]
    return pairs


def _font_control_pairs(font_path: Path) -> dict[int, int]:
    """在字体文件上重新推导_控制符_对照表：同一字形 + 控制符码位更小 ⇒ 需要一条映射。

    思源黑体把 U+0000–U+001F（与 U+00A0）都映到空格字形（gid 1），Chromium 的 ToUnicode
    反查挑码位最小的那个 ⇒ 空格在文本层里变成 U+0001。
    """
    by_glyph = _font_glyph_groups(font_path)
    pairs = {}
    for codepoints in by_glyph.values():
        controls = sorted(cp for cp in codepoints if 0x01 <= cp <= 0x1F or cp == 0x7F)
        canon = sorted(cp for cp in codepoints if not (cp <= 0x1F or cp == 0x7F))
        if not controls or not canon:
            continue
        for cp in controls:
            pairs[cp] = canon[0]
    return pairs


@pytest.mark.skipif(not _NOTO_VF.exists(), reason="本机没装 Noto Sans SC，跳过对照表复算")
def test_compat_fix_table_matches_font_cmaps():
    """``_COMPAT_FIX`` 必须**完整**覆盖交付字体里所有「同字形且兼容码位更小」的对，且值一致。

    这条测试就是那张表的出处证明：表不是手写的，是从字体 cmap 推出来的。
    """
    fonts = [_NOTO_VF, Path(r"C:\Windows\Fonts\sourcehansansi.ttf"),
             *(path for path, _weight in delivery.print_font_paths())]
    for font in fonts:
        if not font.exists():
            continue
        for compat_cp, canonical_cp in _font_compat_pairs(font).items():
            assert delivery._COMPAT_FIX.get(compat_cp) == canonical_cp, (
                f"{font.name} 的 U+{compat_cp:04X} 应映射到 U+{canonical_cp:04X}")


def test_control_fix_table_matches_font_cmaps():
    """``_CONTROL_FIX`` 从「打印用的那两个面」的 cmap 复算：U+0001–U+001F → 空格（U+0020）。"""
    fonts = [path for path, _weight in delivery.print_font_paths()]
    for font in fonts:
        if not font.exists():
            pytest.skip(f"本机没有 {font}，跳过对照表复算")
    for font in fonts:
        pairs = _font_control_pairs(font)
        assert pairs, f"{font.name} 里应当有「控制符与空格共用字形」的对"
        for cp, canonical_cp in pairs.items():
            assert delivery._CONTROL_FIX.get(cp) == canonical_cp, (
                f"{font.name} 的 U+{cp:04X} 应映射到 U+{canonical_cp:04X}")


# ---------------------------------------------------------------------------
# 任务 98：**异体字对**的反查错位（户 U+6237 被写成 戶 U+6236）—— 两个字形**不同**，
# 单纯是 Chromium 的反查挑错了码位，所以 NFKC / `_COMPAT_FIX` / `_CONTROL_FIX` 都盖不住。
# 首选修法 = 源文本校正（打印入口手上的源 HTML）；`_VARIANT_FIX` 只是拿不到源文本时的兜底。
# ---------------------------------------------------------------------------


def test_variant_fix_table_matches_font_cmaps():
    """``_VARIANT_FIX`` 与实测同源：那一对在交付字体上是「相邻码位 + 相邻字形」。

    实测（DSH 2026-09-25）：`AI-…-20260925-1932.zip` 的讲义 PDF 里 `ToUnicode` 有
    `<3639> <6236>`，而 0x3639 = 13881 **正是字体里 U+6237（户）的字形号** ⇒ 反查把它写成了
    前一个码位 U+6236（戶）。这里在字体上复算这条关系：两个码位都在、字形号相邻且**不同**
    （不是"同字形碰撞" ⇒ 既躲过 NFKC、也躲过 `_COMPAT_FIX`）。
    """
    assert delivery._VARIANT_FIX == {0x6236: 0x6237}
    fonts = [path for path, _weight in delivery.print_font_paths()]
    if not all(path.is_file() for path in fonts):
        pytest.skip("本机没有静态 OFL 打印字体，跳过对照表复算")
    for font in fonts:
        groups = _font_glyph_groups(font)
        gid_of = {cp: gid for gid, codepoints in groups.items() for cp in codepoints}
        wrong, right = 0x6236, 0x6237
        assert wrong in gid_of and right in gid_of, f"{font.name} 里缺 U+6236 / U+6237"
        assert gid_of[wrong] != gid_of[right], (
            f"{font.name} 里这两个码位共用一个字形（那该走 _COMPAT_FIX，不是这一类）")
        assert gid_of[right] - gid_of[wrong] == 1, (
            f"{font.name}：这一对不是相邻字形（{gid_of[wrong]} / {gid_of[right]}）")


def test_variant_rule_prefers_source_text():
    """源文本 > 显式对照表 > 不动：源文本里只有 户（U+6237）⇒ 把 戶（U+6236）改回去。"""
    line = "1 beginbfchar\n<3639> <6236>\nendbfchar\n"
    fixed, count = delivery.repair_tounicode_cmap(line, {0x6237})
    assert count == 1 and "<3639> <6237>" in fixed, fixed
    # 源文本里**就有**这个码位（作者真写的这个字）⇒ 不许动
    fixed, count = delivery.repair_tounicode_cmap(line, {0x6236, 0x6237})
    assert count == 0 and "<3639> <6236>" in fixed, fixed
    # 拿不到源文本（None / 空集）⇒ 显式对照表兜底
    for source in (None, set()):
        fixed, count = delivery.repair_tounicode_cmap(line, source)
        assert count == 1 and "<3639> <6237>" in fixed, fixed


def test_variant_rule_is_narrow_and_never_rewrites_ordinary_text():
    """不许乱改：① 非汉字区（标点 / 圆点）一个字节不碰；② 没有同窗口近邻的汉字也不碰。"""
    # ① Chromium 自己画的列表圆点「•」不在源文本里，但不在汉字区 ⇒ 不许被改成「—」
    bullet = "1 beginbfchar\n<01> <2022>\nendbfchar\n"
    fixed, count = delivery.repair_tounicode_cmap(bullet, {0x2014})
    assert count == 0 and "<01> <2022>" in fixed, fixed
    # ② 汉字区里没有同窗口（同一个 256 段）的近邻 ⇒ 不动（源文本里只有 4E00，离 5000 很远）
    far = "1 beginbfchar\n<01> <5000>\nendbfchar\n"
    fixed, count = delivery.repair_tounicode_cmap(far, {0x4E00})
    assert count == 0 and "<01> <5000>" in fixed, fixed


def test_repair_pdf_text_layer_multi_round_and_safe_on_garbage(tmp_path):
    """多趟修复的第一步就得能安全降级：坏文件不抛异常、不改文件、趟数有界。"""
    junk = tmp_path / "y.pdf"
    junk.write_bytes(b"%PDF-1.4 broken")
    result = delivery.repair_pdf_text_layer(junk, source_text="用户")
    assert result["ok"] is False and result["reason"]
    assert 0 <= result["rounds"] <= delivery._TEXT_LAYER_ROUNDS
    assert junk.read_bytes() == b"%PDF-1.4 broken"


@pytest.mark.skipif(delivery.find_edge() is None, reason="本机没有 Microsoft Edge，跳过真打印断言")
@pytest.mark.skipif(delivery.print_font_style() is None,
                    reason="本机没有静态 OFL 字体（sourcehansans.ttf / sourcehansansb.ttf）")
def test_printed_variant_pair_is_searchable(tmp_path, monkeypatch):
    """真打印：**客户在 PDF 里搜「用户」要搜得到**；关掉第三类修复 ⇒ 立刻搜不到（断言有牙）。

    走产品自己的打印路径（`print_document` + 真 Edge + 打印字体注入 + 多趟文本层修复）。
    实测：修好之后「用户」命中 4 次、`戶`（U+6236）0 次；把 `_variant_to_canonical` 关掉
    ⇒「用户」0 次、`戶` 9 次 —— 正是 DSH 在交付包上量到的那个现象（所以这条断言不是摆设）。
    """
    from pypdf import PdfReader

    markdown = ("## 用户与账户\n\n**用户**在提交账号信息时，系统会核对账户归属。\n\n"
                "- 用户侧：户名与账号一致\n- 平台侧：户口所在地留档\n")
    page = delivery.html_page("用户与账户", delivery.markdown_to_html(markdown), subtitle="任务 98")

    def printed_text(path) -> str:
        result = delivery.print_document(page, path, edge=str(delivery.find_edge()),
                                         source_text=page)
        assert result["ok"], result
        return "".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)

    text = printed_text(tmp_path / "用户.pdf")
    assert text.count("用户") >= 4, f"搜不到「用户」：{text[:120]!r}"
    assert "\u6236" not in text, f"文本层里还有 戶（客户搜「用户」会搜不到）：{text[:120]!r}"

    monkeypatch.setattr(delivery, "_variant_to_canonical", lambda ch, source_cps=None: ch)
    broken = printed_text(tmp_path / "用户-off.pdf")
    assert "用户" not in broken and "\u6236" in broken, (
        f"第三类修复关掉后应当复现「户→戶」：{broken[:120]!r}")
