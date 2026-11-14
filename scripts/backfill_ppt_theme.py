# -*- coding: utf-8 -*-
"""任务 145：把存量课件的主题显式回填进 ``course.meta.ppt_theme``（0 LLM、幂等、默认 dry-run）。

**为什么要有这一步**：任务 145 把 ``generate_deck`` 的函数默认值从 ``"theme07"``（= 专业培训）
改成空串（= 走「课程设置 > 后端配置 > 内置默认」）。**凡是没有显式记过主题的课**，重出时的主题
就由新的优先级决定；回填把每门课**当前实际会出的主题**写死到课程上 ⇒ 这次默认值变更与以后
``PPT_THEME`` 的调整都翻不动存量课（客户挑好的外观记得住）。

回填依据（**不猜**，逐门课印出来）：

1. 该课**整课课件**（``meta.scope == "course"``）的 ``meta.theme`` —— 对外交付最常露脸的那一份；
2. 没有整课课件 ⇒ 该课**最近生成的一份课件**的 ``meta.theme``；
3. 一份带主题的课件都没有（例如全是教师自己上传的 pptx）⇒ 后端配置 ``PPT_THEME`` 的解析值。

写库规则（**幂等**）：

- 课程已经显式设过**合法**主题 ⇒ 一律不动（即使与依据算出的值不同）⇒ 跑第二遍 0 改动；
- 课程没设过 / 设的是**脏值** ⇒ 写上面算出的值（脏值按「没设过」处理，与读取口径一致）。

用法（临时五件套，别指向真库 / 真 storage）::

    cd backend
    .venv\\Scripts\\python.exe ..\\scripts\\backfill_ppt_theme.py                 # dry-run（只读）
    .venv\\Scripts\\python.exe ..\\scripts\\backfill_ppt_theme.py --course <id>   # 只看一门课
    .venv\\Scripts\\python.exe ..\\scripts\\backfill_ppt_theme.py --apply        # 写库（可重复跑）

``--apply`` 落库后**回读断言**：每门课的 ``meta.ppt_theme`` 都是合法主题 id（不达标退出码 1）。
本脚本**只写 ``course.meta``**：不碰 storage / vault / 交付包（跑前跑后可用指纹比对）。
"""
from __future__ import annotations

import argparse
import io
import sys
from collections import Counter
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.core.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.course import ContentItem, Course  # noqa: E402
from app.services import ppt_theme_setting  # noqa: E402
from app.services.ppt_theme import resolve_theme  # noqa: E402


#: 本项目统一口径（任务 99）：Windows 默认（GBK）控制台下也要能跑完 —— 本脚本会打印中文课名与
#: 主题名，Python 默认按控制台代码页编码 ⇒ 未包装时可能崩在 ``db.commit()`` 之前。
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace",
                              line_buffering=True)


def _stamp(deck: ContentItem) -> str:
    """课件的生成时间戳（``meta.generated_at`` 优先，缺失回落 ``created_at``），用于「最近一份」。"""
    meta = deck.meta or {}
    return str(meta.get("generated_at") or getattr(deck, "created_at", "") or "")


def target_of(course, decks, config_theme: str) -> dict:
    """算某门课要回填的主题 + 依据（**纯函数**：只读传入的课件列表，测试直接调）。"""
    themed: list[tuple[ContentItem, str]] = []
    notes: list[str] = []
    for deck in decks:
        raw = str((deck.meta or {}).get("theme") or "").strip()
        if not raw:
            continue
        theme_id = ppt_theme_setting.normalize_key(raw)
        if theme_id is None:
            notes.append(f"{deck.title} 的 meta.theme 是脏值「{raw}」（这份不计入依据）")
            continue
        themed.append((deck, theme_id))
    if not decks:
        notes.append("该课没有任何课件")
    course_scope = [(d, t) for d, t in themed
                    if str((d.meta or {}).get("scope") or "course") == "course"]
    if course_scope:
        deck, value = sorted(course_scope, key=lambda pair: _stamp(pair[0]))[-1]
        basis = f"该课整课课件（{deck.title}）的 meta.theme={value}"
    elif themed:
        deck, value = sorted(themed, key=lambda pair: _stamp(pair[0]))[-1]
        basis = f"该课没有整课课件；最近一份课件（{deck.title}）的 meta.theme={value}"
    else:
        spec, _ = resolve_theme(config_theme)
        value = spec["id"]
        basis = f"该课没有任何带主题的课件 ⇒ 按后端配置 PPT_THEME={value}"
    counts = Counter(theme_id for _, theme_id in themed)
    return {"value": value, "basis": basis, "notes": notes, "counts": counts,
            "themed": len(themed), "total": len(decks)}


def plan_for(db, course, config_theme: str | None = None) -> dict:
    """一门课的回填计划（写 / 不动 + 依据 + 原因）。"""
    config_theme = get_settings().ppt_theme if config_theme is None else config_theme
    decks = (db.query(ContentItem)
             .filter(ContentItem.course_id == course.id, ContentItem.kind == "ppt")
             .order_by(ContentItem.created_at).all())
    target = target_of(course, decks, config_theme)
    raw = ppt_theme_setting.raw_setting(course)
    current = ppt_theme_setting.normalize_key(raw)
    if current == target["value"]:
        action = "skip-same"
        reason = f"本课已设置 {current}（与依据一致，幂等：不写）"
    elif current:
        action = "skip-set"
        reason = f"本课已显式设置 {current}（与依据算出的 {target['value']} 不同，按「用户说了算」不动）"
    else:
        action = "write"
        reason = (f"课程没设过（现有脏值「{raw}」按没设过处理）" if raw else "课程没设过")
    return {"course": course, "decks": decks, "value": target["value"], "basis": target["basis"],
            "action": action, "reason": reason, "raw": raw, "current": current,
            "notes": target["notes"],
            "counts": target["counts"], "themed": target["themed"], "total": target["total"]}


def _describe(plan: dict) -> str:
    counts = "、".join(f"{theme}={count}" for theme, count in plan["counts"].most_common()) or "无"
    return (f"课件 {plan['total']} 份（带主题 {plan['themed']}：{counts}）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把存量课件的主题显式回填进 course.meta.ppt_theme（0 LLM、幂等、默认 dry-run）")
    parser.add_argument("--course", default="", help="只处理该课程 id（默认全部课程）")
    parser.add_argument("--apply", action="store_true", help="写库；默认 dry-run 只报告、不写")
    args = parser.parse_args(argv)

    db = SessionLocal()
    try:
        courses = db.query(Course).order_by(Course.created_at).all()
        if args.course:
            courses = [c for c in courses if c.id == args.course]
            if not courses:
                print(f"找不到课程：{args.course}")
                return 1
        plans = [plan_for(db, course) for course in courses]
        head = "apply" if args.apply else "dry-run"
        for plan in plans:
            course = plan["course"]
            write = plan["action"] == "write"
            shown = plan["value"] if write else (plan["current"] or plan["value"])
            print(f"[{head}] {'会写' if write else '不动'} {shown} ← {course.title}"
                  f"（{course.id[:8]}）"
                  f"｜{plan['reason']}｜依据：{plan['basis']}｜{_describe(plan)}")
            if len(plan["counts"]) > 1:
                print(f"        注：本课课件主题**不一致**（{_describe(plan)}）——"
                      f"回填后「下次重出」会统一成 {plan['value']}")
            for note in plan["notes"]:
                print(f"        注：{note}")

        writes = [plan for plan in plans if plan["action"] == "write"]
        if args.apply:
            for plan in writes:
                course = plan["course"]
                meta = dict(course.meta or {})
                meta[ppt_theme_setting.META_KEY] = plan["value"]
                course.meta = meta
            db.commit()
            db.expire_all()

        same = len([p for p in plans if p["action"] == "skip-same"])
        kept = len([p for p in plans if p["action"] == "skip-set"])
        print(f"\n课程 {len(plans)} 门：写 {len(writes)} 门、已是该值 {same} 门、"
              f"已显式设置且与依据不同 {kept} 门；"
              f"{'已写库' if args.apply else 'dry-run 未写库'}")

        if not args.apply:
            print("dry-run：以上为将要写入的值与依据；加 --apply 才写库（写后回读断言、可重复跑）")
            return 0

        bad: list[str] = []
        for plan in plans:
            course = db.get(Course, plan["course"].id)
            value = ppt_theme_setting.normalize_key(ppt_theme_setting.raw_setting(course))
            if value is None:
                bad.append(f"{course.title}（meta.ppt_theme={ppt_theme_setting.raw_setting(course)!r}）")
            elif plan["action"] == "write" and value != plan["value"]:
                bad.append(f"{course.title}（应为 {plan['value']}，实际 {value}）")
        if bad:
            print("回读断言**未通过**：" + "、".join(bad))
            return 1
        print(f"回读断言通过：全部 {len(plans)} 门课的 meta.ppt_theme 都是合法主题 id")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
