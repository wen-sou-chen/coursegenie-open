"""课程级「使用场景」元数据（任务 85）：五选多 + 主场景。

数据落在 ``course.meta.scenario``（**不改表结构**）：

    {"scenario": {"primary": "recorded",
                  "selected": ["classroom", "recorded"],
                  "updated_at": "2026-09-23T17:00:00+08:00"}}

⚠️ **空值语义（本模块最要紧的一条）**：缺失 / 空 ⇒ 视为「**未选择**」，
:func:`scenario_prompt_block` 返回**空串** —— 老课的提示词一字不加、生成口径与今天逐字一致。
**不要把缺省偷偷补成 ``classroom``**（否则所有老课的行为都会变，且无从断言）。

场景进提示词**只改「口气与要求」，不动判定口径**：五个 key 里 ``lms`` 只影响出包形态
（渲染层留给任务 87）⇒ **不参与口气**，因此只选 ``lms`` 时本模块同样返回空串。

任务 86 追加**「生成时的口径」标记**（本模块是唯一拼装点，0 LLM、不改生成行为）：

- 生成时把当次口径写进内容条目 ``meta``：``scenario_used = {primary, selected, ...}``
  （**未选场景 ⇒ 不写这个键**）+ ``prompt_version``；
- 老内容没有这个键 ⇒ 界面/笔记一律显示「**通用(未标记)**」，**不许猜、不许默认成某个场景**；
- 界面徽标、笔记库 frontmatter、交付说明汇总都从 :func:`badge_of` / :func:`frontmatter_fields` /
  :func:`tally` 取，避免三处各写一份文案。

范式照 ``app/services/k12.py``（任务 51）：Course 对象与 dict 都吃、纯函数、无副作用。
"""
from __future__ import annotations

META_BLOCK_KEY = "scenario"

# (key, 显示名, 一句话说明) —— 与前端 frontend/src/lib/scenarios.ts **必须逐字一致**
# （界面唯一出处在前端，提示词唯一出处在本文件；两侧 key 集合与文案由门禁比对）。
SCENARIOS: tuple[tuple[str, str, str, str], ...] = (
    ("classroom", "线下课堂面授", '照念/讲解为主，要有板书与提问点', "课堂向"),
    ("live", "线上直播", '对着镜头讲，需要稿子和互动点', "直播向"),
    ("recorded", "录成视频课", '每小节能独立成段(≤6 分钟)，不依赖手势指着说', "录课向"),
    ("self_study", "学生自学", '书面完整句，不能靠"上面说过"', "自学向"),
    ("lms", "平台投放(SCORM)", '只影响出包形态，不影响口气', "出包向"),
)
SCENARIO_KEYS: tuple[str, ...] = tuple(key for key, _, _, _ in SCENARIOS)
_LABELS = {key: label for key, label, _, _ in SCENARIOS}
_HINTS = {key: hint for key, _, hint, _ in SCENARIOS}
#: 第 4 列 = 界面徽标与交付说明汇总用的**短标**（中文标签留给 frontmatter）。
_BADGES = {key: badge for key, _, _, badge in SCENARIOS}

#: 内容条目 ``meta`` 里的「当次生成口径」标记（任务 86）。未选场景 ⇒ **不写** ``scenario_used``。
SCENARIO_USED_KEY = "scenario_used"
PROMPT_VERSION_KEY = "prompt_version"
#: 老内容没有标记时显示什么 —— **不许猜**（既不默认成 classroom，也不写成"未知"）。
UNMARKED_BADGE = "通用(未标记)"
UNMARKED_LABEL = "通用"
UNMARKED_VERSION = "未记录"

# 「场景要求」正文：只有**改口气**的场景有内容；``lms`` 只影响出包 ⇒ 空串。
_REQUIREMENT = {
    "classroom": (
        "口语态：可以写「我们来看」「大家注意」这类现场话术，句子短、例子密度高；"
        "每一小节至少留 1 个互动 / 提问点（讲师能当场提问、学员能当场答）。"),
    "live": (
        "口语态：写成对着镜头能直接念的稿子（短句、有过渡语、不冷场）；"
        "每一小节至少 1 个互动点（弹幕 / 连麦 / 投票 / 举手都算）。"),
    "recorded": (
        "每小节可独立成段：按 6 分钟口语量（约 1400–1800 字）切分"
        "（依据：edX 10 万+ 学习者数据显示超过 6 分钟注意力迅速流失）；"
        "段首用一句话交代「这一段讲什么」；"
        "**不依赖画面 / 手势指代**（不写「看这张图右下角」「这里」「刚才那个」）；"
        "逐字稿要能被直接念出来。"),
    "self_study": (
        "书面态：用完整句，**不许依赖前文指代**（不出现「上节课讲过」「刚才那个」"
        "「这个我们」）；每一小节自带定义与例子，末尾给一个自测（1~2 道题或 1 条自查清单）。"),
    "lms": "",
}


def _meta_of(source) -> dict:
    """从 Course 对象或 dict 里取 meta（两种都接受，便于纯函数测试）。"""
    if isinstance(source, dict):
        return dict(source)
    return dict(getattr(source, "meta", None) or {})


def scenario_meta(source) -> dict:
    """归一化后的场景元数据；**未选择返回 ``{}``**（绝不补默认场景）。

    能容错的地方只做「自洽修正」：``selected`` 去重 / 只留合法 key / 按固定顺序排列，
    ``primary`` 必须 ∈ ``selected``（不在就退回 ``selected[0]``）。
    """
    block = _meta_of(source).get(META_BLOCK_KEY)
    if not isinstance(block, dict):
        return {}
    raw = block.get("selected")
    if isinstance(raw, str):
        raw = [raw]
    wanted = {str(item) for item in (raw or [])}
    picked = [key for key in SCENARIO_KEYS if key in wanted]
    if not picked:
        return {}
    primary = str(block.get("primary") or "")
    if primary not in picked:
        primary = picked[0]
    out = {"primary": primary, "selected": picked}
    if block.get("updated_at"):
        out["updated_at"] = str(block["updated_at"])
    return out


def is_selected(source) -> bool:
    """这门课有没有选过使用场景（没选 = 一切与今天一致）。"""
    return bool(scenario_meta(source))


def label(key: str) -> str:
    """场景显示名（未知 key 返回空串）。"""
    return _LABELS.get(str(key or ""), "")


def hint(key: str) -> str:
    """场景一句话说明（同时作为界面 tooltip）。"""
    return _HINTS.get(str(key or ""), "")


def badge(key: str) -> str:
    """场景短标（界面徽标 / 交付说明汇总用）；未知 key 返回空串。"""
    return _BADGES.get(str(key or ""), "")


def requirement(key: str) -> str:
    """某场景对「写作口气」的要求；不改口气的场景（``lms`` / 未知 key）返回空串。"""
    return _REQUIREMENT.get(str(key or ""), "")


def describe(source) -> str:
    """一句话描述（如「线下课堂面授 + 录成视频课（主：线下课堂面授）」）；未选择返回空串。"""
    meta = scenario_meta(source)
    if not meta:
        return ""
    picked = " + ".join(label(key) for key in meta["selected"])
    return f"{picked}（主：{label(meta['primary'])}）"


def tone_keys(source) -> list[str]:
    """本次真正改口气的场景（主场景在前，其余为叠加要求）；空 = 提示词一字不加。"""
    meta = scenario_meta(source)
    if not meta:
        return []
    rest = [key for key in meta["selected"] if key != meta["primary"] and requirement(key)]
    if requirement(meta["primary"]):
        return [meta["primary"], *rest]
    return rest


def scenario_prompt_block(source) -> str:
    """讲义 / 大纲 / 课件口播稿三处的「场景要求」段（**未选场景返回空串**）。"""
    keys = tone_keys(source)
    if not keys:
        return ""
    lines = "\n".join(
        (f"- 【主场景】{label(key)}：{requirement(key)}" if index == 0
         else f"- 【叠加要求】{label(key)}：{requirement(key)}")
        for index, key in enumerate(keys))
    return (
        "\n【场景要求（本次按使用场景生成，必须遵守）】\n"
        f"本次使用场景：{describe(source)}。\n"
        f"{lines}\n"
    )

# ---------------------------------------------------------------------------
# 任务 86：「生成时的口径」标记（内容条目 meta / 界面徽标 / 笔记 frontmatter / 交付说明）
# ---------------------------------------------------------------------------

def run_meta(course, prompt_version: str = "") -> dict:
    """当次生成生效的口径标记，直接 ``**`` 进内容条目 meta。

    **未选场景 ⇒ 不写** :data:`SCENARIO_USED_KEY`（老课行为与从前一字不差）；
    ``prompt_version`` 为空（该产物没有提示词版本）时同样不写。
    """
    out: dict = {}
    block = scenario_meta(course)
    if block:
        out[SCENARIO_USED_KEY] = block
    if prompt_version:
        out[PROMPT_VERSION_KEY] = str(prompt_version)
    return out


def used_meta(meta) -> dict:
    """从**内容条目 meta** 读回当次口径（没有 / 脏数据 ⇒ ``{}``）。

    只认 :data:`SCENARIO_USED_KEY`；归一化规则与 :func:`scenario_meta` 完全一致
    （去重 / 只留合法 key / 固定顺序 / primary ∈ selected）。
    """
    used = meta.get(SCENARIO_USED_KEY) if isinstance(meta, dict) else None
    if not isinstance(used, dict):
        return {}
    return scenario_meta({META_BLOCK_KEY: used})


def badge_of(meta) -> str:
    """内容条目的徽标：有标记 ⇒ 主场景短标；**没有标记 ⇒ 「通用(未标记)」**（不许猜）。"""
    used = used_meta(meta)
    return badge(used.get("primary") if used else "") or UNMARKED_BADGE


def prompt_version_of(meta) -> str:
    """内容条目记录的提示词版本（没有 ⇒ 空串）。"""
    raw = meta.get(PROMPT_VERSION_KEY) if isinstance(meta, dict) else None
    return str(raw) if raw else ""


def frontmatter_fields(meta) -> list[tuple[str, str]]:
    """笔记库 frontmatter 的两个口径字段：``scenario``（中文标签；未标记写「通用」）+ ``prompt_version``。"""
    used = used_meta(meta)
    label_text = label(used.get("primary")) if used else ""
    return [("scenario", label_text or UNMARKED_LABEL),
            ("prompt_version", prompt_version_of(meta) or UNMARKED_VERSION)]


def tally(metas) -> str:
    """交付说明里那行「本包各篇的口径」汇总（空输入 ⇒ 空串）。

    例：``6 篇：录课向 6`` / ``混合：录课向 5、通用(未标记) 1``。
    """
    items = [m if isinstance(m, dict) else {} for m in (metas or [])]
    if not items:
        return ""
    counts: dict[str, int] = {}
    for meta in items:
        name = badge_of(meta)
        counts[name] = counts.get(name, 0) + 1
    total = len(items)
    if len(counts) == 1:
        only = next(iter(counts))
        return f"{total} 篇：{only} {total}"
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return "混合：" + "、".join(f"{name} {count}" for name, count in ordered)

