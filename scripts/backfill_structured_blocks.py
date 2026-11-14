# -*- coding: utf-8 -*-
"""任务 48b：存量讲义回填「结构块」（Markdown 表格 / Mermaid 流程图），0 LLM、幂等。

真机问题：任务 48 重生成后 27 篇里有 16 篇**既没有 Markdown 表格、也没有 ```mermaid**
（根因是来源标注纪律让表格变「贵」，见 ``backend/docs/task48b-diagram-tables.md``）。
新生成走 ``lecture.ensure_structured_block`` 兜底；**已经落库的老讲义**用本脚本回填，
不必等全课重生成（48c）。

回填口径与生成路径**完全同一套函数**：优先用该节点已有的 chart 数据渲染 Markdown 表格
（表下统一写一行来源），没有可用 chart 就按大纲要点画一张 flowchart TD。

用法（临时三件套，别指向真库/真 storage/真 vault）::

    $env:DATABASE_URL="sqlite:///C:/tmp/cg_48b/coursegenie.db"
    $env:STORAGE_DIR="C:/tmp/cg_48b/storage"
    $env:OBSIDIAN_VAULT_ROOT="C:/tmp/cg_48b/vault"
    cd backend
    .venv\\Scripts\\python.exe ..\\scripts\\backfill_structured_blocks.py --course <course_id>            # dry-run
    .venv\\Scripts\\python.exe ..\\scripts\\backfill_structured_blocks.py --course <course_id> --apply    # 写库 + 复检

写库时会顺带按当前正文复检质检（``lecture_qc.refresh_item``，零 LLM），
所以跑完的「表格/mermaid」判定与分数是当场可核的。跑完回读断言：
处理过的讲义**全部**含表格或 Mermaid，不达标退出码 1（不静默）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.db.session import SessionLocal  # noqa: E402
from app.models.course import ContentItem, Course, OutlineNode  # noqa: E402
from app.services import lecture_qc  # noqa: E402
from app.services.lecture import (  # noqa: E402
    STRUCTURED_KEY,
    _node_chart_options,
    ensure_structured_block,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="存量讲义结构块回填（0 LLM、幂等）")
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

        applied = skipped = failed = 0
        problems: list[str] = []
        for item in items:
            markdown = lecture_qc.lecture_markdown(item)
            if not markdown:
                continue
            node = db.get(OutlineNode, item.node_id) if item.node_id else None
            node_key = str((item.meta or {}).get("node_key") or "-")
            chart_count = len(_node_chart_options(db, node)) if node is not None else 0
            try:
                new_markdown, info = ensure_structured_block(
                    markdown, node=node, charts=_node_chart_options(db, node) if node else [])
            except Exception as exc:  # noqa: BLE001 —— 单篇失败不影响其余讲义
                failed += 1
                problems.append(f"{node_key} {item.title}：兜底抛错 {exc}")
                print(f"[error] {node_key} {item.title}：{exc}")
                continue
            if not info.get("applied"):
                skipped += 1
                print(f"[skip ] {node_key} {item.title}：{info.get('reason')}")
                continue
            applied += 1
            status = "apply" if args.apply else "dry"
            print(f"[{status}] {node_key} {item.title}：回填 {info.get('source')}"
                  f"（节点现有 chart {chart_count} 条）")
            if args.apply:
                item.body = {**(item.body or {}), "markdown": new_markdown}
                item.meta = {**dict(item.meta or {}), STRUCTURED_KEY: info}
                if item.course_id:
                    course = db.get(Course, item.course_id)
                    if course is not None:
                        lecture_qc.refresh_item(db, course, item, node, commit=False)
            # 回读断言：回填后必须真的含表格或 Mermaid
            if not lecture_qc.has_structured_block(new_markdown):
                problems.append(f"{node_key} {item.title}：回填后仍无表格/ mermaid")

        if args.apply:
            db.commit()
        print(f"\n讲义 {len(items)} 篇：回填 {applied} 篇、已有结构块 {skipped} 篇、"
              f"失败 {failed} 篇；{'已写库（含质检复检）' if args.apply else 'dry-run 未写库'}")
        if problems:
            print(f"待处理 {len(problems)} 处：")
            for line in problems:
                print(f"  x {line}")
            return 1
        print("断言通过：处理过的讲义全部含 Markdown 表格或 Mermaid 流程图")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
