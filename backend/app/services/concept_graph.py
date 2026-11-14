"""知识图谱导出（任务 24-A）：概念库 → Mermaid 图 + Obsidian Canvas。

数据源只有概念库（``title`` / ``body.definition`` / ``body.relations`` / ``meta.chapters``），
不另算一套：概念名 → 概念的解析沿用 :func:`concept.link_index` 的口径。

产出两件（写进 ``课程/<课名>/概念库/``）：

- ``_图谱.md``：Mermaid ``graph TD``，按章节分子图并分色，Obsidian 原生即可渲染；
- ``_图谱.canvas``：Obsidian Canvas JSON，节点=概念卡（含定义摘要），边=关系。

口径：只有**两端都在概念库**的关系才画边；指向库外的关系（对不上任何概念名或别名）
列进 ``dropped_relations``，不静默丢弃；没有任何关系的概念列进 ``isolated``。
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.course import Course
from app.services import concept as concept_service
from app.services.concept import chapter_key_of, list_concepts, normalize_key
from app.services.planner import list_nodes
from app.services.ppt_theme import resolve_theme

GRAPH_STEM = "_图谱"
CONCEPTS_DIR = concept_service.CONCEPTS_DIR
GRAPH_MD_REL = f"{CONCEPTS_DIR}/{GRAPH_STEM}.md"
GRAPH_CANVAS_REL = f"{CONCEPTS_DIR}/{GRAPH_STEM}.canvas"

# (body.relations 字段, 中文标签, 方向)：broader 的边从「上位概念」指向本概念
RELATION_KINDS = (
    ("broader", "上位", "parent"),
    ("narrower", "下位", "child"),
    ("related", "相关", "related"),
)
DEFINITION_CHARS = 88
CANVAS_W, CANVAS_H = 320, 132
CANVAS_X0, CANVAS_Y0 = 40.0, 40.0
CANVAS_GAP_X, CANVAS_GAP_Y = 400.0, 176.0
UNASSIGNED = "未归属章节"


def chapter_palette() -> list[str]:
    """章节配色：复用 PPT 主题色板（换主题时图谱与课件一起变）。"""
    spec, _ = resolve_theme(get_settings().ppt_theme)
    return [f"#{str(color).lstrip('#')}" for color in spec["palette"]["colors"]]


def chapter_order(db: Session, course: Course) -> list[tuple[str, str]]:
    """``[(章 key, 章标题)]``：按大纲顺序，分组 / 配色 / Canvas 排布共用。"""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for node in list_nodes(db, course.id):
        if "." in node.node_key:  # 带点的是知识点，不是章
            continue
        key = chapter_key_of(node.node_key)
        if key in seen:
            continue
        seen.add(key)
        out.append((key, node.title))
    return out


def _snippet(text: Any, limit: int = DEFINITION_CHARS) -> str:
    body = " ".join(str(text or "").split())
    return body if len(body) <= limit else body[: limit - 1] + "…"


def _mermaid_label(text: str) -> str:
    """Mermaid 节点标签：去掉会破坏语法的引号与换行。"""
    return str(text or "").replace('"', "'").replace("\n", " ").strip()


def graph_data(db: Session, course: Course) -> dict[str, Any]:
    """概念库 → ``{nodes, edges, isolated, dropped_relations, chapters, stats}``。"""
    rows = list_concepts(db, course.id)
    chapters = chapter_order(db, course)
    chapter_index = {key: i for i, (key, _title) in enumerate(chapters)}

    nodes: list[dict[str, Any]] = []
    by_name: dict[str, str] = {}
    for i, row in enumerate(rows, 1):
        node_id = f"c{i}"
        meta = row.meta or {}
        body = row.body or {}
        keys = [chapter_key_of(key) for key in (meta.get("chapters") or [])]
        own = keys[0] if keys and keys[0] in chapter_index else ""
        nodes.append({
            "id": node_id,
            "name": row.title,
            "definition": _snippet(body.get("definition")),
            "points": [str(p) for p in (body.get("points") or [])][:5],
            "aliases": [str(a) for a in (meta.get("aliases") or [])],
            "chapters": [key for key in keys if key],
            "chapter": own,
            "chapter_index": chapter_index.get(own, len(chapters)),
        })
        by_name.setdefault(normalize_key(row.title), node_id)
        for alias in meta.get("aliases") or []:
            by_name.setdefault(normalize_key(alias), node_id)

    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    dropped: list[dict[str, str]] = []
    for node, row in zip(nodes, rows):
        relations = (row.body or {}).get("relations") or {}
        for field, label, direction in RELATION_KINDS:
            for raw in relations.get(field) or []:
                target_name = str(raw or "").strip()
                if not target_name:
                    continue
                target = by_name.get(normalize_key(target_name))
                if target is None:
                    dropped.append({"from": node["name"], "kind": label, "target": target_name})
                    continue
                if target == node["id"]:  # 自环不画，也不算孤立
                    continue
                if direction == "parent":
                    source, dest, dedup = target, node["id"], (target, node["id"])
                elif direction == "child":
                    source, dest, dedup = node["id"], target, (node["id"], target)
                else:
                    pair = sorted([node["id"], target])
                    source, dest, dedup = pair[0], pair[1], (pair[0], pair[1])
                key = (direction, dedup[0], dedup[1])
                if key in seen:
                    continue
                seen.add(key)
                edges.append({"id": f"e{len(edges) + 1}", "source": source, "target": dest,
                              "kind": field, "label": label,
                              "directed": direction != "related"})

    degree = {node["id"]: 0 for node in nodes}
    for edge in edges:
        degree[edge["source"]] += 1
        degree[edge["target"]] += 1
    isolated = [node["name"] for node in nodes if degree[node["id"]] == 0]
    return {
        "course_id": course.id,
        "course_title": course.title,
        "nodes": nodes,
        "edges": edges,
        "isolated": isolated,
        "dropped_relations": dropped,
        "chapters": [{"key": key, "title": title} for key, title in chapters],
        "stats": {"nodes": len(nodes), "edges": len(edges),
                  "isolated": len(isolated), "dropped": len(dropped)},
    }


def to_mermaid(data: dict[str, Any]) -> str:
    """Mermaid ``graph TD``：按章节分子图 + 分色，节点/边与 ``data`` 一一对应。"""
    nodes = data.get("nodes") or []
    edges = data.get("edges") or []
    chapters = data.get("chapters") or []
    palette = chapter_palette()
    isolated = data.get("isolated") or []

    buckets: dict[str, list[dict]] = {}
    for node in nodes:
        buckets.setdefault(node.get("chapter") or UNASSIGNED, []).append(node)

    lines = ["%% CourseGenie 概念库知识图谱（自动生成，重新生成会覆盖）",
             f"%% nodes={len(nodes)} edges={len(edges)} isolated={len(isolated)}",
             "graph TD"]
    for i, color in enumerate(palette):
        lines.append(f"  classDef ch{i + 1} fill:{color}33,stroke:{color},"
                     "stroke-width:2px,color:#1f2937;")
    lines.append("  classDef bare fill:#f1f5f9,stroke:#94a3b8,color:#475569;")

    order = [row["key"] for row in chapters] + [UNASSIGNED]
    class_of: dict[str, str] = {}
    for i, key in enumerate(order, 1):
        bucket = buckets.get(key)
        if not bucket:
            continue
        title = next((row["title"] for row in chapters if row["key"] == key), UNASSIGNED)
        label = UNASSIGNED if key == UNASSIGNED else f"第{key}章 {title}"
        css = f"ch{min(i, len(palette))}" if key != UNASSIGNED else "bare"
        lines.append(f'  subgraph CH{i}["{_mermaid_label(label)}"]')
        for node in bucket:
            lines.append(f'    {node["id"]}["{_mermaid_label(node["name"])}"]')
            class_of[node["id"]] = css
        lines.append("  end")
    for node in nodes:  # 兜底：没有章节信息的节点也要出现在图里
        if node["id"] not in class_of:
            lines.append(f'  {node["id"]}["{_mermaid_label(node["name"])}"]')
            class_of[node["id"]] = "bare"

    for edge in edges:
        arrow = "-->" if edge.get("directed") else "-.->"
        lines.append(f'  {edge["source"]} {arrow}|{edge["label"]}| {edge["target"]}')

    by_css: dict[str, list[str]] = {}
    for node_id, css in class_of.items():
        by_css.setdefault(css, []).append(node_id)
    for css, ids in by_css.items():
        lines.append(f"  class {','.join(ids)} {css};")
    if isolated:
        lines.append(f"  %% 孤立概念（没有任何关系）：{'、'.join(isolated)}")
    return "\n".join(lines) + "\n"


def to_canvas(data: dict[str, Any]) -> dict[str, Any]:
    """Obsidian Canvas JSON：按章节横向排布的概念卡 + 关系边。"""
    palette = chapter_palette()
    nodes: list[dict[str, Any]] = []
    offsets: dict[str, int] = {}
    for node in data.get("nodes") or []:
        key = node.get("chapter") or UNASSIGNED
        row = offsets.get(key, 0)
        offsets[key] = row + 1
        index = int(node.get("chapter_index") or 0)
        text = f"**{node['name']}**"
        if node.get("definition"):
            text += f"\n\n{node['definition']}"
        item = {
            "id": node["id"],
            "type": "text",
            "text": text,
            "x": CANVAS_X0 + CANVAS_GAP_X * index,
            "y": CANVAS_Y0 + CANVAS_GAP_Y * row,
            "width": CANVAS_W,
            "height": CANVAS_H,
        }
        if key != UNASSIGNED:
            item["color"] = str(index % max(len(palette), 1) + 1)
        nodes.append(item)
    edges = [{
        "id": edge["id"],
        "fromNode": edge["source"],
        "fromSide": "right",
        "toNode": edge["target"],
        "toSide": "left",
        "label": edge["label"],
    } for edge in data.get("edges") or []]
    return {"nodes": nodes, "edges": edges}


def _link_map(db: Session, course: Course) -> dict[str, str]:
    """``{概念名: 可解析的双链目标}``——解析不到就不发双链（导出后 0 坏链）。"""
    index, valid = concept_service.link_index(db, course)
    out: dict[str, str] = {}
    for row in list_concepts(db, course.id):
        target = concept_service.resolve_target(row.title, index, valid)
        if target:
            out[row.title] = target
    return out


def graph_markdown(db: Session, course: Course, data: dict[str, Any]) -> str:
    """``_图谱.md``：Mermaid 图 + 统计 + 孤立概念 + 按章索引 + 库外目标。"""
    links = _link_map(db, course)

    def link(name: str) -> str:
        target = links.get(name)
        return f"[[{target}|{name}]]" if target else name

    stats = data["stats"]
    parts = [
        "---",
        f'title: "{course.title} · 概念知识图谱"',
        'tags: "coursegenie 概念库 知识图谱"',
        "---",
        "",
        f"# {course.title} · 概念知识图谱",
        "",
        "> 由概念库自动生成（重新生成会覆盖）；在 Obsidian 里直接看下面的图。",
        "",
        "```mermaid",
        to_mermaid(data).rstrip("\n"),
        "```",
        "",
        "## 统计",
        "",
        f"- 概念 **{stats['nodes']}** 个 ／ 关系 **{stats['edges']}** 条 ／ "
        f"孤立概念 **{stats['isolated']}** 个 ／ 库外目标 **{stats['dropped']}** 个",
        "",
    ]
    if data["isolated"]:
        parts += ["## 孤立概念（没有任何关系，建议补 broader / related）", ""]
        parts += [f"- {link(name)}" for name in data["isolated"]]
        parts += [""]
    parts += ["## 按章索引", ""]
    by_chapter: dict[str, list[str]] = {}
    for node in data["nodes"]:
        by_chapter.setdefault(node.get("chapter") or UNASSIGNED, []).append(node["name"])
    for row in data["chapters"]:
        names = by_chapter.get(row["key"]) or []
        if not names:
            continue
        parts += [f"### 第{row['key']}章 {row['title']}", ""]
        parts += [f"- {link(name)}" for name in names]
        parts += [""]
    others = by_chapter.get(UNASSIGNED) or []
    if others:
        parts += [f"### {UNASSIGNED}", ""]
        parts += [f"- {link(name)}" for name in others]
        parts += [""]
    if data["dropped_relations"]:
        parts += ["## 库外目标（对不上概念库的名字，未画边）", ""]
        parts += [f"- {row['target']}（来自「{row['from']}」的{row['kind']}关系）"
                  for row in data["dropped_relations"]]
        parts += [""]
    parts += ["---", ""]
    return "\n".join(parts).rstrip() + "\n"


def graph_payloads(db: Session, course: Course, *,
                   data: dict[str, Any] | None = None) -> list[tuple[str, str]]:
    """整课导出用：``[(vault 相对路径, 文本)]``；概念库为空时返回空列表（不写空图）。"""
    payload = data if data is not None else graph_data(db, course)
    if not payload["nodes"]:
        return []
    return [(GRAPH_MD_REL, graph_markdown(db, course, payload)),
            (GRAPH_CANVAS_REL, json.dumps(to_canvas(payload), ensure_ascii=False, indent=2) + "\n")]


def write_graph(db: Session, course: Course) -> dict[str, Any]:
    """生成并写入 ``概念库/_图谱.md`` + ``_图谱.canvas``（同名覆盖，幂等）。"""
    from app.services.kb import KB_KEY, _course_out_dir, _now_iso, _write_safe, course_kb

    root, out_dir, rel = _course_out_dir(course)
    data = graph_data(db, course)
    if not data["nodes"]:
        raise ValueError("概念库为空：请先在「概念库」抽一次概念，再导出知识图谱")

    kb = course_kb(course)
    manifest = dict(kb.get("manifest") or {})
    written: list[str] = []
    preserved: list[dict] = []
    (out_dir / CONCEPTS_DIR).mkdir(parents=True, exist_ok=True)
    for rel_path, text in graph_payloads(db, course, data=data):
        _write_safe(out_dir, rel_path, text, manifest, True, root, preserved, written)

    kb["manifest"] = manifest
    kb["graph_at"] = _now_iso()
    meta = dict(course.meta or {})
    meta[KB_KEY] = kb
    course.meta = meta
    db.commit()
    return {
        "ok": True,
        "vault_path": str(root),
        "output_rel": rel,
        "files": written,
        "preserved": preserved,
        "stats": data["stats"],
        "isolated": data["isolated"],
        "dropped_relations": data["dropped_relations"],
        "chapters": data["chapters"],
    }
