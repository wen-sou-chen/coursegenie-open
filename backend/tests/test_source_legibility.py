"""任务 57：讲义可核对性 —— 来源能解读（新旧写法兼容）+ 素材能定位（段落号）+ 阅读图例。

三条硬要求：
1. ``_CITE_RE`` **同时认**新写法 ``（来源:《素材》第 33 段）`` 与历史写法 ``（来源: 素材 #33）``
   —— 否则换措辞后历史 27 篇会被判成「没标来源」，等于把 sources 判定推翻；
2. 素材页每段前的 ``[N]`` 与提示词 ``material_prompt_section(numbered=True)`` **逐段一致**
   （共用 ``numbered_paragraphs``），读者才能按 ``[N]`` 定位到「第 N 段」；
3. 笔记页与交付讲义顶部都有同一份「怎么读」图例。

不自造第二套渲染/编号/正则：所有断言都打在生产函数上。
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

from app.db.session import SessionLocal
from app.models.course import Course, SourceMaterial
from app.services import kb as kb_mod
from app.services import lecture_qc as qc_mod
from app.services.lecture_qc import _CITE_RE, _NO_BASIS_RE, parse_sources
from app.services.material import material_prompt_section, numbered_paragraphs

MATERIAL_TITLE = "破解AI幻觉"

# 素材目录（parse_sources 的入参口径：title + paragraphs 一起给）
CATALOG = [{"id": "m1", "index": 1, "title": MATERIAL_TITLE,
            "paragraphs": [f"第 {i} 段的素材原文" for i in range(1, 61)]}]


def _cite(line: str) -> dict | None:
    rows = parse_sources(line, "1.1", CATALOG)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# ① 新旧写法都要认（兼容是硬要求）
# ---------------------------------------------------------------------------

def test_cite_re_accepts_new_and_old_writing():
    """新写法（提示词侧只产它）与历史写法都必须解析出同一个段落号。"""
    new_form = "温度越高越容易编（来源:《破解AI幻觉》第 33 段）"
    old_form = "温度越高越容易编（来源: 破解AI幻觉 #33）"

    for line in (new_form, old_form):
        hit = _cite(line)
        assert hit is not None, f"解析不出来：{line}"
        assert hit["paragraph"] == 33, hit
        assert hit["material_name"] == MATERIAL_TITLE, hit
        assert hit["material_id"] == "m1", hit          # 书名号不影响素材解析
        assert hit["material_quote"] == "第 33 段的素材原文", hit

    # 半角/全角括号、冒号、书名号缺失、空白，都是历史与模型现场出现过的形态
    for line, para in (("…（来源：《破解AI幻觉》第 7 段）", 7),
                       ("…(来源: 破解AI幻觉 第 7 段)", 7),
                       ("…（来源:破解AI幻觉第7段）", 7),
                       ("…（来源: 1 #7）", 7)):          # 模型写「素材1」也认
        hit = _cite(line)
        assert hit is not None and hit["paragraph"] == para, (line, hit)


def test_cite_re_still_rejects_miswrites():
    """守护「没放宽」：区间/多段、免责标注混写、纯免责标注都不许被当成来源。"""
    for line in ("…（来源: 破解AI幻觉 #42-45）",       # 区间（旧口径也认不出）
                 "…（来源:《破解AI幻觉》第 42-45 段）",
                 "…（来源: 破解AI幻觉 #60、#75）",
                 "…（来源: 无直接依据,建议人工确认）",  # 免责标注塞进「来源:」外壳
                 "…（来源: 建议人工确认）",
                 "…（无直接依据,建议人工确认）",       # 本来就该走免责标记，不是来源
                 "…（来源: 破解AI幻觉 #）"):
        assert _CITE_RE.search(line) is None, f"不该被当成来源：{line}"
        assert _cite(line) is None, f"不该产生来源条目：{line}"

    # 免责标记走另一套正则，仍被认出（不被 _CITE_RE 兼容改动带偏）
    assert _NO_BASIS_RE.search("…（无直接依据,建议人工确认）")


# ---------------------------------------------------------------------------
# ② 段落号：素材页与提示词必须逐段一致（共用 numbered_paragraphs）
# ---------------------------------------------------------------------------

TWO_PARA = "第一段：光反应发生在类囊体薄膜上。\n\n\n第二段：卡尔文循环在基质中固碳。"


def _make_material(course_id: str, title: str, text: str) -> SourceMaterial:
    db = SessionLocal()
    try:
        row = SourceMaterial(course_id=course_id, title=title, kind="text", text=text,
                             meta={"ext": ".text", "chars": len(text), "original": ""})
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    finally:
        db.close()


def test_numbered_paragraphs_is_the_single_source_of_numbers(client, course_id):
    """编号只有一个来源：素材页正文 == numbered_paragraphs == 提示词里的 [N]。"""
    numbered = numbered_paragraphs(TWO_PARA)
    assert numbered == ["[1] 第一段：光反应发生在类囊体薄膜上。", "[2] 第二段：卡尔文循环在基质中固碳。"]

    # 截断只影响末尾：前缀各段的序号与不截断时一致（提示词口径不变）
    truncated = numbered_paragraphs(TWO_PARA, max_chars=12)
    assert truncated == ["[1] " + TWO_PARA[:12]]
    assert [re.match(r"^\[(\d+)\]", line).group(1) for line in truncated] == \
           [re.match(r"^\[(\d+)\]", line).group(1) for line in numbered[:1]]

    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        _make_material(course_id, "光合作用导论", TWO_PARA)
        db.expire_all()
        course = db.get(Course, course_id)
        prompt = material_prompt_section(db, course, numbered=True)
        plain = material_prompt_section(db, course, numbered=False)
    finally:
        db.close()

    prompt_lines = [line for line in prompt.splitlines() if re.match(r"^\[\d+\] ", line)]
    assert prompt_lines == numbered, (prompt_lines, numbered)
    assert not re.search(r"^\[\d+\] ", plain, re.M), "不加编号时不许多出段落号"

    # 素材页正文用的是同一份编号（不截断）
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        material = db.query(SourceMaterial).filter(SourceMaterial.course_id == course_id).one()
        page = "\n".join(kb_mod._material_page(course, material))
    finally:
        db.close()
    page_lines = [line for line in page.splitlines() if re.match(r"^\[\d+\] ", line)]
    assert page_lines == numbered_paragraphs(TWO_PARA, max_chars=None) == numbered
    assert "段落号 = 按素材解析顺序编号" in page, "素材页要写明段落号口径"
    assert kb_mod.READ_LEGEND in page, "笔记页顶部要有「怎么读」图例"


def test_exported_material_page_numbers_every_paragraph(client, course_id):
    """真导出路径（/api/kb/.../export）里的素材页每段都带 [N]。"""
    root = Path(tempfile.mkdtemp(prefix="coursegenie_task57_"))
    assert client.post(f"/api/kb/course/{course_id}/bind",
                       json={"vault_root": str(root), "output_rel": "课程/光合作用"}).status_code == 200
    resp = client.post("/api/material/paste", json={
        "course_id": course_id, "title": "课堂观察记录",
        "text": "第一段：叶片变蓝，说明产生了淀粉。\n\n第二段：遮光处理的对照片段没有变蓝。",
    })
    assert resp.status_code == 200, resp.text

    exported = client.post(f"/api/kb/course/{course_id}/export")
    assert exported.status_code == 200, exported.text
    out = root / exported.json()["output_rel"]
    pages = list((out / "素材库").glob("*.md"))
    assert len(pages) == 1, pages
    text = pages[0].read_text(encoding="utf-8")

    numbered = [line for line in text.splitlines() if re.match(r"^\[\d+\] ", line)]
    assert len(numbered) == 2, text
    assert numbered[0].startswith("[1] 第一段") and numbered[1].startswith("[2] 第二段")
    # 与提示词口径逐段一致
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        prompt = material_prompt_section(db, course, numbered=True)
    finally:
        db.close()
    assert [line for line in prompt.splitlines() if re.match(r"^\[\d+\] ", line)] == numbered
    assert "段落号 = 按素材解析顺序编号" in text
    assert "怎么读" in text and "内部链接" in text


def test_read_legend_is_shared_by_notes_and_pages():
    """图例只有一份文案（笔记页与交付讲义共用），避免两处漂移。"""
    assert "怎么读" in kb_mod.READ_LEGEND
    assert "第 N 段" in kb_mod.READ_LEGEND and "[N]" in kb_mod.READ_LEGEND
    assert "紫色" in kb_mod.READ_LEGEND and "内部链接" in kb_mod.READ_LEGEND
    assert kb_mod.READ_LEGEND in "\n".join(kb_mod.BANNER), "横幅里要带上图例"


# ---------------------------------------------------------------------------
# ③ 落点：新写法必须在 sources 检查里算「已标注」（提示词侧只产新写法）
# ---------------------------------------------------------------------------

NEW_FORM_MD = """## 本讲目标

- 幻觉来自概率生成机制（来源:《破解AI幻觉》第 3 段）
- 温度调低只能减少、不能消除（来源:《破解AI幻觉》第 12 段）

### 小结

- 先看成因再选方法（来源:《破解AI幻觉》第 20 段）
"""


def test_new_form_counts_as_sourced_in_sources_check():
    """全篇新写法 → sources 判 ok；掺一条没标注的 → 仍判 warn（兼容不等于放宽）。"""
    sources = parse_sources(NEW_FORM_MD, "1.1", CATALOG)
    assert len(sources) == 3, sources
    check = qc_mod._sources_check(NEW_FORM_MD, sources, CATALOG)
    assert check["level"] == "ok", check
    assert "3 条来源标注" in check["detail"], check

    mixed = NEW_FORM_MD + "\n- 任何模型都会编，这是没有标注的一条\n"
    mixed_sources = parse_sources(mixed, "1.1", CATALOG)
    assert len(mixed_sources) == 3
    assert qc_mod._sources_check(mixed, mixed_sources, CATALOG)["level"] == "warn"

    # 一条来源都解析不出来时照旧 warn（老口径不动：「正文没有来源标注」）
    no_cite = "## 本讲目标\n\n- 先看成因再选方法（无直接依据,建议人工确认）\n"
    assert qc_mod._sources_check(no_cite, [], CATALOG)["level"] == "warn"
