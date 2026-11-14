# -*- coding: utf-8 -*-
"""任务 145：主题跟随课程（``course.meta.ppt_theme``）+ 两个打架的默认值修掉。

五类断言：
① **优先级逐档**（唯一口径 `ppt_theme_setting.resolve`）：显式传 > 课程 meta > 后端配置 >
   内置默认；空串 / 脏值 / 非法配置各有一档，**都要给出原因**（不许静默）；
② **真出课件证明**：同一门课同一份内容按四档各出一份，比形状清单（含封面色与标题带色）
   —— 该相等的逐字节相等、该不同的必须不同（否则用例本身证明不了任何事）；
③ **`generate_deck` 默认值不再是「专业培训」**（行为级：不传 theme 时走配置，而不是顶掉配置）；
④ **接口**：`POST /api/ppt/theme` 校验 + 落库 + 日志；`GET /api/ppt/themes?course_id=` 带
   「本课当前主题 + 来源」；清空回到配置；非法值 4xx 且**不改库**；
⑤ **迁移脚本**（`scripts/backfill_ppt_theme.py`，真跑子进程）：dry-run 不写库、apply 只写
   该写的、**跑第二遍 0 改动**（幂等）、依据逐门课可解释。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from pptx import Presentation
from pptx.enum.dml import MSO_FILL

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.course import Course
from app.services import ppt_theme_setting
from app.services.ppt import generate_deck
from app.services.ppt_theme import resolve_theme, theme_keys
from tests.test_ppt_v4 import (_add_diagram, _add_item, _add_point, _chapter, _course_deck,
                               _gen, _plan_and_approve, _wait)

REPO = Path(__file__).resolve().parents[2]
BACKFILL = REPO / "scripts" / "backfill_ppt_theme.py"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _patch_course_meta(client, course_id: str, **meta) -> dict:
    """PATCH 课程 meta（合并语义）：造「课程里存着脏值」这种只能用通用接口造出来的状态。"""
    resp = client.patch(f"/api/course/{course_id}", json={"meta": meta})
    assert resp.status_code == 200, resp.text
    return resp.json()["meta"]


def _set_theme(client, course_id: str, theme: str):
    return client.post("/api/ppt/theme", json={"course_id": course_id, "theme": theme})


def _make_chapter(client, course_id: str) -> dict:
    """给一门课造「大纲已通过 + 有章节」的最小状态。"""
    _plan_and_approve(client, course_id, num_chapters=1)
    return _chapter(client, course_id)


def _run_backfill(*args: str) -> subprocess.CompletedProcess:
    """真跑迁移脚本（子进程 + 当前测试库的临时五件套）：命令行能不能用也一并验了。"""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "LLM_PROVIDER": "mock",
           "IMAGE_PROVIDER": "mock"}
    return subprocess.run([sys.executable, str(BACKFILL), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", env=env, cwd=str(REPO))


def _run_colors(shape) -> tuple:
    """形状里每个 run 的字色（取不到 ⇒ None）；空元组 = 形状没有文字。"""
    if not getattr(shape, "has_text_frame", False):
        return ()
    out = []
    for para in shape.text_frame.paragraphs:
        for run in (para.runs or []):
            try:
                out.append(str(run.font.color.rgb))
            except (AttributeError, ValueError):
                out.append(None)
    return tuple(out)


def _fill_of(shape) -> str | None:
    """形状的实色填充（取不到 ⇒ None）—— 封面色块 / 内页标题带色走这条。"""
    try:
        if shape.fill.type == MSO_FILL.SOLID:
            return str(shape.fill.fore_color.rgb)
    except (AttributeError, TypeError, ValueError):
        return None
    return None


def _shape_rows(pptx) -> list[dict]:
    """形状清单（任务 144 那套 + 颜色）：(page,name,type,l,t,w,h,size,text,colors,fill)。"""
    prs = Presentation(str(pptx))
    rows: list[dict] = []
    for page, slide in enumerate(prs.slides, 1):
        for shape in slide.shapes:
            sizes = [run.font.size.pt for para in shape.text_frame.paragraphs
                     for run in (para.runs or []) if run.font.size] \
                if getattr(shape, "has_text_frame", False) else []
            rows.append({
                "page": page, "name": str(getattr(shape, "name", "")),
                "type": str(shape.shape_type),
                "l": round(shape.left / 914400, 4), "t": round(shape.top / 914400, 4),
                "w": round(shape.width / 914400, 4), "h": round(shape.height / 914400, 4),
                "size": max(sizes) if sizes else None,
                "text": (shape.text_frame.text.strip()
                         if getattr(shape, "has_text_frame", False) else ""),
                "colors": _run_colors(shape), "fill": _fill_of(shape),
            })
    return rows


def _manifest(client, course_id: str) -> dict:
    """整课课件的「主题 + 形状清单」快照（§B2 / §B4 的比对单元）。"""
    deck = _course_deck(client, course_id)
    return {"theme": deck["theme"], "theme_name": deck["theme_name"],
            "theme_source": deck.get("theme_source"),
            "rows": _shape_rows(Path(deck["body"]["pptx"]))}


# ---------------------------------------------------------------------------
# ① 优先级逐档（纯函数，唯一口径）
# ---------------------------------------------------------------------------

def test_priority_is_param_then_course_then_config_then_default():
    """四档优先级逐档点名（§A1-2）；空串 / None 不再等于「用配置」。"""
    cases = [
        # (显式传, 课程 meta, 配置, 期望主题 id, 期望来源)
        ("navy", {"ppt_theme": "clean"}, "professional", "navy", "param"),
        ("theme03", {"ppt_theme": "clean"}, "professional", "professional", "param"),
        ("", {"ppt_theme": "navy"}, "clean", "navy", "course"),
        ("", {"ppt_theme": "深蓝商务"}, "clean", "navy", "course"),
        ("", {"ppt_theme": "theme07"}, "clean", "professional", "course"),
        ("", {}, "professional", "professional", "config"),
        ("", None, "professional", "professional", "config"),
        ("", {}, "", "clean", "default"),
        (None, {"ppt_theme": ""}, "", "clean", "default"),
    ]
    for explicit, course, config, theme_id, source in cases:
        spec, got_source, error = ppt_theme_setting.resolve(explicit, course, config)
        assert (spec["id"], got_source) == (theme_id, source), (explicit, course, config)
        assert error is None, (explicit, course, config, error)


def test_dirty_and_illegal_values_fall_back_with_reason():
    """脏值 / 非法配置：**回落到确定的一档 + 给出原因**（不许静默）。"""
    spec, source, error = ppt_theme_setting.resolve("", {"ppt_theme": "瞎写"}, "professional")
    assert (spec["id"], source) == ("professional", "config")
    assert "瞎写" in error and "professional（专业培训）" in error and "已按后端配置" in error
    spec, source, error = ppt_theme_setting.resolve("", {"ppt_theme": "瞎写"}, "乱配")
    assert (spec["id"], source) == ("clean", "default")
    assert "瞎写" in error and "乱配" in error and "回退默认主题" in error
    spec, source, error = ppt_theme_setting.resolve("", {}, "乱配")
    assert (spec["id"], source) == ("clean", "default")
    assert "乱配" in error and "回退默认主题" in error
    spec, source, error = ppt_theme_setting.resolve("无此主题", {"ppt_theme": "navy"}, "clean")
    assert (spec["id"], source) == ("clean", "param")
    assert "无此主题" in error


def test_write_validates_and_normalizes_to_canonical_id():
    """写库只认合法值、只存规范 id；非法值抛 ValueError（**不写、不回落**）。"""
    course = Course(title="临时课", subject="", audience="", duration_minutes=60, meta={})
    result = ppt_theme_setting.write(course, "深蓝商务")
    assert course.meta == {"ppt_theme": "navy"} and result["theme"] == "navy"
    assert ppt_theme_setting.write(course, "theme07")["theme"] == "professional"
    assert course.meta["ppt_theme"] == "professional"
    cleared = ppt_theme_setting.write(course, "")
    assert "ppt_theme" not in course.meta and cleared["cleared"] is True
    assert cleared["source"] in ("config", "default")
    for bad in ("瞎写", "cleanx", "6"):
        try:
            ppt_theme_setting.write(course, bad)
        except ValueError as exc:
            assert "不是合法主题" in str(exc) and "未写库" in str(exc)
            assert "clean（清简教学）" in str(exc) and "navy（深蓝商务）" in str(exc)
        else:  # pragma: no cover - 走到这里就是用例失败
            raise AssertionError(f"非法主题「{bad}」必须抛 ValueError")
    assert "ppt_theme" not in course.meta, "非法值不许留下任何痕迹"


def test_legal_values_come_from_theme_keys():
    """合法值清单就是 `ppt_theme.theme_keys()`（别名认得、存规范 id），两边不许各写一份。"""
    legal = ppt_theme_setting.legal_values_text()
    for key in theme_keys():
        assert ppt_theme_setting.normalize_key(key) is not None, key
    for theme_id in ("clean", "professional", "handdrawn", "modern", "navy"):
        assert f"{theme_id}（" in legal
    assert ppt_theme_setting.normalize_key("") is None


# ---------------------------------------------------------------------------
# ② 真出课件：四档各出一份，比形状清单（含封面色 / 标题带色）
# ---------------------------------------------------------------------------

def test_four_slots_produce_expected_output_by_shape_manifest(client, course_id, monkeypatch):
    """§B2：显式 navy / 课程 navy / 配置 clean / 非法配置 ⇒ 内置默认，逐档比形状清单。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "ppt_theme", "clean")
    _plan_and_approve(client, course_id, num_chapters=1)
    chapter = _chapter(client, course_id)
    point = _add_point(course_id, chapter["id"])
    _add_diagram(course_id, point["id"], "概念图-优先级四档")

    _gen(client, course_id, scope="course", theme="navy")
    navy_explicit = _manifest(client, course_id)
    assert navy_explicit["theme"] == "navy" and navy_explicit["theme_source"] == "param"

    # 档①：课程 meta=clean + 显式传 navy ⇒ **navy**（显式参数赢）
    _patch_course_meta(client, course_id, ppt_theme="clean")
    _gen(client, course_id, scope="course", theme="navy")
    slot_param = _manifest(client, course_id)
    assert slot_param["theme"] == "navy"
    assert slot_param["rows"] == navy_explicit["rows"], "同主题两份产物的形状清单必须逐字节相同"

    # 档②：不传 + 课程 meta=navy ⇒ **navy**（课程设置赢过配置 clean）
    assert _set_theme(client, course_id, "navy").status_code == 200
    _gen(client, course_id, scope="course")
    slot_course = _manifest(client, course_id)
    assert slot_course["theme"] == "navy" and slot_course["theme_source"] == "course"
    assert slot_course["rows"] == navy_explicit["rows"]

    # 档③：不传 + 课程 meta 清空 + 配置=clean ⇒ **clean**
    assert _set_theme(client, course_id, "").status_code == 200
    _gen(client, course_id, scope="course")
    slot_config = _manifest(client, course_id)
    assert slot_config["theme"] == "clean" and slot_config["theme_source"] == "config"
    assert slot_config["rows"] != navy_explicit["rows"], "clean 与 navy 的产物必须真的不同"

    # 档④：不传 + meta 清空 + 配置非法 ⇒ 回落内置默认 + 原因（不许静默）
    monkeypatch.setattr(settings, "ppt_theme", "乱配的主题")
    _gen(client, course_id, scope="course")
    slot_default = _manifest(client, course_id)
    assert slot_default["theme"] == "clean" and slot_default["theme_source"] == "default"
    assert slot_default["rows"] == slot_config["rows"], "内置默认就是 clean：应与档③一致"
    deck = _course_deck(client, course_id)
    assert "乱配的主题" in (deck["theme_error"] or "")
    assert any(n["kind"] == "theme" and "乱配的主题" in n["reason"]
               for n in deck["layout_notes"]), deck["layout_notes"]


def test_course_dirty_value_falls_back_to_config_and_says_why(client, course_id, monkeypatch):
    """课程里是脏值 ⇒ 回落配置 + 记原因（``meta.theme_error`` 与 ``layout_notes`` 都要有）。"""
    monkeypatch.setattr(get_settings(), "ppt_theme", "professional")
    _plan_and_approve(client, course_id, num_chapters=1)
    _add_point(course_id, _chapter(client, course_id)["id"])
    _patch_course_meta(client, course_id, ppt_theme="我不认识的主题")
    _gen(client, course_id, scope="course")
    deck = _course_deck(client, course_id)
    assert deck["theme"] == "professional" and deck["theme_source"] == "config"
    assert "我不认识的主题" in (deck["theme_error"] or "")
    assert any(n["kind"] == "theme" for n in deck["layout_notes"])


def test_no_regression_explicit_vs_course_setting_are_byte_identical(client, course_id):
    """§B4（最强口径）：回填后「显式传主题」与「不传（走课程设置）」两份产物形状清单相同。"""
    _plan_and_approve(client, course_id, num_chapters=1)
    chapter = _chapter(client, course_id)
    point = _add_point(course_id, chapter["id"])
    _add_diagram(course_id, point["id"], "概念图-零回归")
    assert _set_theme(client, course_id, "professional").status_code == 200

    _gen(client, course_id, scope="course", theme="professional")
    explicit = _manifest(client, course_id)
    _gen(client, course_id, scope="course")
    inherited = _manifest(client, course_id)
    assert explicit["theme_source"] == "param" and inherited["theme_source"] == "course"
    diff = [(a, b) for a, b in zip(explicit["rows"], inherited["rows"]) if a != b]
    assert not diff, f"差异 {len(diff)} 行（应为 0）：{diff[:3]}"
    assert len(explicit["rows"]) == len(inherited["rows"]) > 20


# ---------------------------------------------------------------------------
# ③ generate_deck 的默认值不再顶掉配置（行为级）
# ---------------------------------------------------------------------------

def test_generate_deck_default_no_longer_forces_professional(client, course_id, monkeypatch):
    """`generate_deck()` **不传 theme** ⇒ 走配置（而不是写死的 theme07=专业培训）。"""
    monkeypatch.setattr(get_settings(), "ppt_theme", "handdrawn")
    _plan_and_approve(client, course_id, num_chapters=1)
    chapter = _chapter(client, course_id)
    _add_point(course_id, chapter["id"])
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        item = generate_deck(db, course, scope="chapter", chapter_node_id=chapter["id"])
        assert item.meta["theme"] == "handdrawn", item.meta["theme"]
        assert item.meta["theme_source"] == "config"
        assert item.meta["theme_name"] == "手绘亲和"
        assert "theme_error" not in item.meta
    finally:
        db.close()


def test_generate_deck_signature_and_priority_are_documented():
    """源码级：signature 默认值就是空串、口径注释点名四档（防「顺手」改回去）。"""
    source = (REPO / "backend" / "app" / "services" / "ppt.py").read_text("utf-8")
    assert 'theme: str = "theme07"' not in source
    assert 'theme: str = "", engine: str | None = None,' in source
    assert "显式传的 theme（非空）> 课程 meta.ppt_theme > 后端配置 PPT_THEME > 内置默认" in source


# ---------------------------------------------------------------------------
# ④ 接口：设置课程主题 + /ppt/themes 带「本课当前主题」
# ---------------------------------------------------------------------------

def test_set_theme_route_validates_persists_and_logs(client, course_id, caplog):
    """`POST /api/ppt/theme`：合法值落库、非法值 4xx 且不改库、清空回到配置；改动写日志。"""
    before = client.get(f"/api/course/{course_id}").json()["meta"]
    assert "ppt_theme" not in before

    with caplog.at_level("INFO"):
        ok = _set_theme(client, course_id, "深蓝商务")
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["theme"] == "navy" and body["cleared"] is False
    assert body["source"] == "course" and body["source_label"] == "本课设置"
    assert ppt_theme_setting.APPLY_NOTE in body["message"]
    after = client.get(f"/api/course/{course_id}").json()["meta"]
    assert after["ppt_theme"] == "navy"
    assert set(after) - set(before) == {"ppt_theme"}, (before, after)
    assert "课程主题已更新" in caplog.text and "只改外观" in caplog.text
    assert "重新生成" in caplog.text

    bad = _set_theme(client, course_id, "瞎写")
    assert bad.status_code == 400
    assert "不是合法主题" in bad.json()["detail"] and "navy（深蓝商务）" in bad.json()["detail"]
    assert client.get(f"/api/course/{course_id}").json()["meta"]["ppt_theme"] == "navy", "非法值不许改库"

    cleared = _set_theme(client, course_id, "")
    assert cleared.status_code == 200 and cleared.json()["cleared"] is True
    assert "ppt_theme" not in client.get(f"/api/course/{course_id}").json()["meta"]
    assert cleared.json()["source"] in ("config", "default")
    assert _set_theme(client, "no-such-course", "navy").status_code == 404


def test_themes_endpoint_carries_course_theme_and_source(client, course_id, monkeypatch):
    """`GET /api/ppt/themes`：不带 course_id 时 course=None；带则给「本课当前主题 + 来源」。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "ppt_theme", "clean")
    payload = client.get("/api/ppt/themes").json()
    assert payload["course"] is None
    active, error = resolve_theme(settings.ppt_theme)
    assert payload["active"]["id"] == active["id"] and payload["active"]["error"] == error
    assert [item["id"] for item in payload["items"]][:5] == list(theme_keys())[:5]

    assert _set_theme(client, course_id, "navy").status_code == 200
    course = client.get(f"/api/ppt/themes?course_id={course_id}").json()["course"]
    assert course["id"] == "navy" and course["source"] == "course"
    assert course["source_label"] == "本课设置" and course["setting"] == "navy"
    assert course["colors"] == list(resolve_theme("navy")[0]["palette"]["colors"])
    assert course["error"] is None

    assert _set_theme(client, course_id, "").status_code == 200
    monkeypatch.setattr(settings, "ppt_theme", "handdrawn")
    course = client.get(f"/api/ppt/themes?course_id={course_id}").json()["course"]
    assert course["id"] == "handdrawn" and course["source"] == "config"
    assert course["source_label"] == "后端配置" and course["setting"] is None

    _patch_course_meta(client, course_id, ppt_theme="瞎写")
    course = client.get(f"/api/ppt/themes?course_id={course_id}").json()["course"]
    assert course["id"] == "handdrawn" and course["source"] == "config"
    assert "瞎写" in course["error"] and course["setting"] == "瞎写"
    assert client.get("/api/ppt/themes?course_id=no-such-course").status_code == 404


def test_batch_generation_uses_the_same_priority(client, course_id):
    """A2-2：批量补章节课件（不传 theme）与单份生成走**同一条**优先级。"""
    _plan_and_approve(client, course_id, num_chapters=2)
    chapter = _chapter(client, course_id)
    _add_point(course_id, chapter["id"])
    assert _set_theme(client, course_id, "navy").status_code == 200

    resp = client.post("/api/ppt/generate-chapters",
                       json={"course_id": course_id, "redo": True, "theme": ""})
    assert resp.status_code == 200, resp.text
    state = _wait(client, resp.json()["job_id"])
    assert state["status"] == "success", state
    decks = client.get(f"/api/ppt/{course_id}").json()["chapters"]
    assert decks, state
    assert all(deck["theme"] == "navy" for deck in decks), [d["theme"] for d in decks]
    assert all(deck["theme_source"] == "course" for deck in decks)


# ---------------------------------------------------------------------------
# ⑤ 迁移脚本：dry-run / apply / 幂等（真跑子进程）
# ---------------------------------------------------------------------------

def test_backfill_script_dry_run_apply_and_idempotent(client, course_id):
    """迁移：dry-run 不写库；apply 只写该写的；第二遍 0 改动；依据逐门课可解释。"""
    course_deck = course_id                             # 整课 professional + 章 clean（真机形状）
    chapters_only = client.post("/api/course/", json={"title": "只有章节课件"}).json()["id"]
    no_deck = client.post("/api/course/", json={"title": "一份课件都没有"}).json()["id"]
    already = client.post("/api/course/", json={"title": "已显式设置"}).json()["id"]
    config_id = resolve_theme(get_settings().ppt_theme)[0]["id"]

    _add_item(course_deck, None, "ppt", "整课课件", {"pptx": "x.pptx"},
              {"scope": "course", "theme": "professional", "generated_at": "2026-10-08T11:14:28"})
    # 真机形状：同一门课里整课是 professional、章节课件还是老的 clean（混合）——
    # 回填取「整课课件」那一份（依据里要点名），并提示下次重出会统一
    _add_item(course_deck, None, "ppt", "第1章课件", {"pptx": "x1.pptx"},
              {"scope": "chapter", "theme": "clean", "generated_at": "2026-10-08T11:14:51"})
    _add_item(chapters_only, None, "ppt", "第1章课件", {"pptx": "y.pptx"},
              {"scope": "chapter", "theme": "clean", "generated_at": "2026-10-08T11:14:51"})
    assert _set_theme(client, already, "navy").status_code == 200
    _add_item(already, None, "ppt", "整课课件", {"pptx": "z.pptx"},
              {"scope": "course", "theme": "clean", "generated_at": "2026-10-08T11:15:00"})

    # dry-run：逐门课的「写 / 不动 + 值 + 依据」都印出来，且**一个字都不写**
    dry = _run_backfill("--course", course_deck)
    assert dry.returncode == 0, dry.stderr
    assert "会写 professional" in dry.stdout and "dry-run 未写库" in dry.stdout
    assert "整课课件" in dry.stdout and "注：本课课件主题**不一致**" in dry.stdout
    by_chapter = _run_backfill("--course", chapters_only)
    assert "会写 clean" in by_chapter.stdout and "没有整课课件" in by_chapter.stdout
    no_deck_run = _run_backfill("--course", no_deck)
    assert f"会写 {config_id}" in no_deck_run.stdout
    assert "没有任何带主题的课件" in no_deck_run.stdout
    kept = _run_backfill("--course", already)
    assert "不动 navy" in kept.stdout and "已显式设置" in kept.stdout and "写 0 门" in kept.stdout
    for cid in (course_deck, chapters_only, no_deck):
        assert client.get(f"/api/course/{cid}").json()["meta"].get("ppt_theme") is None
    assert client.get(f"/api/course/{already}").json()["meta"]["ppt_theme"] == "navy"

    for cid in (course_deck, chapters_only, no_deck, already):
        applied = _run_backfill("--course", cid, "--apply")
        assert applied.returncode == 0, applied.stderr
        assert "回读断言通过" in applied.stdout
    assert client.get(f"/api/course/{course_deck}").json()["meta"]["ppt_theme"] == "professional"
    assert client.get(f"/api/course/{chapters_only}").json()["meta"]["ppt_theme"] == "clean"
    assert client.get(f"/api/course/{no_deck}").json()["meta"]["ppt_theme"] == config_id
    assert client.get(f"/api/course/{already}").json()["meta"]["ppt_theme"] == "navy"

    for cid in (course_deck, chapters_only, no_deck, already):
        again = _run_backfill("--course", cid, "--apply")
        assert again.returncode == 0, again.stderr
        assert "写 0 门" in again.stdout and "回读断言通过" in again.stdout, again.stdout
    assert _run_backfill("--course", "no-such-course").returncode == 1
