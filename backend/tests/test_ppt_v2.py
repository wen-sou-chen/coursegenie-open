"""M4-PPT v2 测试：页级大纲预览、PDF 回退、课件落地笔记库（受管区/坏链/幂等）。"""
from __future__ import annotations

import io
import re
import tempfile
import time
from pathlib import Path


def _plan_and_approve(client, course_id: str, num_chapters: int = 2) -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "让初中生理解光合作用", "num_chapters": num_chapters,
    })
    assert resp.status_code == 200, resp.text
    assert client.post("/api/plan/approve",
                       json={"course_id": course_id, "approved": True}).status_code == 200
    return client.get(f"/api/plan/{course_id}").json()


def _chapters(client, course_id: str) -> list[dict]:
    nodes = client.get(f"/api/plan/{course_id}").json()["nodes"]
    return [n for n in nodes if n["type"] in ("chapter", "section")] or nodes


def _wait(client, job_id: str, timeout: float = 30.0) -> dict:
    state: dict | None = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = client.get(f"/api/ppt/jobs/{job_id}").json()
        if state["status"] in ("success", "failed"):
            return state
        time.sleep(0.1)
    raise AssertionError(f"任务超时未结束：{state}")


def _gen(client, course_id: str, **payload) -> dict:
    resp = client.post("/api/ppt/generate", json={"course_id": course_id, **payload})
    assert resp.status_code == 200, resp.text
    return _wait(client, resp.json()["job_id"])


def _decks(client, course_id: str) -> dict:
    return client.get(f"/api/ppt/{course_id}").json()


def _bind(client, course_id: str, rel: str = "课程/光合作用") -> Path:
    root = Path(tempfile.mkdtemp(prefix="coursegenie_ppt_v2_"))
    resp = client.post(f"/api/kb/course/{course_id}/bind",
                       json={"vault_root": str(root), "output_rel": rel})
    assert resp.status_code == 200, resp.text
    return root


def _export(client, course_id: str) -> dict:
    resp = client.post(f"/api/kb/course/{course_id}/export")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _slide_titles(pptx_path: str) -> list[str]:
    """用 python-pptx 独立读回标题（不复用服务端读回的 meta.slides）。"""
    from pptx import Presentation

    prs = Presentation(pptx_path)
    return [(s.shapes.title.text if s.shapes.title is not None else "").strip()
            for s in prs.slides]


def test_meta_slides_match_actual_pptx(client, course_id):
    """① meta.slides 的页数与标题与磁盘上的 pptx 实际一致（整课 + 每章）。"""
    from app.services.ppt import read_deck_slides

    _plan_and_approve(client, course_id, num_chapters=2)
    assert _gen(client, course_id, scope="course")["status"] == "success"
    node = _chapters(client, course_id)[0]
    assert _gen(client, course_id, scope="chapter",
                chapter_node_id=node["id"])["status"] == "success"

    decks = _decks(client, course_id)["items"]
    assert len(decks) == 2
    for deck in decks:
        slides = deck["slides"]
        assert slides, deck["title"]
        assert len(slides) == deck["meta"]["slide_count"]
        assert [s["title"] for s in slides] == _slide_titles(deck["body"]["pptx"])
        assert slides == read_deck_slides(deck["body"]["pptx"])
        for i, entry in enumerate(slides, 1):
            assert entry["index"] == i
            assert isinstance(entry["bullets"], list)
            assert entry["notes_excerpt"].strip(), f"第 {i} 页备注摘要不应为空"


def test_slide_images_exported_and_served(client, course_id, monkeypatch):
    """幻灯片图片：导出成功后 meta.slides[].image 全部有值、文件存在且 URL 可取。"""
    from app.core.config import get_settings
    from app.services import ppt as ppt_service

    _plan_and_approve(client, course_id, num_chapters=2)
    monkeypatch.setattr(get_settings(), "ppt_slide_images", True)
    monkeypatch.setattr(ppt_service, "powerpoint_exe", lambda: r"C:\fake\POWERPNT.EXE")

    def _fake_png(src, out_dir, timeout=180):
        from pptx import Presentation

        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        for i in range(1, len(Presentation(str(src)).slides) + 1):
            (out / f"幻灯片{i}.PNG").write_bytes(b"\x89PNG\r\n\x1a\n fake")  # 模拟 PowerPoint 命名

    monkeypatch.setattr(ppt_service, "_run_com_png", _fake_png)

    assert _gen(client, course_id, scope="course")["status"] == "success"
    deck = _decks(client, course_id)["course"][0]
    assert deck["slides"], deck
    assert not deck["images_error"], deck["images_error"]
    for slide in deck["slides"]:
        assert slide.get("image"), slide
        assert slide["image"].endswith(f"slides/slide-{slide['index']:02d}.png"), slide["image"]
        assert slide.get("image_url", "").startswith("/ppt-files/"), slide
        assert client.get(slide["image_url"]).status_code == 200
    slides_dir = Path(deck["body"]["pptx"]).parent / "slides"
    assert sorted(p.name for p in slides_dir.glob("*.png"))[0] == "slide-01.png"
    assert len(list(slides_dir.glob("*.png"))) == len(deck["slides"])


def test_slide_images_unavailable_falls_back_with_reason(client, course_id, monkeypatch):
    """COM 不可用 / 报错：回退为文字页级大纲，images_error 记录原因并清理半成品。"""
    from app.core.config import get_settings
    from app.services import ppt as ppt_service

    _plan_and_approve(client, course_id, num_chapters=1)
    monkeypatch.setattr(get_settings(), "ppt_slide_images", True)

    # ① 探测不到 PowerPoint
    monkeypatch.setattr(ppt_service, "powerpoint_exe", lambda: None)
    assert _gen(client, course_id, scope="course")["status"] == "success"
    deck = _decks(client, course_id)["course"][0]
    assert deck["slides"] and all(s["title"] for s in deck["slides"])  # 文字大纲仍可用
    assert all(not s.get("image") for s in deck["slides"])
    assert all(not s.get("image_url") for s in deck["slides"])
    assert "PowerPoint" in (deck["images_error"] or "")

    # ② COM 报错：显式原因 + 半成品目录被清理
    monkeypatch.setattr(ppt_service, "powerpoint_exe", lambda: r"C:\fake\POWERPNT.EXE")

    def _boom(src, out_dir, timeout=180):
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        (Path(out_dir) / "幻灯片1.PNG").write_bytes(b"partial")
        raise RuntimeError("COM boom")

    monkeypatch.setattr(ppt_service, "_run_com_png", _boom)
    assert _gen(client, course_id, scope="course")["status"] == "success"
    deck2 = _decks(client, course_id)["course"][0]
    assert "COM boom" in (deck2["images_error"] or "")
    assert all(not s.get("image") for s in deck2["slides"])
    assert not (Path(deck2["body"]["pptx"]).parent / "slides").exists(), "失败后应清理半成品"


def test_export_lands_decks_and_links_resolve(client, course_id):
    """② 导出后 课件/ 落地、概览页双链可解析（坏链 0）、verify 无不一致。"""
    root = _bind(client, course_id)
    _plan_and_approve(client, course_id, num_chapters=2)
    chapters = _chapters(client, course_id)
    assert _gen(client, course_id, scope="course")["status"] == "success"
    assert _gen(client, course_id, scope="chapter",
                chapter_node_id=chapters[0]["id"])["status"] == "success"

    result = _export(client, course_id)
    out = root / result["output_rel"]
    names = sorted(p.name for p in (out / "课件").glob("*.pptx"))
    assert "00-整课课件.pptx" in names, names
    assert any(n.startswith("第1章-") and n.endswith("课件.pptx") for n in names), names

    overview = (out / "00-课程概览.md").read_text(encoding="utf-8")
    assert "> 课件:[[00-整课课件.pptx]]" in overview
    assert "> 本章课件:[[" in overview

    links = re.findall(r"\[\[([^\]|]+\.pptx)(?:\|[^\]]*)?\]\]", overview)
    assert links, overview
    for target in links:
        target = target.strip().replace("\\", "/")
        path = (out / target) if "/" in target else (out / "课件" / target)
        assert path.exists(), f"课件双链不可解析（坏链）：{target}"

    report = client.post(f"/api/kb/course/{course_id}/verify").json()
    assert report["inconsistent"] == 0, [r for r in report["nodes"] if r["status"] != "consistent"]
    parts = {r["part"] for r in report["nodes"]}
    assert "ppt" in parts


def test_export_is_idempotent_no_duplicate_decks(client, course_id):
    """③ 重复导出幂等：不重复建文件、第二次无 preserved、课件字节不变。"""
    root = _bind(client, course_id)
    _plan_and_approve(client, course_id, num_chapters=1)
    node = _chapters(client, course_id)[0]
    assert _gen(client, course_id, scope="course")["status"] == "success"
    assert _gen(client, course_id, scope="chapter", chapter_node_id=node["id"])["status"] == "success"

    first = _export(client, course_id)
    deck_dir = root / first["output_rel"] / "课件"
    names_before = sorted(p.name for p in deck_dir.glob("*.pptx"))
    bytes_before = {p.name: p.read_bytes() for p in deck_dir.glob("*.pptx")}
    mtimes_before = {p.name: p.stat().st_mtime_ns for p in deck_dir.glob("*.pptx")}
    assert len(names_before) == 2

    second = _export(client, course_id)
    names_after = sorted(p.name for p in deck_dir.glob("*.pptx"))
    assert names_after == names_before
    assert not second["preserved"], second["preserved"]
    for path in deck_dir.glob("*.pptx"):
        assert path.read_bytes() == bytes_before[path.name]
        assert path.stat().st_mtime_ns == mtimes_before[path.name], "幂等导出不应重写课件文件"


def test_hand_edited_pptx_is_preserved_and_listed(client, course_id):
    """④ 教师手改 pptx → 导出时 preserved 并列出，且不被覆盖；verify 报不一致。"""
    root = _bind(client, course_id)
    _plan_and_approve(client, course_id, num_chapters=1)
    assert _gen(client, course_id, scope="course")["status"] == "success"
    first = _export(client, course_id)
    deck_path = root / first["output_rel"] / "课件" / "00-整课课件.pptx"
    assert deck_path.exists()
    deck_path.write_bytes(deck_path.read_bytes() + b"teacher-edit")

    second = _export(client, course_id)
    kept = {(e["file"], e["reason"]) for e in second["preserved"]}
    deck_rel = f"{first['output_rel']}/课件/00-整课课件.pptx"
    assert (deck_rel, "ppt-edited") in kept, second["preserved"]
    assert deck_path.read_bytes().endswith(b"teacher-edit")

    report = client.post(f"/api/kb/course/{course_id}/verify").json()
    row = next(r for r in report["nodes"] if r["note"] == "课件/00-整课课件.pptx")
    assert row["status"] == "mismatch", row


def test_uploaded_pptx_gets_slides_and_count(client, course_id):
    """上传的 PPTX 也要读回页级大纲（失败显式记录，不静默）。"""
    from pptx import Presentation

    _plan_and_approve(client, course_id, num_chapters=1)
    prs = Presentation()
    for title in ("上传封面", "上传第二页", "上传第三页"):
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = title
    buf = io.BytesIO()
    prs.save(buf)
    resp = client.post("/api/ppt/import",
                       data={"course_id": course_id, "scope": "course"},
                       files={"file": ("mine.pptx", buf.getvalue(), "application/octet-stream")})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["meta"]["slide_count"] == 3
    assert [s["title"] for s in body["slides"]] == ["上传封面", "上传第二页", "上传第三页"]


def test_legacy_deck_without_meta_slides_reads_back_from_file(client, course_id):
    """旧版课件（此功能上线前生成、没有 meta.slides）也能预览：现场读回 pptx。"""
    from app.db.session import SessionLocal
    from app.models.course import ContentItem

    _plan_and_approve(client, course_id, num_chapters=1)
    assert _gen(client, course_id, scope="course")["status"] == "success"
    deck = _decks(client, course_id)["course"][0]

    db = SessionLocal()
    try:
        row = db.get(ContentItem, deck["id"])
        meta = dict(row.meta or {})
        meta.pop("slides", None)
        row.meta = meta
        db.commit()
    finally:
        db.close()

    again = _decks(client, course_id)["course"][0]
    assert again["meta"].get("slides") is None
    assert again["slides"], "旧版课件应现场读回页级大纲"
    assert again["slides"][0]["title"]


def test_pdf_preview_disabled_never_calls_com(client, course_id, monkeypatch):
    """⑤ PPT_PREVIEW_PDF=false：不调用 COM，显式回退页级大纲。"""
    from app.core.config import get_settings
    from app.services import ppt as ppt_service

    _plan_and_approve(client, course_id, num_chapters=1)
    assert _gen(client, course_id, scope="course")["status"] == "success"
    deck = _decks(client, course_id)["course"][0]

    calls = {"n": 0}

    def _boom(*args, **kwargs):
        calls["n"] += 1
        raise AssertionError("关闭时不应调用 COM")

    monkeypatch.setattr(get_settings(), "ppt_preview_pdf", False)
    monkeypatch.setattr(ppt_service, "_run_com_export", _boom)
    body = client.post(f"/api/ppt/{deck['id']}/preview").json()
    assert body["ok"] is False and body["enabled"] is False
    assert body["fallback"] == "outline"
    assert calls["n"] == 0
    assert body["slides"], "回退时仍要返回页级大纲"


def test_pdf_preview_failure_falls_back_with_reason(client, course_id, monkeypatch):
    """⑤ 开启但失败：显式回退 + 写明原因，绝不静默。"""
    from app.core.config import get_settings
    from app.services import ppt as ppt_service

    _plan_and_approve(client, course_id, num_chapters=1)
    assert _gen(client, course_id, scope="course")["status"] == "success"
    deck = _decks(client, course_id)["course"][0]

    monkeypatch.setattr(get_settings(), "ppt_preview_pdf", True)
    monkeypatch.setattr(ppt_service, "powerpoint_exe", lambda: r"C:\fake\POWERPNT.EXE")
    monkeypatch.setattr(
        ppt_service, "_run_com_export",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("COM 卡死")),
    )
    body = client.post(f"/api/ppt/{deck['id']}/preview").json()
    assert body["ok"] is False and body["enabled"] is True
    assert body["fallback"] == "outline"
    assert "COM 卡死" in (body["reason"] or "")
    assert body["slides"]


def test_pdf_preview_success_returns_servable_url(client, course_id, monkeypatch):
    """开启且探测到 PowerPoint：产出可内嵌的 PDF URL。"""
    from app.core.config import get_settings
    from app.services import ppt as ppt_service

    _plan_and_approve(client, course_id, num_chapters=1)
    assert _gen(client, course_id, scope="course")["status"] == "success"
    deck = _decks(client, course_id)["course"][0]

    def _fake(src, target, timeout=180):
        Path(target).write_bytes(b"%PDF-1.4 fake preview")

    monkeypatch.setattr(get_settings(), "ppt_preview_pdf", True)
    monkeypatch.setattr(ppt_service, "powerpoint_exe", lambda: r"C:\fake\POWERPNT.EXE")
    monkeypatch.setattr(ppt_service, "_run_com_export", _fake)
    body = client.post(f"/api/ppt/{deck['id']}/preview").json()
    assert body["ok"] is True and body["enabled"] is True
    assert body["url"].startswith("/ppt-files/") and body["url"].endswith("preview.pdf")
    assert client.get(body["url"]).status_code == 200


def test_preview_unknown_item_404_and_gate_still_409(client, course_id):
    """⑥ 未通过大纲仍 409；预览不存在的课件 404。"""
    resp = client.post("/api/ppt/generate", json={"course_id": course_id, "scope": "course"})
    assert resp.status_code == 409, resp.text
    assert client.post("/api/ppt/not-a-deck/preview").status_code == 404
