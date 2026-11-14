# -*- coding: utf-8 -*-
"""任务 58：存量大纲「陌生术语」0-LLM 处理（术语入表 + 质检复检）。

真机问题：用户在大纲里看到「**教学策略：马尔可夫链预测演示**」，全课没有任何地方解释它
（概念库 9 条里没有、术语表为空、正文也没出现过）—— 因为**概念抽取以前只读讲义正文**，
大纲字段里的自造术语必然漏网。

新生成的大纲走新提示词（白话优先 / 术语紧跟一句解释）+ 新抽取口径；**已经落库的老大纲**
用本脚本处理，两步都不调 LLM：

1. ``--apply``：把「策略/评估字段里出现、且**同一行自带括号解释**」的术语写进
   ``course.meta.terminology``（带 definition）——读者从此查得到；
2. ``--recheck-qc``：按当前口径重算每篇讲义的 ``meta.qc``（0 LLM），让「缓存分」与
   「强制重算分」重新对齐（任务 58 改了 ``terminology`` 判据，必然出现分叉）。
3. ``--report``：打印该课「缓存分 vs 强制重算分」对照，便于判断要不要复检。

⚠️ 只有**自带括号解释**的术语能自动入表；光秃秃的术语（如「马尔可夫链预测演示」）
脚本**不编解释**，只列进报告，由 DSH 用 ``PATCH /api/plan/node/{id}`` 改成白话或补解释。

用法（铁律 #7：临时三件套，别指向真库 / 真 storage / 真 vault）::

    $env:DATABASE_URL="sqlite:///C:/tmp/cg_58/coursegenie.db"
    $env:STORAGE_DIR="C:/tmp/cg_58/storage"
    $env:OBSIDIAN_VAULT_ROOT="C:/tmp/cg_58/vault"
    cd backend
    .venv\\Scripts\\python.exe ..\\scripts\\backfill_outline_terms.py --course <course_id> --report
    .venv\\Scripts\\python.exe ..\\scripts\\backfill_outline_terms.py --course <course_id> --apply --recheck-qc
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.db.session import SessionLocal  # noqa: E402
from app.models.course import ContentItem, Course  # noqa: E402
from app.services import concept as concept_service  # noqa: E402
from app.services import lecture_qc  # noqa: E402
from app.services.planner import list_nodes  # noqa: E402

FIELDS = (("教学策略", "teaching_strategy"), ("评估方式", "assessment"))


def scan_course(db, course) -> list[dict]:
    """列出策略/评估字段里「查不到解释」的术语嫌疑词 + 该行有没有自带括号解释。"""
    concepts = lecture_qc.concepts_of(db, course)
    terminology = concept_service.terminology_only(course)
    rows: list[dict] = []
    for node in list_nodes(db, course.id):
        for label, attr in FIELDS:
            text = str(getattr(node, attr, "") or "")
            for term in lecture_qc.unexplained_jargon(text, concepts, terminology):
                rows.append({
                    "node_id": node.id,
                    "node_key": node.node_key,
                    "field": label,
                    "term": term,
                    "definition": concept_service.definition_from_context(term, text),
                })
    return rows


def recheck_lectures(db, course) -> int:
    """按当前口径重算该课所有讲义的 meta.qc（0 LLM，幂等）。"""
    rows = (db.query(ContentItem)
            .filter(ContentItem.course_id == course.id, ContentItem.kind == "lecture")
            .order_by(ContentItem.created_at).all())
    for item in rows:
        lecture_qc.refresh_item(db, course, item)
    return len(rows)


def qc_gap(db, course) -> dict:
    """「缓存分 vs 强制重算分」对照（不写库）：改了判据后这两个数必然分叉。"""
    cached = lecture_qc.course_report(db, course)
    rows = (db.query(ContentItem)
            .filter(ContentItem.course_id == course.id, ContentItem.kind == "lecture")
            .order_by(ContentItem.created_at).all())
    forced: list[int] = []
    warned: list[str] = []
    for item in rows:
        node = db.get(lecture_qc.OutlineNode, item.node_id) if item.node_id else None
        result = lecture_qc.build_for_item(
            db, course, node, item, lecture_qc.lecture_markdown(item))
        forced.append(int(result["qc"]["score"]))
        check = next((c for c in result["qc"]["checks"] if c["key"] == "terminology"), None)
        if check is not None and check.get("level") == "warn":
            warned.append(str((node.node_key if node is not None else "") or item.title))
    average = round(sum(forced) / len(forced), 1) if forced else 0.0
    return {"cached": cached.get("average_score"), "forced": average,
            "lectures": len(forced), "terminology_warn": warned}


def main() -> int:
    parser = argparse.ArgumentParser(description="存量大纲陌生术语处理（0 LLM、幂等）")
    parser.add_argument("--course", default="", help="只处理该课程 id（默认全部课程）")
    parser.add_argument("--apply", action="store_true",
                        help="把自带括号解释的术语写进 course.meta.terminology")
    parser.add_argument("--recheck-qc", action="store_true",
                        help="按当前口径重算讲义 meta.qc（0 LLM）")
    parser.add_argument("--report", action="store_true",
                        help="打印「缓存分 vs 强制重算分」对照")
    parser.add_argument("--require-clean", action="store_true",
                        help="仍有「需人工处理」的术语时以退出码 1 结束（不静默）")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        courses = [db.get(Course, args.course)] if args.course else db.query(Course).all()
        courses = [row for row in courses if row is not None]
        total = pending = 0
        for course in courses:
            rows = scan_course(db, course)
            if rows or args.report or args.recheck_qc:
                print(f"\n=== {course.title}（{course.id}）===")
            for row in rows:
                mark = "可入表" if row["definition"] else "需人工改白话"
                extra = f" → 定义：{row['definition']}" if row["definition"] else ""
                print(f"  [{mark}] {row['node_key']} {row['field']}：{row['term']}{extra}")
            total += len(rows)
            pending += sum(1 for row in rows if not row["definition"])
            if args.apply:
                entries = [{"name": row["term"], "aliases": [], "chapters": [row["node_key"]],
                            "definition": row["definition"]}
                           for row in rows if row["definition"]]
                if entries:
                    concept_service.record_terminology(db, course, entries)
                    print(f"  → 写入术语表 {len(entries)} 条（带 definition，幂等）")
            if args.recheck_qc:
                print(f"  → 复检讲义 meta.qc：{recheck_lectures(db, course)} 篇")
            if args.report:
                gap = qc_gap(db, course)
                print(f"  → 缓存分 {gap['cached']} vs 强制重算分 {gap['forced']}"
                      f"（{gap['lectures']} 篇；terminology warn："
                      f"{'、'.join(gap['terminology_warn']) or '无'}）")
        print(f"\n合计：术语嫌疑 {total} 处；其中需人工（无自带解释）{pending} 处")
        if args.require_clean and pending:
            print("仍有需人工处理的术语 → 退出码 1（不静默）")
            return 1
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
