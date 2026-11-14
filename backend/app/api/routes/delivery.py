"""M5c 交付包接口（任务 31）：构建 / 列历史 / 下载。

交付包是「给客户交货」用的：一次构建 = 一个目录 + 同名 zip（详见 M5c-1 版 §A）。
构建本身 **0 次 LLM 调用**（纯本地渲染与复制），可重复执行。

任务 38 起，构建前会自检「笔记库 ↔ 网页端」一致性：不一致就先自动导出一次笔记，
导出后仍不一致则**拒绝出包**（``allow_inconsistent=true`` 可强制，验收单会标注不一致项数）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.db.session import get_db
from app.models.course import Course
from app.services import delivery as delivery_mod
from app.services import kb as kb_service

router = APIRouter(prefix="/delivery", dependencies=[Depends(require_api_key)])


class DeliveryBuildRequest(BaseModel):
    pdf: str = Field(default="auto", examples=["auto", "edge", "off"], description=(
        "讲义 PDF 引擎，**字符串枚举（不是布尔）**：auto=有 Edge 就打 PDF / edge=强制 / "
        "off=只交付 HTML。传 false 或 true 会被 422 拒绝（集成方常踩）"))
    external: bool | None = Field(default=None, description=(
        "交付模式（任务 48p）：**true = 对外（默认）** —— 客户包里不出现金额 / tokens / "
        "LLM 调用次数，改为一句中性说明；false = 内部复盘（写明用量与金额，只给自己看）。"
        "留空 = 用课程里已保存的模式（从没设过 = 对外）"))
    allow_inconsistent: bool = Field(default=False, description=(
        "默认 false：构建前先自检「笔记库 ↔ 网页端」一致性，不一致就先自动导出一次笔记；"
        "导出后仍不一致则拒绝出包（验收单数字会过期）。true = 照现状出包，并在验收单与 "
        "manifest 里标注不一致项数"))
    student_lectures: bool | None = Field(default=None, description=(
        "学员版讲义（任务 86，**0 LLM**）：true = 额外产出 `学员版讲义/`（与 `讲义/` 同结构，"
        "去答案与解析、正确项不再标 ✓，题目与作答区保留）；留空 = 按课程场景自动"
        "（含 self_study 才出）。**默认八件套结构不变**"))


def _course_or_404(db: Session, course_id: str) -> Course:
    course = db.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "课程不存在")
    return course


def _consistency_payload(db: Session, course: Course) -> dict:
    """生成前的「笔记库 ↔ 网页端」一致性状态（任务 38 的判定口径，一个字没改）。

    任务 48z：从这里抽成独立函数 —— 它要逐项比对整个笔记库（AI 课 150 项、实测
    **4.33 秒**），放在首屏会把交付包面板卡住，看起来像「交付包丢了」。
    """
    consistency = kb_service.verify_course_notes(db, course)
    return {
        "ok": bool(consistency.get("ok")),
        "reason": str(consistency.get("reason") or ""),
        "checked": int(consistency.get("checked") or 0),
        "inconsistent": int(consistency.get("inconsistent") or 0),
        "consistent": int(consistency.get("consistent") or 0),
        "vault_root": consistency.get("vault_root"),
        "hint": ("一致：出包不会自动改笔记库" if int(consistency.get("inconsistent") or 0) == 0
                 else "不一致：出包前会先自动导出一次笔记；仍不一致会拒绝出包"),
    }


@router.get("/course/{course_id}")
def read_course_deliveries(course_id: str, with_consistency: bool = False,
                           db: Session = Depends(get_db)):
    """清单预览（将包含哪些文件、哪些缺项）+ 历史交付包（新→旧）+ 交付模式。

    任务 48z：**首屏不再跑整库笔记一致性检查**（AI 课实测 4.33 秒、包子课 0.7 秒）——
    面板先渲染预览与历史，再**并行**请求 ``GET /course/{id}/consistency`` 补那一行提示。
    需要「一次请求拿齐」的老口径 ⇒ 传 ``?with_consistency=1``（返回的字段与改造前逐字一致）。
    """
    course = _course_or_404(db, course_id)
    return {
        "course_id": course.id,
        "course_title": course.title,
        "preview": delivery_mod.collect_plan(db, course),
        "delivery_mode": delivery_mod.delivery_mode(course),
        "builds": delivery_mod.list_deliveries(course.id),
        # 任务 38：生成前就能看到当前一致性状态（点了才发现数字不对就晚了）。
        # 任务 48z：默认 null + deferred 标记，由 /consistency 单独补（口径不变）。
        "consistency": _consistency_payload(db, course) if with_consistency else None,
        "consistency_deferred": not with_consistency,
    }


@router.get("/course/{course_id}/consistency")
def read_course_consistency(course_id: str, db: Session = Depends(get_db)):
    """生成前的笔记一致性（任务 48z 拆出的独立接口，判定口径同任务 38）。"""
    course = _course_or_404(db, course_id)
    return _consistency_payload(db, course)


@router.post("/course/{course_id}/build")
def build_course_delivery(course_id: str, payload: DeliveryBuildRequest | None = None,
                          db: Session = Depends(get_db)):
    """构建交付包（**同一份内容 = 同一次交付**：重复构建重建同一个包，跨分钟也复用）。"""
    course = _course_or_404(db, course_id)
    request = payload or DeliveryBuildRequest()
    try:
        return delivery_mod.build_delivery(db, course, pdf_engine=request.pdf,
                                           external=request.external,
                                           allow_inconsistent=request.allow_inconsistent,
                                           student_lectures=request.student_lectures)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/{build_id}")
def read_delivery(build_id: str):
    """某个交付包的 manifest（文件清单 + sha1 + 生成参数）。"""
    manifest = delivery_mod.read_manifest(build_id)
    if manifest is None:
        raise HTTPException(404, "交付包不存在")
    return manifest


@router.get("/{build_id}/download")
def download_delivery(build_id: str):
    path = delivery_mod.zip_path_of(build_id)
    if path is None:
        raise HTTPException(404, "交付包不存在")
    return FileResponse(path, media_type="application/zip", filename=f"{build_id}.zip")
