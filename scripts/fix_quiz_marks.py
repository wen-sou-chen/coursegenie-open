# -*- coding: utf-8 -*-
"""任务 56：存量讲义的内联「随堂练习」按新规则重渲染（0 LLM、幂等）。

真机问题：旧渲染在 ``answer`` 为空时既漏掉「**答案**」行、也不给正确选项打 ✓。
DSH 已修好数据（8 道 choice 的 answer 补成 "0"），但讲义 **正文**（body.markdown）
还是旧渲染 —— 学生看到的练习与数据库对不上。本脚本按 ``quiz_items`` 用
``lecture.rerender_quiz_section`` 重画那一段，**只替换「随堂练习」小节，正文一字不动**。

用法（临时三件套，别指向真库/真 storage/真 vault）::

    $env:DATABASE_URL="sqlite:///C:/tmp/cg_ab/coursegenie.db"
    $env:STORAGE_DIR="C:/tmp/cg_ab/storage"
    $env:OBSIDIAN_VAULT_ROOT="C:/tmp/cg_ab/vault"
    cd backend
    .venv\\Scripts\\python.exe ..\\scripts\\fix_quiz_marks.py --course <course_id>   # dry-run
    .venv\\Scripts\\python.exe ..\\scripts\\fix_quiz_marks.py --course <course_id> --apply

跑完会回读断言：每道 ``choice`` 题的正文块里都有「**答案**」行、正确选项带 ✓；
不达标即退出码 1（不静默）。
"""
from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.db.session import SessionLocal  # noqa: E402
from app.models.course import ContentItem  # noqa: E402
from app.services.lecture import rerender_quiz_section  # noqa: E402
from app.services.lecture_qc import _RE_PRACTICE_HEAD, lecture_markdown, quiz_rows_of  # noqa: E402

#: 本项目统一口径（任务 99）：**Windows 默认（GBK）控制台**下也要能跑完 —— 下面的明细与
#: 断言会打印 ✓，Python 默认按控制台代码页编码 ⇒ 未包装时 `UnicodeEncodeError` 报错收场
#: （真机上「其实已经写库了、终端却报错」）。stderr 同样要包。
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

_RE_QUIZ_HEAD = re.compile(r"^#{2,4}[ \t]*第[ \t]*(\d+)[ \t]*题")
_RE_HEADING = re.compile(r"^[ \t]{0,3}#{1,4}[ \t]+\S")


def quiz_blocks(markdown: str) -> dict[int, str]:
    """把正文里的「### 第 N 题」块切出来，返回 ``{题号: 块文本}``。"""
    out: dict[int, str] = {}
    number: int | None = None
    buf: list[str] = []
    for line in markdown.splitlines():
        match = _RE_QUIZ_HEAD.match(line)
        if match:
            if number is not None:
                out[number] = "\n".join(buf)
            number, buf = int(match.group(1)), [line]
        elif number is not None and _RE_HEADING.match(line):
            out[number] = "\n".join(buf)
            number, buf = None, []
        elif number is not None:
            buf.append(line)
    if number is not None:
        out[number] = "\n".join(buf)
    return out


def choice_problems(markdown: str, rows: list) -> tuple[list[str], list[str]]:
    """回读断言，返回 ``(问题列表, 数据缺口列表)``（都是可读字符串）。"""
    blocks = quiz_blocks(markdown)
    problems: list[str] = []
    gaps: list[str] = []
    for index, quiz in enumerate(rows, 1):
        if quiz.qtype != "choice" or not quiz.options:
            continue
        answer = "" if quiz.answer is None else str(quiz.answer).strip()
        if not (answer.isdigit() and int(answer) < len(quiz.options)):
            gaps.append(f"第 {index} 题：数据库本身没有合法答案（answer={quiz.answer!r}）")
            continue
        block = blocks.get(index)
        if block is None:
            problems.append(f"第 {index} 题：正文里找不到该题的块")
            continue
        correct = str(quiz.options[int(answer)])
        if f"**答案**：{correct}" not in block:
            problems.append(f"第 {index} 题：缺「**答案**：{correct}」行")
        # 渲染出来是「✓  选项」（marker 自带一个空格），按行比对，别用字符串包含
        marked = [line.strip()[1:].strip() for line in block.splitlines()
                  if line.strip().startswith("✓")]
        if correct.strip() not in marked:
            problems.append(f"第 {index} 题：正确选项「{correct}」没有 ✓")
    return problems, gaps


def main() -> int:
    parser = argparse.ArgumentParser(description="存量讲义内联练习重渲染（0 LLM、幂等）")
    parser.add_argument("--course", default="", help="只处理该课程 id（默认全部课程）")
    parser.add_argument("--node", default="", help="只处理该 node_key（默认全部节点）")
    parser.add_argument("--limit", type=int, default=0, help="最多处理几篇（默认不限）")
    parser.add_argument("--apply", action="store_true", help="写库；默认 dry-run 只报告")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        query = db.query(ContentItem).filter(ContentItem.kind == "lecture")
        if args.course:
            query = query.filter(ContentItem.course_id == args.course)
        items = query.order_by(ContentItem.created_at).all()
        if args.node:
            items = [it for it in items
                     if str((it.meta or {}).get("node_key") or "") == args.node]
        if args.limit:
            items = items[: args.limit]

        changed = same = missing_section = 0
        problems: list[str] = []
        gaps: list[str] = []
        for item in items:
            markdown = lecture_markdown(item)
            rows = list(quiz_rows_of(db, item))
            if not markdown or not rows:
                continue
            node_key = str((item.meta or {}).get("node_key") or "-")
            if not _RE_PRACTICE_HEAD.search(markdown):
                missing_section += 1
                print(f"[无小节] {node_key} {item.title}：正文没有「随堂练习」标题，跳过")
                continue
            new_markdown = rerender_quiz_section(markdown, rows)
            before, _ = choice_problems(markdown, rows)
            after, item_gaps = choice_problems(new_markdown, rows)
            status = "same" if new_markdown == markdown else ("apply" if args.apply else "dry")
            if new_markdown == markdown:
                same += 1
            else:
                changed += 1
            print(f"[{status}] {node_key} {item.title}：重渲染前缺标注 {len(before)} 处 "
                  f"→ 重渲染后 {len(after)} 处")
            for line in before:
                print(f"        before: {line}")
            for line in after:
                print(f"        after : {line}")
            if args.apply and new_markdown != markdown:
                item.body = {**(item.body or {}), "markdown": new_markdown}
            problems += [f"{node_key} {line}" for line in after]
            gaps += [f"{node_key} {line}" for line in item_gaps]

        if args.apply:
            db.commit()
        print(f"\n讲义 {len(items)} 篇：重渲染 {changed} 篇、已是新渲染 {same} 篇、"
              f"无随堂练习小节 {missing_section} 篇；{'已写库' if args.apply else 'dry-run 未写库'}")
        if gaps:
            print(f"数据库本身缺答案：{len(gaps)} 处")
            for line in gaps:
                print(f"  ! {line}")
        if problems:
            print(f"重渲染后仍缺「答案」/✓：{len(problems)} 处")
            for line in problems:
                print(f"  x {line}")
            return 1
        print("断言通过：所有 choice 题的正文块都有「**答案**」行且正确选项带 ✓")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
