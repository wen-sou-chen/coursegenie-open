# -*- coding: utf-8 -*-
"""任务 92：存量概念图 / 图表场景的图标**就地归一 + 重渲染**（0 LLM、幂等、默认 dry-run）。

真机问题：交付包 ``图表/svg/*.svg`` 里 122/139 张图的**左上角标题条**顶着 👨‍🍳，节点图标里
也混着 📊 / 🔄 / 🟠 / 🧠 … 这类白名单外的图标 —— 客户翻图就能看到。根因是
``excalidraw_layout.DEFAULT_HEAD`` 曾经是 👨‍🍳，而任务 77 只收敛了「生成时」的口径，
存量场景的图标**在生成时就写进了 elements**，改提示词也追不回来。

本脚本按 ``diagram.py`` 那**唯一一套**白名单收敛（``normalize_scene_icons`` ⇒
``_emoji_of`` / ``DIAGRAM_EMOJI_WHITELIST`` / ``_EMOJI_WHEEL``），标题条那一枚换成
``_DIAGRAM_HEAD_EMOJI``（📘），圈号（①②③④）与正文**不动**；改完按既有渲染器重出
``body.svg_preview`` 指向的 SVG（覆盖，不留孤儿缓存）。全程不调模型。

⚠️ **范围包含 ``chart_*``**（思维导图 / 时间线 / 折线 / 柱状…）：它们的标题条图标来自
**同一个** ``DEFAULT_HEAD``，真机实测交付包 ``图表/svg/`` 里 18 份「厨师图」正是这批
（只修 ``kind='diagram'`` 会让「包内 👨‍🍳 = 0」这条判据永远达不成）。

用法（临时五件套，别指向真库 / 真 storage）::

    $env:DATABASE_URL="sqlite:///C:/tmp/cg92/coursegenie.db"
    $env:STORAGE_DIR="C:/tmp/cg92/storage"
    cd backend
    .venv\\Scripts\\python.exe ..\\scripts\\normalize_diagram_icons.py              # dry-run（全部课程）
    .venv\\Scripts\\python.exe ..\\scripts\\normalize_diagram_icons.py --course <course_id>
    .venv\\Scripts\\python.exe ..\\scripts\\normalize_diagram_icons.py --course <course_id> --apply

``--apply`` 落库后会**回读断言**：库内 ``kind='diagram'`` 的图标全部落在白名单内
（圈号除外），不达标即退出码 1（不静默）。连跑两次 ⇒ 第二次 0 处改动（幂等）。

顺序（任务 99）：``--apply`` **先把 body 落库（``db.commit()``）、再统一重渲染 SVG** ——
途中任何异常都不会留下「SVG 换了、库没换」这种"看着像做了"的反向状态；反过来的代价是
**出图那一步失败时库已改、对应的那份 SVG 还是旧的**（此时重跑本脚本报 0 处改动、不会
自动补图 ⇒ 需重新生成该图），脚本会带着异常退出、不静默。
控制台口径（任务 99）：脚本开头把 stdout / stderr 包成 UTF-8 ⇒ **Windows 默认（GBK）
控制台**直接跑也不会因打印 emoji 而崩。
"""
from __future__ import annotations

import argparse
import io
import sys
from collections import Counter
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from sqlalchemy import or_  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402
from app.models.course import ContentItem  # noqa: E402
from app.services.diagram import (  # noqa: E402
    DIAGRAM_EMOJI_WHITELIST,
    normalize_item_icons,
    normalize_scene_icons,
    rerender_svg_preview,
    scene_icon_text,
)

#: 本项目统一口径（任务 99）：**Windows 默认（GBK）控制台**下也要能跑完 —— 本脚本会打印
#: 带 emoji 的改动明细，Python 默认按控制台代码页编码 ⇒ 未包装时 `UnicodeEncodeError`
#: 崩在 `db.commit()` 之前（真机上「看着跑了、其实一个字没落库」）。stderr 同样要包，
#: 否则异常消息里带 emoji 会二次崩。
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

#: 白名单比对键：忽略变体选择符（⚠️ 与 ⚠ 是同一枚图标；任务 77 的清单里带 FE0F）。
_WHITELIST_KEYS = {icon.replace("\ufe0f", "") for icon in DIAGRAM_EMOJI_WHITELIST}


def scene_items(db):
    """所有带 Excalidraw 场景的条目：概念图（``diagram``）+ 各类数据图（``chart_*``）。"""
    return (db.query(ContentItem)
            .filter(or_(ContentItem.kind == "diagram", ContentItem.kind.like("chart_%")))
            .order_by(ContentItem.created_at).all())


def icon_keys(elements: list[dict]) -> Counter:
    """一张场景里所有图标（emoji 文字元素）的白名单比对键 —— 圈号依定义不进这里。"""
    return Counter(scene_icon_text(el).replace("\ufe0f", "")
                   for el in elements if scene_icon_text(el) is not None)


def outside_whitelist(elements: list[dict]) -> Counter:
    """白名单外的图标统计（判据 = 空）。"""
    return Counter({key: count for key, count in icon_keys(elements).items()
                    if key not in _WHITELIST_KEYS})


def elements_of(item: ContentItem) -> list[dict]:
    doc = (item.body or {}).get("excalidraw_json")
    return list(doc.get("elements") or []) if isinstance(doc, dict) else []


def main() -> int:
    parser = argparse.ArgumentParser(description="存量概念图图标就地归一 + 重渲染（0 LLM、幂等）")
    parser.add_argument("--course", default="", help="只处理该课程 id（默认全部课程）")
    parser.add_argument("--limit", type=int, default=0, help="最多处理几张图（默认不限）")
    parser.add_argument("--apply", action="store_true", help="写库并重渲染；默认 dry-run 只报告")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        items = scene_items(db)
        if args.course:
            items = [it for it in items if it.course_id == args.course]
        if args.limit:
            items = items[: args.limit]

        changed = same = skipped = 0
        pending_render: list[ContentItem] = []   # 归一了 body、还没出图的条目（先落库再统一出图）
        before_icons: Counter = Counter()
        after_icons: Counter = Counter()
        for item in items:
            elements = elements_of(item)
            if not elements:
                skipped += 1
                print(f"[跳过] {item.title}：没有 excalidraw_json.elements")
                continue
            before_icons += outside_whitelist(elements)
            if args.apply:
                # 只改 body，**不**在循环里出图（任务 99）：先 commit、再统一重渲染，
                # 免得中途异常留下「SVG 换了、库没换」这种看不出来的不同步。
                notes = normalize_item_icons(item, render=False)
                after = elements_of(item)  # 归一后的 body（同一事务内读回，不用刷新）
                if notes:
                    pending_render.append(item)
            else:
                normalized, notes = normalize_scene_icons(elements)
                after = normalized
            after_icons += outside_whitelist(after)
            if notes:
                changed += 1
                head = "apply" if args.apply else "dry"
                print(f"[{head}] {item.title}：{len(notes)} 处 —— {'；'.join(notes)}")
            else:
                same += 1
        if args.apply:
            db.commit()
            # 落库成功之后再出图：出图中途异常最多让**这一份** SVG 落后（库已是新的），
            # 不会出现「图是新的、库没换」。
            for item in pending_render:
                rerender_svg_preview(item)

        print(f"\n概念图 / 图表场景 {len(items)} 张：改动 {changed} 张、本来就干净 {same} 张、"
              f"跳过 {skipped} 张；{'已写库 + 已重渲染 SVG' if args.apply else 'dry-run 未写库'}")
        if before_icons:
            summary = "、".join(f"{icon}×{count}" for icon, count in before_icons.most_common())
            print(f"归一前白名单外图标：{sum(before_icons.values())} 个（{summary}）")
        stay = after_icons if args.apply else Counter()
        if stay:
            summary = "、".join(f"{icon}×{count}" for icon, count in stay.most_common())
            print(f"  x 归一后仍有白名单外图标：{sum(stay.values())} 个（{summary}）")
            return 1
        if args.apply:
            print("断言通过：库内概念图 / 图表场景的图标全部落在白名单内"
                  "（圈号 ①②③④ 依定义不属于图标，未动）")
        else:
            print("dry-run：以上为将要改动的量；加 --apply 才写库（写库后会回读断言）")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
