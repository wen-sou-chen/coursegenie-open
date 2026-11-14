"""PPT 路由：整课 / 每章课件生成（异步 job + 单课互斥），原地重做与教师上传。"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.core.config import get_settings
from app.db.session import SessionLocal, get_db
from app.models.course import STATUS_APPROVED, ContentItem, Course, GenerationJob
from app.schemas.ppt import (
    PptBatchRequest,
    PptGenerateRequest,
    PptOutlineResponse,
    PptThemeRequest,
    PresenterDataResponse,
    StudentDataResponse,
)
from app.services import deck_html
from app.services import delivery as delivery_mod
from app.services import presenter_mode
from app.services import student_mode
from app.services import ppt_theme_setting
from app.services.jobs import acquire_scope, claim_job_exclusive, has_active_job, release_scope
from app.services.deck_label import COURSE_DECK_LABEL, node_label
from app.services.planner import list_nodes
from app.services.ppt import (
    SCOPE_CHAPTER, SCOPE_COURSE, audit_pptx, export_pdf_preview, generate_all_chapter_decks,
    generate_deck, import_ppt_upload, list_decks, outline_slides,
)
from app.services.ppt_theme import resolve_theme, theme_options
from app.utils.uploads import UploadTooLarge, read_upload

router = APIRouter(prefix="/ppt", tags=["ppt"], dependencies=[Depends(require_api_key)])

logger = logging.getLogger(__name__)


def _deck_out(item, *, chapter=None) -> dict:
    """课件条目 → 前端契约。

    ``node_id``/``chapter_node_id``/``chapter_key`` 三者一定有值（老数据没有 node_id 时
    用 ``meta.chapter_key`` 匹配到当前大纲节点补齐，**不写库、不迁移**）；
    ``label``/``sub_label`` 是与前端 ``chapterLabel.ts`` 同口径的展示字段。
    """
    meta = dict(item.meta or {})
    scope = str(meta.get("scope") or SCOPE_COURSE)
    chapter_node_id = meta.get("chapter_node_id") or (
        chapter.id if chapter is not None else None
    )
    chapter_key = meta.get("chapter_key") or (
        chapter.node_key if chapter is not None else None
    )
    label = node_label(chapter_key, chapter.title if chapter is not None else "",
                       chapter.node_type if chapter is not None else "chapter") \
        if scope == SCOPE_CHAPTER else COURSE_DECK_LABEL
    return {
        "id": item.id,
        "title": item.title,
        "body": item.body,
        "meta": meta,
        "scope": scope,
        "node_id": item.node_id or chapter_node_id,
        "chapter_node_id": chapter_node_id,
        "chapter_key": chapter_key,
        "label": label,
        "sub_label": node_label(chapter_key, chapter.title if chapter is not None else "",
                                chapter.node_type if chapter is not None else "chapter"),
        "chapter_title": chapter.title if chapter is not None else "",
        "engine": meta.get("engine") or (item.body or {}).get("engine"),
        "slide_count": meta.get("slide_count"),
        "generated_at": meta.get("generated_at"),
        "engine_error": meta.get("engine_error"),
        "source": meta.get("source"),
        "figure_count": meta.get("figure_count"),
        "figure_failures": list(meta.get("figure_failures") or []),
        "slides_per_point": meta.get("slides_per_point"),
        "theme": meta.get("theme"),
        "theme_name": meta.get("theme_name"),
        "theme_source": meta.get("theme_source"),
        "theme_error": meta.get("theme_error"),
        "layouts": list(meta.get("layouts") or []),
        "layout_notes": list(meta.get("layout_notes") or []),
        "slides": _slides_out(item),
        "slides_error": meta.get("slides_error"),
        "images_error": meta.get("images_error"),
        "urls": {k: _servable(v) for k, v in (item.body or {}).items()
                 if k in ("pptx", "pdf", "html") and v},
    }


def _ppt_file_url(raw) -> str | None:
    """meta.slides[].image（相对 ppt_work_dir 或绝对路径）→ /ppt-files 可访问 URL。"""
    if not raw:
        return None
    try:
        base = get_settings().ppt_work_dir.resolve()
        path = Path(str(raw))
        cand = (path if path.is_absolute() else base / path).resolve()
        if not cand.exists():
            return None
        return "/ppt-files/" + cand.relative_to(base).as_posix()
    except (ValueError, OSError):
        return None


def _slides_out(item) -> list[dict]:
    """页级大纲 + 每页图片 URL（图片不存在时不带 URL，前端自然回退文字预览）。"""
    out: list[dict] = []
    for slide in outline_slides(item):
        entry = dict(slide)
        url = _ppt_file_url(entry.get("image"))
        if url:
            entry["image_url"] = url
        out.append(entry)
    return out


@router.post("/generate")
def generate(payload: PptGenerateRequest, background: BackgroundTasks, db: Session = Depends(get_db)):
    course = db.get(Course, payload.course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    if course.status != STATUS_APPROVED:
        raise HTTPException(409, "课程大纲尚未通过，无法生成课件")
    if payload.scope == SCOPE_CHAPTER and not payload.chapter_node_id:
        raise HTTPException(400, "每章课件需要提供 chapter_node_id")
    if has_active_job(db, course.id, "ppt"):
        raise HTTPException(409, "该课程已有进行中的 PPT 生成任务，请等待完成后再试")
    if not acquire_scope(course.id, "ppt"):
        raise HTTPException(409, "该课程 PPT 生成任务正在执行中，请稍后再试")

    def _run() -> None:
        task_db = SessionLocal()
        try:
            job_row = task_db.get(GenerationJob, job_id)
            if not claim_job_exclusive(task_db, job_row):
                return
            item = generate_deck(task_db, task_db.get(Course, payload.course_id),
                                 scope=payload.scope, chapter_node_id=payload.chapter_node_id,
                                 item_id=payload.item_id, theme=payload.theme,
                                 engine=payload.engine)
            job = task_db.get(GenerationJob, job_id)
            if job:
                job.status = "success"
                job.result = {
                    "content_id": item.id,
                    "scope": item.meta.get("scope"),
                    "chapter_node_id": item.meta.get("chapter_node_id"),
                    "engine": item.meta.get("engine"),
                    "slide_count": item.meta.get("slide_count"),
                    "theme": item.meta.get("theme"),
                    "layout_notes": len(item.meta.get("layout_notes") or []),
                    "engine_error": item.meta.get("engine_error"),
                }
                task_db.commit()
        except Exception as exc:  # noqa: BLE001 —— 失败必须显式落库，不静默
            job = task_db.get(GenerationJob, job_id)
            if job:
                job.status = "failed"
                job.error = str(exc)
                task_db.commit()
        finally:
            release_scope(course.id, "ppt")
            task_db.close()

    job = GenerationJob(course_id=payload.course_id, job_type="ppt", status="running",
                        payload=payload.model_dump())
    db.add(job)
    db.commit()
    db.refresh(job)
    job_id = job.id
    try:
        background.add_task(_run)
    except Exception:
        release_scope(course.id, "ppt")
        raise
    label = "每章课件" if payload.scope == SCOPE_CHAPTER else "整课课件"
    return {"job_id": job.id, "status": "running",
            "message": f"{label}生成已入队（{payload.engine}）"}


@router.post("/generate-chapters")
def generate_chapters(payload: PptBatchRequest, background: BackgroundTasks,
                      db: Session = Depends(get_db)):
    """「补齐全部章节课件」：逐章串行生成整份大纲的章课件（默认只补缺）。

    与单章生成共用同一把课程级 PPT 锁与任务表：一次只跑一个批量，进度写进
    ``generation_jobs.result.progress``（前端据此显示「第 2 章 生成中…（2/3）」），
    逐章失败收集到 ``result.failed``（显式列出章与原因，不静默）。
    """
    course = db.get(Course, payload.course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    if course.status != STATUS_APPROVED:
        raise HTTPException(409, "课程大纲尚未通过，无法生成课件")
    if has_active_job(db, course.id, "ppt"):
        raise HTTPException(409, "该课程已有进行中的 PPT 生成任务，请等待完成后再试")
    if not acquire_scope(course.id, "ppt"):
        raise HTTPException(409, "该课程 PPT 生成任务正在执行中，请稍后再试")

    def _run() -> None:
        task_db = SessionLocal()
        try:
            job_row = task_db.get(GenerationJob, job_id)
            if not claim_job_exclusive(task_db, job_row):
                return
            nodes = list_nodes(task_db, payload.course_id)
            total = len([n for n in nodes if "." not in str(n.node_key or "")])
            summary: dict = {"total": total, "generated": [], "skipped": [], "failed": [],
                             "progress": {"index": 0, "total": total, "label": "",
                                          "status": "running", "error": None}}

            def _progress(idx: int, count: int, chapter, status: str, error: str | None) -> None:
                summary["progress"] = {"index": idx, "total": count,
                                       "label": node_label(chapter.node_key, chapter.title,
                                                           chapter.node_type),
                                       "status": status, "error": error}
                job = task_db.get(GenerationJob, job_id)
                if job:
                    job.result = dict(summary)
                    task_db.commit()

            result = generate_all_chapter_decks(
                task_db, task_db.get(Course, payload.course_id), redo=payload.redo,
                theme=payload.theme, engine=payload.engine, progress=_progress,
            )
            job = task_db.get(GenerationJob, job_id)
            if job:
                job.status = "success"
                job.error = "; ".join(f"{f['label']}：{f['error']}" for f in result["failed"])
                done = dict(summary["progress"])
                done.update({"index": result["total"], "total": result["total"],
                             "status": "done", "error": None})
                job.result = {**result, "progress": done}
                task_db.commit()
        except Exception as exc:  # noqa: BLE001 —— 失败必须显式落库，不静默
            job = task_db.get(GenerationJob, job_id)
            if job:
                job.status = "failed"
                job.error = str(exc)
                task_db.commit()
        finally:
            release_scope(course.id, "ppt")
            task_db.close()

    job = GenerationJob(course_id=payload.course_id, job_type="ppt", status="running",
                        payload=payload.model_dump())
    db.add(job)
    db.commit()
    db.refresh(job)
    job_id = job.id
    try:
        background.add_task(_run)
    except Exception:
        release_scope(course.id, "ppt")
        raise
    return {"job_id": job.id, "status": "running",
            "message": f"章节课件补齐已入队（{'重做全部' if payload.redo else '只补缺'}）"}


@router.post("/import")
async def import_upload(
    course_id: str = Form(...),
    scope: str = Form("course"),
    chapter_node_id: str = Form(""),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """教师上传自己的 PPTX：原地替换目标 scope 的课件条目（source=upload）。"""
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    try:
        data = await read_upload(file, get_settings().max_upload_mb * 1024 * 1024)
    except UploadTooLarge as exc:
        raise HTTPException(413, str(exc)) from exc
    if not data:
        raise HTTPException(400, "空文件")
    try:
        item = import_ppt_upload(db, course, file.filename or "deck.pptx", data,
                                 scope=scope, chapter_node_id=chapter_node_id or None)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _deck_out(item)


@router.post("/theme")
def set_course_theme(payload: PptThemeRequest, db: Session = Depends(get_db)):
    """设置**本课主题**（课程级设置，落在 ``course.meta.ppt_theme``；任务 145）。

    唯一出处 ``ppt_theme_setting.write``：合法值存**规范 id**；非法值 400 + 合法值清单
    （**不静默回落**）；传空串 = 清除本课设置（回到后端配置 ``PPT_THEME``）。
    只改外观、不改内容 —— 已生成的课件不会变，**必须重新生成才生效**（日志与返回文案同一句）。
    """
    course = db.get(Course, payload.course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    try:
        result = ppt_theme_setting.write(course, payload.theme)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    db.commit()
    db.refresh(course)
    label = ppt_theme_setting.source_label(result["source"])
    logger.info("课程主题已更新：course_id=%s title=%s theme=%s（来源：%s）—— %s",
                course.id, course.title, result["theme"] or "（已清除，按后端配置）",
                label, ppt_theme_setting.APPLY_NOTE)
    return {
        "ok": True,
        **result,
        "source_label": label,
        "note": ppt_theme_setting.APPLY_NOTE,
        "message": (f"本课主题：{result['theme_name']}。" if result["theme_name"]
                    else "已清除本课主题设置（按后端配置出课件）。") + ppt_theme_setting.APPLY_NOTE,
    }


@router.get("/themes")
def list_themes(course_id: str = "", db: Session = Depends(get_db)):
    """五套内置主题（供前端下拉）：id / 中文名 / 说明 / 主色 / 6 色数据色板。

    ``active`` 是**后端配置**（``PPT_THEME``）解析出的主题（含未知名字的回退原因），
    前端据此给图表上色 —— 与概念图 / PPT 原生图表用的是同一份色板（老口径不变）。

    任务 145：带 ``course_id`` 时额外回**本课当前主题**（``course``）与**它是哪来的**
    （``source``：``course`` 本课设置 / ``config`` 后端配置 / ``default`` 内置默认；
    人话见 ``source_label``）—— 前端据此把下拉默认选中「本课当前主题」，并显示
    「当前：深蓝商务（本课设置）」。课程设了**非法值**时 ``course.error`` 给出原因（不静默）。
    """
    resolved, theme_error = resolve_theme(get_settings().ppt_theme)
    out: dict = {
        "items": theme_options(),
        "active": {"id": resolved["id"], "name": resolved["name"],
                   "colors": list(resolved["palette"]["colors"]),
                   "error": theme_error},
        "course": None,
    }
    if not course_id:
        return out
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    spec, source, error = ppt_theme_setting.resolve(None, course, get_settings().ppt_theme)
    out["course"] = {
        "id": spec["id"], "name": spec["name"], "colors": list(spec["palette"]["colors"]),
        "source": source, "source_label": ppt_theme_setting.source_label(source),
        # 课程里存的**原始**值（没设过 ⇒ null）：排障时一眼看出「是本课设的还是配置给的」
        "setting": ppt_theme_setting.raw_setting(course) or None,
        "error": error,
    }
    return out


@router.get("/{course_id}")
def list_ppt(course_id: str, db: Session = Depends(get_db)):
    """按 scope 分组返回：整课 1 个 + 各章若干个（同时保留平铺列表兼容旧前端）。

    章节课件历史上有 ``node_id`` 为 NULL、章号只存在 ``meta.chapter_key`` 的数据；
    这里按章号匹配当前大纲节点补齐 ``node_id`` 与展示标签，**只读兜底、不写库**。
    """
    nodes = list_nodes(db, course_id)
    by_key = {str(n.node_key or ""): n for n in nodes}
    items = [_deck_out(i, chapter=by_key.get(str((i.meta or {}).get("chapter_key") or "")))
             for i in list_decks(db, course_id)]
    course_decks = [i for i in items if i["scope"] == SCOPE_COURSE]
    chapter_decks = [i for i in items if i["scope"] == SCOPE_CHAPTER]
    chapter_decks.sort(key=lambda i: (_sort_key(i["chapter_key"]), str(i["generated_at"] or "")))
    return {"items": items, "course": course_decks, "chapters": chapter_decks}


def _sort_key(key) -> tuple:
    """章号排序：数字章按数值排（1 < 2 < 10），其余按字符串。"""
    text = str(key or "")
    return (0, int(text), "") if text.isdigit() else (1, 0, text)


@router.get("/jobs/{job_id}")
def job_status(job_id: str, db: Session = Depends(get_db)):
    job = db.get(GenerationJob, job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    return {"job_id": job.id, "status": job.status, "result": job.result, "error": job.error}


@router.get("/{item_id}/audit")
def audit_deck(item_id: str, db: Session = Depends(get_db)):
    """对磁盘上的课件做一次几何自检（文本重叠 / 文本越界 / 图片出页）。"""
    item = db.get(ContentItem, item_id)
    if item is None or item.kind != "ppt":
        raise HTTPException(404, "课件不存在")
    path = str((item.body or {}).get("pptx") or "")
    if not path or not Path(path).exists():
        raise HTTPException(404, "课件文件不存在")
    issues = audit_pptx(path)
    return {"ok": not issues, "issues": issues, "count": len(issues)}


@router.post("/{item_id}/preview")
def preview_pdf(item_id: str, db: Session = Depends(get_db)):
    """可选 PDF 内嵌预览：开启且探测到 PowerPoint 才导；失败显式回退页级大纲。"""
    item = db.get(ContentItem, item_id)
    if item is None or item.kind != "ppt":
        raise HTTPException(404, "课件不存在")
    result = export_pdf_preview(str((item.body or {}).get("pptx") or ""))
    return {
        "ok": bool(result.get("ok")),
        "enabled": bool(result.get("enabled")),
        "reason": result.get("reason"),
        "fallback": result.get("fallback"),
        "url": _servable(result["pdf"]) if result.get("pdf") else None,
        "slides": _slides_out(item),
    }


@router.get("/{item_id}/html", response_class=HTMLResponse)
def deck_html_page(item_id: str, db: Session = Depends(get_db)):
    """网页版课件（带入场动效）：按需渲染这一份 deck 的**单文件 HTML**。

    复用 `app.services.deck_html` 的既有渲染路径（与出包用的是同一个内部函数）⇒
    响应体与交付包 `课件/HTML/{deck 标题}.html` 逐字节一致，**不另写渲染、不落盘、0 LLM**；
    图仍然是 base64 内联 ⇒ 断网也能打开。
    """
    item = db.get(ContentItem, item_id)
    if item is None or item.kind != "ppt":
        raise HTTPException(404, "课件不存在")
    course = db.get(Course, item.course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    # 与出包同一取数口径（按 created_at 升序，同一节点下的多张图先后才一致）
    items = (db.query(ContentItem)
             .filter(ContentItem.course_id == course.id)
             .order_by(ContentItem.created_at).all())
    try:
        row = deck_html.render_deck(db, course, item,
                                   figures=delivery_mod.figure_rows(items))
    except Exception as exc:  # noqa: BLE001
        # 绝不静默返回空页：原因写日志、错误原样回给调用方
        logger.exception("网页版课件渲染失败：item_id=%s title=%s", item_id, item.title)
        raise HTTPException(500, f"网页版课件渲染失败：{exc}") from exc
    return HTMLResponse(content=row["text"], media_type="text/html; charset=utf-8")


@router.get("/{course_id}/outline", response_model=PptOutlineResponse)
def course_outline(course_id: str, item_id: str | None = Query(default=None),
                   chapter_key: str | None = Query(default=None),
                   db: Session = Depends(get_db)):
    """讲师模式节点树（只读）：chapter → point(节点) → pages[]。

    默认取本课**第一章**课件；也可用 ``item_id`` 指定某一份课件、或 ``chapter_key`` 指定章号。
    节点归并规则与讲师模式 HTML 共用 ``presenter_mode.build_nodes``，不另写一套。
    """
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    try:
        deck = presenter_mode.resolve_deck(db, course, item_id=item_id, chapter_key=chapter_key)
        return presenter_mode.outline_payload(db, course, deck)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 —— 绝不静默返回空树
        logger.exception("讲师模式节点树生成失败：course_id=%s", course_id)
        raise HTTPException(500, f"讲师模式节点树生成失败：{exc}") from exc


@router.get("/{course_id}/student", response_model=StudentDataResponse)
def student_data(course_id: str, db: Session = Depends(get_db)):
    """学生版数据（只读）：地图 → 微单元（含即时自测）→ 复习卡。

    数据全部来自本课已有的大纲、讲义 markdown 与 QuizItem；不新编内容、不调 LLM。
    答案/解析只以 base64 返回，前端点击作答后才解码。
    """
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    try:
        return student_mode.build_course_data(db, course)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 —— 绝不静默返回空数据
        logger.exception("学生版数据生成失败：course_id=%s", course_id)
        raise HTTPException(500, f"学生版数据生成失败：{exc}") from exc


@router.get("/{course_id}/student-html", response_class=HTMLResponse)
def student_page(course_id: str, db: Session = Depends(get_db)):
    """学生版单文件 HTML（离线、0 LLM、不落盘）：地图 / 微单元 / 自测 / 复习卡。"""
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    try:
        text = student_mode.render_student_html(db, course)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("学生版渲染失败：course_id=%s title=%s", course_id, course.title)
        raise HTTPException(500, f"学生版渲染失败：{exc}") from exc
    return HTMLResponse(content=text, media_type="text/html; charset=utf-8")


@router.get("/{item_id}/presenter-data", response_model=PresenterDataResponse)
def presenter_data(item_id: str, db: Session = Depends(get_db)):
    """讲师模式完整数据（只读）：每页每条的 raw/full/detail/source/tier + 节点树。"""
    item = db.get(ContentItem, item_id)
    if item is None or item.kind != "ppt":
        raise HTTPException(404, "课件不存在")
    course = db.get(Course, item.course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    try:
        return presenter_mode.build_data(db, course, item)
    except Exception as exc:  # noqa: BLE001
        logger.exception("讲师模式数据生成失败：item_id=%s", item_id)
        raise HTTPException(500, f"讲师模式数据生成失败：{exc}") from exc


@router.get("/{item_id}/presenter", response_class=HTMLResponse)
def presenter_page(item_id: str, db: Session = Depends(get_db)):
    """讲师模式单文件 HTML（离线、0 LLM、不落盘）：逐条揭示 / 展开 / 深浅 / 导图 / 讲者视图。"""
    item = db.get(ContentItem, item_id)
    if item is None or item.kind != "ppt":
        raise HTTPException(404, "课件不存在")
    course = db.get(Course, item.course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    try:
        text = presenter_mode.render_presenter(db, course, item)
    except Exception as exc:  # noqa: BLE001
        logger.exception("讲师模式渲染失败：item_id=%s title=%s", item_id, item.title)
        raise HTTPException(500, f"讲师模式渲染失败：{exc}") from exc
    return HTMLResponse(content=text, media_type="text/html; charset=utf-8")


def _servable(path: str) -> str | None:
    """把存储的真实路径换算为 /ppt-files 挂载下的可下载 URL。"""
    if not path:
        return None
    try:
        p = Path(path).resolve()
        base = get_settings().ppt_work_dir.resolve()
        return "/ppt-files/" + p.relative_to(base).as_posix()
    except (ValueError, OSError):
        return None
