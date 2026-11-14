"""任务 142：客户可见的两处收口 —— 验收单不许印「PDF True 篇」，降级项不许只写在交付说明里。

来历（DSH 复测 141 实测）：① `01-验收单.md` 里印着「**本次生成 PDF True 篇**」——
`plan["counts"]["pdf_ok"]`（讲义 PDF 计数）被教案 / 学案循环的解包**覆盖成布尔值**；
② 验收单里含「文本层」**0 行**、含「降级」**0 行** ⇒ 降级只写在 `00-交付说明.md` §4，
而客户往往先看验收单。

本文件钉的都是**从产物读出来**的事实（读包内 `01-验收单.md` / `manifest.json` / 目录），
不是函数返回值。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.db.session import SessionLocal
from app.models.course import ContentItem
from app.services import delivery
from tests.test_delivery import _build, _course_with_content, _delivery_dir, _new_course

#: 只有真出 PDF 才谈得上「篇数不是 0」；没有 Edge 的机器上那条会 skip（与 141 的用例同口径）。
_needs_edge = pytest.mark.skipif(delivery.find_edge() is None,
                                 reason="本机没有 Microsoft Edge，跳过「真出 PDF」的断言")

#: 验收单里那句「本次生成 PDF N 篇」。
_pdf_count_re = re.compile(r"本次生成 PDF (\S+) 篇")
#: 客户可见文档里**不该出现**的裸布尔 / None（前后不接字母数字下划线，免得误伤 `NoneType` 之类）。
_bool_word_re = re.compile(r"(?<![A-Za-z0-9_])(True|False|None)(?![A-Za-z0-9_])")


def _build_course(client, title: str, monkeypatch, *, payload: dict | None = None,
                  drop_chart_tables: bool = False):
    """出一包真货，返回（course_id, 出包目录, 出包返回值）。"""
    course_id = _new_course(client, title)
    _course_with_content(client, course_id, monkeypatch)
    if drop_chart_tables:
        # 本用例的测试课自带一条**正常**的既有降级项（数据表已并入讲义正文）⇒
        # 要造「本次无降级项」的真包，就得先把那张表拿掉。
        db = SessionLocal()
        try:
            db.query(ContentItem).filter(ContentItem.course_id == course_id,
                                         ContentItem.kind == "chart_table").delete()
            db.commit()
        finally:
            db.close()
    result = _build(client, course_id, payload or {"pdf": "off"})
    return course_id, _delivery_dir(result["build_id"]), result


def _read(out_dir: Path, name: str) -> str:
    return (out_dir / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# ① 验收单那句 PDF 篇数：真数字，且与 manifest / 包内文件 / 返回值三方对得上
# ---------------------------------------------------------------------------

def test_acceptance_sheet_prints_a_real_pdf_count(client, monkeypatch):
    """「本次生成 PDF N 篇」必须是**真数字** —— N == 包内 `讲义/` 下 PDF 份数 == counts.pdf_ok。

    把 `pdf_ok` 改回布尔值（教案 / 学案解包覆盖）时，这里印出来的是 `True` / `False` ⇒ 必红；
    这条不依赖 Edge（没 Edge 时三个数都合法地是 0）。
    """
    _course_id, out_dir, summary = _build_course(client, "验收单PDF篇数", monkeypatch,
                                                 payload={"pdf": "auto"})
    manifest = json.loads(_read(out_dir, "manifest.json"))
    sheet = _read(out_dir, delivery.ACCEPT_NAME)

    match = _pdf_count_re.search(sheet)
    assert match, "验收单里找不到「本次生成 PDF N 篇」那一行"
    assert match.group(1).isdigit(), f"印出来的不是数字：{match.group(0)}"
    printed = int(match.group(1))
    # 客户能自己数出来的那个数、清单里的那个数、返回给网页端的那个数 —— 三者同一个
    assert printed == len(list((out_dir / "讲义").rglob("*.pdf")))
    assert printed == manifest["counts"]["pdf_ok"]
    assert printed == summary["pdf"]["generated"]
    for value in (manifest["counts"]["pdf_ok"], summary["pdf"]["generated"]):
        assert isinstance(value, int) and not isinstance(value, bool), repr(value)


@_needs_edge
def test_acceptance_sheet_pdf_count_is_the_lecture_count_not_the_package_total(
        client, monkeypatch):
    """口径写明白：这个数是**讲义**的 PDF 份数，不是全包 PDF 总数（教案 / 学案另有两行）。"""
    _course_id, out_dir, _summary = _build_course(client, "验收单PDF口径", monkeypatch,
                                                  payload={"pdf": "auto"})
    sheet = _read(out_dir, delivery.ACCEPT_NAME)
    printed = int(_pdf_count_re.search(sheet).group(1))
    lectures = len(list((out_dir / "讲义").rglob("*.pdf")))
    total = len(list(out_dir.rglob("*.pdf")))

    assert printed == lectures >= 1
    assert total > printed, "这一课同时有教案 / 学案 PDF，全包总数必然大于「讲义」份数"
    assert "包内 `讲义/` 下的 PDF 份数" in sheet and "counts.pdf_ok" in sheet


# ---------------------------------------------------------------------------
# ② 客户可见文档里不许出现裸布尔值（`True` / `False` / `None`）
# ---------------------------------------------------------------------------

def test_customer_visible_docs_never_print_a_bare_boolean(client, monkeypatch):
    """验收单与交付说明里**一个裸布尔值都不许有** —— 客户不该看到「PDF True 篇」这种话。"""
    _course_id, out_dir, _summary = _build_course(client, "客户可见面无布尔", monkeypatch)
    # 任务 143：把客户可见的第三份（素材清单）也扫进来（复测点名的那三份）
    for name in (delivery.ACCEPT_NAME, delivery.README_NAME, delivery.MATERIAL_NAME):
        hits = sorted({m.group(0) for m in _bool_word_re.finditer(_read(out_dir, name))})
        assert hits == [], f"{name} 里印出了裸布尔值：{hits}"


# ---------------------------------------------------------------------------
# ③ 降级项：有 ⇒ 逐条点名；无 ⇒ 明确写「本次无降级项」
# ---------------------------------------------------------------------------

def test_acceptance_sheet_lists_degraded_items(client, monkeypatch):
    """有降级 ⇒ 验收单**逐条点名**（几条、分别是什么），不再只写在交付说明里。"""
    _course_id, out_dir, _summary = _build_course(client, "验收单有降级", monkeypatch)
    manifest = json.loads(_read(out_dir, "manifest.json"))
    sheet = _read(out_dir, delivery.ACCEPT_NAME)

    assert manifest["degraded"], "这条用例需要至少 1 条降级项（测试课自带「数据表已并入正文」）"
    assert "## 2.2 降级项" in sheet
    assert f"本次共 **{len(manifest['degraded'])}** 条降级项" in sheet
    for row in manifest["degraded"]:
        assert f"**{row['item']}**：{row['reason']}" in sheet, row
    assert "本次无降级项" not in sheet


@_needs_edge   # 明确关掉 PDF 本身就是一条降级项 ⇒「无降级」只在真出 PDF 的机器上考得到
def test_acceptance_sheet_says_so_when_there_is_no_degradation(client, monkeypatch):
    """无降级 ⇒ 验收单**明确写「本次无降级项」**（不是留空、更不是静默）。"""
    _course_id, out_dir, _summary = _build_course(client, "验收单无降级", monkeypatch,
                                                  payload={"pdf": "auto"},
                                                  drop_chart_tables=True)
    manifest = json.loads(_read(out_dir, "manifest.json"))
    sheet = _read(out_dir, delivery.ACCEPT_NAME)

    assert manifest["degraded"] == [], manifest["degraded"]
    assert "## 2.2 降级项" in sheet
    assert "本次无降级项" in sheet


# ---------------------------------------------------------------------------
# ④ 任务 143：客户可见数值的**防御性渲染**（`_cell`）—— 不再靠「取值恰好不是布尔」
# ---------------------------------------------------------------------------

def test_cell_renders_none_and_booleans_as_words():
    """`None` ⇒ `—`；布尔 ⇒ `是` / `否` —— 直接喂给渲染函数，钉字面串。"""
    assert delivery._cell(None) == "—"
    assert delivery._cell(True) == "是"
    assert delivery._cell(False) == "否"


def test_cell_keeps_numbers_and_strings_as_is():
    """其余原样：数字不许变成 `是` / `否`（`0` / `1` 是数字，不是布尔）。"""
    assert delivery._cell(0) == "0"
    assert delivery._cell(1) == "1"
    assert delivery._cell(27) == "27"
    assert delivery._cell("—") == "—"
    assert delivery._cell("讲义（27 篇）") == "讲义（27 篇）"


def test_md_table_never_prints_a_bare_boolean():
    """验收单的表走 `_md_table` ⇒ 喂 `None` / `True` / `False` 也不许印出来。"""
    text = "\n".join(delivery._md_table(["项", "值", "说明"],
                                        [[None, True, False], ["正常", 27, "x"]]))
    hits = sorted({m.group(0) for m in _bool_word_re.finditer(text)})
    assert hits == [], f"表里印出了裸布尔 / None：{hits}"
    assert "—" in text and "是" in text and "否" in text and "27" in text
