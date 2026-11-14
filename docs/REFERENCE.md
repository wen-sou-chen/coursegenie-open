# 参考手册（从 README 首页搬出来的细节）

首页只讲「这是什么、怎么上手、有什么边界」。这一份放实现细节：配置全表、课件版式、接口清单、分享机制、路线图。
内容与首页同源，改的时候两边一起看。

---

## 1. 关键配置（`backend/.env`）

| 变量 | 说明 |
| --- | --- |
| `LLM_PROVIDER` | openai / anthropic / openrouter / mock（默认 mock，无密钥可跑） |
| `OPENROUTER_API_KEY` / `OPENROUTER_MODEL` | 一个 key 路由多模型（建议低成本模型如 `deepseek/deepseek-chat`） |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | 真实生成时填写 |
| `OPENAI_BASE_URL` / `OPENAI_MODEL` | OpenAI 兼容端点与模型；DeepSeek 官方直连见下节 |
| `OPENAI_DISABLE_THINKING` | 默认 `false`；DeepSeek 官方直连建议设 `true`，关闭 reasoning 以保留输出预算 |
| `IMAGE_PROVIDER` | openai / stability / mock |
| `OBSIDIAN_VAULT_ROOT` | 导出笔记系统的目标根目录（默认 `backend/storage/kb`；课程可单独覆盖） |
| `AUTH_ENABLED` | true 时接口需 `X-API-Key`；开启后必须设置非默认 `DEV_API_KEY` 否则拒绝启动 |
| `DEV_API_KEY` | 开发 Key；开启认证时改为随机长字符串（默认值仅供零配置演示，不可用于开启认证） |
| `DASHI_PPT_PROJECT_ROOT` | dashi-ppt-runtime 项目路径；不配置走 python-pptx 兜底 |
| `PPT_PREVIEW_PDF` | 默认 `false`；设 `true` 且装好 PowerPoint（+ `pywin32`）时，`POST /ppt/{item_id}/preview` 用 COM 导 PDF 供网页内嵌，失败显式回退页级大纲 |
| `PPT_SLIDE_IMAGES` | 默认 `true`；用 PowerPoint COM 把每页导成 `storage/ppt/<课件目录>/slides/slide-NN.png` 并写进 `meta.slides[].image`；探测不到 PowerPoint 时显式回退文字大纲并记 `meta.images_error` |
| `PPT_THEME` | 课件主题的**兜底**（本课没设置时才用它）：`clean` 清简教学（默认）/ `professional` 专业培训 / `handdrawn` 手绘亲和 / `modern` 现代简约 / `navy` 深蓝商务 |
| `PPT_SHOW_FOOTER` | 默认 `true`；页脚 `课程名 · 章号 章名 · P页码` |
| `PPT_WATERMARK` | 默认空；填「试看版·请勿外传」这类文字即为整页旋转水印 |
| `PPT_INCLUDE_QUESTIONS` | 默认 `true`；每章课件附 1 页课堂提问（参考答案写在演讲备注） |
| `PPT_COVER_AUTHOR` | 封面主讲人 / 机构；留空则封面只显示日期 |
| `YTDLP_EXE` / `YTDLP_CMD` | 「从链接导入」（YouTube）用哪个 yt-dlp；可选依赖，缺了只影响这一个入口 |
| `YTDLP_COOKIES` | cookies.txt 的**路径**（必须是 `.youtube.com` 域）。只把路径交给 yt-dlp：不复制、不进日志、不进接口返回、不写进库 |
| `SERVE_FRONTEND` / `FRONTEND_DIST_DIR` | 是否由后端托管前端产物 / 产物在别处时的路径 |

**DeepSeek 官方直连（OpenAI 兼容协议）**：

```dotenv
LLM_PROVIDER=openai
OPENAI_BASE_URL=https://api.deepseek.com
OPENAI_MODEL=deepseek-v4-pro
OPENAI_API_KEY=<你的 DeepSeek Key>
OPENAI_DISABLE_THINKING=true
```

DeepSeek 官方推理模型的 `reasoning_content` 也计入 `max_tokens`，长讲义可能把预算吃光并返回空 `content`。
`OPENAI_DISABLE_THINKING=true` 会在请求体带 `thinking: {"type":"disabled"}`；没关推理时，后端遇到空 `content`
会给出含 `finish_reason` / token 用量的可读错误，不误报成 JSON 解析失败。

## 2. 课件（PPT）版式与主题

- 五套内置主题：**清简教学**（白底大留白，靛蓝强调）、**专业培训**（深色标题条）、**手绘亲和**（暖色纸感）、**现代简约**（深墨字 + 高饱和强调）、**深蓝商务**（深蓝页眉带 + 白字标题）；同一套色板同时驱动正文、原生图表与概念图 PNG。
- 版式族：封面 / 课程目标 / 目录 / 章节页 / 知识点页 / 纯图页 / 章节回顾页 / 随堂练习页 / 课堂提问页 / 结束页；图注统一「图 N · 图题」。
- 版式质量：标题 ≤24 字、要点 ≤4 条且每条 ≤22 字，超出自动精简；生成后逐页几何自检（文本∩文本 = 0、文本越界 = 0、图片出页 = 0），不通过自动降级并把原因写进 `meta.layout_notes`（不静默）。
- 教师实用化：每页备注是 150~300 字、可直接照念的口播稿；练习页答案与解析只在备注。
- **主题跟随课程**：优先级唯一口径为「调用方显式传的主题（非空）> 本课设置 `course.meta.ppt_theme` > `PPT_THEME` 配置 > 内置默认」；非法值 4xx 不静默回落。主题只改外观；**改了要重新生成课件才生效**。

## 3. 接口一览（前缀 `/api`）

- `POST /material/upload` `POST /material/paste` `POST /material/import-link` `POST /material/{material_id}/import-link` `GET /material/{course_id}` `DELETE /material/{material_id}`
- `GET /kb/vault` `GET /kb/course/{id}` `POST /kb/course/{id}/bind` `POST /kb/course/{id}/export?force=`
- `POST /course/` `GET /course/` `GET/PATCH /course/{id}`
  `DELETE /course/{id}?confirm=<课程全名>`（confirm 不符 400；删除会清理该课 storage 产物）
- `POST /plan/generate` `POST /plan/approve` `GET /plan/{course_id}` `PATCH /plan/node/{node_id}`
- `POST /content/generate`（异步）`GET /content/{course_id}` `GET /content/jobs/{job_id}` `PATCH|DELETE /content/{item_id}`
- `POST /image/generate|import` `POST /flowchart/generate|import`
- `POST /quiz/generate` `PATCH|DELETE /quiz/{quiz_id}`
- `POST /ppt/generate`（异步，可带 item_id 重做）`POST /ppt/import` `GET /ppt/{course_id}` `GET /ppt/{item_id}/audit` `POST /ppt/{item_id}/preview` `GET /ppt/themes` `POST /ppt/theme`
- `POST /version/{course_id}/snapshot` `GET /version/{course_id}` `POST /version/{course_id}/restore/{version_id}`
- `POST /publish/export`（html/scorm）`POST /publish/`（teachable 适配器）`GET /analytics/{course_id}`
- `POST /share/course/{id}` `GET /share/course/{id}` `POST /share/{link_id}/revoke` `DELETE /share/{link_id}`（只读分享）
- 公开侧：`GET /public/{token}` `/plan` `/content` `/ppt` `/images/…` `/ppt-files/…`

## 4. 只读分享机制

1. 课程工作台 → 右上「高级工具」→「分享」→ 填备注 / 有效期天数 / 可选密码 → 「创建链接」；
2. **明文 token 只在创建时显示一次**（后端只存 sha256 哈希），当场复制保存；
3. 客户打开 `http://<你的地址>/share/<token>`：顶部是「只读分享」横幅，正文是第 4 步预览同款呈现，没有任何编辑/生成/删除按钮；
4. 面板里能看到访问次数与最近访问，随时撤销或删除记录。

状态码约定：token 无效 → `403`；已撤销/已过期 → `410`；密码缺失或错误 → `401`；公开侧任何写方法 → `405`。
范围隔离：一个 token 只能读它绑定的那门课（带 `?course_id=<别的课>` 直接 `403`）。

## 5. 路线图

- **P0（已交付）**：课程管理、素材上传提取、大纲（含依赖图谱）、文本/习题、图片/流程图、PPT、导出、自动笔记系统（防覆盖）、统一编辑/重做/导入、版本、分析、测试。
- **P1**：素材按知识点粒度自动分拣、真实图像生成、素材库（MinIO 接口）、模板库、笔记回写（双向一致性设计）。
- **P2**：互动设计器、SCORM/xAPI 完整导出、Moodle/Canvas 适配器。
- **P3**：LRS 学习行为分析、课程内反馈、A/B 测试。

## 6. 其他注意

- 课件导出到 `课程/<课名>/课件/`（整课 `00-整课课件.pptx`、每章 `第N章-…课件.pptx`），概览页与章节页用 `[[…]]` 双链引用。
- 导出到有版本管理的 vault 前建议先纳入 Git / Obsidian 同步；生成文件与教师手动修改互不覆盖。
- **安全边界**：当前为单机 MVP，无多用户租户隔离 —— 认证开启后任一有效 `X-API-Key` 仍可访问全部课程；默认 `AUTH_ENABLED=false` 时请把后端绑定在 `127.0.0.1`。
- 只读分享链接默认只服务本机/内网；token 泄露等于课程内容泄露，外网访问必须加反代与 TLS。
- Mock 模式产出为占位内容，仅用于流程验证；接真实 LLM Key 后质量显著提升。
