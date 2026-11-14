# -*- coding: utf-8 -*-
"""任务 144：字体显式化 + 主题扩容 + 封面 / 内页标题区视觉层次。

三类断言：
① **主题表**：五套主题各自的 ``font`` 字段、``clean`` 就地升级（id / name 不变）、
   ``professional`` / ``handdrawn`` 的 ``palette`` + ``sizes`` 与改动前**逐字节冻结**、
   四档对比度（正文 / 标题 / 封面标题 / 主色）；
② **字体口径**：真出一门课课件，解压 pptx 数 ``a:rPr`` / ``a:defRPr`` / ``a:endParaRPr``
   —— 三写齐全（``a:latin`` + ``a:ea`` + ``a:cs``）且 typeface 等于主题字体，缺失清单必须为空，
   口播稿（notesSlides）单独一行同样成立（只写 ``run.font.name`` 只写 ``a:latin``，
   中文走 ``a:ea`` ⇒ 那样的实现会在本文件转红）；
③ **封面与标题区**：封面数据条只报真实值（章数 / 页数 / 配图 / 课时）、进 meta 名不算正文；
   封面装饰 ≤ ``MAX_DECOR_PER_SLIDE``；**内页装饰数不得上涨**；**内页正文几何逐字节冻结**。
"""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

import pytest
from lxml import etree

from app.services.ppt import MAX_DECOR_PER_SLIDE, SLIDE_WIDTH_IN, MARGIN_IN, read_deck_slides
from app.services.ppt_theme import THEMES, resolve_theme, theme_keys, theme_names, theme_options
from tests.test_ppt_v4 import (_add_diagram, _add_item, _add_point, _chapter, _course_deck,
                               _gen, _plan_and_approve)

A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
THEME_IDS_5 = ("clean", "professional", "handdrawn", "modern", "navy")
#: 期望字体（任务 144 的口径）：a:latin 与 a:cs 写拉丁名，a:ea 写中文字面名
FONT_LATIN, FONT_CJK = "Microsoft YaHei", "微软雅黑"
TITLE_ZONE_IN = 1.50      # CONTENT_TOP_IN：以上 = 标题文字框 / 标题区装饰 / 纯图页页首行
FOOTER_TOP_IN = 6.98      # 页脚文字框的顶

#: 改动前（d78021d）的 professional / handdrawn：palette + sizes **逐字节冻结**（§B-5）
FROZEN_BEFORE: dict[str, dict] = {
    "professional": {
        "palette": {"accent": "2563EB", "title": "FFFFFF", "cover_title": "1E293B",
                    "secondary": "0EA5E9", "text": "1E293B", "muted": "475569",
                    "caption": "475569", "footer": "94A3B8", "bg": "FFFFFF",
                    "panel": "1E293B", "rule": "3B82F6",
                    "colors": ["2563EB", "0EA5E9", "14B8A6", "F59E0B", "8B5CF6", "F43F5E"],
                    "warm": False},
        "sizes": {"cover_title": 40, "title": 28, "body": 18, "body_dense": 16,
                  "caption": 12, "footer": 10},
    },
    "handdrawn": {
        "palette": {"accent": "B45309", "title": "B45309", "cover_title": "B45309",
                    "secondary": "0F766E", "text": "44403C", "muted": "78716C",
                    "caption": "78716C", "footer": "A8A29E", "bg": "FFFBF2",
                    "panel": "FEF3C7", "rule": "FDE68A",
                    "colors": ["F97316", "EF4444", "F59E0B", "14B8A6", "6366F1", "0EA5E9"],
                    "warm": True},
        "sizes": {"cover_title": 40, "title": 28, "body": 18, "body_dense": 16,
                  "caption": 12, "footer": 10},
    },
}
#: 改动前四档字号（body / body_dense / caption / footer）—— 本轮只许动两档标题字号
FROZEN_BODY_SIZES = {"body": 18, "body_dense": 16, "caption": 12, "footer": 10}

#: 已知未达标项（**如实报出，不修**：§A2-5 本轮只修 clean，存量主题只报数）
#: handdrawn 的封面标题色 B45309 压在 FFFBF2 上只有 4.74:1（要求 ≥ 7:1）—— 存量课件的既有口径，
#: 本轮冻结不动；哪天修好了这里会转红，提醒同步更新本表与交付说明。
KNOWN_CONTRAST_GAPS = {("handdrawn", "cover_title"): 4.86}


def _srgb(channel: float) -> float:
    channel = channel / 255.0
    return channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4


def _luminance(color: str) -> float:
    raw = str(color).lstrip("#")
    r, g, b = (int(raw[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _srgb(r) + 0.7152 * _srgb(g) + 0.0722 * _srgb(b)


def _contrast(fg: str, bg: str) -> float:
    a, b = _luminance(fg), _luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return round((hi + 0.05) / (lo + 0.05), 2)


# ---------------------------------------------------------------------------
# ① 主题表
# ---------------------------------------------------------------------------

def test_every_theme_declares_its_own_font():
    """每套主题各自持一个 font 字段（不是共用一个对象），值 = 微软雅黑。"""
    assert list(THEMES) == list(THEME_IDS_5), list(THEMES)
    for theme_id, spec in THEMES.items():
        font = spec.get("font")
        assert isinstance(font, dict), theme_id
        assert font == {"latin": FONT_LATIN, "cjk": FONT_CJK}, (theme_id, font)
    seen = {id(spec["font"]) for spec in THEMES.values()}
    assert len(seen) == len(THEMES), "每套主题必须是自己的 font 字段（换某套字体不该牵动别的主题）"


def test_clean_upgraded_in_place_keeps_id_and_name():
    """clean 就地升级：id / name 一字不改（存量课重出一次就变好看），调色板换成新口径。"""
    spec, error = resolve_theme("clean")
    assert error is None
    assert spec["id"] == "clean" and spec["name"] == "清简教学"
    assert spec["palette"]["accent"] == "4338CA"
    assert spec["palette"]["accent"] != "1D4ED8", "旧口径（浅蓝）不该还在"
    assert spec["palette"]["panel"] == "EEF2FF" and spec["palette"]["rule"] == "C7D2FE"
    assert spec["sizes"]["cover_title"] == 44 and spec["sizes"]["title"] == 32


def test_new_themes_are_light_modern_and_declared():
    """新增两套浅色现代主题：现代简约（圆角无标题条）/ 深蓝商务（深色页眉带 + 白字标题）。"""
    assert theme_names() == ["清简教学", "专业培训", "手绘亲和", "现代简约", "深蓝商务"]
    modern, error = resolve_theme("现代简约")
    assert error is None
    assert modern["id"] == "modern" and modern["title_band"] is False and modern["round"] is True
    navy, error = resolve_theme("深蓝商务")
    assert error is None
    assert navy["id"] == "navy" and navy["title_band"] is True and navy["round"] is False
    assert navy["palette"]["title"] == "FFFFFF", "深色页眉带上的标题必须是白字"
    assert navy["palette"]["panel"] != modern["palette"]["panel"]
    for spec in (modern, navy):
        assert len(spec["description"]) >= 12, spec["description"]
        assert spec["id"] in theme_keys()
    options = {item["id"]: item for item in theme_options()}
    assert set(options) == set(THEME_IDS_5)
    for theme_id in THEME_IDS_5:
        assert options[theme_id]["colors"] and options[theme_id]["accent"]


def test_existing_themes_are_frozen_byte_for_byte():
    """§B-5 主题隔离：professional / handdrawn 的 palette + sizes 与改动前逐字节相同。"""
    for theme_id, frozen in FROZEN_BEFORE.items():
        spec = THEMES[theme_id]
        palette = dict(spec["palette"])
        palette["colors"] = list(palette["colors"])
        assert palette == frozen["palette"], (theme_id, palette)
        assert spec["sizes"] == frozen["sizes"], (theme_id, spec["sizes"])


def test_only_the_two_title_tiers_moved_for_existing_themes():
    """§B-3 字号不变量：body / body_dense / caption / footer 四档一动不许动。"""
    for theme_id in ("clean", "professional", "handdrawn"):
        sizes = THEMES[theme_id]["sizes"]
        for key, value in FROZEN_BODY_SIZES.items():
            assert sizes[key] == value, (theme_id, key, sizes[key])
    assert THEMES["professional"]["sizes"]["cover_title"] == 40
    assert THEMES["professional"]["sizes"]["title"] == 28
    # clean 是唯一允许涨两档标题的主题（封面标题更大、内页标题更大）
    assert THEMES["clean"]["sizes"]["cover_title"] == 44 > 40
    assert THEMES["clean"]["sizes"]["title"] == 32 > 28
    assert THEMES["handdrawn"]["sizes"]["title"] == 28


def test_contrast_ratios_per_theme():
    """§B-6 对比度：正文 ≥4.5 / 标题（压它自己的底）≥4.5 / 封面标题 ≥7 / 主色 ≥4.5。"""
    missing = []
    for theme_id, spec in THEMES.items():
        palette = spec["palette"]
        bg = palette["bg"]
        title_bg = palette["panel"] if spec["title_band"] else bg
        checks = {"text": (palette["text"], bg, 4.5),
                  "title": (palette["title"], title_bg, 4.5),
                  "cover_title": (palette["cover_title"], bg, 7.0),
                  "accent": (palette["accent"], bg, 4.5)}
        for key, (fg, back, bar) in checks.items():
            ratio = _contrast(fg, back)
            gap = KNOWN_CONTRAST_GAPS.get((theme_id, key))
            if gap is not None:
                assert ratio == pytest.approx(gap, abs=0.01), (theme_id, key, ratio)
                assert ratio < bar, (theme_id, key, "这条已知不达标，指标被谁改了？")
                missing.append(f"{theme_id}.{key} {ratio}:1 < {bar}:1（存量主题，本轮只报不修）")
                continue
            assert ratio >= bar, f"{theme_id}.{key} 只有 {ratio}:1（要求 ≥ {bar}:1，{fg} on {back}）"
    assert missing == ["handdrawn.cover_title 4.86:1 < 7.0:1（存量主题，本轮只报不修）"], missing


# ---------------------------------------------------------------------------
# ② 字体口径（真出课件 + 解压 pptx）
# ---------------------------------------------------------------------------

def _font_audit(pptx: Path) -> dict:
    """解压 pptx：① 总数 ② 三写齐全且等于期望字体的数 ③ 缺失清单（按 tag / 部件分开）。"""
    out: dict = {"total": 0, "ok": 0, "missing": [], "by_tag": {}, "by_part": {}}

    def bucket(mapping, key):
        return mapping.setdefault(key, {"total": 0, "ok": 0})

    with zipfile.ZipFile(pptx) as zin:
        for name in sorted(zin.namelist()):
            if not name.startswith("ppt/") or not name.endswith(".xml"):
                continue
            root = etree.fromstring(zin.read(name))
            part = ("notesSlides" if name.startswith("ppt/notesSlides/")
                    else "slides" if name.startswith("ppt/slides/") else "other")
            for tag in ("rPr", "defRPr", "endParaRPr"):
                for el in root.iter(A_NS + tag):
                    texts = {key: (el.find(A_NS + key).get("typeface")
                                   if el.find(A_NS + key) is not None else None)
                             for key in ("latin", "ea", "cs")}
                    want = {"latin": FONT_LATIN, "ea": FONT_CJK, "cs": FONT_LATIN}
                    ok = texts == want
                    out["total"] += 1
                    out["ok"] += 1 if ok else 0
                    for label, mapping in ((tag, out["by_tag"]), (part, out["by_part"])):
                        bucket(mapping, label)["total"] += 1
                        bucket(mapping, label)["ok"] += 1 if ok else 0
                    if not ok:
                        out["missing"].append(f"{name} a:{tag} {texts}")
    return out


def _course_with_deck(client, course_id: str, *, theme: str = "clean") -> dict:
    """一门课（2 章 + 知识点 + 概念图 + 数据图 + 讲义 + 口播稿）→ 整课课件。"""
    plan = _plan_and_approve(client, course_id, num_chapters=2)
    chapters = [n for n in plan["nodes"] if n["type"] in ("chapter", "section")]
    chapter = chapters[0]
    point = _add_point(course_id, chapter["id"], markdown=(
        "## 概念引入\n- 微生物的两大类\n- 发酵的四个阶段\n## 小结\n- 发酵是微生物的代谢过程\n"))
    _add_diagram(course_id, point["id"], "概念图-字体")
    _add_item(course_id, point["id"], "chart_bar", "颜色对比",
              {"data": {"labels": ["A", "B"], "datasets": [{"label": "数值", "data": [1, 2]}]}})
    _gen(client, course_id, scope="course", theme=theme)
    return {"chapter": chapter, "point": point, "plan": plan, "deck": _course_deck(client, course_id)}


def test_deck_writes_theme_font_into_every_text_property(client, course_id):
    """§B-2：a:rPr / a:defRPr / a:endParaRPr / notesSlides 全都要三写齐全，缺失清单为空。"""
    built = _course_with_deck(client, course_id)
    pptx = Path(built["deck"]["body"]["pptx"])
    audit = _font_audit(pptx)
    assert audit["missing"] == [], audit["missing"][:5]
    assert audit["ok"] == audit["total"] > 0, audit
    for tag in ("rPr", "defRPr", "endParaRPr"):
        assert audit["by_tag"][tag]["ok"] == audit["by_tag"][tag]["total"] > 0, audit["by_tag"]
    assert audit["by_part"]["notesSlides"]["total"] > 0, (
        "口播稿（notesSlides）也要带字体：python-pptx 只给设过字号的 run 建 rPr，"
        "notesSlides 的裸 run 必须补上", audit["by_part"])
    assert audit["by_part"]["notesSlides"]["ok"] == audit["by_part"]["notesSlides"]["total"]
    assert audit["by_part"]["slides"]["ok"] == audit["by_part"]["slides"]["total"] > 0


def test_font_triple_helper_and_no_latin_only_shortcut():
    """只写 a:latin（等于只写 run.font.name）时中文一个字都不会变 ⇒ 三写才算数。"""
    from app.services.ppt import font_triple

    assert font_triple({"latin": FONT_LATIN, "cjk": FONT_CJK}) == (FONT_LATIN, FONT_CJK, FONT_LATIN)
    assert font_triple({"cjk": FONT_CJK}) == (FONT_CJK, FONT_CJK, FONT_CJK)
    assert font_triple({}) == ("", "", "")


# ---------------------------------------------------------------------------
# ③ 封面与内页标题区
# ---------------------------------------------------------------------------

def _cover_stats_text(pptx: Path) -> str:
    from pptx import Presentation

    for shape in Presentation(str(pptx)).slides[0].shapes:
        if str(getattr(shape, "name", "")).startswith("cg-meta-cover-stats"):
            return shape.text_frame.text.strip()
    return ""


def _shape_rows(pptx: Path) -> list[dict]:
    from pptx import Presentation

    prs = Presentation(str(pptx))
    rows: list[dict] = []
    for page, slide in enumerate(prs.slides, 1):
        decor = 0
        for shape in slide.shapes:
            name = str(getattr(shape, "name", ""))
            if name.startswith("cg-decor"):
                decor += 1
            sizes = [run.font.size.pt for para in shape.text_frame.paragraphs
                     for run in (para.runs or []) if run.font.size] \
                if getattr(shape, "has_text_frame", False) else []
            rows.append({"page": page, "name": name, "type": str(shape.shape_type),
                         "l": round(shape.left / 914400, 4), "t": round(shape.top / 914400, 4),
                         "w": round(shape.width / 914400, 4), "h": round(shape.height / 914400, 4),
                         "size": max(sizes) if sizes else None,
                         "text": (shape.text_frame.text.strip() if getattr(shape, "has_text_frame", False) else "")})
        rows.append({"page": page, "name": "<decor-count>", "decor": decor})
    return rows


def _inner_geometry(rows: list[dict]) -> list[str]:
    """内页「正文区」几何指纹：排除封面 / 标题文字框 / 标题区装饰 / 页脚 / 装饰计数行。"""
    out: list[str] = []
    for row in rows:
        if "decor" in row or row["page"] == 1:
            continue
        if row["name"].startswith("Title") or row["t"] < TITLE_ZONE_IN or row["t"] >= FOOTER_TOP_IN:
            continue
        out.append(f"P{row['page']}|{row['name']}|{row['l']}|{row['t']}|{row['w']}|{row['h']}|{row['size']}")
    return out


#: 内页正文区几何（改动前后逐字节相同 —— 见交付说明 §B-4 的旧代码对照）
FROZEN_INNER_GEOMETRY: tuple[str, ...] = (
    "P2|Content Placeholder 2|0.5|1.75|9.0|4.9497|None",
    "P2|cg-decor-goal-card-1|0.55|1.5|3.891|4.95|None",
    "P2|cg-decor-goal-no-2|0.73|1.68|0.34|0.34|None",
    "P2|cg-icon-6|0.73|1.68|0.34|0.34|14.0",
    "P2|cg-card-7|0.73|2.14|3.531|4.13|16.0",
    "P2|cg-decor-goal-card-3|4.721|1.5|3.891|4.95|None",
    "P2|cg-decor-goal-no-4|4.901|1.68|0.34|0.34|None",
    "P2|cg-icon-10|4.901|1.68|0.34|0.34|14.0",
    "P2|cg-card-11|4.901|2.14|3.531|4.13|16.0",
    "P2|cg-decor-goal-card-5|8.892|1.5|3.891|4.95|None",
    "P2|cg-decor-goal-no-6|9.072|1.68|0.34|0.34|None",
    "P2|cg-icon-14|9.072|1.68|0.34|0.34|14.0",
    "P2|cg-card-15|9.072|2.14|3.531|4.13|16.0",
    "P3|Content Placeholder 2|0.5|1.75|9.0|4.9497|None",
    "P3|cg-decor-toc-card-1|0.55|1.5|12.233|2.335|None",
    "P3|cg-decor-toc-no-2|0.73|1.68|0.92|0.92|None",
    "P3|cg-icon-6|0.73|1.68|0.92|0.92|30.0",
    "P3|cg-card-7|1.81|1.68|10.793|1.975|16.0",
    "P3|cg-decor-toc-card-3|0.55|4.115|12.233|2.335|None",
    "P3|cg-decor-toc-no-4|0.73|4.295|0.92|0.92|None",
    "P3|cg-icon-10|0.73|4.295|0.92|0.92|30.0",
    "P3|cg-card-11|1.81|4.295|10.793|1.975|16.0",
    "P4|cg-decor-point-bar-2|0.46|1.5|0.06|0.3375|None",
    "P4|cg-decor-point-card-3|0.43|1.8986|12.473|0.4375|None",
    "P4|cg-decor-point-bar-4|0.46|1.9486|0.06|0.3375|None",
    # 任务 146：正文占位符提到最上层（_bring_to_front），z-order 改变；
    # 左 / 上 / 宽 / 高 / 字号六个数值与冻结点完全一致。
    "P4|Content Placeholder 2|0.55|1.5|12.233|0.8861|18.0",
    "P4|Picture 8|2.5778|2.4661|8.1774|3.6639|None",
    "P4|TextBox 9|0.55|6.15|12.233|0.32|12.0",
    "P5|cg-decor-point-bar-2|0.46|1.5|0.06|0.3375|None",
    "P5|cg-decor-point-card-3|0.43|1.8986|12.473|0.4375|None",
    "P5|cg-decor-point-bar-4|0.46|1.9486|0.06|0.3375|None",
    "P5|Content Placeholder 2|0.55|1.5|12.233|4.95|18.0",
    "P6|cg-meta-4|1.4|3.0|10.533|0.5|20.0",
    "P6|cg-meta-5|1.4|5.82|10.533|0.36|12.0",
    "P6|cg-decor-end-rule-2|6.1165|2.86|1.1|0.07|None",
    "P6|cg-decor-end-dot-3|0.55|6.3|0.3|0.3|None",
    "P6|cg-decor-end-dot-4|12.483|6.3|0.3|0.3|None",
    "P6|Content Placeholder 2|1.4|3.62|10.533|2.05|18.0",
)
FROZEN_INNER_DECOR: tuple[int, ...] = (6, 4, 4, 4, 4)


def test_cover_stats_bar_reports_real_numbers_only(client, course_id):
    """§A3-1：封面数据条的数字只来自真实值（大纲章数 / 最终页数 / 课时），且不算正文。"""
    built = _course_with_deck(client, course_id)
    deck = built["deck"]
    pptx = Path(deck["body"]["pptx"])
    text = _cover_stats_text(pptx)
    chapters = len([n for n in built["plan"]["nodes"] if n["type"] in ("chapter", "section")])
    assert text.startswith(f"{chapters} 章 · {deck['slide_count']} 页 · "), text
    assert "课时 90 分钟" in text, text          # conftest 的课程夹具 duration_minutes=90
    assert re.fullmatch(r"\d+ 章 · \d+ 页( · 配图 \d+ 张)?( · 课时 \d+ 分钟)?", text), text
    # 数据条走 cg-meta（元信息）⇒ 不算正文：页级大纲里一个字都不多
    slides = read_deck_slides(pptx)
    assert all(text not in (s["title"] or "") for s in slides)
    assert all(text != b for s in slides for b in s["bullets"])


def test_cover_visual_and_title_area_stay_within_budget(client, course_id):
    """§A3-3：封面有锚 / 贯穿条 / 数据条且装饰 ≤ 14；内页装饰数不得上涨。"""
    built = _course_with_deck(client, course_id)
    pptx = Path(built["deck"]["body"]["pptx"])
    rows = _shape_rows(pptx)
    cover = [r for r in rows if r["page"] == 1 and "decor" not in r]
    names = {re.sub(r"-\d+$", "", r["name"][len("cg-decor-"):]) for r in cover
             if r["name"].startswith("cg-decor-")}
    assert {"cover-bar", "cover-block", "cover-rule"} <= names, names
    rule = [r for r in cover if r["name"].startswith("cg-decor-cover-rule")][0]
    assert rule["w"] == pytest.approx(SLIDE_WIDTH_IN, abs=0.001), "封面强调条必须贯穿整页"
    assert any(r["name"].startswith("cg-meta-cover-stats") for r in cover)
    decor = {r["page"]: r["decor"] for r in rows if "decor" in r}
    assert max(decor.values()) <= MAX_DECOR_PER_SLIDE, decor
    inner = tuple(decor[page] for page in sorted(decor) if page != 1)
    assert inner == FROZEN_INNER_DECOR, (
        f"内页装饰数变了：{inner}（改动前 {FROZEN_INNER_DECOR}）"
        "—— 内页装饰数不得超过改动前")
    for row in rows:
        if row["name"] == "Rectangle 3":
            assert row["w"] == pytest.approx(SLIDE_WIDTH_IN - MARGIN_IN * 2, abs=0.001), row


def test_inner_body_geometry_is_frozen(client, course_id):
    """§B-4：内页正文区（卡片 / 图 / 正文框）的左 上 宽 高 字号逐字节冻结。"""
    built = _course_with_deck(client, course_id)
    rows = _inner_geometry(_shape_rows(Path(built["deck"]["body"]["pptx"])))
    assert rows == list(FROZEN_INNER_GEOMETRY), "\n".join(
        ["内页正文几何与冻结值不同："] + [f"  实际 {r}" for r in rows])
    assert len(rows) >= 5, rows


def test_deck_geometry_audit_passes_for_new_themes(client, course_id):
    """新增两套主题也要过既有几何自检（越界 / 重叠 / 出页为 0）。"""
    from app.services.ppt_layout import audit_pptx

    plan = _plan_and_approve(client, course_id, num_chapters=1)
    chapter = _chapter(client, course_id)
    point = _add_point(course_id, chapter["id"])
    _add_diagram(course_id, point["id"], "概念图-新主题")
    for theme in ("modern", "navy"):
        _gen(client, course_id, scope="course", theme=theme)
        deck = _course_deck(client, course_id)
        assert deck["theme"] == theme
        assert audit_pptx(Path(deck["body"]["pptx"])) == [], theme
    assert plan["nodes"]
