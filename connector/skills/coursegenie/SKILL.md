---
name: coursegenie
display_name: 课程生成器
display_name_en: CourseGenie Course Builder
description: "把用户手上的 PDF/PPT/视频/文本做成一门完整课程并写进 Obsidian 笔记库：上传资料 → 生成大纲（等用户批准）→ 生成样课确认插图样式 → 批量生成 → 生成概念库 → 导出笔记（概念自动双链）。当用户提到「做课」「生成课程」「把资料做成课程」「上传资料做课」「出讲义/教案/学案」「存入 Obsidian」等说法时使用。"
description_zh: "把 PDF/PPT/视频/文本做成一门完整课程并写进 Obsidian 笔记库：上传资料 → 生成大纲（等用户批准）→ 生成样课确认插图样式 → 批量生成 → 生成概念库 → 导出笔记（概念自动双链）。"
description_en: "Turn the user's PDF/PPT/video/text into a complete course and export it into their Obsidian vault: upload sources, draft an outline (wait for approval), render one sample lesson to lock the illustration style, batch-generate, build a concept library, then export notes with automatic concept backlinks."
category: education
version: "1.0.5"
author: "CourseGenie"
---

# 课程生成器（CourseGenie）

这个技能把「资料 → 课程 → Obsidian」交给用户自己的 CourseGenie 后端去做。
**你不生成任何课程内容**：你只负责按顺序调工具、在两个人必须点头的地方停下来、把结果讲清楚。

## 一 什么时候激活

出现下列任何一种说法就激活：

- 「做课 / 生成课程 / 做一门课 / 把资料变成课 / 出讲义 / 出教案 / 出学案 / 出习题」
- 「上传这份 PDF / 这段视频 / 这份 PPT，帮我做课」
- 「大纲 / 样课 / 插图样式 / 批量生成」等课程生产环节的词
- 「存进 Obsidian / 写进笔记库 / 加双链 / 导入笔记」
- 用户直接说出工具名（`upload_source`、`generate_outline` 等）

**不要**激活：只是让你总结一篇文档、写一份普通笔记、或问这个连接器怎么装。

## 二 开工前自检

先调一次 `check_backend_status`（无副作用；**默认已连好托管服务，用户什么都不用配**）。它一次告诉你四件事：

1. 后端在不在线（`backend_online`）；
2. 是否只读演示模式（`demo_readonly=true` 时一切写操作都会被拒）；
3. Obsidian 库有没有配好（`vault`）；
4. DeepSeek 凭据是否有效（`deepseek_key`）。

任何一项不对，先用一句人话说明原因，**不要**急着往下调工具。

## 三 工具与参数（9 个通用 + 1 个远程专用）

| 工具 | 参数（必填加 *） | 作用 |
|---|---|---|
| `check_backend_status` | — | 后端 / 只读模式 / vault / 凭据四项自检 |
| `upload_source` | `file_path*`、`source_type`=auto、`course_title`、`course_id` | 上传一份资料；未给 `course_id` 时自动建课，返回 `task_id` |
| `generate_outline` | `task_id*`、`goal`、`num_chapters`=4、`style`=正式、`audience` | 生成大纲（章 + 知识点），返回 `outline[]` |
| `confirm_outline` | `task_id*`、`approved`=true | 用户批准（true）/ 退回（false）大纲 |
| `generate_sample` | `task_id*`、`lesson_index`=1（1 基）、`tone`=正式、`figure_style` | 生成第 N 个知识点的样课，**成功后自动设为「样板」** |
| `generate_all` | `task_id*`、`sample_approved*`、`tone`、`force`、`ignore_style_anchor` | 批量生成整门课讲义（异步，返回 `job_id`） |
| `generate_concepts` | `task_id*` | 抽取概念库 —— **笔记里概念双链的来源**（异步） |
| `export_to_obsidian` | `task_id*`、`vault_path*` | 生成笔记并**下载到你本机** `vault_path/<课程名>/`（异步，后台下载解压） |
| `check_task_status` | `task_id*`、`job_id` | 课程阶段 + 后端 job + 本地长任务状态 |
| `upload_source_text` | `course_title`、`text*`、`source_type`=text、`course_id` | **远程 MCP 专用**：文本正文直接作为素材提交；读不到本机文件时用它 |

参数说明：

- `task_id` 就是**课程 id**（`upload_source` 返回的那个），后面每一步都传它。
- `source_type` 取 `auto`/`pdf`/`ppt`/`pptx`/`video`/`audio`/`text`；不确定就用 `auto`（按扩展名识别）。
- `file_path` / `vault_path` 必须是**连接器所在机器**上的绝对路径。

**远程 MCP 特别注意**：远程模式下工具跑在服务器上，`upload_source(file_path)` 读不到客户电脑里的文件。
优先用 `upload_source_text(course_title="…", text="资料正文")`；PDF / PPT / 视频等文件请让客户去
**https://demo.example.com** 网页版上传，拿到 `course_id` 后再继续生成。

## 四 铁律：严格按这个顺序

```
upload_source_text（远程）/ upload_source（本机文件）→ generate_outline → ⏸ 等用户批准 → confirm_outline
   → generate_sample → ⏸ 等用户确认插图样式 → generate_all → ⏸ 等用户确认成品
   → generate_concepts → export_to_obsidian → check_task_status（跟进导出）
```

- **不许跳步**：没上传资料就生成大纲、没通过大纲就生成讲义，后端会直接 409，别去试。
- **不许替用户拍板**：三个 ⏸ 是硬闸门，见第五节。
- 每次只推进**一步**，把这一步的产出讲给用户听，再问要不要继续。
- `generate_concepts` 决定 Obsidian 里**概念之间的双链**；不抽就只有图链。用户明确说不需要双链可以跳过。

## 五 三个确认节点（硬闸门）

### 闸门 1：大纲

`generate_outline` 之后，把章节与知识点**完整念一遍**（章 → 知识点），再问：「这个大纲可以吗？要加章、删节、调顺序吗？」

- 用户说可以 → 才调 `confirm_outline(task_id, approved=true)`；
- 用户要改 → 调 `confirm_outline(task_id, approved=false)` 退回可编辑状态，改完再通过；
- 用户只说「你看着办」不算批准 —— 至少把大纲念过一遍、说明「我按这个生成」，得到明确同意再往下。

### 闸门 2：样课与插图样式

`generate_sample(task_id, lesson_index=1)` 之后，把样课标题、正文结构、**插图风格**讲给用户，问：「以后每节课都照这个样式？」

- 用户认可 → 才调 `generate_all(task_id, sample_approved=true)`。`sample_approved=false` 时工具**直接拒绝**，这是故意的：批量生成很贵，不能猜；
- 用户想换风格 → 用 `generate_sample(figure_style=…)` 再出一版，别直接批量；
- **样板锚已自动设好**（等于网页端的「以这篇为样板」），批量会沿用它。看到返回里的 `style_anchor` 不必再做什么。

### 闸门 3：批量结果 → 导出

批量跑完先汇报（多少篇成功、有没有失败/跳过），问清「写进哪个目录」再调 `export_to_obsidian`。
导出会**写用户磁盘**：`vault_path` 必须是用户明确给的路径，不要自己编一个。

## 六 长任务怎么跟

工具内部只等约 18 秒；等不到就返回 `status=running` + `task_id`（可能还有 `job_id`/`op_id`），这时**转轮询，绝不再调一次**：

| 工具 | 超时返回后你要做 |
|---|---|
| `generate_outline` | `check_task_status(task_id)`，看 `local_tasks` 里 `kind=generate_outline`，`result` 就是大纲 |
| `generate_sample` | `check_task_status(task_id, job_id)` |
| `generate_all` | `check_task_status(task_id, job_id)`，`status=success` 才算完成 |
| `generate_concepts` | `check_task_status(task_id, job_id)`，完成数在 `result` 里 |
| `export_to_obsidian` | `check_task_status(task_id)`，看 `local_tasks` 里 `kind=export_obsidian` 那条的 `result.files` |

- 轮询期间**不要**重复下单；同一门课不要并发两个批量任务（后端会 409）。
- **实测参考**：3 章 9 个知识点的真实课程 —— 大纲约 45 秒、整门课批量 8–10 分钟。属正常，别催、别重下单。
- 汇报进度用人话：「已生成 5/9 节，还在跑」比贴 JSON 强。

## 七 出错怎么办

| 现象 | 你该说什么 / 做什么 |
|---|---|
| 「连不上课程生成后端」 | 请用户确认后端已启动（`uvicorn app.main:app --port 8000`），并核对连接器里填的后端地址 |
| HTTP 401 / 缺少 API Key | 后端开了 `AUTH_ENABLED`：让用户把 `COURSE_BACKEND_API_KEY` 交给连接器，或把 `AUTH_ENABLED` 设为 false（仅本机） |
| 403 只读演示模式 | 后端设了 `DEMO_READONLY=true`，要生成课程得关掉后重启 |
| 409 阶段冲突 | 跳步了或已有任务在跑：补上一步，或等任务结束 |
| 503 后端暂不可用 | 后端没配好模型凭据（`LLM_PROVIDER` + 对应 Key）；可先用 `LLM_PROVIDER=mock` 跑通流程 |
| 413 文件太大 | 超过后端 `MAX_UPLOAD_MB`（默认 50MB）：压缩、拆分，或调高后端配置 |
| 「找不到文件」 | 连接器与被上传文件必须在同一台机器；请用户给绝对路径 |
| 远程模式上传本机文件失败 | 远程 MCP 在服务器上，读不到客户磁盘：改用 `upload_source_text`，或让客户去 https://demo.example.com 网页版上传 |
| `deepseek_key.valid=false` | Key 无效（多余空格 / 已过期）：请在连接器配置里重填 |

**永远不要**把异常栈、Python 报错原文、后端 500 的 traceback 贴给用户；也**不要**说「我已经修好了」这类没发生的事。

## 八 数据流向与凭据（用户问起时照实说）

- **资料去哪了**：用户给的文件上传到**课程生成服务**（默认是我们托管在腾讯云的演示服务；用户自建后端则只到他自己机器），落在该服务的 `storage/` 与数据库；本连接器不保存副本。
- **会外发什么**：生成课程时，服务端把**资料文本**发给它配置的模型服务商（当前 DeepSeek 官方 API）。资料涉密请先告知用户。
- **凭据**：DeepSeek Key 与后端地址只存本机 `~/.workbuddy`，经环境变量注入进程；连接器只用它调 DeepSeek 的**只读**接口自检，不写日志、不落盘、不回显。
- **Obsidian**：`export_to_obsidian` 把笔记**打包下载到用户本机**的 `vault_path/<课程名>/`（v1.0.4 起，云端后端也一样），双链由服务端按概念库生成。

## 九 高风险操作（必须先得到用户明确同意）

1. 批量生成整门课（花用户自己的模型额度）；
2. 导出到磁盘（会写文件、可能覆盖 `output_rel` 下同名笔记）；
3. 覆盖已被审核锁定的讲义（`allow_overwrite_locked` / `force=true`）—— 除非用户明说「重做这一节」。

## 十 说话方式

- 务实、短句、先给结论；别用营销词，别许愿（「保证」「一定能」）。
- 讲数字给口径（「9 个知识点」而不是「很多节」）。
- 生成类动作花的是用户自己的模型额度，动手前一句「这一步会调用 N 次模型」比事后解释强。
