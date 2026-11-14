# -*- coding: utf-8 -*-
"""导出收尾清理：增量写入留下的孤儿图表应移入 99_归档（不删除、可拖回）。"""
from __future__ import annotations

import tempfile
from pathlib import Path

from app.db.session import SessionLocal
from app.models.course import ContentItem, Course
from app.services import kb as kb_service

CHARTS = "图表库"
REPORT = "00-课程体检报告.md"


def _root() -> Path:
    return Path(tempfile.mkdtemp(prefix="coursegenie_prune_"))


def _bind(client, course_id: str, root: Path) -> None:
    resp = client.post(f"/api/kb/course/{course_id}/bind", json={
        "vault_root": str(root), "output_rel": "课程/清理课",
    })
    assert resp.status_code == 200, resp.text


def _plan_approve(client, course_id: str) -> dict:
    plan = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "初中生物光合作用", "num_chapters": 1,
    })
    assert plan.status_code == 200, plan.text
    assert client.post("/api/plan/approve", json={
        "course_id": course_id, "approved": True}).status_code == 200
    return plan.json()


def _add_deck(course_id: str, src: Path) -> None:
    """落一条整课课件记录（kind=ppt）：导出会把它写成 `课件/00-整课课件.pptx`。"""
    db = SessionLocal()
    try:
        db.add(ContentItem(course_id=course_id, kind="ppt", title="整课课件",
                           body={"pptx": str(src)}, meta={}))
        db.commit()
    finally:
        db.close()


# 报告里的数字（P9-1 验收：磁盘报告与重新体检逐项相等）
_REPORT_FIELDS = {
    "score": r"\*\*总分 (\d+) / 100",
    "orphans": r"孤儿文件 \*\*(\d+)\*\* 个",
    "preserved": r"会被保留（疑似手改）的文件 \*\*(\d+)\*\* 个",
    "lectures": r"\| 完整讲义 \| (\d+)",
    "concepts": r"\| 概念 \| (\d+) \|",
    "diagrams": r"\| 概念图/流程图（条目） \| (\d+) \|",
    "charts": r"\| 数据图表（条目） \| (\d+) \|",
    "files": r"课程目录共 (\d+) 个文件",
    "links": r"= \*\*(\d+) 条\*\*",
    "broken": r"坏链 \*\*(\d+) 条\*\*",
    "quality_avg": r"平均分 \*\*([\d.]+)\*\*",
    "tokens": r"累计 tokens \*\*(\d+)\*\*",
    "llm_calls": r"- LLM 调用 (\d+) 次",
}


def _report_numbers(text: str) -> dict:
    import re

    out: dict[str, str] = {}
    for key, pattern in _REPORT_FIELDS.items():
        match = re.search(pattern, text)
        if match:
            out[key] = match.group(1)
    return out


def _expect_numbers(text: str, fresh: dict) -> None:
    """磁盘报告里的每个数字都要等于重新体检的同一项（逐项相等）。"""
    got = _report_numbers(text)
    cover, health, quality, usage = (fresh["coverage"], fresh["export_health"],
                                     fresh["quality"], fresh["usage"])
    want = {
        "score": str(fresh["conclusion"]["score"]),
        "orphans": str(health["orphan_count"]),
        "preserved": str(health["preserved_count"]),
        "lectures": str(cover["lectures"]),
        "concepts": str(cover["concepts"]),
        "diagrams": str(cover["diagrams"]),
        "charts": str(cover["charts"]),
        "files": str(health["files"]),
        "links": str(fresh["links"]["links"]),
        "broken": str(fresh["links"]["broken"]),
        "quality_avg": str(quality["average_score"]),
        "tokens": str(usage["total_tokens"]),
        "llm_calls": str(usage["llm_calls"]),
    }
    for key, expected in want.items():
        assert got.get(key) == expected, f"报告 {key}：磁盘 {got.get(key)} vs 现算 {expected}"


def _leaf(plan: dict) -> dict:
    return next((n for n in plan["nodes"] if "." in n["key"]), plan["nodes"][0])


def _files(charts_dir: Path) -> set[str]:
    return {p.name for p in charts_dir.iterdir() if p.is_file()}


def _chart_names(pruned: list[dict]) -> set[str]:
    """本次清理里属于 `图表库/` 的文件名（清理也会收走失效的笔记页，这里只数图）。"""
    return {Path(p["file"]).name for p in pruned if f"/{CHARTS}/" in p["file"]}


def _teach_then_drop(client, course_id, gen_lecture_row) -> tuple[Path, dict, set[str]]:
    """建课 → 生成讲义 → 导出 → 删讲义（其概念图/数据图连带从库里删掉，磁盘图成孤儿）。"""
    root = _root()
    _bind(client, course_id, root)
    plan = _plan_approve(client, course_id)
    node = _leaf(plan)
    lecture = gen_lecture_row(course_id, node["id"])

    first = client.post(f"/api/kb/course/{course_id}/export?force=true").json()
    out = root / first["output_rel"]
    before = _files(out / "图表库")
    assert any(n.startswith("图-") for n in before), f"讲义应产出图表库文件：{sorted(before)}"

    assert client.delete(f"/api/lecture/{lecture['id']}").status_code == 200
    return out, first, before


def test_export_prunes_orphan_chart_files(client, course_id, gen_lecture_row):
    """删条目后没人引用的旧图应被移入 99_归档，仍在用的图要留着。"""
    out, first, before = _teach_then_drop(client, course_id, gen_lecture_row)
    charts_dir = out / "图表库"

    second = client.post(f"/api/kb/course/{course_id}/export?force=true").json()
    pruned = _chart_names(second["pruned"])
    assert pruned, "应清理孤儿图表"
    assert pruned <= before, "清理的应是本课图表库里的文件"
    after = _files(charts_dir)
    assert len(after) == len(before) - len(pruned), (sorted(before), sorted(after), pruned)
    for entry in second["pruned"]:
        dest = Path(entry["archived_to"])
        assert dest.exists(), entry
        assert "_待整理_" in dest.as_posix(), entry
        assert not (Path(first["vault_root"]) / entry["file"]).exists(), "原位置不应再留着"


def test_export_prune_can_be_disabled(client, course_id, gen_lecture_row):
    """prune=false 时不做清理（孤儿留在原地），下次默认导出再收。"""
    out, first, before = _teach_then_drop(client, course_id, gen_lecture_row)

    kept = client.post(f"/api/kb/course/{course_id}/export?force=true&prune=false").json()
    assert kept["pruned"] == []
    assert _files(out / "图表库") == before

    again = client.post(f"/api/kb/course/{course_id}/export?force=true").json()
    assert again["pruned"], "恢复默认后应把孤儿收走"
    assert len(_files(out / "图表库")) == len(before) - len(_chart_names(again["pruned"]))


def test_export_report_is_written_after_prune(client, course_id, gen_lecture_row, tmp_path):
    """P9-1：体检报告在「清理孤儿 + 写回清单」**之后**生成，磁盘报告 == 重新体检。

    回归场景（两处都踩在收尾顺序上）：
    ① 本次导出重写的 `课件/*.pptx` 是二进制、没有内嵌指纹，清单若还是上一版，
       它会被判成「疑似手改」进 preserved；
    ② 上一轮留下的孤儿此刻还没被清理，会被算进「孤儿文件」。
    报告早一步生成 → 同一门课同一时刻在磁盘与网页端出现两个数字。
    """
    root = _root()
    _bind(client, course_id, root)
    plan = _plan_approve(client, course_id)
    node = _leaf(plan)
    gen_lecture_row(course_id, node["id"])

    deck = tmp_path / "整课课件.pptx"
    deck.write_bytes(b"PK\x03\x04 first")
    _add_deck(course_id, deck)

    first = client.post(f"/api/kb/course/{course_id}/export?force=true").json()
    out = root / first["output_rel"]
    assert (out / "课件" / "00-整课课件.pptx").exists()

    # 下一轮导出前：课件字节变了（与清单对不上）＋ 多出一个没人引用的图（孤儿）
    deck.write_bytes(b"PK\x03\x04 second")
    (out / CHARTS / "图-孤儿测试.svg").write_text("<svg/>", encoding="utf-8")

    second = client.post(f"/api/kb/course/{course_id}/export?force=true").json()
    assert any(p["file"].endswith("图-孤儿测试.svg") for p in second["pruned"]), second["pruned"]
    assert not any(p["file"].endswith(REPORT) for p in second["pruned"]), \
        "报告每轮都重写、不是孤儿，不该被收进 99_归档"

    text = (out / REPORT).read_text(encoding="utf-8")
    fresh = client.get(f"/api/audit/course/{course_id}/report").json()
    _expect_numbers(text, fresh)
    assert fresh["export_health"]["orphan_count"] == 0, fresh["export_health"]
    assert fresh["export_health"]["preserved_count"] == 0, fresh["export_health"]

    # 幂等：重新生成一次，磁盘那份字节不变（数字不会漂）
    client.post(f"/api/audit/course/{course_id}/report")
    assert (out / REPORT).read_text(encoding="utf-8") == text


def test_first_export_report_has_no_self_inflicted_broken_link(client, course_id,
                                                              gen_lecture_row):
    """任务 60：首次导出（报告自己还没落盘）也不许把「索引 → 报告」报成坏链。

    报告正文里写着链接体检结果，而报告是**最后一个**落盘的文件；48s 把课程级页面
    接进索引后，只导一次的全新笔记库里必现「坏链 1 条 / 总分 −20」的假阳性，客户
    在交付包里直接看得到。这条同时锁住「报告数字 == 现算」。
    """
    root = _root()
    _bind(client, course_id, root)
    plan = _plan_approve(client, course_id)
    gen_lecture_row(course_id, _leaf(plan)["id"])

    first = client.post(f"/api/kb/course/{course_id}/export?force=true").json()
    out = root / first["output_rel"]
    text = (out / REPORT).read_text(encoding="utf-8")
    fresh = client.get(f"/api/audit/course/{course_id}/report").json()

    assert fresh["links"]["broken"] == 0, fresh["links"]
    assert "坏链 **0 条** ✅" in text, text.splitlines()[:12]
    _expect_numbers(text, fresh)


def test_report_still_reports_real_broken_link(client, course_id, gen_lecture_row):
    """任务 60 反例：真坏链（老师手工页链到不存在的目标）仍要报出、仍要扣分。"""
    root = _root()
    _bind(client, course_id, root)
    plan = _plan_approve(client, course_id)
    gen_lecture_row(course_id, _leaf(plan)["id"])

    first = client.post(f"/api/kb/course/{course_id}/export?force=true").json()
    out = root / first["output_rel"]
    before = int(_report_numbers((out / REPORT).read_text(encoding="utf-8"))["score"])

    (out / "我的补充笔记.md").write_text("# 补充\n\n见 [[不存在-xyz]]。\n", encoding="utf-8")
    client.post(f"/api/audit/course/{course_id}/report")

    text = (out / REPORT).read_text(encoding="utf-8")
    fresh = client.get(f"/api/audit/course/{course_id}/report").json()
    assert fresh["links"]["broken"] == 1, fresh["links"]
    assert "不存在-xyz" in text
    assert int(_report_numbers(text)["score"]) == before - 20
    _expect_numbers(text, fresh)


def test_force_write_report_has_no_fake_broken_link(client, course_id, gen_lecture_row):
    """任务 60：强制刷新课程级页面（repair 路径）时，报告报自身也不算坏链。"""
    root = _root()
    _bind(client, course_id, root)
    plan = _plan_approve(client, course_id)
    gen_lecture_row(course_id, _leaf(plan)["id"])

    first = client.post(f"/api/kb/course/{course_id}/export?force=true").json()
    out = root / first["output_rel"]
    (out / REPORT).unlink()  # 模拟「报告此刻还没落盘」，强制刷新时重写

    db = SessionLocal()
    try:
        kb_service.force_write_course_files(db, db.get(Course, course_id))
    finally:
        db.close()

    text = (out / REPORT).read_text(encoding="utf-8")
    fresh = client.get(f"/api/audit/course/{course_id}/report").json()
    assert fresh["links"]["broken"] == 0, fresh["links"]
    assert "坏链 **0 条** ✅" in text
    _expect_numbers(text, fresh)


def test_export_keeps_teacher_files_and_manual_edits(client, course_id, gen_lecture_row):
    """教师自己的文件、以及手改过的机器页都不会被清理。"""
    root = _root()
    _bind(client, course_id, root)
    plan = _plan_approve(client, course_id)
    node = _leaf(plan)
    gen_lecture_row(course_id, node["id"])
    first = client.post(f"/api/kb/course/{course_id}/export?force=true").json()
    out = root / first["output_rel"]

    mine = out / "我的补充笔记.md"
    mine.write_text("# 我的补充\n上课时给学生补的实验步骤。\n", encoding="utf-8")
    page = next(p for p in out.rglob("*.md") if p.name.startswith(node["key"] + "-"))
    page.write_text(page.read_text(encoding="utf-8") + "\n<!-- teacher manual edit -->\n",
                    encoding="utf-8")

    second = client.post(f"/api/kb/course/{course_id}/export").json()
    pruned = {Path(p["file"]).name for p in second["pruned"]}
    assert mine.exists(), "教师自己放的文件不应被清理"
    assert mine.name not in pruned
    assert page.exists() and "teacher manual edit" in page.read_text(encoding="utf-8")
    assert page.name not in pruned
    assert any(p["file"].endswith(page.name) for p in second["preserved"])
