"""任务 94 回归测试：网页里按需预览 HTML 课件（**0 LLM**、复用出包渲染路径）。

钉四组：
① **接口**：`GET /api/ppt/{item_id}/html` ⇒ 200 + `content-type: text/html`；响应体就是
   单文件 HTML（`@keyframes` ≥3 处、`animation-duration:1.05s` 在、无外链、有 viewport、
   有 `@media print` + A4 横版）——与任务 88 的交付物**同一套判据**；
② **与交付包一致（不许是另一套渲染）**：同一次构建出的 `课件/HTML/{deck 标题}.html`
   与接口返回的**字节逐字节相同**；`deck_html.render_deck()` 也与 `build_files()` 的行一致；
③ **错误可见**：deck 不存在 / 不是 ppt 条目 ⇒ 404；渲染异常 ⇒ 500 且原因回给调用方
   （**绝不许静默返回空页**）；
④ **0 LLM**：取这份 HTML 期间任何一次模型调用都算失败。

协作规则第 7 条：五件套由 conftest 统一指向临时目录；本文件不碰真实库、真实知识库、
真实 storage/deliveries，也不调任何模型。
"""
from __future__ import annotations

from app.db.session import SessionLocal
from app.models.course import ContentItem, Course
from app.services import deck_html as deck_html_mod
from app.services import delivery as delivery_mod
from app.services.planner import list_nodes
from tests.test_deck_html import _audit_html, _build_no_llm, _course_with_decks


def _deck_ids(course_id: str) -> list[str]:
    db = SessionLocal()
    try:
        return [i.id for i in db.query(ContentItem).filter(
            ContentItem.course_id == course_id, ContentItem.kind == "ppt").all()]
    finally:
        db.close()


def _get_html(client, item_id: str):
    return client.get(f"/api/ppt/{item_id}/html")


# ---------------------------------------------------------------------------
# ① 接口 + ② 与交付包一致
# ---------------------------------------------------------------------------

def test_deck_html_endpoint_serves_the_delivery_single_file_html(client, monkeypatch):
    course_id = _course_with_decks(client, "网页版课件预览")
    summary = _build_no_llm(client, course_id, monkeypatch)
    out_dir = delivery_mod.deliveries_root() / summary["build_id"]

    for item_id in _deck_ids(course_id):
        db = SessionLocal()
        try:
            title = str(db.get(ContentItem, item_id).title)
        finally:
            db.close()
        resp = _get_html(client, item_id)
        assert resp.status_code == 200, resp.text
        # ② 字节逐字节 == 交付包里那一份（同一条渲染路径，不是另一套近似物）
        packed = (out_dir / "课件" / "HTML" / f"{title}.html").read_bytes()
        assert resp.content == packed, f"{title}：接口与交付包不一致（{len(resp.content)} vs {len(packed)} B）"
        assert "text/html" in resp.headers["content-type"]
        assert "charset=utf-8" in resp.headers["content-type"].lower()
        # ① 单文件口径与入动效（任务 88 同一套判据）
        text = resp.content.decode("utf-8")
        assert _audit_html(text) == [], _audit_html(text)
        assert text.count("@keyframes") >= 3, "入场动效的 @keyframes 少于 3 处"
        assert "animation-duration:1.05s" in text.replace(" ", ""), "慢动画不是 1.05s"


def test_render_deck_is_the_same_row_as_build_files(client, monkeypatch):
    course_id = _course_with_decks(client, "网页版课件同一渲染路径")
    _build_no_llm(client, course_id, monkeypatch)

    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        items = (db.query(ContentItem)
                 .filter(ContentItem.course_id == course_id)
                 .order_by(ContentItem.created_at).all())
        figures = delivery_mod.figure_rows(items)
        deck = [i for i in items if i.kind == "ppt"][-1]
        one = deck_html_mod.render_deck(db, course, deck, figures=figures)
        every = {row["deck_id"]: row for row in deck_html_mod.build_files(db, course, figures=figures)}
    finally:
        db.close()
    assert one == every[one["deck_id"]], "render_deck 与 build_files 渲染出的不是同一份"


# ---------------------------------------------------------------------------
# ③ 错误可见（404 / 500，绝不静默空页）
# ---------------------------------------------------------------------------

def test_deck_html_endpoint_404_for_unknown_and_non_ppt_items(client, monkeypatch):
    course_id = _course_with_decks(client, "网页版课件 404")
    assert _get_html(client, "no-such-item-id").status_code == 404

    db = SessionLocal()
    try:
        lecture = ContentItem(course_id=course_id, kind="lecture", title="讲义", body={}, meta={})
        db.add(lecture)
        db.commit()
        lecture_id = lecture.id
    finally:
        db.close()
    resp = _get_html(client, lecture_id)
    assert resp.status_code == 404, "非 ppt 条目必须是 404，不能渲染成空页"


def test_deck_html_endpoint_500_keeps_the_reason(client, monkeypatch):
    course_id = _course_with_decks(client, "网页版课件 500")
    item_id = _deck_ids(course_id)[0]

    def _boom(*a, **k):
        raise RuntimeError("渲染炸了（测试注入）")

    monkeypatch.setattr(deck_html_mod, "render_deck", _boom)
    resp = _get_html(client, item_id)
    assert resp.status_code == 500, resp.text
    assert "渲染炸了（测试注入）" in resp.text, "500 必须把原因带出来（不许静默空页）"


# ---------------------------------------------------------------------------
# ④ 0 LLM
# ---------------------------------------------------------------------------

def test_deck_html_endpoint_makes_no_llm_call(client, monkeypatch):
    course_id = _course_with_decks(client, "网页版课件 0 LLM")
    item_id = _deck_ids(course_id)[0]
    monkeypatch.setattr("app.services.llm.build_llm_client",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("预览不许调 LLM")))
    resp = _get_html(client, item_id)
    assert resp.status_code == 200, resp.text
    assert "@keyframes" in resp.text
