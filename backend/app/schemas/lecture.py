"""完整讲义（Lecture）生成与编辑模式。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class LectureGenerateRequest(BaseModel):
    course_id: str
    node_id: str
    tone: str = Field(default="正式", description="语气风格：正式/轻松/幽默")
    item_id: str | None = Field(default=None, description="提供则原地替换该讲义")
    allow_overwrite_locked: bool = Field(
        default=False,
        description="任务 91：目标讲义已「审核通过/人工编辑」加锁时默认拒绝（409）；"
                    "置 true 表示确认覆盖——覆盖后该篇的 locked/locked_reason 会被清掉，需重新审核",
    )


class LectureBatchGenerateRequest(BaseModel):
    """一次性生成整门课完整讲义（M3 起收进「高级选项」）。

    未设置样板锚时必须显式传 ignore_style_anchor=True（=「忽略样板，一次性生成整门课」），
    否则接口拒绝，引导用户先走「样板 → 按章」三段式。
    """

    course_id: str
    tone: str = Field(default="正式", description="语气风格：正式/轻松/幽默")
    ignore_style_anchor: bool = Field(
        default=False, description="忽略样板风格锚，确认一次性生成整门课")
    force: bool = Field(
        default=False, description="强制重生成：忽略输入指纹，输入没变也重做")
    allow_anchor_overwrite: bool = Field(
        default=False, description="样板节没加锁时，显式确认「本次批量会覆盖它」")


class LectureChapterGenerateRequest(BaseModel):
    """M3 按章批量：只生成该章节下的知识点，一章一个任务。"""

    course_id: str
    chapter_node_id: str
    tone: str = Field(default="正式", description="语气风格：正式/轻松/幽默")
    force: bool = Field(
        default=False, description="强制重生成：忽略输入指纹，输入没变也重做")
    allow_anchor_overwrite: bool = Field(
        default=False, description="样板节没加锁时，显式确认「本次批量会覆盖它」")


class StyleAnchorRequest(BaseModel):
    """M3 样板锚：给 source_item_id 就从该讲义抽取；给的字段则覆盖抽取结果（UI 可编辑）。"""

    course_id: str
    source_item_id: str | None = Field(default=None, description="样板讲义条目 id")
    tone: str | None = None
    depth: str | None = None
    figure_style: str | None = None
    quiz_difficulty: str | None = None
    terminology: list[str] | None = None


class StyleAnchorLockRequest(BaseModel):
    """样板节加锁 / 解锁（P4-1：「以这篇为样板」会自动加锁，这里给一键切换）。"""

    course_id: str
    locked: bool = True


class LectureLockRequest(BaseModel):
    """M3 节点加锁：锁定后按章/整课批量都跳过它。"""

    locked: bool = True
    reason: str = ""


class LectureEditRequest(BaseModel):
    markdown: str = Field(description="讲义 Markdown 全文")


class LessonPlanGenerateRequest(BaseModel):
    """教案骨架生成（任务 49）：0 LLM，从大纲三要素 + 讲义正文派生。"""

    node_ids: list[str] = Field(default_factory=list,
                                description="只生成这些知识点；空表示整门课（跳过章节点）")


class LectureQcRegenerateRequest(BaseModel):
    """「按质检建议重生成」：把 meta.qc 里的 warn/fail 逐条注入提示词后重做本页。"""

    tone: str | None = Field(default=None, description="语气；缺省沿用上一版讲义")


class CourseQcRequest(BaseModel):
    """课程级质检汇总参数（整课质量报告页签用）。"""

    worst_n: int = Field(default=5, ge=1, le=20, description="返回最差的 N 篇")
    refresh: bool = Field(default=False, description="忽略缓存，按当前正文全部复检")
