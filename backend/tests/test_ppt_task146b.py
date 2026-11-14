# -*- coding: utf-8 -*-
"""任务 146b：把图要回来（图与要点同页优先，丢图必须有真实理由）。

护栏四组，全部 0 LLM + 几何确定：

- A1 不许丢图 > 不许把图压小：带要点时纯图页会把图挤进备注，分栏/满幅仍达标就留在页上；
- A2 默认每知识点 3 页，显式 1 仍只有 1 页；
- A3 图页必须至少 1 条要点、正文 >= 12 字，禁止回到「一句话页」；
- A4 丢图原因只许是页数上限 / 要点不足 / 几何门槛，且带具体数值。

主证据是一条端到端用例：1 个知识点 + 2 张位图 + 4 条要点，成图后两张图都在页上、
每张图所在页至少 1 条要点、标题不叠续页后缀。
"""
from __future__ import annotations

import re
import time
from types import SimpleNamespace

import pytest

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.course import ContentItem, OutlineNode
from app.services import ppt


POINT_TITLE = "幻觉与人工复核"

BULLETS = [
    "幻觉指模型编造出看似合理但事实错误的内容",
    "训练数据里的噪声会在生成时被放大成错误结论",
    "模型会为了语言流畅而牺牲事实准确，因此需要复核",
    "人工复核按查来源、对事实、看边界三步检查",
]


def _bitmap(name: str, px: tuple[int, int] = (1000, 400),
            min_px: float = 20.0) -> dict:
    return {"type": "image", "path": name, "px": list(px), "caption": f"图-{name}",
            "legibility": {"min_px": min_px}}


def _node() -> SimpleNamespace:
    return SimpleNamespace(node_key="1.1", title=POINT_TITLE,
                           objective="理解模型为什么会自信地编造事实。",
                           teaching_strategy="先举一个逐词预测的例子，再让学员复述因果链。")


def _items(markdown: str) -> list[SimpleNamespace]:
    return [SimpleNamespace(kind="lecture", title=POINT_TITLE,
                            body={"markdown": markdown}, meta={})]


def _markdown(lines: list[str]) -> str:
    return "## 核心讲解\n" + "\n".join(f"- {line}" for line in lines)


# ---------------------------------------------------------------------------
# 端到端夹具（与 test_ppt_v5 同款：直接落节点与内容项，不调 LLM）
# ---------------------------------------------------------------------------

def _plan_and_approve(client, course_id: str, num_chapters: int = 1) -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "让初中生理解光合作用", "num_chapters": num_chapters,
    })
    assert resp.status_code == 200, resp.text
    assert client.post("/api/plan/approve",
                       json={"course_id": course_id, "approved": True}).status_code == 200
    return client.get(f"/api/plan/{course_id}").json()


def _chapter(client, course_id: str) -> dict:
    nodes = client.get(f"/api/plan/{course_id}").json()["nodes"]
    return ([n for n in nodes if n["type"] in ("chapter", "section")] or nodes)[0]


def _add_item(course_id: str, node_id: str, kind: str, title: str, body: dict) -> str:
    db = SessionLocal()
    try:
        row = ContentItem(course_id=course_id, node_id=node_id, kind=kind, title=title,
                          body=body, meta={})
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    finally:
        db.close()


def _add_point(course_id: str, chapter_id: str) -> dict:
    db = SessionLocal()
    try:
        chapter = db.get(OutlineNode, chapter_id)
        node = OutlineNode(outline_id=chapter.outline_id, node_key="1.1", node_type="point",
                           title=POINT_TITLE, objective=f"理解{POINT_TITLE}", order_index=1)
        db.add(node)
        db.commit()
        db.refresh(node)
        return {"id": node.id, "key": node.node_key}
    finally:
        db.close()


def _add_diagram(course_id: str, node_id: str, title: str) -> str:
    return _add_item(course_id, node_id, "diagram", title, {
        "excalidraw_json": {"type": "excalidraw", "elements": [
            {"type": "rectangle", "x": 20, "y": 40, "width": 220, "height": 110,
             "backgroundColor": "#ffe4e6", "strokeColor": "#f43f5e", "strokeWidth": 2.5},
            {"type": "text", "x": 40, "y": 80, "width": 180, "height": 40,
             "text": title[:6], "fontSize": 20, "strokeColor": "#374151",
             "textAlign": "center"},
        ]},
        "caption": title,
    })


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


def _chapter_deck(client, course_id: str) -> dict:
    decks = client.get(f"/api/ppt/{course_id}").json()["chapters"]
    assert decks, "应已生成章节课件"
    return decks[0]


def _pictures(shape) -> int:
    """数页上的图片（组合形状钻进去，A5 的口径）。"""
    if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
        return sum(_pictures(child) for child in shape.shapes)
    return 1 if shape.shape_type == MSO_SHAPE_TYPE.PICTURE else 0


_FOOTER = re.compile(r"·\s*P\d+\s*$")
_CAPTION = re.compile(r"^图\s*\d+\s*·")


def _visible_paras(slide) -> list[str]:
    """从 pptx 读页上可见正文段落（排除标题 / 图注 / 页脚），主证据不读 spec。"""
    title = slide.shapes.title.text_frame.text.strip() if slide.shapes.title is not None else ""
    out: list[str] = []

    def walk(shapes):
        for shape in shapes:
            if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                yield from walk(shape.shapes)
            else:
                yield shape

    for shape in walk(slide.shapes):
        if not getattr(shape, "has_text_frame", False):
            continue
        for para in shape.text_frame.paragraphs:
            text = str(para.text or "").strip()
            if not text or text == title or _FOOTER.search(text) or _CAPTION.match(text):
                continue
            out.append(text)
    return out


# ---------------------------------------------------------------------------
# 主证据：1 个知识点 + 2 张位图 + 4 条要点 ⇒ 两张图都在页上
# ---------------------------------------------------------------------------

def test_two_bitmaps_and_four_bullets_keep_both_figures_on_pages(client, course_id):
    _plan_and_approve(client, course_id, num_chapters=1)
    chapter = _chapter(client, course_id)
    point = _add_point(course_id, chapter["id"])
    _add_item(course_id, point["id"], "lecture", POINT_TITLE,
              {"markdown": _markdown(BULLETS), "tone": "正式"})
    _add_diagram(course_id, point["id"], "概念图-幻觉")
    _add_diagram(course_id, point["id"], "概念图-复核")

    _gen(client, course_id, scope="chapter", chapter_node_id=chapter["id"])
    deck = _chapter_deck(client, course_id)
    assert deck["meta"]["figure_count"] == 2, deck["meta"]["figure_failures"]

    prs = Presentation(deck["body"]["pptx"])
    meta_slides = deck["meta"]["slides"]
    pictures_per_slide = [sum(_pictures(shape) for shape in slide.shapes)
                          for slide in prs.slides]
    assert sum(pictures_per_slide) == 2, pictures_per_slide

    for idx, count in enumerate(pictures_per_slide):
        assert ppt.suffix_count(meta_slides[idx]["title"]) < 2
        if not count:
            continue
        # 从 pptx 实际文字形状读，不信 meta.slides
        visible = _visible_paras(prs.slides[idx])
        assert visible, meta_slides[idx]
        assert sum(len(b) for b in visible) >= ppt.MEDIA_PAGE_MIN_TEXT_CHARS

    assert all(ppt.suffix_count(s["title"]) < 2 for s in meta_slides)
    assert not any("（续）" in s["title"] or "（续图）" in s["title"] for s in meta_slides)


# ---------------------------------------------------------------------------
# A1：不许丢图 > 不许把图压小
# ---------------------------------------------------------------------------

def test_a1_keeps_the_figure_on_page_and_accepts_smaller_type():
    spec = {"layout": ppt.LAYOUT_POINT, "title": "一页",
            "bullets": ["光反应", "暗反应"],
            "media": [_bitmap("x", (2000, 1500), 45.0)]}
    tier, reason = ppt.media_tier_of(spec, 18.0)
    column = ppt.media_tier_boxes(spec["bullets"], 18.0)[0][1]
    _, width_col, pt_col = ppt.media_cells_report(spec["media"], column)
    assert tier == ppt.MEDIA_TIER_COLUMN
    assert width_col >= ppt.MIN_MEDIA_WIDTH_IN
    assert pt_col >= ppt.MIN_TEXT_PT
    assert "为了保住图，接受图内字号从" in reason and "图与要点同页" in reason


# ---------------------------------------------------------------------------
# A2：默认 3 页；显式 1 仍只有 1 页
# ---------------------------------------------------------------------------

def test_default_three_pages_and_explicit_one_stays_one():
    assert get_settings().ppt_slides_per_point == 3
    node, items = _node(), _items(_markdown(BULLETS))
    media = [_bitmap("a"), _bitmap("b"), _bitmap("c")]

    default = ppt._point_slides(node, items, media)
    assert len(default) == 3, [page["title"] for page in default]

    explicit_one = ppt._point_slides(node, items, media, slides_per_point=1)
    assert len(explicit_one) == 1
    assert "页数上限：limit=1" in explicit_one[0]["figure_dropped"]["reasons"][0]


# ---------------------------------------------------------------------------
# A3：图页至少 1 条要点、正文 >= 12 字
# ---------------------------------------------------------------------------

def test_figure_page_short_title_adds_a_second_bullet_to_reach_thirty_chars():
    """验收表「有图但正文 < 30 字 = 0」：短图注 + 一条 22 字要点时补第 2 条。"""
    bullets = ["【引入】在学术研究和专业写作中，正确地引用…",
               "【示例】下面给出一个标准的引用格式示例"]
    assert ppt._figure_bullet_lines(bullets, "引用要素") == 2
    assert ppt._figure_bullet_lines(bullets, "这是一个足够长的图注标题超过三十个字吗") == 1
    plan = ppt.page_media_plan(
        {"layout": ppt.LAYOUT_FIGURE, "title": "引用要素", "bullets": bullets,
         "media": [_bitmap("x", (1000, 700))]}, ppt.deck_palette())
    assert plan["bullet_lines"] == 2
    assert plan["bullet_strip_in"] == pytest.approx(ppt.FIGURE_BULLET_STRIP_IN * 2)
    assert plan["media_box"][3] < ppt.MEDIA_FIGURE_BOX[3]


def test_figure_pages_never_have_zero_bullets_and_keep_twelve_chars():
    bitmap_page = [[_bitmap("a")]]
    assert ppt.MEDIA_PAGE_MIN_TEXT_CHARS == 12
    assert not ppt._groups_carry_content([[]], bitmap_page)
    assert not ppt._groups_carry_content([["短"]], bitmap_page)
    assert ppt._groups_carry_content([["这里有十二个字符以上的可见正文"]], bitmap_page)

    slides = ppt._point_slides(_node(), _items(_markdown(BULLETS)),
                               [_bitmap("a"), _bitmap("b")])
    for page in slides:
        if not page.get("media"):
            continue
        bullets = [str(b) for b in (page.get("bullets") or []) if str(b).strip()]
        assert bullets, page
        assert sum(len(b) for b in bullets) >= ppt.MEDIA_PAGE_MIN_TEXT_CHARS


# ---------------------------------------------------------------------------
# A3 结构页：图挤掉正文到 30 字以下时，舍图保要点；正文仍 >=30 字则保图
# ---------------------------------------------------------------------------

def test_structural_page_drops_figure_only_when_body_would_fall_below_thirty():
    image = _bitmap("chapter-cover", (1962, 823), 29.9)
    short = ["本章目标：掌握投入与产出的性价比",
             "9.1 五法性能基准测试", "9.2 速查决策工具"]
    spec = {"layout": ppt.LAYOUT_CHAPTER, "title": "第 9 章 测试",
            "bullets": list(short), "media": [dict(image)]}
    notes: list[dict] = []
    ppt._fit_all([spec], ppt.deck_palette(), notes)
    assert spec["media"] == []
    assert spec["bullets"] == short
    assert sum(len(b) for b in spec["bullets"]) >= ppt.PAGE_MIN_TEXT_CHARS
    reasons = spec["figure_dropped"]["reasons"]
    assert len(reasons) == 1 and reasons[0].startswith("几何门槛：本页 3 条要点")
    assert "8.41 磅" in reasons[0] and "< 9 磅" in reasons[0]
    dropped = [n for n in notes if n.get("kind") == "figure-dropped"]
    assert dropped and dropped[0]["reason"] == reasons[0], dropped

    longer = ["本章目标：理解发酵原理、微生物基础与面团结构",
              "1.1 酵母与乳酸菌的分工及产气机制",
              "1.2 温度与湿度的控制窗口和判断标准"]
    keep = {"layout": ppt.LAYOUT_CHAPTER, "title": "第 1 章 测试",
            "bullets": list(longer), "media": [dict(image)]}
    notes = []
    ppt._fit_all([keep], ppt.deck_palette(), notes)
    assert keep["media"], "正文去掉 1 条要点仍有 >=30 字时不该丢图"
    assert sum(len(b) for b in keep["bullets"]) >= ppt.PAGE_MIN_TEXT_CHARS
    assert not [n for n in notes if n.get("kind") == "figure-dropped"]


# ---------------------------------------------------------------------------
# A4：三类真实原因 + 具体数值，figure_dropped["reasons"] 同步
# ---------------------------------------------------------------------------

def test_a4_page_limit_reason_has_limit_and_figure_numbers():
    slides = ppt._point_slides(_node(), _items(_markdown(BULLETS)),
                               [_bitmap("a"), _bitmap("b")], slides_per_point=1)
    assert len(slides) == 1
    reasons = slides[0]["figure_dropped"]["reasons"]
    assert len(reasons) == 1
    assert "页数上限：limit=1 已排满" in reasons[0]
    assert "共 2 张图" in reasons[0] and "第 2 张" in reasons[0]
    assert reasons[0] in slides[0]["notes"]
    assert "每页图上限" not in reasons[0]


def test_a4_bullet_shortage_reason_has_counts():
    slides = ppt._point_slides(_node(), _items(_markdown(["只有一条要点"])) ,
                               [_bitmap("a"), _bitmap("b")], slides_per_point=3)
    assert len(slides) == 1
    reasons = slides[0]["figure_dropped"]["reasons"]
    assert len(reasons) == 1
    assert "要点不足" in reasons[0]
    assert "要点 1 条 < 图 2 张" in reasons[0]
    assert reasons[0] in slides[0]["notes"]
    assert "每页图上限" not in reasons[0]


def test_a4_geometry_gate_reason_has_all_tier_numbers():
    spec = {"layout": ppt.LAYOUT_POINT, "title": "一页",
            "bullets": ["光反应把光能转成 ATP 与 NADPH", "暗反应用 ATP 固定二氧化碳"],
            "media": [_bitmap("narrow", (300, 2000), 20.0)]}
    notes: list[dict] = []
    ppt._fit_all([spec], ppt.deck_palette(), notes)

    assert spec["media"] == []
    reasons = spec["figure_dropped"]["reasons"]
    assert len(reasons) == 1
    assert "几何门槛" in reasons[0]
    assert "分栏" in reasons[0] and "满幅" in reasons[0] and "整页档" in reasons[0]
    assert "≥6.00 英寸" in reasons[0] and "≥9 磅" in reasons[0]
    assert reasons[0] in (spec.get("notes") or "")
    assert "每页图上限" not in reasons[0]
