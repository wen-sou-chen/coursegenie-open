"""内容生成：按知识点生成讲解文本（Markdown）、摘要、关键词，并批量落库。

规则：同一知识点再次生成时「原地替换」，旧版本自动进 meta.history（最多 5 条），
不无限堆积内容条目；课程绑定 Obsidian 知识库素材时自动注入作为创作依据。
"""
from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from app.models.course import ContentItem, Course, GenerationJob, Outline, OutlineNode, QuizItem
from app.services import usage as usage_log
from app.services.chart_data import CHART_DATA_ISSUES_KEY, normalize_chart_data
from app.services.content_flow import MERMAID_BLOCKS_KEY, count_mermaid
from app.services.kb import attach_vault_write, vault_status_map
from app.services.material import material_prompt_section
from app.services.llm import build_llm_client

logger = logging.getLogger(__name__)

TEXT_SYSTEM = """CONTENT_JSON
你是课程内容作者。为给定的知识点写详细讲解（Markdown），只输出 JSON：
{"markdown": "正文（含 ## 标题、列表、> 学习提示）", "summary": "一句话摘要", "keywords": ["关键词"]}"""


def generate_text_for_node(db: Session, node: OutlineNode, tone: str = "正式",
                           material: str = "") -> ContentItem:
    """知识点讲解正文（外层负责用量采集）。"""
    with usage_log.collect() as collector:
        item = _generate_text_inner(db, node, tone, material)
        if collector.depth == 1 and usage_log.attach(item, collector) is not None:
            db.commit()
        return item


def _generate_text_inner(db: Session, node: OutlineNode, tone: str,
                         material: str) -> ContentItem:
    client = build_llm_client()
    kind_cn = "章节" if node.node_type == "chapter" else "知识点"
    user = (
        f"当前{kind_cn}：{node.title}（编号 {node.node_key}）\n"
        f"目标：{node.objective}\n"
        f"写作边界：只写本{kind_cn}的内容；素材中其他章节只作背景参考，禁止展开成正文。\n"
        f"语气：{tone}{material}"
    )
    data = usage_log.complete_json(client, TEXT_SYSTEM, user,
                                   max_tokens=2500, purpose="content")
    body = {
        "markdown": data.get("markdown", f"## {node.title}"),
        "summary": data.get("summary", ""),
        "keywords": data.get("keywords", []),
        "tone": tone,
    }
    item = _latest_text(db, node)
    if item is None:
        item = ContentItem(
            course_id=node.outline.course_id,
            node_id=node.id,
            kind="text",
            title=node.title,
            body=body,
            meta={"node_key": node.node_key},
        )
        db.add(item)
        return item
    # 原地替换 + 保留历史
    history = list(item.meta.get("history") or [])
    history.insert(0, {"body": item.body})
    item.meta = {**item.meta, "history": history[:5]}
    item.body = body
    item.title = node.title
    return item


def _latest_text(db: Session, node: OutlineNode) -> ContentItem | None:
    return (
        db.query(ContentItem)
        .filter(ContentItem.course_id == node.outline.course_id,
                ContentItem.node_id == node.id,
                ContentItem.kind == "text")
        .order_by(ContentItem.created_at.desc())
        .first()
    )


def generate_all_texts(db: Session, course_id: str, node_ids: list[str], tone: str) -> int:
    """批量生成文本内容（含课程素材注入）；返回生成条数。"""
    nodes = _select_nodes(db, course_id, node_ids)
    course = db.get(Course, course_id)
    material = material_prompt_section(db, course) if course else ""
    count = 0
    for node in nodes:
        item = generate_text_for_node(db, node, tone=tone, material=material)
        count += 1
        db.flush()
        _track_job(db, course_id, "content", node.id, item.id)
        try:
            attach_vault_write(db, item, "text")
        except Exception:  # noqa: BLE001 —— Vault 增量失败不影响主流程
            logger.warning("vault incremental write skipped for content %s", item.id)
    db.commit()
    return count


def _select_nodes(db: Session, course_id: str, node_ids: list[str]) -> list[OutlineNode]:
    if node_ids:
        return list(db.query(OutlineNode).filter(OutlineNode.id.in_(node_ids)).all())
    outline = (
        db.query(Outline)
        .filter(Outline.course_id == course_id)
        .order_by(Outline.created_at.desc())
        .first()
    )
    if outline is None:
        return []
    return list(
        db.query(OutlineNode)
        .filter(OutlineNode.outline_id == outline.id)
        .order_by(OutlineNode.order_index)
        .all()
    )


def _track_job(db: Session, course_id: str, job_type: str, node_id: str, result_id: str) -> None:
    db.add(
        GenerationJob(
            course_id=course_id,
            job_type=job_type,
            status="success",
            payload={"node_id": node_id},
            result={"content_id": result_id},
        )
    )


def extract_words(markdown: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff\w]+", markdown or ""))


def build_content_rows(db: Session, course_id: str, *, include_vault: bool = True) -> list[dict]:
    """课程内容行（与前端 ContentRow 同构）：内容条目 + 习题。

    include_vault=False 用于公开只读分享，会额外剥掉内部信息：
    - `meta.vault`（Obsidian 导出状态，里面有本机绝对路径 vault_path / note_path）；
    - `kind=ppt` 的课件条目（body 里是 pptx 绝对路径），公开侧改用 /public/{token}/ppt。
    """
    node_key_by_id = {
        n.id: n.node_key
        for n in db.query(OutlineNode)
        .join(Outline, OutlineNode.outline_id == Outline.id)
        .filter(Outline.course_id == course_id)
        .all()
    }
    items = (db.query(ContentItem)
             .filter(ContentItem.course_id == course_id)
             .order_by(ContentItem.created_at).all())
    if not include_vault:
        items = [i for i in items if i.kind != "ppt"]
    rows = []
    for item in items:
        meta = dict(item.meta or {})
        if not include_vault:
            meta.pop("vault", None)
        body = item.body
        if str(item.kind).startswith("chart_"):
            # 老图表（库里 3/14 张 labels 与 data 长度不符、14/14 张没有颜色）在**读取时**
            # 归一化并补主题色：不迁移数据，前端/导出/PPT 拿到的都是同一份干净数据。
            chart_type = str((body or {}).get("chart_type") or item.kind[len("chart_"):])
            normalized, issues = normalize_chart_data(chart_type, (body or {}).get("data"))
            body = {**(body or {}), "data": normalized}
            if issues or CHART_DATA_ISSUES_KEY not in meta:
                meta[CHART_DATA_ISSUES_KEY] = issues
        if str(item.kind) == "lecture":
            # 「流程图」= 讲义正文里的 ```mermaid 段数（库里 kind='flowchart' 是 0 条，
            # 按 kind 计数会让界面永远显示 0 并与顶部统计打架）。前端读 meta 或用同一口径解析。
            meta[MERMAID_BLOCKS_KEY] = count_mermaid((body or {}).get("markdown"))
        rows.append(
            {"id": item.id, "node_id": item.node_id, "node_key": node_key_by_id.get(item.node_id, ""),
             "kind": item.kind, "title": item.title, "body": body, "meta": meta}
        )
    incr: dict = {}
    if include_vault:
        incr = vault_status_map(db, course_id)
        for row in rows:
            status = incr.get(f"item:{row['id']}")
            if status is not None:
                row["meta"] = {**dict(row["meta"]), "vault": status}
    quizzes = (db.query(QuizItem)
               .filter(QuizItem.course_id == course_id)
               .order_by(QuizItem.created_at).all())
    for q in quizzes:
        row = {
            "id": q.id,
            "node_id": q.node_id,
            "node_key": node_key_by_id.get(q.node_id, ""),
            "kind": "quiz",
            "title": q.question[:50],
            "body": {"qtype": q.qtype, "difficulty": q.difficulty, "question": q.question,
                     "options": q.options, "answer": q.answer, "explanation": q.explanation},
            "meta": {},
        }
        if include_vault and incr.get(f"quiz:{q.node_id}") is not None:
            row["meta"] = {"vault": incr.get(f"quiz:{q.node_id}")}
        rows.append(row)
    return rows
