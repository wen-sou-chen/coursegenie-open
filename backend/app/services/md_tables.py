"""Markdown 表格解析 + 「正文已有同数据表格」判定（任务 64）。

起因：讲义里的「数据卡」（``chart_table`` 条目）如果和正文里某张表完全同数据，
渲染时就会把同一张表画两遍 —— 课程预览页 / 交付 HTML / PDF 都能看见（2026-09-19
DSH 从「接单样品包」截图里抓到）。**不是**数据错，是同一份数据重复呈现。

这里给出**一套**判定，供后端渲染（``delivery`` / ``publish``）使用；前端
``frontend/src/lib/mdTables.ts`` 是同一套口径的 TypeScript 版（两侧都要改，
别只改一边）。

判定口径（两侧逐条一致）：
1. 表格 = 「表头行 + 分隔行 + 若干数据行」的连续块；代码块里的竖线不算；
2. 单元格比较（``cell_covers``）：去掉**所有**括号内容后要完全相同，**并且**卡片的文字
   要完整出现在正文对应单元格里；
3. 表头与每一行数据**逐格**都能这样对上才算「正文能代表这张卡」—— 少一列、多一行、
   正文里换个词都不算。

为什么要有第 2 条的「后半句」：真库里有两种括号写法 ——
正文 ``五法全上（反思校对+引用锚定+手动RAG+温度锁零+多模型交叉）`` vs 卡片 ``五法全上``
（正文更全 ⇒ 删卡不丢字，该删）；反过来 ``卡片带括号补充、正文没有`` 时删卡会把那点内容
弄丢 ⇒ 不删。判定必须**既不多删也不少删**。

口径回归用例集：``backend/tests/fixtures/task64_chart_table_cases.json``
（pytest 与前端 ``scripts/check-chart-table-dedup.ts`` 读**同一份**，防止两侧走偏）。
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping

logger = logging.getLogger(__name__)

# 表格 = (表头, 数据行)
Table = tuple[list[str], list[list[str]]]

_RE_FENCE = re.compile(r"```[A-Za-z0-9_-]*\n.*?```", re.S)
_RE_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_RE_TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
# 左圆括号允许全角/半角，只吃掉「来源:…」这一种括号（别的括号算正文的一部分）
_RE_SOURCE_PAREN = re.compile(r"[（(]\s*来源\s*[:：][^）)]*[）)]")
_RE_ANY_PAREN = re.compile(r"[（(][^）)]*[）)]")
_RE_CHART_REF = re.compile(r"!\[[^\]]*\]\(\s*chart://([A-Za-z0-9_-]+)\s*\)")


def split_row(line: str) -> list[str]:
    """``| a | b |`` → ``["a", "b"]``（两侧竖线去掉、单元格 strip）。"""
    return [cell.strip() for cell in str(line).strip().strip("|").split("|")]


def is_separator(line: str) -> bool:
    """是不是表格的分隔行（必须含至少一个 ``-``，``| : |`` 不算）。"""
    text = str(line)
    return bool(_RE_TABLE_SEP.match(text)) and "-" in text


def parse_tables(markdown: str) -> list[Table]:
    """正文里的 Markdown 表格 → ``[(表头, 数据行), …]``；代码块内的竖线不算表。"""
    lines = _RE_FENCE.sub("\n", str(markdown or "")).split("\n")
    tables: list[Table] = []
    i = 0
    while i < len(lines):
        if (i + 1 < len(lines) and _RE_TABLE_ROW.match(lines[i])
                and is_separator(lines[i + 1])):
            rows: list[list[str]] = []
            j = i + 2
            while j < len(lines) and _RE_TABLE_ROW.match(lines[j]):
                rows.append(split_row(lines[j]))
                j += 1
            tables.append((split_row(lines[i]), rows))
            i = j
            continue
        i += 1
    return tables


def _squeeze(text: str) -> str:
    """去掉 ``**`` / 反引号 / 所有空白。"""
    return re.sub(r"\s+", "", str(text).replace("**", "").replace("`", ""))


def normalize_cell(cell: object) -> str:
    """单元格归一化：去「（来源:…）」→ 去 ``**`` / 反引号 → 去所有空白。"""
    return _squeeze(_RE_SOURCE_PAREN.sub("", str(cell if cell is not None else "")))


def core_cell(cell: object) -> str:
    """去掉**所有**括号内容后再归一化（正文常在同一格里补一句括号说明）。"""
    return _squeeze(_RE_ANY_PAREN.sub("", str(cell if cell is not None else "")))


def cell_covers(body_cell: object, card_cell: object) -> bool:
    """正文单元格能否代表卡片单元格（能就说明删卡不丢字）。

    ① 去掉所有括号内容后两格相同；② 卡片那格（去掉来源括号后）完整出现在正文格里。
    """
    return (core_cell(body_cell) == core_cell(card_cell)
            and normalize_cell(card_cell) in normalize_cell(body_cell))


def table_covers(body: Table, card: Table) -> bool:
    """正文这张表能否代表卡片这张表（表头 + 每行逐格都要能代表；行/列数必须一致）。"""
    (body_head, body_rows), (card_head, card_rows) = body, card
    if len(body_head) != len(card_head) or len(body_rows) != len(card_rows):
        return False
    if not all(cell_covers(b, c) for b, c in zip(body_head, card_head)):
        return False
    # 逐行比列数（zip 会悄悄截断，别拿它当列数校验）
    return all(len(body_row) == len(card_row)
               and all(cell_covers(b, c) for b, c in zip(body_row, card_row))
               for body_row, card_row in zip(body_rows, card_rows))


def referenced_chart_ids(markdown: str) -> list[str]:
    """正文里 inline 引用的数据卡 id（按出现顺序去重）。"""
    seen: dict[str, None] = {}
    for ident in _RE_CHART_REF.findall(str(markdown or "")):
        seen.setdefault(ident, None)
    return list(seen)


def duplicate_chart_ids(markdown: str, tables: Mapping[str, str]) -> list[str]:
    """本篇**引用了的**数据卡里，与正文某张表同数据的卡 id（``tables`` 见调用方）。

    两点保守：
    ① 正文没有表格 ⇒ 一律不算重复；
    ② 某张卡解析不出表格（格式坏）⇒ 不算重复 —— 宁可多渲染一张，也不能把正文里
       没有的那份数据删掉。
    """
    body = parse_tables(markdown)
    if not body:
        return []
    out: list[str] = []
    for ident in referenced_chart_ids(markdown):
        card_md = tables.get(ident)
        if not card_md:
            continue
        card = parse_tables(card_md)
        if card and any(table_covers(candidate, card[0]) for candidate in body):
            out.append(ident)
    return out


def strip_duplicate_chart_refs(markdown: str,
                               tables: Mapping[str, str]) -> tuple[str, list[str]]:
    """把「正文已有同数据表格」的数据卡引用整条去掉，并逐条打日志。

    返回 ``(新 markdown, 被跳过的卡 id 列表)``；没有重复时原样返回。
    只删正文里的引用；卡片条目本身不动。**交付包是否单独出 ``图表/表格/*.md`` 降级件由
    ``delivery._figures_section`` 决定**（任务 66 起：数据已并入讲义正文的那几张不再出文件），
    内容至少留一份。
    """
    duplicates = duplicate_chart_ids(markdown, tables)
    if not duplicates:
        return markdown, []
    out = str(markdown or "")
    for ident in duplicates:
        ref = re.compile(r"!\[[^\]]*\]\(\s*chart://" + re.escape(ident) + r"\s*\)")
        out, count = ref.subn("", out)
        if count:
            logger.info("讲义正文已有同数据表格，跳过数据卡 chart://%s（同一张表不再渲染两次）",
                        ident)
    return out, duplicates
