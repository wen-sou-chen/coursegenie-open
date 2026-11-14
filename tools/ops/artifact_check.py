"""连接器「审核冻结体检」：一个命令回答「这份提交包还是不是我们交上去的那一份」。

为什么要有它
------------
1.0.2/1.0.3/1.0.4 是提交到开放平台审核时的产物。审核期间**版本号、端口、URL、安装源、
工具数量**任何一个变了，已经交上去的那份就连不上或者对不上。这些值散在 5 个文件里
（`pyproject.toml` / `connector-meta.json` / `mcp.json` / `README.md` / `token-schema.json`），
靠人眼对齐迟早出错；包本身（zip / wheel）也会在本地被重新构建而不自知。

用法
----
    py -3 tools/ops/artifact_check.py check                 # 只体检，不写文件
    py -3 tools/ops/artifact_check.py check --health        # 顺带看演示服务当前状态
    py -3 tools/ops/artifact_check.py snapshot              # 把当前指纹写进基线
    py -3 tools/ops/artifact_check.py check --strict        # 有 WARN 也当成失败（提交前用）

退出码：0 = 全 PASS（strict 模式下有 WARN 为 1）；1 = 有 FAIL；2 = 脚本自身用错。
不做任何写操作（snapshot 除外，且只写一个 JSON）；不联网（除非 --health）；不打印任何密钥。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONNECTOR = REPO / "connector"
BASELINE = REPO / "tools" / "ops" / "fingerprint.json"
INSTALL_SOURCE_RE = re.compile(r"^coursegenie-mcp==\d+\.\d+\.\d+$")

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, level: str, name: str, detail: str) -> None:
        self.rows.append((level, name, detail))

    def ok(self, name: str, detail: str) -> None:
        self.add(PASS, name, detail)

    def warn(self, name: str, detail: str) -> None:
        self.add(WARN, name, detail)

    def fail(self, name: str, detail: str) -> None:
        self.add(FAIL, name, detail)

    def counts(self) -> dict[str, int]:
        return {lvl: sum(1 for r in self.rows if r[0] == lvl) for lvl in (PASS, WARN, FAIL)}

    def render(self) -> str:
        out = []
        for level, name, detail in self.rows:
            out.append(f"[{level}] {name}\n       {detail}")
        c = self.counts()
        out.append(f"\n合计：PASS {c[PASS]} / WARN {c[WARN]} / FAIL {c[FAIL]}")
        return "\n".join(out)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _pyproject_version() -> str:
    m = re.search(r'^version\s*=\s*"([^"]+)"', _read(CONNECTOR / "pyproject.toml"), re.M)
    return m.group(1) if m else ""


def _meta_version() -> str:
    return str(json.loads(_read(CONNECTOR / "connector-meta.json")).get("version") or "")


def _mcp_install_source() -> str:
    cfg = json.loads(_read(CONNECTOR / "mcp.json"))
    args = (((cfg.get("mcpServers") or {}).get("coursegenie") or {}).get("args") or [])
    for i, arg in enumerate(args):
        if arg == "--from" and i + 1 < len(args):
            return str(args[i + 1])
    return ""


def _install_source_version(source: str) -> str:
    m = re.search(r"^coursegenie-mcp==(\d+\.\d+\.\d+)$", source)
    return m.group(1) if m else ""


def _token_schema() -> dict:
    return json.loads(_read(CONNECTOR / "token-schema.json"))


def _tool_count() -> int:
    return len(re.findall(r"@mcp\.tool", _read(CONNECTOR / "course_generator" / "server.py")))


def _zip_paths() -> list[Path]:
    return sorted(REPO.glob("coursegenie-connector-*.zip"))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprint() -> dict:
    """当前应该被冻结的指纹（写进基线、或与基线比对）。"""
    zips = []
    for path in _zip_paths():
        zips.append({"name": path.name, "bytes": path.stat().st_size, "sha256": _sha256(path)})
    return {
        "version": _pyproject_version(),
        "meta_version": _meta_version(),
        "install_source": _mcp_install_source(),
        "install_version": _install_source_version(_mcp_install_source()),
        "tool_count": _tool_count(),
        "zips": zips,
    }


def check(rep: Report, baseline: dict | None, strict: bool, health: bool) -> None:
    # ① 版本号五处一致（含仓库根 README）
    ver = _pyproject_version()
    meta = _meta_version()
    source = _mcp_install_source()
    source_ver = _install_source_version(source)
    readme = _read(CONNECTOR / "README.md")
    readme_match = re.search(r"coursegenie-mcp==(\d+\.\d+\.\d+)", readme)
    readme_ver = readme_match.group(1) if readme_match else ""
    root_readme = _read(REPO / "README.md")
    root_match = re.search(r"coursegenie-mcp==(\d+\.\d+\.\d+)", root_readme)
    root_ver = root_match.group(1) if root_match else ""
    found = {"pyproject": ver, "connector-meta": meta, "mcp.json": source_ver,
             "connector/README": readme_ver, "README": root_ver}
    if "47831/exports" in root_readme:
        rep.fail("① 版本号一致", "根 README 仍含 47831/exports：安装源不许再回到 http 直链")
    elif not all(found.values()) or len(set(found.values())) > 1:
        rep.fail("① 版本号一致", f"不一致：{found}")
    else:
        rep.ok("① 版本号一致", " / ".join(f"{k}={v}" for k, v in found.items()))

    # ② 安装源固定、HTTPS（PyPI），且版本号钉死
    if not source:
        rep.fail("② 安装源", "mcp.json 里没找到 uvx 的 --from 安装源")
    elif source.startswith("http://"):
        rep.fail("② 安装源", f"{source} —— 启动时明文下载可执行文件，必须改用 PyPI 固定版本")
    elif not INSTALL_SOURCE_RE.match(source):
        rep.fail("② 安装源", f"{source} —— 不匹配 {INSTALL_SOURCE_RE.pattern}（包名固定、版本号必须 X.Y.Z）")
    else:
        rep.ok("② 安装源", f"{source}（PyPI + HTTPS，版本钉死）")

    # ③ token-schema 走官方 fields 口径
    schema = _token_schema()
    fields = schema.get("fields")
    if not isinstance(fields, list):
        rep.fail("③ token-schema 结构", "没有 fields[] 数组（官方 Connector 规范要 fields/type，不是 JSON Schema 的 properties）")
    else:
        keys = [str(f.get("key")) for f in fields if isinstance(f, dict)]
        pwd = [k for f in fields if isinstance(f, dict) and f.get("type") == "password" for k in [str(f.get("key"))]]
        if not pwd:
            rep.fail("③ token-schema 结构", f"fields 里没有 type=password 的密钥字段（现有：{keys}）")
        elif "properties" in schema:
            rep.fail("③ token-schema 结构", "同时出现 properties（JSON Schema 写法），官方规范只认 fields[]")
        else:
            rep.ok("③ token-schema 结构", f"fields[] {len(fields)} 项，password 字段：{pwd}")

    # ④ 默认服务地址：连接器常量与配置默认值一致
    default_url = re.search(r'DEFAULT_BACKEND_URL\s*=\s*"([^"]+)"',
                            _read(CONNECTOR / "course_generator" / "server.py"))
    schema_default = ""
    for f in (fields or []):
        if isinstance(f, dict) and f.get("key") == "COURSE_BACKEND_URL":
            schema_default = str(f.get("defaultValue") or "")
    if not default_url:
        rep.warn("④ 默认服务地址", "server.py 里找不到 DEFAULT_BACKEND_URL")
    elif default_url.group(1).rstrip("/") != schema_default.rstrip("/"):
        rep.fail("④ 默认服务地址", f"server.py={default_url.group(1)} ≠ token-schema 默认值={schema_default}")
    else:
        rep.ok("④ 默认服务地址", default_url.group(1))

    # ⑤ 工具数量
    n_tools = _tool_count()
    if baseline and baseline.get("tool_count") not in (None, n_tools):
        rep.fail("⑤ 工具数量", f"基线是 {baseline['tool_count']} 个，现在是 {n_tools} 个 —— 审核期间不许增减工具")
    else:
        rep.ok("⑤ 工具数量", f"{n_tools} 个（审核期间不许增减）")

    # ⑥ 提交包指纹
    cur = fingerprint()
    zips = cur["zips"]
    if not zips:
        rep.warn("⑥ 提交包", "仓库根目录没有 coursegenie-connector-*.zip（还没打包？）")
    elif baseline is None:
        rep.ok("⑥ 提交包", "；".join(f"{z['name']} {z['bytes']} B {z['sha256'][:12]}…" for z in zips))
    else:
        base = {z["name"]: z for z in baseline.get("zips") or []}
        now = {z["name"]: z for z in zips}
        missing = [name for name in base if name not in now]
        bad = []
        for z in zips:
            old = base.get(z["name"])
            if old is None:
                bad.append(f"{z['name']} 是新出现的包")
            elif old.get("sha256") != z["sha256"] or old.get("bytes") != z["bytes"]:
                bad.append(f"{z['name']} 变了（{old.get('bytes')}→{z['bytes']} B，{old.get('sha256','')[:12]}→{z['sha256'][:12]}）")
        if bad:
            rep.fail("⑥ 提交包", "；".join(bad) + " —— 已提交的包变了，必须出新版本重新提审")
        else:
            rep.ok("⑥ 提交包", "与基线逐字节一致：" + "；".join(f"{z['name']} {z['bytes']} B" for z in zips))
        if missing:
            rep.warn("⑥ 提交包（缺件）",
                     "基线里有、现在找不到：" + "、".join(missing)
                     + " —— 若是误删（如磁盘清理顺手指了仓库根目录）请从提交物取回；确实要清理就同时更新基线")

    # ⑦ 演示服务当前状态（可选，联网）
    if health:
        health_url = (schema_default or "http://127.0.0.1:47831").rstrip("/") + "/api/health"
        try:
            with urllib.request.urlopen(health_url, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            rep.fail("⑦ 演示服务", f"{health_url} 取不到：{exc}")
        else:
            demo = data.get("demo") or {}
            caps = {i.get("key"): i.get("available") for i in (data.get("capabilities") or {}).get("items") or []}
            rep.ok("⑦ 演示服务",
                   f"ok={data.get('ok')} frontend={(data.get('frontend') or {}).get('enabled')} "
                   f"demo={demo.get('enabled')} 保留{demo.get('retention_hours')}h "
                   f"每天{demo.get('max_courses_per_day')}门 并发{demo.get('max_concurrent_jobs')} "
                   f"edge={caps.get('edge')}")

    if strict and rep.counts()[WARN]:
        rep.warn("strict 模式", "存在 WARN，按失败对待（提交前必须清零）")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="连接器审核冻结体检")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_check = sub.add_parser("check", help="体检（不写文件）")
    p_check.add_argument("--baseline", default=str(BASELINE))
    p_check.add_argument("--strict", action="store_true")
    p_check.add_argument("--health", action="store_true", help="顺带请求演示服务的 /api/health")
    p_check.add_argument("--json", action="store_true", help="只输出指纹 JSON")
    p_snap = sub.add_parser("snapshot", help="把当前指纹写成基线")
    p_snap.add_argument("--out", default=str(BASELINE))
    args = parser.parse_args(argv)

    if args.cmd == "snapshot":
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(fingerprint(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"基线已写入 {out}")
        return 0

    baseline = None
    bp = Path(args.baseline)
    if bp.is_file():
        baseline = json.loads(bp.read_text(encoding="utf-8"))
    if args.json:
        print(json.dumps(fingerprint(), ensure_ascii=False, indent=2))
        return 0

    rep = Report()
    if baseline is None:
        rep.warn("基线", f"没有 {bp}（先跑 snapshot 把当前状态冻结成基线）")
    check(rep, baseline, args.strict, args.health)
    print(rep.render())
    c = rep.counts()
    if c[FAIL]:
        return 1
    if args.strict and c[WARN]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
