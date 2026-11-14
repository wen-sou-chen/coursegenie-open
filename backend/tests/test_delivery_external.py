"""任务 48p 回归测试：对外交付模式（客户包里不出现金额 / tokens / LLM 调用次数）。

任务 61 补一段：`笔记库/00-课程体检报告.md` 当初漏了（它在包里、客户会打开），
对外包必须按 external 口径重渲染，真 vault 那份保持内部可见。

起因（DSH 通读交付包）：`00-交付说明.md` 与 `01-验收单.md` 把**课程累计 LLM 用量与金额**
写给了客户（`81 次 / 421658 tokens / ≈¥0.890874`）—— 一门报价几千的课，包里写着成本不到一块钱。
本任务加「交付模式」：**默认对外**（金额 / tokens / 调用次数一律不写，改成一句中性说明），
`course.meta.delivery.external = false` 时才恢复内部口径。

协作规则第 7 条：DATABASE_URL / STORAGE_DIR / OBSIDIAN_VAULT_ROOT 由 conftest 统一指向
临时目录，本文件不碰真实库与真实知识库 / 不重出真库交付包。
"""
from __future__ import annotations

import re
import hashlib
import json
import zipfile
from pathlib import Path

from app.db.session import SessionLocal
from app.models.course import Course
from app.services import audit as audit_mod
from app.services import delivery as delivery_mod
from tests.test_delivery import _build, _course_with_content, _delivery_dir, _new_course

SECRETS = ("¥", "tokens", "LLM 用量", "M5c")
DOCS = ("00-交付说明.md", "01-验收单.md", "素材清单.md")


def _capture(course_id: str, body: dict) -> dict:
    """把一次构建的产物读进内存（同一个 build_id 会被下一次构建覆盖，必须先读）。"""
    out = _delivery_dir(body["build_id"])
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    return {
        "out": out,
        "manifest": manifest,
        "docs": {name: (out / name).read_text(encoding="utf-8") for name in DOCS},
        "files": {row["path"]: row["sha1"] for row in manifest["files"]},
    }


def _build_mode(client, course_id: str, external: bool | None = None) -> dict:
    payload: dict = {"pdf": "off"}
    if external is not None:
        payload["external"] = external
    return _capture(course_id, _build(client, course_id, payload))


def _secrets(text: str) -> dict:
    return {word: text.count(word) for word in SECRETS if word in text}


def test_external_mode_is_default_and_hides_usage(client, monkeypatch):
    """默认（课程没设过）就是对外：三个客户先看的文件里 `¥` / `tokens` / `LLM 用量` / `M5c` 全 0。"""
    course_id = _new_course(client, "对外模式探针")
    _course_with_content(client, course_id, monkeypatch)
    built = _build_mode(client, course_id)
    assert _secrets("".join(built["docs"].values())) == {}
    for name in DOCS:
        assert _secrets(built["docs"][name]) == {}, name
    readme = built["docs"]["00-交付说明.md"]
    assert "内容版本：v1" in readme and "M5c" not in readme
    assert delivery_mod.EXTERNAL_USAGE_NOTE in readme          # 中性说明替代用量行
    assert "构建号：" in readme                                 # 客户仍能对上包号
    assert "累计费用" not in built["docs"]["01-验收单.md"]      # 结论句里的金额被抹掉


def test_external_mode_manifest_marks_mode_and_drops_params(client, monkeypatch):
    """manifest 写明本次模式；对外模式下 params 里没有模型 / 用量 / 金额。"""
    course_id = _new_course(client, "对外 manifest 探针")
    _course_with_content(client, course_id, monkeypatch)
    built = _build_mode(client, course_id)
    manifest = built["manifest"]
    assert manifest["delivery_mode"] == "external"
    assert manifest["content_version"] == delivery_mod.CONTENT_VERSION
    assert "build_version" not in manifest
    for key in ("model", "course_llm_calls", "course_total_tokens", "course_cost_cny"):
        assert key not in manifest["params"], key
    assert manifest["llm_calls"] == 0                 # 构建本身 0 次调用：两种模式都写明


def test_internal_mode_keeps_usage_and_version(client, monkeypatch):
    """内部复盘模式（external=false）：用量与金额照旧，逐字与今天的包一致。"""
    course_id = _new_course(client, "内部模式探针")
    _course_with_content(client, course_id, monkeypatch)
    built = _build_mode(client, course_id, external=False)
    readme = built["docs"]["00-交付说明.md"]
    assert "交付包版本：M5c-1" in readme
    assert "课程累计 LLM 用量：" in readme and "tokens" in readme and "≈¥" in readme
    assert "累计费用" in built["docs"]["01-验收单.md"]
    manifest = built["manifest"]
    assert manifest["delivery_mode"] == "internal"
    assert manifest["build_version"] == delivery_mod.BUILD_VERSION
    for key in ("model", "course_llm_calls", "course_total_tokens", "course_cost_cny"):
        assert key in manifest["params"], key


def test_mode_only_changes_the_two_docs(client, monkeypatch):
    """两种模式：其它数字与其它文件逐条不变，只有 00-交付说明 / 01-验收单 的 sha1 变。"""
    course_id = _new_course(client, "模式对照探针")
    _course_with_content(client, course_id, monkeypatch)
    external = _build_mode(client, course_id, external=True)
    internal = _build_mode(client, course_id, external=False)

    assert external["manifest"]["counts"] == internal["manifest"]["counts"]
    assert external["manifest"]["acceptance"] == internal["manifest"]["acceptance"]
    assert external["docs"]["素材清单.md"] == internal["docs"]["素材清单.md"]
    changed = {path for path, sha in external["files"].items()
               if internal["files"].get(path) != sha}
    # 任务 59 起 docx 已确定化（ZIP 时间戳归一化）⇒ 不再排除 .docx：
    # 「两次构建逐字节一致」这条现在覆盖到教案/学案 docx。
    # 任务 61：包内那份体检报告也随模式重渲染（真 vault 的那份不在包里、也不动）
    assert changed == {"00-交付说明.md", "01-验收单.md", "笔记库/00-课程体检报告.md"}, changed


def test_manifest_sha1_matches_zip_in_both_modes(client, monkeypatch):
    """两种模式的包都自洽：manifest 里每条 sha1 == zip 内同名文件（核验脚本第 1 项）。"""
    course_id = _new_course(client, "sha1 对照探针")
    _course_with_content(client, course_id, monkeypatch)
    for external in (True, False):
        built = _build_mode(client, course_id, external=external)
        zip_path = Path(str(built["out"]) + ".zip")
        with zipfile.ZipFile(zip_path) as zf:
            for path, sha in built["files"].items():
                assert hashlib.sha1(zf.read(path)).hexdigest() == sha, (external, path)


def test_mode_persists_and_preview_shows_it(client, monkeypatch):
    """模式写回课程：预览接口能读到，切换后按新模式出包（默认对外）。"""
    course_id = _new_course(client, "模式落库探针")
    _course_with_content(client, course_id, monkeypatch)
    assert client.get(f"/api/delivery/course/{course_id}").json()["delivery_mode"]["external"] is True

    _build_mode(client, course_id, external=False)
    assert client.get(f"/api/delivery/course/{course_id}").json()["delivery_mode"]["mode"] == "internal"

    _build_mode(client, course_id, external=True)
    payload = client.get(f"/api/delivery/course/{course_id}").json()
    assert payload["delivery_mode"]["external"] is True
    assert payload["preview"]["delivery_mode"]["mode"] == "external"


def test_public_summary_drops_the_cost_clause():
    """结论句里的「累计费用 ≈… 元」在对外模式下必须被抹掉（0 LLM、不动 audit 口径）。"""
    summary = ("2 个知识点 / 2 篇讲义；坏链 0 条、不一致 0 项、质检平均 92.9 分、"
               "累计费用 ≈0.890874 元。")
    public = delivery_mod._public_summary(summary)
    assert "累计费用" not in public and "¥" not in public
    assert public.endswith("质检平均 92.9 分。")
    assert delivery_mod._public_summary("没有费用这句话。") == "没有费用这句话。"


def test_mode_defaults_to_external_without_meta():
    """course.meta 里没写过 delivery → 对外（宁可少说内部信息）。"""
    db = SessionLocal()
    try:
        course = Course(title="默认模式探针", meta={})
        assert delivery_mod.delivery_mode(course)["external"] is True
        course.meta = {"delivery": {"external": False}}
        assert delivery_mod.delivery_mode(course)["mode"] == "internal"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 任务 61：交付包里 `笔记库/00-课程体检报告.md` 也必须按模式走
# ---------------------------------------------------------------------------

REPORT_REL = "笔记库/00-课程体检报告.md"
# 与 DSH 的整包扫描同口径（工具\交付包核验.py 第 12 项）
COST_RX = (
    re.compile(r"≈\s*\d+(?:\.\d+)?\s*元"),
    re.compile(r"¥"),
    re.compile(r"\btokens\b", re.I),
    re.compile(r"LLM (?:调用|用量)\s*\d+"),
    re.compile(r"累计 tokens"),
    re.compile(r"平均单篇"),
    re.compile(r"累计费用"),
    re.compile(r"M5c"),
)


def _cost_hits(text: str) -> list[str]:
    return [rx.pattern for rx in COST_RX if rx.search(text)]


def _vault_report_path(course_id: str) -> Path:
    from app.services import kb as kb_mod

    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        _root, out_dir, _rel = kb_mod._course_out_dir(course)   # 未绑定时的默认目录也一致
    finally:
        db.close()
    return out_dir / audit_mod.REPORT_NAME


def _normalized(markdown: str) -> list[str]:
    """把「成本相关」的位置归一后逐行返回（用于断言其余内容一字不动）。

    归一三处：① 第 6 段整段（内部是成本表、对外是中性说明）；② 结论句里的成本从句；
    ③ 建议动作里那条内部运维提示的括注（对外把「元/百万 tokens」换成「单价表」）；
    ④ frontmatter 的 `cg_fingerprint`（按正文算，正文不同值自然不同）。
    """
    out: list[str] = []
    in_six = False
    for line in markdown.splitlines():
        if line.startswith("## 6."):
            in_six = True
            out.append("## 6. <成本段>")
            continue
        if in_six:
            if not line.startswith("## 7."):
                continue
            in_six = False
        if line.startswith("- 一句话结论："):
            line = audit_mod.public_summary(line)
        for hint in ("（元/百万 tokens）", "（单价表）"):
            line = line.replace(hint, "（单价表）")
        if line.startswith("cg_fingerprint:"):
            line = "cg_fingerprint: <按正文算>"
        out.append(line)
    return out


def test_external_package_report_hides_cost_but_vault_keeps_it(client, monkeypatch):
    """任务 61：对外包里那份体检报告没有成本字样；真 vault 那份仍有（只换包内副本）。"""
    course_id = _new_course(client, "报告脱敏探针")
    _course_with_content(client, course_id, monkeypatch)
    vault_path = _vault_report_path(course_id)

    external = _build_mode(client, course_id, external=True)
    pkg = (external["out"] / REPORT_REL).read_text(encoding="utf-8")
    vault = vault_path.read_text(encoding="utf-8")

    assert _cost_hits(pkg) == [], _cost_hits(pkg)
    assert delivery_mod.EXTERNAL_USAGE_NOTE in pkg          # 换成中性说明
    assert audit_mod.EXTERNAL_COST_NOTE in pkg
    assert "## 6. 内容生成说明" in pkg and "## 6. 用量与成本" not in pkg
    assert "累计费用" in vault and _cost_hits(vault) != []   # 真 vault 一个字没改
    assert _normalized(vault) == _normalized(pkg)            # 只有成本段 / 结论句的成本从句不同
    score = next(l for l in pkg.splitlines() if l.startswith("- **总分"))
    assert score in vault                                    # 客户最该看的数字一字不变

    internal = _build_mode(client, course_id, external=False)
    pkg_in = (internal["out"] / REPORT_REL).read_text(encoding="utf-8")
    assert "## 6. 用量与成本" in pkg_in                       # 不是一刀切删掉
    assert "累计费用" in pkg_in and "tokens" in pkg_in
    assert delivery_mod.EXTERNAL_USAGE_NOTE not in pkg_in
    assert vault_path.read_text(encoding="utf-8") == vault


def test_render_report_external_drops_cost_lines_only(client, monkeypatch):
    """任务 61：render_report(external=True) 不含 ≈N元 / tokens / LLM 调用 N，其余逐字一致。"""
    course_id = _new_course(client, "报告渲染探针")
    _course_with_content(client, course_id, monkeypatch)
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        inner = audit_mod.course_report(db, course)["markdown"]
        outer = audit_mod.course_report(db, course, external=True)["markdown"]
    finally:
        db.close()
    assert _cost_hits(inner) != []                       # 内部口径照旧有成本
    assert _cost_hits(outer) == [], _cost_hits(outer)
    assert _normalized(inner) == _normalized(outer)
