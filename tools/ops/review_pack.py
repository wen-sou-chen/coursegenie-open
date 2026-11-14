"""生成「审核提交说明」：把当前提交物指纹 + 演示地址 + 三步验证路径写成可直接粘贴的一段。

为什么要有它
------------
审核提交时要填的说明，每次手写都要重新对一遍：版本号、安装源、演示地址、演示实例的限制数字。
数字对不上，审核员一试就发现"说明和实际不一致"。这个脚本把这些**全部从仓库里读**（版本号、
安装源、默认地址、演示服务实时状态），不允许手抄。

用法
----
    py -3 tools/ops/review_pack.py --out build/review/审核提交说明-今天.md
    py -3 tools/ops/review_pack.py --health          # 顺带把演示服务实时状态写进说明
    py -3 tools/ops/review_pack.py --print           # 只把「粘贴块」打到标准输出

安全：只读仓库文件；写一个 md（默认 build/ 下，已被 .gitignore）；不打印任何密钥。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifact_check as ac  # noqa: E402  同目录工具，复用同一份"读仓库"的逻辑


def health_snapshot(url: str) -> dict:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/health", timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"error": str(exc)}


def build_markdown(with_health: bool) -> str:
    fp = ac.fingerprint()
    schema = ac._token_schema()
    fields = [f for f in (schema.get("fields") or []) if isinstance(f, dict)]
    backend_url = next((str(f.get("defaultValue") or "") for f in fields
                        if f.get("key") == "COURSE_BACKEND_URL"), "")
    backend_url = backend_url.rstrip("/")
    install_source = fp["install_source"]
    version = fp["version"]
    today = date.today().isoformat()

    limits = {
        "retention_hours": "—", "max_generations_per_minute": "—",
        "max_courses_per_day": "—", "max_concurrent_jobs": "—",
    }
    capabilities = ""
    health_line = "（未探测；加 --health 可写入实时状态）"
    if with_health:
        data = health_snapshot(backend_url)
        if "error" in data:
            health_line = f"⚠️ 探测失败：{data['error']}"
        else:
            demo = data.get("demo") or {}
            limits = {k: demo.get(k, "—") for k in limits}
            caps = {i.get("key"): i.get("available") for i in (data.get("capabilities") or {}).get("items") or []}
            capabilities = "、".join(f"{k}={'可用' if v else '缺'}" for k, v in caps.items())
            health_line = (f"ok={data.get('ok')}；前端托管={(data.get('frontend') or {}).get('enabled')}；"
                           f"演示模式={demo.get('enabled')}")

    paste_block = "\n".join([
        f"演示地址：{backend_url} （网页版可直接点，无需账号）",
        "三步验证：新建课程 → 第 1 步点「AI 补充资料」后「下一步：生成大纲」→ 第 3 步「先生成一节样板」，",
        "在该讲义卡点「以这篇为样板」，再点「按章生成剩余全部」；第 5 步可导出 ZIP（含 PDF）。",
        f"连接器安装源：PyPI  {install_source}（由 uvx 自动安装，HTTPS 拉取）",
        f"演示实例限制：{limits['retention_hours']} 小时清理、每 IP 每分钟 {limits['max_generations_per_minute']} 次生成、"
        f"每天 {limits['max_courses_per_day']} 门课、同时 {limits['max_concurrent_jobs']} 个任务、注册登录关闭（403）。",
    ])

    zip_rows = "\n".join(f"| `{z['name']}` | {z['bytes']:,} B | `{z['sha256']}` |"
                         for z in fp["zips"]) or "| （仓库根目录暂无 zip） | — | — |"

    return f"""# 审核提交说明（{today} 自动生成）

> 由 `tools/ops/review_pack.py` 从仓库实际内容生成：版本号、安装源、默认地址都取自文件本身，
> 演示实例的限制数字取自服务实时状态。**不要手改数字**，要改就改源文件再重跑。

## 1. 一句话说明

CourseGenie 把「一个主题」变成「一门能交付的课」：生成课程大纲 → 讲义 / 概念图 / 数据图 / 流程图 / 习题 → 打包成含讲义、时长表、字幕、SCORM 的交付包。WorkBuddy 里通过 {fp['tool_count']} 个工具驱动同一个后端。

## 2. 提交物指纹（审核期间不许变）

| 项目 | 值 |
|---|---|
| 连接器版本 | **{version}** |
| 安装源（`mcp.json` 的 `--from`） | `{install_source}` |
| 默认服务地址 | `{backend_url}` |
| 工具数量 | {fp['tool_count']} 个 |

| 提交包 | 大小 | SHA256 |
|---|---|---|
{zip_rows}

## 3. 演示服务当前状态

{health_line}{('；能力：' + capabilities) if capabilities else ''}

| 限制项 | 值 |
|---|---|
| 课程/素材保留 | {limits['retention_hours']} 小时 |
| 每分钟生成次数（每 IP） | {limits['max_generations_per_minute']} |
| 每天课程数 | {limits['max_courses_per_day']} |
| 同时生成任务 | {limits['max_concurrent_jobs']} |

## 4. 审核员三步走（约 5 分钟）

1. **新建课程**：填标题、主题、受众、时长 → 保存。
2. **第 1 步准备素材**：上传文件 / 粘贴文本 / 从链接导入，或直接点「**AI 补充资料**」（没有素材也能跑）→「下一步：生成大纲」，约 20 秒出章节树 → 确认大纲。
3. **第 3 步做样板再批量**：点「**先生成一节样板**」→ 在该讲义卡点「**以这篇为样板**」→「**按章生成剩余全部**」→ 确认 → 逐章生成。第 4 步看覆盖检查，第 5 步「导出 ZIP」拿交付包。

## 5. 连接器侧验证

连接器安装后由 `uvx` 从 **PyPI** 取包（HTTPS，版本钉死 `{install_source}`）。环境变量只有两个：`DEEPSEEK_API_KEY`（可选，留空用演示后端自带额度）、`COURSE_BACKEND_URL`（默认已指向演示地址）。

在 WorkBuddy 里说一句即可跑通，例如：

> 「帮我建一门『零基础学做咖啡拉花』的课，受众是完全没基础的人，60 分钟，先出大纲，我确认后再写讲义，最后导出交付包。」

对应工具序列：`create_course` → `generate_outline` → `confirm_outline` → `generate_sample_lecture` → `set_style_anchor` → `generate_all_lectures` → `check_course` → `build_export`（→ `download_export`）。

## 6. 已知边界（如实说明，不是缺陷）

- 程序本体走 PyPI（HTTPS，版本钉死 `{install_source}`），启动时不下载任何未校验文件。
- 演示后端走 HTTP（暂无域名与证书），只承载接口数据，不下载、不执行代码。
- 单租户演示实例，**{limits['retention_hours']} 小时自动清理**，不适合存长期数据。
- 生成是真调大模型：一节样板约 20–60 秒，整门课按章数累加；面板有进度与「成本只做提示、不自动扣费」说明。
- 讲义里的图是 Excalidraw / SVG 矢量图，**不是** AI 生图（可编辑、可嵌进笔记库，刻意取舍）。

## 7. 提交时把这段贴到「备注 / 审核说明」

```
{paste_block}
```
"""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="生成审核提交说明")
    parser.add_argument("--out", default="", help="输出路径；默认 build/review/审核提交说明-<日期>.md")
    parser.add_argument("--health", action="store_true", help="把演示服务实时状态写进说明")
    parser.add_argument("--print", dest="only_print", action="store_true", help="只打印粘贴块")
    args = parser.parse_args(argv)

    md = build_markdown(args.health)
    if args.only_print:
        start = md.index("## 7. 提交时把这段贴到")
        block = md[start:].split("```")[1].strip()
        print(block)
        return 0

    out = Path(args.out) if args.out else Path("build") / "review" / f"审核提交说明-{date.today():%Y%m%d}.md"
    if not out.is_absolute():
        out = ac.REPO / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"已生成 {out}（{len(md)} 字符）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
