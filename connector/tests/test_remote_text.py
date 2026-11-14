"""远程 MCP 文本上传（upload_source_text）回归。

覆盖：新课程文本上传走通 / 已有课程上传 / 空文本中文报错 / 非 text 类型拒绝 / 动态注册幂等。
把 `_upload_text` 里的空文本校验删掉，`test_empty_text_returns_chinese_error` 必须变红。
"""

from __future__ import annotations

from typing import Any

from course_generator import remote_tools, server


class _DummyMcp:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, name: str | None = None, description: str | None = None):
        def decorator(func):
            self.tools[name or func.__name__] = func
            return func

        return decorator


def _stub_request(calls: list[tuple[str, str, dict[str, Any]]]):
    def fake(settings, method: str, path: str, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/course/":
            return {"id": "c-new", "title": kwargs["json_body"]["title"]}
        if path == "/material/paste":
            return {"id": "m-1", "chars": 18, "preview": "第一课"}
        raise AssertionError(f"unexpected request: {method} {path}")

    return fake


def test_text_upload_creates_course(monkeypatch):
    calls: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(server, "_request", _stub_request(calls))

    result = remote_tools._upload_text(server, "测试课", "  第一课：发酵原理  ")

    assert result["ok"] is True
    assert result["task_id"] == "c-new"
    assert result["course_created"] is True
    assert "18 字" in result["summary"]
    assert calls[0][:2] == ("POST", "/course/")
    assert calls[1][:2] == ("POST", "/material/paste")
    assert calls[1][2]["json_body"] == {
        "course_id": "c-new",
        "title": "测试课",
        "text": "第一课：发酵原理",
    }


def test_text_upload_into_existing_course(monkeypatch):
    calls: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(server, "_request", _stub_request(calls))
    monkeypatch.setattr(server, "_course", lambda settings, course_id: {"id": course_id, "title": "已有课"})

    result = remote_tools._upload_text(server, "", "补充的文本", course_id="c-old")

    assert result["ok"] is True
    assert result["course_created"] is False
    assert [call[1] for call in calls] == ["/material/paste"]
    assert calls[0][2]["json_body"]["title"] == "已有课"


def test_empty_text_returns_chinese_error():
    result = remote_tools._upload_text(server, "测试课", "   \n  ")

    assert result["ok"] is False
    assert "text 不能为空" in result["error"]
    assert "https://demo.example.com" in result["hint"]


def test_non_text_source_type_rejected(monkeypatch):
    monkeypatch.setattr(server, "_request", _stub_request([]))

    result = remote_tools._upload_text(server, "测试课", "正文", source_type="pdf")

    assert result["ok"] is False
    assert "source_type=text" in result["error"]
    assert "网页版" in result["hint"]


def test_register_is_idempotent_and_tool_callable(monkeypatch):
    calls: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(server, "_request", _stub_request(calls))
    dummy = _DummyMcp()

    remote_tools.register_remote_tools(dummy)
    remote_tools.register_remote_tools(dummy)

    assert list(dummy.tools) == ["upload_source_text"]
    result = dummy.tools["upload_source_text"]("注册测试", "一段正文")
    assert result["ok"] is True
