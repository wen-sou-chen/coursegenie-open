"""任务 52：质检按学段/学科分档（``qc_profile``）+ 概念候选「目标/属性短语」收口。

规格见 HANDOFF「任务 49–52 说明」E 段。四条口径：

1. **同一篇小学古诗讲义**，在「小学语文档」下 ``actionable`` 不再误报（**降级为提示**），
   在「职业培训档」下**仍然报** —— 判据一个字没改，只是按场景选了不同口径；
2. ``objective`` 在 K-12 档按**课时目标**校验（目标句没有可观测行为动词 → warn）；
3. **不许放宽阈值刷分**：``_WEIGHT`` / ``CHECK_KEYS`` / ``QC_VERSION`` / ``QC_RULES_VERSION``
   保持原值，降级项显式带 ``downgraded=True`` + ``hint``（原始结论没被抹掉）；
4. 概念库侧（走查缺陷 2）：「字音准确」这类**目标/属性短语**不进概念库，上下位关系
   （翘舌音 → 字音准确）不当别名，老师点名的补抽不被自动判据挡住。

协作规则第 7 条：DATABASE_URL / STORAGE_DIR / OBSIDIAN_VAULT_ROOT 由 conftest 统一指向
临时目录，本文件不碰真实数据库与知识库。
"""
from __future__ import annotations

from app.db.session import SessionLocal
from app.models.course import ContentItem, Course, Outline, OutlineNode
from app.services import concept as concept_service
from app.services import lecture_qc as qc_mod
from app.services import qc_profile
from app.services.lecture import QC_RULES_VERSION
from app.services.lecture_qc import (_WEIGHT, CHECK_KEYS, QC_VERSION, _actionable_check,
                                     _objective_check, compute_qc, qc_outdated)

K12_META = {"stage": "小学", "grade": "二年级", "textbook": "部编版",
            "volume": "上册", "unit": "第一单元", "class_hours": 2}

# 一份很典型的小学古诗课讲义：有讲解、有小结、每条都标了来源，
# 但**没有可照做的操作句** —— 古诗课本来就不该被要求「动作动词 + 判据信号 + 产出物」。
POEM = """## 静夜思

### 核心讲解
- **疑是地上霜**：把洒在地上的月光看成霜，是比喻的写法（来源:《古诗二首》第 1 段）。
- **举头望明月**：抬头看月亮的动作里藏着想家的心情（来源:《古诗二首》第 2 段）。
- **低头思故乡**：一个「低头」把思念落到了具体动作上（来源:《古诗二首》第 3 段）。

### 小结
- 先看景再想情：景是月光，情是想家（来源:《古诗二首》第 4 段）。
"""

POEM_OBJECTIVE = "能朗读并背诵《静夜思》，说出诗里写了什么"
ELEMENTARY = {"title": "古诗三首", "subject": "语文", "audience": "二年级学生"}
VOCATIONAL = {"title": "客服话术实操", "subject": "客服", "audience": "职业培训（上岗前）"}


def _course(*, meta: dict | None = None, **kwargs) -> Course:
    data = {"duration_minutes": 90}
    data.update(ELEMENTARY)
    data.update(kwargs)
    return Course(meta=dict(meta or {}), **data)


def _check(qc: dict, key: str) -> dict:
    return next(c for c in qc["checks"] if c["key"] == key)


def _cached(**extra) -> dict:
    """一份「够新」的缓存质检（用于 qc_outdated 的档位判据）。"""
    row = {"version": QC_VERSION,
           "checks": [{"key": "sources", "level": "ok", "detail": "来源齐全", "sentences": []}]}
    row.update(extra)
    return row


# ---------------------------------------------------------------- A. 档位判定

def test_resolve_maps_stage_and_subject_to_profile():
    """小学语文 → 文科档；小学数学 → 理科档；初中历史 → 文科档；高中物理 → 理科档。"""
    assert qc_profile.resolve(_course(meta=dict(K12_META)))["key"] == qc_profile.K12_HUMANITIES
    assert qc_profile.resolve(_course(
        subject="数学", meta={"stage": "小学", "grade": "三年级"}))["key"] == qc_profile.K12_STEM
    assert qc_profile.resolve(_course(
        subject="历史", audience="七年级", meta={"stage": "初中", "grade": "七年级"}))[
            "key"] == qc_profile.K12_HUMANITIES
    assert qc_profile.resolve(_course(
        subject="物理", meta={"stage": "高中"}))["key"] == qc_profile.K12_STEM


def test_resolve_keeps_legacy_courses_on_default_profile():
    """护栏（最重要的一条）：**没有学段的存量课**（AI 课 / 包子课 / 企业课）一律缺省档。"""
    plain = qc_profile.resolve(_course(title="AI 幻觉治理", subject="职场", audience="客服"))
    assert plain["key"] == qc_profile.DEFAULT_KEY
    assert plain == qc_profile.default()
    assert plain["actionable_tier"] == "required"      # 与改造前逐字一致
    assert plain["objective_mode"] == "coverage"
    # 受众写了「职业培训」但没有学段 ⇒ 职业培训档（照旧严判 actionable）
    train = qc_profile.resolve(_course(**VOCATIONAL))
    assert train["key"] == qc_profile.VOCATIONAL
    assert train["actionable_tier"] == "required"
    # 纯函数：同一个 course 恒得同一个档位
    assert qc_profile.resolve(_course(meta=dict(K12_META))) == qc_profile.resolve(
        _course(meta=dict(K12_META)))


# ---------------------------------------------------------------- B. 误报修复

def test_poem_actionable_is_hint_under_k12_and_warn_under_vocational():
    """验收原文：同一篇小学古诗讲义，两档结论不同；其它 9 项结论逐条一致。"""
    obj = POEM_OBJECTIVE
    humanities = compute_qc(POEM, objective=obj,
                            profile=qc_profile.resolve(_course(meta=dict(K12_META))))
    vocational = compute_qc(POEM, objective=obj, profile=qc_profile.resolve(_course(**VOCATIONAL)))

    hint = _check(humanities, "actionable")
    assert hint["level"] == "ok", hint
    assert hint["downgraded"] is True
    assert hint["hint"]                      # 原始结论原样留着，没被抹掉
    assert "提示" in hint["detail"]
    assert humanities["downgraded_keys"] == ["actionable"]

    assert _check(vocational, "actionable")["level"] == "warn"
    assert "downgraded" not in _check(vocational, "actionable")
    assert "downgraded_keys" not in vocational

    # 只有 actionable 这一项结论不同 —— 分档不是「整篇放水」
    diff = {a["key"]: (a["level"], b["level"])
            for a, b in zip(humanities["checks"], vocational["checks"]) if a["level"] != b["level"]}
    assert diff == {"actionable": ("ok", "warn")}


def test_hint_tier_only_downgrades_warn_not_fail():
    """护栏：``tier="hint"`` 只把 **warn** 降级；判据本身（含 fail 语义）没被改。"""
    warn_poem = _actionable_check(POEM)
    assert warn_poem["level"] == "warn"
    hinted = _actionable_check(POEM, tier="hint")
    assert (hinted["level"], hinted["downgraded"]) == ("ok", True)
    assert hinted["hint"] == warn_poem["detail"]

    # 有步骤、有信号、也有产出物 ⇒ 两档都 ok（降级不是「把不行的变行」的通用开关）
    good = ("### 操作步骤\n"
            "- 打开温度设置，把温度降到 30℃ 以下，输出一份参数记录表\n")
    assert _actionable_check(good)["level"] == "ok"
    assert _actionable_check(good, tier="hint")["level"] == "ok"
    assert "downgraded" not in _actionable_check(good, tier="hint")


# ---------------------------------------------------------------- C. 阈值守卫

def test_thresholds_and_check_keys_are_untouched():
    """护栏：分档不许动权重、判据集合与版本号（否则就是刷分）。"""
    assert _WEIGHT == {"warn": 6, "fail": 20}
    assert CHECK_KEYS == ("structure", "fluff", "length", "objective", "terminology",
                          "figures", "quiz", "sources", "numbers", "substance", "actionable")
    assert QC_VERSION == 3
    assert QC_RULES_VERSION == "qc-v2"
    # 11 项判据一个不多一个不少，降级项仍然在列表里（只是 level 变 ok）
    qc = compute_qc(POEM, objective=POEM_OBJECTIVE,
                    profile=qc_profile.resolve(_course(meta=dict(K12_META))))
    assert [c["key"] for c in qc["checks"]] == list(CHECK_KEYS)
    assert qc["total_checks"] == 11


# ---------------------------------------------------------------- D. 课时目标

def test_objective_lesson_mode_needs_an_observable_verb():
    """K-12 档按课时目标校验；通用档口径与改造前一致（同一个目标仍判 ok）。"""
    vague = "理解古诗的美"
    assert _objective_check(POEM, vague, mode="coverage")["level"] == "ok"
    lesson = _objective_check(POEM, vague, mode="lesson")
    assert lesson["level"] == "warn"
    assert "行为动词" in lesson["detail"]

    # 目标本身就写清了要学生做什么 ⇒ 不再报「没有行为动词」（覆盖度另算，与改造前一致）
    clear = _objective_check(POEM, POEM_OBJECTIVE, mode="lesson")
    assert "行为动词" not in clear["detail"]
    assert (_objective_check(POEM, POEM_OBJECTIVE, mode="lesson")["level"]
            == _objective_check(POEM, POEM_OBJECTIVE, mode="coverage")["level"])
    # 没填目标 / 全是虚词时照旧跳过（48h 口径不变）
    assert _objective_check(POEM, "", mode="lesson")["level"] == "ok"


# ---------------------------------------------------------------- E. 缓存过期

def test_qc_outdated_on_profile_change_but_legacy_cache_stays_valid():
    """档位变了 ⇒ 必须现算；缺 profile 的**老缓存按缺省档**算 ⇒ 存量课不会整体判过期。"""
    assert qc_outdated(_cached(), profile_key=qc_profile.DEFAULT_KEY) is False
    assert qc_outdated(_cached(), profile_key=qc_profile.K12_HUMANITIES) is True
    # 老数据没有 profile 字段：不传档位 / 传缺省档都不算过期
    assert qc_outdated(_cached()) is False
    assert qc_outdated(_cached(), profile_key="") is False
    # 缓存里就是当前档 ⇒ 不过期
    assert qc_outdated(_cached(profile=qc_profile.default()),
                       profile_key=qc_profile.DEFAULT_KEY) is False
    assert qc_outdated(_cached(profile={"key": qc_profile.K12_HUMANITIES}),
                       profile_key=qc_profile.K12_HUMANITIES) is False
    # 其它过期情形不受影响（版本落后 / 缺句子）
    assert qc_outdated({"version": 1, "checks": [{"key": "sources", "level": "ok"}]}) is True
    assert qc_outdated(None) is True


# ---------------------------------------------------------------- F. 概念候选

def test_goal_phrase_is_not_a_concept_candidate():
    """「字音准确」是目标/属性短语，不是术语名词 —— 建了概念页会让质检给出把内容改坏的建议。"""
    reason = concept_service.name_reject_reason("字音准确", set())
    assert reason and "目标/属性短语" in reason
    assert concept_service.name_reject_reason("提高朗读水平", set())
    # 补抽（老师点名）不被这条自动判据挡住：硬校验仍照做
    assert concept_service.name_reject_reason("字音准确", set(), strict_terms=False) is None
    assert concept_service.name_reject_reason("这是一个很长的句子，不像术语", set(),
                                              strict_terms=False)
    # 名词性术语照旧通过
    assert concept_service.name_reject_reason("翘舌音", set()) is None


def test_alias_must_be_a_synonym_not_a_hypernym():
    """别名必须是同义写法；上下位/相关关系（翘舌音 vs 字音准确）宁可少收不可收错。"""
    assert concept_service.alias_reject_reason("翘舌音", "字音准确")            # 上下位
    assert concept_service.alias_reject_reason("五言诗", "古诗")                # 上下位（名词）
    assert concept_service.alias_reject_reason("翘舌音", "五言诗")
    assert concept_service.alias_reject_reason("五言诗", "五言") is None        # 同义（共享词头）
    assert concept_service.alias_reject_reason("换词法", "替换法") is None      # 同义（共享 2 字）
    assert concept_service.alias_reject_reason("翘舌音", "翘舌音")              # 与标准名相同


def test_concept_pipeline_drops_goal_phrase_and_bad_alias():
    """整条管线：目标/属性短语不进概念库，坏别名进 ``dropped`` 供显式上报。"""
    corpus = {
        "nodes": [
            {"key": "1.1", "title": "读准字音", "chapter": "1", "points": ["翘舌音：zh/ch/sh"]},
            {"key": "1.2", "title": "朗读指导", "chapter": "1", "points": ["翘舌音要读到位"]},
        ],
        "materials": [],
    }
    dropped: list[dict] = []
    out = concept_service.merge_raw([
        {"name": "字音准确", "definition": "读得准确。", "chapters": ["1.1", "1.2"]},
        {"name": "翘舌音", "aliases": ["字音准确", "古诗"],
         "definition": "舌尖上翘发出的音。", "chapters": ["1.1"]},
    ], corpus, dropped)

    assert [c["name"] for c in out] == ["翘舌音"]
    assert out[0]["aliases"] == []
    reasons = {(d["name"], d.get("kind")): d["reason"] for d in dropped}
    assert "目标/属性短语" in reasons[("字音准确", None)]
    assert "别名不是名词性术语" in reasons[("字音准确", "alias")]
    assert "看不出同义" in reasons[("古诗", "alias")]


def test_supplement_still_bypasses_goal_phrase_rule():
    """补抽口径不变：老师点名的概念只做硬校验（人工确认优先于自动准入）。"""
    corpus = {"nodes": [{"key": "1.1", "title": "读准字音", "chapter": "1"}], "materials": []}
    raw = [{"name": "字音准确", "definition": "读得准确。", "chapters": ["1.1"]}]
    assert concept_service.merge_raw(raw, corpus) == []
    out = concept_service.merge_raw(raw, corpus, enforce_admission=False)
    assert [c["name"] for c in out] == ["字音准确"]


# ---------------------------------------------------------------- G. 链路接线

def test_build_for_item_plumbs_profile_into_course_report():
    """课上写着 K-12 ⇒ 讲义 `meta.qc` 里带档位，整课报告里能说清「这条为什么不算问题」。"""
    db = SessionLocal()
    try:
        course = _course(meta=dict(K12_META))
        db.add(course)
        db.commit()
        db.refresh(course)
        outline = Outline(course_id=course.id, title="第一单元")
        db.add(outline)
        db.commit()
        db.refresh(outline)
        node = OutlineNode(outline_id=outline.id, node_key="1.1", node_type="point",
                           title="静夜思", objective=POEM_OBJECTIVE,
                           teaching_strategy="范读 + 跟读", assessment="抽读")
        db.add(node)
        db.commit()
        db.refresh(node)
        item = ContentItem(course_id=course.id, node_id=node.id, kind="lecture",
                           title="1.1 静夜思", body={"markdown": POEM}, meta={})
        db.add(item)
        db.commit()
        db.refresh(item)

        built = qc_mod.build_for_item(db, course, node, item, POEM)
        assert built["qc"]["profile"]["key"] == qc_profile.K12_HUMANITIES
        assert built["qc"]["downgraded_keys"] == ["actionable"]

        qc_mod.attach(built, item)
        db.commit()
        db.refresh(item)

        report = qc_mod.course_report(db, course)
        assert report["profile"]["label"] == "K-12 文科档"
        assert report["downgraded_items"] == 1
        assert "按提示" in report["downgraded_notice"]
        row = report["items"][0]
        assert row["skipped_keys"] == ["terminology"]      # 概念库为空（与改造前一致）
        assert [d["key"] for d in row["downgraded"]] == ["actionable"]
        assert row["downgraded"][0]["hint"]
    finally:
        db.close()
