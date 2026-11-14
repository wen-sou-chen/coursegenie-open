"""规划模块模式：大纲生成入参与输出。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class PlanRequest(BaseModel):
    course_id: str
    audience: str = Field(default="", max_length=255, description="目标受众/给谁看；为空时沿用课程已有受众")
    goal: str = Field(min_length=3, description="课程目标，如：让高中生理解光合作用")
    num_chapters: int = Field(default=4, ge=1, le=12)
    style: str = Field(default="正式", description="内容风格：正式/轻松/幽默")
    semantic: bool = Field(default=False, description=( "任务 70 §B3：语义等价判定是否开启；"
                                                        "默认 False（只做字面比对）；开启也只走本地词面"
                                                        "相似度，不调 LLM、不产生用量"))


class PlanApproveRequest(BaseModel):
    """大纲「通过 / 撤销通过」：通过后大纲被锁定，作为图表与内容的唯一生成依据。"""
    course_id: str
    approved: bool = True


class PlanNodeCreate(BaseModel):
    """新增大纲节点（P3-3）：顶层加章，或指定父章在指定位置加一条知识点。"""
    course_id: str
    parent_id: str | None = Field(default=None, description="父章 id；留空=新增一章")
    position: int | None = Field(default=None, ge=1,
                                 description="插到同级第几位（1 起）；留空=追加到末尾")
    title: str = Field(min_length=1, max_length=255)
    node_type: str = Field(default="", description="chapter/point；留空按父节点推断")
    objective: str = ""
    teaching_strategy: str = ""
    assessment: str = ""


class PlanRestoreRequest(BaseModel):
    confirm: bool = Field(
        default=False,
        description="回滚会删掉现版大纲上绑定的讲义与习题；未确认时返回 409 + 影响面。",
    )


class PlanNodeMove(BaseModel):
    """移动大纲节点（P3-3）：同章上/下移，或跨章移动 / 升降层级。"""
    direction: str | None = Field(default=None, description="up/down：同组相邻移动")
    reparent: bool = Field(default=False, description="true=跨章移动/升降层级")
    to_parent_id: str | None = Field(default=None,
                                     description="目标章 id；reparent=true 且为 null=升到顶层")
    position: int | None = Field(default=None, ge=1)
    confirm: bool = Field(default=False, description="二次确认：影响面非空时必须为 true")


class NodeIn(BaseModel):
    key: str
    title: str
    node_type: str = "section"
    objective: str = ""
    teaching_strategy: str = ""
    assessment: str = ""
    prerequisites: list[str] = []
    points: list["NodeIn"] = []


class OutlineOut(BaseModel):
    id: str
    course_id: str
    title: str
    goal: str
    content: dict
    nodes: list[NodeIn] = []
