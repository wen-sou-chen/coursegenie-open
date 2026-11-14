"""连接器提交包自检（17 项）：把原来散在临时目录的一次性检查搬进仓库。

用法
----
    py -3 tools/ops/connector_check.py
    py -3 tools/ops/connector_check.py --repo C:\\Users\\wen\\CourseGenie

输出 ``[PASS]/[FAIL] 序号. 说明``，最后一行 ``汇总：N/M 通过``；有任何 FAIL 退出码 1。
只读仓库文件，不联网，不打印任何密钥。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifact_check as ac  # noqa: E402  同目录工具：安装源正则只留一份

REQUIRED_ZIP_ENTRIES = {
    "course_generator/__init__.py",
    "course_generator/__main__.py",
    "course_generator/server.py",
    "skills/coursegenie/SKILL.md",
    ".gitignore",
    "connector-meta.json",
    "icon.svg",
    "mcp.json",
    "pyproject.toml",
    "README.md",
    "token-schema.json",
}

SKILL_REQUIRED_FIELDS = (
    "name",
    "display_name",
    "display_name_en",
    "description",
    "description_zh",
    "description_en",
    "category",
    "version",
    "author",
)

SENSITIVE_KEYS = (
    "DEEPSEEK_API_KEY",
    "COURSE_BACKEND_URL",
    "COURSE_BACKEND_API_KEY",
    "COURSE_BACKEND_USE_SYSTEM_PROXY",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _json(path: Path) -> dict:
    return json.loads(_read(path))


class Checker:
    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.connector = repo / "connector"
        self.rows: list[tuple[int, str, bool, str]] = []

    def add(self, no: int, title: str, fn) -> None:
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001 —— 自检本身出错也要变成一条 FAIL
            ok, detail = False, f"检查本身出错：{type(exc).__name__}: {exc}"
        self.rows.append((no, title, bool(ok), str(detail)))

    def render(self) -> str:
        lines = []
        for no, title, ok, detail in self.rows:
            lines.append(f"[{'PASS' if ok else 'FAIL'}] {no}. {title}")
            lines.append(f"       {detail}")
        passed = sum(1 for row in self.rows if row[2])
        lines.append(f"\n汇总：{passed}/{len(self.rows)} 通过")
        return "\n".join(lines)


def _meta(checker: Checker) -> dict:
    return _json(checker.connector / "connector-meta.json")


def _pyproject_version(checker: Checker) -> str:
    match = re.search(r'^version\s*=\s*"([^"]+)"', _read(checker.connector / "pyproject.toml"), re.M)
    return match.group(1) if match else ""


def _mcp(checker: Checker) -> dict:
    return _json(checker.connector / "mcp.json")


def _server_config(checker: Checker) -> dict:
    return ((_mcp(checker).get("mcpServers") or {}).get("coursegenie") or {})


def _install_source(checker: Checker) -> str:
    args = _server_config(checker).get("args") or []
    for index, arg in enumerate(args):
        if arg == "--from" and index + 1 < len(args):
            return str(args[index + 1])
    return ""


def _source_version(checker: Checker) -> str:
    match = re.fullmatch(r"coursegenie-mcp==(\d+\.\d+\.\d+)", _install_source(checker))
    return match.group(1) if match else ""


def _latest_zip(checker: Checker) -> Path | None:
    zips = list(checker.repo.glob("coursegenie-connector-*.zip"))
    if not zips:
        return None

    def key(path: Path) -> tuple[int, int, int]:
        match = re.search(r"-(\d+)\.(\d+)\.(\d+)\.zip$", path.name)
        return tuple(map(int, match.groups())) if match else (0, 0, 0)

    return sorted(zips, key=key)[-1]


def _zip_names(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as archive:
        return {entry.filename for entry in archive.infolist()}


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    block = text.split("---", 2)[1]
    values: dict[str, str] = {}
    for line in block.splitlines():
        match = re.match(r'^([A-Za-z_][\w-]*)\s*:\s*(.*)$', line)
        if match:
            values[match.group(1)] = match.group(2).strip().strip('"').strip("'")
    return values


def build_checks(checker: Checker) -> None:
    meta = _meta(checker)
    version = _pyproject_version(checker)
    schema = _json(checker.connector / "token-schema.json")
    fields = [f for f in (schema.get("fields") or []) if isinstance(f, dict)]
    field_keys = {str(f.get("key")) for f in fields if f.get("key")}
    server_source = _read(checker.connector / "course_generator" / "server.py")

    def check_1():
        source = str(meta.get("source") or "")
        ok_format = bool(re.fullmatch(r"[a-z0-9][a-z0-9-]*", source))
        home = Path.home() / ".workbuddy"
        conflicts: list[str] = []
        for base in ("connectors", "extensions"):
            for path in (home / base).glob("*/connector-meta.json"):
                try:
                    other = str(_json(path).get("source") or "")
                except (OSError, ValueError):
                    continue
                if other == source and path.parent.resolve() != checker.connector.resolve():
                    conflicts.append(path.parent.name)
        if home.exists():
            local = f"本机冲突={conflicts or '无'}"
        else:
            local = "本机无 WorkBuddy 配置目录，跳过全局唯一比对"
        return ok_format and not conflicts, f"source='{source}'；{local}"

    def check_2():
        value = str(meta.get("minWorkbuddyVersion") or "")
        ok = bool(re.fullmatch(r"\d+\.\d+\.\d+", value)) and tuple(map(int, value.split("."))) >= (4, 24, 0)
        return ok, f"minWorkbuddyVersion='{value}'（要求 ≥ 4.24.0）"

    def check_3():
        auth = str(meta.get("auth_mode") or "")
        exists = (checker.connector / "token-schema.json").is_file()
        return auth == "token" and exists, f"auth_mode='{auth}'；token-schema.json={'有' if exists else '缺'}"

    def check_4():
        has_fields = isinstance(schema.get("fields"), list) and bool(fields)
        has_password = any(f.get("type") == "password" for f in fields)
        clean = "properties" not in schema and all(f.get("key") and f.get("type") for f in fields)
        return has_fields and has_password and clean, (
            f"fields[] {len(fields)} 项；password 字段="
            f"{[f.get('key') for f in fields if f.get('type') == 'password']}；旧格式 properties={'有' if 'properties' in schema else '无'}"
        )

    def check_5():
        env = _server_config(checker).get("env") or {}
        placeholders = {
            match.group(1)
            for value in env.values()
            if (match := re.fullmatch(r"\$\{([A-Z0-9_]+)\}", str(value)))
        }
        same_keys = placeholders == field_keys
        same_env = set(map(str, env)) == placeholders
        return same_keys and same_env, (
            f"${placeholders}；fields={field_keys}；env={set(map(str, env))}"
        )

    def check_6():
        targets = [checker.connector / "course_generator" / name
                   for name in ("__init__.py", "__main__.py", "server.py")]
        targets += [checker.connector / "mcp.json", checker.connector / "token-schema.json"]
        text = "\n".join(_read(path) for path in targets if path.is_file())
        patterns = {
            "sk-凭据": r"\bsk-[A-Za-z0-9]{16,}\b",
            "硬编码赋值": r"(?i)\b(api[_-]?key|secret|password|token)\b\s*=\s*[\"'][^\"']{12,}[\"']",
            "机器路径": r"(?i)\b[A-Z]:[\\/](?:Users|Documents and Settings)[\\/]",
            "临时目录": r"(?i)%TEMP%|/tmp/",
        }
        hits = {name: len(re.findall(pattern, text)) for name, pattern in patterns.items()}
        return all(count == 0 for count in hits.values()), f"6 类模式命中：{hits}"

    def check_7():
        missing = [key for key in SENSITIVE_KEYS if f'os.environ.get("{key}"' not in server_source]
        literal = [key for key in SENSITIVE_KEYS
                   if re.search(rf'\b{key}\s*=\s*["\']', server_source)]
        return not missing and not literal, (
            f"缺 os.environ 读取={missing or '无'}；字面量赋值={literal or '无'}"
        )

    def check_8():
        budget = re.search(r"^TOOL_BUDGET_S\s*=\s*([\d.]+)", server_source, re.M)
        outline = re.search(r"^OUTLINE_WAIT_S\s*=\s*([\d.]+)", server_source, re.M)
        timeout = _server_config(checker).get("timeout")
        ok = bool(budget) and float(budget.group(1)) <= 25 and timeout == 30000
        detail = (f"TOOL_BUDGET_S={budget.group(1) if budget else '缺'}；"
                  f"OUTLINE_WAIT_S={outline.group(1) if outline else '缺'}；mcp timeout={timeout}")
        return ok, detail

    def check_9():
        tool_count = len(re.findall(r"@mcp\.tool", server_source))
        fallback_count = len(re.findall(r"return exc\.as_dict\(\)", server_source))
        has_chinese = bool(re.search(r"[\u4e00-\u9fff]", server_source))
        no_traceback = "traceback" not in server_source.lower()
        return tool_count > 0 and fallback_count >= tool_count and has_chinese and no_traceback, (
            f"工具 {tool_count} 个；工具级兜底 {fallback_count} 处；中文错误={has_chinese}；无 traceback={no_traceback}"
        )

    def check_10():
        skill = checker.connector / "skills" / str(meta.get("source") or "") / "SKILL.md"
        if not skill.is_file():
            return False, f"缺 {skill}"
        data = _frontmatter(_read(skill))
        missing = [key for key in SKILL_REQUIRED_FIELDS if not data.get(key)]
        name_ok = data.get("name") == meta.get("source")
        version_ok = not data.get("version") or data.get("version") == version
        return not missing and name_ok and version_ok, (
            f"{skill.relative_to(checker.repo)}；缺必填={missing or '无'}；name 对齐={name_ok}；版本对齐={version_ok}"
        )

    def check_11():
        latest = _latest_zip(checker)
        if latest is None:
            return False, "仓库根目录没有 coursegenie-connector-*.zip"
        names = _zip_names(latest)
        expected_name = f"coursegenie-connector-{version}.zip"
        return latest.name == expected_name and names == REQUIRED_ZIP_ENTRIES, (
            f"{latest.name}；条目 {len(names)} 个；缺={sorted(REQUIRED_ZIP_ENTRIES - names) or '无'}；"
            f"多={sorted(names - REQUIRED_ZIP_ENTRIES) or '无'}"
        )

    def check_12():
        latest = _latest_zip(checker)
        if latest is None:
            return False, "仓库根目录没有 coursegenie-connector-*.zip"
        bad = [name for name in _zip_names(latest) if ".venv" in name or "node_modules" in name]
        return not bad, f".venv / node_modules 命中：{len(bad)}"

    def check_13():
        cfg = _mcp(checker)
        servers = cfg.get("mcpServers") or {}
        server = _server_config(checker)
        args = server.get("args") or []
        ok = (list(servers) == ["coursegenie"] and server.get("type") == "stdio"
              and server.get("command") == "uvx" and bool(args)
              and args[-1] == "coursegenie-mcp" and server.get("timeout") == 30000)
        return ok, f"servers={list(servers)}；command={server.get('command')}；args 末项={args[-1] if args else '缺'}"

    def check_14():
        readme = _read(checker.connector / "README.md")
        has_flow = "数据流向" in readme
        has_store = "~/.workbuddy" in readme
        has_install_note = "PyPI" in readme and "不下载" in readme
        return has_flow and has_store and has_install_note, (
            f"数据流向={has_flow}；凭据位置={has_store}；PyPI 安装说明={has_install_note}"
        )

    def check_15():
        latest = _latest_zip(checker)
        names = _zip_names(latest) if latest else set()
        skill = checker.connector / "skills" / str(meta.get("source") or "") / "SKILL.md"
        skill_data = _frontmatter(_read(skill)) if skill.is_file() else {}
        meta_keys = {"name", "name_zh", "name_en", "description", "description_zh",
                     "description_en", "source", "type", "auth_mode", "version",
                     "minWorkbuddyVersion", "examples_zh", "examples_en"}
        items = [
            ("元信息齐全", not (meta_keys - set(meta))),
            ("图标可见", (checker.connector / "icon.svg").is_file()
             and "<svg" in _read(checker.connector / "icon.svg")),
            ("mcpServers 可解析", bool(_mcp(checker).get("mcpServers"))),
            ("token-schema 可解析", bool(fields)),
            ("SKILL.md 在包内", "skills/coursegenie/SKILL.md" in names),
            ("README 非空", len(_read(checker.connector / "README.md")) > 1000),
            ("pyproject 入口", "[project.scripts]" in _read(checker.connector / "pyproject.toml")),
            ("zip 无本地依赖", not any(".venv" in name or "node_modules" in name for name in names)),
            ("双语示例对齐", len(meta.get("examples_zh") or []) == len(meta.get("examples_en") or []) > 0),
            ("category 已填", bool(skill_data.get("category"))),
        ]
        failed = [label for label, ok in items if not ok]
        return not failed, f"{len(items) - len(failed)}/{len(items)}；未过={failed or '无'}"

    def check_16():
        source = _install_source(checker)
        root_readme = _read(checker.repo / "README.md")
        bad_root = "/exports/coursegenie_mcp-" in root_readme
        ok = (not source.startswith("http://") and bool(ac.INSTALL_SOURCE_RE.fullmatch(source))
              and not bad_root)
        return ok, (f"--from {source}；规则 {ac.INSTALL_SOURCE_RE.pattern}；"
                    f"根 README 残留 /exports/coursegenie_mcp-={bad_root}")

    def check_17():
        values = {"pyproject.toml": version, "connector-meta.json": str(meta.get("version") or ""),
                  "mcp.json": _source_version(checker)}
        return bool(version) and len(set(values.values())) == 1, f"三处版本号={values}"

    checks = [
        (1, "source 小写字母/数字/连字符、本机无冲突", check_1),
        (2, "minWorkbuddyVersion 取最高版本", check_2),
        (3, 'auth_mode="token" 且有 token-schema.json', check_3),
        (4, "token-schema 官方 fields[] + password", check_4),
        (5, "${VAR} 与 fields[].key 逐字一致", check_5),
        (6, "无硬编码凭据 / 机器特定路径", check_6),
        (7, "只从 os.environ 读敏感值", check_7),
        (8, "单次响应 30 秒内", check_8),
        (9, "中文错误、不抛栈", check_9),
        (10, "SKILL.md 在 skills/{name}/ 且必填齐全", check_10),
        (11, "zip 根目录必需文件齐全", check_11),
        (12, "无 .venv / node_modules", check_12),
        (13, "mcp.json 用 uvx、只配一个 Server", check_13),
        (14, "README 声明数据流向与凭据位置", check_14),
        (15, "官方「提交前检查」10 条可机械判定项", check_15),
        (16, "安装源不是 http://，且形如 coursegenie-mcp==X.Y.Z", check_16),
        (17, "pyproject / connector-meta / mcp.json 三处版本一致", check_17),
    ]
    for no, title, fn in checks:
        checker.add(no, title, fn)


def main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="连接器提交包自检（17 项）")
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[2]),
                        help="仓库根目录，默认脚本所在仓库")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    if not (repo / "connector").is_dir():
        print(f"[FAIL] 仓库里没有 connector/：{repo}")
        return 2

    checker = Checker(repo)
    build_checks(checker)
    print(checker.render())
    return 0 if all(row[2] for row in checker.rows) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
