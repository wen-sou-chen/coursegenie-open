# -*- coding: utf-8 -*-
"""任务 36 测试：整课课件去掉「内部话术」+ 章节概览页真嵌代表图。

覆盖：
1. 内部话术词表（``find_internal_words`` / ``strip_internal_words`` / ``_purge_internal_words``）；
2. 代表图优先级（概念图 > 数据图 > 概念卡）与末页学员收尾文案；
3. 端到端：整课课件每个章节概览页 ≥1 张图、正文 0 内部话术、目录页无「图 N」、
   ``figure_count > 0``，且不回退任务 35 的「协议引用 0」。
"""
from __future__ import annotations

import re
import time

import pytest

from app.db.session import SessionLocal
from app.models.course import ContentItem, OutlineNode
from app.services.ppt import (INTERNAL_WORDS, _DEFAULT_ACTIONS, _chapter_overview_slide,
                              _course_end_bullets,
                              _figure_rank, _purge_internal_words, find_internal_words,
                              strip_internal_words)

OPS_TEXT = "工作台第 3 步"


# ---------------------------------------------------------------------------
# 夹具（沿用 test_ppt_v4 / v5 的口径）
# ---------------------------------------------------------------------------

def _plan_and_approve(client, course_id: str, num_chapters: int = 2) -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "让初中生理解光合作用", "num_chapters": num_chapters,
    })
    assert resp.status_code == 200, resp.text
    assert client.post("/api/plan/approve",
                       json={"course_id": course_id, "approved": True}).status_code == 200
    return client.get(f"/api/plan/{course_id}").json()


def _chapter_nodes(client, course_id: str) -> list[dict]:
    nodes = client.get(f"/api/plan/{course_id}").json()["nodes"]
    return [n for n in nodes if n["type"] in ("chapter", "section")]


def _add_point(course_id: str, chapter_id: str, *, key: str, title: str) -> dict:
    db = SessionLocal()
    try:
        chapter = db.get(OutlineNode, chapter_id)
        node = OutlineNode(outline_id=chapter.outline_id, node_key=key, node_type="point",
                           title=title, objective=f"理解{title}", order_index=1)
        db.add(node)
        db.commit()
        db.refresh(node)
        return {"id": node.id, "key": node.node_key}
    finally:
        db.close()


def _add_item(course_id: str, node_id: str, kind: str, title: str, body: dict,
              meta: dict | None = None) -> str:
    db = SessionLocal()
    try:
        row = ContentItem(course_id=course_id, node_id=node_id, kind=kind, title=title,
                          body=body, meta=meta or {})
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    finally:
        db.close()


def _diagram_body(text: str) -> dict:
    return {"excalidraw_json": {"type": "excalidraw", "elements": [
        {"type": "rectangle", "x": 20, "y": 40, "width": 220, "height": 110,
         "backgroundColor": "#ffe4e6", "strokeColor": "#f43f5e", "strokeWidth": 2.5},
        {"type": "text", "x": 40, "y": 80, "width": 180, "height": 40, "text": text,
         "fontSize": 20, "strokeColor": "#374151", "textAlign": "center"},
    ]}, "caption": text}


def _add_concept(course_id: str, node_id: str, title: str) -> str:
    return _add_item(course_id, node_id, "diagram", f"{title} · 概念图", _diagram_body(title),
                     {"role": "concept"})


def _add_concept_card(course_id: str, node_id: str, title: str) -> str:
    return _add_item(course_id, node_id, "diagram", f"{title} · 概念卡", _diagram_body(title),
                     {"role": "card"})


def _add_chart(course_id: str, node_id: str, title: str) -> str:
    return _add_item(course_id, node_id, "chart_bar", title, {"data": {
        "labels": ["A", "B", "C"], "datasets": [{"label": "数值", "data": [3, 5, 4]}]}},
        {"role": "card"})


def _wait(client, job_id: str, timeout: float = 60.0) -> dict:
    state: dict | None = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = client.get(f"/api/ppt/jobs/{job_id}").json()
        if state["status"] in ("success", "failed"):
            return state
        time.sleep(0.1)
    raise AssertionError(f"任务超时未结束：{state}")


def _gen(client, course_id: str, **payload) -> dict:
    resp = client.post("/api/ppt/generate", json={"course_id": course_id, **payload})
    assert resp.status_code == 200, resp.text
    state = _wait(client, resp.json()["job_id"])
    assert state["status"] == "success", state
    return state


def _course_deck(client, course_id: str) -> dict:
    decks = client.get(f"/api/ppt/{course_id}").json()["course"]
    assert decks, "应已生成整课课件"
    return decks[0]


def _page_kinds(deck: dict) -> list[tuple[str, dict]]:
    return list(zip(deck["meta"]["layouts"], deck["meta"]["slides"]))


def _media_count(pptx_path: str, index: int) -> int:
    """该页真实嵌入的图 / 图表 / 表格数（以磁盘 pptx 为准）。"""
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    slide = list(Presentation(pptx_path).slides)[index - 1]
    return sum(1 for shape in slide.shapes
               if shape.shape_type == MSO_SHAPE_TYPE.PICTURE
               or getattr(shape, "has_chart", False) or getattr(shape, "has_table", False))


def _page_captions(pptx_path: str, index: int) -> list[str]:
    from pptx import Presentation

    slide = list(Presentation(pptx_path).slides)[index - 1]
    return [shape.text_frame.text.strip() for shape in slide.shapes
            if getattr(shape, "has_text_frame", False)
            and shape.text_frame.text.strip().startswith("图 ")]


# ---------------------------------------------------------------------------
# ① 内部话术词表
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,hits", [
    ("1 AI 幻觉初印象 课件：在工作台第 3 步选中该章生成", ["工作台", "第 3 步", "选中该章"]),
    ("整课课件与各章课件均可单独重做（原地替换）。", ["原地替换"]),
    ("调用接口返回 item_id 与 slug", ["item_id", "slug"]),
    ("导出到 _图谱 目录，看 meta.slides", ["_图谱", "meta."]),
    ("先跑 verify.ps1 再看后端", ["verify"]),
    ("学完你将能：把 AI 输出变得可信任", []),
    ("本章目标：掌握老面引子调配与发酵状态判断", []),
])
def test_find_internal_words(raw, hits):
    assert find_internal_words(raw) == hits


@pytest.mark.parametrize("raw,expected", [
    ("1 AI 幻觉初印象 课件：在工作台第 3 步选中该章生成", "1 AI 幻觉初印象 课件"),
    ("整课课件与各章课件均可单独重做（原地替换）。", "整课课件与各章课件均可单独重做"),
    ("重做这一步：把接口返回值写进后端日志", "重做这一步：把接口返回值写进后端日志"),
    ("学完你将能：把 AI 输出变得可信任", "学完你将能：把 AI 输出变得可信任"),
])
def test_strip_internal_words(raw, expected):
    assert strip_internal_words(raw) == expected
    assert find_internal_words(strip_internal_words(raw)) == []


def test_purge_internal_words_moves_body_lines_to_notes():
    specs = [{
        "layout": "end",
        "title": "总结与下一步",
        "bullets": [f"1 第一章 课件：在{OPS_TEXT}选中该章生成",
                    "整课课件与各章课件均可单独重做（原地替换）。",
                    "落地清单 1：给关键结论补一条可核验的来源"],
        "notes": "口播稿。",
    }]
    notes: list[dict] = []
    _purge_internal_words(specs, notes)

    assert specs[0]["bullets"] == ["落地清单 1：给关键结论补一条可核验的来源"]
    assert not any(word in " ".join(specs[0]["bullets"]) for word in INTERNAL_WORDS)
    assert "工作台" in specs[0]["notes"], "内部话术应保留在备注里"
    assert [n["kind"] for n in notes] == ["internal-word", "internal-word"]
    assert all(n["slide"] == 1 for n in notes)
    assert specs[0]["title"] == "总结与下一步"


def test_purge_internal_words_keeps_technical_body_text():
    """任务 37：技术课的合法正文（「调用接口」「重做这一步」「后端」）不再被搬进备注。"""
    technical = "调用接口失败时，重做这一步，并把后端返回的错误码记下来"
    specs = [{
        "layout": "content",
        "title": "调用接口返回处理",
        "bullets": [technical, "参数写在 slug 里"],
        "notes": "口播稿。",
    }]
    notes: list[dict] = []
    _purge_internal_words(specs, notes)

    assert specs[0]["bullets"] == [technical], specs[0]["bullets"]
    assert specs[0]["title"] == "调用接口返回处理"
    assert [n["kind"] for n in notes] == ["internal-word"]
    assert "slug" in specs[0]["notes"]


def test_purge_internal_words_cleans_title_and_keeps_notes():
    specs = [{"layout": "cover", "title": "1 第一章 课件：在工作台第 3 步生成", "bullets": []}]
    notes: list[dict] = []
    _purge_internal_words(specs, notes)
    assert specs[0]["title"] == "1 第一章 课件"
    assert notes and notes[0]["kind"] == "internal-word"


# ---------------------------------------------------------------------------
# ② 代表图优先级 + 末页文案
# ---------------------------------------------------------------------------

def _fake_figure(kind: str, role: str, title: str):
    item = ContentItem(kind=kind, title=title, meta={"role": role}, body={})
    return item


def test_figure_rank_prefers_concept_then_chart_then_card():
    card = _fake_figure("diagram", "card", "概念卡")
    chart = _fake_figure("chart_bar", "card", "数据图")
    concept = _fake_figure("diagram", "concept", "概念图")
    assert [_figure_rank(f) for f in (concept, chart, card)] == [0, 1, 2]
    assert [f.title for f in sorted([card, chart, concept], key=_figure_rank)] == \
        ["概念图", "数据图", "概念卡"]


def _fake_chapter(key: str, title: str, objective: str):
    return OutlineNode(node_key=key, title=title, node_type="chapter", objective=objective)


class _FakeCourse:
    title = "AI 幻觉治理实战"
    subject = "AI"
    audience = "通用受众"


def test_course_end_bullets_are_learner_facing():
    chapters = [_fake_chapter("1", "AI 幻觉初印象", "理解 AI 幻觉的定义、现象及其影响"),
                _fake_chapter("2", "五大病根全解析", "深入理解幻觉的五大成因"),
                _fake_chapter("3", "方法一：反思校对", "掌握引导AI自检的方法")]
    bullets = _course_end_bullets(_FakeCourse(), chapters, "让 AI 输出可信任")

    assert len(bullets) == 4
    assert bullets[0].startswith("学完你将能：")
    assert [b.split("：")[0] for b in bullets[1:]] == ["落地清单 1", "落地清单 2", "落地清单 3"]
    assert bullets[1] == "落地清单 1：用「AI 幻觉初印象」这一章的方法，挑一份你手上的真实材料试一遍"
    assert bullets[2].startswith("落地清单 2：用「五大病根全解析」")
    assert "行动" not in " ".join(bullets)
    assert not any(word in " ".join(bullets) for word in INTERNAL_WORDS)


def test_course_end_bullets_keep_action_tail_for_long_chapter_titles():
    """长章名：宁可截章名，也不让动作句被版式（end 单条 40 字位）砍掉（任务 37）。"""
    from app.services.ppt_layout import char_units

    bullets = _course_end_bullets(_FakeCourse(),
                                  [_fake_chapter("1", "第1章 光合作用入门基础概念", "")], "")
    assert bullets[1].startswith("落地清单 1：用「第1章 光合作用")
    assert bullets[1].endswith("这一章的方法，挑一份你手上的真实材料试一遍"), bullets[1]
    assert char_units(bullets[1]) <= 40, bullets[1]


def test_course_end_bullets_fill_generic_actions_when_few_chapters():
    bullets = _course_end_bullets(_FakeCourse(), [_fake_chapter("1", "发酵原理", "")], "")
    assert len(bullets) == 4
    assert bullets[1] == "落地清单 1：用「发酵原理」这一章的方法，挑一份你手上的真实材料试一遍"
    assert bullets[2] == f"落地清单 2：{_DEFAULT_ACTIONS[1]}"
    assert bullets[3] == f"落地清单 3：{_DEFAULT_ACTIONS[2]}"
    assert not any(word in " ".join(bullets) for word in INTERNAL_WORDS)


# ---------------------------------------------------------------------------
# ③ 端到端：整课课件
# ---------------------------------------------------------------------------

def test_course_deck_has_no_internal_words_and_overviews_embed_figures(client, course_id):
    _plan_and_approve(client, course_id, num_chapters=2)
    chapters = _chapter_nodes(client, course_id)
    assert len(chapters) >= 2
    for idx, chapter in enumerate(chapters, 1):
        point = _add_point(course_id, chapter["id"], key=f"{idx}.1", title=f"知识点{idx}")
        _add_concept_card(course_id, point["id"], f"概念卡{idx}")   # 优先级最低
        _add_chart(course_id, point["id"], f"数据图{idx}")          # 中间
        _add_concept(course_id, point["id"], f"概念图{idx}")        # 应被选中
    _gen(client, course_id, scope="course")
    deck = _course_deck(client, course_id)
    pptx = deck["body"]["pptx"]

    # 整课课件现在真的有图（修复前 figure_count = 0）
    assert deck["meta"]["figure_count"] >= len(chapters), deck["meta"]["figure_failures"]
    assert deck["figure_failures"] == []

    # 每章概览页：≥1 张真图 + 图注；正文不再有「本章图 N 张」内部清单。
    # 任务 108 起 meta.layouts 记的是**渲染后**的最终版式（与 pptx 逐页一致）：图档位判据
    # （任务 110）可能把概览页改成「纯图页」，两种版式都算概览页。
    overviews = [(lay, page) for lay, page in _page_kinds(deck)
                 if lay in ("chapter", "figure_only")]
    assert len(overviews) == len(chapters)
    for _layout, page in overviews:
        assert _media_count(pptx, page["index"]) >= 1, page
        assert any("概念图" in cap for cap in _page_captions(pptx, page["index"])), \
            _page_captions(pptx, page["index"])
        body = " ".join(page["bullets"])
        assert "本章图" not in body, body
        assert not re.search(r"\[图:", body), body

    # 目录页：没有「图 N」内部计数
    toc_pages = [page for lay, page in _page_kinds(deck) if lay == "toc"]
    assert toc_pages
    for page in toc_pages:
        for bullet in page["bullets"]:
            assert not re.search(r"图\s*\d", bullet), bullet

    # 末页：给学员的收尾；产品操作提示只在备注
    end_pages = [page for lay, page in _page_kinds(deck) if lay == "end"]
    assert end_pages
    end = end_pages[0]
    body = " ".join(end["bullets"])
    assert body.count("落地清单") == 3, end["bullets"]
    assert "这一章的方法，挑一份你手上的真实材料试一遍" in body, end["bullets"]
    assert any(word in body for word in INTERNAL_WORDS) is False, body
    assert OPS_TEXT.replace("第 3 步", "") in (end["notes_excerpt"] or ""), end["notes_excerpt"]
    assert "工作台" in (end["notes_excerpt"] or ""), end["notes_excerpt"]
    assert (end["notes_excerpt"] or "").count("课件操作提示") == 1, end["notes_excerpt"]

    # 不回退任务 35：整课课件页面文字里没有协议引用原文
    for page in deck["meta"]["slides"]:
        blob = "\n".join([str(page.get("title") or ""), *(page.get("bullets") or []),
                          str(page.get("notes_excerpt") or "")])
        assert not any(marker in blob for marker in ("diagram://", "chart://", "gen://")), page


def test_chapter_overview_slide_spec_carries_media(client, course_id):
    """单元级：概览页 spec 含 media 且正文无「本章图」。"""
    from app.services.ppt import deck_work_dir
    from app.db.session import SessionLocal as _SL
    from app.services.planner import list_nodes

    _plan_and_approve(client, course_id, num_chapters=1)
    chapter = _chapter_nodes(client, course_id)[0]
    point = _add_point(course_id, chapter["id"], key="1.1", title="发酵原理")
    _add_concept(course_id, point["id"], "发酵原理")

    db = _SL()
    try:
        node = db.get(OutlineNode, chapter["id"])
        nodes = list_nodes(db, course_id)
        spec = _chapter_overview_slide(db, course_id, node, nodes,
                                       figures_dir=deck_work_dir(course_id, "course") / "figures")
    finally:
        db.close()
    assert len(spec["media"]) == 1, spec
    assert "本章图" not in " ".join(spec["bullets"])
    assert spec["bullets"][0].startswith("本章目标：")
