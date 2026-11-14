"""完整讲义（Lecture）测试：门禁、生成（配图+习题一体化）、重建替换、编辑与删除。"""
from __future__ import annotations


def _plan(client, course_id: str) -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "初中生物光合作用", "num_chapters": 2,
    })
    assert resp.status_code == 200, resp.text
    assert client.post("/api/plan/approve", json={"course_id": course_id, "approved": True}).status_code == 200
    return resp.json()


def _node(plan: dict) -> str:
    leaf = next((n for n in plan["nodes"] if "." in n["key"]), None)
    return (leaf or plan["nodes"][0])["id"]


def _rows(client, course_id: str) -> list[dict]:
    return client.get(f"/api/content/{course_id}").json()


def test_lecture_rejects_before_outline_approved(client, course_id):
    resp = client.post("/api/lecture/generate", json={
        "course_id": course_id, "node_id": "whatever",
    })
    assert resp.status_code == 409, resp.text


def test_lecture_generate_creates_lecture_diagram_quizzes(client, course_id, gen_lecture_row):
    plan = _plan(client, course_id)
    node_id = _node(plan)
    row = gen_lecture_row(course_id, node_id)
    assert row["kind"] == "lecture"
    md = str(row["body"]["markdown"])
    assert "## 随堂练习" in md, "讲义末尾应渲染随堂练习"
    assert "/images/" not in md, "讲义默认零 AI 位图，不应含 PNG 配图引用"
    assert "diagram://" in md, "讲义应自动带一张一图看懂概念图"
    assert "概念卡：" in md, "讲义应把内容拆成就近的概念卡"
    assert "gen://" not in md, "不应残留未替换的配图占位符"

    rows = _rows(client, course_id)
    lectures = [i for i in rows if i["kind"] == "lecture" and i["node_id"] == node_id]
    assert len(lectures) == 1
    assert lectures[0]["id"] == row["id"]

    quizzes = [i for i in rows if i["kind"] == "quiz" and i["node_id"] == node_id]
    assert len(quizzes) >= 2, "讲义里的习题应落为 quiz_items"
    assert any(q["body"]["explanation"] for q in quizzes)

    images = [i for i in rows if i["kind"] == "image" and i["node_id"] == node_id]
    assert len(images) == 0, "默认零 AI 位图，不应生成配图条目"
    diagrams = [i for i in rows if i["kind"] == "diagram" and i["node_id"] == node_id]
    concepts = [i for i in diagrams if (i["meta"].get("role") or "concept") != "card"]
    cards = [i for i in diagrams if i["meta"].get("role") == "card"]
    # 任务 125：图形上限（>6）也生效 ⇒ mock 的一图看懂按容量拆成 2 张（都挂同一篇讲义）
    assert len(concepts) == 2, "每篇讲义应有一图看懂：不超限一张、超限按容量拆多张"
    assert len(cards) >= 1, "讲义应自动生成就近的概念卡"
    assert all(i["meta"].get("from_lecture") == row["id"] for i in diagrams)


def test_lecture_strips_gen_placeholder_without_bitmap(client, course_id, monkeypatch, gen_lecture_row):
    """LLM 违反提示词仍写 gen:// 时：占位被剥离，不落 PNG 位图条目。"""
    plan = _plan(client, course_id)
    node_id = _node(plan)

    class _GenClient:
        name = "gen-stubborn"

        def complete_json(self, system: str, user: str, *, max_tokens: int = 2000) -> dict:
            return {
                "markdown": (
                    f"## {node_id}\n\n正文……\n\n"
                    "![显微结构示意](gen://酵母显微结构，画面干净无文字)\n\n"
                    "## 小结\n要点已讲完。"
                ),
                "summary": "摘要",
                "keywords": [],
                "quizzes": [],
            }

    import app.services.lecture as lecture_mod

    monkeypatch.setattr(lecture_mod, "build_llm_client", lambda: _GenClient())
    row = gen_lecture_row(course_id, node_id)
    md = str(row["body"]["markdown"])
    assert "gen://" not in md, "gen:// 占位应被剥离"
    assert "/images/" not in md, "不应为残留占位生成 AI 位图"
    assert "![一图看懂](diagram://" in md, "讲义仍应自动配矢量概念图"

    rows = _rows(client, course_id)
    images = [r for r in rows if r["kind"] == "image"]
    assert len(images) == 0, "零位图策略下不应出现 image 条目"


def test_lecture_regenerate_replaces_without_stacking(client, course_id, gen_lecture_row):
    plan = _plan(client, course_id)
    node_id = _node(plan)
    first = gen_lecture_row(course_id, node_id)
    second = gen_lecture_row(course_id, node_id)
    assert second["id"] == first["id"], "再生成应原地替换同一讲义条目"

    rows = _rows(client, course_id)
    node_lectures = [i for i in rows if i["kind"] == "lecture" and i["node_id"] == node_id]
    node_quizzes = [i for i in rows if i["kind"] == "quiz" and i["node_id"] == node_id]
    node_images = [i for i in rows if i["kind"] == "image" and i["node_id"] == node_id]
    node_diagrams = [i for i in rows if i["kind"] == "diagram" and i["node_id"] == node_id]
    assert len(node_lectures) == 1
    assert len(node_quizzes) == 2, "旧讲义习题应被清理，不重复堆积"
    assert len(node_images) == 0, "零位图策略下不应产生配图条目"
    node_concepts = [i for i in node_diagrams
                     if (i["meta"].get("role") or "concept") != "card"]
    node_cards = [i for i in node_diagrams if i["meta"].get("role") == "card"]
    assert len(node_concepts) == 2, "旧讲义一图看懂概念图应被清理（只剩本次拆出的 2 张），不重复堆积"
    assert len(node_cards) >= 1, "旧讲义概念卡应被清理，不重复堆积"
    assert all(i["meta"].get("from_lecture") == second["id"] for i in node_diagrams)
    assert len(first["meta"].get("history") or []) == 0
    assert len(second["meta"].get("history") or []) == 1, "重跑应保留上一版讲义"


def test_lecture_edit_and_delete(client, course_id, gen_lecture_row):
    plan = _plan(client, course_id)
    node_id = _node(plan)
    row = gen_lecture_row(course_id, node_id)

    edited = client.patch(f"/api/lecture/{row['id']}", json={
        "markdown": "## 教师改写后的讲义\n\n手动调整全文。",
    })
    assert edited.status_code == 200, edited.text
    assert edited.json()["body"]["markdown"].startswith("## 教师改写后的讲义")

    deleted = client.delete(f"/api/lecture/{row['id']}")
    assert deleted.status_code == 200, deleted.text
    rows = _rows(client, course_id)
    assert not [i for i in rows if i["id"] == row["id"]]
    assert not [i for i in rows if i["kind"] == "quiz" and i["node_id"] == node_id], "删除讲义应连带删除其习题"
    assert not [i for i in rows if i["kind"] == "image" and i["node_id"] == node_id], "删除讲义应连带删除其配图"
    assert not [i for i in rows if i["kind"] == "diagram" and i["node_id"] == node_id], "删除讲义应连带删除其概念图"


def test_inject_diagram_before_level3_summary():
    """真实讲义小结常用 ### 小结：一图看懂引用应插到该标题之前。"""
    import app.services.lecture as lecture_mod

    md = "## 标题\n\n正文讲解……\n\n### 小结\n本节要点。\n"
    out = lecture_mod._inject_diagram_markdown(md, "abc123")
    assert out.index("![一图看懂](diagram://abc123)") < out.index("### 小结")


def test_relocate_concept_diagram_from_after_summary():
    """存量讲义若把一图看懂留在小结之后，重建卡片时应归位到小结之前。"""
    import app.services.lecture as lecture_mod

    md = ("## 标题\n\n正文讲解……\n\n### 小结\n要点已讲完。\n\n"
          "![一图看懂](diagram://old1)\n\n![一图看懂](diagram://old2)\n")
    out = lecture_mod._relocate_concept_diagram_refs(md)
    assert out.count("diagram://old1") == 1
    assert out.count("diagram://old2") == 1
    assert out.index("![一图看懂](diagram://old1)") < out.index("### 小结")
    assert out.index("![一图看懂](diagram://old2)") < out.index("### 小结")


def test_inject_before_summary_handles_level3_and_chapter_summary():
    import app.services.lecture as lecture_mod

    md = "## 标题\n\n正文……\n\n### 本章小结\n要点。\n\n## 随堂练习\n题目。\n"
    out = lecture_mod._inject_before_summary(md, "\n![概念卡：测试](diagram://card1)")
    assert out.index("diagram://card1") < out.index("### 本章小结")


def test_anchor_fuzzy_match_paraphrase_heading():
    """LLM 改写过的 anchor 也能就近命中：对比小节标题被插到该小节之后、小结之前。"""
    import app.services.lecture as lecture_mod

    md = ("## 标题\n\n### 与纯酵母发酵的对比\n"
          "| 特征 | 老面 | 纯酵母 |\n| 时间 | 6-12h | 1-2h |\n\n"
          "### 小结\n要点。\n")
    out, ok = lecture_mod._insert_after_anchor(
        md, "老面发酵与纯酵母发酵的对比", "![概念卡：发酵方式](diagram://card1)")
    assert ok
    assert out.index("diagram://card1") > out.index("### 与纯酵母发酵的对比")
    assert out.index("diagram://card1") < out.index("### 小结")
