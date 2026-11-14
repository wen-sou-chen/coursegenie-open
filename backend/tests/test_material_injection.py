"""任务 69：素材注入的完整性 + 权威性区分 + 入库卫生（回归测试）。

三条硬要求（规格 §B1 / §B1.5 / §B2）：
1. **注入完整性**：不再「前 6 份 × 每份 2500 字」——按相关性选材、长素材按段落注入、
   超预算显式提示（未参与清单 + 注入/原始字数），并把本次用量落库；
2. **权威性区分**：注入文本分段标注「教师提供的原始素材（以它为准）」与
   「AI 补充参考（不得作为事实依据）」，AI 补充素材的引用带 ``AI补充——`` 前缀；
3. **入库卫生**：剥离对话外壳与页眉页脚并记「已清理 N 行」（剥不了的只提示不硬改），
   页数写进 ``meta``（老数据首次读取时回填）。

纪律：``DATABASE_URL`` / ``STORAGE_DIR`` / ``OBSIDIAN_VAULT_ROOT`` 由 ``conftest`` 统一指向
临时目录，本文件不碰真实库；**不动 ``QC_*`` 判据**、**不改 ``PROMPT_TEMPLATE_VERSION``**。
反证据见文件末尾：把口径换回 HEAD 的「前 6 份 × 2500 字」⇒ 上面那些不变量必红。
"""
from __future__ import annotations

import io
import re

from app.db.session import SessionLocal
from app.models.course import Course, SourceMaterial
from app.services import kb as kb_mod
from app.services import lecture_qc
from app.services import material as material_mod
from app.services.material import (
    AI_SECTION_TITLE,
    AI_TITLE_PREFIX,
    TEACHER_SECTION_TITLE,
    UNGROUNDED_MARK,
)

# DSH 实测的真素材外壳（2026-09-21 走查第 2 页：AI 课第 6 份素材开头）
SHELL_TEXT = """生成详细版
###### ChatGPT 说：
好的 ✅ 按照你需要的详细版，这里是 **PE_LangChain工具.md**：
第一段：LangChain 是一套把模型与检索工具串起来的框架。
第二段：检索增强要靠向量库与重排。
"""


def _paste(client, course_id, title, text):
    resp = client.post("/api/material/paste",
                       json={"course_id": course_id, "title": title, "text": text})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ai_material(course_id, title, text, **extra_meta):
    """直接建一条 ``source="ai"`` 的补充素材（等价于 ``POST /api/material/generate`` 的产物）。

    ``extra_meta`` 用来补 meta 位（如 ``shell_stripped=True`` 模拟「入库即剥」的新数据）。
    """
    db = SessionLocal()
    try:
        row = SourceMaterial(course_id=course_id, title=title, kind="text", file_rel="",
                             text=text,
                             meta={"ext": ".text", "chars": len(text), "original": "",
                                   "source": "ai", "direction": "概念铺垫", **extra_meta})
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    finally:
        db.close()


def _legacy_row(material_id, *, drop=("pages", "cleaned_lines", "cleaned_rules", "shell_stripped"),
                text=None):
    """把一条素材改成「69 之前入库的老数据」：抹掉指定的 meta 键（可选换正文）。"""
    db = SessionLocal()
    try:
        row = db.get(SourceMaterial, material_id)
        meta = dict(row.meta or {})
        for key in drop:
            meta.pop(key, None)
        row.meta = meta
        if text is not None:
            row.text = text
        db.commit()
    finally:
        db.close()


def _plan(course_id, **kwargs):
    """直接在生产函数上算注入计划（不经过 LLM；走模块属性以便反证据 monkeypatch）。"""
    db = SessionLocal()
    try:
        return material_mod.plan_injection(db, db.get(Course, course_id), **kwargs)
    finally:
        db.close()


def _stored_text(material_id):
    db = SessionLocal()
    try:
        return db.get(SourceMaterial, material_id)
    finally:
        db.close()

# ---------------------------------------------------------------------------
# §B1.1 / §C1①：第 7 份起不再静默丢弃
# ---------------------------------------------------------------------------

def test_seventh_material_is_injected_not_silently_dropped(client, course_id):
    """8 份素材全都要么进提示词、要么进「未参与」清单 —— 不许静默少 2 份。"""
    for i in range(1, 9):
        _paste(client, course_id, f"素材{i}", f"第 {i} 份素材的正文，含关键词 关键词{i}。")

    plan = _plan(course_id, scope="lecture")

    assert plan["total_materials"] == 8
    assert plan["used_count"] == 8, plan["notice"]      # HEAD 的「前 6 份」会把它压到 6
    assert "素材 8" in plan["text"] and "关键词8" in plan["text"]
    assert plan["unused"] == []
    assert "8/8" in plan["notice"]


def test_material_relevance_picks_by_chapter_not_by_order(client, course_id):
    """选材按**相关性**：后加的、跟章节真正相关的素材必须进来（不再只看先后顺序）。"""
    for i in range(1, 9):
        _paste(client, course_id, f"无关素材{i}", f"这一段讲的是别的学科的第 {i} 个话题。")
    _paste(client, course_id, "幻觉治理专题", "幻觉治理的三个方法：反思校对、引用锚定、手动 RAG。")

    plan = _plan(course_id, scope="lecture", budget=1_200,
                 targets=["幻觉治理", "反思校对"])

    assert plan["used"], plan["notice"]
    assert "幻觉治理专题" in plan["text"], plan["notice"]
    # 相关性最高的那份排在前面（份额按相关性分配，不是按创建顺序）
    assert plan["used"][0]["title"] == "幻觉治理专题"


# ---------------------------------------------------------------------------
# §B1.2 / §C1②：长素材按段落注入，不再硬切前 2500 字
# ---------------------------------------------------------------------------

def test_long_material_is_injected_by_paragraph_not_hard_cut(client, course_id):
    """>2500 字的素材：注入长度超过旧的 2500 硬上限，且**末尾段落也看得见**。"""
    paras = [f"第 {i} 段：行业报告里的数据与案例说明（编号 {i:04d}）。" for i in range(1, 401)]
    long_text = "\n\n".join(paras)
    assert len(long_text) > 8_000
    _paste(client, course_id, "行业报告", long_text)

    plan = _plan(course_id, scope="lecture")

    assert plan["injected_chars"] > material_mod.MAX_INJECT_CHARS     # 旧上限 2500
    used = plan["used"][0]
    assert used["paragraphs_total"] == used["paragraphs_injected"] == 400   # 预算够 ⇒ 一段不少
    assert all(para in plan["text"] for para in paras)                # 400 段全文可见
    assert "第 1 段" in plan["text"] and "第 400 段" in plan["text"]    # 头尾都在
    # 注入字数按「段正文合计」计（不含拼接用空行）⇒ 必然 ≤ 原文，且远大于旧的 2500 上限
    assert 8_000 < used["injected_chars"] <= used["original_chars"] == len(long_text)


def test_injected_paragraph_numbers_follow_the_full_text(client, course_id):
    """按段落取舍后 ``[N]`` 仍是**全篇段落号**（素材页按同一个 N 能定位）。"""
    paras = [f"第{i}段正文内容，这里凑够四十个字符用来触发布局取舍的分段逻辑。" for i in range(1, 201)]
    _paste(client, course_id, "长素材", "\n\n".join(paras))

    # 只让第 150 段与目标相关 ⇒ 取舍后必然跳号（证明用的是全篇序号，而不是重排成 1..N）
    plan = _plan(course_id, scope="lecture", budget=3_000, numbered=True,
                 targets=["第150段正文内容"])
    nums = [int(n) for n in re.findall(r"^\[(\d+)\]", plan["text"], re.M)]

    assert nums, plan["text"][:200]
    assert nums == sorted(nums)                       # 保序
    assert 150 in nums                                # 相关的那段被留下来了
    assert max(nums) > len(nums)                      # 有跳号 ⇒ 用的是全篇序号，不是重排
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        row = material_mod.list_materials(db, course_id)[0]
        page = "\n".join(kb_mod._material_page(course, row))
    finally:
        db.close()
    for n in nums:
        assert f"[{n}] " in page                      # 素材页定位得到同一段

# ---------------------------------------------------------------------------
# §B1.3 / §C2：超预算必须显式提示（未参与清单 + 注入/原始字数）
# ---------------------------------------------------------------------------

def test_over_budget_is_explicit_with_unused_list(client, course_id):
    """预算不够时：注入量守在预算内、未参与的进清单、提示里带 N/M 与字数。"""
    for i in range(1, 13):
        paras = [f"报告{i} 第 {j} 段：" + "数据与案例" * 20 for j in range(1, 21)]
        _paste(client, course_id, f"报告{i}", "\n\n".join(paras))

    plan = _plan(course_id, scope="lecture", budget=6_000)

    assert plan["injected_chars"] <= 6_000
    assert plan["used_count"] < plan["total_materials"] == 12
    assert plan["unused"], plan["notice"]
    assert "未参与" in plan["notice"]
    assert f"{plan['used_count']}/{plan['total_materials']}" in plan["notice"]
    assert "超过预算" in plan["notice"]
    for item in plan["unused"]:
        assert item["title"] and item["original_chars"] > 0 and item["reason"]


def test_no_material_returns_empty_and_says_so(client, course_id):
    """没有素材时：注入为空、提示说明「没有素材」（不报假数字）。"""
    plan = _plan(course_id, scope="outline")
    assert plan["text"] == ""
    assert plan["total_materials"] == 0 and plan["used_count"] == 0
    assert "还没有素材" in plan["notice"]


# ---------------------------------------------------------------------------
# §B2 / §C3：教师素材与 AI 补充的分段标注 + 引用可区分
# ---------------------------------------------------------------------------

def test_teacher_and_ai_sections_are_labeled(client, course_id):
    """注入文本分两段：教师素材在前（以它为准）、AI 补充在后（不得作为事实依据）。"""
    _paste(client, course_id, "教师素材A", "教师给的原始依据：温度窗口是 28 度。")
    _ai_material(course_id, "补充参考B", "模型生成的参考：温度窗口是 35 度。")

    plan = _plan(course_id, scope="lecture", numbered=True)
    text = plan["text"]

    assert TEACHER_SECTION_TITLE in text and AI_SECTION_TITLE in text
    # 用户 2026-09-21 用词：「人工提供的素材」（原规格写「教师提供的原始素材」——
    # 本课素材本身就是 AI 生成的，「教师/原始」会强化「有人工原稿」的误读）
    assert "人工提供的素材(以它为准" in TEACHER_SECTION_TITLE
    assert "不得作为事实依据" in AI_SECTION_TITLE
    assert "教师提供的" not in text
    assert text.index(TEACHER_SECTION_TITLE) < text.index("教师素材A") < text.index(AI_SECTION_TITLE)
    assert text.index(AI_SECTION_TITLE) < text.index("补充参考B")
    assert "不得当作事实依据" in text and "无直接依据" in text
    # AI 补充的引用必须带前缀（提示词里把写法说死，模型才能照做）
    assert f"（来源:{AI_TITLE_PREFIX}" in text and f"（来源:《" in text
    # 两类素材在 used 里也分得开（落库后可查）
    sources = {item["title"]: item["source"] for item in plan["used"]}
    assert sources["教师素材A"] == "" and sources[f"{AI_TITLE_PREFIX}补充参考B"] == "ai"


def test_ai_citation_is_distinguishable_and_resolvable(client, course_id):
    """讲义里写「（来源:AI补充——《X》第 N 段）」：既能解析回素材，又在正文里可区分。"""
    material_id = _ai_material(course_id, "编造素材", "第一段内容。\n\n第二段内容。")
    db = SessionLocal()
    try:
        catalog = material_mod.material_catalog(db, db.get(Course, course_id))
    finally:
        db.close()
    entry = [row for row in catalog if row["id"] == material_id][0]
    assert entry["title"] == f"{AI_TITLE_PREFIX}编造素材"

    cites = lecture_qc.parse_sources(f"- 结论如此（来源:{AI_TITLE_PREFIX}编造素材 第 2 段）。",
                                     "1.1", catalog)
    assert cites, "AI 补充的写法必须能解析（否则等于没标来源）"
    assert cites[0]["material_id"] == material_id
    assert cites[0]["material_name"] == f"{AI_TITLE_PREFIX}编造素材"
    assert cites[0]["material_quote"] == "第二段内容。"
    # 教师素材不加前缀（老口径不变）
    _paste(client, course_id, "教师素材C", "第一段。\n\n第二段。")
    db = SessionLocal()
    try:
        catalog2 = material_mod.material_catalog(db, db.get(Course, course_id))
    finally:
        db.close()
    plain = [row for row in catalog2 if row["title"] == "教师素材C"]
    assert plain, [row["title"] for row in catalog2]


# ---------------------------------------------------------------------------
# §B1.5 / §C：入库卫生（对话外壳 / 页眉 → 「已清理 N 行」）
# ---------------------------------------------------------------------------

def test_conversation_shell_is_stripped_at_ingest(client, course_id):
    """实测形态的对话外壳：入库即剥离，行数记进 meta，素材页与接口都能看到。"""
    row = _paste(client, course_id, "带壳素材", SHELL_TEXT)

    assert row["cleaned_lines"] == 3
    stored = _stored_text(row["id"])
    assert stored is not None
    assert "生成详细版" not in stored.text and "ChatGPT 说" not in stored.text
    assert "LangChain 是一套把模型与检索工具串起来的框架" in stored.text
    assert stored.meta["cleaned_lines"] == 3 and stored.meta["cleaned_rules"]
    assert stored.meta["raw_chars"] > stored.meta["chars"]      # 原始字数与剥离后字数分开记

    page = "\n".join(kb_mod._material_page(Course(title="测试课"), stored))
    assert "已清理 3 行" in page


def test_ppt_header_is_stripped(client, course_id):
    """PPT 页眉署名与日期（实测「汇报人:Kimi AI / 时间:2025.01.01」）。"""
    row = _paste(client, course_id, "PPT 摘录",
                 "破解AI幻觉：从成因到对策\n汇报人:Kimi AI\n时间:2025.01.01\n第一段正文内容。")
    assert row["cleaned_lines"] == 2
    stored = _stored_text(row["id"])
    assert "汇报人" not in stored.text and "2025.01.01" not in stored.text
    assert "破解AI幻觉：从成因到对策" in stored.text and "第一段正文内容" in stored.text


def test_unsafe_shell_strip_keeps_original(client, course_id):
    """剥完会清空正文 ⇒ **放弃剥离、只提示**（绝不硬改内容）。"""
    row = _paste(client, course_id, "只有外壳", "生成详细版\n好的 ✅ 按照你需要的详细版")
    assert row["cleaned_lines"] == 0
    assert any("保留原样" in rule for rule in row["cleaned_rules"])
    assert "生成详细版" in (_stored_text(row["id"]).text or "")

# ---------------------------------------------------------------------------
# §B1.5.2：页数落库（老数据首次读取回填）
# ---------------------------------------------------------------------------

def _make_pptx(text: str) -> bytes:
    from pptx import Presentation

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "光合作用"
    slide.placeholders[1].text = text
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def test_pages_is_persisted_at_import_and_get_does_not_write(client, course_id):
    """入库即写 ``meta.pages``；**GET 不回填**（任务 72 §B2：GET 不许改状态）。"""
    resp = client.post(
        "/api/material/upload",
        data={"course_id": course_id, "title": "课件"},
        files={"file": ("光合作用.pptx", _make_pptx("光合作用合成有机物"),
                        "application/vnd.openxmlformats-officedocument.presentationml.presentation")},
    )
    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["pages"] == 1
    assert _stored_text(row["id"]).meta["pages"] == 1

    # 造一条「老数据」：抹掉 meta 里的 pages / cleaned_lines（模拟 68 时代的入库结果）
    _legacy_row(row["id"], drop=("pages", "cleaned_lines", "cleaned_rules"))

    listed = client.get(f"/api/material/{course_id}").json()
    hit = [m for m in listed if m["id"] == row["id"]][0]
    assert hit["pages"] == 1                                  # 读取时现算出来
    assert hit["cleaned_lines"] == 0
    # ⚠️ 任务 72 §B2：读一次**不许**把现算值写回 meta —— 修前这里断言的是「回填进了 meta」，
    # 那正是「打开素材页就改真库」的来源（实测真库 mtime 2026-09-21 13:29:04 被写变）。
    stored = _stored_text(row["id"])
    assert stored.meta.get("pages") is None, "GET 把 pages 写回库了（GET 必须只读）"
    assert stored.meta.get("cleaned_lines") is None, "GET 把 cleaned_lines 写回库了"


def test_explicit_backfill_entry_writes_meta_and_logs(client, course_id, caplog):
    """老数据回填只走**显式入口**：写 meta、写 INFO 日志、正文一个字不动（任务 72 §B2）。"""
    row = _upload(client, course_id, "老课件.md", UPLOAD_HEADER_TEXT)
    _legacy_row(row["id"], drop=("pages", "origin", "detected_lines", "detected_rules"))

    body_text = _stored_text(row["id"]).text
    with caplog.at_level("INFO"):
        resp = client.post(f"/api/material/{course_id}/backfill")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["ok"] is True and payload["backfilled"] == 1 and payload["total"] == 1

    stored = _stored_text(row["id"])
    assert stored.meta["origin"] == "upload" and stored.meta["detected_lines"] == 2
    assert stored.text == body_text                       # 只补 meta，正文没动
    messages = [rec.getMessage() for rec in caplog.records]
    assert any("补齐素材信息" in m for m in messages), messages

    # 幂等：再点一次没有可补的（界面提示「已是最新」），也不会重复写库
    again = client.post(f"/api/material/{course_id}/backfill").json()
    assert again["backfilled"] == 0


def test_fresh_materials_need_no_backfill(client, course_id, monkeypatch):
    """新数据（**三条真实入库路径**）都已经算好 pages / origin / 外壳行数 ⇒ 补齐入口无活可干。

    任务 72 §B2 的「优先方案」就是这条：回填之所以能挪出 GET，是因为入库时已经写全了 ——
    三种来源（上传 / 粘贴 / AI 补充）各建一条，跑一遍真实接口再点补齐，必须 0 份。
    """
    _upload(client, course_id, "新课件.md", UPLOAD_HEADER_TEXT)
    _paste(client, course_id, "新粘贴件", SHELL_TEXT)

    def fake_complete_json(llm, system, user, *, max_tokens=0, purpose=""):   # noqa: ARG001
        return {"materials": [{"direction": "概念铺垫", "title": "新补充",
                               "text": "模型写的参考内容。"}]}

    monkeypatch.setattr(material_mod.usage_log, "complete_json", fake_complete_json)
    assert client.post("/api/material/generate",
                       json={"course_id": course_id, "directions": ["concept"]}).status_code == 200

    assert client.post(f"/api/material/{course_id}/backfill").json()["backfilled"] == 0


def test_list_materials_exposes_shell_stripped_for_three_states(client, course_id):
    """三态文案的判据 ``shell_stripped`` 必须透出（任务 72 §B1）。"""
    # ① 上传原件：检测到但库里保持原样
    upload = _upload(client, course_id, "原件.md", UPLOAD_HEADER_TEXT)
    # ② 老数据粘贴件：库里正文还带着外壳，只有出库时才剥
    legacy = _paste(client, course_id, "老粘贴件", "普通正文。")
    _legacy_row(legacy["id"], drop=("shell_stripped", "cleaned_lines", "cleaned_rules"),
                text=SHELL_TEXT)
    # ③ 入库即剥（新粘贴件）
    fresh = _paste(client, course_id, "新粘贴件", SHELL_TEXT)

    listed = {m["id"]: m for m in client.get(f"/api/material/{course_id}").json()}
    assert listed[upload["id"]]["shell_stripped"] is False          # ① 保持原样
    assert listed[legacy["id"]]["shell_stripped"] is False          # ② 出库才剥（库里没动）
    assert listed[legacy["id"]]["cleaned_lines"] == 3               #    但这 3 行确实会被剥
    assert listed[fresh["id"]]["shell_stripped"] is True            # ③ 库里真改过
    assert listed[fresh["id"]]["must_clean"] is True
    # ② 的原文本仍然带着外壳（说「已清理」就是说反话）
    assert "生成详细版" in (_stored_text(legacy["id"]).text or "")


# ---------------------------------------------------------------------------
# §B1.4 / §C2：本次注入情况落库 + 接口返回
# ---------------------------------------------------------------------------

def test_outline_generation_records_injection_and_exposes_notice(client, course_id):
    """大纲照旧生成成功；接口返回本次注入情况；course.meta 里能查到用量明细。"""
    for i in range(1, 9):
        _paste(client, course_id, f"素材{i}", f"方法{i}：先做 A，再做 B。")

    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "理解 AI 幻觉的成因与对策",
        "num_chapters": 3, "style": "正式", "audience": "职场人",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["nodes"], "大纲仍要正常生成"

    check = body["check"]
    assert check["material_inject_notice"], check
    assert "8/8" in check["material_inject_notice"]
    assert check["material_inject"]["used_count"] == 8

    db = SessionLocal()
    try:
        stored = material_mod.stored_injection(db.get(Course, course_id))
    finally:
        db.close()
    assert stored and stored["used_count"] == 8
    assert stored["injected_chars"] > 0 and stored["original_chars"] > 0
    assert all(item["injected_chars"] > 0 for item in stored["used"])
    assert stored["notice"]


def test_prompt_version_and_qc_judgements_untouched():
    """规格 §B2-4：**不擅自**改 ``PROMPT_TEMPLATE_VERSION``，也不动 QC 判据。"""
    from app.services import lecture as lecture_mod

    assert lecture_mod.PROMPT_TEMPLATE_VERSION == "lecture-v14"
    assert len(lecture_qc.CHECK_KEYS) == 11
    assert lecture_qc.CHECK_KEYS[0] == "structure" and lecture_qc.CHECK_KEYS[-1] == "actionable"


# ---------------------------------------------------------------------------
def test_legacy_row_is_stripped_on_the_way_out(client, course_id):
    """老数据（入库时还没这道工序）只记行数、正文一个字没动 ⇒ **注入时就地剥**。

    真库那 6 份素材全是这种：如果只看「meta 里有 cleaned_lines」就当已剥，会出现
    「素材行说已清理 3 行、注入给模型的文本里壳还在」——两条口径互相打脸。
    """
    row = _paste(client, course_id, "老数据素材", "正文第一段内容。\n第二段内容。")
    # 模拟 69 之前入库的行：没有 shell_stripped 这一位，正文里带着对话外壳（库里保持原样）
    _legacy_row(row["id"], drop=("shell_stripped",), text=SHELL_TEXT)

    listed = client.get(f"/api/material/{course_id}").json()
    hit = [m for m in listed if m["id"] == row["id"]][0]
    assert hit["cleaned_lines"] == 3          # 读取时现算（**不写库**，任务 72 §B2）
    assert hit["shell_stripped"] is False     # 库里没改过 ⇒ 界面说「出库时会剥离 3 行」

    plan = _plan(course_id, scope="outline")
    assert "生成详细版" not in plan["text"] and "ChatGPT 说" not in plan["text"]
    assert "LangChain 是一套把模型与检索工具串起来的框架" in plan["text"]
    assert "生成详细版" in (_stored_text(row["id"]).text or "")   # 库里的原文没被硬改


def test_legacy_paste_can_be_cleaned_through_the_button(client, course_id):
    """老数据（未落库的粘贴件 / AI 补充）点「一键清理」才把剥离结果落库（任务 72 §B1）。

    修前 ``clean_material`` 对**所有** ``must_clean`` 素材一律 400 ⇒ 界面在「库内保留原文」
    那一态根本落不了库，只能一直说「出库时会剥离」。已落库的一律仍拒绝（防抹掉记录）。
    """
    row = _paste(client, course_id, "老粘贴件", "正文第一段内容。\n第二段内容。")
    _legacy_row(row["id"], drop=("shell_stripped", "cleaned_lines", "cleaned_rules"),
                text=SHELL_TEXT)

    resp = client.post(f"/api/material/{row['id']}/clean")
    assert resp.status_code == 200, resp.text
    assert resp.json()["cleaned_lines"] == 3
    stored = _stored_text(row["id"])
    assert stored.meta["shell_stripped"] is True            # 落库了
    assert "生成详细版" not in (stored.text or "") and "LangChain 是一套" in (stored.text or "")
    listed = [m for m in client.get(f"/api/material/{course_id}").json() if m["id"] == row["id"]][0]
    assert listed["shell_stripped"] is True
    # 出库口径不变：注入里本来就没有外壳
    assert "生成详细版" not in _plan(course_id, scope="outline")["text"]
    # 已落库的不许二次清理（会抹掉「已清理 3 行」的记录）
    assert client.post(f"/api/material/{row['id']}/clean").status_code == 400
    assert _stored_text(row["id"]).meta["cleaned_lines"] == 3


def test_notice_does_not_cry_wolf_when_nothing_was_cut(client, course_id):
    """预算足足的 ⇒ 提示必须说「全部素材都已注入」，不许假报「超过预算、已按段落取舍」。"""
    for i in range(1, 7):
        _paste(client, course_id, f"短素材{i}", f"第 {i} 份素材：三行以内的正文。")

    plan = _plan(course_id, scope="lecture")

    assert plan["unused"] == [] and plan["used_count"] == 6
    assert all(item["paragraphs_injected"] == item["paragraphs_total"] for item in plan["used"])
    assert "全部素材都已注入" in plan["notice"], plan["notice"]
    assert "超过预算" not in plan["notice"], plan["notice"]



# ---------------------------------------------------------------------------
# §B1.5.1 卫生分档（用户 2026-09-21 口径）：上传原件保持原样 + 可选一键清理
# ---------------------------------------------------------------------------

def _upload(client, course_id, filename: str, text: str):
    resp = client.post(
        "/api/material/upload",
        data={"course_id": course_id, "title": filename.split(".")[0]},
        files={"file": (filename, text.encode("utf-8"), "text/markdown")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# 真素材的 PPT 页眉形态（实测「汇报人:Kimi AI / 时间:2025.01.01」）
UPLOAD_HEADER_TEXT = ("汇报人:Kimi AI\n时间:2025.01.01\n"
                      "第一段正文：温度窗口是 28 度，湿度保持在 60% 左右最稳妥。\n")


def test_upload_is_kept_as_is_and_only_flagged(client, course_id):
    """**上传原件**：正文一个字不动（用户口径：可以接受），只报「检测到 N 行」+ 一键清理。"""
    row = _upload(client, course_id, "课件摘录.md", UPLOAD_HEADER_TEXT)

    assert row["origin"] == "upload" and row["origin_label"] == "人工上传"
    assert row["must_clean"] is False
    assert row["cleaned_lines"] == 0 and row["detected_lines"] == 2
    stored = _stored_text(row["id"])
    assert "汇报人" in stored.text and "2025.01.01" in stored.text          # 库里保持原样
    plan = _plan(course_id, scope="outline")
    assert "汇报人" in plan["text"]                                        # 注入的也是原样
    assert "第一段正文：温度窗口是 28 度" in plan["text"]


def test_optional_clean_writes_back_only_when_called(client, course_id):
    """一键清理：**点了才改**正文（并写 meta）；AI 补充 / 粘贴件不需要也不会被二次清理。"""
    row = _upload(client, course_id, "课件摘录2.md", UPLOAD_HEADER_TEXT)
    assert "汇报人" in (_stored_text(row["id"]).text or "")                 # 不点 ⇒ 一直原样

    resp = client.post(f"/api/material/{row['id']}/clean")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cleaned_lines"] == 2
    assert body["material"]["cleaned_lines"] == 2 and body["material"]["detected_lines"] == 2
    stored = _stored_text(row["id"])
    assert "汇报人" not in stored.text and "2025.01.01" not in stored.text
    assert "第一段正文：温度窗口是 28 度" in stored.text
    assert stored.meta["shell_stripped"] is True
    page = "\n".join(kb_mod._material_page(Course(title="测试课"), stored))
    assert "已清理 2 行" in page
    assert "检测到" not in page        # 清过之后不能还写着「保持原样」（两句会互相打脸）

    # 入库即剥的（``shell_stripped=True``，新数据走的就是这条）一律不许二次清理
    # 入库即剥的（``shell_stripped=True``，新数据走的就是这条）一律不许二次清理
    ai_id = _ai_material(course_id, "补充X", "模型写的参考。",
                         shell_stripped=True, cleaned_lines=0)
    assert client.post(f"/api/material/{ai_id}/clean").status_code == 400
    # 已清理过的上传件也不许二次清理：再点一次会把「已清理 2 行」的记录抹成 0
    assert client.post(f"/api/material/{row['id']}/clean").status_code == 400
    assert _stored_text(row["id"]).meta["cleaned_lines"] == 2      # 记录还在


def test_paste_and_ai_are_cleaned_automatically(client, course_id):
    """**粘贴件 / AI 补充必须干净**（用户口径）：入库即剥，接口明说 must_clean。"""
    row = _paste(client, course_id, "粘贴件", SHELL_TEXT)
    assert row["origin"] == "paste" and row["origin_label"] == "粘贴文本(疑似 AI 产物)"
    assert row["must_clean"] is True and row["cleaned_lines"] == 3
    assert row["detected_lines"] == 0

    ai_id = _ai_material(course_id, "AI 补充件",
                         "第一段内容：模型给出的参考说明。\n生成详细版\n第二段内容：还要人工确认。")
    hit = [m for m in client.get(f"/api/material/{course_id}").json() if m["id"] == ai_id][0]
    assert hit["origin"] == "ai" and hit["must_clean"] is True
    assert hit["cleaned_lines"] == 1        # 老 AI 素材（meta 里没记 origin）也按「必须干净」处理

    plan = _plan(course_id, scope="lecture", numbered=True)
    assert "[1] 第一段内容：模型给出的参考说明。" in plan["text"]
    assert "生成详细版" not in plan["text"]


def test_material_list_exposes_origin_and_grounding(client, course_id):
    """素材列表接口把「来源类型 / 检测行数 / 有依据-无依据」都透出（界面据此提示）。"""
    upload = _upload(client, course_id, "原件.md", UPLOAD_HEADER_TEXT)
    paste = _paste(client, course_id, "粘贴件", SHELL_TEXT)
    ai = _ai_material(course_id, "补充", GROUNDED_BODY)

    listed = {m["id"]: m for m in client.get(f"/api/material/{course_id}").json()}
    assert listed[upload["id"]]["origin"] == "upload"
    assert listed[upload["id"]]["detected_lines"] == 2 and listed[upload["id"]]["cleaned_lines"] == 0
    assert listed[paste["id"]]["must_clean"] is True and listed[paste["id"]]["cleaned_lines"] == 3
    assert listed[ai]["grounded_count"] == 2 and listed[ai]["ungrounded_count"] == 1
    assert listed[ai]["grounded"][0]["para"] == 1 and listed[ai]["origin_label"] == "AI 补充"


# ---------------------------------------------------------------------------
# §B1.5.3 「有来处」（用户口径：AI 补充不许随便乱讲）
# ---------------------------------------------------------------------------

# 一条带两种来处标注的 AI 补充正文（模拟模型按提示词格式写出来的东西）
GROUNDED_BODY = (
    "## 关键结论\n"
    "温度窗口是 28 度（来源:《原始素材》第 1 段）。\n"
    "这一步要凭经验判断(无依据·模型补充)。\n"
    "检索增强要先重排（来源:AI补充——旧补充 第 2 段）。\n"
)


def test_parse_grounding_counts_and_claims():
    """解析「有来处 / 无来处」：条数 + 每条前面的那句事实性表述。"""
    parsed = material_mod.parse_grounding(GROUNDED_BODY)

    assert parsed["grounded_count"] == 2 and parsed["ungrounded_count"] == 1
    first, second = parsed["grounded"]
    assert first == {"claim": "温度窗口是 28 度", "material": "原始素材", "para": 1, "ai": False}
    assert second["material"] == "旧补充" and second["ai"] is True and second["para"] == 2
    assert parsed["ungrounded"] == ["这一步要凭经验判断"]


def test_grounding_regex_is_the_same_as_the_lecture_side():
    """「有来处」的解析口径必须与讲义侧**同一口径**（否则讲义里的引用回查不到素材）。"""
    from app.services.lecture_qc import parse_sources

    catalog = [{"index": 1, "id": "m1", "title": f"{AI_TITLE_PREFIX}旧补充",
                "paragraphs": ["甲", "乙"], "source": "ai"}]
    line = "检索增强要先重排（来源:AI补充——旧补充 第 2 段）。"

    cites = parse_sources(line, "1.1", catalog)
    parsed = material_mod.parse_grounding(line)

    assert cites and cites[0]["paragraph"] == 2 == parsed["grounded"][0]["para"]
    assert cites[0]["material_id"] == "m1"
    assert parsed["grounded"][0]["material"] == "旧补充" and parsed["grounded"][0]["ai"] is True


def test_ai_generation_forces_grounding_and_persists_it(client, course_id, monkeypatch):
    """AI 补充生成：提示词写死「要接地」，产出解析出「有依据 2 / 无依据 1」并落库。"""
    _paste(client, course_id, "原始素材", "温度窗口是 28 度。\n\n第二段无关内容。")
    seen: dict = {}

    def fake_complete_json(llm, system, user, *, max_tokens=0, purpose=""):
        seen["system"] = system
        seen["user"] = user
        return {"materials": [{"direction": "概念铺垫", "title": "接地补充",
                               "text": GROUNDED_BODY}]}

    monkeypatch.setattr(material_mod.usage_log, "complete_json", fake_complete_json)
    resp = client.post("/api/material/generate",
                       json={"course_id": course_id, "directions": ["concept"]})
    assert resp.status_code == 200, resp.text
    row = resp.json()[0]

    # ① 强制接地的提示词原文（用户口径：AI 补充的资料一定要干净、有来处）
    assert "只能基于" in seen["system"] and UNGROUNDED_MARK in seen["system"]
    assert "（来源:《素材标题》第 N 段）" in seen["system"]
    assert "AI补充——《素材标题》第 N 段" in seen["system"]
    # ② 注入给模型的是**带 [N] 段落号**的素材（没有 [N]，「第 N 段」根本写不出来）
    assert "[1] 温度窗口是 28 度。" in seen["user"]
    # ③ 接口 + 落库：有依据 2 条 / 无依据 1 条
    assert row["grounded_count"] == 2 and row["ungrounded_count"] == 1
    assert row["origin"] == "ai" and row["must_clean"] is True
    stored = _stored_text(row["id"])
    assert stored.meta["grounded_count"] == 2 and stored.meta["ungrounded_count"] == 1
    assert stored.meta["grounded"]["grounded"][0]["material"] == "原始素材"
    assert stored.meta["grounded"]["ungrounded"][0].startswith("这一步要凭经验判断")
    # ④ 无来处的段落也进了注入文本 ⇒ 讲义侧才能按它写「（无直接依据,建议人工确认）」
    assert UNGROUNDED_MARK in _plan(course_id, scope="lecture", numbered=True)["text"]


def test_numbered_full_text_matches_injection_numbers(client, course_id):
    """「跳到原素材第 N 段核对」：numbered 模式给的段号与注入里的 [N] 逐段一致。"""
    row = _paste(client, course_id, "分段素材", "第一段甲。\n\n第二段乙。\n\n第三段丙。")

    api = client.get(f"/api/material/full/{row['id']}?numbered=true&limit=2").json()
    assert api["numbered"] is True and api["total_paragraphs"] == 3
    assert [p["n"] for p in api["paragraphs"]] == [1, 2]
    assert api["paragraphs"][1]["text"] == "第二段乙。"
    assert api["has_more"] is True and api["next_offset"] == 2
    assert "[2] 第二段乙。" in _plan(course_id, scope="lecture", numbered=True)["text"]


def test_lecture_prompt_covers_ungrounded_paragraphs():
    """讲义侧提示词：引用带 (无依据·模型补充) 的段落**必须**写「（无直接依据,建议人工确认）」。"""
    from app.services import lecture as lecture_mod

    assert UNGROUNDED_MARK in lecture_mod.LECTURE_SYSTEM
    assert "一律写「（无直接依据,建议人工确认）」" in lecture_mod.LECTURE_SYSTEM
    assert "人工提供的素材" in lecture_mod.LECTURE_SYSTEM
    assert "教师素材" not in lecture_mod.LECTURE_SYSTEM


# ---------------------------------------------------------------------------
# §B1.5.3 交付包口径：素材清单标注来源类型与有依据比例；交付说明写清「来源」含义
# ---------------------------------------------------------------------------

def test_delivery_material_list_marks_origin_and_grounding(client, course_id):
    """`素材清单.md`：来源类型（人工上传 / 粘贴文本(疑似 AI 产物) / AI 补充）+ 有依据比例。"""
    from app.services import delivery as delivery_mod

    _paste(client, course_id, "人工粘贴", "第一段。")
    _upload(client, course_id, "原件.md", UPLOAD_HEADER_TEXT)
    _ai_material(course_id, "补充", GROUNDED_BODY)

    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        lines = delivery_mod._material_lines(db, course,
                                             material_mod.list_materials(db, course_id))
    finally:
        db.close()
    text = "\n".join(lines)

    assert "来源类型" in text and "AI 补充依据" in text
    assert "人工上传" in text and "粘贴文本(疑似 AI 产物)" in text and "AI 补充" in text
    assert "有依据 2 / 无依据 1" in text
    assert "「来源」= 导入本课的素材" in text
    assert "（来源:AI补充——《素材名》第 N 段）" in text


def test_delivery_readme_states_source_meaning(client, course_id):
    """`00-交付说明.md`：补一句「来源 = 导入本课的素材；素材本身是 AI 生成的会如实标注」。"""
    from app.services import delivery as delivery_mod

    counts = {"chapters": 1, "points": 1, "lectures": 1, "quizzes": 0, "decks": 0,
              "figures": 0, "notes_files": 0, "lesson_plans": 0, "student_sheets": 0}
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        lines = delivery_mod._readme_lines(db, course, {"counts": counts, "notes_sync": {}},
                                           {}, [], [], "build-test")
    finally:
        db.close()
    text = "\n".join(lines)

    assert "「来源」的含义" in text
    assert "若素材本身是 AI 生成的" in text and "不等同于人工原始资料" in text


# ---------------------------------------------------------------------------
# §C6 反证据：换回 HEAD 的「前 6 份 × 每份 2500 字」⇒ 上面的不变量必红
# ---------------------------------------------------------------------------

def _legacy_plan(db, course, *, scope: str = "outline", targets=None,
                 numbered: bool = False, budget=None) -> dict:
    """HEAD（任务 69 之前）的注入口径：只取前 6 份、每份硬切 2500 字、无任何提示。"""
    rows = material_mod.list_materials(db, course.id)[:material_mod.MAX_INJECT_FILES]
    parts = ["\n\n=== 课程素材（教师提供的原始资料，请以其为准创作）==="]
    for i, row in enumerate(rows, 1):
        body = (row.text or "").strip()[:material_mod.MAX_INJECT_CHARS]
        if not body:
            continue
        if numbered:
            body = "\n".join(material_mod.numbered_paragraphs(row.text))
        parts.append(f"\n--- 素材 {i}：《{row.title}》---\n{body}")
    return {"text": "\n".join(parts), "used": [], "unused": [], "notice": "",
            "injected_chars": 0, "original_chars": 0, "used_count": len(rows),
            "total_materials": len(rows), "budget": 0, "scope": scope, "targets": []}


def test_legacy_six_files_2500_would_fail_new_invariants(client, course_id, monkeypatch):
    """反证据：把口径换回 HEAD ⇒ 上面那些断言逐条变红（说明新断言有判别力）。"""
    for i in range(1, 9):
        _paste(client, course_id, f"素材{i}", f"第 {i} 份素材的正文。")
    long_paras = [f"第 {i} 段：行业报告数据。" for i in range(1, 401)]
    _paste(client, course_id, "行业报告", "\n\n".join(long_paras))
    _ai_material(course_id, "补充参考", "模型编的参考内容。")

    monkeypatch.setattr(material_mod, "plan_injection", _legacy_plan)
    plan = _plan(course_id, scope="lecture")

    assert "素材 8" not in plan["text"]              # ① 第 7 份起被静默丢弃
    assert "第 400 段" not in plan["text"]           # ② 长素材硬切在 2500 字
    assert AI_SECTION_TITLE not in plan["text"]      # ③ 两类素材不再分段标注
    assert plan["notice"] == ""                      # ④ 超限没有任何提示
    assert plan["used_count"] == 6 and plan["unused"] == []            # ⑤ 只进 6 份且未参与清单为空
    db = SessionLocal()
    try:
        everything = material_mod.list_materials(db, course_id)
    finally:
        db.close()
    assert len(everything) == 10                 # 库里明明有 10 份……
    assert plan["used_count"] == 6               # ……提示词里只看得见 6 份，另外 4 份静默消失


def test_reverse_evidence_hygiene_tiers_and_grounding(client, course_id, monkeypatch):
    """反证据（2026-09-21 追加的断言）：卫生分档与「有来处」还原成老口径 ⇒ 新断言必红。"""
    upload = _upload(client, course_id, "原件.md", UPLOAD_HEADER_TEXT)

    # ① 老口径「所有素材都剥」⇒ 上传原件被顺手剥掉：新断言「注入里仍有页眉」变红
    monkeypatch.setattr(material_mod, "material_text",
                        lambda row: material_mod.strip_shell(row.text or "")[0])
    listed = {m["id"]: m for m in client.get(f"/api/material/{course_id}").json()}
    assert listed[upload["id"]]["detected_lines"] == 2      # 提示还在（检测与清理是两件事）
    assert listed[upload["id"]]["cleaned_lines"] == 0       # 「已清理」仍是 0：没清过
    assert "汇报人" not in _plan(course_id, scope="outline")["text"]     # ← 新断言在这里变红
    monkeypatch.undo()

    # ② 老口径「不解析有来处」⇒ 卡片数字全 0，新断言「有依据 2 / 无依据 1」变红
    monkeypatch.setattr(material_mod, "parse_grounding",
                        lambda text: {"grounded": [], "ungrounded": [],
                                      "grounded_count": 0, "ungrounded_count": 0})
    ai_id = _ai_material(course_id, "补充", GROUNDED_BODY)
    hit = [m for m in client.get(f"/api/material/{course_id}").json() if m["id"] == ai_id][0]
    assert hit["grounded_count"] == 0 and hit["ungrounded_count"] == 0   # ← 新断言在这里变红
    assert hit["ungrounded"] == []
