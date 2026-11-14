"""远程 MCP 专用工具：把文本资料直接提交为课程素材。

为什么单独放一个文件：连接器 1.0.5 的 9 个工具在审核窗口内冻结，
`server.py` 里的 `@mcp.tool` 数量不能变；远程服务启动时用动态注册追加第 10 个工具，
stdio 连接器（PyPI 1.0.5）的工具列表保持原样。
"""

from __future__ import annotations

from typing import Any

REMOTE_TEXT_TOOL = "upload_source_text"
WEB_UPLOAD_HINT = "文件请去 https://demo.example.com 网页版上传；远程 MCP 读不到客户电脑上的文件。"

_REGISTERED: set[int] = set()


def _upload_text(
    server: Any,
    course_title: str,
    text: str,
    source_type: str = "text",
    course_id: str = "",
) -> dict[str, Any]:
    """把一段文本提交为素材；依赖 server 模块的 ToolError / Settings / _request。"""
    try:
        kind = (source_type or "text").strip().lower()
        if kind != "text":
            raise server.ToolError(
                f"远程文本上传只支持 source_type=text，收到 {source_type!r}",
                WEB_UPLOAD_HINT,
            )
        body_text = (text or "").strip()
        if not body_text:
            raise server.ToolError("text 不能为空", "把资料正文粘贴到 text；" + WEB_UPLOAD_HINT)

        settings = server.Settings()
        target = (course_id or "").strip()
        title = (course_title or "").strip()
        created = False
        if target:
            course = server._course(settings, target)
            title = title or str((course or {}).get("title") or "")
        else:
            title = title or "文本资料课程"
            created_course = server._request(
                settings,
                "POST",
                "/course/",
                json_body={"title": title, "subject": "", "audience": "", "duration_minutes": 60},
            )
            target = str((created_course or {}).get("id") or "")
            if not target:
                raise server.ToolError("后端创建课程后没有返回 id", "请查看后端日志确认 /api/course/ 是否正常。")
            created = True

        result = server._request(
            settings,
            "POST",
            "/material/paste",
            json_body={"course_id": target, "title": title, "text": body_text},
        )
        body = result if isinstance(result, dict) else {}
        chars = body.get("chars") or body.get("raw_chars") or len(body_text)
        return {
            "ok": True,
            "task_id": target,
            "course_id": target,
            "course_created": created,
            "material": body,
            "summary": f"已提交文本素材（约 {chars} 字）" + ("；已新建课程" if created else ""),
            "next_step": "继续补资料，或调用 generate_outline 生成大纲（需要 goal）。",
        }
    except server.ToolError as exc:
        return exc.as_dict()


def register_remote_tools(mcp: Any) -> None:
    """在远程 MCP 上注册文本上传工具（重复调用幂等）。"""
    if id(mcp) in _REGISTERED:
        return
    from course_generator import server

    def upload_source_text(
        course_title: str,
        text: str,
        source_type: str = "text",
        course_id: str = "",
    ) -> dict[str, Any]:
        """把一段文本资料直接提交为课程素材（远程 MCP 专用）。

        Args:
            course_title: 新课程标题；传了 course_id 时可留空。
            text: 资料正文（纯文本）。不能为空。
            source_type: 目前只支持 text。
            course_id: 已有课程 id；留空则自动新建课程。
        """
        return _upload_text(server, course_title, text, source_type, course_id)

    mcp.tool(
        name=REMOTE_TEXT_TOOL,
        description=(
            "把一段文本资料直接提交为课程素材（远程 MCP 专用）。"
            "远程模式下 upload_source 读不到客户电脑上的文件，优先用本工具；"
            "PDF/PPT/视频等大文件请让用户去 https://demo.example.com 网页版上传。"
        ),
    )(upload_source_text)
    _REGISTERED.add(id(mcp))
