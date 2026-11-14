"""CourseGenie MCP Connector —— 把课程生成应用接入 WorkBuddy。

包内结构：

- ``server.py``：FastMCP 服务与八个工具（HTTP 桥接到用户自己的 CourseGenie 后端）
- ``__main__.py``：``python -m course_generator`` / ``uvx coursegenie-mcp`` 的统一入口
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
