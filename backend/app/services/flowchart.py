"""流程图生成：自然语言 → Mermaid 源码（前端渲染，后端可选导出）。

- item_id 表示「原地重做」：替换该流程图，旧版本进 meta.history。
- import_flowchart 接收教师自绘/粘贴的 Mermaid 源码（source=import）。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.course import ContentItem, Course, OutlineNode
from app.services import usage as usage_log
from app.services.kb import attach_vault_write
from app.services.llm import build_llm_client

MERMAID_SYSTEM = """MERMAID
你是流程建模专家。根据用户描述输出 Mermaid 源码（只输出 flowchart TD 代码，不要代码块标记）。
保留关键步骤与分支；使用中文节点文本。"""


def _clean_fences(mermaid: str) -> str:
    mermaid = mermaid.strip()
    if mermaid.startswith("```"):
        mermaid = mermaid.strip("`")
        if mermaid.startswith("mermaid"):
            mermaid = mermaid[len("mermaid"):].strip()
    return mermaid


def generate_flowchart(db: Session, course: Course, description: str, kind: str,
                       item_id: str | None = None, node_id: str | None = None) -> ContentItem:
    client = build_llm_client()
    mermaid = _clean_fences(usage_log.complete(
        client, MERMAID_SYSTEM, description, max_tokens=1500, purpose="flowchart"))
    body = {"mermaid": mermaid, "kind": kind, "description": description, "source": "generate"}
    node = _resolve_node(db, course, node_id)
    item = _upsert(db, course, body, description, item_id, node)
    try:
        attach_vault_write(db, item, "flowchart", {"mermaid_code": mermaid})
    except Exception:  # noqa: BLE001 —— Vault 增量失败不影响主流程
        pass
    return item


def import_flowchart(db: Session, course: Course, title: str, mermaid: str,
                     item_id: str | None = None, node_id: str | None = None) -> ContentItem:
    """教师自绘/粘贴的 Mermaid 流程图直接入库。"""
    body = {"mermaid": _clean_fences(mermaid), "kind": "import", "description": title,
            "source": "import"}
    node = _resolve_node(db, course, node_id)
    item = _upsert(db, course, body, title or "导入流程图", item_id, node)
    try:
        attach_vault_write(db, item, "flowchart", {"mermaid_code": mermaid})
    except Exception:  # noqa: BLE001
        pass
    return item


def _upsert(db: Session, course: Course, body: dict, title: str,
            item_id: str | None, node=None) -> ContentItem:
    item = _target_item(db, course, item_id)
    if item is None:
        item = ContentItem(course_id=course.id, kind="flowchart", title=title[:50], body=body,
                           node_id=node.id if node else None)
        db.add(item)
    else:
        history = list(item.meta.get("history") or [])
        history.insert(0, {"body": item.body})
        item.meta = {**item.meta, "history": history[:5]}
        if node is not None:
            item.node_id = node.id
        item.title = title[:50]
        item.body = body
    db.commit()
    db.refresh(item)
    return item


def _resolve_node(db: Session, course: Course, node_id: str | None) -> OutlineNode | None:
    """校验并返回要绑定的节点；为空表示整课级素材（不绑定）。"""
    if not node_id:
        return None
    node = db.get(OutlineNode, node_id)
    if node is None or node.outline is None or node.outline.course_id != course.id:
        raise ValueError("所选节点不属于当前课程，请重新选择")
    return node


def _target_item(db: Session, course: Course, item_id: str | None) -> ContentItem | None:
    if not item_id:
        return None
    item = db.get(ContentItem, item_id)
    if item is None or item.course_id != course.id or item.kind != "flowchart":
        raise ValueError(f"内容项不存在或类型不符：{item_id}")
    return item
