# 课程架构师（CourseGenie Expert）

WorkBuddy 专家包。把 PDF / Word / PPT / 文本资料变成一门完整课程，并导出交付包或 Obsidian 笔记库。

## 包结构

```
coursegenie-expert/
├── .codebuddy-plugin/plugin.json   # 专家配置（展示信息 + 依赖声明）
├── .mcp.json                       # 远程 MCP（Streamable HTTP）+ Token 表单
├── agents/course-architect.md      # Agent 定义（YAML frontmatter + 系统提示词）
├── avatars/expert.png              # 512×512 PNG
├── skills/coursegenie/SKILL.md     # 与连接器共用的技能说明
└── README.md
```

## 依赖

- 远程 MCP：`https://mcp.example.com/mcp`（Streamable HTTP）
- 鉴权：`Authorization: Bearer ${COURSEGENIE_MCP_TOKEN}`
- 令牌由服务方分配，用户在平台弹出的 Token 表单里填写；**包里不硬编码任何令牌**。

## 用户侧流程

1. 召唤专家 → 平台弹出「课程生成器」连接卡片。
2. 粘贴访问令牌 → 连接成功。
3. 上传资料 → 大纲 → 确认 → 样课 → 整门 → 导出。

## 打包

```powershell
cd C:\Users\wen\CourseGenie\packaging
Compress-Archive -Path expert-coursegenie -DestinationPath coursegenie-expert-1.0.0.zip -Force
```

上线前检查：

- `plugin.json` 的 `author.email` 必须是能收信的地址（当前填 `support@example.com`，未验证邮箱时先改）。
- `displayDescription.zh` 字数 40–50。
- 头像 512×512、≤500KB。
- `.mcp.json` 的 `url` 已随网关开通（`mcp.example.com`）。
- 包里搜不到真实令牌：`rg "Bearer [A-Za-z0-9_-]{32,}" .` 应无命中。
