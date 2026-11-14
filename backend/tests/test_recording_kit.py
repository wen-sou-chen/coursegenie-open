"""任务 87（修订版）回归测试：录课包（逐字稿 / 时长表 / 字幕 / 分镜 / 互动点）+ SCORM 按课时打包。

**全部 0 LLM**：只做纯本地确定性渲染与打包，不调模型、不花钱、不重跑讲义。
协作规则第 7 条：DATABASE_URL / STORAGE_DIR / OBSIDIAN_VAULT_ROOT 等五件套由 conftest
统一指向临时目录；本文件不碰真实库、真实知识库与真实 storage/deliveries。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from app.db.session import SessionLocal
from app.models.course import ContentItem, Course, QuizItem
from app.services import delivery as delivery_mod
from app.services import publish as publish_mod
from app.services import recording_kit as kit
from app.services.planner import list_nodes
from tests.test_delivery import _add_section, _new_course, _plan_approve

CP_NS = "http://www.imsglobal.org/xsd/imscp_v1p1"
NS = {"cp": CP_NS}
ADLCP_NS = "http://www.adlnet.org/xsd/adlcp_v1p3"

SHORT_NOTE = "这一页我们看发酵。"
#: 一份「卡在上限」的口播稿（备注上限 300 字/页）—— 6 页就该撞上 6 分钟闸门。
LONG_NOTE = ("这一页我们看「发酵原理」。老面发酵依靠多种微生物协同作用，温度与湿度决定菌群活性，"
             "面团状态是判断发酵是否到位的直接依据；实操时先看体积、再看蜂窝、最后闻酸香，"
             "三步都过才进下一步。") * 3 + "先看体积，再看蜂窝，最后闻酸香。"
INTERNAL_NOTE = "课件操作提示（仅讲师 / 运营可见，勿投屏）：每章课件在工作台第 3 步选中该章生成。"
LECTURE_MD = """## 发酵原理

### 核心讲解
- 老面发酵依靠多种微生物协同作用
- 温度与湿度决定菌群活性
- 面团体积与蜂窝是判断标准
"""


# ---------------------------------------------------------------------------
# 造课
# ---------------------------------------------------------------------------

def _note(spoken: str, extra: str = "") -> str:
    """与 ``ppt_layout.compose_notes`` 同构：口播稿 + 换行 + 诊断 / 内部提示。"""
    return "\n".join(part for part in (spoken, extra) if part)


def _set_scenario(course_id: str, selected: tuple[str, ...]) -> None:
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        course.meta = {**(course.meta or {}), "scenario": {
            "primary": selected[0], "selected": list(selected),
            "updated_at": "2026-09-24T10:00:00+08:00"}}
        db.commit()
    finally:
        db.close()


def _nodes(course_id: str) -> list[dict]:
    db = SessionLocal()
    try:
        return [{"id": node.id, "key": str(node.node_key or ""), "title": node.title}
                for node in list_nodes(db, course_id)]
    finally:
        db.close()


def _seed_deck(course_id: str, *, title: str, scope: str, chapter_key: str,
               slides: list[tuple[str, str, list[str]]], layouts: list[str],
               pptx: str = "real") -> str:
    """造一份带页级备注的课件（``slides`` = ``[(页标题, 备注, 要点)]``）。

    ``pptx`` = ``real`` 时写一份**真的** pptx（python-pptx，Title Only 版式、无内嵌图）；
    ``missing`` 时只记一个不存在的路径 —— 用来验「源文件不在 ⇒ 明写未核对」这条降级。
    """
    from app.core.config import get_settings

    deck_dir = get_settings().storage_dir / "ppt" / f"{course_id}_{scope or 'course'}"
    body: dict = {"engine": "python-pptx"}
    if pptx == "real":
        from pptx import Presentation

        deck_dir.mkdir(parents=True, exist_ok=True)
        path = deck_dir / f"deck-{len(slides)}.pptx"
        prs = Presentation()
        for _ in slides:
            prs.slides.add_slide(prs.slide_layouts[5])       # Title Only：没有内嵌图
        prs.save(str(path))
        body["pptx"] = str(path)
    else:
        body["pptx"] = str(deck_dir / "不存在的课件.pptx")
    db = SessionLocal()
    try:
        item = ContentItem(
            course_id=course_id, kind="ppt", title=title, body=body,
            meta={"scope": scope, "chapter_key": chapter_key, "layouts": list(layouts),
                  "slides": [{"index": index, "title": page_title, "bullets": list(bullets),
                              "notes_excerpt": note, "image": ""}
                             for index, (page_title, note, bullets) in enumerate(slides, 1)]})
        db.add(item)
        db.commit()
        return item.id
    finally:
        db.close()


def _seed_lecture(course_id: str, node_id: str, node_key: str) -> None:
    db = SessionLocal()
    try:
        db.add(ContentItem(course_id=course_id, node_id=node_id, kind="lecture",
                           title="讲义", body={"markdown": LECTURE_MD},
                           meta={"node_key": node_key}))
        db.commit()
    finally:
        db.close()


def _seed_quiz(course_id: str, node_id: str, question: str) -> None:
    db = SessionLocal()
    try:
        db.add(QuizItem(course_id=course_id, node_id=node_id, qtype="choice", difficulty="easy",
                        question=question, options=["甲", "乙"], answer=0, explanation="甲"))
        db.commit()
    finally:
        db.close()


def _course_with_kit(client, title: str, *, selected: tuple[str, ...] = ("recorded",),
                     decks: bool = True, pptx: str = "real",
                     course_deck_pptx: str = "missing") -> str:
    """一门「有课件 + 有讲义 + 有练习」的课（大纲由 mock 规划器生成，0 真实调用）。"""
    course_id = _new_course(client, title)
    _plan_approve(client, course_id)
    _add_section(course_id, "1.1", "发酵原理")
    _add_section(course_id, "1.2", "温度窗口")
    if selected:
        _set_scenario(course_id, selected)
    nodes = _nodes(course_id)
    chapter = next(node for node in nodes if "." not in node["key"])
    points = [node for node in nodes if "." in node["key"]]
    for point in points:
        _seed_lecture(course_id, point["id"], point["key"])
        _seed_quiz(course_id, point["id"], f"「{point['title']}」的关键结论是什么？")
    if decks:
        _seed_deck(course_id, title="整课课件", scope="course", chapter_key="",
                   slides=[("课程封面", _note(SHORT_NOTE), ["受众：初中生"]),
                           ("课程目标", _note(SHORT_NOTE), ["理解发酵原理"])],
                   layouts=["cover", "goals"], pptx=course_deck_pptx)
        # 章课件：一个知识点 6 页（备注都是「卡上限」的长稿）⇒ 该小节必然 > 360 秒
        slides = [(f"{chapter['key']} {chapter['title']}",
                   _note(LONG_NOTE, INTERNAL_NOTE), ["学习目标：掌握发酵"])]
        slides += [(points[0]["title"] if index == 1 else f"{points[0]['title']}（续）",
                    _note(LONG_NOTE), [f"第 {index} 条要点"]) for index in range(1, 7)]
        slides += [("本章回顾", _note(SHORT_NOTE), ["要点回顾"]),
                   ("随堂练习", _note(SHORT_NOTE), ["第1题"])]
        layouts = ["cover"] + ["point"] * 6 + ["review", "quiz"]
        _seed_deck(course_id, title=f"第{chapter['key']}章课件", scope="chapter",
                   chapter_key=chapter["key"], slides=slides, layouts=layouts, pptx=pptx)
    return course_id


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _files(course_id: str) -> dict[str, str]:
    """直接调渲染层取文件（不落盘）：``{相对路径: 文本}``。"""
    db = SessionLocal()
    try:
        return {row["path"]: row["text"]
                for row in kit.build_files(db, db.get(Course, course_id))}
    finally:
        db.close()


def _build(client, course_id: str, payload: dict | None = None) -> dict:
    resp = client.post(f"/api/delivery/course/{course_id}/build",
                       json=payload or {"pdf": "off", "allow_inconsistent": True})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _audit_srt(text: str, cps: float = kit.CPS_LIMIT) -> list[str]:
    """按规格审一遍 SRT：格式 / 递增不重叠 / 单行 ≤42 / ≤2 行 / ≤20 字符每秒。"""
    problems: list[str] = []
    previous_end = -1.0
    for block in re.split(r"\n\s*\n", text.strip()):
        if not block.strip():
            continue
        lines = [line for line in block.split("\n") if line.strip()]
        match = re.match(r"^(\d\d):(\d\d):(\d\d),(\d\d\d) --> (\d\d):(\d\d):(\d\d),(\d\d\d)$",
                         lines[1] if len(lines) > 1 else "")
        if not match or len(lines) < 3:
            problems.append(f"时间轴格式不合法：{lines[1][:40] if len(lines) > 1 else block[:40]}")
            continue
        values = [int(value) for value in match.groups()]
        start = values[0] * 3600 + values[1] * 60 + values[2] + values[3] / 1000
        end = values[4] * 3600 + values[5] * 60 + values[6] + values[7] / 1000
        body = lines[2:]
        chars = sum(len(line) for line in body)
        duration = max(end - start, 0.001)
        if end <= start:
            problems.append(f"cue 时长 ≤0：{start} → {end}")
        if start < previous_end - 1e-6:
            problems.append(f"cue 与上一条重叠：{start} < {previous_end}")
        if chars / duration > cps:
            problems.append(f"阅读速度 {chars / duration:.1f} 字符/秒 > {cps}")
        if max((len(line) for line in body), default=0) > kit.MAX_LINE_CHARS:
            problems.append(f"单行超过 {kit.MAX_LINE_CHARS} 字符")
        if len(body) > kit.MAX_CUE_LINES:
            problems.append(f"一条字幕 {len(body)} 行 > {kit.MAX_CUE_LINES}")
        previous_end = end
    return problems


# ---------------------------------------------------------------------------
# ① 生成条件：场景含 recorded 才出（否则结构与从前逐字一致）
# ---------------------------------------------------------------------------

def test_kit_is_not_built_without_recorded_scenario(client):
    """未选场景 / 只含 classroom / self_study+lms ⇒ 一个文件都不出，也不报缺项。"""
    for selected in ((), ("classroom",), ("self_study", "lms")):
        course_id = _course_with_kit(client, f"不出录课包-{len(selected)}", selected=selected)
        assert _files(course_id) == {}, selected
        built = _build(client, course_id)
        package = delivery_mod.deliveries_root() / built["build_id"]
        assert not (package / kit.DIR_NAME).exists(), selected
        assert built["counts"]["recording_kit_files"] == 0
        assert not any(row["item"].startswith(kit.DIR_NAME) for row in built["missing"])


def test_kit_is_built_when_recorded_and_declared_in_manifest(client):
    """含 recorded ⇒ 多出 `录课包/`：文件齐、manifest 逐条声明（sha1 对得上）、计数一致。"""
    course_id = _course_with_kit(client, "出录课包", selected=("recorded",))
    files = _files(course_id)
    assert all(path.startswith(f"{kit.DIR_NAME}/") for path in files)
    assert {path.split("/")[1] for path in files if path.count("/") == 2} == {
        kit.SCRIPT_DIR, kit.SRT_DIR}
    assert {Path(path).name for path in files if path.count("/") == 1} == {
        kit.DURATION_NAME, kit.STORYBOARD_NAME}
    assert files[f"{kit.DIR_NAME}/{kit.DURATION_NAME}"]
    assert files[f"{kit.DIR_NAME}/{kit.STORYBOARD_NAME}"]
    assert len([path for path in files if path.endswith("-逐字稿.md")]) == 2       # 两个 deck
    assert len([path for path in files if path.endswith(".srt")]) == 2

    built = _build(client, course_id)
    package = delivery_mod.deliveries_root() / built["build_id"]
    on_disk = sorted(path.relative_to(package).as_posix()
                     for path in (package / kit.DIR_NAME).rglob("*") if path.is_file())
    assert on_disk == sorted(files), on_disk
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    declared = {row["path"]: row for row in manifest["files"]}
    for rel in on_disk:
        assert rel in declared, f"{rel} 没进 manifest"
        digest = hashlib.sha1((package / rel).read_bytes()).hexdigest()
        assert digest == declared[rel]["sha1"]
    assert manifest["counts"]["recording_kit_files"] == len(files)
    assert manifest["counts"]["recording_decks"] == 2
    assert manifest["llm_calls"] == 0
    # 说明文件里也要写清「本次多出了什么」，客户不会以为漏了文件
    readme = (package / "00-交付说明.md").read_text(encoding="utf-8")
    assert "录课包/" in readme and "6 分钟闸门" in readme


# ---------------------------------------------------------------------------
# ② 逐字稿：逐页拼装、不改写、内部提示不进时长
# ---------------------------------------------------------------------------

def test_transcript_pages_verbatim_and_internal_note_marked(client):
    course_id = _course_with_kit(client, "逐字稿", selected=("recorded",))
    files = _files(course_id)
    scripts = {path: text for path, text in files.items() if path.endswith("-逐字稿.md")}
    assert len(scripts) == 2
    for path, script in scripts.items():
        assert "未做任何改写" in script
        marks = re.findall(r"【第 (\d+) 页 · ≈(\d+) 秒】", script)
        assert [int(mark[0]) for mark in marks] == list(range(1, len(marks) + 1)), path
        assert script.count(kit.TRANSITION_HEAD) == len(marks) - 1, path
    # 整课课件首页 = 短备注；章课件首页 = 长备注 + 内部提示（内部提示不计进秒数）
    course_deck = next(text for text in scripts.values() if SHORT_NOTE in text)
    chapter_deck = next(text for text in scripts.values() if INTERNAL_NOTE in text)
    assert re.findall(r"≈(\d+) 秒", course_deck)[0] == str(kit.seconds_of(SHORT_NOTE))
    assert re.findall(r"≈(\d+) 秒", chapter_deck)[0] == str(kit.seconds_of(LONG_NOTE))
    assert LONG_NOTE in chapter_deck and SHORT_NOTE in chapter_deck
    assert (kit.INTERNAL_NOTE_PREFIX + INTERNAL_NOTE) in chapter_deck
    srt = next(text for path, text in files.items() if path.endswith(".srt") and "第1章" in path)
    assert "勿投屏" not in srt and "工作台" not in srt


# ---------------------------------------------------------------------------
# ③ 时长表：固定行格式 + 6 分钟闸门 + 总计
# ---------------------------------------------------------------------------

ROW_RE = re.compile(
    r"^- (?P<chapter>\d+)-(?P<point>\d+) (?P<title>.+)：共 (?P<pages>\d+) 页、"
    r"正文 (?P<chars>\d+) 字，约 (?P<seconds>\d+) 秒（≈(?P<minutes>\d+\.\d) 分钟）"
    rf"(?P<warn> {re.escape(kit.SECTION_LIMIT_NOTE)})?$")


def _row_of(markdown: str, key: str) -> re.Match:
    for line in markdown.splitlines():
        match = ROW_RE.match(line)
        if match and f"{match.group('chapter')}-{match.group('point')}" == key:
            return match
    raise AssertionError(f"时长表里没有小节 {key}：\n{markdown}")


def test_duration_rows_format_gate_and_total(client):
    course_id = _course_with_kit(client, "时长表", selected=("recorded",))
    markdown = _files(course_id)[f"{kit.DIR_NAME}/{kit.DURATION_NAME}"]
    rows = [ROW_RE.match(line) for line in markdown.splitlines() if line.startswith("- ")]
    assert rows and all(rows), markdown
    for match in rows:
        chars, seconds = int(match.group("chars")), int(match.group("seconds"))
        assert seconds == math.ceil(chars / kit.CHARS_PER_SECOND), match.group(0)
        assert (seconds > kit.SECTION_LIMIT_SECONDS) == bool(match.group("warn")), match.group(0)
        assert match.group("minutes") == f"{seconds / 60:.1f}"
    gate = _row_of(markdown, "1-1")
    assert gate.group("warn"), "6 页长备注的小节应该触发 6 分钟闸门"
    assert int(gate.group("seconds")) == 6 * kit.seconds_of(LONG_NOTE)
    assert not _row_of(markdown, "1-0").group("warn"), "没超 6 分钟的行不该挂 ⚠️"
    total = int(re.search(r"全 deck 合计 约 (\d+) 秒 / ≈(\d+\.\d) 分钟", markdown).group(1))
    assert total == sum(int(match.group("seconds")) for match in rows)


# ---------------------------------------------------------------------------
# ④ 字幕：格式 / ≤20 字符每秒 / 单行 ≤42 / ≤2 行 / 递增不重叠 / 总长 == 时长表
# ---------------------------------------------------------------------------

def test_subtitles_follow_spec_and_match_duration_total(client):
    course_id = _course_with_kit(client, "字幕", selected=("recorded",))
    files = _files(course_id)
    srts = {Path(path).stem: text for path, text in files.items() if path.endswith(".srt")}
    assert len(srts) == 2
    db = SessionLocal()
    try:
        totals = {entry["deck"].title: sum(page["seconds"] for page in entry["pages"])
                  for entry in kit.collect_entries(db, db.get(Course, course_id))}
    finally:
        db.close()
    for title, text in srts.items():
        assert _audit_srt(text) == [], title
        stamps = re.findall(r"--> (\d\d):(\d\d):(\d\d),(\d\d\d)", text)
        last = stamps[-1]
        seconds = int(last[0]) * 3600 + int(last[1]) * 60 + int(last[2]) + int(last[3]) / 1000
        assert seconds == totals[title], f"{title} 字幕总长与时长表不一致"


def test_cps_gate_catches_widened_constant(client, monkeypatch):
    """反证据（§B3）：把「字/秒」放宽到 30 ⇒ 同一条字幕的阅读速度 > 20 ⇒ 断言变红。"""
    course_id = _course_with_kit(client, "CPS 变异", selected=("recorded",))
    before = {path: text for path, text in _files(course_id).items() if path.endswith(".srt")}
    assert len(before) == 2 and all(_audit_srt(text) == [] for text in before.values())
    monkeypatch.setattr(kit, "CHARS_PER_SECOND", 30.0)
    after = {path: text for path, text in _files(course_id).items() if path.endswith(".srt")}
    assert all(after[path] != text for path, text in before.items()), "放宽字/秒后时长没变 ⇒ 变异没生效"
    problems = [row for text in after.values() for row in _audit_srt(text)]
    assert problems and any("阅读速度" in row for row in problems), problems


# ---------------------------------------------------------------------------
# ⑤ 分镜表 / 互动点
# ---------------------------------------------------------------------------

def test_storyboard_has_one_row_per_page(client):
    course_id = _course_with_kit(client, "分镜表", selected=("recorded",))
    text = _files(course_id)[f"{kit.DIR_NAME}/{kit.STORYBOARD_NAME}"]
    rows = [line for line in text.splitlines() if line.startswith("| 第 ")]
    assert len(rows) == 2 + 9, rows          # 整课 2 页 + 章课件 9 页（封面 + 6 知识点页 + 回顾 + 练习）
    assert "cover · 内嵌图 0 张" in text      # 真 pptx 读回来（这一份没有内嵌图）
    assert "老面发酵依靠多种微生物协同作用" in text     # 知识点页板书 = 讲义关键点
    assert "| — |" in text                    # 非知识点页板书留空
    # 源 pptx 不在（整课课件）⇒ 明写「未核对」，不猜 0 张
    assert "未核对（源 pptx 不在 / 读不回）" in text


def test_interaction_points_need_classroom_or_live(client):
    recorded_only = _course_with_kit(client, "无互动点", selected=("recorded",))
    assert f"{kit.DIR_NAME}/{kit.INTERACTION_NAME}" not in _files(recorded_only)

    mixed = _course_with_kit(client, "有互动点", selected=("recorded", "classroom"))
    files = _files(mixed)
    assert f"{kit.DIR_NAME}/{kit.INTERACTION_NAME}" in files
    text = files[f"{kit.DIR_NAME}/{kit.INTERACTION_NAME}"]
    db = SessionLocal()
    try:
        points = [node for node in list_nodes(db, mixed)
                  if "." in str(node.node_key or "")]
    finally:
        db.close()
    assert text.count("\n## ") == len(points) == 2
    bullets = [line for line in text.splitlines() if line.startswith("- ")]
    assert len(points) <= len(bullets) <= len(points) * 2
    assert any("先答后讲" in line for line in bullets)     # 有随堂练习 ⇒ 第 2 条提问点


# ---------------------------------------------------------------------------
# ⑥ 0-LLM 护栏 + SCORM 按课时打包
# ---------------------------------------------------------------------------

def test_recording_kit_has_no_model_calls():
    """源码级护栏：录课包的渲染层**不许出现**调模型的代码（任务 87 硬规则）。"""
    source = (Path(__file__).resolve().parents[1] / "app" / "services"
              / "recording_kit.py").read_text(encoding="utf-8")
    assert "build_llm_client" not in source and "complete_json" not in source
    assert "LlmClient" not in source and "openai" not in source.lower()


def test_scorm_package_items_carry_metadata_and_selfcheck(client):
    """SCORM：`<item>` 数 == 课时数、每个 item 带标题 / 预计时长 / 知识点 key，自检须通过。"""
    course_id = _course_with_kit(client, "SCORM", selected=("recorded",))
    resp = client.post("/api/publish/export", json={"course_id": course_id, "format": "scorm"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["url"].startswith("/exports/")
    report = body["self_check"]
    assert report["ok"] and report["problems"] == []
    with zipfile.ZipFile(Path(body["file"])) as zf:
        manifest = zf.read("imsmanifest.xml").decode("utf-8")
        names = set(zf.namelist())
    root = ElementTree.fromstring(manifest)
    items = root.findall(".//cp:item", NS)
    db = SessionLocal()
    try:
        lessons = db.query(ContentItem).filter(
            ContentItem.course_id == course_id, ContentItem.kind == "lecture").count()
    finally:
        db.close()
    assert len(items) == lessons == report["items"] == 2
    adlcp = f"{{{ADLCP_NS}}}"
    for item in items:
        assert item.find("cp:title", NS).text
        key_node = item.find(f"{adlcp}datafromlms")
        assert key_node is not None and key_node.text.startswith("node_key=")
        duration = item.find(f"{adlcp}maxtimeallowed")
        assert duration is not None and re.fullmatch(r"\d\d:\d\d:\d\d", duration.text)
    assert report["total_bytes"] > 0 and report["files"] == len(names)
    assert report["estimated_seconds"] == sum(
        publish_mod.lesson_seconds(LECTURE_MD) for _ in range(lessons))


def test_scorm_whole_course_as_one_item_is_caught(client, monkeypatch):
    """反证据（§B3）：把 `<item>` 切成「整课一个」⇒ item 数 != 课时数 ⇒ 规格断言变红。"""
    course_id = _course_with_kit(client, "SCORM 变异", selected=("recorded",))
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        bundle = publish_mod.build_pages(db, course)
        lessons = len(bundle["pages"])
        assert lessons == 2
        merged = {"rows": bundle["rows"][:1], "pages": [bundle["pages"][0]],
                  "index": bundle["index"], "assets": bundle["assets"]}
        monkeypatch.setattr(publish_mod, "build_pages", lambda _db, _course: merged)
        path, report = publish_mod.export_scorm_package_with_report(db, course)
        with zipfile.ZipFile(path) as zf:
            root = ElementTree.fromstring(zf.read("imsmanifest.xml").decode("utf-8"))
        items = root.findall(".//cp:item", NS)
    finally:
        db.close()
    # 正常路径的规格断言是 `len(items) == lessons`；变异后必然不成立 ⇒ 这条断言会红
    assert len(items) != lessons, "整课切一个 item 竟然还等于课时数 —— 规格断言抓不到，必须补"
    assert report["ok"] and report["items"] == 1
