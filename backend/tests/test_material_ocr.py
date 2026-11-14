"""任务 54 素材 OCR 回归测试：图片素材 → `[N]` 段落编号 → 溯源 → 置信度提示。

协作区铁律 7：``DATABASE_URL`` / ``STORAGE_DIR`` / ``OBSIDIAN_VAULT_ROOT`` 由 ``conftest``
统一指向临时目录，本文件不碰真实库。

本任务附加纪律：OCR 是**可选依赖**（``backend/requirements-ocr.txt``，约 400MB）——
真引擎链路一律 ``importorskip`` 保护，**没装引擎的机器上本文件仍然全绿**；
未装 / 失败的降级路径用 monkeypatch 模拟，不依赖真引擎。
"""
from __future__ import annotations

import io
import os

import pytest

from app.db.session import SessionLocal
from app.models.course import Course, SourceMaterial
from app.services import kb as kb_mod
from app.services import lecture_qc
from app.services import material as material_mod

OCR_FONT = "C:/Windows/Fonts/msyh.ttc"
TEXT_LINES = ["光合作用在叶绿体中进行", "光反应把光能转成 ATP"]


def _png_with_text(lines: list[str] | None = None) -> bytes:
    """用 PIL 现画一张带中文的图（不依赖任何外部素材文件）。"""
    Image = pytest.importorskip("PIL.Image")
    ImageDraw = pytest.importorskip("PIL.ImageDraw")
    ImageFont = pytest.importorskip("PIL.ImageFont")
    if not os.path.exists(OCR_FONT):
        pytest.skip("本机没有中文字体（msyh.ttc），跳过图片构造")
    rows = lines or TEXT_LINES
    img = Image.new("RGB", (900, 120 * len(rows) + 40), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(OCR_FONT, 40)
    for index, line in enumerate(rows):
        draw.text((30, 30 + index * 120), line, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _upload_image(client, course_id: str, name: str = "扫描页.png") -> dict:
    resp = client.post("/api/material/upload", data={"course_id": course_id},
                       files={"file": (name, _png_with_text(), "image/png")})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_image_material_ocr_full_chain(client, course_id):
    """验收①：真引擎 —— 图片入库为 image、段落编号正确、讲义能引用到具体段。"""
    pytest.importorskip("rapidocr_onnxruntime")
    row = _upload_image(client, course_id)

    assert row["kind"] == "image"
    assert row["ext"] == ".png"
    assert row["pages"] == 1
    ocr = row["ocr"]
    assert ocr and ocr["engine"] == material_mod.OCR_ENGINE
    assert ocr["version"], "要记下引擎版本，交付件里才说得清用什么识别的"
    assert ocr["confidence"] and ocr["confidence"] > 0.5
    assert ocr["lines"] >= 1 and not ocr["skipped"]

    db = SessionLocal()
    try:
        material = db.get(SourceMaterial, row["id"])
        numbered = material_mod.numbered_paragraphs(material.text)
        assert numbered and numbered[0].startswith("[1] ")
        assert len(numbered) == len(material_mod.split_paragraphs(material.text))
        # 识别出的中文确实进了正文（不是空壳入库）
        assert sum("\u4e00" <= ch <= "\u9fff" for ch in material.text) >= 4

        # 素材页顶部写明「由 OCR 识别（引擎/版本/平均置信度）」
        page = "\n".join(kb_mod._material_page(Course(title="测试课"), material))
        assert "本页文字由 OCR 识别" in page and material_mod.OCR_ENGINE in page
        assert "[1] " in page

        # 注入链路同源：讲义里的「第 N 段」能定位到素材页 [N]
        catalog = material_mod.material_catalog(db, db.get(Course, course_id))
        paras = [p for entry in catalog for p in entry["paragraphs"]]
        assert paras and paras[0] == numbered[0].split(" ", 1)[1]
    finally:
        db.close()


def test_image_material_without_ocr_engine_is_still_ingested(client, course_id, monkeypatch):
    """验收②：没装 OCR 组件 —— 素材照旧入库、接口不 500、显式说明、素材级提示。"""
    monkeypatch.setattr(material_mod, "OCR_AVAILABLE", False)
    row = _upload_image(client, course_id, name="无引擎.png")

    assert row["kind"] == "image"
    ocr = row["ocr"]
    assert ocr and "未安装 OCR 组件" in ocr["skipped"]
    assert ocr["confidence"] is None and ocr["lines"] == 0

    detail = client.get(f"/api/material/full/{row['id']}")
    assert detail.status_code == 200, detail.text
    assert "未安装 OCR 组件" in detail.json()["ocr"]["skipped"]

    db = SessionLocal()
    try:
        material = db.get(SourceMaterial, row["id"])
        note = material_mod.ocr_page_note(material)
        assert note.startswith("> \u26a0\ufe0f 本页没有 OCR 文字")
        page = "\n".join(kb_mod._material_page(Course(title="测试课"), material))
        assert "未安装 OCR 组件" in page

        notices = material_mod.ocr_notices(db, db.get(Course, course_id))
        assert any("未安装 OCR 组件" in item["reason"] for item in notices)
    finally:
        db.close()


def test_low_confidence_material_becomes_material_notice(client, course_id, monkeypatch):
    """低置信度 ⇒ 质检报告里的**素材级提示**（不动 10 项判据 / 权重 / 分母）。"""
    monkeypatch.setattr(material_mod, "OCR_AVAILABLE", True)
    monkeypatch.setattr(material_mod, "ocr_image", lambda data: {
        "engine": material_mod.OCR_ENGINE, "version": "test", "pages": 1,
        "confidence": 0.42, "text": "第一段\n第二段", "lines": 2, "skipped": ""})

    row = _upload_image(client, course_id, name="模糊扫描.png")
    assert row["kind"] == "image" and row["chars"] > 0
    assert row["ocr"]["confidence"] == 0.42

    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        mine = [n for n in material_mod.ocr_notices(db, course)
                if n["material_id"] == row["id"]]
        assert mine and mine[0]["level"] == "warn"
        assert "0.42" in mine[0]["reason"]

        report = lecture_qc.course_report(db, course)
        assert any(n["material_id"] == row["id"] for n in report["material_notices"])
        assert len(lecture_qc.CHECK_KEYS) == 11, "素材级提示不参与判据（第 11 项 numbers 是任务 106 的）"
    finally:
        db.close()


def test_plain_image_without_text_is_not_flagged(client, course_id, monkeypatch):
    """纯图形/照片：入库为「无文字素材」，但**不打扰老师**（不产生提示）。"""
    monkeypatch.setattr(material_mod, "OCR_AVAILABLE", True)
    monkeypatch.setattr(material_mod, "ocr_image", lambda data: {
        "engine": material_mod.OCR_ENGINE, "version": "test", "pages": 1,
        "confidence": None, "text": "", "lines": 0, "skipped": ""})

    row = _upload_image(client, course_id, name="照片.png")
    assert "没有识别到文字" in row["ocr"]["skipped"]

    db = SessionLocal()
    try:
        notices = material_mod.ocr_notices(db, db.get(Course, course_id))
        assert [n for n in notices if n["material_id"] == row["id"]] == []
    finally:
        db.close()


def test_ocr_page_note_marks_low_confidence():
    """素材页那行提示：低于阈值要显式写「建议人工抽查」。"""
    low = SourceMaterial(title="模糊", kind="image", text="甲",
                         meta={"ocr": {"engine": "e", "version": "1.0",
                                       "confidence": 0.5, "skipped": ""}})
    high = SourceMaterial(title="清晰", kind="image", text="乙",
                          meta={"ocr": {"engine": "e", "version": "1.0",
                                        "confidence": 0.99, "skipped": ""}})
    plain = SourceMaterial(title="文本", kind="text", text="丙", meta={})

    assert "建议人工抽查" in material_mod.ocr_page_note(low)
    assert "建议人工抽查" not in material_mod.ocr_page_note(high)
    assert material_mod.ocr_page_note(plain) == ""
