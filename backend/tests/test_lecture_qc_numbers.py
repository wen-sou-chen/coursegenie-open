"""任务 106：引用数字对账（``numbers`` 项）—— 「标了来源」升级成「数字真的在被引段里」。

硬要求（HANDOFF「任务 106 说明」§A / §B）：
1. 只查**带来源标注**的文本单元（同一句 / 同一条列表项 / 同一行表格），锚点新旧两种写法
   （``（来源:《X》第 N 段）`` 与 ``（来源:《X》06:47）``，含 ``AI补充——`` 前缀）都要生效；
2. 三个结论：在被引段里 ⇒ 过；不在被引段但在**同一素材**的别的段 ⇒ 提示级（报「疑似出自…」，
   本项仍是 ok）；整个素材里都没有 ⇒ 本项 **warn**（不加 fail、不动权重、不动版本号）；
3. 必须排除（否则噪声爆表 —— 真实语料上 naive 实现 16 个单元全报）：图卡 ID / 路径、
   行首列表与步骤序号、表格首列纯序号、「编号词 + 数字」、「随堂练习」之后的一切、
   代码块、URL / 文件名里的数字；
4. 中文数字 v1 **不做**（「三百个」），见模块口径；判据不该因此误报。

纯函数断言（喂 Markdown + 手工 catalog）：不碰真实库、不调 LLM（0 LLM）。
"""
from __future__ import annotations

from app.services import lecture as lecture_mod
from app.services import lecture_qc as qc_mod
from app.services.lecture_qc import (_WEIGHT, CHECK_KEYS, CHECK_LABELS, numbers_audit,
                                     numbers_in_unit, prompt_section)

VIDEO_TITLE = "2026-01-08 16-26-13"
VIDEO = {"id": "v1", "index": 1, "title": VIDEO_TITLE,
         "paragraphs": ["[00:00] 开场白，这一句里没有数字。",
                        "[00:25] 这一段也没有数字。",
                        "[00:57] 你看他一共有156个视频。",
                        "[01:32] 这些视频都一键下回来了。",
                        "[09:20] 前面那个单客有 758个视频。"]}
BOOK = {"id": "t1", "index": 2, "title": "破解AI幻觉",
        "paragraphs": ["第 1 段：说话要有依据。",
                       "第 2 段：这里有 2 个要点，第 3 段继续。",
                       "第 3 段：别把训练数据当依据。"]}
CAP = {"id": "t2", "index": 3, "title": "容量手册",
       "paragraphs": ["第 1 段：单次最多导入1000个视频。", "第 2 段：其余内容。"]}
CATALOG = [VIDEO, BOOK, CAP]


def _audit(markdown: str) -> dict:
    return numbers_audit(markdown, CATALOG)


def _check(markdown: str) -> dict:
    return qc_mod._numbers_check(markdown, CATALOG)


def _values(rows: list[dict]) -> list[str]:
    return [row["value"] for row in rows]


# ---------------------------------------------------------------------------
# ① 三种结论：在被引段里 / 出自别的段 / 素材里都没有
# ---------------------------------------------------------------------------

def test_number_inside_the_cited_paragraph_passes():
    """时间戳锚点：156 就在 [00:57] 那段里 ⇒ 过（本项 ok、明细里 verdict=ok）。"""
    audit = _audit("- 单口博主一共有156个视频（来源:《2026-01-08 16-26-13》00:57）")

    assert audit["checked"] == 1 and audit["numbers"] == 1
    assert _values(audit["ok"]) == ["156"] and not audit["hints"] and not audit["warns"]
    assert _check("- 单口博主一共有156个视频（来源:《2026-01-08 16-26-13》00:57）")["level"] == "ok"


def test_number_from_another_paragraph_is_only_a_hint():
    """758 出自 09:20，却被引到 01:32 ⇒ 提示级：本项仍 ok，detail 点名「疑似出自 09:20」。"""
    markdown = "- 单口博主的758个视频可一键导入（来源:《2026-01-08 16-26-13》01:32）"
    audit = _audit(markdown)
    check = _check(markdown)

    assert _values(audit["hints"]) == ["758"] and not audit["warns"]
    assert audit["hints"][0]["where"] == "09:20"
    assert check["level"] == "ok", "出自别的段不是编造 ⇒ 不许提到 warn"
    assert "疑似出自 09:20" in check["detail"]


def test_number_missing_from_the_whole_material_is_warn():
    """300 整个素材里都没有 ⇒ 本项 warn（规格 §A2 的第三种结论）。"""
    markdown = "- 学习某英文博主的300个视频时（来源:《2026-01-08 16-26-13》00:25）"
    check = _check(markdown)

    assert check["level"] == "warn" and "300" in check["detail"]
    assert [row["value"] for row in check["numbers"]] == ["300"]
    assert check["numbers"][0]["line"] == 1


def test_old_paragraph_anchor_still_audits_numbers():
    """旧写法「第 N 段」照旧生效（历史讲义 + 文本素材）。"""
    audit = _audit("- 这里有 2 个要点（来源:《破解AI幻觉》第 2 段）")
    assert _values(audit["ok"]) == ["2"], audit
    assert _check("- 这里有 999 个要点（来源:《破解AI幻觉》第 2 段）")["level"] == "warn"


def test_unresolvable_anchor_is_not_reported_twice():
    """锚点对不上（时间戳不存在 / 素材名不认识）⇒ 只归 unverified，不在这里报警。"""
    audit = _audit("- 这里有 999 个视频（来源:《2026-01-08 16-26-13》99:99）\n"
                   "- 那里有 888 个视频（来源:《查无此素材》第 1 段）")

    assert not audit["warns"] and not audit["hints"] and not audit["ok"]
    assert len(audit["unverified"]) == 2
    assert _check("- 这里有 999 个视频（来源:《2026-01-08 16-26-13》99:99）")["level"] == "ok"


def test_same_number_is_reported_once_per_document():
    """同一个数字整篇只出一条（取最强结论），否则条数虚高。"""
    audit = _audit("- 第一句有999个（来源:《2026-01-08 16-26-13》00:57）\n"
                   "- 第二句也有999个（来源:《2026-01-08 16-26-13》01:32）")

    assert _values(audit["warns"]) == ["999"]
    assert audit["checked"] == 2 and audit["numbers"] == 1


def test_numbers_in_one_sentence_are_reported_as_one_entry():
    """同一句里的多个数字合并成 **1 条**（都点名）。

    真实语料（讲义-1.1）那句 `如156-758个` 是「一句双标注」：156 出自 00:57、758 出自 09:20，
    被引的是 00:00 与 01:32 ⇒ 两个都对不上、但同属一句。报 1 条（条数按**文本单元**算，
    与规格的 naive 基线「16 个来源单元 → 报了 13 条」同一个口径），两个数字都在 detail 里点名。
    """
    markdown = ("- 知识博主视频数量庞大（如156-758个），逐一看完耗时费力"
                "（来源:《2026-01-08 16-26-13》00:00）（来源:《2026-01-08 16-26-13》01:32）")
    audit = _audit(markdown)

    assert audit["checked"] == 1 and audit["numbers"] == 1, audit
    assert len(audit["hints"]) == 1 and not audit["warns"], audit
    assert audit["hints"][0]["values"] == ["156", "758"]
    assert audit["hints"][0]["wheres"] == {"156": "00:57", "758": "09:20"}

    check = _check(markdown)
    assert check["level"] == "ok"
    assert "疑似出自 00:57" in check["detail"] and "疑似出自 09:20" in check["detail"]


# ---------------------------------------------------------------------------
# ② 排除项：噪声红线（规格 §A3 / §一 的 naive 实测）
# ---------------------------------------------------------------------------

def test_chart_and_diagram_ids_are_not_numbers():
    """图卡 ID 与 alt 里的数字都不算（naive 实现最大的噪声源）。"""
    markdown = ("![数据卡：插件安装3步骤](chart://7c82333c4674484dbc086867acb628ec)"
                "（来源:《2026-01-08 16-26-13》01:32）\n"
                "![概念卡](diagram://b48a9751baf2497b9e634c0f4bb54d49)"
                "（来源:《2026-01-08 16-26-13》01:32）")

    audit = _audit(markdown)
    assert audit["checked"] == 0 and audit["numbers"] == 0, audit
    assert _check(markdown)["level"] == "ok"


def test_step_numbers_table_ordinals_and_label_numbers_are_excluded():
    """行首步骤号 / 表格首列序号 /「案例1」这类编号都不是内容数字。"""
    markdown = ("1. 安装浏览器插件（来源:《2026-01-08 16-26-13》01:32）\n"
                "| 1 | 打开插件商店 | 定位插件页面（来源:《2026-01-08 16-26-13》01:32） |\n"
                "| 2 | 点击安装 | 安装完成（来源:《2026-01-08 16-26-13》01:32） |\n"
                "- 案例1：单口博主的批量导入（来源:《2026-01-08 16-26-13》01:32）\n"
                "- 第 2 步的配置（来源:《2026-01-08 16-26-13》01:32）")

    audit = _audit(markdown)
    assert audit["checked"] == 0 and audit["numbers"] == 0, audit
    assert _check(markdown)["level"] == "ok"


def test_practice_section_is_not_audited():
    """「## 随堂练习」之后的一切（题号 / 用时 / 评分要点 / 答案 / 解析）都不查。"""
    markdown = ("## 小结\n\n本节讲怎么导入视频（来源:《2026-01-08 16-26-13》00:57）。\n\n"
                "## 随堂练习\n\n### 基础\n\n"
                "#### 第 1 题 · 选择题（难度：易，约 3 分钟）\n\n"
                "**评分要点**：① 技术实现 1分 ② 应用场景 1分\n\n"
                "**解析**：插件能导入566个视频（来源:《2026-01-08 16-26-13》01:32）\n")

    audit = _audit(markdown)
    assert audit["checked"] == 0 and audit["numbers"] == 0, audit


def test_code_fences_urls_and_file_names_are_excluded():
    """代码块 / URL / 文件名里的数字不算。"""
    markdown = ("```bash\ncurl https://example.com/158.mp4 -o 158.mp4\n```\n\n"
                "见 `素材-758.md` 与 https://a.example.com/1000.html"
                "（来源:《2026-01-08 16-26-13》01:32）\n")

    audit = _audit(markdown)
    assert audit["numbers"] == 0, audit
    assert _check(markdown)["level"] == "ok"


def test_units_without_citation_are_not_audited():
    """不带来源标注的句子不查（本项只管「标了来源的数字」）。"""
    audit = _audit("- 这里有 999 个视频，没有任何来源标注")
    assert audit["checked"] == 0 and audit["warns"] == []


# ---------------------------------------------------------------------------
# ③ 取数细节：千分位 / % / 中文数字
# ---------------------------------------------------------------------------

def test_thousands_separator_and_percent_are_normalized():
    """``1,000`` 归一成 ``1000``；``%`` 只留数字 —— 归一后才拿去和素材比对。"""
    assert numbers_in_unit("单次最多 1,000 个视频") == ["1000"]
    assert numbers_in_unit("命中率 1000%") == ["1000"]
    assert numbers_in_unit("段首 [00:57] 有 156 个") == ["00", "57", "156"]   # 没剥锚点时是三个

    ok = _audit("- 单次最多导入1,000个视频（来源:《容量手册》第 1 段）")
    assert _values(ok["ok"]) == ["1000"], ok


def test_chinese_numbers_are_out_of_scope_v1():
    """中文数字 v1 不做（「三百个」不报）——口径写在 lecture_qc 任务 106 那一段。"""
    audit = _audit("- 单口博主有三百个视频（来源:《2026-01-08 16-26-13》00:57）")
    assert audit["checked"] == 0 and audit["numbers"] == 0


# ---------------------------------------------------------------------------
# ④ 结构 / 口径守护：第 11 项，只出 ok|warn，不动权重与版本号
# ---------------------------------------------------------------------------

def test_numbers_is_the_eleventh_check_and_never_fails():
    assert len(CHECK_KEYS) == 11
    assert CHECK_KEYS.index("numbers") == CHECK_KEYS.index("sources") + 1
    assert CHECK_KEYS[-1] == "actionable"
    assert CHECK_LABELS["numbers"] == "数字与来源不符"
    assert _WEIGHT == {"warn": 6, "fail": 20}
    assert lecture_mod.QC_RULES_VERSION == "qc-v2" and qc_mod.QC_VERSION == 3
    assert lecture_mod.PROMPT_TEMPLATE_VERSION == "lecture-v14"

    qc = qc_mod.compute_qc("- 学习某英文博主的300个视频时（来源:《2026-01-08 16-26-13》00:25）",
                           catalog=CATALOG)
    numbers = [c for c in qc["checks"] if c["key"] == "numbers"]
    assert len(numbers) == 1 and numbers[0]["level"] == "warn"
    assert {c["level"] for c in qc["checks"]} <= {"ok", "warn", "fail", "skipped"}
    assert numbers[0]["level"] in {"ok", "warn"}, "numbers 只出 ok·warn，绝不 fail"
    hint = qc_mod.compute_qc("- 单口博主的758个视频可一键导入"
                             "（来源:《2026-01-08 16-26-13》01:32）", catalog=CATALOG)
    assert [c["level"] for c in hint["checks"] if c["key"] == "numbers"] == ["ok"]


def test_warn_detail_flows_into_the_regeneration_prompt():
    """复用既有「按质检建议重生成」注入：numbers 的 warn 必须出现在提示词段里。"""
    md = "- 学习某英文博主的300个视频时（来源:《2026-01-08 16-26-13》00:25）"
    qc = qc_mod.compute_qc(md, catalog=CATALOG)

    section = prompt_section(qc)
    assert "[numbers/warn]" in section and "300" in section


def test_detail_is_ok_when_nothing_to_audit():
    check = _check("## 小结\n\n本节没有数字（来源:《2026-01-08 16-26-13》00:57）。\n")
    assert check["level"] == "ok" and "没有带数字的来源标注" in check["detail"]
