# -*- coding: utf-8 -*-
"""课程级 PPT 主题设置（任务 145）：``course.meta["ppt_theme"]`` 的读写 + 主题优先级唯一口径。

**为什么要有这个模块**（任务 145 要修的真问题）：主题原先只有「调用方传参」与「后端配置
``PPT_THEME``」两档，而 :func:`app.services.ppt.generate_deck` 的函数默认值写死 ``"theme07"``
（= 专业培训）——于是「界面留空」与「不传参数」会得到**两个不同主题**（前者走配置、后者走
专业培训），而且**课程上没有任何地方记主题**（``course.meta`` 里只有 ``style_anchor`` 的
``tone``，那只是调研溯源）⇒ 客户挑好的外观记不住，换个入口重出就可能换色。

优先级（**唯一口径**，``generate_deck`` / 路由 / 迁移脚本都走 :func:`resolve`）::

    调用方显式传的 theme（非空字符串） > 课程 meta.ppt_theme > 后端配置 PPT_THEME > 内置默认

⚠️ 空串 / ``None`` **不再**等于「用配置」（那是任务 145 之前的老口径
``resolve_theme(theme or get_settings().ppt_theme)``）：现在的空串 = 「按上面这条优先级走」，
**课程设置比后端配置优先**。

空值 / 脏值语义（与「使用场景」那套「不许猜」一致，见 ``app/services/scenario.py``）：

- 课程**没设过** ⇒ 不算设置，直接走配置；
- 课程里是**非法值** ⇒ 不静默回落了事：回落到**配置**并**给出原因**（原因写进
  ``deck.meta.theme_error`` 与 ``layout_notes``，接口里给 ``course.error``）；
- **写入**时非法值一律 ``ValueError`` / 接口 4xx，**不写库**（合法值清单见 :func:`legal_values_text`）。
"""
from __future__ import annotations

from app.core.config import get_settings
from app.services.ppt_theme import THEMES, resolve_theme

#: 课程级设置的存放位置。``course.meta`` 里已有 ``style_anchor`` / ``scenario`` /
#: ``outline_check`` 这些课程级设置，主题与它们同层 —— **有意复用 meta，不新开数据库列**。
META_KEY = "ppt_theme"

#: 主题来源（接口 ``course.source`` 与课件 ``meta.theme_source`` 用这套 key）
SOURCE_PARAM = "param"
SOURCE_COURSE = "course"
SOURCE_CONFIG = "config"
SOURCE_DEFAULT = "default"

#: 来源 key → 人话。**前端 ``frontend/src/lib/pptTheme.ts`` 的同名表必须逐字一致**
#: （门禁 ``frontend/scripts/check-ppt-course-theme.ts`` 会读两个文件逐条比对）。
SOURCE_LABELS: dict[str, str] = {
    "param": "本次调用指定",
    "course": "本课设置",
    "config": "后端配置",
    "default": "内置默认",
}

#: 改主题的后果（接口返回 / 日志 / 界面提示共用一句话，避免三处各写一份文案）。
#: 口径与「使用场景」那条「不会影响已生成的内容，必须重新生成才会变」一致。
APPLY_NOTE = "只改外观，不改内容；改了主题要重新生成课件才会生效"


def source_label(source: str | None) -> str:
    """来源 key → 人话；未知 key 返回空串（**不猜**）。"""
    return SOURCE_LABELS.get(str(source or ""), "")


def raw_setting(course) -> str:
    """课程里存的**原始**主题值（Course 对象或 ``meta`` dict 都吃）；没有 ⇒ 空串。"""
    meta = course if isinstance(course, dict) else getattr(course, "meta", None)
    if not isinstance(meta, dict):
        return ""
    return str(meta.get(META_KEY) or "").strip()


def legal_values_text() -> str:
    """合法值的人话清单（接口 4xx 与脏值提示共用，唯一出处）。"""
    return " / ".join(f"{spec['id']}（{spec['name']}）" for spec in THEMES.values())


def normalize_key(name: str | None) -> str | None:
    """合法主题名 → **规范 id**（中文名与 ``themeNN`` 别名都归一化）；空 / 非法 ⇒ ``None``。

    **不猜、不回落**：拿到 ``None`` 的调用方必须显式报错（写入路径）或显式记原因（读取路径）。
    """
    raw = str(name or "").strip()
    if not raw:
        return None
    spec, error = resolve_theme(raw)
    return None if error else spec["id"]


def resolve(explicit: str | None = None, course=None,
            config: str | None = None) -> tuple[dict, str, str | None]:
    """主题优先级唯一口径 → ``(主题定义副本, 来源, 回退原因)``。

    来源取值：``param`` / ``course`` / ``config`` / ``default``（人话见 :data:`SOURCE_LABELS`）；
    回退原因是**人话**（没有回退 ⇒ ``None``），调用方照原样记进 meta / 接口，不改口径。
    """
    config = get_settings().ppt_theme if config is None else str(config or "")
    raw_param = str(explicit or "").strip()
    if raw_param:
        spec, error = resolve_theme(raw_param)
        return spec, SOURCE_PARAM, error
    raw_course = raw_setting(course)
    if raw_course:
        spec, error = resolve_theme(raw_course)
        if error is None:
            return spec, SOURCE_COURSE, None
        # 脏值：回落到**配置**（不是直接落内置默认），并把原因说全
        spec, config_error = resolve_theme(config)
        reason = (f"课程设置的主题「{raw_course}」不是合法主题（合法值：{legal_values_text()}），"
                  f"已按后端配置出课件：{spec['name']}")
        if config_error:
            reason += f"；后端配置本身也有问题：{config_error}"
        return spec, (SOURCE_DEFAULT if config_error else SOURCE_CONFIG), reason
    spec, error = resolve_theme(config)
    if error:
        return spec, SOURCE_DEFAULT, error
    return spec, (SOURCE_CONFIG if config.strip() else SOURCE_DEFAULT), None


def write(course, value: str | None) -> dict:
    """把课程主题写进 ``course.meta.ppt_theme``（调用方负责 ``db.commit()``）。

    ``value`` 为空串 / ``None`` ⇒ **清除**本课设置（回到后端配置）；
    非法值 ⇒ ``ValueError``（人话 + 合法值清单），**绝不静默回落、绝不写库**。
    合法值一律存**规范 id**（``theme07`` / 「专业培训」都归一化成 ``professional``）。
    """
    raw = str(value or "").strip()
    meta = dict(getattr(course, "meta", None) or {})
    if not raw:
        meta.pop(META_KEY, None)
        course.meta = meta
        # 清除后的来源用**同一条**优先级算（meta 里已经没有这个键 ⇒ 走配置 / 内置默认）
        _, source, _ = resolve(None, meta, None)
        return {"course_id": getattr(course, "id", ""), "theme": "", "theme_name": "",
                "setting": None, "source": source, "cleared": True}
    theme_id = normalize_key(raw)
    if theme_id is None:
        raise ValueError(
            f"主题「{raw}」不是合法主题，未写库。合法值：{legal_values_text()}；"
            f"传空串 = 清除本课设置（回到后端配置 PPT_THEME）")
    meta[META_KEY] = theme_id
    course.meta = meta
    return {"course_id": getattr(course, "id", ""), "theme": theme_id,
            "theme_name": THEMES[theme_id]["name"], "setting": theme_id,
            "source": SOURCE_COURSE, "cleared": False}
