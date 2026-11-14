"""任务 86 §B1：**生成时的口径**标记（内容条目 meta / 笔记 frontmatter / 交付说明）测试。

为什么要有这条：用户 2026-09-23 反馈「点了场景**没看到哪里有变化**」。场景其实生成了内容，
但**没有任何地方**记着「这一篇是按什么口径生成的」—— 内容条目 `meta` 没这项、笔记库
frontmatter 也没有、界面上更没有。本文件钉住这套标记的五条口径：

1. `run_meta`：**未选场景 ⇒ 不写 `scenario_used`**（老课行为与从前一字不差）；
   没有提示词版本的产物（大纲）也**不许硬造版本号**；
2. `badge_of`：没有标记一律回「**通用(未标记)**」—— **不许猜**、不许默认成某个场景；
   有标记时取**主场景**的短标；
3. 笔记库 frontmatter：`scenario`（中文标签，未标记写「通用」）+ `prompt_version`（未标记写「未记录」）；
4. 交付说明那行汇总：`N 篇：录课向 N` / `混合：录课向 5、通用(未标记) 1`；
5. **真的生成一篇**（mock LLM）⇒ 条目 `meta` 里有口径与生成时间；没选场景的课则两个键都不出现。
"""
from __future__ import annotations

from app.db.session import SessionLocal
from app.models.course import Course
from app.services import delivery as delivery_mod
from app.services import kb as kb_mod
from app.services import scenario as sc
from app.services.lecture import PROMPT_TEMPLATE_VERSION

RECORDED = {"scenario": {"primary": "recorded", "selected": ["recorded"]}}
MIXED = {"scenario": {"primary": "recorded", "selected": ["recorded", "lms"]}}
WORK_META = {"kb": {"vault_root": "D:/vault", "output_rel": "课程/x"}}
UNMARKED = sc.UNMARKED_BADGE          # 通用(未标记)
ROAD = {"primary": "recorded", "selected": ["recorded"]}


def _plan(client, course_id: str) -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "初中生物光合作用", "num_chapters": 2,
    })
    assert resp.status_code == 200, resp.text
    assert client.post("/api/plan/approve",
                       json={"course_id": course_id, "approved": True}).status_code == 200
    return resp.json()


def _leaf(plan: dict) -> str:
    """mock 大纲不一定给出知识点层 ⇒ 退到第一个节点（与 test_lecture 同款兜底）。"""
    leaf = next((n for n in plan["nodes"] if "." in n["key"]), None)
    return (leaf or plan["nodes"][0])["id"]


# ---------------------------------------------------------------- A. run_meta 的空值语义

def test_run_meta_writes_nothing_when_scenario_unselected():
    """**未选场景 ⇒ 一个键都不写**（老课生成口径与从前逐字一致，也不留「空标记」）。"""
    for source in ({}, WORK_META, {"scenario": None}, {"scenario": {}},
                   {"scenario": {"selected": []}}, {"scenario": {"selected": ["不认识"]}},
                   Course(title="x", meta=dict(WORK_META))):
        assert sc.run_meta(source) == {}, source
        # 有版本也不写场景键；场景没有就只写版本
        assert sc.run_meta(source, PROMPT_TEMPLATE_VERSION) ==             {sc.PROMPT_VERSION_KEY: PROMPT_TEMPLATE_VERSION}, source


def test_run_meta_writes_the_normalized_block_and_version():
    assert sc.run_meta(MIXED) == {
        sc.SCENARIO_USED_KEY: {"primary": "recorded", "selected": ["recorded", "lms"]}}
    assert sc.run_meta(RECORDED, "lecture-v14") == {
        sc.SCENARIO_USED_KEY: {"primary": "recorded", "selected": ["recorded"]},
        sc.PROMPT_VERSION_KEY: "lecture-v14",
    }
    # 大纲没有自己的提示词版本 ⇒ 只记场景，**不硬造版本号**
    assert sc.run_meta(RECORDED) == {sc.SCENARIO_USED_KEY:
                                     {"primary": "recorded", "selected": ["recorded"]}}
    # 脏数据（primary 不在 selected）也要自洽修正后再落库
    assert sc.run_meta({"scenario": {"primary": "lms", "selected": ["recorded"]}}) == {
        sc.SCENARIO_USED_KEY: {"primary": "recorded", "selected": ["recorded"]}}


# ---------------------------------------------------------------- B. 徽标：不许猜

def test_badge_of_never_guesses_for_unmarked_content():
    """⚠️ 本任务最要紧的一条：老内容没有标记 ⇒ **一律「通用(未标记)」**。"""
    for meta in ({}, None, {"scenario_used": None}, {"scenario_used": {}},
                 {"scenario_used": {"selected": []}}, {"scenario_used": {"selected": ["nope"]}},
                 {"vault": {"state": "synced"}}, []):
        assert sc.badge_of(meta) == UNMARKED, meta
    assert UNMARKED == "通用(未标记)"


def test_badge_of_takes_the_primary_scenario():
    assert sc.badge_of({sc.SCENARIO_USED_KEY: ROAD}) == "录课向"
    assert sc.badge_of({sc.SCENARIO_USED_KEY: {"primary": "classroom",
                                               "selected": ["classroom", "live"]}}) == "课堂向"
    assert sc.badge_of({sc.SCENARIO_USED_KEY: {"primary": "lms", "selected": ["lms"]}}) == "出包向"
    for key, badge in (("classroom", "课堂向"), ("live", "直播向"), ("recorded", "录课向"),
                       ("self_study", "自学向"), ("lms", "出包向")):
        assert sc.badge(key) == badge
    assert sc.badge("nope") == "" and sc.badge(None) == ""


def test_used_meta_and_prompt_version_readers():
    assert sc.used_meta({}) == {}
    assert sc.used_meta({"scenario_used": "不是字典"}) == {}
    assert sc.used_meta({sc.SCENARIO_USED_KEY: {"primary": "lms",
                                               "selected": ["recorded", "nope", "recorded"]}}) == ROAD
    assert sc.prompt_version_of({}) == ""
    assert sc.prompt_version_of({sc.PROMPT_VERSION_KEY: "lecture-v14"}) == "lecture-v14"


# ---------------------------------------------------------------- C. 笔记库 frontmatter

def test_frontmatter_fields_labels_and_placeholders():
    assert sc.frontmatter_fields({}) == [("scenario", "通用"), ("prompt_version", "未记录")]
    assert sc.frontmatter_fields({sc.SCENARIO_USED_KEY: ROAD,
                                  sc.PROMPT_VERSION_KEY: "lecture-v14"}) ==         [("scenario", "录成视频课"), ("prompt_version", "lecture-v14")]
    # 中文标签取**显示名**（不是短标）：frontmatter 是给人读的
    assert sc.frontmatter_fields({sc.SCENARIO_USED_KEY: {"primary": "self_study",
                                                         "selected": ["self_study"]}})[0] == \
        ("scenario", "学生自学")


def test_kb_frontmatter_renders_scenario_and_prompt_version():
    """真渲染一遍 `kb._frontmatter`：导出后的笔记页必须看得出口径。"""
    marked = kb_mod._frontmatter([("title", "1.1 光合作用"),
                                  *sc.frontmatter_fields({sc.SCENARIO_USED_KEY: ROAD,
                                                          sc.PROMPT_VERSION_KEY: "lecture-v14"})])
    assert marked[0] == "---" and marked.count("---") == 2, marked
    assert 'scenario: "录成视频课"' in marked
    assert 'prompt_version: "lecture-v14"' in marked

    plain = kb_mod._frontmatter([("title", "1.1 光合作用"), *sc.frontmatter_fields({})])
    assert 'scenario: "通用"' in plain
    assert 'prompt_version: "未记录"' in plain


# ---------------------------------------------------------------- D. 交付说明汇总

def test_tally_single_and_mixed():
    roads = [{sc.SCENARIO_USED_KEY: ROAD}] * 6
    assert sc.tally(roads) == "6 篇：录课向 6"
    mixed = [{sc.SCENARIO_USED_KEY: ROAD}] * 5 + [{}]
    assert sc.tally(mixed) == "混合：录课向 5、通用(未标记) 1"
    assert sc.tally([{}, {}]) == "2 篇：通用(未标记) 2"
    assert sc.tally([]) == "" and sc.tally(None) == ""


def test_readme_carries_the_voice_tally_line(client, course_id):
    """交付说明里真的写出了那行「本包各篇的口径」（渲染 `_readme_lines` 真实现）。"""
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        plan = {"counts": {"chapters": 2, "points": 4, "lectures": 6, "quizzes": 12,
                           "decks": 3, "figures": 5, "notes_files": 0},
                "voice_tally": sc.tally([{sc.SCENARIO_USED_KEY: ROAD}] * 5 + [{}])}
        lines = delivery_mod._readme_lines(db, course, plan, {"model": "mock"},
                                          [], [], "20260923-0000")
        assert "- 本包各篇的口径：混合：录课向 5、通用(未标记) 1" in lines

        # 没算出汇总时**不许**留一行空壳
        plan["voice_tally"] = ""
        lines = delivery_mod._readme_lines(db, course, plan, {"model": "mock"},
                                          [], [], "20260923-0000")
        assert not any(line.startswith("- 本包各篇的口径") for line in lines)
    finally:
        db.close()


# ---------------------------------------------------------------- E. 端到端：真的生成一篇

def test_lecture_meta_records_used_scenario_and_generation_time(client, course_id, gen_lecture_row):
    client.patch(f"/api/course/{course_id}", json={"meta": MIXED})
    plan = _plan(client, course_id)
    row = gen_lecture_row(course_id, _leaf(plan))
    meta = row["meta"]
    assert meta[sc.SCENARIO_USED_KEY] == {"primary": "recorded", "selected": ["recorded", "lms"]}
    assert meta[sc.PROMPT_VERSION_KEY] == PROMPT_TEMPLATE_VERSION
    assert meta["generated_at"]                       # 徽标 hover 的「生成时间」
    assert sc.badge_of(meta) == "录课向"


def test_lecture_meta_has_no_scenario_when_course_unselected(client, course_id, gen_lecture_row):
    """没选场景的课：**不写**场景键（老课行为不变），徽标回「通用(未标记)」。"""
    plan = _plan(client, course_id)
    row = gen_lecture_row(course_id, _leaf(plan))
    meta = row["meta"]
    assert sc.SCENARIO_USED_KEY not in meta
    assert sc.badge_of(meta) == UNMARKED
    # 版本仍然记（讲义模板版本与该课选没选场景无关）
    assert meta[sc.PROMPT_VERSION_KEY] == PROMPT_TEMPLATE_VERSION
