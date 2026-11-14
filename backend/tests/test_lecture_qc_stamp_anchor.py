"""任务 105：``parse_sources`` 新增**时间戳锚点**（音视频素材）—— 旧写法必须继续认。

三条硬要求（HANDOFF「任务 105 说明」§A4 / §B3）：
1. ``（来源:《录像名》06:47）`` 解析成功，且锚点**落在该素材某一段的起点上**
   （``paragraph`` = 那一段的段号、``material_quote`` = 那一段的正文）；
2. 对不上（素材里没有的时刻 / 落在段中 / 给文本素材写时间戳）⇒ 与「第 N 段越界」**同款处理**
   （来源条目照旧产出、段落号 0、摘要为空 —— 判定逻辑一个字不动）；
3. **旧写法「第 N 段」照旧解析**（旧讲义 / 文本素材 / 转写未完成的音视频）；
   区间 / 并列（``06:47-08:12`` / ``06:47、07:20``）照旧解析不了 ⇒ 走「缺来源标注」的 warn 路径。

纯函数断言（喂 Markdown + 手工 catalog）：不碰真实库、不调 LLM（0 LLM）。
"""
from __future__ import annotations

from app.services import lecture_qc as qc_mod
from app.services.lecture_qc import parse_sources

VIDEO_TITLE = "2026-01-08 16-26-13"
VIDEO = {"id": "v1", "index": 1, "title": VIDEO_TITLE,
         "paragraphs": ["[00:00] 开场白", "[06:47] YouTube to NotebookLM 插件",
                        "[08:12] 第二个要点", "[62:03] 一小时零两分"]}
HOUR = {"id": "v2", "index": 2, "title": "长录像",
        "paragraphs": ["[00:00] 开场", "[62:03] 一小时零两分三秒"]}
TEXT = {"id": "t1", "index": 3, "title": "破解AI幻觉",
        "paragraphs": [f"第 {i} 段的素材原文" for i in range(1, 61)]}
CATALOG = [VIDEO, HOUR, TEXT]


def _cite(line: str, catalog: list[dict] | None = None) -> dict | None:
    rows = parse_sources(line, "1.1", CATALOG if catalog is None else catalog)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# ① 时间戳锚点解析成功，且落在某一段起点上
# ---------------------------------------------------------------------------

def test_stamp_anchor_resolves_to_the_paragraph_starting_there():
    hit = _cite("插件是官方的（来源:《2026-01-08 16-26-13》06:47）")

    assert hit is not None, "时间戳锚点没解析出来"
    assert hit["material_id"] == "v1" and hit["material_name"] == VIDEO_TITLE
    assert hit["paragraph"] == 2                      # [06:47] 是第 2 段的段首
    assert hit["anchor_kind"] == "stamp" and hit["anchor"] == "06:47"
    assert hit["material_quote"] == "[06:47] YouTube to NotebookLM 插件"
    assert hit["quote"] == "插件是官方的"              # 锚点从原文里剥掉（同旧口径）


def test_stamp_anchor_accepts_hh_mm_ss_and_three_digit_minutes():
    """超 1 小时：段首是三位分钟 ``[62:03]``，模型写 ``1:02:03`` 也按秒对上。"""
    for written in ("62:03", "1:02:03"):
        hit = _cite(f"（来源:《长录像》{written}）")
        assert hit is not None, written
        assert hit["paragraph"] == 2 and hit["material_id"] == "v2", (written, hit)
        assert hit["anchor"] == written and hit["material_quote"] == "[62:03] 一小时零两分三秒"


def test_stamp_anchor_accepts_the_full_width_and_citation_variants():
    """书名号缺失 / 冒号全角 / 多余空白都是模型现场出现过的形态（与旧写法同一宽容度）。"""
    for line in ("（来源: 2026-01-08 16-26-13 06:47）",
                 "（来源：《2026-01-08 16-26-13》06:47）",
                 "(来源:2026-01-08 16-26-13 06:47)"):
        hit = _cite(line)
        assert hit is not None and hit["paragraph"] == 2, (line, hit)


# ---------------------------------------------------------------------------
# ② 对不上 ⇒ 与「第 N 段越界」同款处理（判定逻辑不动）
# ---------------------------------------------------------------------------

def test_unmatched_stamp_is_handled_like_out_of_range_paragraph():
    bad_stamp = _cite("（来源:《长录像》99:99）")           # 素材里根本没有的时刻
    bad_para = _cite("（来源:《破解AI幻觉》第 999 段）")     # 越界段号（现行口径）

    assert bad_stamp["material_id"] == "v2" and bad_para["material_id"] == "t1"
    assert bad_stamp["paragraph"] == 0 and bad_stamp["material_quote"] == ""
    assert bad_para["paragraph"] == 999 and bad_para["material_quote"] == ""
    # 段落号照旧留着原文（越界时的现状形状），时间戳锚点落在**段中**也不算
    assert _cite("（来源:《长录像》00:30）")["paragraph"] == 0
    # 给**文本素材**写时间戳同样对不上（它的段没有 [mm:ss]）
    assert _cite("（来源:《破解AI幻觉》06:47）")["paragraph"] == 0


def test_qc_path_is_unchanged_for_both_mismatch_kinds():
    """两条都对不上 ⇒ 都照旧算「有来源」（现状没有「段号对不上」判据，任务 105 不新增）。"""
    for md in ("- 插件是官方的（来源:《长录像》99:99）",
               "- 插件是官方的（来源:《破解AI幻觉》第 999 段）"):
        rows = parse_sources(md, "1.1", CATALOG)
        assert rows, md                                  # 来源条目照旧产出，不静默丢
        check = qc_mod._sources_check(md, rows, CATALOG)
        assert check["level"] == "ok", (md, check)

    # 解析不了的形态（区间 / 并列）照旧走「缺来源标注」的 warn 路径
    for md in ("- 插件是官方的（来源:《长录像》06:47-08:12）",
               "- 插件是官方的（来源:《长录像》06:47、07:20）"):
        assert parse_sources(md, "1.1", CATALOG) == [], md
        check = qc_mod._sources_check(md, [], CATALOG)
        assert check["level"] == "warn" and "1 条关键点" in check["detail"], (md, check)


# ---------------------------------------------------------------------------
# ③ 旧写法「第 N 段」必须继续解析（旧讲义 / 文本素材 / 转写未完成的音视频）
# ---------------------------------------------------------------------------

def test_legacy_paragraph_anchor_still_parses():
    for line, paragraph, material_id in (
            ("（来源:《破解AI幻觉》第 33 段）", 33, "t1"),
            ("（来源: 破解AI幻觉 #33）", 33, "t1"),
            ("（来源:《2026-01-08 16-26-13》第 2 段）", 2, "v1"),     # 音视频素材照旧认段号
            ("（来源: 2026-01-08 16-26-13 #2）", 2, "v1")):
        hit = _cite(line)
        assert hit is not None, line
        assert (hit["paragraph"], hit["material_id"]) == (paragraph, material_id), (line, hit)
        assert hit["anchor_kind"] == "para" and hit["material_quote"], (line, hit)


def test_old_miswritten_forms_are_still_not_parsed():
    """老口径没放宽：混写 / 无依据前缀一律不产出来源条目（靠提示词写对，不靠解析宽容）。"""
    for line in ("（来源: 无直接依据,建议人工确认）",
                 "（来源:《破解AI幻觉》第 80 段；无直接依据,建议人工确认）",
                 "（来源: 破解AI幻觉 #60、#75）"):
        assert parse_sources(line, "1.1", CATALOG) == [], line
