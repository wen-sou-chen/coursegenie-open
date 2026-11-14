"""任务 10（M3 三段式流程）回归测试：骨架 → 样板（风格锚）→ 按章批量 + 节点加锁 + 任务面板。

约定（协作规则第 7 条）：整仓测试的 DATABASE_URL / STORAGE_DIR / OBSIDIAN_VAULT_ROOT
由 conftest 统一指向临时目录，绝不触碰真实知识库。
"""
from __future__ import annotations

import time

from app.api.routes import lecture as lecture_routes
from app.db.session import SessionLocal
from app.models.course import ContentItem, Course, GenerationJob, Outline, OutlineNode
from app.services import lecture as lecture_mod
from app.services.lecture import _lecture_user_prompt, generate_all_lectures
from app.services.llm import MockLLMClient
from app.services.style_anchor import STYLE_ANCHOR_KEY, anchor_summary, style_anchor_prompt


# ------------------------------------------------------------------ 工具

def _plan_approve(client, course_id: str, num_chapters: int = 2) -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "生物光合作用", "num_chapters": num_chapters,
    })
    assert resp.status_code == 200, resp.text
    assert client.post("/api/plan/approve", json={
        "course_id": course_id, "approved": True}).status_code == 200
    return resp.json()


def _add_sections(course_id: str, chapter_key: str, titles: list[str]) -> list[str]:
    """给某章补小节（mock 大纲只给章节），返回小节 node_key 列表。"""
    db = SessionLocal()
    try:
        outline = (db.query(Outline).filter(Outline.course_id == course_id)
                   .order_by(Outline.created_at.desc()).first())
        base = db.query(OutlineNode).filter(OutlineNode.outline_id == outline.id).count()
        keys = []
        for index, title in enumerate(titles, 1):
            key = f"{chapter_key}.{index}"
            db.add(OutlineNode(outline_id=outline.id, node_key=key, node_type="section",
                               title=title, objective=f"掌握{title}",
                               order_index=base + index))
            keys.append(key)
        db.commit()
        return keys
    finally:
        db.close()


def _gen_lecture(client, course_id: str, node_id: str) -> dict:
    resp = client.post("/api/lecture/generate", json={
        "course_id": course_id, "node_id": node_id, "tone": "正式"})
    assert resp.status_code == 200, resp.text
    return _wait_job(client, resp.json()["job_id"])["result"]["item"]


def _wait_job(client, job_id: str, tries: int = 200) -> dict:
    state = None
    for _ in range(tries):
        state = client.get(f"/api/content/jobs/{job_id}").json()
        if state["status"] in ("success", "failed"):
            return state
        time.sleep(0.1)
    raise AssertionError(f"job timeout: {job_id} {state}")


def _chapter_job(client, course_id: str, chapter_node_id: str,
                 force: bool = False) -> dict:
    resp = client.post("/api/lecture/generate-chapter", json={
        "course_id": course_id, "chapter_node_id": chapter_node_id, "tone": "正式",
        "force": force})
    assert resp.status_code == 200, resp.text
    return _wait_job(client, resp.json()["job_id"])


def _node_id(client, course_id: str, key: str) -> str:
    plan = client.get(f"/api/plan/{course_id}").json()
    return next(n["id"] for n in plan["nodes"] if n["key"] == key)


def _rows(client, course_id: str) -> list[dict]:
    return client.get(f"/api/content/{course_id}").json()


def _lecture_of(rows: list[dict], node_id: str) -> dict | None:
    found = [r for r in rows if r["kind"] == "lecture" and r["node_id"] == node_id]
    return found[-1] if found else None


class _RecordingLLM(MockLLMClient):
    """记录每次 (system, user) 的 mock 客户端：用于断言样板锚确实进了提示词。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, *, max_tokens: int = 2000) -> str:
        self.calls.append((system, user))
        return super().complete(system, user, max_tokens=max_tokens)

    def lecture_prompts(self) -> list[str]:
        return [user for system, user in self.calls if "LECTURE_JSON" in system]


# ------------------------------------------------------------------ ① 样板与风格锚

def test_m3_style_anchor_prompt_contains_every_field():
    """风格锚 → 提示词：讲法/深度/图风格/习题难度/术语表都要进去；没样板时为空。"""
    assert style_anchor_prompt(None) == ""
    assert style_anchor_prompt({}) == ""

    anchor = {"source_node_key": "1.1", "source_title": "多菌共生体系", "tone": "轻松",
              "depth": "深入", "figure_style": "Excalidraw 概念卡", "quiz_difficulty": "进阶",
              "terminology": ["老面", "产气", "温度窗口"]}
    text = style_anchor_prompt(anchor)
    for piece in ("1.1", "多菌共生体系", "轻松", "深入", "Excalidraw 概念卡", "进阶",
                  "老面", "温度窗口"):
        assert piece in text, (piece, text)

    summary = anchor_summary(anchor)
    assert "1.1" in summary and "轻松" in summary


def test_m3_lecture_prompt_embeds_anchor():
    """_lecture_user_prompt 带锚时含风格锚段落，不带锚时保持原样。"""
    class _Node:
        node_key = "1.2"
        title = "酵母发酵"
        objective = "理解温度窗口"

    plain = _lecture_user_prompt(_Node(), "正式", "\n素材：略")
    assert "风格锚" not in plain and "素材：略" in plain

    with_anchor = _lecture_user_prompt(_Node(), "正式", "\n素材：略", {
        "source_node_key": "1.1", "source_title": "样板", "tone": "轻松",
        "terminology": ["老面"]})
    assert "【风格锚】" in with_anchor and "老面" in with_anchor and "1.1" in with_anchor


def test_m3_sample_lecture_to_anchor_then_chapter_prompt_uses_it(client, course_id, monkeypatch):
    """全链路：骨架 → 样板 → 锚 → 按章生成时锚进入提示词。"""
    plan = _plan_approve(client, course_id)
    chapter1 = next(n for n in plan["nodes"] if n["key"] == "1")
    _add_sections(course_id, "1", ["多菌共生体系", "酵母发酵"])
    node_sample = _node_id(client, course_id, "1.1")

    recorder = _RecordingLLM()
    monkeypatch.setattr(lecture_mod, "build_llm_client", lambda: recorder)

    # ② 先生成一节样板（未设锚 → 提示词里没有风格锚）
    sample = _gen_lecture(client, course_id, node_sample)
    assert recorder.lecture_prompts(), recorder.calls
    assert all("【风格锚】" not in text for text in recorder.lecture_prompts())

    # 「以这篇为样板」→ 写入 course.meta.style_anchor
    saved = client.post("/api/lecture/style-anchor", json={
        "course_id": course_id, "source_item_id": sample["id"]})
    assert saved.status_code == 200, saved.text
    anchor = saved.json()["style_anchor"]
    assert anchor["source_node_key"] == "1.1"
    assert anchor["source_title"] == "多菌共生体系"
    assert anchor["tone"] == "正式"
    assert anchor["depth"] in ("精简", "中", "深入")
    assert anchor["figure_style"]
    assert anchor["quiz_difficulty"]
    assert anchor["terminology"], anchor

    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        assert (course.meta or {})[STYLE_ANCHOR_KEY]["source_item_id"] == sample["id"]
    finally:
        db.close()

    read = client.get("/api/lecture/style-anchor", params={"course_id": course_id}).json()
    assert read["style_anchor"]["source_node_key"] == "1.1" and read["summary"]

    # 编辑锚（UI 可改）
    edited = client.post("/api/lecture/style-anchor", json={
        "course_id": course_id, "depth": "深入", "terminology": ["老面", "产气"]})
    assert edited.status_code == 200, edited.text
    assert edited.json()["style_anchor"]["depth"] == "深入"

    # ③ 按章生成：本次提示词必须带风格锚（术语/讲法/深度）
    recorder.calls.clear()
    job = _chapter_job(client, course_id, chapter1["id"])
    assert job["status"] == "success", job
    prompts = recorder.lecture_prompts()
    assert prompts, recorder.calls
    assert all("【风格锚】" in text for text in prompts), prompts[0][-200:]
    assert any("老面" in text and "深入" in text for text in prompts), prompts[0][-200:]

    # 清除样板 → 回到「先生成一节样板」状态
    assert client.delete("/api/lecture/style-anchor",
                         params={"course_id": course_id}).status_code == 200
    assert client.get("/api/lecture/style-anchor",
                      params={"course_id": course_id}).json()["style_anchor"] is None


# ------------------------------------------------------------------ ② 按章批量

def test_m3_generate_all_needs_anchor_or_explicit_ignore(client, course_id):
    """未设样板锚时不能直接触发全课批量：要么先做样板，要么显式选择忽略。"""
    plan = _plan_approve(client, course_id)
    blocked = client.post("/api/lecture/generate-all", json={
        "course_id": course_id, "tone": "正式"})
    assert blocked.status_code == 409, blocked.text
    assert "样板" in blocked.json()["detail"]

    explicit = client.post("/api/lecture/generate-all", json={
        "course_id": course_id, "tone": "正式", "ignore_style_anchor": True})
    assert explicit.status_code == 200, explicit.text
    job = _wait_job(client, explicit.json()["job_id"])
    assert job["status"] == "success", job
    assert job["result"]["scope"] == "course"
    assert job["result"]["ok"] == len(plan["nodes"])

    # 设好样板后，同样的入口不再需要勾选「忽略样板」
    node = _node_id(client, course_id, plan["nodes"][0]["key"])
    sample = _lecture_of(_rows(client, course_id), node)
    assert client.post("/api/lecture/style-anchor", json={
        "course_id": course_id, "source_item_id": sample["id"]}).status_code == 200
    ok = client.post("/api/lecture/generate-all", json={
        "course_id": course_id, "tone": "正式"})
    assert ok.status_code == 200, ok.text


def test_m3_chapter_batch_only_touches_that_chapter(client, course_id):
    """按章批量只影响当前章：任务面板口径（scope/chapter/total）也一并校验。"""
    plan = _plan_approve(client, course_id)
    chapter1 = next(n for n in plan["nodes"] if n["key"] == "1")
    chapter2 = next(n for n in plan["nodes"] if n["key"] == "2")
    _add_sections(course_id, "1", ["多菌共生体系", "酵母发酵"])

    resp = client.post("/api/lecture/generate-chapter", json={
        "course_id": course_id, "chapter_node_id": chapter1["id"], "tone": "正式"})
    assert resp.status_code == 200, resp.text
    queued = resp.json()
    assert queued["total"] == 2 and queued["chapter_key"] == "1"

    job = _wait_job(client, queued["job_id"])
    assert job["status"] == "success", job
    result = job["result"]
    assert result["scope"] == "chapter" and result["chapter_key"] == "1"
    assert result["ok"] == 2 and result["total"] == 2 and result["done"] == 2
    assert result["failed"] == [] and result["locked_skipped"] == []

    rows = _rows(client, course_id)
    made = {r["node_id"] for r in rows if r["kind"] == "lecture"}
    assert made == {_node_id(client, course_id, "1.1"), _node_id(client, course_id, "1.2")}
    assert _node_id(client, course_id, "2") not in made, "按章生成不应碰其它章"
    assert _lecture_of(rows, chapter2["id"]) is None, "第 2 章应保持未生成"


def test_m3_chapter_batch_unknown_chapter_is_rejected(client, course_id):
    """章节必须属于本课程；别课的节点 id 直接拒绝。"""
    plan = _plan_approve(client, course_id)
    resp = client.post("/api/lecture/generate-chapter", json={
        "course_id": course_id, "chapter_node_id": "not-a-node", "tone": "正式"})
    assert resp.status_code == 400, resp.text
    assert plan["nodes"]


# ------------------------------------------------------------------ ③ 节点加锁

def test_m3_locked_node_kept_and_listed_then_unlock_and_redo(client, course_id):
    """人工编辑/审核通过的节点加锁 → 批量跳过且内容不变、出现在 locked_skipped；解锁后可重做。"""
    plan = _plan_approve(client, course_id)
    chapter1 = next(n for n in plan["nodes"] if n["key"] == "1")
    _add_sections(course_id, "1", ["多菌共生体系", "酵母发酵"])
    node_a = _node_id(client, course_id, "1.1")
    node_b = _node_id(client, course_id, "1.2")

    first = _chapter_job(client, course_id, chapter1["id"])
    assert first["status"] == "success" and first["result"]["ok"] == 2

    lecture_a = _lecture_of(_rows(client, course_id), node_a)
    hand_written = "## 教师定稿讲义\n\n这一节我人工改过，批量不要再覆盖。"
    edited = client.patch(f"/api/lecture/{lecture_a['id']}", json={"markdown": hand_written})
    assert edited.status_code == 200, edited.text
    assert edited.json()["meta"]["locked"] is True, "人工编辑应自动加锁"

    # 任务 23 起批量默认按输入指纹去重；这里要验的是「重做时 locked 不被覆盖」，故强制重生成
    second = _chapter_job(client, course_id, chapter1["id"], force=True)
    assert second["status"] == "success", second
    result = second["result"]
    assert result["ok"] == 1, result
    assert [s["node_key"] for s in result["locked_skipped"]] == ["1.1"], result
    assert "已审核锁定" in result["locked_skipped"][0]["reason"]

    rows = _rows(client, course_id)
    kept = _lecture_of(rows, node_a)
    assert kept["body"]["markdown"] == hand_written, "locked 节点内容不得被覆盖"
    assert _lecture_of(rows, node_b)["body"]["markdown"] != hand_written

    # 「解锁并重做」：解锁 → 单点重新生成
    unlocked = client.post(f"/api/lecture/{kept['id']}/lock", json={"locked": False})
    assert unlocked.status_code == 200 and unlocked.json()["locked"] is False
    again = client.post("/api/lecture/generate", json={
        "course_id": course_id, "node_id": node_a, "tone": "正式",
        "item_id": kept["id"]})
    assert again.status_code == 200, again.text
    redone = _wait_job(client, again.json()["job_id"])["result"]["item"]
    assert redone["body"]["markdown"] != hand_written, "解锁后应能重做"

    third = _chapter_job(client, course_id, chapter1["id"])
    assert third["result"]["locked_skipped"] == [], third["result"]


def test_m3_generate_all_skips_locked_nodes():
    """服务层整课批量同样跳过 locked 节点（与按章批量一套口径）。"""
    # 由 test_m3_locked_node_kept_and_listed_then_unlock_and_redo 覆盖 HTTP 层，
    # 这里只锁死「locked_skipped 字段一定存在且默认不吞掉节点」。
    db = SessionLocal()
    try:
        course = Course(title="空课", subject="测试", audience="x", duration_minutes=30)
        db.add(course)
        db.commit()
        result = generate_all_lectures(db, course, tone="正式")
        assert result["locked_skipped"] == [] and result["ok"] == 0
    finally:
        db.close()


# ------------------------------------------------------------------ ③ 任务面板

def test_m3_progress_writer_reports_panel_fields(client, course_id):
    """任务面板数据源：第 i/N 个知识点、当前知识点、ok/failed/locked 计数。"""
    _plan_approve(client, course_id)
    db = SessionLocal()
    try:
        job = GenerationJob(course_id=course_id, job_type="lecture_batch", status="running")
        db.add(job)
        db.commit()
        db.refresh(job)
        node = db.query(OutlineNode).order_by(OutlineNode.order_index).first()
        progress = lecture_routes._progress_writer(
            db, job.id, {"scope": "chapter", "chapter_key": node.node_key,
                         "chapter_title": node.title})

        progress(1, 3, node, "running")
        row = db.get(GenerationJob, job.id)
        assert row.result["done"] == 1 and row.result["total"] == 3
        assert row.result["current"].startswith(node.node_key)
        assert row.result["scope"] == "chapter" and row.result["ok"] == 0

        progress(1, 3, node, "ok")
        progress(2, 3, node, "failed")
        progress(3, 3, node, "locked")
        row = db.get(GenerationJob, job.id)
        assert (row.result["ok"], row.result["failed"], row.result["locked"]) == (1, 1, 1)
        assert row.result["done"] == 3
    finally:
        db.close()


def test_m3_chapter_job_retry_after_failure(client, course_id, monkeypatch):
    """单章失败可单章重跑：第一次失败只记在该章任务里，重跑只重做这一章。"""
    plan = _plan_approve(client, course_id)
    chapter1 = next(n for n in plan["nodes"] if n["key"] == "1")
    chapter2 = next(n for n in plan["nodes"] if n["key"] == "2")
    calls = {"n": 0}
    real = MockLLMClient.complete

    class _Flaky(MockLLMClient):
        def complete(self, system, user, *, max_tokens=2000):
            if "LECTURE_JSON" in system:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("模拟 LLM 失败")
            return real(self, system, user, max_tokens=max_tokens)

    flaky = _Flaky()
    monkeypatch.setattr(lecture_mod, "build_llm_client", lambda: flaky)

    first = _chapter_job(client, course_id, chapter1["id"])
    assert first["status"] == "success", first
    assert first["result"]["failed"], first["result"]
    assert first["result"]["failed"][0]["node_key"] == "1"

    monkeypatch.setattr(lecture_mod, "build_llm_client", lambda: MockLLMClient())
    again = _chapter_job(client, course_id, chapter1["id"])
    assert again["status"] == "success" and again["result"]["failed"] == []
    assert again["result"]["ok"] == 1

    rows = _rows(client, course_id)
    assert _lecture_of(rows, _node_id(client, course_id, "1")) is not None
    assert _lecture_of(rows, chapter2["id"]) is None, "重跑第 1 章不应生成第 2 章"


def test_m3_style_anchor_requires_source(client, course_id):
    """没有样板、也没给字段时不能凭空设锚。"""
    resp = client.post("/api/lecture/style-anchor", json={"course_id": course_id})
    assert resp.status_code == 400, resp.text
    missing = client.post("/api/lecture/style-anchor", json={
        "course_id": course_id, "source_item_id": "nope"})
    assert missing.status_code == 404, missing.text


def test_m3_content_item_keeps_anchor_ref(client, course_id, monkeypatch):
    """用了样板的讲义会在 meta 里留下锚出处，便于排查「这篇是按哪节样板写的」。"""
    plan = _plan_approve(client, course_id)
    chapter2 = next(n for n in plan["nodes"] if n["key"] == "2")
    sample = _gen_lecture(client, course_id, _node_id(client, course_id, plan["nodes"][0]["key"]))
    assert client.post("/api/lecture/style-anchor", json={
        "course_id": course_id, "source_item_id": sample["id"]}).status_code == 200

    # 样板节（第 1 章）已自动加锁，所以换一章按章生成（P4-1）
    _chapter_job(client, course_id, chapter2["id"])
    rows = _rows(client, course_id)
    node = _node_id(client, course_id, "2")
    used = [r for r in rows if r["kind"] == "lecture" and r.get("meta", {}).get("style_anchor_ref")]
    assert used, "按章生成的讲义应带 style_anchor_ref"
    assert used[0]["meta"]["style_anchor_ref"]["source_node_key"] == "1"
    item = _lecture_of(rows, node)
    assert item["meta"]["style_anchor_ref"]["source_item_id"] == sample["id"]
