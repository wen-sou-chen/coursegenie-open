"""笔记系统/知识库模式。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class KbBindRequest(BaseModel):
    vault_root: str | None = Field(default=None, description="成果笔记目标根目录（Obsidian vault 或任意文件夹）")
    output_rel: str | None = Field(default=None, description="导出子目录（相对根），默认 课程/<课程名>")


class KbVerifyRequest(BaseModel):
    """一致性校验/修复的节点范围（不传=全课程）。"""
    node_ids: list[str] = Field(default_factory=list, description="只校验/修复这些知识点；空=全部")


class KbImportFlowchart(BaseModel):
    """用户自绘/粘贴的 Mermaid 流程图入库。"""
    course_id: str
    title: str = Field(default="", max_length=255)
    mermaid: str = Field(min_length=5, description="Mermaid 源码（flowchart 开头）")
    item_id: str | None = Field(default=None, description="提供则原地替换该流程图")
    node_id: str | None = Field(default=None, description="绑定的章节/知识点节点：归属该节点，随章节页导出")
