"""素材路由：教师上传/粘贴原始资料；生成时自动作为依据注入。"""
from __future__ import annotations

import logging
import shutil
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.core.config import get_settings
from app.db.session import SessionLocal, get_db
from app.models.course import Course, GenerationJob, SourceMaterial
from app.services import material as material_service
from app.services import link_import
from app.services.jobs import acquire_scope, claim_job_exclusive, release_scope
from app.utils.uploads import UploadTooLarge, read_upload, read_upload_to_path

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/material", tags=["material"], dependencies=[Depends(require_api_key)])


class MaterialPasteRequest(BaseModel):
    course_id: str
    title: str = Field(default="", max_length=255)
    text: str = Field(min_length=1)


class MaterialGenerateRequest(BaseModel):
    course_id: str
    directions: list[str] = Field(min_length=1, description="补充方向：concept/examples/learner/difficulty/background/custom")
    custom_instruction: str = Field(default="", max_length=500, description="自定义方向的额外要求")


class MaterialUpdateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    text: str | None = Field(default=None, min_length=1)



class MaterialLinkRequest(BaseModel):
    """**从链接导入**（任务 118）的请求体：YouTube 视频或播放列表链接。"""

    course_id: str
    url: str = Field(min_length=1, max_length=2048, description="YouTube 视频或播放列表链接")
    title: str = Field(default="", max_length=255, description="留空就用 YouTube 上的标题")
    cookies: str = Field(default="", max_length=1000,
                         description="可选：cookies.txt 的完整路径（只按路径交给 yt-dlp，"
                                     "不复制、不落库、不进日志）")


class MaterialLinkRetryRequest(BaseModel):
    """「重新导入」的请求体。cookie 路径**不落库** ⇒ 重试时再给一次（留空则用 .env 的 YTDLP_COOKIES）。"""

    cookies: str = Field(default="", max_length=1000)

def _course_or_404(db: Session, course_id: str) -> Course:
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    return course


def _out(m) -> dict:
    cleaned = material_service.cleaning_info(m)
    detected = material_service.detection_info(m)
    grounding = material_service.grounding_of(m)
    return {
        "id": m.id,
        "title": m.title,
        "kind": m.kind,
        "file_rel": m.file_rel,
        "chars": m.meta.get("chars", len(m.text or "")),
        "raw_chars": m.meta.get("raw_chars", m.meta.get("chars", len(m.text or ""))),
        "truncated": bool(m.meta.get("truncated")),
        "pages": material_service.pages_of(m),
        "ext": m.meta.get("ext", ""),
        "original": m.meta.get("original", ""),
        "preview": (m.text or "")[:200],
        "source": m.meta.get("source", ""),
        # 任务 69 §B1.5.1：来源类型（人工上传 / 粘贴文本(疑似 AI 产物) / AI 补充），
        # must_clean = 用户口径里「必须干净」的那两类（AI 补充 + 粘贴件）
        "origin": material_service.origin_of(m),
        "origin_label": material_service.origin_label(m),
        "must_clean": material_service.must_clean(m),
        # 任务 69 §B1.5：入库卫生 —— 已剥离的对话外壳/页眉行数（素材行显式提示）
        "cleaned_lines": cleaned["lines"],
        "cleaned_rules": cleaned["rules"],
        # 任务 72 §B1：库里的正文**到底改没改**（三态文案全靠它区分）
        #   True  ⇒ 入库时已剥 / 老师点过「一键清理」⇒「已清理 N 行（库内正文已更新）」
        #   False ⇒ 只记了行数、库内保留原文（老数据）⇒「出库时会剥离 N 行（库内保留原文）」
        "shell_stripped": bool((m.meta or {}).get("shell_stripped")),
        # 任务 69 §B1.5.1：上传原件**只提示**检测到的外壳（默认不动原稿，可一键清理）
        "detected_lines": detected["lines"],
        "detected_rules": detected["rules"],
        # 任务 69 §B1.5.3：AI 补充的「有来处 / 无来处」（无来处的条目界面标红 + 人工确认）
        "grounded": grounding["grounded"],
        "grounded_count": grounding["grounded_count"],
        "ungrounded": grounding["ungrounded"],
        "ungrounded_count": grounding["ungrounded_count"],
        # 任务 54：图片素材的 OCR 元信息（素材行 / 全文弹窗显示）
        "ocr": (m.meta or {}).get("ocr") or None,
        # 任务 54：图片素材的 OCR 元信息（素材行 / 全文弹窗显示）
        "ocr": (m.meta or {}).get("ocr") or None,
        # 任务 102：音视频素材的转写元信息（引擎/模型/时长/段数/置信度/状态/跳过原因）
        "asr": (m.meta or {}).get("asr") or None,
        # 任务 118：链接导入的元信息（用了哪条路 / 语言 / 时长 / 来源 URL / 抓取时间 / 失败原因）
        "link": (m.meta or {}).get("link") or None,
    }


_ASR_JOB_PREFIX = "material_asr:"
"""转写作业的 job_type 前缀（**带素材 id**）⇒ 互斥作用域按**素材**划分：
同一门课里几条视频可以各转各的（老师一次选多个文件上传不会被 409 挡掉），
同一条素材不会被重复转两遍。见 ``app/services/jobs.scope_for``。"""
_PROGRESS_MIN_SECONDS = 0.7
"""进度写库的最小间隔（秒）：whisper 每转完一段都回调，太密会把库写爆。"""


def _enqueue_transcribe(db: Session, course_id: str, material_id: str,
                        background: BackgroundTasks) -> str | None:
    """用**既有作业机制**起一条后台转写作业，返回 ``job_id``（已有在跑的就返回它）。

    任务 102 §A4：1 小时视频在 CPU 上要 10–40 分钟 ⇒ 上传请求只负责「落素材行 + 入队」，
    进度写进 ``GET /content/jobs/{job_id}`` 的 ``result.progress``（= 已转写时长 ÷ 总时长）。
    失败原因写 ``job.error``，素材行与原件都不动（可点「重新转写」重试）。
    """
    job_type = f"{_ASR_JOB_PREFIX}{material_id}"
    running = (
        db.query(GenerationJob.id)
        .filter(GenerationJob.course_id == course_id,
                GenerationJob.job_type == job_type,
                GenerationJob.status.in_(("pending", "running")))
        .first()
    )
    if running is not None:
        return str(running[0])
    if not acquire_scope(course_id, job_type):
        return None

    def _run() -> None:
        task_db = SessionLocal()
        last = {"at": 0.0}
        try:
            job_row = task_db.get(GenerationJob, job_id)
            if not claim_job_exclusive(task_db, job_row):
                return
            row = task_db.get(SourceMaterial, material_id)
            if row is None:
                if job_row is not None:
                    job_row.status = "failed"
                    job_row.error = "素材不存在（可能已被删除）"
                    task_db.commit()
                return

            def _progress(done_sec: float, total_sec: float) -> None:
                now = time.time()
                ratio = round(done_sec / total_sec, 4) if total_sec else 0.0
                if ratio < 1.0 and now - last["at"] < _PROGRESS_MIN_SECONDS:
                    return
                last["at"] = now
                if job_row is not None:
                    job_row.result = {"material_id": material_id, "progress": ratio,
                                      "done_sec": round(done_sec, 1),
                                      "total_sec": round(total_sec, 1)}
                    task_db.commit()

            outcome = material_service.transcribe_material(task_db, row, on_progress=_progress)
            job_row = task_db.get(GenerationJob, job_id)
            if job_row is not None:
                done = outcome.get("status") == "done"
                previous = job_row.result or {}
                job_row.result = {**outcome,
                                  "progress": 1.0 if done else float(previous.get("progress") or 0.0)}
                if done:
                    job_row.status = "success"
                else:
                    job_row.status = "failed"
                    job_row.error = str(outcome.get("skipped_reason") or "转写失败")
                task_db.commit()
        except Exception as exc:  # noqa: BLE001
            job_row = task_db.get(GenerationJob, job_id)
            if job_row is not None:
                job_row.status = "failed"
                job_row.error = str(exc)
                task_db.commit()
        finally:
            release_scope(course_id, job_type)
            task_db.close()

    job = GenerationJob(course_id=course_id, job_type=job_type, status="running")
    db.add(job)
    db.commit()
    db.refresh(job)
    job_id = job.id
    try:
        background.add_task(_run)
    except Exception:
        release_scope(course_id, job_type)
        raise
    return job_id


def _transcribe_payload(db: Session, row: SourceMaterial,
                        background: BackgroundTasks) -> dict:
    """上传路径的转写入队：只在 ``meta.asr.status == "pending"`` 时起作业。

    没装组件时入库那条路已经写成 ``failed`` + 跳过原因（照任务 54 OCR 的降级口径）
    ⇒ 这里就不入队了；素材照旧可用，装好组件后在素材行点「重新转写」。
    """
    payload = _out(row)
    job_id = None
    if str((payload.get("asr") or {}).get("status") or "") == "pending":
        job_id = _enqueue_transcribe(db, row.course_id, row.id, background)
    return {**payload, "job_id": job_id}


async def _upload_av(course: Course, filename: str, file: UploadFile, title: str | None,
                     background: BackgroundTasks, db: Session) -> dict:
    """音视频上传：**分块落盘** → 先落素材行（pending）+ 原件 → 起后台转写 → 立刻返回。

    任务 102 §A4：超 2GB / 超 3 小时**明确拒收**（400 + 人话）；损坏、没音轨这类
    **不拒收**（素材行与原件照落，失败原因写进 ``meta.asr`` + 作业 ``error``，可重试）。
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="cg_av_"))
    tmp = tmp_dir / (Path(filename).name or "material.mp4")
    try:
        try:
            size = await read_upload_to_path(file, tmp, material_service.ASR_MAX_BYTES)
        except UploadTooLarge as exc:
            raise HTTPException(400, str(exc)) from exc
        if not size:
            raise HTTPException(400, "空文件")
        try:
            row = material_service.add_material_file(db, course, filename, b"", title,
                                                     source_path=tmp)
        except ValueError as exc:          # 时长超 3 小时（只读文件头就能判）
            raise HTTPException(400, str(exc)) from exc
        payload = _transcribe_payload(db, row, background)
        logger.info("音视频素材入库：material=%s ext=%s %.1fMB job=%s 状态=%s",
                    row.id, material_service.ext_of(filename), size / 1024 / 1024,
                    payload.get("job_id"), (payload.get("asr") or {}).get("status"))
        return {**payload, "size_bytes": size}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)



_LINK_JOB_PREFIX = "material_link:"
"""链接导入作业的 job_type 前缀（**带素材 id**）⇒ 互斥作用域按**素材**划分：同一条链接不会被
重复抓两遍，同课程里另一个链接素材照常各抓各的（见 ``app/services/jobs.scope_for``）。"""
_LINK_PROGRESS_MIN_SECONDS = 0.7
"""进度写库的最小间隔（秒）：播放列表一条一条抓，太密会把库写爆。"""


def _enqueue_link_import(db: Session, course_id: str, material_id: str,
                         background: BackgroundTasks, *, cookies: str | None = None) -> str | None:
    """用**既有作业机制**起一条后台抓取作业，返回 ``job_id``（已有在跑的就返回它）。

    进度走 ``GET /content/jobs/{job_id}`` 的 ``result.progress``（= 抓成的条数 ÷ 总条数），
    失败原因写 ``job.error``，**素材行与失败原因都留在库里**（可点「重新导入」重试）。
    """
    job_type = f"{_LINK_JOB_PREFIX}{material_id}"
    running = (
        db.query(GenerationJob.id)
        .filter(GenerationJob.course_id == course_id,
                GenerationJob.job_type == job_type,
                GenerationJob.status.in_(("pending", "running")))
        .first()
    )
    if running is not None:
        return str(running[0])
    if not acquire_scope(course_id, job_type):
        return None

    def _run() -> None:
        task_db = SessionLocal()
        last = {"at": 0.0}
        try:
            job_row = task_db.get(GenerationJob, job_id)
            if not claim_job_exclusive(task_db, job_row):
                return
            row = task_db.get(SourceMaterial, material_id)
            if row is None:
                if job_row is not None:
                    job_row.status = "failed"
                    job_row.error = "素材不存在（可能已被删除）"
                    task_db.commit()
                return
            link = dict((row.meta or {}).get("link") or {})
            parsed = link_import.parse_link(str(link.get("url") or ""))

            def _progress(done: int, total: int) -> None:
                now = time.time()
                ratio = round(done / total, 4) if total else 0.0
                if ratio < 1.0 and now - last["at"] < _LINK_PROGRESS_MIN_SECONDS:
                    return
                last["at"] = now
                if job_row is not None:
                    job_row.result = {"material_id": material_id, "progress": ratio,
                                      "items_done": done, "items_total": total}
                    task_db.commit()

            outcome = link_import.run_link_import(
                task_db, row, parsed=parsed,
                opts=link_import.FetchOptions(cookies=cookies or None),
                on_progress=_progress)
            job_row = task_db.get(GenerationJob, job_id)
            if job_row is not None:
                ok = outcome.get("status") == "done"
                previous = job_row.result or {}
                job_row.result = {**outcome,
                                  "progress": 1.0 if ok else float(previous.get("progress") or 0.0)}
                if ok:
                    job_row.status = "success"
                else:
                    job_row.status = "failed"
                    job_row.error = str(outcome.get("skipped_reason") or "抓取失败")
                task_db.commit()
        except Exception as exc:  # noqa: BLE001
            job_row = task_db.get(GenerationJob, job_id)
            if job_row is not None:
                job_row.status = "failed"
                job_row.error = str(exc)
                task_db.commit()
        finally:
            release_scope(course_id, job_type)
            task_db.close()

    job = GenerationJob(course_id=course_id, job_type=job_type, status="running")
    db.add(job)
    db.commit()
    db.refresh(job)
    job_id = job.id
    try:
        background.add_task(_run)
    except Exception:
        release_scope(course_id, job_type)
        raise
    return job_id

@router.post("/upload")
async def upload(
    background: BackgroundTasks,
    course_id: str = Form(...),
    title: str | None = Form(default=None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """上传原始资料（md/txt/csv/docx/pdf/pptx/png/jpg + 任务 102 的音视频），自动提取正文。

    音视频走 :func:`_upload_av`：**本机转写**（faster-whisper，可选依赖），上传请求
    只负责落素材行 + 入队，**立刻返回** ``job_id``。
    """
    course = _course_or_404(db, course_id)
    filename = file.filename or "material.txt"
    if material_service.ext_of(filename) in material_service.AV_EXT:
        return await _upload_av(course, filename, file, title, background, db)
    try:
        data = await read_upload(file, get_settings().max_upload_mb * 1024 * 1024)
    except UploadTooLarge as exc:
        raise HTTPException(413, str(exc)) from exc
    if not data:
        raise HTTPException(400, "空文件")
    try:
        row = material_service.add_material_file(db, course, filename, data, title)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _out(row)


@router.post("/paste")
def paste(payload: MaterialPasteRequest, db: Session = Depends(get_db)):
    """直接粘贴原始资料文本。"""
    course = _course_or_404(db, payload.course_id)
    try:
        row = material_service.add_material_text(db, course, payload.title, payload.text)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _out(row)


@router.post("/generate")
def generate(payload: MaterialGenerateRequest, db: Session = Depends(get_db)):
    """AI 按方向补充备课素材（source=ai），返回列表供教师编辑/删除。"""
    course = _course_or_404(db, payload.course_id)
    try:
        rows = material_service.generate_materials(
            db, course, payload.directions, payload.custom_instruction
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return [_out(row) for row in rows]


@router.get("/text/{material_id}")
def text_detail(material_id: str, db: Session = Depends(get_db)):
    """读取文本素材全文（编辑弹窗用）。"""
    try:
        row = material_service.get_material_text(db, material_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"id": row.id, "title": row.title, "text": row.text or ""}


@router.get("/full/{material_id}")
def full_text(material_id: str, offset: int = 0, limit: int = 2000, numbered: bool = False,
              db: Session = Depends(get_db)):
    """读任意素材（含 PPT/PDF/Word 等文件型）的解析全文，分页。

    P2-1：原先只有 ``GET /material/text/{id}`` 且对文件型素材报 404，
    老师无法核对「PPT 里的字到底读进去多少」。

    ``numbered=true``（任务 69 §B1.5.3）：按**段落**分页并带段号 —— AI 补充素材卡片上
    「第 N 段」的链接点进来就能定位到同一段（口径与注入/素材页一致）。
    """
    try:
        return material_service.get_material_full(db, material_id, offset=offset, limit=limit,
                                                  numbered=numbered)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/{course_id}")
def list_materials(course_id: str, db: Session = Depends(get_db)):
    """列素材 —— **只读**。

    任务 72 §B2：原先这里会顺手跑一次老数据回填（那一步会提交事务），
    于是「打开素材页」这一次 GET 就会改真库 mtime（实测 2026-09-21 13:29:04 破过
    「GET 不改状态」）。新数据在入库时就算好了 ``pages`` / ``origin`` / 外壳行数，
    老数据走显式入口 :func:`backfill_materials`（素材页「补齐素材信息」按钮）。
    """
    rows = material_service.list_materials(db, course_id)
    return [_out(m) for m in rows]


@router.post("/{course_id}/backfill")
def backfill_materials(course_id: str, db: Session = Depends(get_db)):
    """**显式**补齐老素材的 ``pages`` / ``origin`` / 外壳行数（只写 meta，正文一个字不动）。

    任务 72 §B2：「读时回填」不再静默发生 —— 由素材页「补齐素材信息」按钮触发，
    返回被补齐的份数（界面提示「已补齐 N 份素材的页数/来源信息」），并写一条 INFO 日志。
    """
    _course_or_404(db, course_id)
    rows = material_service.list_materials(db, course_id)
    changed = material_service.backfill_material_meta(db, rows)
    logger.info("补齐素材信息入口被调用：course=%s 回填 %d/%d 份（pages/origin/外壳行数，正文未动）",
                course_id, changed, len(rows))
    return {"ok": True, "backfilled": changed, "total": len(rows)}


@router.patch("/{material_id}")
def update(material_id: str, payload: MaterialUpdateRequest, db: Session = Depends(get_db)):
    """编辑文本素材（标题/正文）。"""
    try:
        row = material_service.update_material_text(
            db, material_id,
            title=payload.title if payload.title is not None else None,
            text=payload.text if payload.text is not None else None,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _out(row)


@router.post("/{material_id}/clean")
def clean(material_id: str, db: Session = Depends(get_db)):
    """一键清理页眉页脚 / 对话外壳（用户 2026-09-21 口径：默认不动原稿，点了才落库）。

    适用**上传原件**，以及**老数据**里 ``shell_stripped`` 还没写上的 AI 补充 / 粘贴件
    （界面在「出库时会剥离 N 行（库内保留原文）」那一态也放这个按钮，任务 72 §B1）；
    入库时就已经剥过的（``shell_stripped=True``，含新数据）这里会报 400。
    """
    try:
        row, removed = material_service.clean_material(db, material_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "cleaned_lines": removed, "material": _out(row)}


@router.delete("/{material_id}")
def delete(material_id: str, db: Session = Depends(get_db)):
    try:
        material_service.delete_material(db, material_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {"ok": True, "id": material_id}


@router.post("/{material_id}/transcribe")
def transcribe(material_id: str, background: BackgroundTasks, db: Session = Depends(get_db)):
    """**显式**「重新转写」入口（任务 102）：转写失败 / 跳过 / 想换模型后重跑。

    和 ``POST /{course_id}/backfill`` 一样是显式动作：**GET 列表不会顺手回填**。
    引擎没装 ⇒ 400 + 人话（素材行与原件本来就在，装好后重试即可）；否则起一条后台作业
    并返回 ``job_id``（进度看 ``GET /content/jobs/{job_id}``）。
    """
    row = db.get(SourceMaterial, material_id)
    if row is None:
        raise HTTPException(404, "素材不存在")
    ext = str((row.meta or {}).get("ext") or "") \
        or material_service.ext_of(str((row.meta or {}).get("original") or ""))
    # 任务 118：链接导入的素材**没有本机原件**（音频是临时下载、字幕是抓来的）⇒ 指回「重新导入」
    if (row.meta or {}).get("link"):
        raise HTTPException(400, "这条素材是「从链接导入」的：原件不在本机，请点「重新导入」重跑抓取")
    if ext not in material_service.AV_EXT:
        raise HTTPException(400, "这条素材不是音视频，没有可转写的声音")
    if not material_service.asr_available():
        raise HTTPException(400, material_service.ASR_SKIP_REASON)
    job_id = _enqueue_transcribe(db, row.course_id, row.id, background)
    logger.info("重新转写入口被调用：material=%s ext=%s job=%s", material_id, ext, job_id)
    return {"ok": True, **_out(row), "job_id": job_id}


@router.post("/import-link")
def import_link(payload: MaterialLinkRequest, background: BackgroundTasks,
                db: Session = Depends(get_db)):
    """**从链接导入**（任务 118）：粘贴 YouTube 视频 / 播放列表链接 → 落一条素材行 → 后台抓取。

    抓取顺序**写死**（规格 §二.2）：① 抓字幕（en / zh-Hans 等）→ ② 没字幕才下音频走**既有本地
    ASR** → ③ 都不行就明确失败原因，素材行保留、可重试。全程 **0 LLM**（转写是本地模型）。

    缺 yt-dlp / 缺 JS 运行时 ⇒ **不 500、也不静默失败**：素材行照落，``meta.link`` 里写清
    缺什么、影响什么、怎么装（与 ``GET /api/health`` 的能力清单同一套话）。
    链接写错则不落行（400 + 人话）—— 输入错不算降级，脏链接别进库。
    """
    course = _course_or_404(db, payload.course_id)
    try:
        parsed = link_import.parse_link(payload.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    cookies = (payload.cookies or "").strip() or link_import.default_cookies()
    warning = ""
    js_runtime = link_import.js_runtime_probe()
    if not js_runtime["available"]:
        warning = js_runtime["reason"]
    if cookies:
        info = link_import.inspect_cookies(cookies)
        if not info["is_file"]:
            raise HTTPException(400, "cookies.txt 路径不对：" + (info["hint"] or "请给出完整路径"))
        if not info["youtube_domain"]:
            warning = "；".join(part for part in (warning, info["hint"]) if part)
    row = material_service.add_material_link(db, course, parsed["url"], parsed=parsed,
                                             title=payload.title)
    probe = link_import.ytdlp_probe()
    job_id = None
    result: dict | None = None
    if probe["available"]:
        job_id = _enqueue_link_import(db, course.id, row.id, background, cookies=cookies or None)
        message = "已开始抓取：先抓字幕，没字幕才下音频在本机转写（进度见素材行）"
    else:
        # 缺工具：**同步**把失败态写清（不发任何网络请求），界面照样能给「装好再点重新导入」
        result = link_import.run_link_import(db, row, parsed=parsed,
                                            opts=link_import.FetchOptions(cookies=cookies or None))
        message = (str(result.get("skipped_reason") or "没能抓取")
                   + "；怎么装：" + link_import.YTDLP_INSTALL)
        logger.info("从链接导入：缺 yt-dlp，已直接写失败态 material=%s", row.id)
    logger.info("从链接导入：material=%s kind=%s job=%s cookies=%s",
                row.id, parsed["kind"], job_id, bool(cookies))
    return {**_out(row), "job_id": job_id, "result": result, "message": message,
            "warning": warning}


@router.post("/{material_id}/import-link")
def reimport_link(material_id: str, background: BackgroundTasks,
                  payload: MaterialLinkRetryRequest | None = None,
                  db: Session = Depends(get_db)):
    """**重新导入**已落库的链接素材（任务 118）：失败 / 装好依赖 / 换了新 cookie 之后重跑。

    链接存在 ``meta.link.url`` 里（cookie 路径**不存**，重试时可以再给一次，或用 ``.env``
    的 ``YTDLP_COOKIES``）。转写引擎缺组件这类降级与首次导入完全同一套口径。
    """
    row = db.get(SourceMaterial, material_id)
    if row is None:
        raise HTTPException(404, "素材不存在")
    link = dict((row.meta or {}).get("link") or {})
    if not link.get("url"):
        raise HTTPException(400, "这条素材不是「从链接导入」来的，没有可重跑的链接")
    try:
        parsed = link_import.parse_link(str(link["url"]))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    probe = link_import.ytdlp_probe()
    if not probe["available"]:
        raise HTTPException(400, probe["reason"])
    cookies = ((payload.cookies if payload else "") or "").strip() or link_import.default_cookies()
    job_id = _enqueue_link_import(db, row.course_id, row.id, background, cookies=cookies or None)
    logger.info("重新导入链接素材：material=%s job=%s cookies=%s", material_id, job_id, bool(cookies))
    return {"ok": True, **_out(row), "job_id": job_id}
