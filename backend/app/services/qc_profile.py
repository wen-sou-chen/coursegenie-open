"""质检档位（任务 52）：按学段/学科选判据口径，而不是把所有课都按职业培训课严判。

背景（DSH 2026-09-17 走查）：小学语文古诗课的 ``actionable`` 判 warn，要求「动作动词 +
判断信号 + 产出物」——那是**职业培训实操课**的口径。对一节古诗课，"可照做的操作句"不是
合理要求；照原样判 warn 只会让老师学会忽略质检。

两条铁律：
1. **分档是「按场景选口径」，不是把 warn 改 ok 刷分**。阈值一个没动（``_WEIGHT`` /
   ``CHECK_KEYS`` / ``QC_VERSION`` 全不变），只是「这一档要不要这门判据」；
   降级为提示的项在结果里显式带 ``downgraded=True`` 与 ``hint``，报告里单列。
2. **缺省档 = 现状**：没有学段元数据的课（AI 课 / 包子课 / 企业课）一律走 ``default``，
   判据与分数与改造前逐字一致。

档位表（判据写死在代码里，用例守护，见 ``backend/docs/task52-qc-profiles.md``）:

| 档位 | 命中条件 | actionable | objective |
| --- | --- | --- | --- |
| `k12-humanities` | 学段在 小学/初中/高中 且（小学 或 学科属文科） | **降级为提示** | 按课时目标判（额外要求目标可观测） |
| `k12-stem` | 学段在 小学/初中/高中 且学科属理科 | 照旧 required | 按课时目标判 |
| `vocational` | 无学段且受众/学科/标题含 职业/培训/实训/岗位 等 | 照旧 required | 照旧 coverage |
| `default` | 其余（含全部存量课） | 照旧 required | 照旧 coverage |
"""
from __future__ import annotations

from app.services import k12 as k12_mod

DEFAULT_KEY = "default"
K12_HUMANITIES = "k12-humanities"
K12_STEM = "k12-stem"
VOCATIONAL = "vocational"

K12_STAGES = ("小学", "初中", "高中")

HUMANITIES_SUBJECTS = ("语文", "英语", "历史", "地理", "政治", "道德与法治", "思想品德",
                       "思想政治", "音乐", "美术", "艺术", "书法", "作文", "阅读",
                       "心理健康", "劳动")
STEM_SUBJECTS = ("数学", "物理", "化学", "生物", "科学", "信息技术", "信息科技",
                 "通用技术", "技术与工程", "人工智能")

VOCATIONAL_HINTS = ("职业", "培训", "实训", "岗位", "上岗", "技能鉴定", "企业内训",
                    "岗前", "师傅", "带教", "产线", "工位")

LABELS = {
    DEFAULT_KEY: "通用档",
    K12_HUMANITIES: "K-12 文科档",
    K12_STEM: "K-12 理科档",
    VOCATIONAL: "职业培训档",
}

NOTES = {
    DEFAULT_KEY: "按现有通用口径判（与改造前逐字一致）。",
    K12_HUMANITIES: ("文科/低学段不强制「可照做的操作句」：该项若不合格按**提示**呈现，"
                     "不扣分、不刷分；其余 9 项照旧严判。"),
    K12_STEM: "理科与职教同样要能照着做，actionable 照旧严判。",
    VOCATIONAL: "职业培训/实操课必须给出可照做的操作说明，actionable 照旧严判。",
}


def default() -> dict:
    """缺省档（存量课与无法判定学段/学科的课）。"""
    return {"key": DEFAULT_KEY, "label": LABELS[DEFAULT_KEY], "stage": "", "subject": "",
            "actionable_tier": "required", "objective_mode": "coverage",
            "note": NOTES[DEFAULT_KEY]}


def _vocational(course) -> bool:
    haystack = " ".join(str(getattr(course, name, "") or "")
                        for name in ("subject", "audience", "title"))
    return any(hint in haystack for hint in VOCATIONAL_HINTS)


def resolve(course) -> dict:
    """把课程元数据映射成档位（纯函数；同一个 course 恒得同一个档位）。"""
    meta = k12_mod.k12_meta(course)
    stage = str(meta.get("stage") or "").strip()
    subject = str(getattr(course, "subject", "") or "").strip()
    if not stage and _vocational(course):
        return {"key": VOCATIONAL, "label": LABELS[VOCATIONAL], "stage": "", "subject": subject,
                "actionable_tier": "required", "objective_mode": "coverage",
                "note": NOTES[VOCATIONAL]}
    if stage not in K12_STAGES:
        return default()
    # 小学整个学段都算「低学段」；初高中则看学科是不是文科
    low = stage == "小学"
    humanities = any(word in subject for word in HUMANITIES_SUBJECTS) or not subject
    stem = any(word in subject for word in STEM_SUBJECTS)
    if stem and not humanities:
        key, tier = K12_STEM, "required"
    else:
        key = K12_HUMANITIES if (low or humanities) else K12_STEM
        tier = "hint" if key == K12_HUMANITIES else "required"
    return {"key": key, "label": LABELS[key], "stage": stage, "subject": subject,
            "actionable_tier": tier, "objective_mode": "lesson", "note": NOTES[key]}
