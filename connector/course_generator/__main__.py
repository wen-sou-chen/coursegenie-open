"""统一入口：``python -m course_generator`` 或 ``uvx coursegenie-mcp`` 都走这里。"""

from course_generator.server import main

__all__ = ["main"]

if __name__ == "__main__":
    main()
