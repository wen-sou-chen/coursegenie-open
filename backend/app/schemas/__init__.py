"""Pydantic 模式聚合导出。"""
from app.schemas.common import ApiResponse, JobResult
from app.schemas.course import CourseCreate, CourseListItem, CourseOut, CourseUpdate
from app.schemas.plan import NodeIn, OutlineOut, PlanRequest
from app.schemas.content import ContentGenerateRequest, ContentItemOut
from app.schemas.quiz import QuizGenerateRequest, QuizOut
from app.schemas.ppt import FlowchartGenerateRequest, ImageGenerateRequest, PptGenerateRequest

__all__ = [
    "ApiResponse",
    "JobResult",
    "CourseCreate",
    "CourseOut",
    "CourseListItem",
    "CourseUpdate",
    "NodeIn",
    "OutlineOut",
    "PlanRequest",
    "ContentGenerateRequest",
    "ContentItemOut",
    "QuizGenerateRequest",
    "QuizOut",
    "FlowchartGenerateRequest",
    "ImageGenerateRequest",
    "PptGenerateRequest",
]
