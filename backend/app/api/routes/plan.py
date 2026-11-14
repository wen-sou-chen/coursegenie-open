"""智能规划路由：生成大纲 + 读取大纲（含知识图谱边数据）。"""
from __future__ import annotations

import logging
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.db.session import get_db
from app.models.course import STATUS_APPROVED, STATUS_PLANNED, Course, Outline, OutlineNode
from app.schemas.ops import PlanNodeUpdate
from app.schemas.plan import (PlanApproveRequest, PlanNodeCreate, PlanNodeMove,
                              PlanRequest, PlanRestoreRequest)
from app.services import usage as usage_log
from app.services.planner import (
    NodeConstraintError,
    add_node as create_plan_node,
    delete_node,
    find_plan_version,
    generate_outline,
    get_outline,
    list_nodes,
    move_node,
    node_impact,
    outline_check,
    outline_snapshot,
    plan_diff,
    plan_history,
    plan_params,
    record_outline_check,
    restore_impact,
    restore_outline,
    stored_outline_check,
    update_node,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/plan", tags=["plan"], dependencies=[Depends(require_api_key)])


@router.post("/generate")
def generate(payload: PlanRequest, db: Session = Depends(get_db)):
    course = db.get(Course, payload.course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    if course.status == STATUS_APPROVED:
        raise HTTPException(409, "大纲已通过（已锁定为生成依据），如需重新生成请先点「撤销通过」")
    outline = generate_outline(db, course, payload)
    nodes = list_nodes(db, payload.course_id)
    stored = stored_outline_check(course) or {}
    return {
        "outline_id": outline.id,
        "title": outline.title,
        "goal": outline.goal,
        "nodes": [_node_out(n) for n in nodes],
        "graph": _graph(nodes),
        # 本次大纲生成的 LLM 用量（口径与讲义链路的 job.result.usage 一致）；
        # 原先这段调用没有采集器、被静默丢弃（P3-4）。
        "usage": usage_log.aux_usage(course, "outline"),
        # 生成后自检（P3-2 / 任务 70 §B1）：读的是**刚落库的那一份**（不再临时现算）——
        # 因此这里返回的结论与刷新后的 GET /plan/{id}、GET /plan/{id}/check 完全一致。
        **_stored_check_out(stored),
    }


@router.post("/approve")
def approve(payload: PlanApproveRequest, db: Session = Depends(get_db)):
    """大纲「通过 / 撤销通过」：通过后大纲锁定，作为图表与章节内容的唯一生成依据。"""
    course = db.get(Course, payload.course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    if payload.approved and not list_nodes(db, course.id):
        raise HTTPException(400, "还没有大纲，请先「生成大纲」再通过")
    course.status = STATUS_APPROVED if payload.approved else STATUS_PLANNED
    db.commit()
    return {"ok": True, "status": course.status}


@router.get("/{course_id}/history")
def read_plan_history(course_id: str, db: Session = Depends(get_db)):
    """大纲历史版本（P3-1）：每版的生成参数、节点数与用量，最新在前。"""
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    current = outline_snapshot(db, course)
    versions = [{
        "id": row.get("id"),
        "at": row.get("at"),
        "reason": row.get("reason"),
        "title": row.get("title"),
        "goal": row.get("goal"),
        "params": row.get("params") or {},
        "usage": row.get("usage"),
        "nodes": len(row.get("nodes") or []),
    } for row in plan_history(course)]
    return {
        "course_id": course.id,
        "current": {
            "nodes": len((current or {}).get("nodes") or []),
            "params": plan_params(course),
            "status": course.status,
        },
        "versions": versions,
    }


@router.get("/{course_id}/history/{version_id}")
def read_plan_version(course_id: str, version_id: str, db: Session = Depends(get_db)):
    """某个历史版本的详情 + 与当前大纲的节点级 diff。"""
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    version = find_plan_version(course, version_id)
    if version is None:
        raise HTTPException(404, "历史版本不存在")
    current = outline_snapshot(db, course) or {"nodes": []}
    old_nodes = list(version.get("nodes") or [])
    return {
        "course_id": course.id,
        "id": version.get("id"),
        "at": version.get("at"),
        "reason": version.get("reason"),
        "title": version.get("title"),
        "goal": version.get("goal"),
        "params": version.get("params") or {},
        "usage": version.get("usage"),
        "nodes": old_nodes,
        "diff": plan_diff(old_nodes, current["nodes"]),
        "impact": restore_impact(db, course),
    }


@router.post("/{course_id}/history/{version_id}/restore")
def restore_plan_version(course_id: str, version_id: str,
                         payload: PlanRestoreRequest | None = None,
                         db: Session = Depends(get_db)):
    """回滚到某个历史版本（P3-1）。

    未确认时返回 409 + 影响面（会删掉现版大纲上绑定的讲义/习题）；
    当前版本会先存进历史，回滚本身也可再回滚。
    """
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    if find_plan_version(course, version_id) is None:
        raise HTTPException(404, "历史版本不存在")
    impact = restore_impact(db, course)
    if not (payload and payload.confirm):
        raise HTTPException(409, detail={"message": "回滚会删掉现版大纲上绑定的内容，请确认",
                                        "impact": impact})
    try:
        return restore_outline(db, course, version_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/{course_id}/check")
def get_plan_check(course_id: str, db: Session = Depends(get_db)):
    """大纲自检的**落库结论**（任务 70 §B2）：刷新 / 换设备都在；没生成过给明确空态。

    单独开这一条，是为了「还没有大纲」时也能拿到空态（那时 GET /plan/{id} 是 404）。
    """
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    return {
        "course_id": course.id,
        "has_outline": bool(list_nodes(db, course_id)),
        **_stored_check_out(stored_outline_check(course) or {}),
    }


@router.post("/{course_id}/check")
def run_plan_check(course_id: str, db: Session = Depends(get_db)):
    """对**当前已有的大纲**补跑一次生成自检并落库（任务 81 §B3 的「立即自检」入口）。

    与 ``POST /plan/generate`` 的区别：**不重新生成大纲** —— 不调模型、不产生用量、不动任何
    节点，只是按当前素材把「章节数是否兑现 + 素材要点覆盖」重算一遍，写进生成时用的同一个键
    （``course.meta.outline_check``，见 ``planner.record_outline_check``）。原先只有 GET，
    界面上没法给「大纲已通过、但还没跑过自检」的课补一次结论。
    """
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    nodes = list_nodes(db, course_id)
    if not nodes:
        raise HTTPException(400, "还没有大纲：请先「生成大纲」（生成时会自动跑一次自检）")
    # ``outline_check()`` 只从 request 里读 ``num_chapters``（当初请求了几章）；生成参数没落库的
    # 老课按现大纲的章数对照，避免出现「请求 0 章、实际 39 章」这种假结论。
    params = plan_params(course)
    chapters = len([n for n in nodes if "." not in str(n.node_key or "")])
    request = SimpleNamespace(num_chapters=int(params.get("num_chapters") or 0) or chapters)
    record_outline_check(db, course, outline_check(db, course, request, nodes, semantic=False))
    db.commit()
    # 任务 83 §B4：与 material.py 的「补齐素材信息」同类显式写入口 —— 写一行 INFO（内容取自刚落库的结论）。
    checked = (stored_outline_check(course) or {}).get("check") or {}
    logger.info("大纲自检补跑入口被调用：course=%s 章数 %d/%d、素材要点 %d 覆盖 %d（不调模型、不产生用量）",
                course_id, checked.get("actual_chapters") or 0, checked.get("requested_chapters") or 0,
                checked.get("material_points_total") or 0, checked.get("material_covered_count") or 0)
    return {
        "course_id": course.id,
        "has_outline": True,
        **_stored_check_out(stored_outline_check(course) or {}),
    }


@router.get("/{course_id}")
def get_plan(course_id: str, db: Session = Depends(get_db)):
    outline = get_outline(db, course_id)
    if outline is None:
        raise HTTPException(404, "尚无大纲，请先 POST /plan/generate")
    nodes = list_nodes(db, course_id)
    course = db.get(Course, course_id)
    stored = stored_outline_check(course) if course is not None else {}
    return {
        "outline_id": outline.id,
        "title": outline.title,
        "goal": outline.goal,
        "nodes": [_node_out(n) for n in nodes],
        "graph": _graph(nodes),
        # 任务 70 §B2：把落库的自检结论一起返回 ⇒ 刷新 / 换设备后界面上仍然常显。
        **_stored_check_out(stored),
    }


def _stored_check_out(stored: dict) -> dict:
    """把落库的自检记录摊平成接口字段（POST /generate、GET /plan/{id}、GET /plan/{id}/check 共用）。"""
    stored = stored or {}
    return {
        "check": stored.get("check"),
        "check_generated_at": stored.get("generated_at") or "",
        "check_at": stored.get("at") or "",
        "check_semantic_enabled": bool(stored.get("semantic_enabled")),
        "check_materials_fingerprint": stored.get("materials_fingerprint") or [],
        "check_materials_fingerprint_hash": stored.get("materials_fingerprint_hash") or "",
    }


def _node_out(node: OutlineNode) -> dict:
    """节点输出：``key``/``type`` 与库字段 ``node_key``/``node_type`` **同时给出**（任务 51）。

    集成方不必再猜「接口叫 key、库里叫 node_key」——两套名字都在，值完全一致。
    """
    return {
        "id": node.id,
        "key": node.node_key,
        "type": node.node_type,
        "node_key": node.node_key,
        "node_type": node.node_type,
        "title": node.title,
        "objective": node.objective,
        "teaching_strategy": node.teaching_strategy,
        "assessment": node.assessment,
        "prerequisites": node.prerequisites,
    }


def _graph(nodes: list[OutlineNode]) -> dict:
    """知识图谱：nodes + edges（prerequisites 依赖边，供 React Flow 渲染）。"""
    key_to_id = {n.node_key: n.id for n in nodes}
    edges = []
    for n in nodes:
        for pre in n.prerequisites or []:
            target_id = key_to_id.get(pre)
            if target_id:
                edges.append({"source": target_id, "target": n.id, "label": "前置"})
    return {
        "nodes": [
            {"id": n.id, "key": n.node_key, "node_key": n.node_key, "label": n.title,
             "type": n.node_type, "node_type": n.node_type} for n in nodes
        ],
        "edges": edges,
    }

@router.patch("/node/{node_id}")
def edit_node(node_id: str, payload: PlanNodeUpdate, db: Session = Depends(get_db)):
    """教师手动修订大纲节点（标题/目标/策略/评估/前置依赖）。"""
    try:
        node = update_node(db, node_id, payload.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return _node_out(node)


@router.post("/node")
def create_node(payload: PlanNodeCreate, db: Session = Depends(get_db)):
    """新增大纲节点（P3-3）：顶层加一章，或指定父章在指定位置加一条知识点。

    新增后整份大纲的 node_key 自动重排成连续（1 / 1.1 / 1.2 / 2 …），已生成内容与
    笔记的绑定点（ContentItem.node_id）不动，只同步 meta 里的 key 缓存。
    """
    course = db.get(Course, payload.course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    try:
        result = create_plan_node(
            db, course, title=payload.title, parent_id=payload.parent_id,
            position=payload.position, objective=payload.objective,
            teaching_strategy=payload.teaching_strategy,
            assessment=payload.assessment, node_type=payload.node_type)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    nodes = list_nodes(db, course.id)
    return {"node": _node_out(result["node"]), "renumbered": result["renumbered"],
            "nodes": [_node_out(n) for n in nodes], "graph": _graph(nodes),
            "status": course.status,
            "notice": "编号已自动重排；如已生成讲义，导出时会按新编号重写笔记文件名。"}


@router.get("/node/{node_id}/impact")
def node_impact_view(node_id: str, db: Session = Depends(get_db)):
    """节点影响面：绑定内容/习题/锁定讲义 + 笔记库里的文件 + 编号变化（删除/移动前看）。"""
    try:
        course = _course_of_node(db, node_id)
        return node_impact(db, course, node_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.post("/node/{node_id}/move")
def move_plan_node(node_id: str, payload: PlanNodeMove, db: Session = Depends(get_db)):
    """移动大纲节点（P3-3）：同章上/下移，或跨章移动 / 升降层级（reparent）。

    会改动编号时要求 confirm=true（二次确认）：不确认时返回 409 + 影响面，
    由界面把「会动哪些讲义/图/习题/笔记文件」写清楚再提交。
    """
    try:
        course = _course_of_node(db, node_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    kwargs = {"direction": payload.direction, "reparent": payload.reparent,
              "to_parent_id": payload.to_parent_id, "position": payload.position}
    try:
        preview = move_node(db, course, node_id, preview=True, **kwargs)
    except NodeConstraintError as exc:
        raise HTTPException(409, detail={"message": str(exc), "impact": exc.impact})
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    impact = preview["impact"]
    if not payload.confirm and (impact["key_change_count"] or impact["content_total"]
                                or impact["notes"]["count"]):
        raise HTTPException(409, detail={
            "message": "移动会重排节点编号并影响已生成内容与笔记文件名，请确认后重试",
            "impact": impact})
    try:
        result = move_node(db, course, node_id, **kwargs)
    except NodeConstraintError as exc:
        raise HTTPException(409, detail={"message": str(exc), "impact": exc.impact})
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    nodes = list_nodes(db, course.id)
    return {"node": _node_out(result["node"]), "renumbered": result["renumbered"],
            "impact": result["impact"], "nodes": [_node_out(n) for n in nodes],
            "graph": _graph(nodes), "status": course.status,
            "notice": "编号已重排；导出时会按新编号重写笔记文件名。"}


@router.delete("/node/{node_id}")
def delete_plan_node(node_id: str, cascade: bool = False, db: Session = Depends(get_db)):
    """删除大纲节点（P3-3，含其下知识点）：默认不删绑定的内容/习题，需 cascade=true。

    未确认时返回 409 + 影响面（会删哪些内容条目、哪些笔记文件将失效）。
    """
    try:
        course = _course_of_node(db, node_id)
        result = delete_node(db, course, node_id, cascade=cascade)
    except NodeConstraintError as exc:
        raise HTTPException(409, detail={"message": str(exc), "impact": exc.impact})
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    nodes = list_nodes(db, course.id)
    return {"deleted": result["deleted"], "removed_content": result["removed_content"],
            "removed_quizzes": result["removed_quizzes"],
            "renumbered": result["renumbered"], "impact": result["impact"],
            "nodes": [_node_out(n) for n in nodes], "graph": _graph(nodes),
            "status": course.status,
            "notice": "下次导出笔记时会清理/改名这些文件（图库与讲义按新编号重写）。"}


def _course_of_node(db: Session, node_id: str) -> Course:
    """按节点反查课程（三个新接口共用；节点不存在时抛 ValueError）。"""
    node = db.get(OutlineNode, node_id)
    if node is None:
        raise ValueError("大纲节点不存在")
    outline = db.get(Outline, node.outline_id)
    course = db.get(Course, outline.course_id) if outline is not None else None
    if course is None:
        raise ValueError("大纲节点不存在")
    return course
