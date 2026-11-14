# 课程生成器（CourseGenie）· WorkBuddy 连接器

把 PDF / PPT / 视频 / 音频 / 文本做成一门完整课程，并一键写进你**本机**的 Obsidian 笔记库 —— 全程在 WorkBuddy 对话里完成。

**开箱即用**：连接器默认连我们托管的课程生成服务，装上就能用；两项配置都可以不改。

---

## 1. 安装与配置

### 1.1 前置条件

| 需要 | 说明 |
|---|---|
| **WorkBuddy 客户端** ≥ 4.24.0 | 见 `connector-meta.json` 的 `minWorkbuddyVersion` |
| **本机有 `uv`** | `pip install uv` 或见 <https://docs.astral.sh/uv/>。WorkBuddy 用 `uvx` 拉起本连接器（托管运行时目前只支持 Node，Python 侧需要 uv） |
| ~~自建后端~~ | **不需要**。默认连我们托管的服务；只有你想自己部署时才需要 |
| ~~DeepSeek API Key~~ | **可选**。课程生成用的是服务端的模型凭据；Key 只用于连接器开工时做一次只读凭据自检 |

### 1.2 安装连接器

把本目录（`connector/`）作为一个连接器包装入 WorkBuddy，或按 `mcp.json` 手动登记：

```json
{
  "mcpServers": {
    "coursegenie": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "--from",
        "coursegenie-mcp==1.0.5",
        "coursegenie-mcp"
      ],
      "env": {
        "DEEPSEEK_API_KEY": "${DEEPSEEK_API_KEY}",
        "COURSE_BACKEND_URL": "${COURSE_BACKEND_URL}"
      },
      "timeout": 30000
    }
  }
}
```

程序本体从 PyPI 安装，走 HTTPS 且版本号钉死（`==1.0.5`），不在启动时下载未校验的二进制。
演示后端 `http://127.0.0.1:8000` 只是数据接口（无域名与证书），不下载、不执行任何代码。

token 模式（`auth_mode: "token"`）会弹表单，**两项都可以不改**：

| 表单字段 | 类型 | 必填 | 默认值 |
|---|---|---|---|
| `COURSE_BACKEND_URL` | text | 否 | `http://127.0.0.1:8000`（托管演示服务） |
| `DEEPSEEK_API_KEY` | **password** | 否 | —（留空即跳过自检） |

`${DEEPSEEK_API_KEY}` / `${COURSE_BACKEND_URL}` 与 `token-schema.json` 的 `fields[].key` **逐字对应**；
凭证只存本机 `~/.workbuddy`，不经过云端。

### 1.3 想连自己部署的后端

把「课程生成服务地址」改成你的地址即可（如 `http://127.0.0.1:8000`，`/api` 可省略）：

```bash
cd backend
.venv/Scripts/python -m uvicorn app.main:app --port 8000
```

```bash
# 本地开发：不走 uvx
python -m venv .venv
.venv/Scripts/pip install "mcp[cli]>=1.2,<2" httpx
set COURSE_BACKEND_URL=http://127.0.0.1:8000
.venv/Scripts/python -m course_generator          # 直接跑 stdio 服务
mcp dev course_generator/server.py                # 用 MCP Inspector 调试
```

---

## 2. 九个工具与调用顺序

| # | 工具 | 作用 | 是否阻塞（实测） |
|---|---|---|---|
| 0 | `check_backend_status` | 服务在线？只读模式？凭据有效？（Key 留空则跳过凭据自检） | 秒回（1.4–3.5 s） |
| 1 | `upload_source` | 上传一份资料（可自动新建课程），返回 `task_id` | 秒回（1–8 s） |
| 2 | `generate_outline` | 依资料生成大纲，返回章节 + 知识点 | 最多等约 18 s，超时转后台（真模型实测 20–45 s） |
| 3 | `confirm_outline` | 用户批准 / 退回大纲 | 秒回 |
| 4 | `generate_sample` | 生成第 N 个知识点的样课，并**自动设为「样板」** | 最多等约 18 s，超时转 job（真模型实测约 25–60 s） |
| 5 | `generate_all` | 批量生成整门课讲义（异步） | **立即返回** `task_id` + `job_id` |
| 6 | `generate_concepts` | 抽取概念库 —— 笔记里**概念双链的来源** | 最多等约 16 s，超时转 job |
| 7 | `export_to_obsidian` | 生成笔记并**打包下载到你本机** `vault_path/<课程名>/` | **立即返回**，后台下载解压 |
| 8 | `check_task_status` | 课程阶段 + 后端 job + 本地长任务状态 | 秒回 |

标准链路（**两个 ⏸ 是硬闸门，AI 不许自己跳过**）：

```
upload_source → generate_outline → ⏸用户批准 → confirm_outline
  → generate_sample → ⏸用户确认样式 → generate_all → ⏸用户确认成品
  → generate_concepts → export_to_obsidian → check_task_status
```

**30 秒约束**：所有工具都在 25 秒预算内返回（内部最多等 18 秒）。批量生成、概念抽取、笔记导出是天然长任务，一律「立即返回 + 轮询」。
真模型实测：3 章 9 个知识点的课程，大纲约 45 秒、整门课批量 8–10 分钟。

---

## 3. 数据流向与凭据存储（重要，请照实理解）

| 数据 | 去了哪 | 会不会留存 |
|---|---|---|
| 你上传的资料文件 | 上传到**课程生成服务**（默认是我们托管在腾讯云的演示服务，**当前为 HTTP**；自建后端则只到你的机器） | 保存在该服务的 `storage/` 与数据库，用于本门课生成；**启用演示模式（`DEMO_ONLY`）的实例会在 24 小时后自动清理**；连接器本身不留副本 |
| 资料文本（生成课程时） | 由服务端发给它配置的模型服务商（当前 **DeepSeek 官方 API**） | 留存与否取决于该服务商的策略；涉密资料请先确认 |
| 课程产物（讲义/插图/习题/笔记） | 笔记**打包下载到你本机** `vault_path/<课程名>/`；其余产物保存在服务端供预览 | 你自己的机器上 |
| `DEEPSEEK_API_KEY` | **可选**。填了才用，且只用于连接器向 DeepSeek 的**只读**接口做凭据自检 | 仅保存在本机 `~/.workbuddy`，不打印、不写日志、不落盘 |
| `COURSE_BACKEND_URL` | 连接器用它拼 REST 请求 | 同上 |
| 生成请求 | 发到课程生成服务 | 启用 `DEMO_ONLY` 的实例限流：每个 IP 每分钟 6 次生成、每天 5 门课、同时 1 个生成任务；超限返回 **429 + 人话** |

日志里只记「方法 + 路径 + 状态码 + 耗时」，**任何凭据、请求体、响应体都不会进日志**。

> 自建后端并开了 `AUTH_ENABLED=true` 时，用环境变量 `COURSE_BACKEND_API_KEY` 传 API Key（本连接器支持，但不放进表单，避免与占位符的对应关系混淆）。

> **关于加密**：默认服务地址是固定的 IP + 端口，**当前走 HTTP**（还没有域名和证书，所以没有 HTTPS）。
> 作为演示环境可以接受；正式对客户前会换成域名 + HTTPS。资料内容会经过公网明文传输，涉密材料请先把服务地址改成你自己的后端。
---

## 4. 故障排查

| 报错 | 原因 | 处理 |
|---|---|---|
| 连不上课程生成后端 | 服务地址写错 / 托管服务不可达 / 自建后端没起 | 核对 `COURSE_BACKEND_URL`；自建时确认 uvicorn 在跑 |
| HTTP 401 | 后端开了鉴权 | 传 `COURSE_BACKEND_API_KEY`，或后端设 `AUTH_ENABLED=false` |
| HTTP 403 | 后端是只读演示模式 | 关掉 `DEMO_READONLY` 后重启 |
| HTTP 409 | 跳步了，或已有任务在跑 | 按提示补上一步 / 等任务结束 |
| HTTP 413 | 文件超过 `MAX_UPLOAD_MB`（默认 50MB） | 压缩、拆分，或调高后端配置 |
| HTTP 429 | 触发了演示服务配额（每分钟生成次数 / 每天课程数 / 同时只跑 1 个任务） | 按 `detail` 里的人话等一会儿再试；自建后端可调高配额或关掉 `DEMO_ONLY` |
| HTTP 422 | 必填参数不合法（如 `goal` 少于 3 字） | 补全参数 |
| HTTP 503 | 后端模型凭据没配好 / 模型返回空或非 JSON | 看 `detail` 里那句人话；自建时配 `LLM_PROVIDER` + Key |
| 「下载笔记包失败」 | 笔记还在生成，或静态目录不可达 | 等几秒重试；持续失败看后端日志 |
| 「这个目录不存在」 | `vault_path` 指向的本地目录没建 | 先创建该文件夹 |

---

## 5. 目录结构（严格按官方规范）

```
connector/
├── connector-meta.json           # 元信息（name/name_zh/name_en、description 三件套、source、type、auth_mode、version、minWorkbuddyVersion、examples_zh/_en）
├── mcp.json                      # MCP 启动配置（stdio + uvx --from 发行包 + ${VAR} + timeout）
├── token-schema.json             # 凭据表单（官方 fields[] 格式，密码字段 type=password）
├── icon.svg                      # 市场图标（矢量、透明背景）
├── skills/
│   └── coursegenie/
│       └── SKILL.md              # 技能定义（YAML frontmatter + 正文）
├── README.md                     # 本文件
├── pyproject.toml                # 包定义（coursegenie-mcp）
└── course_generator/
    ├── __init__.py
    ├── __main__.py               # 入口（python -m / uvx 一致）
    └── server.py                 # FastMCP 服务 + 九个工具 + HTTP 桥接
```

---

## 6. 安全边界

- 凭据只从 `os.environ` 读；未填 Key 时**不报错**、只跳过自检。
- 无任何硬编码密钥、密码或机器特定路径；默认地址是公开的托管服务地址，可在表单里改。
- 一切后端异常翻译成中文 + 下一步建议，不把栈抛给 AI。
- 下载的笔记包解压时**拒绝路径穿越**（绝对路径 / `..` 一律跳过），并拒绝软链、加密包、Windows 非法文件名与 zip 炸弹；下载体积同样有上限。
- 长任务在本地线程池执行，状态落 `%TEMP%/coursegenie-mcp/tasks.json`（只存任务 id 与结果摘要，不含凭据与资料正文）。

---

## 版本

- **1.0.2**（2026-10-20）：默认指向托管服务、DeepSeek Key 改为可选；`export_to_obsidian` 改为**下载到本机**（vault 路径真实落地）。
- 1.0.0：首个提交审核版本。
