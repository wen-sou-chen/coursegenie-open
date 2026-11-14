"""教学图表生成/查询/编辑/删除路由。"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.db.session import get_db
from app.models.course import STATUS_APPROVED, ContentItem, Course, Outline, OutlineNode
from app.schemas.chart import ChartGenerateRequest, ChartUpdateRequest
from app.services.chart import CHART_TYPES, generate_chart
from app.services.chart_data import CHART_DATA_ISSUES_KEY, normalize_chart_data

router = APIRouter(prefix="/chart", tags=["chart"], dependencies=[Depends(require_api_key)])


def _chart_out(item: ContentItem, node_key: str = "") -> dict:
    return {
        "id": item.id,
        "node_id": item.node_id,
        "node_key": node_key,
        "kind": item.kind,
        "title": item.title,
        "body": item.body,
        "meta": item.meta,
    }


@router.post("/generate")
def generate(payload: ChartGenerateRequest, db: Session = Depends(get_db)):
    course = db.get(Course, payload.course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    if course.status != STATUS_APPROVED:
        raise HTTPException(409, "请先在大纲步骤点「大纲通过」，锁定大纲后再生成图表")
    node = None
    if payload.node_id:
        node = db.get(OutlineNode, payload.node_id)
        if node is None or node.outline is None or node.outline.course_id != course.id:
            raise HTTPException(400, "所选节点不属于当前课程，请重新选择")
    try:
        item = generate_chart(db, course, node, payload.chart_type, payload.instruction,
                              item_id=payload.item_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"图表生成失败（LLM 服务暂不可用或限流）：{exc}") from exc
    return _chart_out(item)


@router.get("/{course_id}")
def list_charts(course_id: str, db: Session = Depends(get_db)):
    if db.get(Course, course_id) is None:
        raise HTTPException(404, "课程不存在")
    key_by_id = {
        n.id: n.node_key
        for n in db.query(OutlineNode)
        .join(Outline, OutlineNode.outline_id == Outline.id)
        .filter(Outline.course_id == course_id)
        .all()
    }
    kinds = [f"chart_{t}" for t in CHART_TYPES]
    items = (
        db.query(ContentItem)
        .filter(ContentItem.course_id == course_id, ContentItem.kind.in_(kinds))
        .order_by(ContentItem.created_at)
        .all()
    )
    return [_chart_out(i, key_by_id.get(i.node_id, "")) for i in items]


@router.patch("/{chart_id}")
def update(chart_id: str, payload: ChartUpdateRequest, db: Session = Depends(get_db)):
    item = db.get(ContentItem, chart_id)
    if item is None or not item.kind.startswith("chart_"):
        raise HTTPException(404, "图表不存在")
    if payload.title is not None:
        item.title = payload.title
    if payload.data is not None:
        # 手工编辑同样过归一化 + 主题上色（与生成、渲染、导出、PPT 共用同一份口径）
        chart_type = str((item.body or {}).get("chart_type") or item.kind[len("chart_"):])
        data, issues = normalize_chart_data(chart_type, payload.data)
        item.body = {**(item.body or {}), "data": data}
        item.meta = {**(item.meta or {}), CHART_DATA_ISSUES_KEY: issues}
    db.commit()
    db.refresh(item)
    return _chart_out(item)


@router.delete("/{chart_id}")
def delete(chart_id: str, db: Session = Depends(get_db)):
    item = db.get(ContentItem, chart_id)
    if item is None or not item.kind.startswith("chart_"):
        raise HTTPException(404, "图表不存在")
    db.delete(item)
    db.commit()
    return {"ok": True, "id": chart_id}
