# tools/ops —— 这个项目的运维跑道（脚本进仓库，不再放临时目录）

这些事原来都靠临时目录里的一次性脚本或人肉记忆：换台机器就没了，也没人知道"上一次是怎么上去的"。
现在它们进仓库，任何一个能被 `py -3` 跑起来的解释器都能复现同样的动作。

| 脚本 | 回答什么问题 | 会不会写东西 |
|---|---|---|
| `connector_check.py` | **这个连接器包能不能提交？** 17 项机械自检（包结构 / 版本 / 安装源 / 凭据 / 工具预算 / zip 条目） | 不写 |
| `artifact_check.py` | **这份提交包还是不是我们交上去的那一份？** 版本号四处一致、安装源、token-schema 结构、工具数量、zip 指纹 | 只在你显式 `snapshot` 时写一个 JSON 基线 |
| `review_pack.py` | **这次提审该填什么说明？** 从仓库读版本/地址，从服务读实时限制，生成可直接粘贴的一段 | 写一个 md 到 `build/`（已 gitignore） |
| `deploy_demo.py` | **怎么把前端/后端放上演示机？** 默认只演练，`--yes` 才执行；`.env` 自动备份还原 | 只在 `--yes` 时动服务器 |
| `profile_guard.py` | **个人档案里有没有漏进个人信息？** 身份证 / 银行卡 / 手机号 / 邮箱 / 三类结构化地址 / 明文口令 | 不写（只报文件与行号，不打印原文） |
| `profile_build.py` | **档案该加什么、是不是过期了？** `scan` 抓带 `#档案/*` 标签的候选；`check` 体检段落 / 篇幅 / 占位 / 新鲜度 | `scan` 写 `build/profile-candidates.md`（已 gitignore），`check` 不写 |

## 常用命令

```powershell
# 0) 连接器包 17 项自检（提交前必跑；有 FAIL 退出码 1）
py -3 tools/ops/connector_check.py

# 1) 提交前 / 每次改动后：体检（--strict 表示 WARN 也算失败）
py -3 tools/ops/artifact_check.py check --strict

# 2) 确认演示服务当前状态（联网）
py -3 tools/ops/artifact_check.py check --health

# 3) 冻结基线（只在「确认这份就是要提交的那份」时跑一次）
py -3 tools/ops/artifact_check.py snapshot

# 4) 生成审核提交说明（粘贴块可单独打印）
py -3 tools/ops/review_pack.py --health
py -3 tools/ops/review_pack.py --print --health

# 5) 部署（不加 --yes 只演练）
py -3 tools/ops/deploy_demo.py --target backend  --host <IP>
py -3 tools/ops/deploy_demo.py --target backend  --host <IP> --yes

# 6) 个人档案：抓候选 / 体检（本地来源见 profile/.source 与 profile/.inbox.md，都不进仓库）
py -3 tools/ops/profile_build.py scan --print
py -3 tools/ops/profile_build.py check --strict
py -3 tools/ops/profile_guard.py
```

## 铁律

1. **审核期间不许改连接器**：端口、URL、wheel 文件名、版本号、工具数量。`artifact_check.py` 就是为了让你在改之前先看见后果。
2. `fingerprint.json` 是**基线**，不是配置：改了它等于宣布"我重新冻结了"，改之前先确认你真的要出新版本。
3. 部署脚本默认演练；`--yes` 是唯一一道闸，不要在脚本里给它加默认值。
4. 密钥只走 SSH 私钥 + 环境变量；**脚本不接受密码参数**（密码会落进命令行历史）。

## 依赖

- `artifact_check.py` / `review_pack.py`：只用标准库（Python 3.10+）；
- `deploy_demo.py`：需要 `paramiko`（`pip install paramiko`），建议用项目里已经装了它的虚拟环境跑。

## 怎么知道它真的在守着

做过三次注入试验，每次都是「注入 ⇒ 红灯 ⇒ 字节级还原 ⇒ 回绿」：

1. 把 `connector-meta.json` 的 `version` 改成 1.0.5 ⇒ `① 版本号一致` **FAIL**；
2. 把 `server.py` 里的 `@mcp.tool` 改名 ⇒ `⑤ 工具数量` **FAIL**；
3. 把 `coursegenie-connector-1.0.4.zip` 暂时移走（模拟误删）⇒ `⑥ 提交包（缺件）` **WARN**，
   且 `--strict` 下**退出码 1**。

还原后三次都回到 `PASS 6 / WARN 0 / FAIL 0`、`--strict` 退出码 0，且被改文件的 sha256 与注入前逐字节相同。

> 安装源（②）现在必须是 `coursegenie-mcp==X.Y.Z`（PyPI，HTTPS 拉取）。`http://` 直链会被判 FAIL——
> 启动时明文下载可执行文件是审核驳回项，不再是可接受的取舍。
