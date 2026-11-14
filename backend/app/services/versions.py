"""版本控制：快照当前课程状态、列出历史版本、回滚。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.course import ContentItem, Course, CourseVersion, Outline, OutlineNode, QuizItem
from app.services.storage_files import collect_image_refs_in, item_file_refs, remove_files


def snapshot(db: Session, course_id: str, note: str = "") -> CourseVersion:
    course = db.get(Course, course_id)
    if course is None:
        raise ValueError(f"课程不存在：{course_id}")
    latest = (
        db.query(CourseVersion)
        .filter(CourseVersion.course_id == course_id)
        .order_by(CourseVersion.version_no.desc())
        .first()
    )
    version_no = (latest.version_no + 1) if latest else 1
    outline = _current_outline(db, course_id)
    snap = {
        "course": {
            "title": course.title,
            "subject": course.subject,
            "audience": course.audience,
            "duration_minutes": course.duration_minutes,
            "status": course.status,
            "meta": course.meta,
        },
        "outline": _outline_snapshot(db, course_id),
        # 任务 48y：goal 与「原始 LLM 大纲输出」都要进快照 —— 否则回滚时无值可写，
        # 只能写空（旧行为）⇒ 大纲/配图/课件封面的课程目标一起变空。
        "outline_goal": outline.goal if outline else "",
        "outline_content": dict(outline.content or {}) if outline else {},
        "content": _content_snapshot(db, course_id),
        "quizzes": _quiz_snapshot(db, course_id),
    }
    row = CourseVersion(course_id=course_id, version_no=version_no, snapshot=snap, note=note)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _course_nodes(db: Session, course_id: str) -> dict[str, OutlineNode]:
    """当前课程的大纲节点 id → 节点（供内容/习题快照记录关联）。"""
    rows = (
        db.query(OutlineNode)
        .join(Outline, OutlineNode.outline_id == Outline.id)
        .filter(Outline.course_id == course_id)
        .all()
    )
    return {n.id: n for n in rows}


def _current_outline(db: Session, course_id: str) -> Outline | None:
    """课程当前（最新创建）的大纲：快照与回滚都靠它取 goal / 原始 LLM 输出。"""
    return (
        db.query(Outline)
        .filter(Outline.course_id == course_id)
        .order_by(Outline.created_at.desc())
        .first()
    )


def _outline_snapshot(db: Session, course_id: str) -> list[dict]:
    outline = _current_outline(db, course_id)
    if outline is None:
        return []
    nodes = (
        db.query(OutlineNode)
        .filter(OutlineNode.outline_id == outline.id)
        .order_by(OutlineNode.order_index)
        .all()
    )
    return [
        {
            "id": n.id,
            "key": n.node_key,
            "type": n.node_type,
            "title": n.title,
            "objective": n.objective,
            "teaching_strategy": n.teaching_strategy,
            "assessment": n.assessment,
            "prerequisites": n.prerequisites,
        }
        for n in nodes
    ]


def _content_snapshot(db: Session, course_id: str) -> list[dict]:
    by_id = _course_nodes(db, course_id)
    return [
        {
            "kind": c.kind,
            "title": c.title,
            "body": c.body,
            "meta": c.meta,
            "node_id": c.node_id,
            "node_key": by_id[c.node_id].node_key if c.node_id and c.node_id in by_id else None,
        }
        for c in db.query(ContentItem).filter(ContentItem.course_id == course_id).all()
    ]


def _quiz_snapshot(db: Session, course_id: str) -> list[dict]:
    by_id = _course_nodes(db, course_id)
    return [
        {
            "qtype": q.qtype,
            "difficulty": q.difficulty,
            "question": q.question,
            "options": q.options,
            "answer": q.answer,
            "explanation": q.explanation,
            "node_id": q.node_id,
            "node_key": by_id[q.node_id].node_key if q.node_id and q.node_id in by_id else None,
        }
        for q in db.query(QuizItem).filter(QuizItem.course_id == course_id).all()
    ]


def _snapshot_goal(snap: dict, fallback: str) -> str:
    """快照里的课程目标；**旧快照**没有该键（或值不是字符串）时保持现值。

    这是任务 48y 的兼容护栏：历史上回滚会把课程目标清空，旧快照本来就没记 goal，
    所以只能「保持现值」，绝不允许再写空。
    """
    goal = snap.get("outline_goal")
    return goal if isinstance(goal, str) else fallback


def _snapshot_content(snap: dict, fallback: dict) -> dict:
    """快照里记录的「原始 LLM 大纲输出」（含 goal）；旧快照没有该键时保持现值。

    历史 bug：这里曾把**整个版本快照**写进 ``content`` —— 既让 ``content["goal"]``
    取不到（⇒ 课件封面课程目标变空），也使 ``content`` 不再是原始大纲输出。
    """
    content = snap.get("outline_content")
    return dict(content) if isinstance(content, dict) else dict(fallback)


def list_versions(db: Session, course_id: str) -> list[CourseVersion]:
    return (
        db.query(CourseVersion)
        .filter(CourseVersion.course_id == course_id)
        .order_by(CourseVersion.version_no.desc())
        .all()
    )


def restore(db: Session, course_id: str, version_id: str) -> CourseVersion:
    """回滚：以版本快照重建课程数据（幂等：先清空再写入）。

    P1-2 修复：快照记录大纲节点 id 与内容/习题的 node 关联；回滚按原 id 重建
    OutlineNode 并回填 node_id，避免内容变课程级孤儿、再生成时重复。旧快照
    （无 id/关联字段）回退为“不绑定”的历史行为。

    任务 48y 修复：快照记下大纲 ``goal`` 与原始 LLM 输出（``outline_goal`` /
    ``outline_content``），回滚时写回；**旧快照**缺这两个键时保持现值（不再清空课程目标）；
    ``content`` 不再被整个版本快照覆盖。
    """
    version = db.get(CourseVersion, version_id)
    if version is None or version.course_id != course_id:
        raise ValueError("版本不存在")
    snap = version.snapshot

    course = db.get(Course, course_id)
    if course is None:
        raise ValueError(f"课程不存在：{course_id}")

    # 任务 48y：清空大纲前先取「现值」兜底 —— 旧快照没有 outline_goal /
    # outline_content 键时，回滚必须保持现值，绝不能把课程目标清空。
    current = _current_outline(db, course_id)
    current_goal = current.goal if current is not None else ""
    current_content = dict(current.content or {}) if current is not None else {}

    # 清空现有内容；文件在提交后统一清理，且只删「不在本快照引用内」的孤儿文件，
    # 避免回滚后新行仍引用同一批 storage 文件时被误删。
    old_rows = (
        db.query(ContentItem)
        .filter(ContentItem.course_id == course_id)
        .all()
    )
    old_files: list = []
    for row in old_rows:
        old_files.extend(item_file_refs(row))
    db.query(ContentItem).filter(ContentItem.course_id == course_id).delete(
        synchronize_session=False)
    db.query(QuizItem).filter(QuizItem.course_id == course_id).delete()
    for outline in db.query(Outline).filter(Outline.course_id == course_id).all():
        db.query(OutlineNode).filter(OutlineNode.outline_id == outline.id).delete()
        db.delete(outline)

    c = snap.get("course", {})
    for field in ("title", "subject", "audience", "duration_minutes", "status", "meta"):
        if field in c:
            setattr(course, field, c[field])

    # 快照含大纲节点 id 时按原 id 重建，保证内容/习题的 node 关联稳定。
    restored_node_ids: set[str] = set()
    node_key_to_id: dict[str, str] = {}
    if snap.get("outline"):
        goal = _snapshot_goal(snap, current_goal)
        content = _snapshot_content(snap, current_content)
        if goal:
            # ppt.py:792 的课件封面文案读的是 content["goal"] ⇒ 两者必须一致。
            content["goal"] = goal
        outline = Outline(
            course_id=course_id,
            title=c.get("title", course.title),
            goal=goal,
            content=content,
        )
        db.add(outline)
        db.flush()
        for i, node in enumerate(snap["outline"]):
            row = OutlineNode(
                outline_id=outline.id,
                node_key=node.get("key", f"{i}"),
                node_type=node.get("type", "section"),
                title=node.get("title", ""),
                objective=node.get("objective", ""),
                teaching_strategy=node.get("teaching_strategy", ""),
                assessment=node.get("assessment", ""),
                prerequisites=node.get("prerequisites") or [],
                order_index=i,
            )
            snap_id = node.get("id")
            if isinstance(snap_id, str) and snap_id and len(snap_id) <= 32:
                row.id = snap_id
            db.add(row)
            db.flush()
            restored_node_ids.add(row.id)
            node_key_to_id.setdefault(row.node_key, row.id)

    def _restore_node_id(raw_id: object, raw_key: object) -> str | None:
        """优先按快照中的原 id 恢复关联；旧快照无 id 时按 node_key 匹配。"""
        if isinstance(raw_id, str) and raw_id in restored_node_ids:
            return raw_id
        if isinstance(raw_key, str) and raw_key in node_key_to_id:
            return node_key_to_id[raw_key]
        return None

    for item in snap.get("content", []):
        db.add(ContentItem(course_id=course_id, kind=item.get("kind", "text"),
                           title=item.get("title", ""), body=item.get("body", {}),
                           meta=item.get("meta", {}),
                           node_id=_restore_node_id(item.get("node_id"), item.get("node_key"))))
    for q in snap.get("quizzes", []):
        db.add(QuizItem(course_id=course_id, qtype=q.get("qtype", "choice"),
                        difficulty=q.get("difficulty", "medium"), question=q.get("question", ""),
                        options=q.get("options", []), answer=q.get("answer", ""),
                        explanation=q.get("explanation", ""),
                        node_id=_restore_node_id(q.get("node_id"), q.get("node_key"))))

    db.commit()
    kept_files = collect_image_refs_in(snap)
    remove_files([p for p in old_files if p not in kept_files])
    return version
