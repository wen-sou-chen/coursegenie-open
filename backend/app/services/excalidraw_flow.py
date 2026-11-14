# -*- coding: utf-8 -*-
"""把 CourseGenie 流程图的 mermaid 文本转换为 Excalidraw JSON（.excalidraw）。

供 Obsidian + Excalidraw 插件直接查看/编辑；导出时写入课程 03图表库，
页面用 ![[图-流程图-xxx.excalidraw]] 双链引用（图归图库、课挂双链）。

支持 flowchart 常见子集：矩形 []、菱形 {}、圆角 () 节点与 --> 连线（可带 |文字| 边标签）。
"""
from __future__ import annotations

import json
import re
import time

from app.services.excalidraw_format import normalize_document
from app.services.excalidraw_layout import INK_DARK
from app.services.figure_png import FigurePalette, figure_palette

_R_NODE = re.compile(
    r"^([A-Za-z0-9_\u4e00-\u9fff]+?)(?:\[([^\]]*)\]|\{([^}]*)\}|\(([^)]*)\))?\s*$"
)

_FONT = 20
_FONT_EDGE = 14

# 任务 107（可读性口径）：节点尺寸由标签文字度量推导（见 _node_size），字号与画布跨度
# 同比放大 ⇒ 上页后的等效磅数不变。要真正放大图内正文得改「一屏放几个节点」的版面结构；
# 现阶段的执行点在渲染侧 figure_png.min_text_px()（按显示档位抬字号并记进 layout_notes）。
_X0 = 120
_Y0 = 140
_COL = 360
_ROW = 190
# 任务 121：图内色不再写死（原 rect 蓝 / diamond 琥珀 / round 绿），改为**从 PPT 主题派生** ——
# 三个形状各占一个主题色槽，填充 = 该色的同色阶浅档、描边 = 主题色原样；换主题整套跟着换。
_SHAPE_SLOTS = {"rect": 0, "diamond": 1, "round": 2}


def _style_of(shape: str, pal: FigurePalette) -> dict:
    """节点描边 / 填充（主题色槽 + 同色阶浅档；口径正本 = figure_png.FigurePalette）。"""
    slot = _SHAPE_SLOTS.get(str(shape), 0)
    return {"strokeColor": pal.base(slot), "backgroundColor": pal.fill(slot)}
_TYPE = {"rect": "rectangle", "diamond": "diamond", "round": "rectangle"}


def _nid(prefix: str, token: str | int) -> str:
    """确定性元素 id：同一份 mermaid 每次转换得到同一份文件（指纹稳定、少重写）。"""
    return f"{prefix}-{token}"


def _parse(mermaid: str) -> tuple[dict, list]:
    """解析 mermaid 为 (nodes, edges)。nodes 保序：{id: {id,label,shape}}。"""
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def ensure(nid: str, label: str, shape: str) -> None:
        if nid not in nodes:
            nodes[nid] = {"id": nid, "label": label, "shape": shape}

    for raw in (mermaid or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(("graph", "flowchart", "subgraph", "end", "%%", "classDef", "style")):
            continue
        if "-->" in line:
            left, _, right = line.partition("-->")
            left = left.strip()
            right = right.strip()
            elabel = ""
            if right.startswith("|"):
                bar = right.find("|", 1)
                if bar > 0:
                    elabel = right[1:bar]
                    right = right[bar + 1:].strip()
            lm = _R_NODE.match(left)
            rm = _R_NODE.match(right)
            if not lm or not rm:
                continue
            lid, llabel, lshape = _node_def(lm)
            rid, rlabel, rshape = _node_def(rm)
            ensure(lid, llabel, lshape)
            ensure(rid, rlabel, rshape)
            edges.append({"from": lid, "to": rid, "label": elabel})
        else:
            m = _R_NODE.match(line)
            if m:
                nid, label, shape = _node_def(m)
                ensure(nid, label, shape)
    return nodes, edges


def _node_def(m: re.Match) -> tuple[str, str, str]:
    nid = m.group(1)
    if m.group(2) is not None:
        return nid, m.group(2), "rect"
    if m.group(3) is not None:
        return nid, m.group(3), "diamond"
    if m.group(4) is not None:
        return nid, m.group(4), "round"
    return nid, nid, "rect"


def _rank_map(nodes: dict, edges: list) -> dict:
    rank = {nid: 0 for nid in nodes}
    for _ in range(len(nodes) + 2):
        changed = False
        for e in edges:
            if e["to"] in rank and rank[e["to"]] < rank[e["from"]] + 1:
                rank[e["to"]] = rank[e["from"]] + 1
                changed = True
        if not changed:
            break
    return rank


def _node_size(node: dict) -> tuple[float, float]:
    label = node["label"]
    if node["shape"] == "diamond":
        return max(200.0, len(label) * _FONT + 100), 96.0
    return max(160.0, len(label) * _FONT + 60), 68.0


def _elem(node: dict, x: float, y: float, w: float, h: float, ts: int,
          pal: FigurePalette, seq: int = 0) -> dict:
    style = _style_of(node["shape"], pal)
    roundness = {"type": 3} if node["shape"] in ("rect", "round") else None
    return {
        "id": f"n-{node['id']}",
        "type": _TYPE[node["shape"]],
        "x": round(x, 1), "y": round(y, 1),
        "width": round(w, 1), "height": round(h, 1),
        "angle": 0,
        "strokeColor": style["strokeColor"],
        "backgroundColor": style["backgroundColor"],
        "fillStyle": "solid",
        "strokeWidth": 2,
        "strokeStyle": "solid",
        "roughness": 1,
        "opacity": 100,
        "groupIds": [], "frameId": None,
        "roundness": roundness,
        "seed": (seq * 7919 + 104729) % (2 ** 31),
        "version": 1,
        "versionNonce": (seq * 104729 + 7919) % (2 ** 31),
        "isDeleted": False,
        "boundElements": [],
        "updated": ts,
        "link": None,
        "locked": False,
    }


def _text(text: str, x: float, y: float, size: int, color: str, ts: int,
          container: str | None = None, *, eid: str, seq: int,
          pal: FigurePalette) -> dict:
    el = _elem({"id": container or "t", "shape": "round"}, x, y,
               1.0, 1.0, ts, pal, seq)
    el.update({
        "id": eid,
        "type": "text",
        "strokeColor": color,
        "backgroundColor": "transparent",
        "roundness": None,
        "text": text, "rawText": text, "originalText": text,
        "fontSize": size, "fontFamily": 5,
        "textAlign": "center", "verticalAlign": "middle",
        "containerId": container,
        "autoResize": True, "lineHeight": 1.25,
    })
    if container:
        el["width"] = max(1, round(len(text) * size))
        el["height"] = size
    else:
        el["width"] = max(1, round(len(text) * size))
        el["height"] = size
    return el


def _arrow(sx: float, sy: float, ex: float, ey: float, ts: int, *,
           eid: str, seq: int) -> dict:
    x = min(sx, ex)
    y = min(sy, ey)
    return {
        "id": eid,
        "type": "arrow",
        "x": round(x, 1), "y": round(y, 1),
        "width": round(max(abs(ex - sx), 1), 1),
        "height": round(max(abs(ey - sy), 1), 1),
        "angle": 0,
        "strokeColor": "#475569",
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": 2,
        "strokeStyle": "solid",
        "roughness": 1,
        "opacity": 100,
        "groupIds": [], "frameId": None,
        "roundness": {"type": 2},
        "seed": (seq * 7919 + 104729) % (2 ** 31),
        "version": 1,
        "versionNonce": (seq * 104729 + 7919) % (2 ** 31),
        "isDeleted": False,
        "boundElements": [],
        "updated": ts,
        "link": None,
        "locked": False,
        "points": [
            [round(sx - x, 1), round(sy - y, 1)],
            [round(ex - x, 1), round(ey - y, 1)],
        ],
        "startArrowhead": None,
        "endArrowhead": "arrow",
        "elbowed": False,
    }


def mermaid_to_excalidraw_json(mermaid: str, *, theme: str | None = None) -> dict | None:
    """mermaid -> Excalidraw JSON dict；解析失败或无节点返回 None。

    ``theme`` 给 PPT 主题（留空用后端 ``PPT_THEME`` 配置）：图内配色由它派生
    （:func:`app.services.figure_png.figure_palette`），与概念图 / 数据图同一出处。
    """
    pal = figure_palette(theme)
    nodes, edges = _parse(mermaid)
    if not nodes:
        return None
    rank = _rank_map(nodes, edges)
    ts = int(time.time() * 1000)
    pos: dict[str, tuple] = {}
    by_rank: dict[int, list] = {}
    for nid in nodes:
        by_rank.setdefault(rank[nid], []).append(nid)
    for r in sorted(by_rank):
        for i, nid in enumerate(by_rank[r]):
            w, h = _node_size(nodes[nid])
            pos[nid] = (_X0 + i * _COL, _Y0 + r * _ROW, w, h)
    elements: list[dict] = []
    seq = 0
    for nid in nodes:
        x, y, w, h = pos[nid]
        seq += 1
        box = _elem(nodes[nid], x, y, w, h, ts, pal, seq)
        tx = x + 10
        ty = y + h / 2 - _FONT * 0.6
        txt = _text(nodes[nid]["label"], tx, ty, _FONT, INK_DARK, ts,
                    container=box["id"], eid=_nid("t", nid), seq=seq, pal=pal)
        box["boundElements"].append({"id": txt["id"], "type": "text"})
        elements.extend([box, txt])
    for i, e in enumerate(edges):
        fx, fy, fw, fh = pos[e["from"]]
        tx, ty, tw, th = pos[e["to"]]
        if rank[e["to"]] > rank[e["from"]]:
            s = (fx + fw / 2, fy + fh)
            t = (tx + tw / 2, ty)
        else:
            s = (fx + fw, fy + fh / 2)
            t = (tx, ty + th / 2)
        seq += 1
        elements.append(_arrow(s[0], s[1], t[0], t[1], ts,
                               eid=_nid("a", i), seq=seq))
        if e.get("label"):
            mx = (s[0] + t[0]) / 2 + 12
            my = (s[1] + t[1]) / 2 - 12
            seq += 1
            elements.append(_text(e["label"], mx, my, _FONT_EDGE, "#334155", ts,
                                  eid=_nid("l", i), seq=seq, pal=pal))
    for i, el in enumerate(elements):
        el["index"] = f"a{i}"
    return normalize_document({
        "type": "excalidraw",
        "version": 2,
        "source": "https://excalidraw.com",
        "elements": elements,
        "appState": {"gridSize": None, "viewBackgroundColor": pal.bg},
        "files": {},
    })


def mermaid_to_excalidraw_text(mermaid: str) -> str | None:
    data = mermaid_to_excalidraw_json(mermaid)
    if data is None:
        return None
    return json.dumps(data, ensure_ascii=False, indent=1)
