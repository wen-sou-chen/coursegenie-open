"""任务 105：音视频素材的来源锚点改用**时间戳**（+ 转写分段 350 → 120）。

四条硬要求（HANDOFF「任务 105 说明」§A / §B）：
1. **锚点口径** ``anchor_kind``：音视频素材**且有转写正文** ⇒ ``"stamp"``；其余（转写未完成的
   音视频 / 文本 / 图片 / AI 补充素材）⇒ ``"para"``；
2. **注入文本**：``stamp`` 素材的段落**不再叠** ``[N]``（段首本来就是 ``[mm:ss]``），
   ``para`` 素材照旧 ``[N]``；注入说明按**本次真正注入的素材**给措辞（两种口径都在 ⇒ 都写）；
3. **分段粒度**：``ASR_SEGMENT_MAX_CHARS`` 350 → 120（仍按语音停顿合并、段首仍 ``[mm:ss]``）；
4. **文案**：素材页备注 / 阅读图例 / 交付包素材清单都写明「音视频素材的引用是时间戳」。

纪律：``DATABASE_URL`` / ``STORAGE_DIR`` / ``OBSIDIAN_VAULT_ROOT`` 由 ``conftest`` 指向临时目录，
本文件只在临时库里造素材行 —— 不碰真实库、不调 LLM（0 LLM）。

反证据（§B6）：把 ``anchor_kind`` 强制回 ``"para"`` ⇒ 口径 / 注入文本 / 素材页备注三条必红。
"""
from __future__ import annotations

from app.db.session import SessionLocal
from app.models.course import Course, SourceMaterial
from app.services import delivery as delivery_mod
from app.services import kb as kb_mod
from app.services import material as material_mod

VIDEO_TITLE = "2026-01-08 16-26-13"
VIDEO_TEXT = ("[00:00] 开场：今天讲三个要点。\n\n"
              "[00:25] 第一个要点是安装插件。\n\n"
              "[00:52] 第二个要点是选笔记本。")
TEXT_TITLE = "破解AI幻觉"
TEXT_BODY = "第一段。\n\n第二段。"
PARA_NOTICE = "文本素材写「（来源:《素材标题》第 N 段）」"
STAMP_NOTICE = "音视频素材的每段行首是 [mm:ss]"


def _make(course_id: str, *, kind: str, title: str, text: str, **meta) -> str:
    """在临时库里造一条素材行（等价于入库 / 转写后的状态），返回素材 id。"""
    db = SessionLocal()
    try:
        row = SourceMaterial(course_id=course_id, title=title, kind=kind, file_rel="",
                             text=text,
                             meta={"ext": ".mp4" if kind in ("video", "audio") else ".text",
                                   "chars": len(text), "original": "", **meta})
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    finally:
        db.close()


def _kind(material_id: str) -> str:
    """锚点口径**走生产函数**读（本地复写的话变异反证就抓不住了）。"""
    db = SessionLocal()
    try:
        return material_mod.anchor_kind(db.get(SourceMaterial, material_id))
    finally:
        db.close()


def _plan(course_id: str, **kwargs) -> dict:
    db = SessionLocal()
    try:
        return material_mod.plan_injection(db, db.get(Course, course_id), **kwargs)
    finally:
        db.close()


def _material_page(course_id: str, material_id: str) -> str:
    db = SessionLocal()
    try:
        return "\n".join(kb_mod._material_page(db.get(Course, course_id),
                                               db.get(SourceMaterial, material_id)))
    finally:
        db.close()


def _delivery_material_lines(course_id: str) -> str:
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        return "\n".join(delivery_mod._material_lines(db, course,
                                                      material_mod.list_materials(db, course.id)))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# ① 锚点口径：只有「音视频 + 有转写正文」才是 stamp
# ---------------------------------------------------------------------------

def test_anchor_kind_stamp_only_for_transcribed_av(client, course_id):
    """三种读数：转写好的音视频 ⇒ stamp；还没转写的音视频 / 文本 / 图片 ⇒ para。"""
    video = _make(course_id, kind="video", title="录像A", text=VIDEO_TEXT)
    audio = _make(course_id, kind="audio", title="录音C", text="[00:00] 只有一句。")
    pending = _make(course_id, kind="video", title="录像B（转写中）", text="")
    failed = _make(course_id, kind="video", title="录像D（无音轨）", text="   \n\n ")
    image = _make(course_id, kind="image", title="板书E", text="图里识别出来的文字")
    text = _make(course_id, kind="text", title="文稿F", text=TEXT_BODY)

    assert _kind(video) == "stamp" and _kind(audio) == "stamp"
    for material_id, want in ((pending, "para"), (failed, "para"), (image, "para"), (text, "para")):
        assert _kind(material_id) == want, material_id


def test_anchor_kind_uses_the_hygiene_body(client, course_id):
    """口径看的是**出库正文**（``material_text``）：只有空白的正文一律按 para。"""
    blank = _make(course_id, kind="video", title="空白转写", text="\n\n   \n")
    assert _kind(blank) == "para"


# ---------------------------------------------------------------------------
# ② 注入文本：stamp 素材不叠 [N]；说明文案按本次注入的素材给
# ---------------------------------------------------------------------------

def test_injected_body_drops_index_for_stamp_material(client, course_id):
    _make(course_id, kind="video", title=VIDEO_TITLE, text=VIDEO_TEXT,
          original="2026-01-08 16-26-13.mkv")

    plan = _plan(course_id, scope="lecture", numbered=True)
    text = plan["text"]

    assert plan["used"][0]["title"] == VIDEO_TITLE
    # 段首就是段首的时间戳：没有 [N] 前缀
    assert "[00:00] 开场：今天讲三个要点。" in text
    assert "[00:25] 第一个要点是安装插件。" in text
    assert "\n[1] [00:00]" not in text and "[1] [00:00]" not in text
    # 说明文案只有时间戳口径（本次只注入了音视频素材）
    assert STAMP_NOTICE in text and "照抄该段段首方括号里的时间戳" in text
    assert PARA_NOTICE not in text


def test_injected_body_keeps_index_for_para_material(client, course_id):
    _make(course_id, kind="text", title=TEXT_TITLE, text=TEXT_BODY)

    text = _plan(course_id, scope="lecture", numbered=True)["text"]

    assert "[1] 第一段。" in text and "[2] 第二段。" in text
    assert PARA_NOTICE + "，N 就是该段前的 [序号]" in text
    assert STAMP_NOTICE not in text          # 没有音视频素材 ⇒ 一个字都不提时间戳


def test_injection_notice_covers_both_anchor_kinds(client, course_id):
    """同一门课两种口径并存（规格边界）：注入说明两句都写。"""
    _make(course_id, kind="video", title=VIDEO_TITLE, text=VIDEO_TEXT)
    _make(course_id, kind="text", title=TEXT_TITLE, text=TEXT_BODY)

    text = _plan(course_id, scope="lecture", numbered=True)["text"]

    assert PARA_NOTICE in text and STAMP_NOTICE in text
    # 音频 / 视频段不带 [N]，文本素材段照旧带
    assert "[00:00] 开场：今天讲三个要点。" in text and "\n[1] 第一段。" in text


# ---------------------------------------------------------------------------
# ③ 分段粒度：350 → 120（按语音停顿合并的边界）
# ---------------------------------------------------------------------------

def test_merge_boundary_under_120_chars():
    """边界的真值：60 + 1 + 59 = 120 恰好合并；再长一段（121 字）自己成段。"""
    assert material_mod.ASR_SEGMENT_MAX_CHARS == 120

    rows = [{"start": 0.0, "end": 1.0, "text": "甲" * 60},
            {"start": 1.0, "end": 2.0, "text": "乙" * 59},
            {"start": 2.0, "end": 3.0, "text": "丙" * 121}]

    merged = material_mod.merge_asr_segments(rows)
    assert [len(seg["text"]) for seg in merged] == [120, 121]
    assert merged[0]["text"] == "甲" * 60 + " " + "乙" * 59
    assert (merged[0]["start"], merged[0]["end"]) == (0.0, 2.0)

    # 段首仍是 [mm:ss]、段间仍是空行（口径一个字没改）
    text = material_mod.asr_segments_to_text(merged)
    assert text.startswith("[00:00] ") and "\n\n[00:02] " in text
    assert len(material_mod.split_paragraphs(text)) == 2

    # 同样是这三段，350 口径下会并成 1 段 —— 收紧到 120 才拿到 ≈25–30 秒的锚点精度
    assert len(material_mod.merge_asr_segments(rows, max_chars=350)) == 1


def test_stamp_helpers_accept_three_digit_minutes():
    """时间戳不是固定 5 位：``_asr_stamp`` 超 1 小时是三位分钟，模型写 h:mm:ss 也按秒对上。"""
    assert material_mod.paragraph_stamp("[105:03] 一小时后的内容") == "105:03"
    assert material_mod.paragraph_stamp("[00:25] 第二个要点") == "00:25"
    assert material_mod.paragraph_stamp("没有时间戳的段") == ""
    assert material_mod.stamp_seconds("06:47") == 407
    assert material_mod.stamp_seconds("105:03") == 105 * 60 + 3
    assert material_mod.stamp_seconds("1:02:03") == 3723
    assert material_mod.stamp_seconds("不是时间戳") is None


# ---------------------------------------------------------------------------
# ④ 文案：素材页备注 / 阅读图例 / 交付包素材清单
# ---------------------------------------------------------------------------

def test_material_page_note_and_legend_speak_both_anchors(client, course_id):
    video = _make(course_id, kind="video", title=VIDEO_TITLE, text=VIDEO_TEXT)
    text_material = _make(course_id, kind="text", title=TEXT_TITLE, text=TEXT_BODY)

    video_page = _material_page(course_id, video)
    assert "本素材是音视频 ⇒ 引用写段首的时间戳" in video_page
    assert "本页 `[N]` 供旧讲义里的「第 N 段」回查" in video_page
    # 文本素材那一页照旧（口径没变）
    assert "正文里的（来源: … 第 N 段）就是指本页 `[N]`" in _material_page(course_id, text_material)

    # 阅读图例（笔记页与交付讲义共用同一份）两种口径都写明
    assert "第 N 段" in kb_mod.READ_LEGEND and "[N]" in kb_mod.READ_LEGEND
    assert "音视频素材写" in kb_mod.READ_LEGEND and "06:47" in kb_mod.READ_LEGEND
    assert "[mm:ss]" in kb_mod.READ_LEGEND


def test_delivery_material_list_explains_both_anchors(client, course_id):
    _make(course_id, kind="video", title=VIDEO_TITLE, text=VIDEO_TEXT)
    _make(course_id, kind="text", title=TEXT_TITLE, text=TEXT_BODY)

    lines = _delivery_material_lines(course_id)

    assert "音视频素材写 `（来源:《录像名》06:47）`" in lines
    assert "`（来源:《素材名》第 N 段）` 指向**文本素材**的" in lines
    assert "## 来源标注口径" in lines and "该句依据该**音视频素材**的 06:47 处" in lines
