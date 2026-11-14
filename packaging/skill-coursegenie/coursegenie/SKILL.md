---
name: coursegenie
display_name: 课程生成器
display_name_en: CourseGenie Course Builder
description: "把用户手上的 PDF/Word/PPT/文本资料做成一门完整课程：上传资料 → 生成大纲（等用户批准）→ 生成样课确认插图风格 → 批量生成 → 生成概念库 → 导出笔记或交付包。当用户提到「做课」「生成课程」「把资料做成课程」「出讲义/教案/学案」时使用。"
description_zh: "把资料变成一门完整课程：大纲、讲义、教案、学案、习题、课件与配图，并导出成可直接交付的文件。"
description_en: "Turns PDF/Word/PPT/text materials into a complete course: outline, handouts, teaching plans, student sheets, quizzes, slides and figures, exported as a deliverable package."
examples_zh:
  - "我有一份 12 页的 PDF，帮我做成 4 章 8 节的课，先给大纲"
  - "大纲没问题，生成第一节样课看看插图风格"
  - "样课可以，按这个风格把整门课生成出来"
examples_en:
  - "Turn this 12-page PDF into a 4-chapter, 8-lesson course and show me the outline first"
  - "The outline looks fine — render the first lesson as a sample so I can check the figures"
  - "The sample works; generate the whole course in that style"
version: 1.0.0
author: "CourseGenie"
---

# 课程生成器（CourseGenie）

这个技能把「资料 → 课程」交给课程生成服务去做。**你不写课程内容**，只负责按顺序调脚本、在两个必须点头的地方停下来、把结果讲清楚。

所有操作都通过技能目录下的 `scripts/cg.py` 执行（Python 3.10+，只用标准库）：

```bash
python scripts/cg.py <命令> [参数]
```

默认连托管服务 `https://demo.example.com`；用户自建后端时加 `--base http://127.0.0.1:8000`。

## 一、什么时候激活

出现下列说法就激活：

- 「做课 / 生成课程 / 做一门课 / 把资料变成课 / 出讲义 / 出教案 / 出学案 / 出习题」
- 「上传这份 PDF / PPT / Word，帮我做课」
- 「大纲 / 样课 / 插图风格 / 批量生成 / 导出笔记」

**不要**激活：只是总结一篇文档、写一份普通笔记、问工具怎么装。

## 二、开工自检

```bash
python scripts/cg.py health
```

看三件事：`ok`、`demo_only`、`demo_readonly`。`demo_readonly=true` 时一切写操作都会被拒，先用一句人话告诉用户，不要继续。

## 三、命令表

| 命令 | 作用 | 关键参数 |
|---|---|---|
| `health` | 服务自检 | — |
| `create` | 新建课程 | `--title` 必填；`--audience`、`--subject`、`--minutes` |
| `upload` | 上传一份资料 | `--course-id`、`--file`（本机绝对路径） |
| `outline` | 生成大纲（同步等待） | `--course-id`；`--goal`、`--chapters`、`--audience`、`--style` |
| `approve` | 批准 / 退回大纲 | `--course-id`；退回加 `--reject` |
| `sample` | 生成第 N 个知识点的样课 | `--course-id`；`--index`、`--tone`、`--figure-style` |
| `all` | 批量生成整门课 | `--course-id`、`--sample-approved` |
| `concepts` | 抽取概念库（决定笔记双链） | `--course-id` |
| `status` | 查课程 / 大纲 / 生成进度 | `--course-id`；`--job-id` |
| `export` | 导出笔记包到本机目录 | `--course-id`、`--vault`（已存在的目录） |

## 四、铁律：严格按这个顺序

```
create → upload（可多次）→ outline → ⏸ 用户批准 → approve
  → sample → ⏸ 用户确认插图样式 → all → ⏸ 用户确认成品
  → concepts → export → status（确认写盘）
```

- 没上传资料就生成大纲、大纲没通过就生成样课，后端会返回 409 / 400；不要反复重试。
- **两个 ⏸ 是硬闸门**，见第五节。
- 每次只推进一步，把这一步的产出讲给用户，再问要不要继续。
- 用户明确说不需要概念双链时，可以跳过 `concepts`。

## 五、两个闸门

### 闸门 1：大纲

`outline` 返回后，把章名、知识点数量念给用户，问「按这个生成？」。同意才 `approve`；要改就 `approve --reject`，改完重新 `outline`。

「你看着办」不算批准：至少复述一遍大纲，得到明确同意再往下。

### 闸门 2：样课与插图风格

`sample` 之后，把样课标题、正文结构、插图风格讲给用户，问「以后每节都照这个样式？」。

- 用户认可才 `all --sample-approved`；
- 想换风格就用 `sample --figure-style "…"` 再出一版，不要直接批量；
- 批量生成花的是服务端的模型额度，动手前说清「这一步会调用 N 次模型」。

### 收尾：导出

批量跑完先汇报「多少篇成功、有没有失败或跳过」，问清写进哪个目录，再 `export`。

`--vault` 必须是用户明确给出的、已存在的目录；不要自己编路径，也不要覆盖用户没提过的目录。

## 六、长任务

- `outline` 默认最多等 600 秒；超时会报错，不要立刻重试，先让用户缩小资料或章数。
- `sample` 会自己轮询到完成（默认 600 秒），返回 `job.status=success/failed`。
- `all` 立刻返回 `job_id`：用 `status --course-id … --job-id …` 轮询，`status=success` 才算完成。
- 实测参考：3 章 9 个知识点，大纲约 45 秒，整门批量 8–10 分钟，属正常。

汇报进度用人话：「已生成 5/9 节，还在跑」，不要贴 JSON。

## 七、当前能力边界（照实说）

- **支持**：PDF、Word（docx）、PPT（pptx）、纯文本 / 粘贴文字、图片。
- **可能不可用**：音频 / 视频转写、从网页链接导入（托管演示服务没装相应组件）。遇到就请用户给文字稿，或说明正式交付时单独处理。
- **课件预览**：演示服务的 PowerPoint 组件可能缺失，预览退化成「文字页级大纲」；PPTX 文件本身照常生成、可下载。
- 单文件默认 ≤50MB；托管演示服务同时只跑 1 个生成任务，数据 **24 小时后自动清理**。

## 八、出错怎么办

| 现象 | 你说什么 / 做什么 |
|---|---|
| 连不上服务 | 请用户确认后端地址；自建后端要确认已启动 |
| HTTP 401 | 后端开了鉴权：让用户提供 API Key（`COURSEGENIE_API_KEY` 或 `--api-key`） |
| HTTP 403 只读模式 | 服务方开了 `DEMO_READONLY`，要生成得关掉并重启 |
| HTTP 409 | 跳步了或已有任务在跑：补上一步，或等任务结束 |
| HTTP 413 | 文件超过 50MB：压缩或拆分 |
| 找不到文件 | `--file` 必须是本机绝对路径，且文件真实存在 |
| 503 | 服务端模型凭据没配好：换 `LLM_PROVIDER=mock` 先跑通流程 |

**不要把 Python 异常栈、traceback 原文贴给用户**；也不要说「已经修好了」这类没发生的事。

## 九、数据与隐私（用户问起照实说）

- 资料上传到课程生成服务（默认托管服务）；生成时服务端会把资料文本发给它配置的模型服务商。
- 演示服务 24 小时清理课程与资料；涉密资料先提醒用户，必要时改走自建后端。
- 这个技能不保存任何令牌；自建后端需要鉴权时只从环境变量或参数读。
- `export` 会写用户磁盘：覆盖同名文件前必须得到用户明确同意。

## 十、说话方式

- 先结论后细节，短句，务实；不用营销词，不许愿（「保证」「一定能」）。
- 数字给口径：「9 个知识点」比「很多节」强。
- 失败时说清三件事：缺什么、影响到哪一步、用户下一步能做什么。
