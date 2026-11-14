"""任务 101：风格库「角色组」默认中性化 + 按 ``course.meta["roles"]`` 配置。

对应 HANDOFF「任务 101 说明」§B：
① 缺省导出不再出现 师傅👨‍🍳 / 酵母菌，角色组 = 讲解者 / 学员 / 主题（中性三件套）；
② ``course.meta["roles"]`` 合法 ⇒ 照它出，且**其它三组（14 条）逐字节不变**；删掉 ⇒ 回落默认；
③ 非法配置（空表 / 超 6 条 / 缺 label / 缺 emoji / 全空格）⇒ 回落默认 + 一条 warn、**不抛异常**；
④ 同一输入连跑两次 ⇒ 文件逐字节相同（与既有幂等口径一致）；
⑤ 三条出口都接课程：GET 预览 / POST 写盘 / ``kb.export_course`` 出包那条链。

协作规则第 7 条：DATABASE_URL / STORAGE_DIR / OBSIDIAN_VAULT_ROOT 由 conftest 指向临时目录。
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db.session import SessionLocal
from app.models.course import Course
from app.services import kb as kb_service
from app.services import style_library

OUT_REL = "课程/光合作用"
CHEF = "\U0001F468\u200d\U0001F373"          # 👨‍🍳
YEAST = "\U0001F9A0"                          # 🦠
NEUTRAL = ["角色组 · 讲解者", "角色组 · 学员", "角色组 · 主题"]
CONFIGURED = ["角色组 · 师傅", "角色组 · 学员", "角色组 · 酵母菌"]
BAOZI_ROLES = [{"label": "师傅", "emoji": CHEF},
               {"label": "学员", "emoji": "\U0001F9D1\u200d\U0001F393"},
               {"label": "酵母菌", "emoji": YEAST}]


def _bind(client, course_id: str, root: Path) -> None:
    resp = client.post(f"/api/kb/course/{course_id}/bind", json={
        "vault_root": str(root), "output_rel": OUT_REL,
    })
    assert resp.status_code == 200, resp.text


def _set_roles(course_id: str, roles) -> None:
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        meta = dict(course.meta or {})
        if roles is None:
            meta.pop("roles", None)
        else:
            meta["roles"] = roles
        course.meta = meta
        db.commit()
    finally:
        db.close()


def _role_names(library: dict) -> list[str]:
    return [i["name"] for i in library["libraryItems"] if i["name"].startswith("角色组")]


def _tail_text(library: dict) -> str:
    """卡片组 / 连线组 / 色卡组（共 14 条）的逐字节指纹口径。"""
    return json.dumps(library["libraryItems"][3:], ensure_ascii=False, indent=2)


def _preview(client, course_id: str) -> dict:
    resp = client.get(f"/api/kb/course/{course_id}/style-library")
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# ① 默认中性三件套
# ---------------------------------------------------------------------------

def test_default_roles_are_neutral(client, course_id, tmp_path):
    """缺省（没配 roles）角色组 = 讲解者/学员/主题；全库不出现 👨‍🍳 与 酵母菌。"""
    _bind(client, course_id, tmp_path / "vault")
    body = _preview(client, course_id)
    library = body["library"]

    assert _role_names(library) == NEUTRAL
    assert len(library["libraryItems"]) == 17
    assert set(body["stats"]["groups"]) == set(style_library.GROUPS)
    text = json.dumps(library, ensure_ascii=False)
    assert CHEF not in text and YEAST not in text and "师傅" not in text


# ---------------------------------------------------------------------------
# ② 按课程配置
# ---------------------------------------------------------------------------

def test_roles_follow_course_meta_and_tail_is_byte_identical(client, course_id, tmp_path):
    """配了 roles ⇒ 角色组照它出；其它 14 条逐字节不变；删掉配置 ⇒ 回落中性默认。"""
    _bind(client, course_id, tmp_path / "vault")

    default_library = _preview(client, course_id)["library"]
    assert _role_names(default_library) == NEUTRAL

    _set_roles(course_id, BAOZI_ROLES)
    configured = _preview(client, course_id)["library"]
    assert _role_names(configured) == CONFIGURED
    assert _tail_text(configured) == _tail_text(default_library)
    labels = [e["text"] for item in configured["libraryItems"][:3]
              for e in item["elements"] if e["type"] == "text"]
    assert "👨‍🍳" in labels and "🦠" in labels

    _set_roles(course_id, None)
    restored = _preview(client, course_id)["library"]
    assert _role_names(restored) == NEUTRAL
    assert _tail_text(restored) == _tail_text(default_library)


def test_roles_apply_to_export_and_write_paths(client, course_id, tmp_path):
    """`kb.export_course` 与 POST 写盘两条出口都读 ``course.meta["roles"]``（and 逐字节可复现）。"""
    root = tmp_path / "vault"
    _bind(client, course_id, root)
    _set_roles(course_id, BAOZI_ROLES)

    db = SessionLocal()
    try:
        kb_service.export_course(db, db.get(Course, course_id), force=True)
    finally:
        db.close()
    path = root / OUT_REL / style_library.STYLE_LIB_REL
    first = path.read_text(encoding="utf-8")
    assert _role_names(json.loads(first)) == CONFIGURED

    resp = client.post(f"/api/kb/course/{course_id}/style-library")
    assert resp.status_code == 200, resp.text
    second = path.read_text(encoding="utf-8")
    assert second == first                                   # 两条出口同一份字节
    assert hashlib.sha256(first.encode()).hexdigest() == hashlib.sha256(second.encode()).hexdigest()

    # 连跑两次 ⇒ 逐字节相同（幂等、不取时间/随机数）
    again = client.post(f"/api/kb/course/{course_id}/style-library")
    assert again.status_code == 200, again.text
    assert path.read_text(encoding="utf-8") == first


# ---------------------------------------------------------------------------
# ③ 非法配置 ⇒ 回落默认 + warn（不抛）
# ---------------------------------------------------------------------------

BAD_ROLES = [
    ("空表", []),
    ("超 6 条", [{"label": f"角色{i}", "emoji": "\U0001F3AF"} for i in range(1, 8)]),
    ("缺 label", [{"emoji": "\U0001F9D1\u200d\U0001F393"}]),
    ("缺 emoji", [{"label": "师傅"}]),
    ("全空格", [{"label": "   ", "emoji": "  "}]),
    ("不是列表", {"label": "师傅", "emoji": CHEF}),
]


@pytest.mark.parametrize("case,roles", BAD_ROLES, ids=[c for c, _ in BAD_ROLES])
def test_invalid_roles_fall_back_with_warning(case, roles, caplog):
    """非法配置一律回落中性默认：不抛异常、记一条 warn、产物仍是合法中性库。"""
    fake = SimpleNamespace(meta={"roles": roles}, id="probe-course")
    with caplog.at_level(logging.WARNING, logger="app.services.style_library"):
        library = style_library.build_library(course=fake)
    assert _role_names(library) == NEUTRAL
    assert style_library.validate_library(library) == []
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, warnings
    assert "风格库角色组配置不合法，回落默认" in warnings[0]
    assert "probe-course" in warnings[0]


def test_roles_of_reports_reason_for_each_invalid_case():
    """每条非法配置都给出可读原因（含实得值），合法/缺省两种好路径不报错。"""
    for case, roles in BAD_ROLES:
        got, problem = style_library._roles_of(SimpleNamespace(meta={"roles": roles}, id="c"))
        assert [label for label, _emoji in got] == ["讲解者", "学员", "主题"], case
        assert problem, case
    good, problem = style_library._roles_of(SimpleNamespace(meta={"roles": BAOZI_ROLES}, id="c"))
    assert [label for label, _emoji in good] == ["师傅", "学员", "酵母菌"] and problem == ""
    missing, problem = style_library._roles_of(SimpleNamespace(meta={}, id="c"))
    assert [label for label, _emoji in missing] == ["讲解者", "学员", "主题"] and problem == ""
    assert problem == ""            # 没配 ⇒ 不告警（可选配置不该刷日志）