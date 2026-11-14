"""素材提取测试：txt/md 直接读取、docx/pptx 解析、不支持格式报错。"""
from __future__ import annotations

import io
import zipfile

import pytest

from app.services import material as material_service


def _make_docx(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        paras = "".join(
            f'<w:p xmlns:w="{ns}"><w:r><w:t>{p}</w:t></w:r></w:p>' for p in text.split("\n") if p.strip()
        )
        zf.writestr("word/document.xml", f'<?xml version="1.0"?><w:document xmlns:w="{ns}"><w:body>{paras}</w:body></w:document>')
    return buf.getvalue()


def _make_pptx(text: str) -> bytes:
    from pptx import Presentation

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "光合作用"
    slide.placeholders[1].text = text
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def test_extract_txt_and_docx_and_pptx():
    assert "叶绿体" in material_service.extract_text("a.txt", "光合作用在叶绿体进行".encode("utf-8"))
    assert "卡尔文循环" in material_service.extract_text("a.docx", _make_docx("光合作用\n卡尔文循环"))
    assert "有机物" in material_service.extract_text("a.pptx", _make_pptx("光合作用合成有机物"))


def test_unsupported_format_raises():
    with pytest.raises(ValueError):
        material_service.extract_text("a.exe", b"MZ")


# ---------------------------------------------------------------------------
# P2-1：文件型素材也能读解析全文（原先只有纯文本素材能读 → 404）
# ---------------------------------------------------------------------------

def test_file_material_full_text_is_paged(client, course_id):
    """PPTX 素材：列表里带字数/页数，全文接口能分页读。"""
    text = "".join(f"光合作用第{i}段说明。" for i in range(300))
    up = client.post(
        "/api/material/upload",
        data={"course_id": course_id, "title": "PPT 素材"},
        files={"file": ("光合作用.pptx", _make_pptx(text),
                        "application/vnd.openxmlformats-officedocument.presentationml.presentation")},
    )
    assert up.status_code == 200, up.text
    row = up.json()
    assert row["kind"] == "file"
    assert row["pages"] == 1, "一页幻灯片 → 页数 = 1"
    assert row["chars"] > 0 and row["raw_chars"] == row["chars"]
    assert row["truncated"] is False

    first = client.get(f"/api/material/full/{row['id']}", params={"limit": 50}).json()
    assert first["offset"] == 0 and len(first["text"]) == 50
    assert first["total_chars"] == row["chars"]
    assert first["pages"] == 1
    assert first["has_more"] is True and first["next_offset"] == 50

    second = client.get(f"/api/material/full/{row['id']}",
                        params={"offset": first["next_offset"], "limit": 50}).json()
    assert second["offset"] == 50 and len(second["text"]) == 50
    assert second["text"] != first["text"]

    whole = client.get(f"/api/material/full/{row['id']}", params={"limit": 20000}).json()
    assert whole["has_more"] is False and whole["next_offset"] is None
    assert len(whole["text"]) == whole["total_chars"]
    assert whole["text"].startswith(first["text"]) and whole["text"][50:100] == second["text"]


def test_full_text_rejects_unknown_material_and_keeps_text_material(client, course_id):
    assert client.get("/api/material/full/does-not-exist").status_code == 404
    pasted = client.post("/api/material/paste", json={
        "course_id": course_id, "title": "粘贴素材", "text": "叶绿体里进行光合作用。"}).json()
    full = client.get(f"/api/material/full/{pasted['id']}").json()
    assert full["kind"] == "text"
    assert full["text"] == "叶绿体里进行光合作用。"
    assert full["pages"] is None and full["has_more"] is False


def test_full_text_computes_pages_for_legacy_rows_without_meta(client, course_id):
    """本次之前入库的老素材没有 meta.pages → 就地读原件现算。"""
    up = client.post(
        "/api/material/upload",
        data={"course_id": course_id, "title": "PPT 素材"},
        files={"file": ("光合作用.pptx", _make_pptx("叶绿体内进行"),
                        "application/vnd.openxmlformats-officedocument.presentationml.presentation")},
    ).json()

    from app.db.session import SessionLocal
    from app.models.course import SourceMaterial

    db = SessionLocal()
    try:
        row = db.get(SourceMaterial, up["id"])
        row.meta = {k: v for k, v in (row.meta or {}).items() if k != "pages"}
        db.commit()
    finally:
        db.close()

    assert client.get(f"/api/material/{course_id}").json()[0]["pages"] == 1
    assert client.get(f"/api/material/full/{up['id']}").json()["pages"] == 1
