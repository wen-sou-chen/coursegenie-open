/**
 * 正文 Markdown 表格解析 + 「正文已有同数据表格」判定（任务 64）。
 *
 * 讲义正文里已经写出同一张表时，「数据卡」（kind=chart_table）不该再渲染一遍 ——
 * 否则同一张表在课程预览页 / 分享页 / 交付 HTML 里出现两次。后端
 * `backend/app/services/md_tables.py` 是同一套口径的 Python 版（两侧都要改）。
 *
 * 判定口径（与 Python 版逐条一致）：
 * 1. 表格 = 「表头行 + 分隔行 + 若干数据行」的连续块；代码块里的竖线不算；
 * 2. 单元格比较（`cellCovers`）：去掉**所有**括号内容后要完全相同，**并且**卡片的文字要
 *    完整出现在正文对应单元格里；
 * 3. 表头与每一行数据逐格都能这样对上才算「正文能代表这张卡」—— 少一列、多一行、
 *    正文里换个词都不算。
 *
 * 第 2 条后半句是**防丢字**：正文 `五法全上（反思校对+…）` 能代表卡片 `五法全上`（该删），
 * 反过来卡片带正文没有的括号补充时不能删（删了那点内容就没了）。
 *
 * 口径回归用例集与 pytest 共用同一份：`backend/tests/fixtures/task64_chart_table_cases.json`
 * （见 `scripts/check-chart-table-dedup.ts`）。
 */

/** 一张 Markdown 表格：表头 + 数据行（分隔行不算数据行）。 */
export interface MdTable {
  headers: string[];
  rows: string[][];
}

const FENCE_ALL = /```[A-Za-z0-9_-]*\n[\s\S]*?```/g;
const TABLE_ROW = /^\s*\|.*\|\s*$/;
const TABLE_SEP = /^\s*\|[\s:|-]+\|\s*$/;
const SOURCE_PAREN = /[（(]\s*来源\s*[:：][^）)]*[）)]/g;
const ANY_PAREN = /[（(][^）)]*[）)]/g;

/** `| a | b |` → `["a", "b"]`（两侧竖线去掉、单元格 trim）。 */
export function splitRow(line: string): string[] {
  return line
    .trim()
    .replace(/^\|+/, "")
    .replace(/\|+$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

/** 是不是表格的分隔行（必须含至少一个 `-`，`| : |` 不算）。 */
export function isSeparator(line: string): boolean {
  return TABLE_SEP.test(line) && line.includes("-");
}

/** 正文里的 Markdown 表格 → `[{headers, rows}, …]`；代码块内的竖线不算表。 */
export function parseTables(markdown: string): MdTable[] {
  const lines = String(markdown ?? "")
    .replace(FENCE_ALL, "\n")
    .split("\n");
  const tables: MdTable[] = [];
  let i = 0;
  while (i < lines.length) {
    if (i + 1 < lines.length && TABLE_ROW.test(lines[i]) && isSeparator(lines[i + 1])) {
      const rows: string[][] = [];
      let j = i + 2;
      while (j < lines.length && TABLE_ROW.test(lines[j])) {
        rows.push(splitRow(lines[j]));
        j += 1;
      }
      tables.push({ headers: splitRow(lines[i]), rows });
      i = j;
      continue;
    }
    i += 1;
  }
  return tables;
}

const squeeze = (text: string): string =>
  String(text).replace(/\*\*/g, "").replace(/`/g, "").replace(/\s+/g, "");

/** 单元格归一化：去「（来源:…）」→ 去 `**` / 反引号 → 去所有空白。 */
export function normalizeCell(cell: unknown): string {
  return squeeze(String(cell ?? "").replace(SOURCE_PAREN, ""));
}

/** 去掉**所有**括号内容后再归一化（正文常在同一格里补一句括号说明）。 */
export function coreCell(cell: unknown): string {
  return squeeze(String(cell ?? "").replace(ANY_PAREN, ""));
}

/**
 * 正文单元格能否代表卡片单元格（能就说明删卡不丢字）：
 * ① 去掉所有括号内容后两格相同；② 卡片那格（去掉来源括号后）完整出现在正文格里。
 */
export function cellCovers(bodyCell: unknown, cardCell: unknown): boolean {
  return coreCell(bodyCell) === coreCell(cardCell)
    && normalizeCell(bodyCell).includes(normalizeCell(cardCell));
}

/** 正文这张表能否代表卡片这张表（表头 + 每行逐格都要能代表；行/列数必须一致）。 */
export function tableCovers(body: MdTable, card: MdTable): boolean {
  if (body.headers.length !== card.headers.length || body.rows.length !== card.rows.length) {
    return false;
  }
  if (!body.headers.every((cell, idx) => cellCovers(cell, card.headers[idx]))) return false;
  return body.rows.every((row, idx) =>
    row.length === card.rows[idx].length
    && row.every((cell, col) => cellCovers(cell, card.rows[idx][col])));
}

/**
 * 数据卡的 markdown 表格是否与正文里某张表同数据（同数据 ⇒ 该卡不必再渲染）。
 * 卡片表格解析不出来（格式坏）时返回 false —— 宁可多渲染一张，也不能把正文里
 * 没有的那份数据删掉。
 */
export function isDuplicateTable(cardMd: string, bodyTables: MdTable[]): boolean {
  const card = parseTables(cardMd);
  return card.length > 0 && bodyTables.some((body) => tableCovers(body, card[0]));
}
