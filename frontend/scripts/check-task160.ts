/**
 * 任务 160 前端断言：预览页随堂练习答案默认折叠。
 *
 * 钉住五组：
 * ① 真实样例（4 选项 + 答案 + 解析）：答案区恰好 1 个，且答案与解析在同一组；
 * ② 同一段两道题：答案区 2 个，互不合并；
 * ③ 没有答案：答案区 0 个、块数与输入一致；
 * ④ 答案在中间：顺序不变，前后正文都保留；
 * ⑤ AnswerReveal 默认收起（`useState(false)` + `hidden={!open}`），且只在预览页接线；
 * ⑥ 学员版仍不含 `**答案**` / `**解析**`（真跑 Python）。
 *
 * 运行：`node scripts/check-task160.ts`
 */
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { splitPreviewAnswers } from "../src/lib/previewAnswers.ts";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..");
const read = (rel: string) => readFileSync(join(repo, rel), "utf8").replace(/\r+\n/g, "\n");

const failures: string[] = [];
const check = (ok: boolean, msg: string) => { if (!ok) failures.push(msg); };

const SAMPLE = `#### 第 1 题 · 选择题（难度：易，约 3 分钟）

酵母发酵的最适温度是多少？

-  20~25℃
-  25~30℃
-  30~35℃
-  35~40℃

**答案**：25~30℃

**解析**：酵母发酵的最适温度为 25~30℃，过高会杀死酵母。`;

const TWO_QUESTIONS = `#### 第 1 题

第一题题干

- A
- B

**答案**：B

**解析**：第一题解析

#### 第 2 题

第二题题干

- C
- D

**答案**：D

**解析**：第二题解析`;

const NO_ANSWERS = `# 发酵原理

酵母把糖分解成酒精与二氧化碳。

- 观察体积
- 观察蜂窝`;

const ANSWER_IN_MIDDLE = `前言段落

**答案**：B

**解析**：中间答案

后记段落`;

// ① 真实样例 ---------------------------------------------------------------
const sample = splitPreviewAnswers(SAMPLE);
const sampleAnswers = sample.filter((b) => b.kind === "answers");
check(sampleAnswers.length === 1, `真实样例应有 1 个答案区，实际 ${sampleAnswers.length}`);
check(sample.length === 4, `真实样例切块数应为 4（题头/题干/选项/答案区），实际 ${sample.length}`);
check(sampleAnswers[0]?.blocks.length === 2, "答案与解析必须在同一个答案区（2 个块）");
check((sampleAnswers[0]?.blocks ?? []).join("\n").includes("**解析**"),
  "解析块掉出了答案区");
check(sample[3]?.kind === "answers", "答案区没有落在原答案所在的顺序位置");

// ② 两道题：两个答案区，互不合并 ------------------------------------------
const two = splitPreviewAnswers(TWO_QUESTIONS);
const twoAnswers = two.filter((b) => b.kind === "answers");
check(twoAnswers.length === 2, `两道题应有 2 个答案区，实际 ${twoAnswers.length}`);
check(twoAnswers.every((b) => b.blocks.length === 2), "每道题的答案区都应包含答案 + 解析");
check((twoAnswers[0]?.blocks ?? []).join("\n").includes("第一题解析")
  && (twoAnswers[1]?.blocks ?? []).join("\n").includes("第二题解析"),
  "两道题的答案区内容串了");

// ③ 没有答案：块数不变 -----------------------------------------------------
const noAnswers = splitPreviewAnswers(NO_ANSWERS);
check(noAnswers.filter((b) => b.kind === "answers").length === 0, "没有答案的正文不应出现答案区");
check(noAnswers.length === NO_ANSWERS.split(/\n{2,}/).length,
  `没有答案时块数被改了（${noAnswers.length} != ${NO_ANSWERS.split(/\n{2,}/).length}）`);
check(noAnswers.map((b) => (b.kind === "markdown" ? b.markdown : "")).join("\n").includes("观察蜂窝"),
  "没有答案时丢了正文块");

// ④ 答案在中间：顺序不变 ---------------------------------------------------
const middle = splitPreviewAnswers(ANSWER_IN_MIDDLE);
check(middle.length === 3, `答案在中间应切成 3 块，实际 ${middle.length}`);
check(middle[0].kind === "markdown" && middle[1].kind === "answers" && middle[2].kind === "markdown",
  "答案在中间时顺序不对");
check((middle[0].kind === "markdown" ? middle[0].markdown : "").includes("前言段落")
  && (middle[2].kind === "markdown" ? middle[2].markdown : "").includes("后记段落"),
  "答案前后的正文被吞了");

// ⑤ 组件默认收起 + 只在预览页接线 ------------------------------------------
const reveal = read("frontend/src/components/AnswerReveal.tsx");
const mdflow = read("frontend/src/components/MdWithFlow.tsx");
const preview = read("frontend/src/components/steps/PreviewStep.tsx");
const verify = read("scripts/verify.ps1");

const collapsedByDefault = (src: string) => src.includes("useState(false)") && !src.includes("useState(true)");
check(collapsedByDefault(reveal), "AnswerReveal 默认不是收起（useState(false)）");
check(!collapsedByDefault(reveal.replace("useState(false)", "useState(true)")),
  "变异自检失败：默认展开时应判红");
check(reveal.includes("aria-expanded={open}"), "答案按钮缺 aria-expanded");
check(reveal.includes("hidden={!open}"), "答案容器没有按开关加 hidden");
check(reveal.includes("看答案") && reveal.includes("收起答案"), "答案按钮文案不完整");
check(reveal.includes("REMARK_PLUGINS") && reveal.includes("REHYPE_PLUGINS"),
  "答案区没有复用同一套 Markdown 插件（公式/表格会退化）");

check(mdflow.includes("collapseAnswers?: boolean"), "MdWithFlow 没有 collapseAnswers 可选参数");
check(mdflow.includes("splitPreviewAnswers"), "MdWithFlow 没有使用答案分组纯函数");
check(/<MdWithFlow[\s\S]{0,260}collapseAnswers/.test(preview), "预览页没有开启答案折叠");
for (const rel of [
  "frontend/src/components/steps/WorkbenchStep.tsx",
  "frontend/src/pages/ShareView.tsx",
  "frontend/src/components/ContentPanel.tsx",
]) {
  check(!read(rel).includes("collapseAnswers"), `${rel} 不应开启答案折叠（只影响预览页）`);
}
check(preview.includes("练习题的答案与解析已收起，点「看答案」即可展开（导出与学员版口径不变）。"),
  "预览页缺少答案折叠说明文案");
check(/fontSize:\s*12[\s\S]{0,160}练习题的答案与解析已收起/.test(preview),
  "答案折叠说明不是 12px 灰字");
check(verify.includes("scripts/check-task160.ts"), "verify.ps1 没有接入任务 160 断言");

// ⑥ 学员版口径不变（真跑 Python） ------------------------------------------
const venvPython = join(repo, "backend", ".venv", "Scripts", "python.exe");
const python = existsSync(venvPython) ? venvPython : (process.platform === "win32" ? "python" : "python3");
const probe = `
import json
import sys
sys.path.insert(0, ${JSON.stringify(join(repo, "backend"))})
from app.services.audience import student_markdown
source = json.loads(sys.stdin.read())
out = student_markdown(source)
assert "**答案**" not in out, out
assert "**解析**" not in out, out
assert "**评分要点**" not in out, out
print("ok")
`;
const probeDir = mkdtempSync(join(tmpdir(), "cg160-"));
const probeFile = join(probeDir, "student_probe.py");
writeFileSync(probeFile, probe.replace(/\r+\n/g, "\n"), "utf8");
const result = spawnSync(python, [probeFile], {
  cwd: repo,
  input: JSON.stringify(SAMPLE),
  encoding: "utf8",
  env: { ...process.env, PYTHONIOENCODING: "utf-8" },
});
rmSync(probeDir, { recursive: true, force: true });
check(result.status === 0,
  `学员版回归失败（exit=${result.status} / ${result.error?.code ?? "-"}）：${(result.stderr ?? "").trim().slice(-300)}`);
check((result.stdout ?? "").trim() === "ok", "学员版回归没有正常返回");

if (failures.length) {
  console.error(`任务 160 前端断言失败（${failures.length} 项）：`);
  for (const item of failures) console.error(`  - ${item}`);
  process.exit(1);
}
console.log("任务 160 前端断言通过：答案区 1 题/2 题/无答案/中间答案分组正确，默认收起且只在预览页接线，学员版口径不变。");
