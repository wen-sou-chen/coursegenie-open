"""内容生成与查询路由。"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.db.session import SessionLocal, get_db
from app.models.course import STATUS_APPROVED, ContentItem, Course, GenerationJob, OutlineNode
from app.schemas.content import ContentGenerateRequest
from app.schemas.ops import ContentUpdateRequest
from app.services.content import build_content_rows, generate_all_texts
from app.services.jobs import acquire_scope, claim_job_exclusive, has_active_job, release_scope
from app.services.kb import attach_vault_write
from app.services.quiz import generate_quizzes
from app.services.planner import list_nodes
from app.services.storage_files import item_file_refs, remove_files

router = APIRouter(prefix="/content", tags=["content"], dependencies=[Depends(require_api_key)])


@router.post("/generate")
def generate(payload: ContentGenerateRequest, background: BackgroundTasks, db: Session = Depends(get_db)):
    course = db.get(Course, payload.course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    if course.status != STATUS_APPROVED:
        raise HTTPException(409, "请先在大纲步骤点「大纲通过」，锁定大纲后再生成章节内容")
    if has_active_job(db, payload.course_id, "content"):
        raise HTTPException(409, "该课程已有进行中的内容生成任务，请等待完成后再试")
    if not acquire_scope(payload.course_id, "content"):
        raise HTTPException(409, "该课程内容生成任务正在执行中，请稍后再试")

    def _run() -> None:
        task_db = SessionLocal()
        try:
            job_row = task_db.get(GenerationJob, job_id)
            if not claim_job_exclusive(task_db, job_row):
                return
            count = generate_all_texts(task_db, payload.course_id, payload.node_ids, payload.tone)
            if payload.include_quiz:
                task_course = task_db.get(Course, payload.course_id)
                if payload.node_ids:
                    nodes = task_db.query(OutlineNode).filter(
                        OutlineNode.id.in_(payload.node_ids)).all()
                else:
                    nodes = list_nodes(task_db, payload.course_id)
                for node in nodes:
                    generate_quizzes(task_db, task_course, node, count=3, difficulty="medium")
            job = task_db.get(GenerationJob, job_id)
            if job:
                job.status = "success"
                job.result = {"count": count}
                task_db.commit()
        except Exception as exc:  # noqa: BLE001
            job = task_db.get(GenerationJob, job_id)
            if job:
                job.status = "failed"
                job.error = str(exc)
                task_db.commit()
        finally:
            release_scope(payload.course_id, "content")
            task_db.close()

    job = GenerationJob(course_id=payload.course_id, job_type="content", status="running")
    db.add(job)
    db.commit()
    db.refresh(job)
    job_id = job.id
    try:
        background.add_task(_run)
    except Exception:
        release_scope(payload.course_id, "content")
        raise
    return {"job_id": job.id, "status": "running", "message": "内容生成已入队"}


@router.get("/{course_id}")
def list_content(course_id: str, db: Session = Depends(get_db)):
    return build_content_rows(db, course_id)


@router.post("/{item_id}/vault")
def retry_vault_write(item_id: str, db: Session = Depends(get_db)):
    """重试把单个内容条目增量写入 Vault（不重新生成内容）。"""
    item = db.get(ContentItem, item_id)
    if item is None:
        raise HTTPException(404, "内容项不存在")
    kind = "chart" if item.kind.startswith("chart_") else item.kind
    try:
        result = attach_vault_write(db, item, kind)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"笔记写入失败：{exc}") from exc
    return {"id": item.id, "vault": result}


@router.get("/jobs/{job_id}")
def job_status(job_id: str, db: Session = Depends(get_db)):
    job = db.get(GenerationJob, job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    return {"job_id": job.id, "status": job.status, "result": job.result, "error": job.error}


@router.patch("/{item_id}")
def update_item(item_id: str, payload: ContentUpdateRequest, db: Session = Depends(get_db)):
    """就地编辑内容项（讲解文本/提示词/Mermaid/标题等）；body 做一层合并。"""
    item = db.get(ContentItem, item_id)
    if item is None:
        raise HTTPException(404, "内容项不存在")
    if payload.title is not None:
        item.title = payload.title
    if payload.body is not None:
        item.body = {**(item.body or {}), **payload.body}
    db.commit()
    db.refresh(item)
    return {"id": item.id, "kind": item.kind, "title": item.title, "body": item.body, "meta": item.meta}


@router.delete("/{item_id}")
def delete_item(item_id: str, db: Session = Depends(get_db)):
    item = db.get(ContentItem, item_id)
    if item is None:
        raise HTTPException(404, "内容项不存在")
    files = item_file_refs(item)
    db.delete(item)
    db.commit()
    removed = remove_files(files)
    return {"ok": True, "id": item_id, "removed_files": removed}
