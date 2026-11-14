"""任务 150：讲师模式（数据 / 节点树 / 单文件 HTML / 0 LLM）。"""
from __future__ import annotations

from app.db.session import SessionLocal
from app.models.course import ContentItem, Course
from app.services import presenter_mode
from app.services.planner import list_nodes
from tests.test_deck_html import _seed_deck
from tests.test_delivery import _add_section, _new_course, _plan_approve

COURSE_TITLE = "讲师模式探针"

LECTURE_MD = """## 老面发酵

### 核心讲解

- **发酵判断**：体积膨胀约 2 倍，蜂窝均匀细密（来源:《发酵手册》第 3 段）。
  - **体积**：膨胀约 2 倍（来源:《发酵手册》第 4 段）。
  - **蜂窝**：均匀细密。
- **温度**：25~30℃ 最稳（来源:《发酵手册》第 7 段）。
  - 水温超过 40℃ 酵母失活。
- **配方**：面粉与水按 2:1 混合。
  - 加盐会抑制发酵。

### 举例

- **冬天发面**：可把面盆放在温水旁，延长发酵时间（来源:《发酵手册》第 12 段）。
"""


def _deck_fixture(client) -> tuple[str, str, str]:
    """造一门课 + 一份 8 页章课件（封面 / 知识点 3 页 / 回顾 / 练习 / 提问 / 谢谢）。"""
    course_id = _new_course(client, COURSE_TITLE)
    _plan_approve(client, course_id)
    _add_section(course_id, "1.1", "老面发酵")

    db = SessionLocal()
    try:
        point = next(n for n in list_nodes(db, course_id) if str(n.node_key) == "1.1")
        lecture = ContentItem(course_id=course_id, node_id=point.id, kind="lecture",
                              title="老面发酵", body={"markdown": LECTURE_MD}, meta={})
        db.add(lecture)
        db.commit()
        db.refresh(lecture)
        lecture_id = lecture.id
    finally:
        db.close()

    deck_id = _seed_deck(
        course_id, title="第1章课件", scope="chapter", chapter_key="1",
        slides=[
            ("1 老面发酵", "本章课件", ["本章课件"]),
            ("老面发酵", "先看体积，再看蜂窝。",
             ["【核心讲解】发酵判断包括体积和蜂窝", "体积：膨胀约 2 倍", "温度：25~30℃",
              "【举例】冬天发面要延长时间"]),
            ("老面发酵（接上页）", "温度看窗口。", ["配方：面粉与水按 2:1 混合"]),
            ("老面发酵 · 概念图", "一张图看懂。", ["总反应：糖 → CO₂ + 酒精 + 酸"]),
            # 故意放一条能在讲义里匹配到展开内容的要点：去掉结构页守卫时，
            # 这一页必须重新挂上展开按钮，用例据此判红（任务 150 变异 ㈡）。
            ("1 本章回顾", "收口。", ["要点回顾", "发酵判断"]),
            ("随堂练习", "做题。", ["练习题"]),
            ("1 课堂提问", "提问。", ["问题"]),
            ("谢谢观看", "结束。", ["谢谢"]),
        ],
        layouts=["cover", "point", "point", "point", "review", "quiz", "questions", "end"],
    )
    db = SessionLocal()
    try:
        deck = db.get(ContentItem, deck_id)
        deck.meta = {**(deck.meta or {}), "source": {"lecture_ids": [lecture_id]}}
        db.commit()
    finally:
        db.close()
    return course_id, deck_id, lecture_id


def test_presenter_data_maps_lecture_and_tiers(client):
    course_id, deck_id, _lecture_id = _deck_fixture(client)
    db = SessionLocal()
    try:
        data = presenter_mode.build_data(db, db.get(Course, course_id), db.get(ContentItem, deck_id))
    finally:
        db.close()

    assert data["page_count"] == 8
    assert len(data["nodes"]) == 6
    point = next(n for n in data["nodes"] if n["base_title"] == "老面发酵")
    assert point["page_count"] == 3
    assert point["page_indexes"] == [1, 2, 3]

    page = data["pages"][1]
    assert [it["tier"] for it in page["items"]] == ["core", "core", "expand", "case"]
    assert page["items"][0]["raw"].startswith("【核心讲解】")
    assert "发酵判断" in page["items"][0]["full"]
    assert page["items"][0]["full"] != page["items"][0]["raw"]
    assert "体积" in page["items"][0]["detail"]
    assert page["items"][2]["detail"]
    assert all(it["source"].startswith("来源:") for it in page["items"][:3])
    for it in page["items"]:
        if it["source"]:
            assert it["source"].removeprefix("来源:") in LECTURE_MD

    for page in data["pages"][4:]:
        assert page["is_structural"]
        assert all(not it["can_expand"] for it in page["items"])


def test_outline_endpoint_merges_pages_and_exposes_tree(client):
    course_id, deck_id, _lecture_id = _deck_fixture(client)
    resp = client.get(f"/api/ppt/{course_id}/outline", params={"item_id": deck_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deck_id"] == deck_id
    assert body["page_count"] == 8
    assert len(body["nodes"]) == 6
    point = next(n for n in body["nodes"] if n["base_title"] == "老面发酵")
    assert point["page_count"] == 3
    assert point["page_indexes"] == [1, 2, 3]
    assert body["chapter"]["nodes"] == body["nodes"]


def test_presenter_endpoints_are_single_file_zero_llm_and_leave_existing_html_unchanged(client, monkeypatch):
    course_id, deck_id, _lecture_id = _deck_fixture(client)
    before = client.get(f"/api/ppt/{deck_id}/html")
    assert before.status_code == 200, before.text

    monkeypatch.setattr("app.services.llm.build_llm_client",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("讲师模式不许调 LLM")))
    data = client.get(f"/api/ppt/{deck_id}/presenter-data")
    assert data.status_code == 200, data.text
    assert data.json()["pages"][1]["items"][0]["tier"] == "core"

    resp = client.get(f"/api/ppt/{deck_id}/presenter")
    assert resp.status_code == 200, resp.text
    assert "text/html" in resp.headers["content-type"]
    text = resp.text
    assert "const DECK =" in text
    assert 'id="presenter"' in text and 'id="mm"' in text
    for needle in ("逐条揭示", "本页讲稿", "page_indexes", "presenter", 'm.get("t") === "core"'):
        assert needle in text
    assert "<script src=" not in text and "<link rel=\"stylesheet\"" not in text

    after = client.get(f"/api/ppt/{deck_id}/html")
    assert after.status_code == 200
    assert before.content == after.content, "讲师模式不能改网页版课件/预览的字节口径"


def test_presenter_endpoints_404_and_500_are_explicit(client, monkeypatch):
    course_id, deck_id, _lecture_id = _deck_fixture(client)
    assert client.get("/api/ppt/no-such/presenter").status_code == 404
    assert client.get("/api/ppt/no-such/presenter-data").status_code == 404
    assert client.get(f"/api/ppt/{course_id}/outline", params={"item_id": "no-such"}).status_code == 404

    def boom(*args, **kwargs):
        raise RuntimeError("讲师模式渲染炸了（测试注入）")

    monkeypatch.setattr(presenter_mode, "render_presenter", boom)
    resp = client.get(f"/api/ppt/{deck_id}/presenter")
    assert resp.status_code == 500
    assert "讲师模式渲染炸了（测试注入）" in resp.text
