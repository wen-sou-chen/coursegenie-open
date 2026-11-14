"""PPT 生成模式：整课（course）与每章（chapter）。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PptGenerateRequest(BaseModel):
    course_id: str
    scope: Literal["course", "chapter"] = Field(default="course", description="整课 / 每章")
    chapter_node_id: str | None = Field(default=None, description="scope=chapter 时的章节点 id")
    theme: str = Field(default="", description=(
        "PPT 主题：clean / professional / handdrawn / modern / navy。"
        "留空 = 走本课设置 course.meta.ppt_theme，本课没设过再用后端 PPT_THEME 配置"
        "（任务 145 起：课程设置比配置优先）"))
    engine: Literal["python-pptx", "dashi"] = Field(
        default="python-pptx", description="v1 默认 python-pptx；dashi 失败会显式回退并记录")
    pages_per_chapter: int = Field(default=2, ge=1, le=6, description="保留参数（v1 未使用）")
    item_id: str | None = Field(default=None, description="提供则原地重做该 PPT 条目")


class PptThemeRequest(BaseModel):
    """设置**本课主题**（课程级设置，落在 ``course.meta.ppt_theme``；任务 145）。

    只改外观、不改内容：已生成的课件不会变，必须重新生成才生效。
    """

    course_id: str
    theme: str = Field(default="", description=(
        "主题 id：clean / professional / handdrawn / modern / navy（中文名与 themeNN 别名也认，"
        "一律存规范 id）；非法值 400 并给出合法值清单；传空串 = 清除本课设置（回到后端配置）"))


class PptBatchRequest(BaseModel):
    """「补齐全部章节课件」：逐章串行，默认只补缺（已生成的章跳过）。"""

    course_id: str
    redo: bool = Field(default=False, description="勾选后连已生成的章也重做；默认 false = 只补缺")
    theme: str = Field(default="", description=(
        "PPT 主题：clean / professional / handdrawn / modern / navy。"
        "留空 = 与单章生成走同一条优先级（本课设置 course.meta.ppt_theme > 后端 PPT_THEME 配置）"))
    engine: Literal["python-pptx", "dashi"] = Field(
        default="python-pptx", description="沿用单章生成的引擎口径，dashi 失败会显式回退并记录")


class FlowchartGenerateRequest(BaseModel):
    course_id: str
    description: str = Field(min_length=3, description="自然语言描述，如：光合作用的过程")
    kind: str = Field(default="flow", pattern="^(flow|loop|branch|hierarchy)$")
    item_id: str | None = Field(default=None, description="提供则原地重做该流程图条目")
    node_id: str | None = Field(default=None, description="绑定的章节/知识点节点：归属该节点，随章节页导出")


class ImageGenerateRequest(BaseModel):
    course_id: str
    prompt: str = Field(default="", description="图像描述（可留空：留空则按 node_id 所指章节或整门大纲自动构图）")
    size: str = Field(default="1024x1024", pattern="^(512x512|1024x1024|1024x1792)$")
    item_id: str | None = Field(default=None, description="提供则原地重做该图片条目")
    node_id: str | None = Field(default=None, description="绑定的章节/知识点节点：留空提示词时按该节点大纲构图")
    text_lang: Literal["none", "zh", "en"] = Field(
        default="none", description="图中文字策略：none 无文字（默认）/ zh 中文 / en 英文"
    )
    overlay_text: str = Field(default="", max_length=120, description="叠加到图上的文字；留空时取章节标题")
    overlay_pos: Literal["top", "center", "bottom"] = Field(default="top", description="叠加文字位置")
    model: str | None = Field(default=None, max_length=120, description="图像模型覆盖（默认用后端 IMAGE_MODEL）")


class PresenterItem(BaseModel):
    """讲师模式的一条要点：page 上的短句 + 讲义里的完整句 / 上下文 / 来源。"""

    raw: str
    full: str
    detail: str = ""
    source: str = ""
    tier: Literal["core", "expand", "case"]
    can_expand: bool = False


class PresenterImage(BaseModel):
    src: str
    name: str = ""
    width: int = 0
    height: int = 0


class PresenterPage(BaseModel):
    index: int
    title: str
    base_title: str
    layout: str = ""
    is_structural: bool
    is_cuttable: bool
    notes: str = ""
    notes_internal: str = ""
    images: list[PresenterImage] = Field(default_factory=list)
    items: list[PresenterItem] = Field(default_factory=list)


class PresenterNodePage(BaseModel):
    index: int
    title: str
    is_structural: bool


class PresenterNode(BaseModel):
    title: str
    base_title: str
    page_indexes: list[int] = Field(default_factory=list)
    page_count: int
    is_structural: bool
    is_cuttable: bool
    pages: list[PresenterNodePage] = Field(default_factory=list)
    node_key: str | None = None
    node_id: str | None = None


class PresenterChapter(BaseModel):
    key: str = ""
    title: str = ""
    node_id: str | None = None
    deck_id: str
    deck_title: str
    scope: str
    nodes: list[PresenterNode] = Field(default_factory=list)


class PptOutlineResponse(BaseModel):
    """``GET /api/ppt/{course_id}/outline``：chapter → point(节点) → pages[]。"""

    course_id: str
    course_title: str
    deck_id: str
    deck_title: str
    chapter: PresenterChapter
    nodes: list[PresenterNode] = Field(default_factory=list)
    page_count: int
    source_error: str = ""


class PresenterDataResponse(PptOutlineResponse):
    """一份课件的讲师模式完整数据（HTML 内嵌的同一份 JSON）。"""

    pages: list[PresenterPage] = Field(default_factory=list)


class StudentQuizOption(BaseModel):
    """学生版自测选项（原文来自 QuizItem.options）。"""

    text: str


class StudentQuiz(BaseModel):
    """学生版一道即时自测；答案与解析只以 base64 传输，初始 HTML 不出现原文。"""

    type: Literal["choice", "fill", "short"]
    type_label: str
    stem: str
    options: list[StudentQuizOption] = Field(default_factory=list)
    correct_b64: str = ""
    answer_b64: str = ""
    explain_b64: str = ""


class StudentUnit(BaseModel):
    """一个微单元：一句话 / 要点 / 例子 / 边界 / 一道即时自测。"""

    title: str
    one_line: str
    points: list[str] = Field(default_factory=list)
    example: str = ""
    pitfall: str = ""
    quiz: StudentQuiz | None = None


class StudentPoint(BaseModel):
    """一个知识点下的 3 个微单元。"""

    title: str
    units: list[StudentUnit] = Field(default_factory=list)


class StudentChapter(BaseModel):
    """一章的学习地图。"""

    title: str
    minutes: int
    points: list[StudentPoint] = Field(default_factory=list)


class StudentCard(BaseModel):
    """一张复习卡：正面问、背面答。"""

    front: str
    back: str


class StudentDataResponse(BaseModel):
    """``GET /api/ppt/{course_id}/student``：整门课的学生版只读数据。"""

    course_id: str
    course: str
    chapters: list[StudentChapter] = Field(default_factory=list)
    cards: list[StudentCard] = Field(default_factory=list)
    total_units: int
    total_quizzes: int
