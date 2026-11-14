"""任务 85：课程「使用场景」（五选多 + 主场景）与「场景进提示词」测试。

四条口径（只加不改）：
1. 场景放 `course.meta.scenario`（**不改表结构**），`CourseOut.scenario` 透出；
2. **未选择 ⇒ 提示词一字不加** —— 讲义 / 大纲 / 课件口播稿三处的拼装结果与基线**逐字相同**，
   而且**绝不把缺省补成 `classroom`**（改错了这条，所有老课的行为都会变）；
3. 场景**只改口气与要求**（口语态 / 独立成段 / 书面态），不动判定口径、不动 `QC_*`；
4. 提示词版本 `lecture-v13 → lecture-v14`（进输入指纹，否则改场景不会重生成）。
"""
from __future__ import annotations

from app.db.session import SessionLocal
from app.models.course import Course, OutlineNode
from app.schemas.plan import PlanRequest
from app.services import planner as planner_mod
from app.services import ppt
from app.services import scenario as sc
from app.services.lecture import (
    PROMPT_TEMPLATE_VERSION, _lecture_user_prompt, input_fingerprint,
)
from app.services.ppt_layout import NOTES_LIMIT, SPEAK_MIN

WORK_META = {"kb": {"vault_root": "D:/vault", "output_rel": "课程/x"},
             "plan_params": {"goal": "让客服看懂幻觉"}}
CLASSROOM = {"scenario": {"primary": "classroom", "selected": ["classroom"]}}
LIVE = {"scenario": {"primary": "live", "selected": ["live"]}}
RECORDED = {"scenario": {"primary": "recorded", "selected": ["recorded"]}}
SELF_STUDY = {"scenario": {"primary": "self_study", "selected": ["self_study"]}}
LMS_ONLY = {"scenario": {"primary": "lms", "selected": ["lms"]}}
MIXED = {"scenario": {"primary": "classroom", "selected": ["classroom", "recorded"]}}


def _node() -> OutlineNode:
    return OutlineNode(node_key="1.1", node_type="point", title="什么是幻觉",
                       objective="能说出幻觉的两种形态",
                       teaching_strategy="案例对比", assessment="口头举例")


# ---------------------------------------------------------------- A. 纯函数与空值语义

def test_scenario_meta_normalizes_and_keeps_order():
    assert sc.scenario_meta(MIXED) == {"primary": "classroom", "selected": ["classroom", "recorded"]}
    assert sc.scenario_meta({"scenario": {"primary": "lms", "selected": [
        "lms", "nope", "classroom", "lms"]}}) == {"primary": "lms", "selected": ["classroom", "lms"]}
    assert sc.scenario_meta(Course(title="AI 幻觉治理", meta=dict(RECORDED)))["primary"] == "recorded"
    assert sc.scenario_meta({"scenario": {"primary": "recorded", "selected": ["recorded"],
                                          "updated_at": "2026-09-23T17:00:00+08:00"}})["updated_at"] \
        == "2026-09-23T17:00:00+08:00"


def test_unselected_scenario_is_empty_and_never_defaults_to_classroom():
    """⚠️ 本任务最要紧的一条：缺失 / 空 ⇒ 未选择；**不许偷偷补 classroom**。"""
    for source in ({}, WORK_META, {"scenario": None}, {"scenario": {}}, {"scenario": []},
                   {"scenario": {"selected": []}}, {"scenario": {"selected": ["不认识"]}},
                   Course(title="AI 幻觉治理", meta=dict(WORK_META))):
        assert sc.scenario_meta(source) == {}, source
        assert sc.is_selected(source) is False, source
        assert sc.tone_keys(source) == [], source
        assert sc.describe(source) == "", source
        assert sc.scenario_prompt_block(source) == "", source


def test_primary_must_be_one_of_selected():
    """`primary` 不在 `selected` 里（脏数据 / 前端漏改）时自洽修正，绝不生出空 primary。"""
    assert sc.scenario_meta({"scenario": {"primary": "lms",
                                          "selected": ["recorded"]}})["primary"] == "recorded"
    assert sc.scenario_meta({"scenario": {"selected": ["recorded"]}})["primary"] == "recorded"
    assert sc.scenario_meta({"scenario": {"primary": "recorded", "selected": "recorded"}}) \
        == {"primary": "recorded", "selected": ["recorded"]}


def test_five_keys_and_copy_are_frozen():
    """key 集合与「显示名 / 一句话说明」冻结（前端 scripts/check-scenario.ts 逐条比同一张表）。"""
    assert sc.SCENARIO_KEYS == ("classroom", "live", "recorded", "self_study", "lms")
    assert [sc.label(key) for key in sc.SCENARIO_KEYS] == [
        "线下课堂面授", "线上直播", "录成视频课", "学生自学", "平台投放(SCORM)"]
    assert sc.hint("lms") == "只影响出包形态，不影响口气"
    assert sc.label("不认识") == "" and sc.hint("不认识") == ""


def test_describe_lists_all_selected_with_primary():
    assert sc.describe(CLASSROOM) == "线下课堂面授（主：线下课堂面授）"
    assert sc.describe(MIXED) == "线下课堂面授 + 录成视频课（主：线下课堂面授）"


# ---------------------------------------------------------------- B. 场景要求正文（口气）

def test_lms_only_does_not_touch_tone():
    """`lms` 只影响出包形态（任务 87）⇒ 只选它时提示词一字不加。"""
    assert sc.requirement("lms") == ""
    assert sc.tone_keys(LMS_ONLY) == []
    assert sc.scenario_prompt_block(LMS_ONLY) == ""
    # 选了口气场景 + lms ⇒ lms 只出现在「本次使用场景」的名单里，**不产生要求行**
    mixed = sc.scenario_prompt_block(
        {"scenario": {"primary": "classroom", "selected": ["classroom", "lms"]}})
    assert "平台投放(SCORM)（主：线下课堂面授）" in mixed, "名单里要如实列出已选项"
    assert "【主场景】平台投放" not in mixed and "【叠加要求】平台投放" not in mixed


def test_classroom_and_live_ask_for_spoken_tone_and_interaction():
    for meta in (CLASSROOM, LIVE):
        block = sc.scenario_prompt_block(meta)
        assert "口语态" in block, meta
        assert "互动" in block, meta
    assert "我们来看" in sc.scenario_prompt_block(CLASSROOM)
    assert "对着镜头" in sc.scenario_prompt_block(LIVE)


def test_recorded_asks_for_six_minute_segments_and_no_deixis():
    block = sc.scenario_prompt_block(RECORDED)
    assert "可独立成段" in block
    assert "1400–1800 字" in block
    assert "6 分钟" in block
    assert "不依赖画面 / 手势指代" in block
    assert "看这张图右下角" in block
    assert "念出来" in block


def test_self_study_forbids_forward_references():
    """验收 C3 的正则（`上节课|刚才|这个我们`）扫的是产出正文 ⇒ 提示词里必须点名禁止。"""
    block = sc.scenario_prompt_block(SELF_STUDY)
    assert "书面态" in block
    assert "不许依赖前文指代" in block
    assert "上节课讲过" in block and "刚才那个" in block and "这个我们" in block
    assert "自测" in block


def test_multi_select_stacks_requirements_under_the_primary():
    """多选：primary 定口气，其余场景作为**附加要求**叠加（§B2）。"""
    block = sc.scenario_prompt_block(MIXED)
    assert "【主场景】线下课堂面授" in block and "口语态" in block
    assert "【叠加要求】录成视频课" in block and "可独立成段" in block
    reversed_block = sc.scenario_prompt_block(
        {"scenario": {"primary": "recorded", "selected": ["classroom", "recorded"]}})
    assert "【主场景】录成视频课" in reversed_block and "【叠加要求】线下课堂面授" in reversed_block


# ---------------------------------------------------------------- C. 三处接线：讲义 / 大纲 / 口播稿

def test_lecture_prompt_is_byte_identical_when_scenario_unselected():
    """未选场景 ⇒ 与「从前那条不传 course 的基线」**逐字相同**（空值语义的可断言形态）。"""
    baseline = _lecture_user_prompt(_node(), "正式")
    for meta in ({}, WORK_META, {"scenario": {}}, {"scenario": {"selected": []}}, LMS_ONLY):
        assert _lecture_user_prompt(_node(), "正式", course=Course(title="AI 幻觉治理",
                                                                  meta=dict(meta))) == baseline, meta
    assert "场景要求" not in baseline


def test_lecture_prompt_differs_verbatim_by_scenario():
    """验收 C2：同一门课同一素材，classroom 与 self_study 的讲义 prompt 只差场景段。"""
    classroom = _lecture_user_prompt(_node(), "正式", course=Course(meta=dict(CLASSROOM)))
    self_study = _lecture_user_prompt(_node(), "正式", course=Course(meta=dict(SELF_STUDY)))
    assert classroom != self_study
    assert "口语态" in classroom and "口语态" not in self_study
    assert "书面态" in self_study and "书面态" not in classroom
    assert classroom.replace(sc.scenario_prompt_block(CLASSROOM), "") == \
        self_study.replace(sc.scenario_prompt_block(SELF_STUDY), "")


def test_input_fingerprint_moves_with_scenario():
    node = _node()
    assert input_fingerprint(node=node) == input_fingerprint(node=node, scenario="")
    assert input_fingerprint(node=node, scenario="A") != input_fingerprint(node=node, scenario="")
    assert PROMPT_TEMPLATE_VERSION == "lecture-v14"


def _capture_outline_prompts(monkeypatch, courses: list[Course]) -> list[str]:
    seen: list[str] = []

    def fake_complete(client, system, user, **kwargs):
        seen.append(user)
        return {"title": "AI 幻觉治理", "goal": "看懂幻觉", "nodes": []}

    monkeypatch.setattr(planner_mod.usage_log, "complete_json", fake_complete)
    db = SessionLocal()
    try:
        for course in courses:
            db.add(course)
        db.commit()
        for course in courses:
            db.refresh(course)
            planner_mod.generate_outline(db, course, PlanRequest(
                course_id=course.id, goal="看懂幻觉", num_chapters=2))
    finally:
        db.close()
    return seen


def test_outline_prompt_carries_scenario_and_is_unchanged_when_unselected(monkeypatch):
    base, plain, picked = _capture_outline_prompts(monkeypatch, [
        Course(title="AI 幻觉治理", subject="职场", audience="客服", duration_minutes=120,
               meta=dict(WORK_META)),
        Course(title="AI 幻觉治理", subject="职场", audience="客服", duration_minutes=120,
               meta={"scenario": {}}),
        Course(title="AI 幻觉治理", subject="职场", audience="客服", duration_minutes=120,
               meta=dict(SELF_STUDY)),
    ])
    assert base == plain, "未选场景的大纲 prompt 必须与基线逐字相同"
    assert "场景要求" not in base
    assert "场景要求" in picked and "不许依赖前文指代" in picked
    assert picked.replace(sc.scenario_prompt_block(SELF_STUDY), "") == base


def _course_with_nodes(db) -> tuple[Course, list[OutlineNode]]:
    course = Course(title="录课样品", subject="职场", audience="客服", duration_minutes=60)
    db.add(course)
    db.commit()
    db.refresh(course)
    planner_mod.generate_outline(db, course, PlanRequest(
        course_id=course.id, goal="看懂幻觉", num_chapters=2))
    return course, planner_mod.list_nodes(db, course.id)


def test_course_slides_notes_change_exactly_by_the_async_table():
    db = SessionLocal()
    try:
        course, nodes = _course_with_nodes(db)
        slides, _ = ppt.course_slides(db, course, nodes)
        course.meta = {**(course.meta or {}), **RECORDED}
        recorded, _ = ppt.course_slides(db, course, nodes)
    finally:
        db.close()
    assert [s["notes"] for s in slides] != [s["notes"] for s in recorded], "录课向必须真的改了备注"
    for before, after in zip(slides, recorded):
        expected = before["notes"]
        for old, new in ppt._SCENARIO_ASYNC_REPLACE:
            expected = expected.replace(old, new)
        assert after["notes"] == expected, after["title"]
        assert SPEAK_MIN <= len(after["notes"]) <= NOTES_LIMIT, (after["title"], len(after["notes"]))


def test_notes_style_follows_primary_only():
    assert ppt.notes_scenario_style(None) == ""
    assert ppt.notes_scenario_style({}) == ""
    assert ppt.notes_scenario_style(LMS_ONLY) == ""
    assert ppt.notes_scenario_style(CLASSROOM) == ""
    assert ppt.notes_scenario_style(LIVE) == ""
    assert ppt.notes_scenario_style(RECORDED) == "recorded"
    assert ppt.notes_scenario_style(SELF_STUDY) == "self_study"
    assert ppt.notes_scenario_style(MIXED) == ""


def test_async_replacements_are_never_shorter_than_the_originals():
    """换句不许让口播稿变短（否则会跌破 `SPEAK_MIN` 的补白口径）。"""
    for old, new in ppt._SCENARIO_ASYNC_REPLACE:
        assert len(new) >= len(old), (old, new)
    assert len(ppt._NOTE_OPENING_WRITTEN) >= len(ppt._NOTE_OPENING_SPOKEN)


def test_apply_scenario_notes_writes_written_opening_only_for_self_study():
    self_study = [{"title": "P1", "notes": "这一页我们来看「什么是幻觉」。第1条，先看定义。"}]
    ppt.apply_scenario_notes(self_study, SELF_STUDY)
    assert self_study[0]["notes"].startswith("本页要讲清楚的是「")
    classroom = [{"title": "P1", "notes": "这一页我们来看「什么是幻觉」。第1条，先看定义。"}]
    ppt.apply_scenario_notes(classroom, CLASSROOM)
    assert classroom[0]["notes"] == "这一页我们来看「什么是幻觉」。第1条，先看定义。"


# ---------------------------------------------------------------- D. 接口透出与写入

def test_course_api_exposes_scenario_and_patch_merges_meta(client, course_id):
    assert client.get(f"/api/course/{course_id}").json()["scenario"] == {}
    client.patch(f"/api/course/{course_id}", json={"meta": {"kb": {"vault_root": "D:/vault"}}})
    resp = client.patch(f"/api/course/{course_id}", json={"meta": {
        "scenario": {"primary": "recorded", "selected": ["recorded", "lms"],
                     "updated_at": "2026-09-23T17:00:00+08:00"}}})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scenario"] == {"primary": "recorded", "selected": ["recorded", "lms"],
                               "updated_at": "2026-09-23T17:00:00+08:00"}
    assert body["meta"]["kb"] == {"vault_root": "D:/vault"}, "场景写入不许清掉 kb 绑定"
    assert "scenario" in client.get("/api/course/").json()[0]["meta"], "列表也带（列表只剥 kb）"
    cleared = client.patch(f"/api/course/{course_id}", json={"meta": {"scenario": {}}}).json()
    assert cleared["scenario"] == {}
