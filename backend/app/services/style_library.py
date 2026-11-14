"""Excalidraw 风格库导出（任务 24-B）：把课程的矢量风格打成可复用的 `.excalidrawlib`。

产出 ``课程/<课名>/图表库/_风格库.excalidrawlib``，固定 4 组：

1. **角色组**：emoji + 气泡卡片（缺省中性三件套 讲解者/学员/主题；包子课那类贴切角色
   可用 ``course.meta["roles"] = [{"label": …, "emoji": …}, …]`` 按课配置，1–6 条）；
2. **卡片组**：圆角卡 / 对比栏 / 环形底座 / 时间轴底座 / 决策菱形；
3. **连线组**：实线箭头 / 虚线箭头 / 括号线；
4. **色卡组**：当前 PPT 主题的 6 色。

格式对齐 Obsidian Excalidraw 插件的库规范
``{"type":"excalidrawlib","version":2,"libraryItems":[...]}``；每个条目的元素都过
:func:`excalidraw_format.normalize_document` 补全字段（含箭头↔图形绑定、容器文字互指），
导入插件后可选中、可改色、拖卡片时文字跟着走。

可复现：所有 id / seed / versionNonce 都由条目名派生（不取时间、不用随机数），
同主题重复导出得到**字节一致**的文件，因此重复导出天然幂等（同名覆盖、不产生副本）。
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.course import Course
from app.services.excalidraw_format import normalize_document, validate_document
from app.services.ppt_theme import resolve_theme

LIB_TYPE = "excalidrawlib"
LIB_VERSION = 2
STYLE_LIB_NAME = "_风格库.excalidrawlib"
CHARTS_DIR = "图表库"
STYLE_LIB_REL = f"{CHARTS_DIR}/{STYLE_LIB_NAME}"
GROUPS = ("角色组", "卡片组", "连线组", "色卡组")
#: 角色组缺省三件套（中性：任何课程都不违和）；按课程配置见 ``course.meta["roles"]``
DEFAULT_ROLES: tuple[tuple[str, str], ...] = (
    ("讲解者", "\U0001F9D1\u200D\U0001F3EB"),   # 🧑‍🏫
    ("学员", "\U0001F9D1\u200D\U0001F393"),     # 🧑‍🎓
    ("主题", "\U0001F3AF"),                     # 🎯
)
#: ``course.meta["roles"]`` 允许的条数（1–6）
ROLE_LIMIT = 6
# 固定时间戳：Excalidraw 只把它当元数据，固定后产物可复现（不随时间变化）
CREATED_TS = 1_700_000_000
UPDATED_TS = 1_700_000_000_000
FONT_HAND = 2          # Virgil 手写体，与课程图一致
INK = "#374151"
MUTED = "#64748b"
LINE_HEIGHT = 1.25

logger = logging.getLogger(__name__)


def _stable_int(*parts: str) -> int:
    """由名字派生的稳定整数（seed / versionNonce 用，保证产物可复现）。"""
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _item_id(name: str) -> str:
    return "cg-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:16]


def _tint(color: str, ratio: float = 0.86) -> str:
    """把主题色兑白成淡底（Excalidraw 只认 6 位 hex，不用 8 位透明写法）。"""
    raw = str(color or "").lstrip("#")[:6]
    if len(raw) != 6:
        return "#f1f5f9"
    channels = [int(raw[i:i + 2], 16) for i in (0, 2, 4)]
    mixed = [round(c + (255 - c) * ratio) for c in channels]
    return "#" + "".join(f"{c:02x}" for c in mixed)


def _element(etype: str, eid: str, *, name: str, salt: str, x: float = 0.0, y: float = 0.0,
             width: float = 0.0, height: float = 0.0, **extra: Any) -> dict[str, Any]:
    """单个元素的公共字段（id/seed/versionNonce 全部由 name+salt 派生）。"""
    el: dict[str, Any] = {
        "id": eid, "type": etype, "x": x, "y": y, "width": width, "height": height,
        "angle": 0, "strokeColor": INK, "backgroundColor": "transparent",
        "fillStyle": "solid", "strokeWidth": 2, "strokeStyle": "solid",
        "roughness": 1, "roundness": None, "opacity": 100,
        "seed": _stable_int(name, salt, "seed"),
        "version": 1, "versionNonce": _stable_int(name, salt, "nonce"),
        "updated": UPDATED_TS,
    }
    el.update(extra)
    return el


def _text(text: str, eid: str, *, name: str, salt: str, x: float, y: float,
          width: float, height: float, size: int = 18, container: str | None = None,
          color: str = INK) -> dict[str, Any]:
    return _element("text", eid, name=name, salt=salt, x=x, y=y, width=width, height=height,
                    text=text, fontSize=size, fontFamily=FONT_HAND,
                    textAlign="center", verticalAlign="middle",
                    containerId=container, lineHeight=LINE_HEIGHT,
                    autoResize=container is None, strokeColor=color)


def _rounded_card(name: str, label: str, *, fill: str, stroke: str, width: float = 240.0,
                  height: float = 120.0, size: int = 18, salt: str = "card",
                  dx: float = 0.0) -> list[dict[str, Any]]:
    """圆角卡 + 绑定在卡里的文字（拖卡片时文字跟着走）。"""
    base = f"{_item_id(name)}-{salt}"
    rect_id, text_id = f"{base}-box", f"{base}-label"
    box = _element("rectangle", rect_id, name=name, salt=f"{salt}-box", x=dx,
                   width=width, height=height,
                   backgroundColor=fill, strokeColor=stroke, roundness={"type": 3},
                   boundElements=[{"id": text_id, "type": "text"}])
    label_el = _text(label, text_id, name=name, salt=f"{salt}-label", x=dx, y=0, width=width,
                     height=height, size=size, container=rect_id)
    return [box, label_el]


def _item(name: str, elements: list[dict[str, Any]]) -> dict[str, Any]:
    """库条目：先过 normalize_document 补全字段，再原样收进 libraryItems。"""
    doc: dict[str, Any] = {
        "type": "excalidraw", "version": 2, "source": "https://excalidraw.com",
        "elements": elements, "appState": {"gridSize": None, "viewBackgroundColor": "#ffffff"},
        "files": {},
    }
    normalize_document(doc)
    return {"id": _item_id(name), "status": "unpublished", "elements": doc["elements"],
            "created": CREATED_TS, "name": name}


def _roles_of(course: Course | None) -> tuple[list[tuple[str, str]], str]:
    """读 ``course.meta["roles"]``（``[{"label": …, "emoji": …}, …]``，1–6 条）。

    合法 ⇒ 原样返回；没配 ⇒ 缺省三件套且不告警；配错 ⇒ 缺省三件套 + 一句告警文案
    （调用方记 warn，**不抛异常**：风格库是附属产物，一个可选配置写坏不能拖垮整课导出）。
    """
    raw = dict(getattr(course, "meta", None) or {}).get("roles")
    if raw is None:
        return list(DEFAULT_ROLES), ""
    if not isinstance(raw, list) or not raw:
        return list(DEFAULT_ROLES), f"应为 1–{ROLE_LIMIT} 条非空列表，实得 {raw!r}"
    if len(raw) > ROLE_LIMIT:
        return list(DEFAULT_ROLES), f"最多 {ROLE_LIMIT} 条，实得 {len(raw)} 条"
    roles: list[tuple[str, str]] = []
    for index, entry in enumerate(raw, 1):
        if not isinstance(entry, dict):
            return list(DEFAULT_ROLES), f"第 {index} 项应为 {{'label': …, 'emoji': …}}，实得 {entry!r}"
        label = str(entry.get("label") or "").strip()
        emoji = str(entry.get("emoji") or "").strip()
        if not label or not emoji:
            return list(DEFAULT_ROLES), f"第 {index} 项 label/emoji 不能为空，实得 {entry!r}"
        roles.append((label, emoji))
    return roles, ""


def _role_items(colors: list[str], roles: list[tuple[str, str]] | None = None) -> list[dict[str, Any]]:
    """角色组：emoji + 气泡卡片（角色来自 ``course.meta["roles"]``，缺省中性三件套）。"""
    out: list[dict[str, Any]] = []
    for i, (label, emoji) in enumerate(roles if roles is not None else DEFAULT_ROLES):
        name = f"角色组 · {label}"
        color = colors[i % len(colors)]
        elements = _rounded_card(name, label, fill=_tint(color), stroke=color, size=20)
        elements.append(_text(emoji, f"{_item_id(name)}-emoji", name=name, salt="emoji",
                              x=250, y=36, width=48, height=48, size=36))
        out.append(_item(name, elements))
    return out


def _card_items(colors: list[str]) -> list[dict[str, Any]]:
    """卡片组：圆角卡 / 对比栏 / 环形底座 / 时间轴底座 / 决策菱形。"""
    out: list[dict[str, Any]] = []
    name = "卡片组 · 圆角卡"
    out.append(_item(name, _rounded_card(name, "要点", fill=_tint(colors[0]), stroke=colors[0])))

    name = "卡片组 · 对比栏"
    left = _rounded_card(name, "方案 A", fill=_tint(colors[0]), stroke=colors[0],
                         width=200.0, salt="left")
    right = _rounded_card(name, "方案 B", fill=_tint(colors[1]), stroke=colors[1],
                          width=200.0, salt="right", dx=220.0)
    out.append(_item(name, [*left, *right]))

    name = "卡片组 · 环形底座"
    ring_id, ring_text = f"{_item_id(name)}-ring", f"{_item_id(name)}-label"
    ring = _element("ellipse", ring_id, name=name, salt="ring", width=170, height=170,
                    backgroundColor=_tint(colors[2]), strokeColor=colors[2],
                    boundElements=[{"id": ring_text, "type": "text"}])
    out.append(_item(name, [ring, _text("环节", ring_text, name=name, salt="label",
                                        x=0, y=0, width=170, height=170, container=ring_id)]))

    name = "卡片组 · 时间轴底座"
    axis = _element("line", f"{_item_id(name)}-axis", name=name, salt="axis", width=320, height=0,
                    points=[[0, 0], [320, 0]], startBinding=None, endBinding=None,
                    startArrowhead=None, strokeColor=MUTED)
    dots = [_element("ellipse", f"{_item_id(name)}-dot{i}", name=name, salt=f"dot{i}",
                     x=float(i * 150), y=-7.0, width=14, height=14,
                     backgroundColor=colors[i % len(colors)], strokeColor=colors[i % len(colors)])
            for i in range(3)]
    out.append(_item(name, [axis, *dots]))

    name = "卡片组 · 决策菱形"
    diamond_id, diamond_text = f"{_item_id(name)}-diamond", f"{_item_id(name)}-label"
    diamond = _element("diamond", diamond_id, name=name, salt="diamond", width=190, height=130,
                       backgroundColor=_tint(colors[3]), strokeColor=colors[3],
                       boundElements=[{"id": diamond_text, "type": "text"}])
    out.append(_item(name, [diamond, _text("判断", diamond_text, name=name, salt="label",
                                           x=0, y=0, width=190, height=130, container=diamond_id)]))

    return out


def _connector_items(colors: list[str]) -> list[dict[str, Any]]:
    """连线组：实线箭头 / 虚线箭头 / 括号线。"""
    out: list[dict[str, Any]] = []
    for label, style in (("实线箭头", "solid"), ("虚线箭头", "dashed")):
        name = f"连线组 · {label}"
        arrow = _element("arrow", f"{_item_id(name)}-arrow", name=name, salt="arrow",
                         width=180, height=0, points=[[0, 0], [180, 0]],
                         strokeColor=colors[0], strokeStyle=style,
                         startArrowhead=None, endArrowhead="arrow")
        out.append(_item(name, [arrow]))

    name = "连线组 · 括号线"
    brace = _element("line", f"{_item_id(name)}-brace", name=name, salt="brace",
                     width=18, height=80, points=[[0, -40], [9, -40], [9, -3], [18, 0],
                                                  [9, 3], [9, 40], [0, 40]],
                     strokeColor=MUTED, startBinding=None, endBinding=None,
                     startArrowhead=None)
    out.append(_item(name, [brace]))
    return out


def _swatch_items(colors: list[str]) -> list[dict[str, Any]]:
    """色卡组：当前主题的 6 色（带色值文字，方便教师抄色号）。"""
    out: list[dict[str, Any]] = []
    for i, color in enumerate(colors, 1):
        name = f"色卡组 · 主题色{i}"
        elements = _rounded_card(name, color.upper(), fill=color, stroke=INK,
                                 width=140.0, height=96.0, size=16)
        out.append(_item(name, elements))
    return out


def build_library(theme: str | None = None, course: Course | None = None) -> dict[str, Any]:
    """构造 `.excalidrawlib` 文档（4 组、条目顺序固定）。

    角色组读 ``course.meta["roles"]``（1–6 条、每项 label/emoji 非空）；没配或配错时
    回落中性三件套，配错另记一条 warn（不抛异常）。
    """
    roles, role_problem = _roles_of(course)
    if role_problem:
        logger.warning("风格库角色组配置不合法，回落默认（course=%s）：%s",
                       getattr(course, "id", None), role_problem)
    spec, _error = resolve_theme(theme or get_settings().ppt_theme)
    colors = [f"#{str(c).lstrip('#')}" for c in spec["palette"]["colors"]]
    items = [
        *_role_items(colors, roles),
        *_card_items(colors),
        *_connector_items(colors),
        *_swatch_items(colors),
    ]
    return {"type": LIB_TYPE, "version": LIB_VERSION, "source": "https://excalidraw.com",
            "libraryItems": items}


def library_text(theme: str | None = None, course: Course | None = None) -> str:
    """序列化（缩进 2、保留中文），便于人肉查看与逐字节比对。"""
    return json.dumps(build_library(theme, course), ensure_ascii=False, indent=2) + "\n"


def library_stats(library: dict[str, Any], theme: str | None = None) -> dict[str, Any]:
    """条目数 / 元素数 / 每组条目数（GET 预览与任务结果共用）。"""
    items = library.get("libraryItems") or []
    groups: dict[str, int] = {}
    elements = 0
    for item in items:
        name = str(item.get("name") or "")
        group = name.split(" ", 1)[0] or "未分组"
        groups[group] = groups.get(group, 0) + 1
        elements += len(item.get("elements") or [])
    spec, theme_error = resolve_theme(theme or get_settings().ppt_theme)
    return {"items": len(items), "elements": elements, "groups": groups,
            "theme": spec["id"], "theme_name": spec["name"], "theme_error": theme_error,
            "names": [str(item.get("name") or "") for item in items]}


def validate_library(library: dict[str, Any]) -> list[str]:
    """库级 + 条目级自检（条目按 excalidraw 文档口径复检），返回问题清单。"""
    problems: list[str] = []
    if library.get("type") != LIB_TYPE:
        problems.append(f"顶层 type 应为 {LIB_TYPE}，实际 {library.get('type')!r}")
    if int(library.get("version") or 0) != LIB_VERSION:
        problems.append(f"顶层 version 应为 {LIB_VERSION}，实际 {library.get('version')!r}")
    items = library.get("libraryItems")
    if not isinstance(items, list) or not items:
        return problems + ["libraryItems 为空"]
    for item in items:
        name = str(item.get("name") or "未命名")
        for key in ("id", "name", "elements", "created"):
            if item.get(key) in (None, "", []):
                problems.append(f"{name}: 缺字段 {key}")
        doc = {"type": "excalidraw", "version": 2, "appState": {}, "files": {},
               "elements": item.get("elements") or []}
        problems += [f"{name}: {problem}" for problem in validate_document(doc)]
    return problems


def write_style_library(db: Session, course: Course) -> dict[str, Any]:
    """生成并写入 ``图表库/_风格库.excalidrawlib``（同名覆盖，重复导出字节一致）。"""
    from app.services.kb import KB_KEY, _course_out_dir, _now_iso, _write_safe, course_kb

    root, out_dir, rel = _course_out_dir(course)
    library = build_library(course=course)
    problems = validate_library(library)
    if problems:
        raise ValueError("风格库自检未通过：" + "；".join(problems[:3]))

    kb = course_kb(course)
    manifest = dict(kb.get("manifest") or {})
    written: list[str] = []
    preserved: list[dict] = []
    (out_dir / CHARTS_DIR).mkdir(parents=True, exist_ok=True)
    text = json.dumps(library, ensure_ascii=False, indent=2) + "\n"
    _write_safe(out_dir, STYLE_LIB_REL, text, manifest, True, root, preserved, written)

    kb["manifest"] = manifest
    kb["style_library_at"] = _now_iso()
    meta = dict(course.meta or {})
    meta[KB_KEY] = kb
    course.meta = meta
    db.commit()
    return {"ok": True, "vault_path": str(root), "output_rel": rel, "file": STYLE_LIB_REL,
            "files": written, "preserved": preserved, "stats": library_stats(library)}
