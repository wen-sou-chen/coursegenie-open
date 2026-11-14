"""概念库路由：LLM 抽取原子概念（异步 job，失败显式上报）；网页端可编辑。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.db.session import SessionLocal, get_db
from app.models.course import STATUS_APPROVED, ContentItem, Course, GenerationJob
from app.schemas.concept import (ConceptGenerateRequest, ConceptSupplementRequest,
                                  ConceptUpdateRequest)
from app.services import concept as concept_service
from app.services import concept_graph
from app.services import lecture_qc
from app.services.jobs import acquire_scope, claim_job_exclusive, has_active_job, release_scope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/concept", tags=["concept"], dependencies=[Depends(require_api_key)])


def _recheck_lectures(db: Session, course: Course, report) -> dict:
    """概念抽取成功后按当前口径复检该课讲义（任务 48k，0 LLM）。

    复检只是「顺手做」：失败也**不阻断**概念任务，把失败篇数写进 job.result 的一句话里。
    """
    out = dict(report) if isinstance(report, dict) else {"report": report}
    try:
        counts = lecture_qc.refresh_course(db, course)
    except Exception as exc:  # noqa: BLE001 —— 复检失败不影响概念抽取结果
        logger.warning("概念抽取后的讲义复检失败：%s", exc)
        out["qc_recheck"] = {"error": str(exc)}
        out["qc_recheck_note"] = f"讲义质检复检失败（{exc}），不影响本次概念抽取"
        return out
    out["qc_recheck"] = counts
    if counts["failed"]:
        out["qc_recheck_note"] = (
            f"讲义质检复检：{counts['refreshed']}/{counts['lectures']} 篇已按当前口径重算，"
            f"{counts['failed']} 篇失败（可在讲义卡片点「开始质检」重试）")
    else:
        out["qc_recheck_note"] = f"讲义质检复检：{counts['refreshed']} 篇已按当前口径重算"
    return out


@router.post("/course/{course_id}/generate")
def generate(
    course_id: str,
    background: BackgroundTasks,
    payload: ConceptGenerateRequest | None = None,
    db: Session = Depends(get_db),
):
    """整课抽取概念（LLM 生成定义/要点/关系）；同名合并、跨章只追加 chapters。"""
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    if course.status != STATUS_APPROVED:
        raise HTTPException(409, "课程大纲尚未通过，无法抽取概念")
    if has_active_job(db, course.id, "concept"):
        raise HTTPException(409, "该课程已有进行中的概念抽取任务，请等待完成后再试")
    if not acquire_scope(course.id, "concept"):
        raise HTTPException(409, "该课程概念抽取任务正在执行中，请稍后再试")

    def _run() -> None:
        task_db = SessionLocal()
        try:
            job_row = task_db.get(GenerationJob, job_id)
            if not claim_job_exclusive(task_db, job_row):
                return
            report = concept_service.generate_concepts(task_db, task_db.get(Course, course_id))
            # 任务 48k：概念库就绪后顺手 0-LLM 复检该课讲义（terminology 不再长期 skipped）
            report = _recheck_lectures(task_db, task_db.get(Course, course_id), report)
            job = task_db.get(GenerationJob, job_id)
            if job:
                job.status = "success"
                job.result = report
                task_db.commit()
        except Exception as exc:  # noqa: BLE001 —— 失败必须显式落库，不静默
            job = task_db.get(GenerationJob, job_id)
            if job:
                job.status = "failed"
                job.error = str(exc)
                task_db.commit()
        finally:
            release_scope(course.id, "concept")
            task_db.close()

    job = GenerationJob(
        course_id=course_id,
        job_type="concept",
        status="running",
        payload={"course_id": course_id, "node_id": getattr(payload, "node_id", None)},
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    job_id = job.id
    try:
        background.add_task(_run)
    except Exception:
        release_scope(course.id, "concept")
        raise
    return {"job_id": job.id, "status": "running", "message": "概念抽取已入队（LLM）"}


@router.get("/course/{course_id}")
def list_course_concepts(course_id: str, db: Session = Depends(get_db)):
    """课程的全部概念条目（含来源与章节，供列表 / 编辑器 / 未来意见挂载使用）。"""
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    rows = concept_service.concept_rows(db, course_id)
    return {
        "course_id": course_id,
        "count": len(rows),
        "concepts": rows,
        "chapters": concept_service.chapter_concept_map(db, course_id),
        # 第 4 步展示用：概念总数 + 关系坏链数（解析不到真实文件的目标不写双链）
        "stats": concept_service.concept_link_report(db, course),
    }


@router.post("/course/{course_id}/graph")
def write_graph(course_id: str, db: Session = Depends(get_db)):
    """生成/刷新概念知识图谱（写 概念库/_图谱.md + _图谱.canvas）。"""
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    try:
        return concept_graph.write_graph(db, course)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/course/{course_id}/supplement")
def supplement(course_id: str, payload: ConceptSupplementRequest | None = None,
               db: Session = Depends(get_db)):
    """一键补抽指定概念（库外关系目标 / 孤点）。

    P6-1：图谱里「指向库外」的关系与「没有任何关系的孤点」原本只能眼看，
    这里把名单交给 LLM 补齐定义与关系；**不会**删掉概念库里其余条目。
    """
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    names = [str(n) for n in (payload.names if payload else []) if str(n or "").strip()]
    if not names:
        data = concept_graph.graph_data(db, course)
        seen: set[str] = set()
        for row in data.get("dropped_relations") or []:
            target = str(row.get("target") or "").strip()
            if target and target not in seen:
                seen.add(target)
                names.append(target)
    if not names:
        raise HTTPException(400, "没有需要补抽的概念（图谱里没有库外关系目标）")
    try:
        return concept_service.supplement_concepts(db, course, names)
    except concept_service.ConceptExtractionError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/course/{course_id}/graph")
def preview_graph(course_id: str, format: str = "mermaid", db: Session = Depends(get_db)):
    """预览概念图谱：format=mermaid 返回 Mermaid 源码（含 md 全文），canvas 返回 Canvas JSON。"""
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    fmt = (format or "mermaid").strip().lower()
    if fmt not in ("mermaid", "canvas"):
        raise HTTPException(400, "format 仅支持 mermaid 或 canvas")
    data = concept_graph.graph_data(db, course)
    payload = {
        "course_id": course.id,
        "course_title": course.title,
        "format": fmt,
        "stats": data["stats"],
        "isolated": data["isolated"],
        "dropped_relations": data["dropped_relations"],
        "chapters": data["chapters"],
        "nodes": [{"id": node["id"], "name": node["name"], "chapter": node["chapter"]}
                  for node in data["nodes"]],
        "files": [concept_graph.GRAPH_MD_REL, concept_graph.GRAPH_CANVAS_REL],
    }
    if fmt == "mermaid":
        payload["mermaid"] = concept_graph.to_mermaid(data)
        payload["text"] = concept_graph.graph_markdown(db, course, data)
    else:
        payload["canvas"] = concept_graph.to_canvas(data)
    return payload


@router.get("/jobs/{job_id}")
def job_status(job_id: str, db: Session = Depends(get_db)):
    job = db.get(GenerationJob, job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    return {"job_id": job.id, "status": job.status, "result": job.result, "error": job.error}


@router.patch("/{item_id}")
def update(item_id: str, payload: ConceptUpdateRequest, db: Session = Depends(get_db)):
    """网页端编辑概念：标记 edited，重抽时保留不覆盖。"""
    row = db.get(ContentItem, item_id)
    if row is None or row.kind != concept_service.CONCEPT_KIND:
        raise HTTPException(404, "概念不存在")
    row = concept_service.update_concept(db, row, payload.model_dump(exclude_unset=True))
    return {"id": row.id, "name": row.title, "body": row.body, "meta": row.meta}


@router.delete("/{item_id}")
def remove(item_id: str, db: Session = Depends(get_db)):
    row = db.get(ContentItem, item_id)
    if row is None or row.kind != concept_service.CONCEPT_KIND:
        raise HTTPException(404, "概念不存在")
    db.delete(row)
    db.commit()
    return {"ok": True, "id": item_id}
