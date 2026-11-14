"""任务 161：连接器必须从 PyPI 固定版本安装，不能在启动时下载明文 HTTP 文件。"""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CONNECTOR = REPO / "connector"
INSTALL_SOURCE_RE = re.compile(r"^coursegenie-mcp==\d+\.\d+\.\d+$")


def _mcp_config() -> dict:
    return json.loads((CONNECTOR / "mcp.json").read_text(encoding="utf-8"))


def _install_source(config: dict | None = None) -> str:
    cfg = config or _mcp_config()
    args = cfg["mcpServers"]["coursegenie"]["args"]
    index = args.index("--from")
    return args[index + 1]


def _version_of(source: str) -> str:
    match = re.fullmatch(r"coursegenie-mcp==(\d+\.\d+\.\d+)", source)
    return match.group(1) if match else ""


def _versions() -> dict[str, str]:
    pyproject = (CONNECTOR / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
    meta = json.loads((CONNECTOR / "connector-meta.json").read_text(encoding="utf-8"))
    return {
        "pyproject.toml": match.group(1) if match else "",
        "connector-meta.json": str(meta.get("version") or ""),
        "mcp.json": _version_of(_install_source()),
    }


def _latest_zip() -> Path | None:
    zips = list(REPO.glob("coursegenie-connector-*.zip"))
    if not zips:
        return None

    def key(path: Path) -> tuple[int, int, int]:
        match = re.search(r"-(\d+)\.(\d+)\.(\d+)\.zip$", path.name)
        return tuple(map(int, match.groups())) if match else (0, 0, 0)

    return sorted(zips, key=key)[-1]


def test_install_source_is_not_plain_http():
    assert not _install_source().startswith("http://")


def test_install_source_is_pinned_pypi_package():
    assert INSTALL_SOURCE_RE.match(_install_source()), _install_source()


def test_three_versions_match():
    versions = _versions()
    assert len(set(versions.values())) == 1, versions


def test_readme_shows_the_same_pinned_source():
    readme = (CONNECTOR / "README.md").read_text(encoding="utf-8")
    assert f'"coursegenie-mcp=={_versions()["pyproject.toml"]}"' in readme


def test_latest_connector_zip_has_the_same_pinned_source():
    archive = _latest_zip()
    if archive is None:
        pytest.skip("仓库根目录没有连接器 zip（本地产物），跳过打包防回归")
    with zipfile.ZipFile(archive) as zf:
        cfg = json.loads(zf.read("mcp.json").decode("utf-8"))
    source = _install_source(cfg)
    assert not source.startswith("http://"), f"{archive.name} 仍在用 HTTP 安装源：{source}"
    assert INSTALL_SOURCE_RE.match(source), f"{archive.name} 的安装源未钉死版本：{source}"
    assert source == _install_source(), "zip 里的 mcp.json 与源码不一致，忘了重打包？"
