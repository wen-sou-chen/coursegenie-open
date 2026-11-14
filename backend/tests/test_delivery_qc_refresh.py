"""任务 112：出包前按现行判据现算质检 —— 验收单 / 课程体检报告的分数与「现算」一致。

起因（DSH 2026-09-29 实测）：任务 106 给质检加了第 11 项（``numbers``）之后，存量讲义的
QC 还停在旧口径（真库 33 篇实测 ``total_checks=10``、没有 ``numbers``），出包时就把旧分数
写进了 ``01-验收单.md`` 与笔记库 ``00-课程体检报告.md`` ⇒ 两个文件互相一致，却都和
「按现行判据现算」对不上（AI 课 94.4 vs 91.56、包子课 88.0 vs 87.00）。

本文件覆盖 HANDOFF 任务 112 §A5 的三条：
① 库里存的是旧口径分数 ⇒ 出包前重算，验收分数 == 现算；
② 重算后的分数**落库**（不是只塞进文件）；
③ 判据口径一致时**不重复重算**（幂等，别每次出包都白跑）。

协作规则第 7 条：DATABASE_URL / STORAGE_DIR / OBSIDIAN_VAULT_ROOT 由 conftest 统一指向
临时目录，本文件不碰真实库与真实知识库。
"""
from __future__ import annotations

import re

from app.db.session import SessionLocal
from app.models.course import ContentItem, Course, OutlineNode
from app.services import delivery as delivery_mod
from app.services import lecture as lecture_mod
from app.services import lecture_qc as lecture_qc
from app.services.lecture_qc import CHECK_KEYS, QC_VERSION, qc_outdated, rules_label
from tests.test_delivery import (_add_section, _build, _delivery_dir, _gen_lecture, _new_course,
                                 _node_id, _plan_approve, _seed_material)

# 与 `工具\交付包核验.py` 第 8 项同一口径：验收单里「平均分 / 均分」后面那个数字
_ACCEPT_AVG = re.compile(r"(平均分|均分)[^0-9]{0,12}(\d{2,3}(?:\.\d+)?)")

#: 讲义里引了一个**素材里没有的数字**（``1000``）⇒ 现行判据的第 11 项 ``numbers`` 记 warn，
#: 旧口径（10 项）算出来是 100 分 —— 两边必然不同，正是本任务要治的那种偏差。
LECTURE_MD_WITH_UNSOURCED_NUMBER = """## 光合作用的过程

### 引入
- 一次导入超过 1000 个叶片样本也能对齐大纲（来源: 光合作用导论 #1）

### 核心讲解
- **光反应**发生在类囊体薄膜上，把光能变成 ATP（来源: 光合作用导论 #2）
- **暗反应**在基质中把二氧化碳固定成糖（来源: 光合作用导论 #2）

### 小结
- 光反应供能、暗反应固碳（来源: 光合作用导论 #3）
"""


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _lectures(course_id: str) -> list[ContentItem]:
    db = SessionLocal()
    try:
        return (db.query(ContentItem)
                .filter(ContentItem.course_id == course_id, ContentItem.kind == "lecture")
                .order_by(ContentItem.created_at).all())
    finally:
        db.close()


def _stored_qc(item_id: str) -> dict:
    db = SessionLocal()
    try:
        return dict((db.get(ContentItem, item_id).meta or {}).get("qc") or {})
    finally:
        db.close()


def _fresh_scores(course_id: str) -> list[int]:
    """现算：走 ``lecture_qc.build_for_item``（= 交付包核验工具的现算路径，只算不写库）。"""
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        out: list[int] = []
        for item in (db.query(ContentItem)
                     .filter(ContentItem.course_id == course_id, ContentItem.kind == "lecture")
                     .order_by(ContentItem.created_at).all()):
            node = db.get(OutlineNode, item.node_id) if item.node_id else None
            fresh = lecture_qc.build_for_item(db, course, node, item,
                                             lecture_qc.lecture_markdown(item))
            out.append(int(fresh["qc"]["score"]))
        return out
    finally:
        db.close()


def _freeze_old_ruleset_qc(item_id: str) -> dict:
    """把库里那份 QC 改回「任务 106 之前」的形态：10 项（没有 ``numbers``）+ 一个旧口径分数。

    真实现场的存量数据就是这个样子（真库 33 篇实测 ``total_checks=10``、无 ``numbers``，
    分数是 10 项口径算出来的 94.4 / 88.0）。旧分数这里写成 **100**：本用例要证明的是
    「出包前必须重算」，所以「库里那份」与「现算」**必须不同** —— 这一点真实现场也一样
    （94.4 vs 91.56、88.0 vs 87.00）。
    """
    db = SessionLocal()
    try:
        item = db.get(ContentItem, item_id)
        meta = dict(item.meta or {})
        qc = dict(meta["qc"])
        checks = [dict(c) for c in qc["checks"] if c.get("key") != "numbers"]
        qc["checks"] = checks
        qc["total_checks"] = len(checks)
        qc["executed_checks"] = len(checks)
        qc["skipped_count"] = 0
        qc["skipped_keys"] = []
        qc["score"] = 100
        qc.pop("downgraded_keys", None)
        meta["qc"] = qc
        item.meta = meta
        db.commit()
        return {"score": int(qc["score"]), "total_checks": int(qc["total_checks"])}
    finally:
        db.close()


def _accept_avg(text: str) -> float:
    match = _ACCEPT_AVG.search(text)
    assert match, "这份报告里没有「平均分 / 均分」那一行"
    return float(match.group(2))


def _course_one_lecture(client, monkeypatch) -> dict:
    """最小可出包的课：1 个知识点 + 1 篇讲义（含一个素材里没有的数字）。"""
    course_id = _new_course(client, "出包前现算质检")
    _plan_approve(client, course_id)
    _add_section(course_id, "1.1", "光合作用的过程")
    node_id = _node_id(client, course_id, "1.1")
    _seed_material(course_id)
    item = _gen_lecture(client, course_id, node_id, LECTURE_MD_WITH_UNSOURCED_NUMBER, monkeypatch)
    return {"course_id": course_id, "node_id": node_id, "item": item}


# ---------------------------------------------------------------------------
# ⓪ 判定侧：判据集合变了 ⇒ 库里那份 QC 判过期（只改「什么时候算」）
# ---------------------------------------------------------------------------

def test_qc_outdated_when_stored_ruleset_is_old():
    """``total_checks`` 记着旧项数（10）⇒ 过期；现行 11 项 ⇒ 不过期；没记的老数据照旧不过期。"""
    def _qc(keys, total=None):
        row: dict = {"version": QC_VERSION,
                     "checks": [{"key": key, "level": "ok"} for key in keys]}
        if total is not None:
            row["total_checks"] = total
        return row

    assert qc_outdated(_qc(CHECK_KEYS[:10], total=10)) is True          # 任务 106 之前的存量数据
    assert qc_outdated(_qc(CHECK_KEYS, total=len(CHECK_KEYS))) is False
    # 项数一样、判据换了（同一位置换了一门检查）也要重算
    assert qc_outdated(_qc([*CHECK_KEYS[:10], "brand_new_check"],
                           total=len(CHECK_KEYS))) is True
    # 老数据没记项数：照旧不判过期（不为了这次改动把全库都判过期）
    assert qc_outdated({"version": QC_VERSION,
                        "checks": [{"key": "sources", "level": "ok", "sentences": []}]}) is False


def test_rules_weights_and_versions_are_untouched():
    """本任务只改「什么时候算、用哪份数字」：判据 / 权重 / 阈值 / 版本号一个字节不动。"""
    assert lecture_qc.CHECK_KEYS == ("structure", "fluff", "length", "objective", "terminology",
                                     "figures", "quiz", "sources", "numbers", "substance",
                                     "actionable")
    assert lecture_qc.QC_VERSION == 3
    assert lecture_qc._WEIGHT == {"warn": 6, "fail": 20}
    assert lecture_mod.QC_RULES_VERSION == "qc-v2"
    assert lecture_mod.PROMPT_TEMPLATE_VERSION == "lecture-v14"
    assert delivery_mod.BUILD_VERSION == "M5c-1"
    assert rules_label() == "qc-v2 · 11 项"       # 验收单 / 体检报告标注的就是这一行


# ---------------------------------------------------------------------------
# ① 旧口径 ⇒ 出包前重算，验收分数 == 现算；② 重算结果落库
# ---------------------------------------------------------------------------

def test_build_recomputes_stale_qc_before_acceptance_sheet(client, monkeypatch):
    info = _course_one_lecture(client, monkeypatch)
    frozen = _freeze_old_ruleset_qc(info["item"]["id"])
    assert frozen == {"score": 100, "total_checks": 10}       # 库里存的是旧口径
    fresh = _fresh_scores(info["course_id"])
    assert len(fresh) == 1
    assert fresh[0] != frozen["score"], "旧口径与现算两侧真的不同，这条用例才有意义"

    summary = _build(client, info["course_id"])

    out_dir = _delivery_dir(summary["build_id"])
    sheet = (out_dir / "01-验收单.md").read_text(encoding="utf-8")
    assert _accept_avg(sheet) == float(fresh[0]), "验收单均分 == 现算"
    assert "按现行判据现算" in sheet and rules_label() in sheet
    assert "旧口径重算 1 篇" in sheet and "已是最新 0 篇" in sheet
    # 同一包里那份体检报告也要是现算口径（DSH 实测的第二个文件）
    report_path = out_dir / "笔记库" / "00-课程体检报告.md"
    assert report_path.is_file(), "包内应含笔记库体检报告"
    report = report_path.read_text(encoding="utf-8")
    assert _accept_avg(report) == float(fresh[0])
    assert rules_label() in report
    assert summary["qc_refresh"]["refreshed"] == 1
    assert summary["qc_refresh"]["current"] == 0

    # ② 落库：不是只塞进文件里 —— meta.qc 已是现行 11 项 + 现算分数
    stored = _stored_qc(info["item"]["id"])
    assert int(stored["score"]) == fresh[0]
    assert int(stored["total_checks"]) == len(CHECK_KEYS)
    assert sorted(str(c.get("key")) for c in stored["checks"]) == sorted(CHECK_KEYS)


# ---------------------------------------------------------------------------
# ③ 幂等：同一份包连出两次，第二次不触发重算
# ---------------------------------------------------------------------------

def test_second_build_does_not_recompute_qc(client, monkeypatch):
    info = _course_one_lecture(client, monkeypatch)
    _freeze_old_ruleset_qc(info["item"]["id"])

    calls: list[str] = []
    real_refresh_item = lecture_qc.refresh_item

    def _counting_refresh_item(db, course, item, node=None, *, commit=True):
        calls.append(item.id)
        return real_refresh_item(db, course, item, node, commit=commit)

    monkeypatch.setattr(lecture_qc, "refresh_item", _counting_refresh_item)

    first = _build(client, info["course_id"])
    assert calls == [info["item"]["id"]], "第一次：旧口径那一篇要重算一次"
    assert first["qc_refresh"]["refreshed"] == 1
    stamped = _stored_qc(info["item"]["id"]).get("generated_at")

    second = _build(client, info["course_id"])
    assert calls == [info["item"]["id"]], "第二次：判据口径一致 ⇒ 一个新调用都不该有"
    assert second["qc_refresh"] == {"lectures": 1, "refreshed": 0, "current": 1,
                                    "failed": 0, "rules": rules_label()}
    sheet = (_delivery_dir(second["build_id"]) / "01-验收单.md").read_text(encoding="utf-8")
    assert "旧口径重算 0 篇" in sheet and "已是最新 1 篇" in sheet
    assert _stored_qc(info["item"]["id"]).get("generated_at") == stamped
