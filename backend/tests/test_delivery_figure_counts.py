"""任务 131：交付包「图表」文件数的护栏（0 LLM，真出包 + 真解 zip）。

来历：任务 130 把「图表」的数字改成按**包内实际文件数**现算 —— 方向对、数字也对，
但 DSH 复验时把 `delivery._figure_file_counts()` 变异成常数（`"files": 99`；四个分桶写死
`87/87/4/1`），**5 个点名文件 48 passed 全绿**，变异包里 `图表/` 只有 36 个文件、
它自己的验收单却写「图表：99 个文件（分项只有 36）」⇒ 自相矛盾照样出厂，没有任何用例会红。
另一处脆弱：`files` 当时是**四个白名单桶之和**，不是「`图表/` 下全部文件数」，
将来 `图表/` 下多出别的文件类型就会**少算**且无测试可拦。

本文件钉住（对应任务 131 §A.2 / §A.3）：
① 端到端：真出一个包 → **自己打开 zip 数 `图表/`**（整棵子树，不看生产代码怎么算）
   ⇒ 与包内 `01-验收单.md` 的总数**逐字相等**、与 `00-交付说明.md` 的总数一致、
   且两份说明括号里的**分项相加 == 总数**；
② 分项也对得上：`svg` / `png` / 两个 Markdown 桶都与 zip 里 `图表/` 的实际文件数相等
   （「只改总数没改分项」「丢掉两个 Markdown 桶」都会红）；
③ 往 `图表/` 塞非白名单文件 ⇒ 总数把它算进去、「其他」桶 = 塞进去的个数、分项之和仍等于总数；
④ 缺目录 / 空目录 ⇒ 全 0 不崩；
⑤ 「其他」桶不为 0 时两份说明都要**显式写出来**（不许静默吞掉）。

协作规则第 7 条：DATABASE_URL / STORAGE_DIR / OBSIDIAN_VAULT_ROOT / PPT_WORK_DIR /
EXPORT_DIR 五件套由 conftest 统一指向临时目录，本文件不碰真实库与真实知识库。
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

from app.models.course import Course
from app.services import delivery as delivery_mod
from tests.test_delivery import (LECTURE_MD, _add_section, _build, _gen_lecture,
                                 _new_course, _node_id, _plan_approve, _seed_deck,
                                 _seed_figures, _seed_material)

FIGURE_DIR = "图表"
ZEROS = {"svg": 0, "png": 0, "tables": 0, "graph": 0, "others": 0, "files": 0}

# 验收单那一行以「图表：」起头；交付说明里 `图表/` 行内也是同一片段
# （「数据图表：svg（…）」不会被误匹配 —— 「图表：」后面不是数字）。
_ROW_RE = re.compile(r"图表：(\d+)\s*个文件（([^）]*)）")
_NUM_RE = re.compile(r"(\d+)\s*个文件（([^）]*)）")
# 分项里的具名桶（两个 Markdown 桶只在不为 0 时出现）。
_BUCKET_RES = {
    "svg": r"svg (\d+)",
    "png": r"png (\d+)",
    "tables": r"表格类数据卡 Markdown (\d+)",
    "graph": r"概念知识图谱 Markdown (\d+)",
    "markdown": r"Markdown (\d+)",       # 交付说明把两个 Markdown 桶合并成一个数
    "others": r"其他 (\d+)",
}


def _parts_sum(parts_text: str) -> int:
    """括号里那串分项里的数字之和（客户会照着加一遍）。"""
    return sum(int(n) for n in re.findall(r"\d+", parts_text))


def _named_parts(parts_text: str) -> dict:
    """抠出分项里的具名桶（没写出来的桶不出现在结果里）。"""
    out = {}
    for key, rex in _BUCKET_RES.items():
        m = re.search(rex, parts_text)
        if m:
            out[key] = int(m.group(1))
    return out


def _zip_figure_files(zip_path: Path) -> dict:
    """打开产出的 zip **自己数** `图表/` 下的文件（整棵子树）—— 不看生产代码怎么算。"""
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist()
                 if n.startswith(f"{FIGURE_DIR}/") and not n.endswith("/")]
    rel = [n[len(FIGURE_DIR) + 1:] for n in names]

    def _in(sub: str, ext: str) -> int:
        return sum(1 for r in rel
                   if r.startswith(f"{sub}/") and "/" not in r[len(sub) + 1:]
                   and r.endswith(ext))

    return {"files": len(names), "svg": _in("svg", ".svg"), "png": _in("png", ".png"),
            "tables": _in("表格", ".md"),
            "graph": sum(1 for r in rel if "/" not in r and r.endswith(".md"))}


def inspect_package(zip_path: Path) -> dict:
    """把「包里数得出来的」与「两份说明上写的」摆在一起（只读，不改任何东西）。"""
    with zipfile.ZipFile(zip_path) as zf:
        acceptance = zf.read("01-验收单.md").decode("utf-8")
        readme = zf.read("00-交付说明.md").decode("utf-8")
    acc = _ROW_RE.search(acceptance)
    assert acc, "验收单里找不到「图表：N 个文件（分项）」这一行"
    readme_line = next((ln for ln in readme.splitlines() if "`图表/`" in ln), "")
    rdm = _NUM_RE.search(readme_line)
    assert rdm, f"交付说明里找不到「图表/」那一行的数量：{readme_line!r}"
    return {
        "disk": _zip_figure_files(zip_path),
        "acceptance": {"total": int(acc.group(1)), "parts": acc.group(2),
                       "parts_sum": _parts_sum(acc.group(2)),
                       "named": _named_parts(acc.group(2))},
        "readme": {"total": int(rdm.group(1)), "parts": rdm.group(2),
                   "parts_sum": _parts_sum(rdm.group(2)),
                   "named": _named_parts(rdm.group(2))},
    }


def assert_package_consistent(zip_path: Path) -> dict:
    """核心护栏（任务 131 §A.2 ①②）：**单子上的数 == 包里数得出来的数**。"""
    info = inspect_package(zip_path)
    disk, acc, rdm = info["disk"], info["acceptance"], info["readme"]
    assert acc["total"] == disk["files"], (
        f"验收单写「图表：{acc['total']} 个文件」，但包里 `图表/` 数出来是 {disk['files']} 个"
        f"（差 {acc['total'] - disk['files']}）—— 客户照单数会数不上；包里明细 {disk}")
    assert rdm["total"] == acc["total"], (
        f"交付说明写 {rdm['total']} 个、验收单写 {acc['total']} 个 —— 两份说明自己就对不上")
    assert acc["parts_sum"] == acc["total"], (
        f"验收单括号里的分项相加 {acc['parts_sum']} != 总数 {acc['total']}"
        f"（原文：{acc['parts']}）")
    assert rdm["parts_sum"] == rdm["total"], (
        f"交付说明括号里的分项相加 {rdm['parts_sum']} != 总数 {rdm['total']}"
        f"（原文：{rdm['parts']}）")
    # 具名分项逐桶对账：非 0 的桶必须写出来，且与包里数得出来的数逐字相等
    for key, label in (("svg", "svg"), ("png", "png"),
                       ("tables", "表格类数据卡 Markdown"), ("graph", "概念知识图谱 Markdown")):
        if disk[key]:
            assert acc["named"].get(key) == disk[key], (
                f"验收单的「{label}」分项写 {acc['named'].get(key)}，"
                f"但包里 `图表/` 数出来是 {disk[key]} 个（原文：{acc['parts']}）")
    if disk["svg"]:
        assert rdm["named"].get("svg") == disk["svg"], f"交付说明的 svg 分项：{rdm['parts']}"
    if disk["png"]:
        assert rdm["named"].get("png") == disk["png"], f"交付说明的 png 分项：{rdm['parts']}"
    assert rdm["named"].get("markdown", 0) == disk["tables"] + disk["graph"], (
        f"交付说明的 Markdown 分项 {rdm['named'].get('markdown')} != 包里两个 Markdown 桶之和 "
        f"{disk['tables'] + disk['graph']}（原文：{rdm['parts']}）")
    extra = disk["files"] - sum(disk[k] for k in ("svg", "png", "tables", "graph"))
    if extra:
        assert acc["named"].get("others") == extra, (
            f"包里还有 {extra} 个非白名单文件，验收单必须显式写出「其他 {extra}」"
            f"（原文：{acc['parts']}）")
    return info


def _packaged_course(client, title: str, monkeypatch) -> Path:
    """真出一包（生产入口 `delivery.build_delivery`，`pdf_engine` 按测试既有做法关掉）。"""
    course_id = _new_course(client, title)
    _plan_approve(client, course_id)
    _add_section(course_id, "1.1", "光合作用的过程")
    node_id = _node_id(client, course_id, "1.1")
    diagram_id = _seed_figures(course_id, node_id, monkeypatch)
    _seed_deck(course_id)
    _seed_material(course_id)
    _gen_lecture(client, course_id, node_id,
                 LECTURE_MD.replace("DIAGRAM_ID", diagram_id), monkeypatch)
    return Path(_build(client, course_id)["zip"])


# ---------------------------------------------------------------------------
# ① 端到端：单子上的数 == 包里数得出来的数（变异 `"files": 99` 时必须红）
# ---------------------------------------------------------------------------

def test_packaged_total_matches_zip_and_both_docs(client, monkeypatch):
    zip_path = _packaged_course(client, "图表计数护栏", monkeypatch)
    info = assert_package_consistent(zip_path)
    disk = info["disk"]
    assert disk["svg"] >= 1 and disk["png"] >= 1, f"本用例要真出图（svg + png）：{disk}"
    assert disk["tables"] + disk["graph"] >= 1, (
        f"本用例要真出 Markdown 桶（表格类数据卡 / 概念知识图谱）——"
        f"否则「丢掉 Markdown 桶」的变异不会红：{disk}")


# ---------------------------------------------------------------------------
# ② 分项也对得上（防「只改总数没改分项」/「丢掉两个 Markdown 桶」）
# ---------------------------------------------------------------------------

def test_packaged_parts_match_zip(client, monkeypatch):
    zip_path = _packaged_course(client, "图表分项护栏", monkeypatch)
    info = inspect_package(zip_path)
    disk = info["disk"]
    assert disk["svg"] >= 1 and disk["png"] >= 1, disk
    assert disk["tables"] >= 1, f"本用例要真出表格类数据卡：{disk}"
    assert info["acceptance"]["named"].get("svg") == disk["svg"], info
    assert info["acceptance"]["named"].get("png") == disk["png"], info
    assert info["acceptance"]["named"].get("tables") == disk["tables"], info
    assert (info["readme"]["named"].get("markdown", 0)
            == disk["tables"] + disk["graph"]), info


# ---------------------------------------------------------------------------
# ③ 多出其它文件类型不会少算（「整棵子树全量」这条要求本身）
# ---------------------------------------------------------------------------

def test_non_whitelisted_file_is_counted(tmp_path):
    root = tmp_path / FIGURE_DIR
    (root / "svg").mkdir(parents=True)
    (root / "svg" / "a.svg").write_text("<svg/>", encoding="utf-8")
    (root / "png").mkdir()
    (root / "png" / "a.png").write_bytes(b"\x89PNG")
    (root / "表格").mkdir()
    (root / "表格" / "a.md").write_text("# a\n", encoding="utf-8")
    (root / "概念图-x.md").write_text("# g\n", encoding="utf-8")
    assert delivery_mod._figure_file_counts(tmp_path) == {
        "svg": 1, "png": 1, "tables": 1, "graph": 1, "others": 0, "files": 4}

    (root / "README.txt").write_text("hi\n", encoding="utf-8")          # 非白名单：顶层
    (root / "png" / "note.txt").write_text("hi\n", encoding="utf-8")    # 非白名单：子目录里
    counts = delivery_mod._figure_file_counts(tmp_path)
    assert counts["files"] == 6, f"总数要是整棵子树的全量（白名单桶之和会少算）：{counts}"
    assert counts["others"] == 2, counts
    assert (counts["svg"] + counts["png"] + counts["tables"] + counts["graph"]
            + counts["others"]) == counts["files"], "分项之和必须恒等于总数"


# ---------------------------------------------------------------------------
# ④ 缺目录 / 空目录不崩
# ---------------------------------------------------------------------------

def test_missing_or_empty_dir_returns_zeros(tmp_path):
    assert delivery_mod._figure_file_counts(tmp_path) == ZEROS       # `图表/` 不存在
    (tmp_path / FIGURE_DIR).mkdir()
    assert delivery_mod._figure_file_counts(tmp_path) == ZEROS       # `图表/` 空目录


# ---------------------------------------------------------------------------
# ⑤ 「其他」桶不为 0 时必须显式写出（不许静默吞掉）
# ---------------------------------------------------------------------------

def _fake_plan() -> dict:
    return {"counts": {"chapters": 1, "points": 2, "lectures": 1, "quizzes": 1,
                       "figures": 3, "decks": 1, "notes_files": 1},
            "voice_tally": "", "notes_sync": {}}


def test_other_bucket_is_written_out_when_nonzero():
    course = Course(title="计数口径", audience="初中生", duration_minutes=90)
    stats = {"model": "mock", "llm_calls": 0, "total_tokens": 0, "cost_cny": 0}
    plan = _fake_plan()
    fig = {"svg": 2, "png": 2, "tables": 1, "graph": 1, "others": 3, "files": 9}
    readme = "\n".join(delivery_mod._readme_lines(
        None, course, plan, stats, [], [], "bid-131", fig_files=fig))
    acceptance = "\n".join(delivery_mod._acceptance_lines(
        None, course, {}, plan, stats, fig_files=fig))
    for text, rex in ((readme, _NUM_RE), (acceptance, _ROW_RE)):
        m = rex.search(text)
        assert m, text[:200]
        assert int(m.group(1)) == 9, m.group(0)
        assert "其他 3" in m.group(2), f"「其他」桶不为 0 却没写出来：{m.group(0)}"
        assert _parts_sum(m.group(2)) == 9, m.group(2)

    # 反证：其他桶为 0 时不许凭空多写一项（否则客户加出来的和会多于总数）
    fig0 = {"svg": 2, "png": 2, "tables": 1, "graph": 1, "others": 0, "files": 6}
    readme0 = "\n".join(delivery_mod._readme_lines(
        None, course, plan, stats, [], [], "bid-131", fig_files=fig0))
    acceptance0 = "\n".join(delivery_mod._acceptance_lines(
        None, course, {}, plan, stats, fig_files=fig0))
    for text in (readme0, acceptance0):
        assert "其他" not in text