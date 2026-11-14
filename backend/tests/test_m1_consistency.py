"""任务 7（M1 一致性修复）回归测试：7a 讲义-笔记同步 / 7b 一致性校验 / 7c 失败上报。"""
from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime
from pathlib import Path

from app.db.session import SessionLocal
from app.models.course import ContentItem, Course
from app.services.kb import (
    _hash,
    _is_managed_asset,
    _skip_reason,
    file_fingerprint,
    incremental_write_content,
    preserved_entries,
    stamp_fingerprint,
    stamp_json,
    stamp_markdown,
    stamp_output,
    stamp_svg,
)
from app.services.lecture import _failure_reason, generate_all_lectures


def _bind(client, course_id: str, root: Path, output_rel: str = "课程/M1") -> None:
    resp = client.post(f"/api/kb/course/{course_id}/bind", json={
        "vault_root": str(root), "output_rel": output_rel,
    })
    assert resp.status_code == 200, resp.text


def _plan_approve(client, course_id: str) -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "生物光合作用", "num_chapters": 2,
    })
    assert resp.status_code == 200, resp.text
    approved = client.post("/api/plan/approve", json={
        "course_id": course_id, "approved": True})
    assert approved.status_code == 200, approved.text
    return resp.json()


def _lecture_row(client, course_id: str, node_id: str) -> dict:
    import time

    resp = client.post("/api/lecture/generate", json={
        "course_id": course_id, "node_id": node_id, "tone": "正式"})
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]
    state = None
    for _ in range(120):
        state = client.get(f"/api/content/jobs/{job_id}").json()
        if state["status"] in ("success", "failed"):
            break
        time.sleep(0.1)
    assert state is not None and state["status"] == "success", state
    return state["result"]["item"]


def _note_path(client, course_id: str, item_id: str, root: Path) -> Path:
    items = client.get(f"/api/content/{course_id}").json()
    lecture = next(i for i in items if i["id"] == item_id)
    vault = (lecture.get("meta") or {}).get("vault") or {}
    note = Path(vault["note_path"])
    assert note.exists(), vault
    assert note.is_relative_to(root), (note, root)
    return note


def _set_markdown(item_id: str, suffix: str) -> str:
    """给数据库讲义追加一段并返回其 node_id（模拟网页端改课）。"""
    db = SessionLocal()
    try:
        item = db.get(ContentItem, item_id)
        md = str((item.body or {}).get("markdown") or "")
        item.body = {**(item.body or {}), "markdown": md + suffix}
        db.commit()
        return item.node_id
    finally:
        db.close()


def _write_incremental(course_id: str, node_id: str) -> dict:
    db = SessionLocal()
    try:
        res = incremental_write_content(db, course_id, node_id, "lecture")
        db.commit()
        return res
    finally:
        db.close()


# --------------------------------------------------------------- 7a 单元级

def test_m1_7a_fingerprint_roundtrip_and_skip_reason():
    """正文指纹自洽：没被手改就能覆盖；被手改就报 manual-edit。"""
    md = stamp_fingerprint(["---", 'title: "x"', "---", "", "# 标题", "", "正文"])
    stored, actual = file_fingerprint(md)
    assert stored is not None and stored == actual
    assert md.count("cg_fingerprint") == 1

    assert _skip_reason(md.encode("utf-8"), None) is None  # 无 manifest 记录但文件自洽 → 可覆盖
    assert _skip_reason(md.encode("utf-8"), "0" * 40) is None  # manifest 漂移不再误判（7a 根因）
    assert _skip_reason(md.encode("utf-8"), _hash(md.encode("utf-8"))) is None

    edited = md + "\n教师手改\n"
    assert file_fingerprint(edited)[0] != file_fingerprint(edited)[1]
    assert _skip_reason(edited.encode("utf-8"), None) == "manual-edit"
    assert _skip_reason(edited.encode("utf-8"), _hash(edited.encode("utf-8"))) is None

    assert _skip_reason(b"# \xe6\xb2\xa1\xe6\x9c\x89 frontmatter", None) == "unknown-drift"
    assert _skip_reason(b"\xff\xfe\x00", None) == "unknown-drift"


def test_m1_b_managed_asset_without_fingerprint_is_machine_owned():
    """7-补-b：受管资产缺指纹 = 旧版产出（允许覆盖），手改过的资产仍受保护。"""
    rel = "课程/M1/图表库/图-chain-1-01.svg"
    assert _is_managed_asset(rel) and _is_managed_asset("课程/M1/图表库/图-1.png")
    assert _is_managed_asset("anything/图-1.excalidraw")
    assert not _is_managed_asset("课程/M1/01-第一章/1.1-x.md")
    assert not _is_managed_asset("")

    legacy_svg = "<svg><rect/></svg>\n"
    assert file_fingerprint(legacy_svg)[0] is None
    # 旧版导出留下：manifest 记的是「带指纹那份」，与磁盘上的旧文件必然不等
    assert _skip_reason(legacy_svg.encode("utf-8"), "0" * 40, rel) is None
    assert _skip_reason(legacy_svg.encode("utf-8"), None, rel) is None
    assert _skip_reason(b"\x89PNG\r\n\x1a\n", "0" * 40, "课程/M1/图表库/图-1.png") is None

    # 带指纹但被改过 → 仍然保护，不能因为是资产就静默覆盖
    edited = stamp_svg(legacy_svg).replace("</svg>", "<!-- 老师手改 --></svg>")
    assert _skip_reason(edited.encode("utf-8"), "0" * 40, rel) == "manual-edit"

    # 文本页（讲义/习题/概览）不在此列：旧版没有指纹仍保守跳过，交给显式修复
    assert _skip_reason("# 旧版讲义\n".encode("utf-8"), "0" * 40,
                        "课程/M1/01-第一章/1.1-x.md") == "unknown-drift"


def test_m1_7a_preserved_entries_normalizes_legacy_strings():
    """旧库里 preserved 是字符串，新结构是 dict：读取侧必须两种都吃得下。"""
    assert preserved_entries(None) == []
    assert preserved_entries(["a.md"]) == [{"file": "a.md", "reason": "unknown-drift"}]
    assert preserved_entries([{"file": "b.md", "reason": "manual-edit"}]) == [
        {"file": "b.md", "reason": "manual-edit"}]


# --------------------------------------------------------------- 7a 集成级

def test_m1_7a_manifest_drift_still_overwrites(client, course_id):
    """7a 根因回归：manifest 漂移时，未被手改的笔记必须照常被新内容覆盖。"""
    root = Path(tempfile.mkdtemp(prefix="coursegenie_m1_7a_drift_"))
    _bind(client, course_id, root)
    plan = _plan_approve(client, course_id)
    row = _lecture_row(client, course_id, plan["nodes"][0]["id"])
    note = _note_path(client, course_id, row["id"], root)
    rel = note.relative_to(root).as_posix()

    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        meta = dict(course.meta or {})
        kb = dict(meta.get("kb") or {})
        manifest = dict(kb.get("manifest") or {})
        assert rel in manifest, manifest.keys()
        manifest[rel] = "0" * 40  # 历史残留：既不是磁盘哈希也不是新内容哈希
        kb["manifest"] = manifest
        meta["kb"] = kb
        course.meta = meta
        db.commit()
    finally:
        db.close()

    node_id = _set_markdown(row["id"], "\n\n## M1 漂移回归小节")
    res = _write_incremental(course_id, node_id)
    assert res["ok"] is True, res
    assert res.get("preserved") == [], res
    assert "M1 漂移回归小节" in note.read_text(encoding="utf-8"), res


def test_m1_7a_manual_edit_reports_file_and_reason(client, course_id):
    """手改过的笔记：不覆盖，并在结果里给出「文件 + 原因」。"""
    root = Path(tempfile.mkdtemp(prefix="coursegenie_m1_7a_edit_"))
    _bind(client, course_id, root)
    plan = _plan_approve(client, course_id)
    row = _lecture_row(client, course_id, plan["nodes"][0]["id"])
    note = _note_path(client, course_id, row["id"], root)
    rel = note.relative_to(root).as_posix()

    note.write_bytes(note.read_bytes() + "\n<!-- teacher manual edit -->\n".encode("utf-8"))
    node_id = _set_markdown(row["id"], "\n\n## 网页端新增小节")

    res = _write_incremental(course_id, node_id)
    assert res["ok"] is True, res
    assert [(p["file"], p["reason"]) for p in res["preserved"]] == [(rel, "manual-edit")], res
    text = note.read_text(encoding="utf-8")
    assert "teacher manual edit" in text
    assert "网页端新增小节" not in text


def test_m1_7a_batch_reports_notes_skipped(client, course_id):
    """整门课批量生成：被跳过的笔记要出现在 notes_skipped（网页可见），而不是只进日志。"""
    root = Path(tempfile.mkdtemp(prefix="coursegenie_m1_7a_batch_"))
    _bind(client, course_id, root)
    plan = _plan_approve(client, course_id)
    row = _lecture_row(client, course_id, plan["nodes"][0]["id"])
    note = _note_path(client, course_id, row["id"], root)
    note.write_bytes(note.read_bytes() + "\n<!-- teacher manual edit -->\n".encode("utf-8"))

    db = SessionLocal()
    try:
        # 任务 23 起批量默认按输入指纹去重，这里要验「手改笔记被保留并上报」，故强制重生成
        result = generate_all_lectures(db, db.get(Course, course_id), tone="正式",
                                       force=True)
    finally:
        db.close()

    skipped = result["notes_skipped"]
    assert skipped, result
    assert any(s["file"].endswith(note.name) and s["reason"] == "manual-edit" for s in skipped), skipped
    assert all({"node_key", "file", "reason"} <= set(s) for s in skipped), skipped
    assert isinstance(result["diagram_failed"], list), result


# --------------------------------------------------------------- 7b 一致性校验

def _verify(client, course_id: str, node_ids: list[str] | None = None) -> dict:
    resp = client.post(f"/api/kb/course/{course_id}/verify", json={"node_ids": node_ids or []})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _rows_by_key(report: dict) -> dict:
    return {r["node_key"]: r for r in report["nodes"]}


def test_m1_7b_verify_clean_after_export(client, course_id):
    """刚导出/增量写过的笔记：一致性校验应为「一致」。"""
    root = Path(tempfile.mkdtemp(prefix="coursegenie_m1_7b_ok_"))
    _bind(client, course_id, root)
    plan = _plan_approve(client, course_id)
    row = _lecture_row(client, course_id, plan["nodes"][0]["id"])

    report = _verify(client, course_id)
    assert report["ok"] is True, report
    assert report["inconsistent"] == 0, report["nodes"]
    node_row = next(r for r in report["nodes"] if r["node_id"] == row["node_id"])
    assert node_row["status"] == "consistent", node_row
    assert node_row["fingerprint"] == "ok", node_row
    assert node_row["diff"] == [], node_row


def test_m1_7b_verify_detects_manual_edit_and_repairs(client, course_id):
    """手改笔记 → 校验报不一致并给差异摘要；修复后恢复一致。"""
    root = Path(tempfile.mkdtemp(prefix="coursegenie_m1_7b_edit_"))
    _bind(client, course_id, root)
    plan = _plan_approve(client, course_id)
    row = _lecture_row(client, course_id, plan["nodes"][0]["id"])
    note = _note_path(client, course_id, row["id"], root)
    note.write_bytes(note.read_bytes() + "\n## 老师额外加的一段\n".encode("utf-8"))

    report = _verify(client, course_id)
    node_row = next(r for r in report["nodes"] if r["node_id"] == row["node_id"])
    assert node_row["status"] == "mismatch", node_row
    assert node_row["diff"] and "正文文本不一致" in node_row["diff"][0], node_row
    assert report["inconsistent"] >= 1, report

    resp = client.post(f"/api/kb/course/{course_id}/repair", json={"node_ids": [row["node_id"]]})
    assert resp.status_code == 200, resp.text
    after = resp.json()
    assert after["repaired"], after
    assert "老师额外加的一段" not in note.read_text(encoding="utf-8")
    assert after["after"]["inconsistent"] == 0, after["after"]["nodes"]


def test_m1_7b_verify_detects_unsynced_regeneration(client, course_id):
    """网页端重生成但笔记未同步 → 校验能指出「笔记旧了」，修复后笔记与网页一致。"""
    root = Path(tempfile.mkdtemp(prefix="coursegenie_m1_7b_stale_"))
    _bind(client, course_id, root)
    plan = _plan_approve(client, course_id)
    row = _lecture_row(client, course_id, plan["nodes"][0]["id"])
    note = _note_path(client, course_id, row["id"], root)

    # 只改数据库（模拟生成后笔记没跟上），不写盘
    _set_markdown(row["id"], "\n\n## 只存在于网页端的新小节")

    report = _verify(client, course_id)
    node_row = next(r for r in report["nodes"] if r["node_id"] == row["node_id"])
    assert node_row["status"] == "mismatch", node_row
    assert "只存在于网页端" not in note.read_text(encoding="utf-8")

    resp = client.post(f"/api/kb/course/{course_id}/repair", json={"node_ids": [row["node_id"]]})
    assert resp.status_code == 200, resp.text
    assert "只存在于网页端的新小节" in note.read_text(encoding="utf-8")
    assert resp.json()["after"]["inconsistent"] == 0, resp.json()["after"]["nodes"]


def test_m1_7b_verify_reports_missing_note(client, course_id):
    """笔记文件被删掉 → 校验报 missing，而不是崩溃。"""
    root = Path(tempfile.mkdtemp(prefix="coursegenie_m1_7b_missing_"))
    _bind(client, course_id, root)
    plan = _plan_approve(client, course_id)
    row = _lecture_row(client, course_id, plan["nodes"][0]["id"])
    note = _note_path(client, course_id, row["id"], root)
    note.unlink()

    report = _verify(client, course_id)
    node_row = next(r for r in report["nodes"] if r["node_id"] == row["node_id"])
    assert node_row["status"] == "missing", node_row
    assert node_row["diff"], node_row


def test_m1_7b_verify_without_vault_binding_reports_reason(client, course_id, no_global_vault):
    """未绑定笔记库：校验返回 ok=False + 原因（前端据此提示去绑定）。"""
    report = _verify(client, course_id)
    assert report["ok"] is False and report["reason"], report
    assert report["checked"] == 0, report


# --------------------------------------------------------------- 7c 失败显式上报

CONCEPT_REF = "![一图看懂](diagram://"

def test_m1_7c_failure_reason_classification():
    """失败原因要能分类到 layout / llm-json / network / other。"""
    import json

    import httpx

    assert _failure_reason(RuntimeError("排版自检未通过：节点重叠"))[0] == "layout"
    assert _failure_reason(json.JSONDecodeError("Expecting value", "{", 0))[0] == "llm-json"
    assert _failure_reason(RuntimeError("LLM 返回的 JSON 无法解析"))[0] == "llm-json"
    assert _failure_reason(httpx.ConnectError("connection refused"))[0] == "network"
    assert _failure_reason(ValueError("说不清的问题")) == ("other", "说不清的问题")


def test_m1_7c_concept_failure_recorded_and_retry_succeeds(client, course_id, monkeypatch):
    """概念图失败：落库 + 进 job 结果；换回正常链路点重试即可成功并清掉记录。"""
    import app.services.lecture as lecture

    root = Path(tempfile.mkdtemp(prefix="coursegenie_m1_7c_retry_"))
    _bind(client, course_id, root)
    plan = _plan_approve(client, course_id)
    node_id = plan["nodes"][0]["id"]

    def _boom(*_args, **_kwargs):
        raise RuntimeError("排版自检未通过：文字溢出画布")

    monkeypatch.setattr(lecture, "generate_diagrams", _boom)
    resp = client.post("/api/lecture/generate", json={
        "course_id": course_id, "node_id": node_id, "tone": "正式"})
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]
    import time

    state = None
    for _ in range(120):
        state = client.get(f"/api/content/jobs/{job_id}").json()
        if state["status"] in ("success", "failed"):
            break
        time.sleep(0.1)
    assert state["status"] == "success", state
    result = state["result"]
    assert result["diagram_failed"], result
    failure = result["diagram_failed"][0]
    assert failure["stage"] == "layout" and failure["reason"], failure
    assert failure["slot"] == "concept" and failure["failure_id"], failure

    item = result["item"]
    failures = (item["meta"] or {}).get("diagram_failures") or []
    assert failures and failures[0]["retry"]["kind"] == "concept", failures
    assert CONCEPT_REF not in str(item["body"]["markdown"]), item["body"]

    monkeypatch.undo()
    retry = client.post(f"/api/lecture/{item['id']}/failures/{failure['failure_id']}/retry")
    assert retry.status_code == 200, retry.text
    data = retry.json()
    assert data["ok"] is True, data
    assert (data["meta"].get("diagram_failures") or []) == [], data["meta"]
    assert CONCEPT_REF in str(data["body"]["markdown"]), data["body"]


def test_m1_7c_batch_reports_diagram_failed(client, course_id, monkeypatch):
    """整门课批量生成：失败清单带 node_key/slot/stage/reason，供任务面板展示。"""
    import app.services.lecture as lecture

    root = Path(tempfile.mkdtemp(prefix="coursegenie_m1_7c_batch_"))
    _bind(client, course_id, root)
    _plan_approve(client, course_id)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("LLM 返回的 JSON 无法解析：DIAGRAM_JSON 非法")

    monkeypatch.setattr(lecture, "generate_diagrams", _boom)
    db = SessionLocal()
    try:
        result = generate_all_lectures(db, db.get(Course, course_id), tone="正式")
    finally:
        db.close()

    assert result["diagram_failed"], result
    row = result["diagram_failed"][0]
    assert set(row) == {"node_key", "failure_id", "slot", "stage", "reason"}, row
    assert row["stage"] == "llm-json" and row["slot"] == "concept", row


# --------------------------------------------------------------- 7d 顶部统计口径

def test_m1_7d_preview_counts_come_from_data(client, course_id):
    """7d：顶部六个数字都能从数据算出——概念图/数据图/位图/作业按 kind，流程图按讲义内 mermaid。"""
    root = Path(tempfile.mkdtemp(prefix="coursegenie_m1_7d_"))
    _bind(client, course_id, root)
    plan = _plan_approve(client, course_id)
    _lecture_row(client, course_id, plan["nodes"][0]["id"])

    rows = client.get(f"/api/content/{course_id}").json()
    by_kind = {}
    for row in rows:
        by_kind.setdefault(row["kind"], []).append(row)

    lectures = by_kind.get("lecture", [])
    diagrams = by_kind.get("diagram", [])
    charts = [r for r in rows if r["kind"].startswith("chart_")]
    assert lectures, "应有讲义行"
    assert diagrams, "每篇讲义应自带一张概念图（kind=diagram）"
    assert charts, "概念卡分流应产出 chart_* 数据图"
    assert by_kind.get("image", []) == [], "零 AI 位图策略：kind=image 必须为 0"

    # 流程图 = 讲义正文里的 mermaid 代码块数（前端 totalMermaid 的口径）
    total_mermaid = sum(
        str((r.get("body") or {}).get("markdown") or "").count("```mermaid") for r in lectures
    )
    assert total_mermaid >= 0, total_mermaid

    for lecture in lectures:
        markdown = str((lecture.get("body") or {}).get("markdown") or "")
        assert "gen://" not in markdown, "AI 位图占位应被剥离，不计入位图口径"


# --------------------------------------------------------------- 7e 同步状态口径

def test_m1_7e_export_status_reflects_backend(client, course_id):
    """7e：标题旁的状态要有真实依据——没写过就「尚未导出」，写过就带时间戳。"""
    root = Path(tempfile.mkdtemp(prefix="coursegenie_m1_7e_"))
    _bind(client, course_id, root)
    plan = _plan_approve(client, course_id)

    kb = client.get(f"/api/kb/course/{course_id}").json()["kb"]
    assert not kb.get("exported_at") and not kb.get("synced_at"), kb
    assert not kb.get("manifest"), kb

    _lecture_row(client, course_id, plan["nodes"][0]["id"])
    kb = client.get(f"/api/kb/course/{course_id}").json()["kb"]
    assert kb.get("manifest"), kb
    assert kb.get("synced_at"), kb  # 生成后增量写笔记 → 记录同步时间
    datetime.fromisoformat(str(kb["synced_at"]).replace("Z", "+00:00"))

    client.post(f"/api/kb/course/{course_id}/export")
    kb = client.get(f"/api/kb/course/{course_id}").json()["kb"]
    assert kb.get("exported_at"), kb
    datetime.fromisoformat(str(kb["exported_at"]).replace("Z", "+00:00"))


# ------------------------------------------------- 7-补 指纹覆盖扩展（习题/概览/资产）

def _frontmatter_of(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    head = re.split(r"\r?\n---\r?\n", text, maxsplit=1)[0]
    assert head.startswith("---"), text[:60]
    return head


def test_m1_supp_stamp_is_idempotent_for_every_format():
    """md / svg / excalidraw 三种载体都能盖章，且重复盖章不叠加、不漂移。"""
    md = stamp_markdown('---\ntitle: "x"\n---\n\n# 标题\n\n正文\n')
    assert md.count("cg_fingerprint") == 1
    assert stamp_markdown(md) == md
    assert file_fingerprint(md)[0] == file_fingerprint(md)[1]

    svg = stamp_svg("<svg><rect/></svg>\n")
    assert file_fingerprint(svg)[0] == file_fingerprint(svg)[1]
    assert stamp_svg(svg) == svg

    doc = '{"type": "excalidraw", "elements": [], "version": 2}'
    js = stamp_json(doc)
    assert file_fingerprint(js)[0] == file_fingerprint(js)[1]
    assert stamp_json(js) == js
    assert json.loads(js)["elements"] == [] and json.loads(js)["version"] == 2

    # 无 frontmatter 的 md 不硬塞指纹（避免插到正文里）；未知后缀原样返回
    assert stamp_markdown("# 只有正文\n") == "# 只有正文\n"
    assert stamp_output("x.bin", "abc") == "abc"


def test_m1_supp_all_outputs_carry_fingerprint(client, course_id):
    """导出后：习题页、00-概览、素材页、图表库 svg/excalidraw 全部带版本指纹。"""
    root = Path(tempfile.mkdtemp(prefix="coursegenie_m1_supp_all_"))
    _bind(client, course_id, root)
    plan = _plan_approve(client, course_id)
    _lecture_row(client, course_id, plan["nodes"][0]["id"])
    upload = client.post("/api/material/upload", data={"course_id": course_id},
                         files={"file": ("素材.txt", "光合作用的原始资料".encode("utf-8"), "text/plain")})
    assert upload.status_code == 200, upload.text
    assert client.post(f"/api/kb/course/{course_id}/export").status_code == 200

    out = root / "课程/M1"
    quiz_pages = list((out / "习题集").glob("*.md"))
    material_pages = list((out / "素材库").glob("*.md"))
    assert quiz_pages, "应有习题页"
    assert material_pages, "应有素材页"
    for page in [*quiz_pages, *material_pages, out / "00-课程概览.md", out / "00-课程级内容.md"]:
        assert "cg_fingerprint" in _frontmatter_of(page), page

    assets = sorted((out / "图表库").glob("*.excalidraw")) + sorted((out / "图表库").glob("*.svg"))
    assert assets, "应有图表库资产"
    for asset in assets:
        stored, actual = file_fingerprint(asset.read_text(encoding="utf-8"))
        assert stored is not None, f"{asset.name} 没有版本指纹"
        assert stored == actual, f"{asset.name} 指纹不自洽"


def test_m1_supp_verify_covers_quiz_index_and_materials(client, course_id):
    """verify 的覆盖范围要包含习题页/概览页/素材页（7-补 ③）。"""
    root = Path(tempfile.mkdtemp(prefix="coursegenie_m1_supp_parts_"))
    _bind(client, course_id, root)
    plan = _plan_approve(client, course_id)
    _lecture_row(client, course_id, plan["nodes"][0]["id"])
    client.post("/api/material/upload", data={"course_id": course_id},
                files={"file": ("素材.txt", "原始资料".encode("utf-8"), "text/plain")})
    client.post(f"/api/kb/course/{course_id}/export")

    report = _verify(client, course_id)
    parts = {r["part"] for r in report["nodes"]}
    assert {"lecture", "quiz", "index", "course-level", "material"} <= parts, parts
    assert report["inconsistent"] == 0, [r for r in report["nodes"] if r["status"] != "consistent"]

    # 资产一致时不占行；一旦有问题（这里删掉一个 svg）必须能报出来
    assets = sorted((root / "课程/M1" / "图表库").glob("*.svg"))
    assert assets, "应有图表库资产"
    assets[0].unlink()
    report = _verify(client, course_id)
    asset_rows = [r for r in report["nodes"] if r["part"] == "asset"]
    assert asset_rows and asset_rows[0]["status"] == "mismatch", report["nodes"]
    assert "缺失" in " ".join(asset_rows[0]["diff"]), asset_rows[0]


def test_m1_supp_old_and_edited_assets_are_reported_and_repairable(client, course_id):
    """旧版资产（无指纹）与手改资产 → verify 能报；repair 后盖章并恢复一致。"""
    root = Path(tempfile.mkdtemp(prefix="coursegenie_m1_supp_asset_"))
    _bind(client, course_id, root)
    plan = _plan_approve(client, course_id)
    row = _lecture_row(client, course_id, plan["nodes"][0]["id"])
    client.post(f"/api/kb/course/{course_id}/export")

    out = root / "课程/M1"
    quiz_page = next((out / "习题集").glob("*.md"))
    svg = sorted((out / "图表库").glob("*.svg"))[0]
    # 旧版：把习题页的指纹行抹掉（模拟 09-08 之前写的文件）
    quiz_page.write_text(re.sub(r"^cg_fingerprint:.*\n", "", quiz_page.read_text(encoding="utf-8"),
                                flags=re.M), encoding="utf-8")
    # 手改：动 SVG 的正文但保留指纹
    svg.write_text(svg.read_text(encoding="utf-8").replace("</svg>", "<!-- 老师手改 --></svg>"),
                   encoding="utf-8")

    report = _verify(client, course_id)
    rows = {(r["part"], r["note"]): r for r in report["nodes"]}
    quiz_rel = quiz_page.relative_to(out).as_posix()
    svg_rel = svg.relative_to(out).as_posix()
    assert rows[("quiz", quiz_rel)]["status"] == "mismatch", rows[("quiz", quiz_rel)]
    assert rows[("quiz", quiz_rel)]["fingerprint"] == "absent"
    assert rows[("asset", svg_rel)]["status"] == "mismatch", rows[("asset", svg_rel)]
    assert "被改动过" in " ".join(rows[("asset", svg_rel)]["diff"]), rows[("asset", svg_rel)]

    resp = client.post(f"/api/kb/course/{course_id}/repair", json={"node_ids": []})
    assert resp.status_code == 200, resp.text
    assert "cg_fingerprint" in _frontmatter_of(quiz_page), "习题页修复后应补上指纹"
    assert "老师手改" not in svg.read_text(encoding="utf-8"), "资产修复应覆盖手改内容"
    # 节点范围修复不含课程级页面，这里显式跑整课导出把 00-概览 等一并补齐
    client.post(f"/api/kb/course/{course_id}/export")
    assert _verify(client, course_id)["inconsistent"] == 0, _verify(client, course_id)["nodes"]
    assert row["id"]


def test_m1_b_legacy_asset_is_refreshed_on_regenerate(client, course_id):
    """7-补-b 回归：旧版资产（无指纹）+ manifest 记录过期 + 内容变化 → 重生成要能正常更新。"""
    root = Path(tempfile.mkdtemp(prefix="coursegenie_m1_b_asset_"))
    _bind(client, course_id, root)
    plan = _plan_approve(client, course_id)
    row = _lecture_row(client, course_id, plan["nodes"][0]["id"])
    out = root / "课程/M1"

    svg = sorted((out / "图表库").glob("*.svg"))[0]
    rel = svg.relative_to(root).as_posix()
    stamped_text = svg.read_text(encoding="utf-8")
    assert file_fingerprint(stamped_text)[0] is not None
    # 旧版导出留下的文件：抹掉指纹标记；DB manifest 里仍是「带指纹那份」的哈希
    legacy_text = re.sub(r"<!-- cg_fingerprint:.*?-->\r?\n", "", stamped_text)
    svg.write_text(legacy_text, encoding="utf-8")
    assert file_fingerprint(svg.read_text(encoding="utf-8"))[0] is None

    # 网页端改课（内容变化）→ 重生成该知识点：资产必须被刷新，不能永久 unknown-drift
    _set_markdown(row["id"], "\n\n```mermaid\ngraph TD\n  A --> B\n```\n")
    result = _write_incremental(course_id, row["node_id"])

    assert rel not in [entry["file"] for entry in result["preserved"]], result["preserved"]
    assert any(str(f).endswith(svg.name) for f in result["files"]), result["files"]
    updated = svg.read_text(encoding="utf-8")
    assert updated != legacy_text, "旧版资产应被刷新"
    stored, actual = file_fingerprint(updated)
    assert stored is not None and stored == actual, updated[-120:]


def test_m1_supp_legacy_quiz_skipped_and_asset_refreshed(client, course_id):
    """旧版习题页（文本）仍保守跳过并显式列出；旧版图表资产则被刷新（7-补-b）。"""
    root = Path(tempfile.mkdtemp(prefix="coursegenie_m1_supp_skip_"))
    _bind(client, course_id, root)
    plan = _plan_approve(client, course_id)
    _lecture_row(client, course_id, plan["nodes"][0]["id"])

    out = root / "课程/M1"
    for page in (out / "习题集").glob("*.md"):
        page.write_text(re.sub(r"^cg_fingerprint:.*\r?\n", "", page.read_text(encoding="utf-8"),
                               flags=re.M), encoding="utf-8")
    legacy_assets = list((out / "图表库").glob("*.svg"))[:2]
    for asset in legacy_assets:
        asset.write_text(re.sub(r"<!-- cg_fingerprint:.*?-->\r?\n", "",
                                asset.read_text(encoding="utf-8")), encoding="utf-8")

    db = SessionLocal()
    try:
        # 任务 23 起批量默认按输入指纹去重（旧页资产需重生成才会被刷新），故强制重生成
        result = generate_all_lectures(db, db.get(Course, course_id), tone="正式",
                                       force=True)
    finally:
        db.close()

    skipped = result["notes_skipped"]
    assert skipped, result
    unknown = [s for s in skipped if s["reason"] == "unknown-drift"]
    assert any(s["file"].endswith("-习题.md") for s in unknown), skipped
    assert all(s["reason"] in {"manual-edit", "unknown-drift"} for s in skipped), skipped
    # 资产不再因为没有指纹被判 unknown-drift：重生成后应被刷新并补上指纹
    assert not [s for s in skipped if s["file"].endswith(".svg")], skipped
    for asset in legacy_assets:
        stored, actual = file_fingerprint(asset.read_text(encoding="utf-8"))
        assert stored is not None and stored == actual, asset
