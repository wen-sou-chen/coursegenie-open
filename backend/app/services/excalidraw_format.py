"""Excalidraw 文档规范：对齐 Obsidian Excalidraw 插件（2.26.4）认的元素结构。

插件内部用的就是官方 `@excalidraw/excalidraw` 的元素 schema（插件 2.26.4 的
`mermaidToExcalidraw` 也来自这个包），所以本模块只做两件事，不改排版观感：

1. `normalize_document()`：把自研排版补成插件认得的形态——箭头与目标图形**双向绑定**
   （`startBinding`/`endBinding` + 目标 `boundElements`）、容器文字互指
   （`containerId` + `autoResize=false`）、唯一 id/index、显式 null 字段。
   绑定之后教师在 Obsidian 里拖动节点，箭头会跟着走；不绑定就是「死线」。
2. `validate_document()`：严格自检（必填字段、绑定对称、文字与容器互指、points 合法），
   供回归测试与导出前体检用。

背景：需求文档 v2 §3C 的 C1/C3（产出原生 .excalidraw、对齐插件 2.26.4 元素格式）。
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

BINDABLE = ("rectangle", "ellipse", "diamond")
LINEAR = ("arrow", "line")

# 元素公共必填字段（官方 schema 的最小集，插件读取时会用到）
REQUIRED_COMMON = (
    "id", "type", "x", "y", "width", "height", "angle", "opacity",
    "strokeColor", "backgroundColor", "fillStyle", "strokeWidth", "strokeStyle",
    "roughness", "roundness", "seed", "version", "versionNonce", "updated",
    "isDeleted", "groupIds", "frameId", "link", "locked",
)
REQUIRED_BY_TYPE: dict[str, tuple[str, ...]] = {
    "text": ("text", "fontSize", "fontFamily", "textAlign", "verticalAlign",
             "containerId", "lineHeight", "autoResize"),
    "arrow": ("points", "startBinding", "endBinding", "startArrowhead",
              "endArrowhead", "elbowed"),
    "line": ("points", "startBinding", "endBinding"),
    "rectangle": ("boundElements",),
    "ellipse": ("boundElements",),
    "diamond": ("boundElements",),
}


def _gap_to_box(px: float, py: float, x: float, y: float, w: float, h: float) -> float:
    """点到矩形边界的最短距离（点在矩形内时为 0）。"""
    dx = max(x - px, 0.0, px - (x + w))
    dy = max(y - py, 0.0, py - (y + h))
    return math.hypot(dx, dy)


def _focus_at(px: float, py: float, box: tuple[float, float, float, float],
              other: tuple[float, float]) -> float:
    """端点落在图形边的相对位置（Excalidraw 的 focus ∈ [0,1]）。"""
    x, y, w, h = box
    if abs(other[0] - px) >= abs(other[1] - py):
        value = (py - y) / h if h else 0.5
    else:
        value = (px - x) / w if w else 0.5
    return round(min(max(value, 0.0), 1.0), 3)


def bind_floating_arrows(elements: list[dict[str, Any]], *, tol: float = 14.0) -> int:
    """把「端点贴着某个图形」的箭头/连线绑定到该图形，返回新建的绑定数。

    只按几何关系判断（和 Excalidraw 里手画时自动吸附同一套语义）：端点离图形
    边界 ≤ tol 就算贴上；focus 取端点在该边上的相对位置，gap 取实测间距，
    这样绑定后观感与绑定前一致。
    """
    shapes = [e for e in elements
              if e.get("type") in BINDABLE and not e.get("isDeleted")]
    if not shapes:
        return 0
    made = 0
    for el in elements:
        if el.get("type") not in LINEAR or el.get("isDeleted"):
            continue
        points = el.get("points") or []
        if len(points) < 2:
            continue
        ox, oy = float(el.get("x") or 0.0), float(el.get("y") or 0.0)
        head = (ox + float(points[0][0]), oy + float(points[0][1]))
        tail = (ox + float(points[-1][0]), oy + float(points[-1][1]))
        for key, point, other in (("startBinding", head, tail),
                                  ("endBinding", tail, head)):
            if el.get(key):
                continue
            best: tuple[float, float, dict[str, Any]] | None = None
            for shape in shapes:
                box = (float(shape.get("x") or 0.0), float(shape.get("y") or 0.0),
                       float(shape.get("width") or 0.0), float(shape.get("height") or 0.0))
                gap = _gap_to_box(point[0], point[1], *box)
                if gap > tol:
                    continue
                area = box[2] * box[3]
                if best is None or (gap, area) < (best[0], best[1]):
                    best = (gap, area, shape)
            if best is None:
                continue
            shape = best[2]
            box = (float(shape.get("x") or 0.0), float(shape.get("y") or 0.0),
                   float(shape.get("width") or 0.0), float(shape.get("height") or 0.0))
            el[key] = {"elementId": shape["id"],
                       "focus": _focus_at(point[0], point[1], box, other),
                       "gap": round(best[0], 1)}
            bound = shape.setdefault("boundElements", [])
            if not any(isinstance(x, dict) and x.get("id") == el["id"] for x in bound):
                bound.append({"id": el["id"], "type": el.get("type") or "arrow"})
            made += 1
    return made


def normalize_document(doc: dict[str, Any], *, bind: bool = True,
                       tol: float = 14.0) -> dict[str, Any]:
    """就地补全文档结构；返回同一个 dict（方便链式调用）。"""
    elements = doc.get("elements")
    if not isinstance(elements, list):
        elements = []
    seen: set[str] = set()
    for i, el in enumerate(elements):
        original = str(el.get("id") or f"e{i}")
        eid = original
        while eid in seen:
            eid = f"{original}-{i}"
        el["id"] = eid
        seen.add(eid)
        el.setdefault("index", f"a{i}")
        el.setdefault("groupIds", [])
        el.setdefault("isDeleted", False)
        el.setdefault("frameId", None)
        el.setdefault("link", None)
        el.setdefault("locked", False)
        etype = el.get("type")
        if etype in BINDABLE:
            el.setdefault("boundElements", [])
            el.setdefault("roundness", {"type": 3} if etype == "rectangle" else None)
        if etype in LINEAR:
            el.setdefault("startBinding", None)
            el.setdefault("endBinding", None)
            el.setdefault("startArrowhead", None)
            if etype == "arrow":
                el.setdefault("endArrowhead", "arrow")
                el.setdefault("elbowed", False)
        if etype == "text":
            if el.get("containerId"):
                # 绑定到容器的文字由容器决定尺寸，插件规范要求 autoResize=false
                el["autoResize"] = False
            else:
                el.setdefault("autoResize", True)
    if bind:
        bind_floating_arrows(elements, tol=tol)
    doc["type"] = doc.get("type") or "excalidraw"
    doc["version"] = 2
    doc.setdefault("source", "https://excalidraw.com")
    doc.setdefault("appState", {"gridSize": None, "viewBackgroundColor": "#ffffff"})
    doc.setdefault("files", {})
    return doc


def validate_document(doc: dict[str, Any]) -> list[str]:
    """严格自检，返回问题清单（空 = 通过）。"""
    problems: list[str] = []
    if not isinstance(doc, dict):
        return ["文档不是 dict"]
    if doc.get("type") != "excalidraw":
        problems.append(f"顶层 type 应为 excalidraw，实际 {doc.get('type')!r}")
    if doc.get("version") != 2:
        problems.append(f"顶层 version 应为 2，实际 {doc.get('version')!r}")
    for key in ("appState", "files"):
        if not isinstance(doc.get(key), dict):
            problems.append(f"顶层 {key} 应为 dict")
    elements = doc.get("elements")
    if not isinstance(elements, list):
        return problems + ["elements 应为 list"]

    by_id: dict[str, dict[str, Any]] = {}
    indexes: list[str] = []
    for i, el in enumerate(elements):
        tag = f"#{i}({el.get('type')})"
        etype = el.get("type")
        eid = el.get("id")
        if not isinstance(eid, str) or not eid:
            problems.append(f"{tag} 缺 id")
        elif eid in by_id:
            problems.append(f"{tag} id 重复：{eid}")
        else:
            by_id[eid] = el
        for key in REQUIRED_COMMON:
            if key not in el:
                problems.append(f"{tag} 缺字段 {key}")
        for key in REQUIRED_BY_TYPE.get(etype, ()):
            if key not in el:
                problems.append(f"{tag} 缺字段 {key}")
        for key in ("x", "y", "width", "height"):
            value = el.get(key)
            if not isinstance(value, (int, float)):
                problems.append(f"{tag} {key} 应为数字")
            elif key in ("width", "height") and value < 0:
                problems.append(f"{tag} {key} 为负")
        if el.get("index"):
            indexes.append(str(el["index"]))
    if len(indexes) != len(set(indexes)):
        problems.append("index 有重复")

    # 绑定与互指必须等全部 id 登记完再查（绑定允许指向列表中靠后的元素）
    for i, el in enumerate(elements):
        tag = f"#{i}({el.get('type')})"
        etype = el.get("type")
        eid = el.get("id")
        if etype == "text":
            if el.get("containerId") and not str(el.get("text") or "").strip():
                problems.append(f"{tag} 容器文字为空")
            if el.get("containerId") and el.get("autoResize") is not False:
                problems.append(f"{tag} 绑定容器的文字必须 autoResize=false")
        if etype in LINEAR:
            points = el.get("points")
            if (not isinstance(points, list) or len(points) < 2
                    or any(not isinstance(p, (list, tuple)) or len(p) != 2 for p in points)):
                problems.append(f"{tag} points 至少 2 个 [x,y]")
            if etype == "arrow" and el.get("endArrowhead") not in ("arrow", "triangle", None):
                problems.append(f"{tag} endArrowhead 非法：{el.get('endArrowhead')!r}")
            for key in ("startBinding", "endBinding"):
                binding = el.get(key)
                if binding is None:
                    continue
                if not isinstance(binding, dict) or not binding.get("elementId"):
                    problems.append(f"{tag} {key} 结构不合法")
                    continue
                target = by_id.get(str(binding["elementId"]))
                if target is None:
                    problems.append(f"{tag} {key} 指向不存在的元素 {binding['elementId']}")
                elif target.get("type") not in BINDABLE:
                    problems.append(f"{tag} {key} 绑到了不可绑定的 {target.get('type')}")
                elif not any(isinstance(x, dict) and x.get("id") == eid
                             for x in (target.get("boundElements") or [])):
                    problems.append(f"{tag} {key} 缺少反向 boundElements")

    for el in elements:
        for be in el.get("boundElements") or []:
            if not isinstance(be, dict):
                problems.append(f"{el.get('id')} boundElements 项不是 dict")
                continue
            other = by_id.get(str(be.get("id")))
            if other is None:
                problems.append(f"{el.get('id')} boundElements 指向不存在的元素 {be.get('id')}")
            elif be.get("type") == "text" and other.get("containerId") != el.get("id"):
                problems.append(f"{el.get('id')} 的文字绑定未互指（{other.get('id')}）")
    return problems


def fingerprint(doc: dict[str, Any]) -> str:
    """文档稳定指纹：唯一忽略 `updated`（每次生成必然变化），比对两次产物是否等价。"""
    payload = json.loads(json.dumps(doc, ensure_ascii=False))
    for el in payload.get("elements") or []:
        el.pop("updated", None)
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def summarize(doc: dict[str, Any]) -> dict[str, Any]:
    """产出体检摘要：元素计数、绑定覆盖率（供日志与任务结果展示）。"""
    elements = doc.get("elements") or []
    linear = [e for e in elements if e.get("type") in LINEAR]
    texts = [e for e in elements if e.get("type") == "text"]
    bound = [e for e in linear if e.get("startBinding") or e.get("endBinding")]
    endpoints = sum(1 for e in linear
                    for key in ("startBinding", "endBinding") if e.get(key))
    return {
        "elements": len(elements),
        "shapes": sum(1 for e in elements if e.get("type") in BINDABLE),
        "linear": len(linear),
        "linear_bound": len(bound),
        "bindings": endpoints,
        "texts": len(texts),
        "texts_in_container": sum(1 for e in texts if e.get("containerId")),
    }
