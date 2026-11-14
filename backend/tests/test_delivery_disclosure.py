"""任务 67：交付物里写明「自动化工具辅助生产 + 交付前已逐篇经人工复核确认」。

用户 2026-09-20 批准这句话（规格见协作区 HANDOFF 任务 67 说明）：`00-交付说明.md` /
`01-验收单.md` / `audit.EXTERNAL_USAGE_NOTE`（体检报告与 48p 中性说明用同一条）三处
**同一条正本**，对外与内部两种模式都要有；它不是成本信息，不触 48p / 61 的脱敏口径。

协作规则第 7 条：五件套由 conftest 统一指向临时目录，本文件不碰真库 / 真 vault / 真交付包。
"""
from __future__ import annotations

from app.db.session import SessionLocal
from app.models.course import Course
from app.services import audit as audit_mod
from app.services import delivery as delivery_mod
from tests.test_delivery import _build, _course_with_content, _delivery_dir, _new_course

DOCS = ("00-交付说明.md", "01-验收单.md")
DISCLOSURE = "交付前已逐篇经人工复核确认"
# 与 工具\交付包核验.py 第 12 项同口径：这句承诺本身不许带成本字样
COST_WORDS = ("¥", "tokens", "累计费用", "M5c", "LLM 用量")


def _docs_of(client, course_id: str, external: bool | None = None) -> dict[str, str]:
    """出一次包（0 LLM、PDF 关掉），把两个客户先看的文档读进内存。"""
    payload: dict = {"pdf": "off"}
    if external is not None:
        payload["external"] = external
    out = _delivery_dir(_build(client, course_id, payload)["build_id"])
    return {name: (out / name).read_text(encoding="utf-8") for name in DOCS}


def test_disclosure_line_in_both_docs_in_both_modes(client, monkeypatch):
    """对外与内部两种模式：`00-交付说明.md` 与 `01-验收单.md` 都含这句。"""
    course_id = _new_course(client, "披露句探针")
    _course_with_content(client, course_id, monkeypatch)
    for external in (True, False):
        docs = _docs_of(client, course_id, external=external)
        for name, text in docs.items():
            assert "本课内容由自动化工具辅助生产" in text, (external, name)
            assert DISCLOSURE in text, (external, name)


def test_disclosure_sentence_is_one_source_and_cost_free(client, monkeypatch):
    """三处同一句：交付说明 / 验收单引用的是 audit 那条正本，且这句不带金额与 tokens。"""
    note = audit_mod.EXTERNAL_USAGE_NOTE
    assert delivery_mod.EXTERNAL_USAGE_NOTE == note          # 一处正本，别处只引用
    assert DISCLOSURE in note
    assert [word for word in COST_WORDS if word in note] == []

    course_id = _new_course(client, "披露句正本探针")
    _course_with_content(client, course_id, monkeypatch)
    for external in (True, False):
        for name, text in _docs_of(client, course_id, external=external).items():
            assert note in text, (external, name)


def test_audit_report_carries_the_same_sentence(client, monkeypatch):
    """第三处（体检报告 §6「内容生成说明」）用的是同一条正本。"""
    course_id = _new_course(client, "披露句报告探针")
    _course_with_content(client, course_id, monkeypatch)
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        outer = audit_mod.course_report(db, course, external=True)["markdown"]
    finally:
        db.close()
    assert audit_mod.EXTERNAL_USAGE_NOTE in outer
    assert DISCLOSURE in outer
