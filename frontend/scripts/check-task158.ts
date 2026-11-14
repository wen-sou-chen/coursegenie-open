/**
 * 任务 158 前端断言：目标受众共享组件 + 课程删除入口。
 *
 * 钉住四组：
 * ① 受众只留一份实现：`AudienceField` 同时被建课表单与大纲表单引用，
 *    候选 >= 10、placeholder 是示例式、下方说明与字号符合规格；
 * ② 列表每行有「删除」（danger + 图标 + 文字），点按钮不触发进课；
 * ③ 删除必须输入完整课程名：按钮 disabled 条件逐字比较，另有「请输入完整课程名」提示；
 * ④ API 与后端：`DELETE /course/{id}?confirm=`、confirm 不符 400、清理按 id/引用而非课名。
 *
 * ⛔ 0 LLM：只读源码，不起服务、不碰真库 / 真 storage。
 * 运行：`node scripts/check-task158.ts`（Node >= 22.18 自带类型擦除）
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..");
const read = (rel: string) => readFileSync(join(repo, rel), "utf8").replace(/\r+\n/g, "\n");
const stripComments = (s: string) =>
  s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

const REL_AUDIENCE = "frontend/src/components/AudienceField.tsx";
const REL_COURSE_LIST = "frontend/src/pages/CourseList.tsx";
const REL_OUTLINE = "frontend/src/components/OutlinePanel.tsx";
const REL_API = "frontend/src/api.ts";
const REL_ROUTE = "backend/app/api/routes/courses.py";
const REL_SERVICE = "backend/app/services/course_deletion.py";
const REL_VERIFY = "scripts/verify.ps1";

const REQUIRED_AUDIENCES = [
  "零基础成人", "职场新人", "在职从业者", "考证备考生", "大学生", "中学生",
  "小学生", "家长", "企业内训学员", "教师与教研员", "自由职业者",
];
const PLACEHOLDER = "例如：零基础成年人、在职会计、备考学员（可自己填）";
const HINT = "影响讲义的口味与难度；可以随时改，不影响已生成内容";
const DELETE_GUARD = "disabled: !deleteTarget || confirmName !== deleteTarget.title";

const failures: string[] = [];
const check = (ok: boolean, msg: string) => { if (!ok) failures.push(msg); };

const audience = stripComments(read(REL_AUDIENCE));
const courseList = stripComments(read(REL_COURSE_LIST));
const outline = stripComments(read(REL_OUTLINE));
const api = stripComments(read(REL_API));
const route = stripComments(read(REL_ROUTE));
const service = stripComments(read(REL_SERVICE));
const verify = read(REL_VERIFY);

// ① 共享受众组件 ------------------------------------------------------------
check(/import\s*\{[^}]*AutoComplete[^}]*\}\s*from\s*"antd"/.test(audience),
  "AudienceField 没有从 antd 引入 AutoComplete（自由输入 + 候选）");
const optionsBlock = audience.match(/export const AUDIENCE_OPTIONS = \[([\s\S]*?)\]\.map/)?.[1] ?? "";
const options = [...optionsBlock.matchAll(/"([^"]+)"/g)].map((m) => m[1]);
check(options.length >= 10, `受众候选只有 ${options.length} 个，规格要求 >= 10`);
for (const name of REQUIRED_AUDIENCES) {
  check(options.includes(name), `受众候选缺少「${name}」`);
}
check(audience.includes(PLACEHOLDER), "placeholder 不是规格里的示例式文案");
check(audience.includes(HINT), "受众字段下方缺少规格里的说明句");
check(/fontSize:\s*12/.test(audience), "说明文字不是 12px 灰字");
check(!/<Input\b/.test(audience), "AudienceField 又退回成 Input（失去候选）");

check(courseList.includes('import AudienceField from "../components/AudienceField"'),
  "建课表单没有引用共享 AudienceField");
check(outline.includes('import AudienceField from "./AudienceField"'),
  "大纲表单没有引用共享 AudienceField");
check((courseList.match(/<AudienceField\s*\/>/g) ?? []).length === 1,
  "建课表单应恰好使用一次 AudienceField");
check((outline.match(/<AudienceField\s*\/>/g) ?? []).length === 1,
  "大纲表单应恰好使用一次 AudienceField");
check(!/<Input placeholder="初中生"/.test(courseList), "建课表单仍留着 placeholder=初中生 的 Input");
check(!/name="audience"[\s\S]{0,160}<Input\b/.test(courseList + outline),
  "仍有表单把目标受众写回 Input（两套实现）");

function audienceIsShared(src: string): boolean {
  return src.includes("import AudienceField")
    && src.includes("<AudienceField />")
    && !/name="audience"[\s\S]{0,160}<Input\b/.test(src);
}
check(audienceIsShared(courseList), "建课表单没有走共享受众组件");
check(audienceIsShared(outline), "大纲表单没有走共享受众组件");
check(!audienceIsShared(courseList.replace("<AudienceField />", '<Input placeholder="初中生" />')),
  "变异自检失败：把共享组件换回 Input 时应判红");

// ② / ③ 删除入口与强确认 ----------------------------------------------------
check(courseList.includes("DeleteOutlined"), "删除按钮没有图标");
check(/<Button[\s\S]{0,180}danger[\s\S]{0,300}删除[\s\S]{0,60}<\/Button>/.test(courseList),
  "列表没有 danger + 文字「删除」的按钮");
check(courseList.includes("event.stopPropagation()"), "点删除会顺带触发进课");
check(courseList.includes("`删除课程《${deleteTarget.title}》？`"),
  "二次确认标题没有带课程名");
check(courseList.includes("将一并删除：大纲与章节、讲义/教案/学案、课件（PPTX 与网页版）、配图、习题、笔记库、以及该课程在"),
  "二次确认没有逐项说明会删什么");
check(courseList.includes("删除后无法恢复。"), "二次确认没有「删除后无法恢复」");
check(courseList.includes("请输入课程全名以确认："), "二次确认没有要求输入课程全名");
check(courseList.includes("请输入完整课程名"), "不一致时没有「请输入完整课程名」提示");
check(courseList.includes(DELETE_GUARD), "确认按钮没有逐字比较课程全名");
check(courseList.includes("api.deleteCourse(target.id, confirmName)"),
  "确认删除没有把用户输入传给后端");
check(courseList.includes("已删除《${target.title}》，清理 ${result.removed_files} 个文件"),
  "成功后没有按规格提示清理文件数");
check(courseList.includes("deleteFailureMessage"), "删除失败没有说人话的映射函数");

function hasDeleteGuard(src: string): boolean {
  return src.includes(DELETE_GUARD) && src.includes("请输入完整课程名");
}
check(hasDeleteGuard(courseList), "删除确认缺少课程全名校验");
check(!hasDeleteGuard(courseList.replace(DELETE_GUARD, "disabled: false")),
  "变异自检失败：去掉课程名校验（直接可删）时应判红");

// ④ API / 后端 --------------------------------------------------------------
check(api.includes("deleteCourse: (id: string, confirm: string)"),
  "api.ts 没有 deleteCourse(id, confirm)");
check(api.includes("?confirm=${encodeURIComponent(confirm)}"),
  "deleteCourse 没有把课程全名作为 confirm 查询参数");
check(/deleteCourse[\s\S]{0,220}method:\s*"DELETE"/.test(api), "deleteCourse 没有用 DELETE");
check(route.includes("confirm: str | None = None"), "后端 DELETE 没有 confirm 查询参数");
check(route.includes("if confirm != course.title"), "后端没有逐字比对 confirm 与课程名");
check(/raise HTTPException\(\s*400/.test(route), "confirm 不符没有返回 400");
check(route.includes("delete_course_with_artifacts"), "路由没有复用删除服务");
check(service.includes('f"{course.id}_*"'), "课件目录没有按课程 id 前缀清理");
check(service.includes('f"{course.id}*"'), "导出目录没有按课程 id 前缀清理");
check(service.includes("manifest.json") && service.includes('(data.get("course") or {}).get("id")'),
  "交付包没有按 manifest 里的 course.id 认领");
check(service.includes("_remaining_references") && service.includes("protected"),
  "没有「仍被其他课程引用则保留」的保护");
check(!/glob\([^)]*course\.title/.test(service), "清理逻辑出现按课名模糊匹配");

check(verify.includes("scripts/check-task158.ts"), "verify.ps1 没有接入任务 158 断言");

if (failures.length) {
  console.error(`任务 158 前端断言失败（${failures.length} 项）：`);
  for (const item of failures) console.error(`  - ${item}`);
  process.exit(1);
}
console.log(`任务 158 前端断言通过：受众候选 ${options.length} 个，共享组件 2 处，删除强确认 + 后端保护齐备。`);
