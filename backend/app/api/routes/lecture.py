"""完整讲义（Lecture）生成 / 编辑 / 删除路由。"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from sqlalchemy.orm import Session
from urllib.parse import quote

from app.api.deps import require_api_key
from app.db.session import SessionLocal, get_db
from app.models.course import STATUS_APPROVED, ContentItem, Course, GenerationJob, Outline, OutlineNode
from app.schemas.lecture import (
    LectureBatchGenerateRequest,
    LectureChapterGenerateRequest,
    LectureEditRequest,
    LectureGenerateRequest,
    LectureLockRequest,
    LectureQcRegenerateRequest,
    LessonPlanGenerateRequest,
    CourseQcRequest,
    StyleAnchorLockRequest,
    StyleAnchorRequest,
)
from app.services.jobs import acquire_scope, claim_job_exclusive, has_active_job, release_scope
from app.services import docx_export
from app.services import kb as kb_mod
from app.services import lesson_plan
from app.services import student_sheet
from app.services.lecture import (
    LOCKED_OVERWRITE_NOTE,
    _failures_of,
    _lecture_target_nodes,
    _locked_of,
    _target_lecture,
    chapter_leaf_nodes,
    delete_lecture,
    edit_lecture,
    generate_all_lectures,
    generate_chapter_lectures,
    generate_lecture,
    locked_overwrite_blocked,
    regenerate_cards_for_lecture,
    retry_diagram_failure,
    set_lecture_locked,
)
from app.services.material import material_prompt_section
from app.services.lecture_qc import course_report, item_qc, open_issues
from app.services.style_anchor import (
    ANCHOR_LOCK_REASON,
    anchor_lock_state,
    anchor_overwrite_risk,
    anchor_source_item,
    anchor_summary,
    clear_style_anchor,
    derive_anchor,
    get_style_anchor,
    save_style_anchor,
)

router = APIRouter(prefix="/lecture", tags=["lecture"], dependencies=[Depends(require_api_key)])


@router.post("/generate")
def generate(
    payload: LectureGenerateRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """单篇完整讲义生成（异步入队；job 状态走 GET /content/jobs/{job_id}）。

    任务 91：目标讲义已加锁（审核通过 / 人工编辑）时默认 **409**——“重跑这一节”
    不再能静默地把老师审过的那一版换成没审过的新版；确认要覆盖就显式传 ``allow_overwrite_locked=True``，
    覆盖成功后该篇的 ``locked`` / ``locked_reason`` 会被清掉（徽标只贴真被审过的那一版）。
    """
    course = db.get(Course, payload.course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    if course.status != STATUS_APPROVED:
        raise HTTPException(409, "请先在大纲步骤点「大纲通过」，锁定大纲后再生成完整讲义")
    node = db.get(OutlineNode, payload.node_id)
    if node is None or node.outline is None or node.outline.course_id != course.id:
        raise HTTPException(400, "所选节点不属于当前课程，请重新选择")
    # 任务 91：已「审核通过/人工编辑」加锁的单篇不允许悄无声息地被重跑覆盖
    # （批量路径本来就 skip_locked 跳过它）。要重跑：先解锁，或显式传 allow_overwrite_locked=True。
    try:
        current = _target_lecture(db, course, node, payload.item_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    blocked = locked_overwrite_blocked(current)
    if blocked and not payload.allow_overwrite_locked:
        raise HTTPException(409, blocked)
    if has_active_job(db, course.id, "lecture"):
        raise HTTPException(409, "该课程已有进行中的讲义生成任务，请等待完成后再试")
    if not acquire_scope(course.id, "lecture"):
        raise HTTPException(409, "该课程讲义生成任务正在执行中，请稍后再试")

    def _run() -> None:
        task_db = SessionLocal()
        try:
            job_row = task_db.get(GenerationJob, job_id)
            if not claim_job_exclusive(task_db, job_row):
                return
            task_course = task_db.get(Course, course.id)
            task_node = task_db.get(OutlineNode, payload.node_id)
            was_locked = _locked_of(
                _target_lecture(task_db, task_course, task_node, payload.item_id))
            item = generate_lecture(
                task_db,
                task_course,
                task_node,
                payload.tone,
                payload.item_id,
                material=material_prompt_section(task_db, task_course, numbered=True),
            )
            job = task_db.get(GenerationJob, job_id)
            if job:
                job.status = "success"
                job.result = {
                    "item": {
                        "id": item.id,
                        "kind": item.kind,
                        "title": item.title,
                        "node_id": item.node_id,
                        "body": item.body,
                        "meta": item.meta,
                    },
                    # 任务 91：覆盖了原本审核锁定的一节 ⇒ 实话实说（徽标已摘，需重新审核）
                    "unlocked": bool(was_locked),
                    "unlock_message": LOCKED_OVERWRITE_NOTE if was_locked else "",
                    # 任务 23-A：单篇任务也带出用量，任务面板可显示「X 次调用 · Y tokens」
                    "usage": (item.meta or {}).get("usage") or {},
                    # 7c：单篇生成也要把「哪张图失败了」显式带出来，网页端才能挂黄标重试
                    "diagram_failed": [
                        {"failure_id": r.get("id"), "node_key": item.meta.get("node_key"),
                         "slot": r.get("slot"), "stage": r.get("stage"), "reason": r.get("reason")}
                        for r in _failures_of(item)
                    ],
                }
                task_db.commit()
        except Exception as exc:  # noqa: BLE001 —— 失败写入 job.error，由轮询方展示
            job = task_db.get(GenerationJob, job_id)
            if job:
                job.status = "failed"
                job.error = str(exc)
                task_db.commit()
        finally:
            release_scope(course.id, "lecture")
            task_db.close()

    job = GenerationJob(course_id=course.id, job_type="lecture", status="running",
                        payload=payload.model_dump())
    db.add(job)
    db.commit()
    db.refresh(job)
    job_id = job.id
    try:
        background.add_task(_run)
    except Exception:
        release_scope(course.id, "lecture")
        raise
    return {"job_id": job.id, "status": "running", "message": "讲义生成已入队"}


@router.post("/generate-all")
def generate_all(
    payload: LectureBatchGenerateRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """一次性为整门课所有知识点生成完整讲义（M3 起收进「高级选项」；异步）。

    未设置样板锚时必须显式 ignore_style_anchor=True，否则拒绝并引导先做样板——
    这是「显式确认」而不是静默换一套风格（网页端按钮级引导，正常不会走到这里）。
    """
    course = db.get(Course, payload.course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    if course.status != STATUS_APPROVED:
        raise HTTPException(409, "请先在大纲步骤点「大纲通过」，锁定大纲后再一键生成整门课讲义")
    outline = (
        db.query(Outline)
        .filter(Outline.course_id == course.id)
        .order_by(Outline.created_at.desc())
        .first()
    )
    if outline is None:
        raise HTTPException(400, "还没有大纲，请先生成并通过大纲")
    anchor = get_style_anchor(course)
    if anchor is None and not payload.ignore_style_anchor:
        raise HTTPException(
            409,
            "请先生成一节样板并点「以这篇为样板」，再按章生成；"
            "确实要一次性生成整门课，请勾选「忽略样板，一次性生成整门课」后重试")
    if has_active_job(db, course.id, "lecture_batch"):
        raise HTTPException(409, "该课程已有进行中的讲义生成任务，请等待完成后再试")
    risk = anchor_overwrite_risk(db, course, _lecture_target_nodes(db, course, None))
    if risk and not risk["locked"] and not payload.allow_anchor_overwrite:
        raise HTTPException(409, risk["message"])
    if not acquire_scope(course.id, "lecture_batch"):
        raise HTTPException(409, "该课程讲义生成任务正在执行中，请稍后再试")

    def _run() -> None:
        task_db = SessionLocal()
        try:
            job_row = task_db.get(GenerationJob, job_id)
            if not claim_job_exclusive(task_db, job_row):
                return
            task_course = task_db.get(Course, payload.course_id)
            progress = _progress_writer(task_db, job_id, {"scope": "course"})
            result = generate_all_lectures(
                task_db, task_course, tone=payload.tone, anchor=anchor,
                ignore_style_anchor=payload.ignore_style_anchor,
                force=payload.force, progress=progress)
            job = task_db.get(GenerationJob, job_id)
            if job:
                job.status = "success"
                job.result = {**result, "scope": "course", "done": result["total"]}
                task_db.commit()
        except Exception as exc:  # noqa: BLE001
            job = task_db.get(GenerationJob, job_id)
            if job:
                job.status = "failed"
                job.error = str(exc)
                task_db.commit()
        finally:
            release_scope(course.id, "lecture_batch")
            task_db.close()

    job = GenerationJob(course_id=course.id, job_type="lecture_batch", status="running")
    db.add(job)
    db.commit()
    db.refresh(job)
    job_id = job.id
    try:
        background.add_task(_run)
    except Exception:
        release_scope(course.id, "lecture_batch")
        raise
    return {"job_id": job.id, "status": "running", "message": "整门课完整讲义生成已入队"}


def _progress_writer(db: Session, job_id: str, scope: dict):
    """任务面板数据源：每进入/结束一个知识点就把进度写进 job.result。

    写出的字段：第 i/N 个知识点、当前知识点、ok/failed/locked 计数、所在章节。
    """
    counters = {"ok": 0, "failed": 0, "locked": 0, "skipped": 0}

    def progress(done: int, total: int, node: OutlineNode, status: str) -> None:
        if status in counters:
            counters[status] += 1
        job = db.get(GenerationJob, job_id)
        if job is None:
            return
        job.result = {
            **scope,
            "done": done,
            "total": total,
            "current": f"{node.node_key} {node.title}",
            "node_key": node.node_key,
            "status": status,
            **counters,
        }
        db.commit()

    return progress


@router.post("/generate-chapter")
def generate_chapter(
    payload: LectureChapterGenerateRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """M3 按章批量：只生成该章节下的知识点（一个章节一个任务，失败可单章重跑）。

    逐知识点顺序生成，单点失败记录并继续；已加锁（人工编辑/审核通过）的节点跳过，
    并在结果里列进 locked_skipped。中途停止 = 不再提交后续章节任务，已完成的章节不受影响。
    """
    course = db.get(Course, payload.course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    if course.status != STATUS_APPROVED:
        raise HTTPException(409, "请先在大纲步骤点「大纲通过」，锁定大纲后再按章生成")
    chapter = db.get(OutlineNode, payload.chapter_node_id)
    if chapter is None or chapter.outline is None or chapter.outline.course_id != course.id:
        raise HTTPException(400, "所选章节不属于当前课程，请重新选择")
    members = chapter_leaf_nodes(db, course, chapter)
    if not members:
        raise HTTPException(400, "该章节下没有可生成的知识点")
    if has_active_job(db, course.id, "lecture_batch"):
        raise HTTPException(409, "该课程已有进行中的讲义生成任务，请等待完成后再试")
    risk = anchor_overwrite_risk(db, course, members)
    if risk and not risk["locked"] and not payload.allow_anchor_overwrite:
        raise HTTPException(409, risk["message"])
    if not acquire_scope(course.id, "lecture_batch"):
        raise HTTPException(409, "该课程讲义生成任务正在执行中，请稍后再试")

    anchor = get_style_anchor(course)
    scope = {"scope": "chapter", "chapter_key": chapter.node_key,
             "chapter_title": chapter.title, "chapter_node_id": chapter.id}

    def _run() -> None:
        task_db = SessionLocal()
        try:
            job_row = task_db.get(GenerationJob, job_id)
            if not claim_job_exclusive(task_db, job_row):
                return
            task_course = task_db.get(Course, payload.course_id)
            task_chapter = task_db.get(OutlineNode, payload.chapter_node_id)
            progress = _progress_writer(task_db, job_id, scope)
            result = generate_chapter_lectures(
                task_db, task_course, task_chapter, tone=payload.tone,
                anchor=anchor, force=payload.force, progress=progress)
            job = task_db.get(GenerationJob, job_id)
            if job:
                job.status = "success"
                job.result = {**result, **scope, "done": result["total"]}
                task_db.commit()
        except Exception as exc:  # noqa: BLE001
            job = task_db.get(GenerationJob, job_id)
            if job:
                job.status = "failed"
                job.error = str(exc)
                task_db.commit()
        finally:
            release_scope(course.id, "lecture_batch")
            task_db.close()

    job = GenerationJob(course_id=course.id, job_type="lecture_batch", status="running",
                        payload={**payload.model_dump(), **scope})
    db.add(job)
    db.commit()
    db.refresh(job)
    job_id = job.id
    try:
        background.add_task(_run)
    except Exception:
        release_scope(course.id, "lecture_batch")
        raise
    return {"job_id": job.id, "status": "running", "total": len(members),
            "chapter_key": chapter.node_key, "chapter_title": chapter.title,
            "message": f"已入队：生成第 {chapter.node_key} 章（{len(members)} 个知识点）"}


@router.get("/style-anchor")
def read_style_anchor(course_id: str, db: Session = Depends(get_db)):
    """读取课程样板锚（网页端顶部横幅用）。"""
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    anchor = get_style_anchor(course)
    state = anchor_lock_state(db, course)
    return {"course_id": course.id, "style_anchor": anchor,
            "summary": anchor_summary(anchor) if anchor else "",
            "locked": state["locked"], "lock_notice": state["notice"],
            "source_item_id": state.get("source_item_id")}


@router.post("/style-anchor")
def write_style_anchor(payload: StyleAnchorRequest, db: Session = Depends(get_db)):
    """设置/更新样板锚：给讲义 id 就抽取，给的字段覆盖抽取结果（UI 可编辑）。"""
    course = db.get(Course, payload.course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    anchor: dict = {}
    locked = False
    if payload.source_item_id:
        item = db.get(ContentItem, payload.source_item_id)
        if (item is None or item.course_id != course.id or item.kind != "lecture"):
            raise HTTPException(404, "样板讲义不存在")
        anchor = derive_anchor(db, course, item)
    else:
        item = None
        anchor = get_style_anchor(course) or {}
    if not anchor:
        raise HTTPException(400, "请指定一篇讲义作为样板（先点讲义卡上的「以这篇为样板」）")
    overrides = payload.model_dump(exclude_none=True,
                                   exclude={"course_id", "source_item_id"})
    anchor.update(overrides)
    saved = save_style_anchor(db, course, anchor)
    if item is not None:
        # P4-1：样板锚会改掉该节点的输入指纹 → 批量时它不再算「输入未变」而被重做。
        # 设样板 = 认可这一版，所以这里自动加锁（locked 语义本来就有）。
        set_lecture_locked(db, item, True, ANCHOR_LOCK_REASON)
        locked = True
    state = anchor_lock_state(db, course)
    return {"ok": True, "style_anchor": saved, "summary": anchor_summary(saved),
            "locked": locked, "source_item_id": state.get("source_item_id"),
            "lock_notice": state.get("notice") or ""}


@router.delete("/style-anchor")
def remove_style_anchor(course_id: str, db: Session = Depends(get_db)):
    """清除样板锚（回到「先生成一节样板」状态）。"""
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    clear_style_anchor(db, course)
    return {"ok": True, "style_anchor": None}


@router.post("/style-anchor/source-lock")
def lock_anchor_source(payload: StyleAnchorLockRequest, db: Session = Depends(get_db)):
    """样板节加锁 / 解锁（P4-1）：「换样板」前想要重做这一版时先解锁。"""
    course = db.get(Course, payload.course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    item = anchor_source_item(db, course)
    if item is None:
        raise HTTPException(404, "样板讲义不存在（可能已被删除），请重新设置样板")
    set_lecture_locked(db, item, payload.locked,
                       ANCHOR_LOCK_REASON if payload.locked else "")
    state = anchor_lock_state(db, course)
    return {"ok": True, "locked": state["locked"], "source_item_id": item.id,
            "lock_notice": state["notice"]}


@router.post("/{item_id}/lock")
def lock_lecture(item_id: str, payload: LectureLockRequest, db: Session = Depends(get_db)):
    """M3 节点加锁：审核通过/人工定稿的讲义锁定，按章批量跳过它（可解锁并重做）。"""
    item = db.get(ContentItem, item_id)
    if item is None or item.kind != "lecture":
        raise HTTPException(404, "讲义不存在")
    set_lecture_locked(db, item, payload.locked, payload.reason)
    return {"id": item.id, "locked": bool((item.meta or {}).get("locked")),
            "meta": item.meta}


@router.post("/{item_id}/failures/{failure_id}/retry")
def retry_failure(item_id: str, failure_id: str, db: Session = Depends(get_db)):
    """7c：单张重试失败的配图/概念卡（成功即清除失败记录并同步笔记）。"""
    item = db.get(ContentItem, item_id)
    if item is None or item.kind != "lecture":
        raise HTTPException(404, "讲义不存在")
    try:
        return retry_diagram_failure(db, item, failure_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(502, f"重试失败：{exc}") from exc


@router.post("/{item_id}/cards")
def rebuild_concept_cards(item_id: str, db: Session = Depends(get_db)):
    """对已有讲义重建「概念卡」：清旧卡 → 按讲义正文重新规划并就近插入。"""
    item = db.get(ContentItem, item_id)
    if item is None or item.kind != "lecture":
        raise HTTPException(404, "讲义不存在")
    try:
        count = regenerate_cards_for_lecture(db, item)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(502, f"概念卡生成失败：{exc}") from exc
    db.refresh(item)
    return {"id": item.id, "cards": count, "kind": item.kind,
            "body": item.body, "meta": item.meta}


@router.patch("/{item_id}")
def update(item_id: str, payload: LectureEditRequest, db: Session = Depends(get_db)):
    item = db.get(ContentItem, item_id)
    if item is None or item.kind != "lecture":
        raise HTTPException(404, "讲义不存在")
    edit_lecture(db, item, payload.markdown)
    return {"id": item.id, "kind": item.kind, "title": item.title,
            "body": item.body, "meta": item.meta}


@router.delete("/{item_id}")
def delete(item_id: str, db: Session = Depends(get_db)):
    item = db.get(ContentItem, item_id)
    if item is None or item.kind != "lecture":
        raise HTTPException(404, "讲义不存在")
    delete_lecture(db, item)
    return {"ok": True, "id": item_id}


# ---------------------------------------------------------------------------
# 任务 49：教案（kind=lesson_plan，0 LLM 骨架；交付包与笔记库导出同一份渲染）
# ---------------------------------------------------------------------------

@router.post("/course/{course_id}/lesson-plans")
def build_lesson_plans(course_id: str, payload: LessonPlanGenerateRequest | None = None,
                       db: Session = Depends(get_db)):
    """生成/刷新教案骨架并落库（幂等：正文没变就不产生新条目、不刷新生成时间）。"""
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    result = lesson_plan.generate_for_course(
        db, course, node_ids=(payload.node_ids if payload else None))
    return {**{key: value for key, value in result.items() if key != "rows"},
            "items": lesson_plan.list_plans(db, course)}


@router.get("/course/{course_id}/lesson-plans")
def read_lesson_plans(course_id: str, db: Session = Depends(get_db)):
    """列出已落库的教案条目（网页端列表用）。"""
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    return {"course_id": course.id, "items": lesson_plan.list_plans(db, course)}


@router.get("/lesson-plan/{item_id}.docx")
def download_lesson_plan_docx(item_id: str, db: Session = Depends(get_db)):
    """下载教案 Word（老师可直接改的那一份）。"""
    item = db.get(ContentItem, item_id)
    if item is None or item.kind != lesson_plan.KIND:
        raise HTTPException(404, "教案不存在")
    markdown = str((item.body or {}).get("markdown") or "")
    data = docx_export.markdown_to_docx(markdown, title=item.title or "教案")
    filename = f"{item.title or 'lesson-plan'}.docx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition":
                 f"attachment; filename*=UTF-8''{quote(filename)}"})


# ---------------------------------------------------------------------------
# 任务 50：学案（kind=student_sheet，学生版：去答案留空 + 作答区 + 页码）
# ---------------------------------------------------------------------------

@router.post("/course/{course_id}/student-sheets")
def build_student_sheets(course_id: str, payload: LessonPlanGenerateRequest | None = None,
                         db: Session = Depends(get_db)):
    """生成/刷新学案（学生版）并落库；幂等口径与教案一致。"""
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    result = student_sheet.generate_for_course(
        db, course, node_ids=(payload.node_ids if payload else None))
    return {**{key: value for key, value in result.items() if key != "rows"},
            "items": student_sheet.list_sheets(db, course)}


@router.get("/course/{course_id}/student-sheets")
def read_student_sheets(course_id: str, db: Session = Depends(get_db)):
    """列出已落库的学案条目。"""
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    return {"course_id": course.id, "items": student_sheet.list_sheets(db, course)}


@router.get("/student-sheet/{item_id}.docx")
def download_student_sheet_docx(item_id: str, db: Session = Depends(get_db)):
    """下载学案 Word（学生版；页脚带页码，可直接打印发放）。"""
    item = db.get(ContentItem, item_id)
    if item is None or item.kind != student_sheet.KIND:
        raise HTTPException(404, "学案不存在")
    course = db.get(Course, item.course_id)
    markdown = str((item.body or {}).get("markdown") or "")
    data = docx_export.markdown_to_docx(
        markdown, title=item.title or "学案", page_numbers=True,
        footer_text=kb_mod._slug(course.title, 30) if course is not None else "")
    filename = f"{item.title or 'student-sheet'}.docx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition":
                 f"attachment; filename*=UTF-8''{quote(filename)}"})


# ---------------------------------------------------------------------------
# 任务 22：讲义质检（读报告 / 整课汇总 / 按质检建议重生成）
# ---------------------------------------------------------------------------

def _lecture_items(db: Session, course_id: str) -> list[ContentItem]:
    return (db.query(ContentItem)
            .filter(ContentItem.course_id == course_id, ContentItem.kind == "lecture")
            .order_by(ContentItem.created_at).all())


def _qc_payload(db: Session, course: Course, item: ContentItem, *,
                refresh: bool = False) -> dict:
    """单篇讲义质检载荷（徽标 + 明细 + 来源锚定，前端讲义卡与页签共用）。"""
    qc = item_qc(db, course, item, refresh=refresh)
    node = db.get(OutlineNode, item.node_id) if item.node_id else None
    meta = item.meta or {}
    return {
        "item_id": item.id,
        "node_id": item.node_id,
        "node_key": (node.node_key if node is not None else meta.get("node_key")),
        "title": item.title,
        "locked": bool(meta.get("locked")),
        "sources": list(meta.get("sources") or []),
        "issues": open_issues(qc),
        "qc": qc,
    }


@router.get("/{item_id}/qc")
def read_qc(item_id: str, refresh: bool = False, db: Session = Depends(get_db)):
    """单篇讲义质检报告（meta.qc）。

    老讲义没有质检结果、或存的是上线前的老口径（缺「是哪几句」）时，
    按当前正文就地补算并落库 —— 纯本地解析、零 LLM，不刷新也能看到具体句子。
    """
    item = db.get(ContentItem, item_id)
    if item is None or item.kind != "lecture":
        raise HTTPException(404, "讲义不存在")
    course = db.get(Course, item.course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    return _qc_payload(db, course, item, refresh=refresh)


@router.post("/course/{course_id}/qc")
def read_course_qc(course_id: str, payload: CourseQcRequest | None = None,
                   db: Session = Depends(get_db)):
    """整课质检汇总：平均分、各检查项失败分布、最差 N 篇（整课质量报告页签）。"""
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    req = payload or CourseQcRequest()
    if req.refresh:
        for item in _lecture_items(db, course_id):
            item_qc(db, course, item, refresh=True)
    return course_report(db, course, worst_n=req.worst_n)


@router.post("/{item_id}/regenerate-qc")
def regenerate_with_qc(item_id: str, background: BackgroundTasks,
                       payload: LectureQcRegenerateRequest | None = None,
                       db: Session = Depends(get_db)):
    """按质检建议重生成：把上一版 warn/fail 逐条注入提示词后原地重做（异步入队）。"""
    item = db.get(ContentItem, item_id)
    if item is None or item.kind != "lecture":
        raise HTTPException(404, "讲义不存在")
    course = db.get(Course, item.course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    if item.node_id is None:
        raise HTTPException(400, "该讲义未绑定知识点，无法按质检建议重生成")
    if course.status != STATUS_APPROVED:
        raise HTTPException(409, "请先在大纲步骤点「大纲通过」，锁定大纲后再生成完整讲义")
    if has_active_job(db, course.id, "lecture"):
        raise HTTPException(409, "该课程已有进行中的讲义生成任务，请等待完成后再试")
    if not acquire_scope(course.id, "lecture"):
        raise HTTPException(409, "该课程讲义生成任务正在执行中，请稍后再试")

    node_id = item.node_id
    tone = (payload.tone if payload is not None else None) or str((item.body or {}).get("tone") or "正式")
    qc = item_qc(db, course, item)

    def _run() -> None:
        task_db = SessionLocal()
        try:
            job_row = task_db.get(GenerationJob, job_id)
            if not claim_job_exclusive(task_db, job_row):
                return
            task_course = task_db.get(Course, course.id)
            task_node = task_db.get(OutlineNode, node_id)
            fresh = generate_lecture(
                task_db,
                task_course,
                task_node,
                tone,
                item_id,
                material=material_prompt_section(task_db, task_course, numbered=True),
                use_qc=True,
            )
            job = task_db.get(GenerationJob, job_id)
            if job:
                job.status = "success"
                job.result = {
                    "item": {
                        "id": fresh.id,
                        "kind": fresh.kind,
                        "title": fresh.title,
                        "node_id": fresh.node_id,
                        "body": fresh.body,
                        "meta": fresh.meta,
                    },
                    "qc_before": qc,
                    "usage": (fresh.meta or {}).get("usage") or {},
                    "diagram_failed": [
                        {"failure_id": r.get("id"), "node_key": fresh.meta.get("node_key"),
                         "slot": r.get("slot"), "stage": r.get("stage"), "reason": r.get("reason")}
                        for r in _failures_of(fresh)
                    ],
                }
                task_db.commit()
        except Exception as exc:  # noqa: BLE001 —— 失败写入 job.error，由轮询方展示
            job = task_db.get(GenerationJob, job_id)
            if job:
                job.status = "failed"
                job.error = str(exc)
                task_db.commit()
        finally:
            release_scope(course.id, "lecture")
            task_db.close()

    job = GenerationJob(course_id=course.id, job_type="lecture", status="running",
                        payload={"item_id": item_id, "node_id": node_id, "tone": tone,
                                 "use_qc": True, "qc_issues": open_issues(qc)})
    db.add(job)
    db.commit()
    db.refresh(job)
    job_id = job.id
    try:
        background.add_task(_run)
    except Exception:
        release_scope(course.id, "lecture")
        raise
    return {"job_id": job.id, "status": "running", "message": "已按质检建议排队重生成",
            "qc_issues": open_issues(qc)}
