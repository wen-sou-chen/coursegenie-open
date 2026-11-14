"""课程资产体检报告（任务 24-C）：把各模块的真实统计汇成一页可交付的报告。

八段结构与数据来源（**数字全部取自对应模块，不各算一套**）：

=================  ===============================================================
1. 结论卡           本文件唯一自算的部分：按下面七段的真实数字做固定扣分（见 SCORE_RULES）
2. 覆盖度           DB 容器计数（章 / 知识点 / 讲义 / 概念 / 图表 / 课件 / 习题）
3. 一致性           :func:`kb.verify_course_notes` 的原样结果
4. 链接健康         :func:`kb.course_link_health`（双链 + 站内链接，坏链必须 0）
5. 质检             :func:`lecture_qc.course_report`
6. 用量与成本       :func:`analytics.course_usage`
7. 导出健康         :func:`kb.course_asset_health`
8. 建议动作         由上面各项的阈值触发，每条都带「触发数字 + 具体做法」
=================  ===============================================================

报告正文不含机器时间戳（只有 ``generated_at`` 在接口返回值里），因此输入不变时
重复生成得到**字节一致**的文件 → 同名覆盖、幂等（不产生副本）。
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.models.course import ContentItem, Course, QuizItem
from app.services import analytics, kb
from app.services import lecture_qc
from app.services.planner import list_nodes

REPORT_NAME = kb.AUDIT_REPORT_NAME  # 口径与 kb 的链接体检/导出健康排除项同一份
# 任务 61：对外交付时报告里的「用量与成本」整段换成中性说明（金额 / tokens / 调用次数
# / 逐篇成本一个都不进客户包）。文案与交付包 48p 的 EXTERNAL_USAGE_NOTE 同一句。
# 任务 67：补上「自动化工具辅助生产 + 交付前已逐篇经人工复核确认」这句承诺。它不是成本
# 信息，不触 48p / 61 的脱敏口径 ⇒ 交付说明、验收单与本报告三处同一句，两种模式都写。
EXTERNAL_USAGE_NOTE = ("本课内容由自动化工具辅助生产，交付前已逐篇经人工复核确认；"
                       "事实性表述请按 `素材清单.md` 回溯原始素材。")
EXTERNAL_COST_NOTE = "本报告不包含生成过程的用量与费用明细（该口径仅内部复盘使用）。"
_RE_COST_CLAUSE = re.compile(r"[、,，]?\s*累计费用[^。；;]*[。；;]?")


def public_summary(summary: str) -> str:
    """对外模式的结论句：抹掉「累计费用 ≈… 元」这一句（金额不写进客户包）。"""
    text = _RE_COST_CLAUSE.sub("。", str(summary or "")).replace("。。", "。").strip()
    return text or "（逐项数字见下表）"


LEVEL_OK, LEVEL_WARN, LEVEL_FAIL = "ok", "warn", "fail"
PASS_SCORE, WARN_SCORE = 90, 75
SCORE_RULES = (
    ("坏链", 20, 40),
    ("一致性不一致", 3, 15),
    ("质检不合格项", 4, 20),
    ("质检待改进项", 1, 10),
    ("孤儿文件", 1, 5),
)
WORST_N = 3
TOP_USAGE_N = 3


def _conclusion(consistency: dict, links: dict, quality: dict, export_health: dict) -> dict:
    """总分 = 100 − 各项扣分（每项都有上限），等级按 90 / 75 分档。"""
    fails = sum(row["fail"] for row in (quality.get("failure_distribution") or []))
    warns = sum(row["warn"] for row in (quality.get("failure_distribution") or []))
    raw = (
        ("坏链", links.get("broken") or 0),
        ("一致性不一致", consistency.get("inconsistent") or 0),
        ("质检不合格项", fails),
        ("质检待改进项", warns),
        ("孤儿文件", export_health.get("orphan_count") or 0),
    )
    deductions: list[dict] = []
    for (label, per, cap), (_same, value) in zip(SCORE_RULES, raw):
        cut = min(int(value) * per, cap)
        if cut:
            deductions.append({"reason": label, "value": int(value), "cut": cut})
    score = max(0, 100 - sum(row["cut"] for row in deductions))
    level = "健康" if score >= PASS_SCORE else ("待改进" if score >= WARN_SCORE else "有风险")
    return {"score": score, "level": level, "badge": LEVEL_OK if score >= PASS_SCORE
            else (LEVEL_WARN if score >= WARN_SCORE else LEVEL_FAIL), "deductions": deductions}


def coverage(db: Session, course: Course) -> dict[str, Any]:
    """覆盖度：章 / 知识点 / 各类内容条目 / 习题（全部来自 DB 容器真实计数）。"""
    nodes = list_nodes(db, course.id)
    chapters = [node for node in nodes if "." not in node.node_key]
    points = [node for node in nodes if "." in node.node_key]
    items = (db.query(ContentItem)
             .filter(ContentItem.course_id == course.id).all())
    kinds = Counter(item.kind for item in items)
    lectures = [item for item in items if item.kind == "lecture"]
    covered = {item.node_id for item in lectures if item.node_id}
    missing = [node.node_key for node in points if node.id not in covered]
    quizzes = (db.query(QuizItem).filter(QuizItem.course_id == course.id).count())
    # 数据图表在库里按子类型存（chart_bar / chart_line / chart_table / chart_mindmap /
    # chart_timeline），**没有裸 "chart" 这个 kind**，所以只能按前缀统计（P9-2：原先写死
    # kinds.get("chart", 0)，报告里恒为 0，而真值是 36）。
    charts = sum(count for kind, count in kinds.items()
                 if str(kind or "").startswith("chart_"))
    return {
        "chapters": len(chapters),
        "points": len(points),
        "lectures": len(lectures),
        "contents": kinds.get("content", 0),
        "concepts": kinds.get("concept", 0),
        # 「概念图/流程图（条目）」取容器里的 diagram 条目数；讲义正文里的 mermaid
        # 流程图段数是另一套口径（前端口径 contentCounts.ts / 任务 27），两者不互相替代。
        "diagrams": kinds.get("diagram", 0),
        "charts": charts,
        "quizzes": quizzes,
        "ppt": kinds.get("ppt", 0),
        "lecture_coverage": round(len(lectures) / len(points), 3) if points else 0.0,
        "missing_lectures": missing[:20],
        "missing_lectures_count": len(missing),
    }


def _actions(consistency: dict, links: dict, quality: dict, usage: dict,
             export_health: dict, cover: dict, *, external: bool = False) -> list[dict[str, Any]]:
    """建议动作：每条都有触发数字与可执行做法，按严重度排序。

    ``external=True`` 时只改「未配置单价」那条措辞里的 ``（元/百万 tokens）`` —— 它是
    我们内部运维口径，写进客户包会把 tokens 这三个字带出去（任务 61）；条目本身保留，
    段落结构与其余条目逐字不变。
    """
    out: list[dict[str, Any]] = []

    def add(level: str, text: str, action: str) -> None:
        out.append({"level": level, "text": text, "action": action})

    if not links.get("ok", True):
        add(LEVEL_WARN, f"还没法做链接体检：{links.get('reason') or '未绑定笔记库'}",
            "在第 5 步绑定 Vault 并导出一次笔记后重新生成报告")
    elif links.get("broken"):
        add(LEVEL_FAIL, f"笔记里有 {links['broken']} 处坏链（双链或站内链接指向不存在的文件）",
            "回到「素材与笔记」页签点「以网页为准修复」，或重新导出整课，再复检")
    if consistency.get("ok") and consistency.get("inconsistent"):
        add(LEVEL_FAIL, f"{consistency['inconsistent']} 个产出与网页端不一致（含被手改的页面）",
            "在「素材与笔记」页签点「修复不一致（以网页为准）」，需要保留的手改请先复制出来")
    if not quality.get("checked"):
        add(LEVEL_WARN, "还没有任何讲义通过质检", "先在第 3 步生成完整讲义")
    else:
        dist = {row["key"]: row for row in (quality.get("failure_distribution") or [])}
        sources = dist.get("sources") or {}
        if sources.get("failed"):
            add(LEVEL_FAIL if sources.get("fail") else LEVEL_WARN,
                f"{sources['failed']} 篇讲义的关键点缺来源标注",
                "在「质量报告」里对这些篇点「按质检建议重生成」（提示词会要求补 (来源: 素材 #段落)）")
        structure = dist.get("structure") or {}
        if structure.get("failed"):
            add(LEVEL_FAIL if structure.get("fail") else LEVEL_WARN,
                f"{structure['failed']} 篇讲义结构不完整（缺关键小节）",
                "对这些篇点「按质检建议重生成」补齐小节，或手工补写后重新导出")
        worst = [row for row in (quality.get("worst") or []) if row.get("score", 100) < WARN_SCORE]
        if worst:
            keys = "、".join(str(row.get("node_key") or row.get("title")) for row in worst)
            add(LEVEL_WARN, f"{len(worst)} 篇讲义质检分低于 {WARN_SCORE}（{keys}）",
                "优先修这几篇：按质检建议重生成，或按问题清单手工修改")
    if export_health.get("orphan_count"):
        add(LEVEL_WARN, f"笔记库里有 {export_health['orphan_count']} 个没人引用的旧图/文件",
            "重新导出一次整课：导出末尾会把孤儿移进 99_归档（可随时拖回）")
    if export_health.get("deck_missing"):
        add(LEVEL_WARN, f"{len(export_health['deck_missing'])} 个课件没有落地到笔记库",
            "在「PPT」页签重新生成课件，再导出一次笔记")
    if usage.get("unpriced_models"):
        add(LEVEL_WARN, f"模型 {('、'.join(usage['unpriced_models']))} 未配置单价，费用按 0 计",
            "在 backend/.env 里配 MODEL_PRICES（单价表）后重新生成用量报告" if external else
            "在 backend/.env 里配 MODEL_PRICES（元/百万 tokens）后重新生成用量报告")
    if cover.get("missing_lectures_count"):
        add(LEVEL_WARN, f"{cover['missing_lectures_count']} 个知识点还没有完整讲义",
            "在第 3 步「按章生成剩余全部」补齐（输入未变的会被自动跳过，不重复花钱）")
    if not cover.get("concepts"):
        add(LEVEL_WARN, "概念库为空，知识图谱与概念索引会缺内容",
            "在第 3 步「本章概念（概念库）」抽一次概念，再导出知识图谱")
    if not out:
        add(LEVEL_OK, "各项体检指标都在阈值内", "保持当前流程即可；每次大改后重新生成报告复检")
    order = {LEVEL_FAIL: 0, LEVEL_WARN: 1, LEVEL_OK: 2}
    return sorted(out, key=lambda row: order.get(row.get("level"), 3))


def _link_table(db: Session, course: Course) -> tuple[Callable[[str, str], str], dict[str, str]]:
    """双链渲染器（解析不到就退化为纯文本，保证报告自身 0 坏链）+ 知识点页路径表。"""
    try:
        _root, out_dir, _rel = kb._course_out_dir(course)
    except ValueError:
        return (lambda _target, label: label), {}
    rels, stems = kb._vault_link_index(out_dir)
    nodes = kb._nodes(db, course.id)
    stems_of = {node.id: kb._node_stem(node) for node in nodes}
    for idx, chapter in enumerate(kb._chapter_list(nodes), 1):
        chapter_dir = kb._chapter_dir(idx, chapter)
        for node in nodes:
            if node.node_key == chapter.node_key or node.node_key.startswith(f"{chapter.node_key}."):
                stems_of[node.id] = f"{chapter_dir}/{kb._node_stem(node)}"

    def link(target: str, label: str) -> str:
        return f"[[{target}|{label}]]" if target and kb._note_link_exists(rels, stems, target) else label

    return link, {node_id: path for node_id, path in stems_of.items() if path}


def render_report(course: Course, report: dict, link: Callable[[str, str], str],
                  node_paths: dict[str, str], *, external: bool = False) -> str:
    """把体检结果渲染成 markdown（八段；正文不含机器时间戳，保证幂等）。

    ``external=True``（任务 61）是**对外交付**口径：第 6 段不再输出金额 / tokens /
    LLM 调用次数 / 逐篇成本（换成中性说明），其余七段与内部口径逐字一致。
    默认 ``False`` = 内部口径（真 vault 那份仍然看得见成本）。
    """
    conclusion = report["conclusion"]
    cover = report["coverage"]
    consistency = report["consistency"]
    links = report["links"]
    quality = report["quality"]
    usage = report["usage"]
    export_health = report["export_health"]

    parts = [
        "---",
        f'title: "{course.title} · 课程体检报告"',
        'tags: "coursegenie 体检报告"',
        "---",
        "",
        f"# {course.title} · 课程体检报告",
        "",
        "> 由 CourseGenie 资产体检自动生成（重新生成会覆盖）；数字取自各模块真实统计。",
        "",
        kb.COURSE_INDEX_BACKLINK,
        "",
        "## 1. 结论卡",
        "",
        f"- **总分 {conclusion['score']} / 100 · 等级：{conclusion['level']}**",
        f"- 一句话结论：{report['summary']}",
    ]
    if conclusion["deductions"]:
        parts += ["", "扣分明细："]
        parts += [f"- {row['reason']}：{row['value']} → −{row['cut']} 分"
                  for row in conclusion["deductions"]]
    parts += ["", "## 2. 覆盖度", "",
              "| 项 | 数量 |", "| --- | --- |",
              f"| 章 | {cover['chapters']} |",
              f"| 知识点 | {cover['points']} |",
              f"| 完整讲义 | {cover['lectures']}（覆盖 {round(cover['lecture_coverage'] * 100)}%） |",
              f"| 概念 | {cover['concepts']} |",
              f"| 概念图/流程图（条目） | {cover['diagrams']} |",
              f"| 数据图表（条目） | {cover['charts']} |",
              f"| 图表库文件 | {export_health.get('charts', 0)} |",
              f"| 随堂练习 | {cover['quizzes']} |",
              f"| 课件 | {cover['ppt']} |"]
    if cover["missing_lectures"]:
        parts += ["", f"缺讲义的知识点（{cover['missing_lectures_count']} 个）："
                  + "、".join(cover["missing_lectures"])]

    parts += ["", "## 3. 一致性", ""]
    if not consistency.get("ok"):
        parts += [f"- 无法校验：{consistency.get('reason') or '未绑定笔记库'}"]
    else:
        parts += [f"- 已检查产出 **{consistency['checked']}** 项："
                  f"一致 {consistency['consistent']}、不一致 {consistency['inconsistent']}",
                  f"- 结论：{'✅ 网页端与笔记库一致' if not consistency['inconsistent'] else '❌ 有产出需要修复'}"]
        bad = [row for row in consistency.get("nodes") or [] if row.get("status") != "consistent"]
        for row in bad[:5]:
            note = row.get("note") or ""
            parts += [f"  - {row.get('node_key') or '00'} {row.get('title') or ''}"
                      f"（{row.get('part')}｜{note}）：{'; '.join(row.get('diff') or [])[:120]}"]

    parts += ["", "## 4. 链接健康", ""]
    if not links.get("ok"):
        parts += [f"- 无法检查：{links.get('reason') or '未绑定笔记库'}"]
    else:
        mark = "✅" if not links["broken"] else "❌"
        parts += [f"- 双链 {links['wiki_links']} 条 + 站内链接 {links['file_links']} 条"
                  f" = **{links['links']} 条**；坏链 **{links['broken']} 条** {mark}"]
        for row in (links.get("broken_links") or [])[:5]:
            parts += [f"  - {row['file']} → {row['target']}（{row['kind']}）"]

    parts += ["", "## 5. 质检", ""]
    if not quality.get("checked"):
        parts += ["- 还没有讲义通过质检"]
    else:
        parts += [f"- 平均分 **{quality['average_score']}**"
                  f"（最低 {quality['min_score']} / 最高 {quality['max_score']}），"
                  f"已质检 {quality['checked']} 篇"
                  # 任务 112：这份分数按哪套判据算的（口径变了就重算，不沿用旧缓存分数）
                  f"；按现行判据现算（{quality.get('rules') or lecture_qc.rules_label()}）", "",
                  "| 检查项 | 不合格 | 待改进 | 未执行 |", "| --- | --- | --- | --- |"]
        for row in quality.get("failure_distribution") or []:
            parts += [f"| {lecture_qc.CHECK_LABELS.get(row['key'], row['key'])} | {row['fail']} | "
                      f"{row['warn']} | {row.get('skipped', 0)} |"]
        if quality.get("skipped_items"):
            # P5-1：有检查项没执行时必须写明，否则平均分会被当成「全项都考过」的分数
            parts += ["", f"- ⚠️ {quality['skipped_items_notice']}"]
        if quality.get("worst"):
            parts += ["", f"最差 {len(quality['worst'])} 篇："]
            for row in quality["worst"]:
                target = node_paths.get(row.get("node_id") or "", "")
                parts += [f"- {link(target, row.get('title') or row.get('node_key') or '')}"
                          f"（{row['score']} 分）"]

    parts += ["", ("## 6. 内容生成说明" if external else "## 6. 用量与成本"), ""]
    if external:
        # 任务 61：客户包里不出现金额 / tokens / 调用次数 / 逐篇成本
        parts += [f"- {EXTERNAL_USAGE_NOTE}", f"- {EXTERNAL_COST_NOTE}"]
    elif not usage.get("items_measured"):
        parts += ["- 还没有用量记录（生成过内容后再看）"]
    else:
        parts += [f"- 累计 tokens **{usage['total_tokens']}**（输入 {usage['prompt_tokens']} / "
                  f"输出 {usage['completion_tokens']}）",
                  f"- 累计费用 **≈{usage['cost_cny']} 元**（平均单篇 ≈{usage['avg_cost_cny']} 元）",
                  f"- 平均单篇耗时 **{round(usage['avg_duration_ms'] / 1000, 1)} 秒**"
                  f"（讲义平均 {round(usage['avg_lecture_duration_ms'] / 1000, 1)} 秒）",
                  f"- LLM 调用 {usage['llm_calls']} 次"]
        if usage.get("note"):
            parts += [f"- ⚠️ {usage['note']}"]
        if usage.get("top_items"):
            parts += ["", f"最贵 {len(usage['top_items'])} 篇："]
            for row in usage["top_items"][:TOP_USAGE_N]:
                target = node_paths.get(row.get("node_id") or "", "")
                parts += [f"- {link(target, row.get('title') or row.get('node_key') or '')}"
                          f"：{row['total_tokens']} tokens / ≈{row['cost_cny']} 元 / "
                          f"{round(row['duration_ms'] / 1000, 1)} 秒"]

    parts += ["", "## 7. 导出健康", ""]
    if not export_health.get("ok"):
        parts += [f"- 无法检查：{export_health.get('reason') or '未绑定笔记库'}"]
    else:
        parts += [f"- 课程目录共 {export_health['files']} 个文件（不含本报告；"
                  f"笔记 {export_health['notes']} / 图表库 {export_health['charts']} / "
                  f"章节素材 {export_health['assets']} / 概念页 {export_health['concept_pages']} / "
                  f"课件 {export_health['ppt']}）",
                  f"- 孤儿文件 **{export_health['orphan_count']}** 个"
                  f"（导出清理会移进 99_归档）",
                  f"- 会被保留（疑似手改）的文件 **{export_health['preserved_count']}** 个",
                  f"- 课件落地：{export_health['decks']} 个"
                  f"（缺 {len(export_health['deck_missing'])}）"]
        for row in (export_health.get("orphans") or [])[:5]:
            parts += [f"  - 孤儿：{row}"]
        for row in (export_health.get("preserved") or [])[:5]:
            parts += [f"  - 保留：{row['file']}（{kb.PRESERVED_REASONS.get(row['reason'], row['reason'])}）"]

    parts += ["", "## 8. 建议动作", ""]
    for i, row in enumerate(report["actions"], 1):
        mark = {"fail": "❌", "warn": "⚠️", "ok": "✅"}.get(row["level"], "•")
        parts += [f"{i}. {mark} {row['text']}", f"   - 该做什么：{row['action']}"]
    parts += ["", "---", ""]
    return "\n".join(parts).rstrip() + "\n"


def course_report(db: Session, course: Course, *,
                  extra_link_targets: Iterable[str] = (),
                  external: bool = False) -> dict[str, Any]:
    """汇总八段体检结果（不写盘；渲染好的 markdown 一并返回）。

    ``extra_link_targets`` 透传给 :func:`kb.course_link_health`：写报告的调用方
    用它声明「报告自身紧接着就会落盘」，避免报告在落盘前体检、把索引 → 报告这条
    合法链接记成坏链写死进正文（任务 60）。读取端不传，如实反映磁盘现状。

    ``external=True`` 时把结论句里的「累计费用」去掉、第 6 段换成中性说明（任务 61）；
    默认 ``False`` = 内部口径（真 vault 用的就是这一条）。
    """
    cover = coverage(db, course)
    consistency = kb.verify_course_notes(db, course)
    links = kb.course_link_health(db, course, extra_targets=extra_link_targets)
    export_health = kb.course_asset_health(db, course)
    quality = lecture_qc.course_report(db, course, worst_n=WORST_N)
    usage = analytics.course_usage(db, course.id, top_n=TOP_USAGE_N)
    conclusion = _conclusion(consistency, links, quality, export_health)
    actions = _actions(consistency, links, quality, usage, export_health, cover, external=external)
    link, node_paths = _link_table(db, course)

    summary = (f"{cover['points']} 个知识点 / {cover['lectures']} 篇讲义；"
               f"坏链 {links.get('broken', '—')} 条、"
               f"不一致 {consistency.get('inconsistent', '—')} 项、"
               f"质检平均 {quality.get('average_score', 0)} 分、"
               f"累计费用 ≈{usage.get('cost_cny', 0)} 元。")
    if external:
        summary = public_summary(summary)
    report = {
        "course_id": course.id,
        "course_title": course.title,
        "conclusion": conclusion,
        "coverage": cover,
        "consistency": consistency,
        "links": links,
        "quality": quality,
        "usage": usage,
        "export_health": export_health,
        "actions": actions,
        "summary": summary,
    }
    report["markdown"] = render_report(course, report, link, node_paths, external=external)
    return report


def course_overview(db: Session, course: Course) -> dict[str, Any]:
    """轻量体检概览（任务 48z）：面板首屏先渲染这一段，重活随后由 course_report 补齐。

    这里**只调 course_report 里同样那几个函数**（覆盖度 / 链接健康 / 质检 / 用量 /
    导出健康），数字与完整报告逐字一致；唯一被跳过的是 ``kb.verify_course_notes``
    （逐项比对整个笔记库，AI 课实测 **4.33 秒**）⇒ ``consistency`` 与依赖它的
    ``conclusion`` / ``actions`` / ``summary`` / ``markdown`` 留空，由 GET report 提供。
    ``links`` 只花 0.08 秒，留着让第 4 段能一起首屏渲染。
    """
    return {
        "course_id": course.id,
        "course_title": course.title,
        "partial": True,
        "deferred": ["consistency", "conclusion", "actions", "summary", "markdown"],
        "coverage": coverage(db, course),
        "links": kb.course_link_health(db, course),
        "quality": lecture_qc.course_report(db, course, worst_n=WORST_N),
        "usage": analytics.course_usage(db, course.id, top_n=TOP_USAGE_N),
        "export_health": kb.course_asset_health(db, course),
        "consistency": None,
        "conclusion": None,
        "actions": [],
        "summary": "",
        "markdown": "",
    }


def write_report(db: Session, course: Course, *, external: bool = False) -> dict[str, Any]:
    """生成并写入 ``课程/<课名>/00-课程体检报告.md``（正文无时间戳，重复生成字节一致）。

    默认内部口径（真 vault 里那份能看到成本）；对外交付包**不改真 vault**，而是在打包
    复制笔记库时用 ``external=True`` 重渲染包内那份副本（任务 61，见 ``delivery``）。
    """
    from app.services.kb import KB_KEY, _course_out_dir, _now_iso, _write_safe, course_kb

    root, out_dir, rel = _course_out_dir(course)
    # 报告紧接着就写入 out_dir ⇒ 体检时把报告自身算作已存在的目标，
    # 数字与写盘后的磁盘状态一致（任务 60）。
    report = course_report(db, course, extra_link_targets=(REPORT_NAME,), external=external)
    kb_meta = course_kb(course)
    manifest = dict(kb_meta.get("manifest") or {})
    written: list[str] = []
    preserved: list[dict] = []
    _write_safe(out_dir, REPORT_NAME, report["markdown"], manifest, True, root, preserved, written)

    kb_meta["manifest"] = manifest
    kb_meta["audit_at"] = _now_iso()
    meta = dict(course.meta or {})
    meta[KB_KEY] = kb_meta
    course.meta = meta
    db.commit()
    return {**report, "written": True, "file": REPORT_NAME, "vault_path": str(root),
            "output_rel": rel, "files": written, "preserved": preserved}
