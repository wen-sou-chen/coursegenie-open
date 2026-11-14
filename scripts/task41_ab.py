# -*- coding: utf-8 -*-
"""任务 41 A/B 实测：同一节课，旧提示词 vs 新提示词各真实生成一次。

背景：任务 41 借鉴「仓颉.skill」的提取优先级（一手案例 / 失败教训 / 能力边界 /
内在矛盾优先，通用观点最低），把要求写进讲义提示词，并新增两项质检
（substance / actionable）。本脚本用**同一节课、同一素材、同一个模型**跑两次，
只换 ``LECTURE_SYSTEM``，把两版正文与质检数字摆在一起，供 DSH 判断是否全课铺开。

用法（临时三件套，别指向真库/真 storage/真 vault）::

    $env:DATABASE_URL="sqlite:///C:/tmp/cg_ab/coursegenie.db"   # 真实库的副本
    $env:STORAGE_DIR="C:/tmp/cg_ab/storage"
    $env:OBSIDIAN_VAULT_ROOT="C:/tmp/cg_ab/vault"
    cd backend
    .venv\\Scripts\\python.exe ..\\scripts\\task41_ab.py --course <course_id> --node 1.2

输出：``--out`` 目录下 ``old.md`` / ``new.md``（两版正文）+ 控制台 JSON 对比。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.db.session import SessionLocal  # noqa: E402
from app.models.course import Course, OutlineNode  # noqa: E402
from app.services import usage as usage_log  # noqa: E402
from app.services.concept import concept_rows  # noqa: E402
from app.services.lecture import LECTURE_SYSTEM, _lecture_user_prompt, lecture_concepts  # noqa: E402
from app.services.lecture_qc import (  # noqa: E402
    _FLUFF_WORDS, _MUDDY_WORDS, _step_candidates, compute_qc, muddy_sentences,
    parse_sources, prose_of, substance_hits, terminology_prompt_section,
)
from app.services.llm import build_llm_client  # noqa: E402
from app.services.material import material_catalog, material_prompt_section  # noqa: E402
from app.services.planner import list_nodes  # noqa: E402
from app.services.style_anchor import get_style_anchor  # noqa: E402

# 任务 41 之前的讲义提示词（提交 0976f7d 时的 LECTURE_SYSTEM，原样粘贴用于对照）
OLD_SYSTEM = """LECTURE_JSON
你是资深课程讲师。为给定的知识点撰写「一篇完整的图文混排讲义」，只输出 JSON：
{"markdown": "完整讲义 Markdown 全文", "summary": "一句话摘要", "keywords": ["关键词"],
 "quizzes": [{"qtype": "choice|blank|short", "difficulty": "easy|medium|hard",
   "question": "题干", "options": ["选项..."], "answer": "答案或正确选项序号", "explanation": "解析"}]}

规则：
1) markdown 从 ## 开始，含引入、分点讲解（列表/表格/引用）、小结，直接可放进 Obsidian；
   不要用代码块把整篇包起来，只写本章节内容，素材中其他章节仅作背景。
2) 不使用 AI 位图（系统不会生成 PNG 插图）：需要图示时用文字/表格/引用讲清，
   流程或结构图直接写 ```mermaid 代码块；每篇讲义末尾会自动配一张矢量概念图，
   不要在正文写 ![...](gen://...) 之类的图片占位符。
3) quizzes 为 0~5 道随堂练习，含答案与解析；choice 的 answer 用正确选项序号（0 起）；
4) 每条关键点（列表项）末尾必须标注来源，写成「（来源: 素材标题 #段落号）」，
   段落号取自课程素材里每段前的 [序号]；素材里找不到依据的段落，必须显式写成
   「（无直接依据,建议人工确认）」，禁止编造来源，也不许留空不标；
5) 术语一律使用下方「课程术语表」的标准名；别名只在首次出现时用括号注明一次。"""

_RE_PIT_HEAD = re.compile(r"^[ \t]{0,3}#{2,4}[ \t]+[^\n]*踩过的坑[ \t]*$", re.M)


def old_user_prompt(node, tone: str, material: str, anchor: dict | None,
                    terminology: str) -> str:
    """任务 41 之前的 ``_lecture_user_prompt``（旧口径对照用，原样复刻）。

    只换 system 不换 user 会让旧组也拿到新要求（踩过的坑 / 边界），对比就失效了。
    """
    from app.services.style_anchor import style_anchor_prompt

    return (
        f"当前知识点：{node.title}（编号 {node.node_key}）\n"
        f"学习目标：{node.objective}\n"
        f"写作边界：只写本知识点内容；素材中其他章节只作背景参考。\n"
        f"语气：{tone}{material}{style_anchor_prompt(anchor)}{terminology}"
    )


def fluff_density(markdown: str) -> float:
    """套话密度 = 套话词命中数 / 千字（与 fluff 检查同一份词表）。"""
    prose = prose_of(markdown)
    chars = len(re.sub(r"\s+", "", prose))
    hits = sum(prose.count(word) for word in _FLUFF_WORDS)
    return round(hits / max(1, chars) * 1000, 2)


def summarize(markdown: str, qc: dict) -> dict:
    levels = {c["key"]: c["level"] for c in qc["checks"]}
    prose = prose_of(markdown)
    return {
        "score": qc["score"],
        "levels": levels,
        "substance": levels.get("substance"),
        "actionable": levels.get("actionable"),
        "fluff": levels.get("fluff"),
        "sources": levels.get("sources"),
        "chars": qc["metrics"]["chars"],
        "metrics_sources": qc["metrics"]["sources"],
        "fluff_hits": sum(prose.count(word) for word in _FLUFF_WORDS),
        "fluff_per_1000": fluff_density(markdown),
        "substance_hits": substance_hits(markdown),
        "step_candidates": len(_step_candidates(markdown)),
        "has_pit_section": bool(_RE_PIT_HEAD.search(markdown)),
        "muddy_words": [w for w in _MUDDY_WORDS if w in prose],
        "boundary_heading": bool(re.search(r"^[ \t]{0,3}#{2,4}[ \t]+[^\n]*(不覆盖|边界|找专家)", markdown, re.M)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--course", required=True)
    parser.add_argument("--node", required=True, help="知识点 key，如 1.2")
    parser.add_argument("--tone", default="正式")
    parser.add_argument("--out", default=str(Path.cwd() / "ab_out"))
    args = parser.parse_args()

    db = SessionLocal()
    try:
        course = db.get(Course, args.course)
        if course is None:
            print(f"课程不存在：{args.course}")
            return 2
        node = next((n for n in list_nodes(db, course.id) if n.node_key == args.node), None)
        if node is None:
            print(f"知识点不存在：{args.node}")
            return 2
        course_title = course.title
        node_label = f"{node.node_key} {node.title}"
        objective = str(node.objective or "")
        material = material_prompt_section(db, course, numbered=True)
        catalog = material_catalog(db, course)
        anchors = concept_rows(db, course.id)
        anchor = get_style_anchor(course)
        terminology = terminology_prompt_section(lecture_concepts(db, course))
        users = {
            "old": old_user_prompt(node, args.tone, material, anchor, terminology),
            "new": _lecture_user_prompt(node, args.tone, material, anchor, terminology),
        }
        node_key = str(node.node_key)
    finally:
        db.close()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    client = build_llm_client()
    report: dict = {"course": course_title, "node": node_label,
                    "objective": objective,
                    "model": getattr(client, "name", ""), "variants": {}}

    for label, system in (("old", OLD_SYSTEM), ("new", LECTURE_SYSTEM)):
        with usage_log.collect() as collector:
            data = usage_log.complete_json(client, system, users[label], max_tokens=4000,
                                           purpose="lecture")
        markdown = str(data.get("markdown") or "")
        (out_dir / f"{label}.md").write_text(markdown, encoding="utf-8")
        quizzes = [{"qtype": q.get("qtype"), "difficulty": q.get("difficulty")}
                   for q in (data.get("quizzes") or [])]
        sources = parse_sources(markdown, node_key, catalog)
        qc = compute_qc(markdown, objective=objective, quizzes=quizzes,
                        sources=sources, concepts=anchors, catalog=catalog, meta={})
        row = summarize(markdown, qc)
        row["usage"] = collector.summary()
        row["head"] = [ln for ln in markdown.splitlines() if ln.strip().startswith("#")][:12]
        row["excerpt_300"] = re.sub(r"\s+", "", markdown)[:300]
        report["variants"][label] = row

    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
