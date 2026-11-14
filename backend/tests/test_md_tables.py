"""任务 64：讲义正文已有同数据表格时，不再重复渲染「数据卡」（chart_table）。

起因：AI 课「为什么幻觉是 AI 应用的头号顽疾」正文只有 1 张表，却还 inline 引用了一张
同数据的 `chart_table` ⇒ 课程预览页与交付 HTML/PDF 里同一张表渲染两次（DSH 从样品包
截图抓到）。修法：渲染前按**归一化后逐格相等**判定，跳过重复的卡（只删引用，不删内容）。

判定口径的用例集是**共享正本**（前端 `scripts/check-chart-table-dedup.ts` 读同一份）：
`tests/fixtures/task64_chart_table_cases.json` —— 两侧口径必须一致。

任务 65 起，**第四处**渲染路径（笔记库导出 `kb._rewrite_chart_refs`）也复用同一套判定
与同一份取数（`delivery.chart_table_markdowns`），本文件同时守住「笔记表格数 == 交付
HTML 表格数」这条一致性口径。

任务 66 起，交付包里 `图表/表格/*.md` 这个**文件级**重复也没了：与正文表格同数据的卡
不再单独出降级件（判定同样复用本模块，见 §⑤）—— 同一组数据在包里只留正文那一份。

协作规则第 7 条：DATABASE_URL / STORAGE_DIR / OBSIDIAN_VAULT_ROOT / PPT_WORK_DIR /
EXPORT_DIR 由 conftest 统一指向临时目录，本文件不碰真实库与真实知识库。
"""
from __future__ import annotations

import json
import logging
import zipfile
from pathlib import Path
from uuid import uuid4

from app.models.course import ContentItem
from app.services import delivery as delivery_mod
from app.services import md_tables
from tests.test_delivery import (_add_section, _build, _delivery_dir, _gen_lecture,
                                 _new_course, _node_id, _plan_approve, _seed_material)

FIXTURE = Path(__file__).parent / "fixtures" / "task64_chart_table_cases.json"


def _cases() -> list[dict]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [{"name": c["name"], "card_id": c["card_id"], "body": "\n".join(c["body"]),
             "card": "\n".join(c["card"]), "duplicate": bool(c["duplicate"]),
             "why": c["why"]} for c in data["cases"]]


# ---------------------------------------------------------------------------
# ① 判定口径：共享用例集（与前端同一份）
# ---------------------------------------------------------------------------

def test_shared_criteria_cases() -> None:
    cases = _cases()
    assert len(cases) >= 10, "用例集被删空过？"
    for case in cases:
        card_id = case["card_id"]
        assert f"chart://{card_id}" in case["body"], (
            f"{case['name']}：用例正文必须真的引用这张卡（否则不真实）")
        tables = {card_id: case["card"]}
        got = md_tables.duplicate_chart_ids(case["body"], tables)
        assert (got == [card_id]) is case["duplicate"], (
            f"{case['name']}：期望 duplicate={case['duplicate']}，实际 {got} —— {case['why']}")
        stripped, skipped = md_tables.strip_duplicate_chart_refs(case["body"], tables)
        assert (f"chart://{card_id}" not in stripped) is case["duplicate"], (
            f"{case['name']}：引用是否删除应与判定一致（{case['why']}）")
        if case["duplicate"]:
            kept = [line for line in stripped.split("\n") if line.strip()]
            expect = [line for line in case["body"].split("\n")
                      if line.strip() and "chart://" not in line]
            assert kept == expect, f"{case['name']}：只该删掉那条引用，别的行一字不动"


def test_unreferenced_card_in_map_is_not_a_duplicate() -> None:
    """没被本篇引用的卡不该被算成重复（否则日志会虚报、白跳过）。"""
    body = "| a | b |\n|---|---|\n| 1 | 2 |\n"
    assert md_tables.duplicate_chart_ids(body, {"other-id": "| a | b |\n|---|---|\n| 1 | 2 |"}) == []


def test_parse_and_normalize_edges() -> None:
    assert md_tables.parse_tables("") == []
    assert md_tables.parse_tables("只有正文，没有任何竖线") == []
    # 分隔行必须含 '-'：`| : |` 不是表格
    assert md_tables.parse_tables("| a | b |\n| : | : |\n| 1 | 2 |") == []
    # 表格前后有正文时仍能取到表；分隔行不计入数据行
    tables = md_tables.parse_tables("前言\n\n|a|b|\n|---|---|\n|1|2|\n\n后记\n")
    assert tables == [(["a", "b"], [["1", "2"]])], tables
    # 代码块里的竖线不算表
    assert md_tables.parse_tables("```text\n| a | b |\n|---|---|\n| 1 | 2 |\n```") == []
    # 归一化：来源括号（全角/半角）、**、反引号、空白都吃掉；别的括号留着
    assert md_tables.normalize_cell(" **信任 崩塌** ") == "信任崩塌"
    assert md_tables.normalize_cell("客服问答（来源:《X》第 3 段）") == "客服问答"
    assert md_tables.normalize_cell("客服问答(来源: X #3)") == "客服问答"
    assert md_tables.normalize_cell("`40℃`") == "40℃"
    assert md_tables.normalize_cell("客服问答（含多轮）") == "客服问答（含多轮）"


def test_cell_and_table_covers_rules() -> None:
    """逐格「正文能否代表卡片」的两条规则（既不多删也不少删）。"""
    # 真库「速查决策工具」那对：正文多一句括号补充 ⇒ 能代表卡片，删卡不丢字
    assert md_tables.cell_covers(
        "五法全上（反思校对+引用锚定+手动RAG+温度锁零+多模型交叉）", "五法全上")
    # 反过来：卡片带正文没有的括号补充 ⇒ 不能代表（删了那点内容就没了）
    assert not md_tables.cell_covers("五法全上", "五法全上（含手动 RAG）")
    # 括号里换了词 ⇒ 不能代表
    assert not md_tables.cell_covers("高危（最严重）", "高危（严重）")
    # 正文里换了词 ⇒ 不能代表
    assert not md_tables.cell_covers("医疗诊断/合规报告", "医疗诊断/合规")
    # 来源括号两边都不算内容
    assert md_tables.cell_covers("客服问答（来源:《X》第 3 段）", "客服问答")
    # 行列数必须一致
    assert not md_tables.table_covers((["a"], [["1"], ["2"]]), (["a"], [["1"]]))
    assert not md_tables.table_covers((["a"], [["1", "2"]]), (["a"], [["1"]]))
    assert md_tables.table_covers((["a"], [["1"]]), (["a"], [["1"]]))


def test_chart_table_markdowns_follows_resolver_shape() -> None:
    """口径跟 resolve_image 走：markdown_table ≥2 行（表头 + 分隔行）就走表格分支 ⇒ 参与去重。

    最后两个：``d`` 只有表头没有数据行 —— resolve_image 一样会把它渲染成表格（表头为空表体），
    所以同样收进来;``c`` 不是 chart_table，不进。
    """
    items = [
        ContentItem(id="a", kind="chart_table", title="表卡",
                    body={"markdown_table": "| a |\n|---|\n| 1 |"}),
        ContentItem(id="b", kind="chart_table", title="有图的表卡",
                    body={"markdown_table": "| a |\n|---|\n| 1 |",
                          "svg_preview": "images/x.svg"}),
        ContentItem(id="d", kind="chart_table", title="坏表格",
                    body={"markdown_table": "| a |\n|---|"}),
        ContentItem(id="c", kind="chart_bar", title="柱状图", body={"data": {}}),
    ]
    assert list(delivery_mod.chart_table_markdowns(items)) == ["a", "b", "d"]


# ---------------------------------------------------------------------------
# ② 交付包：重复的卡不再渲染，唯一的卡照旧渲染（内容不丢）
# ---------------------------------------------------------------------------

LECTURE_MD = """## 光合作用的过程

### 核心讲解

- **光反应**发生在类囊体薄膜上（来源: 光合作用导论 #2）

| 阶段 | 场所 |
|---|---|
| 光反应 | 类囊体薄膜 |
| 暗反应 | 叶绿体基质 |

![数据卡：阶段对比](chart://CARD_DUP)

![数据卡：温度窗口](chart://CARD_UNIQ)

### 小结

- 光反应供能、暗反应固碳（来源: 光合作用导论 #3）
"""
DUP_TABLE = "| 阶段 | 场所 |\n|---|---|\n| 光反应 | 类囊体薄膜 |\n| 暗反应 | 叶绿体基质 |"
UNIQ_TABLE = "| 项目 | 数值 |\n|---|---|\n| 最适温度 | 25~30℃ |\n| 水温上限 | 40℃ |"


def _seed_cards(course_id: str, node_id: str, dup_id: str, uniq_id: str) -> None:
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        db.add_all([
            ContentItem(id=dup_id, course_id=course_id, node_id=node_id,
                        kind="chart_table", title="阶段对比",
                        body={"chart_type": "table", "title": "阶段对比",
                              "markdown_table": DUP_TABLE},
                        meta={"node_key": "1.1"}),
            ContentItem(id=uniq_id, course_id=course_id, node_id=node_id,
                        kind="chart_table", title="温度窗口",
                        body={"chart_type": "table", "title": "温度窗口",
                              "markdown_table": UNIQ_TABLE},
                        meta={"node_key": "1.1"}),
        ])
        db.commit()
    finally:
        db.close()


def _course_with_cards(client, monkeypatch) -> tuple[str, str]:
    """(course_id, 重复卡 id)。两个卡 id 每次唯一 —— 共享临时库不撞 UNIQUE(id)。"""
    dup_id, uniq_id = uuid4().hex, uuid4().hex
    course_id = _new_course(client, "表格去重探针")
    _plan_approve(client, course_id)
    _add_section(course_id, "1.1", "光合作用的过程")
    node_id = _node_id(client, course_id, "1.1")
    _seed_material(course_id)
    _seed_cards(course_id, node_id, dup_id, uniq_id)
    markdown = LECTURE_MD.replace("CARD_DUP", dup_id).replace("CARD_UNIQ", uniq_id)
    _gen_lecture(client, course_id, node_id, markdown, monkeypatch)
    return course_id, dup_id


def test_delivery_html_dedups_duplicate_card(client, monkeypatch, caplog) -> None:
    course_id, dup_id = _course_with_cards(client, monkeypatch)
    with caplog.at_level(logging.INFO, logger="app.services.md_tables"):
        summary = _build(client, course_id)
    out_dir = _delivery_dir(summary["build_id"])

    html = sorted((out_dir / "讲义").rglob("*.html"))[0].read_text(encoding="utf-8")
    assert html.count("<table>") == 2, "正文 1 张 + 唯一的卡 1 张；重复的那张必须跳过"
    assert html.count("叶绿体基质") == 1, "重复卡片的那份表格不该再渲染一遍"
    assert "25–30℃" in html, "不重复的数据卡必须照旧渲染（区间按渲染层规范成短横线）"

    # 源 markdown 不动（引用仍在），内容也不丢
    md_text = sorted((out_dir / "讲义").rglob("*.md"))[0].read_text(encoding="utf-8")
    assert md_text.count("chart://") == 2, "交付的 .md 源保持原样"
    figure_tables = sorted((out_dir / "图表" / "表格").glob("*.md"))
    assert len(figure_tables) == 1, "任务 66 起：与正文同数据的卡不再单独出降级件，只剩唯一那张"
    assert all("叶绿体基质" not in p.read_text(encoding="utf-8") for p in figure_tables)
    assert any("25~30℃" in p.read_text(encoding="utf-8") for p in figure_tables)

    assert any("跳过数据卡 chart://" + dup_id in r.message for r in caplog.records), \
        "跳过重复数据卡要打日志（便于统计影响面）"


def test_publish_package_html_dedups_duplicate_card(client, monkeypatch) -> None:
    """发布包（HTML/SCORM）走同一套去重 —— 与交付包口径一致，别只改一边。"""
    course_id, _dup_id = _course_with_cards(client, monkeypatch)
    resp = client.post("/api/publish/export", json={"course_id": course_id, "format": "html"})
    assert resp.status_code == 200, resp.text
    with zipfile.ZipFile(Path(resp.json()["file"])) as zf:
        pages = [zf.read(n).decode("utf-8") for n in zf.namelist()
                 if n.endswith(".html") and "index" not in n]
    assert pages, "发布包里要有课时页"
    lesson = max(pages, key=len)
    assert lesson.count("<table>") == 2, "正文 1 张 + 唯一的卡 1 张"
    assert lesson.count("叶绿体基质") == 1
    assert "25–30℃" in lesson


# ---------------------------------------------------------------------------
# ④ 任务 65：笔记库导出（kb.py）—— 第四处渲染路径，口径与前三条完全一致
# ---------------------------------------------------------------------------

def _note_entry(entries: list[tuple[str, bytes]], rel: str) -> str:
    for path, data in entries:
        if path == rel:
            return data.decode("utf-8")
    raise AssertionError(f"没找到笔记页 {rel}：{[p for p, _ in entries]}")


def _kb_note_and_lecture(client, monkeypatch):
    """建一课（正文 1 张表 + 重复卡 + 唯一卡），返回 (笔记页 md, 交付 HTML, 重复卡 id)。"""
    from app.db.session import SessionLocal
    from app.models.course import ContentItem, Course
    from app.services import kb as kb_mod
    from app.services import publish as publish_mod

    course_id, dup_id = _course_with_cards(client, monkeypatch)
    db = SessionLocal()
    try:
        course = db.get(Course, course_id)
        items = db.query(ContentItem).filter(ContentItem.course_id == course_id).all()
        lecture = next(i for i in items if i.kind == "lecture")
        nodes = kb_mod._nodes(db, course_id)
        node = next(n for n in nodes if n.node_key == "1.1")
        rel = kb_mod._node_note_rel(nodes, node)
        assert rel is not None
        note = _note_entry(kb_mod.collect_node_notes(db, course, [node.id]), rel)
        # 交付侧按 build_delivery 的两步来：先去重，再渲染（与交付包/发布包同一条路径）
        md, _skipped = md_tables.strip_duplicate_chart_refs(
            delivery_mod._delivery_lecture_markdown(lecture),
            delivery_mod.chart_table_markdowns(items))
        html = delivery_mod.markdown_to_html(
            md, resolve_image=publish_mod._figure_resolver(items))
    finally:
        db.close()
    return note, html, dup_id


def test_kb_note_dedups_duplicate_card(client, monkeypatch, caplog) -> None:
    """任务 65：笔记页里正文已有同数据表格时，数据卡不再渲染第二遍。"""
    from app.services import kb as kb_mod  # noqa: F401  —— 日志 logger 名对齐

    with caplog.at_level(logging.INFO, logger="app.services.kb"):
        note, _html, dup_id = _kb_note_and_lecture(client, monkeypatch)

    assert len(md_tables.parse_tables(note)) == 2, "正文 1 张 + 不重复的卡 1 张；重复的那张要跳过"
    assert note.count("叶绿体基质") == 1, "重复卡片的表格不该在笔记里再出现一次"
    assert "25~30℃" in note, "不重复的数据卡在笔记里照旧渲染"
    assert "表格：阶段对比" not in note, "重复卡的块头也要一起消失"
    assert any(f"chart://{dup_id}" in r.message for r in caplog.records), \
        "笔记路径跳过重复卡也要打日志"


def test_kb_note_table_count_matches_delivery_html(client, monkeypatch) -> None:
    """验收口径：同一篇里「笔记表格数 == 交付 HTML 表格数」（四条渲染路径不许各说各话）。"""
    note, html, _dup_id = _kb_note_and_lecture(client, monkeypatch)
    assert len(md_tables.parse_tables(note)) == html.count("<table>") == 2


def test_kb_course_level_page_dedups_duplicate_card(client, monkeypatch) -> None:
    """课程级内容页（未绑定节点的讲义）走同一条去重。"""
    from app.db.session import SessionLocal
    from app.models.course import ContentItem, Course
    from app.services import kb as kb_mod

    dup_id = uuid4().hex
    course_id = _new_course(client, "课程级表格去重探针")
    db = SessionLocal()
    try:
        db.add_all([
            ContentItem(course_id=course_id, node_id=None, kind="lecture", title="课程级讲义",
                        body={"markdown": LECTURE_MD.replace("CARD_DUP", dup_id)
                              .replace("CARD_UNIQ", uuid4().hex)}),
            ContentItem(id=dup_id, course_id=course_id, node_id=None, kind="chart_table",
                        title="阶段对比",
                        body={"chart_type": "table", "title": "阶段对比",
                              "markdown_table": DUP_TABLE}),
        ])
        db.commit()
        payload = kb_mod._build_course_level_payload(db, db.get(Course, course_id))
    finally:
        db.close()

    assert payload.md, "课程级页要有内容"
    assert len(md_tables.parse_tables(payload.md)) == 1, "正文那张表留下，同数据的卡跳过"
    assert payload.md.count("叶绿体基质") == 1


# ---------------------------------------------------------------------------
# ⑤ 任务 66：交付包里「与正文同数据」的表格降级件也不再单独出文件（文件级去重）
# ---------------------------------------------------------------------------

SUPER_TABLE = ("| 阶段 | 场所 |\n|---|---|\n| 光反应 | 类囊体薄膜 |\n"
               "| 暗反应 | 叶绿体基质 |\n| 光呼吸 | 过氧化物酶体 |")


def _figure_table_files(out_dir: Path) -> list[Path]:
    table_dir = out_dir / "图表" / "表格"
    return sorted(table_dir.glob("*.md")) if table_dir.is_dir() else []


def test_delivery_package_skips_merged_table_file(client, monkeypatch) -> None:
    """重复卡不再单独出 `图表/表格/*.md`（同一组数据只留正文那一份），唯一卡照旧出。"""
    course_id, _dup_id = _course_with_cards(client, monkeypatch)
    summary = _build(client, course_id)
    out_dir = _delivery_dir(summary["build_id"])

    files = _figure_table_files(out_dir)
    assert len(files) == 1, [p.name for p in files]
    text = files[0].read_text(encoding="utf-8")
    assert "25~30℃" in text and "叶绿体基质" not in text, "留下的必须只是「正文没有同数据表格」那张"

    # 讲义一个字没动：正文里那张表还在、两条 chart:// 引用也还在（只影响包里的降级件）
    md_text = sorted((out_dir / "讲义").rglob("*.md"))[0].read_text(encoding="utf-8")
    assert md_text.count("chart://") == 2
    assert md_text.count("叶绿体基质") == 1

    # manifest 与磁盘一致：清单里没有那个文件，磁盘上也没有清单外的文件
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    paths = {row["path"] for row in manifest["files"]}
    assert not [p for p in paths if p.startswith("图表/表格/") and "阶段对比" in p]
    on_disk = {p.relative_to(out_dir).as_posix() for p in out_dir.rglob("*") if p.is_file()}
    assert on_disk - {"manifest.json"} == paths


def test_delivery_report_wording_matches_disk(client, monkeypatch) -> None:
    """报告不能与磁盘打架：降级项要写「已并入正文表格」，不能再指向一个没出的文件。"""
    course_id, _dup_id = _course_with_cards(client, monkeypatch)
    summary = _build(client, course_id)
    out_dir = _delivery_dir(summary["build_id"])

    readme = (out_dir / "00-交付说明.md").read_text(encoding="utf-8")
    assert "阶段对比" in readme, "被并进正文的那张卡要在降级项里如实写明（不是静默少文件）"
    assert "已并入讲义正文表格" in readme
    for line in readme.splitlines():
        if "阶段对比" in line:
            assert "以 Markdown 表格随包交付" not in line, "那条理由已经过期（文件不再单独出）"
    assert "表格类数据卡以 Markdown 随包" in readme, "`图表/` 的口径说明要跟着改"

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    merged = [row for row in manifest["degraded"] if "阶段对比" in row["item"]]
    assert merged and "已并入讲义正文表格" in merged[0]["reason"], manifest["degraded"]

    acceptance = (out_dir / "01-验收单.md").read_text(encoding="utf-8")
    assert "Markdown（表格类数据卡 / 概念知识图谱）" in acceptance, "`图表/` 的口径说明要跟着改"
    assert "数据已写进讲义正文的表格并入正文，不再单独出文件" in acceptance, "不是静默少文件"


def test_delivery_keeps_table_file_when_body_differs(client, monkeypatch) -> None:
    """反向用例：卡片表比正文表**多一行** ⇒ 口径判不出重复 ⇒ 文件照旧出（不许误删）。"""
    from app.db.session import SessionLocal

    course_id = _new_course(client, "文件级去重反例")
    _plan_approve(client, course_id)
    _add_section(course_id, "1.1", "光合作用的过程")
    node_id = _node_id(client, course_id, "1.1")
    _seed_material(course_id)
    card_id = uuid4().hex
    db = SessionLocal()
    try:
        db.add(ContentItem(id=card_id, course_id=course_id, node_id=node_id,
                           kind="chart_table", title="阶段全景",
                           body={"chart_type": "table", "title": "阶段全景",
                                 "markdown_table": SUPER_TABLE},
                           meta={"node_key": "1.1"}))
        db.commit()
    finally:
        db.close()
    markdown = LECTURE_MD.replace("CARD_DUP", card_id).replace("CARD_UNIQ", uuid4().hex)
    _gen_lecture(client, course_id, node_id, markdown, monkeypatch)

    summary = _build(client, course_id)
    out_dir = _delivery_dir(summary["build_id"])
    files = _figure_table_files(out_dir)
    assert len(files) == 1 and "光呼吸" in files[0].read_text(encoding="utf-8"), \
        [p.name for p in files]
    html = sorted((out_dir / "讲义").rglob("*.html"))[0].read_text(encoding="utf-8")
    assert html.count("<table>") == 2, "正文 1 张 + 这张卡 1 张（行数不同，没被判成重复）"
