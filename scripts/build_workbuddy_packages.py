"""把专家包 / 技能包打成可直接上传的 zip（顶层目录 = 平台要求的包名）。"""

from __future__ import annotations

import hashlib
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DIST = REPO / "packaging" / "dist"
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".DS_Store"}
SKIP_SUFFIX = {".pyc", ".pyo"}


def _add_tree(archive: zipfile.ZipFile, root: Path, arc_root: str) -> None:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if SKIP_PARTS.intersection(path.parts) or path.suffix in SKIP_SUFFIX:
            continue
        archive.write(path, f"{arc_root}/{path.relative_to(root).as_posix()}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    DIST.mkdir(parents=True, exist_ok=True)
    jobs = [
        (REPO / "packaging" / "expert-coursegenie", "coursegenie-expert", "coursegenie-expert-1.0.0.zip"),
        (REPO / "packaging" / "skill-coursegenie" / "coursegenie", "coursegenie", "coursegenie-skill-1.0.0.zip"),
    ]
    for root, arc_root, name in jobs:
        target = DIST / name
        if target.exists():
            target.unlink()
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            _add_tree(archive, root, arc_root)
        with zipfile.ZipFile(target) as archive:
            entries = archive.namelist()
        print(f"{name}: {target.stat().st_size} B / {len(entries)} 条目 / sha256 {_sha256(target)}")
        print("  " + "\n  ".join(entries))


if __name__ == "__main__":
    main()
