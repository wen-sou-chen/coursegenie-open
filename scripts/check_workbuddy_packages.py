"""按 WorkBuddy 开放平台文档体检专家包 / 技能包（不联网）。

用法：
    py -3 scripts/check_workbuddy_packages.py
退出码 0 = 全部通过。
"""

from __future__ import annotations

import json
import py_compile
import re
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EXPERT = REPO / "packaging" / "expert-coursegenie"
SKILL = REPO / "packaging" / "skill-coursegenie" / "coursegenie"

CATEGORIES = {
    "01-ProductDesign", "02-Engineering", "03-GameSpatial", "04-DataAI", "05-MarketingGrowth",
    "06-ContentCreative", "07-SalesCommerce", "08-FinanceInvestment", "09-OperationsHR",
    "10-ProjectQuality", "11-SecurityCompliance", "12-IndustryConsultant", "13-TencentZone",
    "14-WorldWise", "15-Education",
}
SECRET_PATTERNS = [
    re.compile(r"Bearer\s+[A-Za-z0-9_\-]{32,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
]


class Report:
    def __init__(self) -> None:
        self.failed = 0

    def ok(self, name: str, detail: str = "") -> None:
        print(f"[PASS] {name}" + (f" —— {detail}" if detail else ""))

    def fail(self, name: str, detail: str) -> None:
        self.failed += 1
        print(f"[FAIL] {name} —— {detail}")


def _frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    if not match:
        return {}
    data: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line and not line.lstrip().startswith("- "):
            key, value = line.split(":", 1)
            data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def _frontmatter_block(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    return match.group(1) if match else ""


def _png_size(path: Path) -> tuple[int, int]:
    raw = path.read_bytes()[:24]
    return struct.unpack(">II", raw[16:24])


def check_expert(report: Report) -> None:
    plugin_path = EXPERT / ".codebuddy-plugin" / "plugin.json"
    if not plugin_path.is_file():
        report.fail("专家 plugin.json", f"缺少 {plugin_path}")
        return
    plugin = json.loads(plugin_path.read_text(encoding="utf-8"))

    required = ["name", "version", "description", "author", "agents", "expertType", "agentName",
                "displayName", "profession", "displayDescription", "avatar", "categoryId", "plugin"]
    missing = [key for key in required if not plugin.get(key)]
    report.ok("专家必填字段", "齐全") if not missing else report.fail("专家必填字段", f"缺 {missing}")

    if plugin.get("expertType") != "agent":
        report.fail("expertType", f"应为 agent，实际 {plugin.get('expertType')}")
    else:
        report.ok("expertType", "agent")

    category = str(plugin.get("categoryId") or "")
    report.ok("行业分类", category) if category in CATEGORIES else report.fail("行业分类", f"不在列表：{category}")

    zh = str((plugin.get("displayDescription") or {}).get("zh") or "")
    if 40 <= len(zh) <= 50:
        report.ok("中文展示描述字数", f"{len(zh)} 字")
    else:
        report.fail("中文展示描述字数", f"{len(zh)} 字（要求 40–50）")

    for key in ("displayName", "profession", "displayDescription"):
        value = plugin.get(key) or {}
        if not value.get("zh") or not value.get("en"):
            report.fail(f"{key} 双语", "缺 zh 或 en")
        else:
            report.ok(f"{key} 双语", "zh + en")

    agent_name = str(plugin.get("agentName") or "")
    agent_file = EXPERT / "agents" / f"{agent_name}.md"
    if not agent_file.is_file():
        report.fail("Agent 定义文件", f"找不到 {agent_file}")
    else:
        front = _frontmatter(agent_file)
        if front.get("name") != agent_name:
            report.fail("Agent frontmatter", f"name={front.get('name')} ≠ agentName={agent_name}")
        elif (
            not front.get("description")
            or "\ndisplayName:" not in "\n" + _frontmatter_block(agent_file)
            or "\nprofession:" not in "\n" + _frontmatter_block(agent_file)
        ):
            report.fail("Agent frontmatter", "缺 description / displayName / profession")
        else:
            report.ok("Agent 定义文件", f"{agent_file.name}，frontmatter 齐全")

    avatar = EXPERT / str(plugin.get("avatar") or "")
    if not avatar.is_file():
        report.fail("头像", f"找不到 {avatar}")
    else:
        width, height = _png_size(avatar)
        size = avatar.stat().st_size
        if (width, height) != (512, 512):
            report.fail("头像尺寸", f"{width}×{height}（要求 512×512）")
        elif size > 500 * 1024:
            report.fail("头像大小", f"{size} B（要求 ≤500KB）")
        else:
            report.ok("头像", f"512×512，{size} B")

    mcp_path = EXPERT / ".mcp.json"
    if not mcp_path.is_file():
        report.fail("专家 MCP 声明", "缺少 .mcp.json")
    else:
        mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
        servers = mcp.get("mcpServers") or {}
        if not servers:
            report.fail("专家 MCP 声明", "mcpServers 为空")
        else:
            name, server = next(iter(servers.items()))
            url = str(server.get("url") or "")
            if not url.startswith("https://"):
                report.fail("远程 MCP 地址", f"不是 https：{url}")
            else:
                report.ok("远程 MCP 地址", url)
            auth = ((server.get("x-workbuddy") or {}).get("auth") or {})
            token_schema = auth.get("tokenSchema") or {}
            fields = token_schema.get("fields") or []
            if auth.get("type") != "token" or not fields:
                report.fail("Token 表单", "auth.type 不是 token 或 fields 为空")
            elif not all(field.get("key") in json.dumps(server.get("headers") or {}) for field in fields):
                report.fail("Token 表单", "fields.key 没有出现在 headers 占位里")
            else:
                report.ok("Token 表单", f"{len(fields)} 个字段，已接入 headers")
            if "dependencies" in plugin and plugin["dependencies"].get("mcpServers") != "./.mcp.json":
                report.fail("依赖声明", "dependencies.mcpServers 应指向 ./.mcp.json")
            else:
                report.ok("依赖声明", "./.mcp.json")

    skill_md = EXPERT / "skills" / "coursegenie" / "SKILL.md"
    report.ok("专家内置技能", "skills/coursegenie/SKILL.md") if skill_md.is_file() else report.fail(
        "专家内置技能", "缺少 skills/coursegenie/SKILL.md"
    )


def check_skill(report: Report) -> None:
    skill_md = SKILL / "SKILL.md"
    if not skill_md.is_file():
        report.fail("技能 SKILL.md", f"缺少 {skill_md}")
        return
    front = _frontmatter(skill_md)
    required = ["name", "display_name", "display_name_en", "description", "description_zh",
                "description_en", "version", "author"]
    missing = [key for key in required if not front.get(key)]
    report.ok("技能必填字段", "齐全") if not missing else report.fail("技能必填字段", f"缺 {missing}")
    if front.get("name") != SKILL.name:
        report.fail("技能目录名", f"{SKILL.name} ≠ frontmatter name {front.get('name')}")
    else:
        report.ok("技能目录名", SKILL.name)

    script = SKILL / "scripts" / "cg.py"
    if not script.is_file():
        report.fail("技能脚本", "缺少 scripts/cg.py")
        return
    try:
        py_compile.compile(str(script), doraise=True)
        report.ok("技能脚本", "cg.py 可编译")
    except py_compile.PyCompileError as exc:
        report.fail("技能脚本", str(exc))


def check_secrets(report: Report) -> None:
    hits: list[str] = []
    for path in list(EXPERT.rglob("*")) + list(SKILL.rglob("*")):
        if not path.is_file() or path.suffix.lower() in {".png", ".pyc", ".zip"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            hits.append(str(path))
    report.ok("无硬编码令牌", "0 命中") if not hits else report.fail("无硬编码令牌", f"命中：{hits}")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    report = Report()
    print("== 专家包 ==")
    check_expert(report)
    print("== 技能包 ==")
    check_skill(report)
    print("== 密钥扫描 ==")
    check_secrets(report)
    print(f"\n合计：FAIL {report.failed}")
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
