# -*- coding: utf-8 -*-
"""任务 92：**存量**概念图图标就地归一 + 重渲染（0 LLM）。

起因（DSH 2026-09-24 只读实测）：交付包 ``图表/svg/*.svg`` 里 122/139 张图的左上角
**标题条**顶着 👨‍🍳，节点图标里也混着 📊 / 🔄 / 🟠 / 🧠 … 这类白名单外的图标。根因是
``excalidraw_layout.DEFAULT_HEAD`` 曾经就是 👨‍🍳，任务 77 只收敛了「生成时」的口径 ——
存量场景的图标在生成时**已经写进 elements**，改提示词追不回来。

本文件钉四件事：① 渲染器缺省值必须是白名单内那枚（改回 👨‍🍳 ⇒ 红）；
② 归一必须处理**标题条那一枚**（只处理节点 ⇒ 红）；③ 只改图标、几何/正文/圈号一个字节不动；
④ 幂等 + 就地重渲染 ``body.svg_preview``。
⑤ **默认（GBK）控制台**跑迁移脚本不许崩（注掉开头那两行 UTF-8 包装 ⇒ 红，任务 99）；
⑥ 迁移脚本**先落库、再出图**：出图炸了，库里的归一也已经提交（顺序改回去 ⇒ 红）。
⑦ Excalidraw 的**编辑镜像** ``rawText`` / ``originalText`` 也要一起归一（去掉那一步 ⇒ 红，
   任务 100：客户拿 .excalidraw 原件回编辑器改标题时读的就是镜像 ⇒ 厨师头会「回来」）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.course import ContentItem
from app.services import figure_png
from app.services.diagram import (
    DIAGRAM_EMOJI_WHITELIST,
    _DIAGRAM_HEAD_EMOJI,
    _build_scene,
    diagram_to_svg,
    normalize_item_icons,
    normalize_scene_icons,
    render,
    scene_icon_text,
)
from app.services.excalidraw_layout import DEFAULT_HEAD

#: 渲染器**旧**缺省值（任务 92 之前的 👨‍🍳）—— 存量场景里写死的就是它。
LEGACY_HEAD = "\U0001F468\u200d\U0001F373"
_WHITELIST_KEYS = {icon.replace("\ufe0f", "") for icon in DIAGRAM_EMOJI_WHITELIST}
CIRCLED = "\u2460\u2461\u2462\u2463"

REPO = Path(__file__).resolve().parents[2]
_SCRIPT = REPO / "scripts" / "normalize_diagram_icons.py"


def _icons(elements) -> list[str]:
    return [icon for icon in (scene_icon_text(el) for el in elements) if icon]


def _outside(elements) -> list[str]:
    return [icon for icon in _icons(elements) if icon.replace("\ufe0f", "") not in _WHITELIST_KEYS]


def _geometry(elements) -> list[tuple]:
    """非图标元素的几何 + 文字快照（图标字形宽会变，其余一个字节都不该动）。"""
    return [(el["type"], round(float(el["x"]), 3), round(float(el["y"]), 3),
             round(float(el.get("width") or 0), 3), round(float(el.get("height") or 0), 3),
             str(el.get("text") or ""))
            for el in elements if scene_icon_text(el) is None]


def _legacy_elements() -> list[dict]:
    """造一张**存量**场景：标题条是 👨‍🍳、节点图标全在白名单外、外加 ①②③④ 圈号。"""
    data = {"layout": "radial", "caption": "存量场景", "center": "中心一句话",
            "center_emoji": "📊", "nodes": [
                {"emoji": "🔄", "title": "要点一", "subs": ["说明一句"]},
                {"emoji": "🧠", "title": "要点二", "subs": ["说明一句"]},
                {"emoji": "🟠", "title": "要点三", "subs": ["说明一句"]},
            ], "edges": []}
    # `_build_scene` 已经会收敛图标（任务 77 的口径），所以这里要把**存量场景**的样子还原回去：
    # 标题条 = 👨‍🍳、中心 / 要点卡 = 白名单外的那批（旧图在生成时就写死了这些值）。
    scene = _build_scene("radial", data, "存量场景")
    scene["emoji"] = LEGACY_HEAD
    scene["center"]["emoji"] = "📊"
    for card, hostile in zip(scene["cards"], ("🔄", "🧠", "🟠")):
        card["emoji"] = hostile
    elements = render("radial", scene)
    # 圈号：真库里是「步骤序号」这类排版符号（不是主题图标），单独塞一枚进来验"不动"
    elements.append({"id": "circled", "type": "text", "x": 40, "y": 900, "width": 40, "height": 40,
                     "text": CIRCLED[0], "fontSize": 30, "textAlign": "center",
                     "strokeColor": "#374151"})
    return elements


def test_default_head_is_the_whitelisted_diagram_head():
    """① 渲染器缺省值 = `_DIAGRAM_HEAD_EMOJI`，且在白名单内（**不许**再用 👨‍🍳）。

    反证据：把 `DEFAULT_HEAD` 改回 👨‍🍳 ⇒ 本断言红（将来缺 `emoji` 字段的场景还会长厨师）。
    """
    assert DEFAULT_HEAD == _DIAGRAM_HEAD_EMOJI == "\U0001F4D8"
    assert DEFAULT_HEAD != LEGACY_HEAD
    assert DEFAULT_HEAD in DIAGRAM_EMOJI_WHITELIST


def test_legacy_scene_without_emoji_field_renders_whitelisted_head():
    """缺 ``emoji`` 字段的场景（存量口径）渲染出来的标题图标必须在白名单内。"""
    scene = _build_scene("radial", {"layout": "radial", "caption": "无 emoji 字段",
                                    "center": "中心", "nodes": [{"title": "要点一"}], "edges": []},
                         "无 emoji 字段")
    scene.pop("emoji", None)          # 存量场景没有这个字段 ⇒ 走渲染器缺省值
    elements = render("radial", scene)
    assert _outside(elements) == [], f"缺 emoji 字段的场景渲染出了白名单外图标：{_icons(elements)}"
    assert LEGACY_HEAD not in diagram_to_svg(elements)


def test_normalize_rewrites_head_and_node_icons_and_keeps_everything_else():
    """②③ 标题条与节点图标都要归一；几何 / 正文 / 圈号一个字节不动。"""
    before = _legacy_elements()
    assert LEGACY_HEAD in _icons(before), "造的场景里没有旧缺省头像，用例本身失效"
    assert len(_outside(before)) >= 4, f"造的场景里白名单外图标太少：{_outside(before)}"

    after, notes = normalize_scene_icons(before)

    assert _outside(after) == [], f"归一后仍有白名单外图标：{_icons(after)}"
    assert len(notes) >= 4, f"标题条那一枚 + 3 个节点图标都该改（实际 {notes}）"
    assert _geometry(after) == _geometry(before), "归一把非图标元素的几何/文字改了"
    circled = [el for el in after
               if el.get("type") == "text" and str(el.get("text")) == CIRCLED[0]]
    assert len(circled) == 1, "圈号被当成图标改了（它是排版序号，不许动）"
    assert circled[0] == [el for el in before
                          if el.get("type") == "text" and str(el.get("text")) == CIRCLED[0]][0], \
        "圈号元素被改了（它是排版序号，一个字节都不该动）"
    head = [icon for icon in _icons(after) if icon == _DIAGRAM_HEAD_EMOJI]
    assert head, "标题条那一枚没有换成 📘"
    assert any("标题条" in note for note in notes), notes


def test_normalize_is_idempotent():
    """④ 幂等：归一过的场景再跑一次，零改动。"""
    once, notes = normalize_scene_icons(_legacy_elements())
    assert notes
    again, second = normalize_scene_icons(once)
    assert second == []
    assert again == once


def test_normalize_fixes_head_even_when_node_icons_are_already_clean():
    """② 的定点反证据：节点图标本来就干净、只有标题条是 👨‍🍳 ⇒ 也必须改。

    变异（迁移只处理节点、放过标题条）⇒ 这里 notes 为空 ⇒ 红。
    """
    data = {"layout": "radial", "caption": "只有标题脏", "center": "中心",
            "center_emoji": _DIAGRAM_HEAD_EMOJI,
            "nodes": [{"emoji": _DIAGRAM_HEAD_EMOJI, "title": "要点一", "subs": []}], "edges": []}
    scene = {**_build_scene("radial", data, "只有标题脏"), "emoji": LEGACY_HEAD}
    elements = render("radial", scene)
    assert _outside(elements) == [LEGACY_HEAD]
    after, notes = normalize_scene_icons(elements)
    assert notes and any(LEGACY_HEAD in note for note in notes), notes
    assert _outside(after) == []


def test_normalize_item_rewrites_svg_preview_in_place(client, course_id):
    """④ 就地重渲染：`body.svg_preview` 那份 SVG 覆盖成新渲染，文件名不变（不留孤儿）。"""
    from app.services.diagram import _write_svg, build_excalidraw_json

    elements = _legacy_elements()
    svg_rel = _write_svg(diagram_to_svg(elements))
    svg_path = get_settings().storage_dir / svg_rel
    old_svg = svg_path.read_text(encoding="utf-8")
    assert LEGACY_HEAD in old_svg

    db = SessionLocal()
    try:
        item = ContentItem(course_id=course_id, kind="diagram",
                           title="存量概念图", body={"layout": "radial", "caption": "存量场景",
                                                 "excalidraw_json": build_excalidraw_json(elements),
                                                 "svg_preview": svg_rel},
                           meta={"node_key": "1.1"})
        db.add(item)
        db.commit()

        notes = normalize_item_icons(item)
        db.commit()
        assert notes, "存量条目没有产生改动"

        new_svg = (get_settings().storage_dir / svg_rel).read_text(encoding="utf-8")
        assert new_svg != old_svg, "SVG 缓存没有重渲染"
        assert LEGACY_HEAD not in new_svg
        assert not list(svg_path.parent.glob(f"{svg_path.stem}-*.svg")), "重渲染留下了孤儿文件"
        assert _outside(list(item.body["excalidraw_json"]["elements"])) == []
        assert normalize_item_icons(item) == [], "第二次归一还有改动（不幂等）"
    finally:
        db.close()


def test_figure_png_icon_verdict_matches_whitelist_and_ignores_circled():
    """分类口径：emoji 才算图标；① 这类圈号不是 emoji ⇒ 天然落进「不动」。"""
    assert figure_png.is_icon_text("\U0001F4D8") is True
    assert figure_png.is_icon_text(LEGACY_HEAD) is True          # 多字符（ZWJ）也算一枚
    assert figure_png.is_icon_text("\u26A0\uFE0F") is True
    assert figure_png.is_icon_text(CIRCLED[0]) is False
    assert figure_png.is_icon_text("要点一") is False
    assert figure_png.is_icon_text("") is False


def _legacy_item(course_id: str, title: str, svg_preview: str,
                 elements: list[dict] | None = None) -> str:
    """往库里放一条**存量**概念图（标题条 👨‍🍳、节点全是白名单外图标），返回条目 id。"""
    from app.services.diagram import build_excalidraw_json

    elements = _legacy_elements() if elements is None else elements
    db = SessionLocal()
    try:
        item = ContentItem(course_id=course_id, kind="diagram", title=title,
                           body={"layout": "radial", "caption": "存量场景",
                                 "excalidraw_json": build_excalidraw_json(elements),
                                 "svg_preview": svg_preview},
                           meta={"node_key": "9.9"})
        db.add(item)
        db.commit()
        return item.id
    finally:
        db.close()


def _run_script(*extra: str, console_encoding: str) -> subprocess.CompletedProcess:
    """真跑迁移脚本，并把它**stdout / stderr 的编码**钉成指定值。

    ``console_encoding="gbk"`` = Windows 默认控制台那套（任务 99 就崩在这里）；
    同时把 ``PYTHONUTF8`` 关掉，免得开发机的 UTF-8 模式把这层伪装掉。
    """
    env = {**os.environ, "PYTHONIOENCODING": console_encoding, "PYTHONUTF8": "0"}
    return subprocess.run([sys.executable, str(_SCRIPT), *extra],
                          cwd=str(REPO / "backend"), env=env, capture_output=True)


def test_script_survives_non_utf8_console(client, course_id):
    """⑤ 默认（GBK）控制台跑 dry-run：exit=0，且**真的走到**带 emoji 的改动明细那一行。

    反证据（任务 99 实测）：把 ``scripts/normalize_diagram_icons.py`` 顶部那两行
    ``io.TextIOWrapper`` 注掉 ⇒ 子进程 `UnicodeEncodeError: 'gbk' codec can't encode
    character '\\U0001f468'`（崩在 `db.commit()` 之前 ⇒ 一个字都没落库）、退出码 1 ⇒ 本用例红。
    """
    from app.services.diagram import _write_svg, diagram_to_svg

    elements = _legacy_elements()
    _legacy_item(course_id, "存量概念图（控制台口径）",
                 _write_svg(diagram_to_svg(elements)), elements)

    result = _run_script("--course", course_id, console_encoding="gbk")
    stdout = result.stdout.decode("utf-8")     # 包装生效才会是 UTF-8 字节；否则这里先红
    stderr = result.stderr.decode("utf-8", "replace")

    assert result.returncode == 0, f"默认（GBK）控制台跑崩了：exit={result.returncode}；stderr={stderr[-500:]}"
    assert "标题条" in stdout and LEGACY_HEAD in stdout and _DIAGRAM_HEAD_EMOJI in stdout, \
        f"没走到「带 emoji 的改动明细」那一行 ⇒ 用例本身失效：{stdout[-300:]}"

    # stdout 这半边上面已经行为级验过；stderr 没有「消息里带 emoji」的现成路径可跑，
    # 这里按**整行**钉住（注掉 / 少包一路 ⇒ 红）：异常消息里带 emoji 会二次崩。
    active = {line.strip() for line in _SCRIPT.read_text(encoding="utf-8").splitlines()}
    for stream in ("stdout", "stderr"):
        line = (f'sys.{stream} = io.TextIOWrapper(sys.{stream}.buffer, '
                f'encoding="utf-8", errors="replace", line_buffering=True)')
        assert line in active, f"脚本没有把 {stream} 包成 UTF-8（任务 99：默认 GBK 控制台下会崩）"


def test_script_commits_icons_before_rerendering_svg(client, course_id):
    """⑥ 顺序（任务 99）：**先 commit、再重渲染** —— 出图那一步炸了，库里的归一也已经落了。

    反证据：把顺序改回「边改边重渲染、最后才 commit」（任务 92 那版）⇒ 出图异常发生在
    commit 之前 ⇒ 这里读到的还是老图标 ⇒ 红。
    """
    item_id = _legacy_item(course_id, "没有 SVG 缓存的存量图", "")   # svg_preview 空 ⇒ 出图必抛

    result = _run_script("--course", course_id, "--apply", console_encoding="utf-8")
    assert result.returncode != 0, "出图失败却静默成功（口径：失败不静默）"
    assert "svg_preview" in result.stderr.decode("utf-8", "replace"), result.stderr[-400:]

    fresh = SessionLocal()      # 新会话读库（子进程写的改动不被本次会话缓存挡住）
    try:
        row = fresh.get(ContentItem, item_id)
        assert row is not None, "条目不见了"
        body = dict(row.body or {})
        elements_now = list((body.get("excalidraw_json") or {}).get("elements") or [])
    finally:
        fresh.close()
    assert _outside(elements_now) == [], \
        f"出图失败把已经归一好的 body 一起丢了（先出图后落库的顺序没改过来）：{_outside(elements_now)}"


def _head_element(elements) -> dict:
    """标题条那一枚（按渲染槽位认），用例自己要能让位于「只有镜像脏」的场景。"""
    from app.services.diagram import _HEAD_SLOT, _HEAD_SLOT_TOL

    for el in elements:
        if scene_icon_text(el) is None:
            continue
        cx = float(el.get("x") or 0) + float(el.get("width") or 0) / 2
        cy = float(el.get("y") or 0) + float(el.get("height") or 0) / 2
        if abs(cx - _HEAD_SLOT[0]) <= _HEAD_SLOT_TOL and abs(cy - _HEAD_SLOT[1]) <= _HEAD_SLOT_TOL:
            return el
    raise AssertionError("造的场景里没有标题条那一枚")


def test_normalize_fixes_mirror_only_dirty_head():
    """⑦ 只改镜像、不动渲染份：`text` 已是 📘 但 `rawText`/`originalText` 还是 👨‍🍳 ⇒ 也要归一。

    这就是真机上现在这一批（92 只改了 `text`）⇒ 客户在 Excalidraw 里编辑标题时厨师头会回来。
    反证据：把镜像那一步去掉 ⇒ notes 为空 ⇒ 本用例红。
    """
    elements = _legacy_elements()
    head = _head_element(elements)
    head["text"] = _DIAGRAM_HEAD_EMOJI          # 渲染份是干净的（92 已改过）
    head["rawText"] = LEGACY_HEAD               # 编辑镜像还是旧的
    head["originalText"] = LEGACY_HEAD

    after, notes = normalize_scene_icons(elements)
    head_after = _head_element(after)
    assert head_after["text"] == head_after["rawText"] == head_after["originalText"] == _DIAGRAM_HEAD_EMOJI, \
        f"镜像没跟着归一：{ {f: head_after.get(f) for f in ('text', 'rawText', 'originalText')} }"
    assert any("rawText" in note for note in notes) and any("originalText" in note for note in notes), notes


def test_normalize_touches_only_the_three_text_fields():
    """⑦ 只改 `text` / `rawText` / `originalText`：其余字段与其它元素一个字节不动。"""
    before = _legacy_elements()
    after, _ = normalize_scene_icons(before)
    assert len(after) == len(before)

    for old, new in zip(before, after):
        if scene_icon_text(old) is None:        # 圈号 / 正文 / 矩形…：整元素逐键相同
            assert old == new, f"非图标元素被改了：{old} → {new}"
            continue
        changed = {key for key in set(old) | set(new) if old.get(key) != new.get(key)}
        assert changed <= {"text", "rawText", "originalText"}, f"图标元素的非文字字段被改了：{changed}"
        assert new.get("text") == new.get("rawText") == new.get("originalText"), \
            f"三份文本没对齐：{ {f: new.get(f) for f in ('text', 'rawText', 'originalText')} }"
        assert scene_icon_text(new).replace("\ufe0f", "") in _WHITELIST_KEYS, \
            f"归一后不在白名单内：{scene_icon_text(new)}"


def test_exported_excalidraw_has_no_legacy_head(client, course_id):
    """⑦ **成品侧**判据：迁移之后导出的 `.excalidraw`（发给客户的可编辑原件）里没有 👨‍🍳。

    反证据：把镜像那一步去掉 ⇒ 这里点名的就是它（`.excalidraw` 里还剩 👨‍🍳）。
    """
    from app.services.diagram import _write_svg, diagram_to_svg, excalidraw_text_of

    elements = _legacy_elements()
    item_id = _legacy_item(course_id, "存量概念图（镜像口径）",
                           _write_svg(diagram_to_svg(elements)), elements)

    db = SessionLocal()
    try:
        item = db.get(ContentItem, item_id)
        assert item is not None
        assert normalize_item_icons(item), "存量条目没有产生改动"
        db.commit()
        text = excalidraw_text_of(item)        # 与导进笔记库/交付包的是同一条出口
    finally:
        db.close()

    assert LEGACY_HEAD not in text, "导出的 .excalidraw 里还剩 👨‍🍳（编辑镜像没归一）"
    doc = json.loads(text)
    head = _head_element(doc["elements"])
    assert head["text"] == head["rawText"] == head["originalText"] == _DIAGRAM_HEAD_EMOJI, \
        f"三份文本没对齐：{ {f: head.get(f) for f in ('text', 'rawText', 'originalText')} }"
