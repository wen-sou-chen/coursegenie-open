"""CourseGenie 数据备份：把「丢了就回不来」的部分打成一个带时间戳的 zip（任务 117 §二.B）。

用法（**在仓库根或 `backend\` 里跑都一样**；脚本自己按应用口径切到 `backend\` 找配置）::

    # 1) 先看清单：数据在哪、能不能重建、丢了什么后果（不写任何文件）
    python scripts/backup_data.py --list

    # 2) 备份（默认只打「不可重建」的部分，排除交付包 / 课件产物 / 模型缓存这些可重建的大件）
    python scripts/backup_data.py --out D:\\cg-backups

    # 3) 连可重建的大件一起打（空间换省事）
    python scripts/backup_data.py --out D:\\cg-backups --include-rebuildable

**基准目录**：`backend\`（与起服务同一口径）—— 脚本一进来就把 cwd 切过去，所以 `.env` 读得到、
`DATABASE_URL` 这类相对路径也按 `backend\` 解析；`--out` 若是相对路径，按**你敲命令时所在的目录**算。
（任务 123：以前只按 cwd 定位 ⇒ 在仓库根跑会读不到 `.env`，把默认路径指到 `<仓库>/data`、
`<仓库>/storage`，报「没有找到任何可备份的数据」，看起来像「你没有数据」。）

**为什么默认不带交付包与模型缓存**：它们都能从库里重新生成 / 重新下载，却往往占掉 90% 的体积
（本机实测：交付包 7.4 万文件级、模型缓存 6.8 GB）。真正丢了回不来的只有三样：
SQLite 库、上传/生成的图片、Obsidian 知识库根。

**Key 不进备份**：`backend/.env`（里面有模型 Key）**从不**被收进 zip —— 备份文件经常会被拷来拷去，
把 Key 装进去等于把钥匙和保险箱一起寄走。恢复到新机器后请**在那台机器上单独配 Key**。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

#: 本项目统一口径（任务 99 同款写法 / 任务 123）：**Windows 默认（GBK / cp936）控制台**下也要能跑完 ——
#: 清单里的人话带 `⇒` 这类字符，Python 默认按控制台代码页编码 ⇒ 未包装时 `UnicodeEncodeError`
#: 直接崩在打印「注意：…」那一步（zip 都来不及产出）。stderr 同样要包，否则异常消息里的非 ASCII 字符会二次崩。
#: **不许**用「把这些字符换成 ASCII」回避（同类字符还会再犯）。
if getattr(sys.stdout, "buffer", None) is not None:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace",
                                  line_buffering=True)
if getattr(sys.stderr, "buffer", None) is not None:
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace",
                                  line_buffering=True)

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402

STAMP_FORMAT = "%Y%m%d-%H%M%S"
README_NAME = "RESTORE.md"
MANIFEST_NAME = "manifest.json"
MODEL_CACHE_DIR = Path.home() / ".cache" / "huggingface"

RESTORE_STEPS = """\
恢复到另一台机器上，就三步：

  1) 解压：把 zip 解开到任意目录，例如 `D:\\cg-restore\\`
     （解开后会是 `D:\\cg-restore\\db\\`、`D:\\cg-restore\\storage\\`、`D:\\cg-restore\\vault\\`）

  2) 起服务时把数据目录指到解出来的那份（在 backend\\ 目录下开 PowerShell；
     若 db\\ 里还有 -wal / -shm，请与库文件放在同一个目录）：
     $env:DATABASE_URL = "sqlite:///D:/cg-restore/db/coursegenie.db"
     $env:STORAGE_DIR  = "D:\\cg-restore\\storage"
     $env:EXPORT_DIR   = "D:\\cg-restore\\storage\\exports"
     $env:PPT_WORK_DIR = "D:\\cg-restore\\storage\\ppt"
     $env:OBSIDIAN_VAULT_ROOT = "D:\\cg-restore\\vault"
     .venv\\Scripts\\python.exe -m uvicorn app.main:app --port 8000

  3) 配 Key（备份里**没有** `.env`）：在那台机器的 `backend\\.env` 里填 `LLM_PROVIDER` 与对应 Key，
     打开 http://127.0.0.1:8000/api/health 看 `capabilities`：`llm` 那项 available=true 就配好了。

想把它当成正式数据用（而不是临时看一眼）：把 `db\\<库文件>`、`storage\\`、`vault\\` 拷回
新机器的 `backend\\data\\`、`backend\\storage\\`、知识库目录，再按平时的方式起服务即可。
"""


@dataclass
class Item:
    """清单里的一项：一个目录或一个文件。"""

    name: str
    path: Path | None          # None = 这项没配 / 不适用（整项跳过，绝不去猜路径）
    kind: str                      # db | dir
    rebuildable: bool
    consequence: str               # 丢了什么后果
    arc_root: str                  # 进 zip 时的顶层目录名
    default_on: bool               # 默认是否入包
    note: str = ""
    files: list[tuple[Path, str]] = field(default_factory=list)
    links: int = 0                 # 被跳过的符号链接数（内容随目标文件一起打，避免重复计体积）
    bytes_total: int = 0


def _sqlite_path(settings) -> Path | None:
    url = str(settings.database_url or "")
    if not url.startswith("sqlite:///"):
        return None
    return Path(url.replace("sqlite:///", "", 1)).expanduser().resolve()


def _collect_dir(root: Path, arc_root: str) -> tuple[list[tuple[Path, str]], int]:
    """收集目录下的真实文件；**跳过符号链接**并单独计数（否则同一份内容会被算两次体积）。"""
    out: list[tuple[Path, str]] = []
    links = 0
    if not root.is_dir():
        return out, links
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            links += 1
            continue
        if path.is_file():
            out.append((path, f"{arc_root}/{path.relative_to(root).as_posix()}"))
    return out, links


def build_inventory(settings=None) -> list[Item]:
    """按当前配置列出「数据在哪、能不能重建、丢了什么后果」——`--list` 与备份共用这一份。"""
    settings = settings if settings is not None else get_settings()
    items: list[Item] = []

    db_path = _sqlite_path(settings)
    if db_path is None:
        items.append(Item("SQLite 库", None, "db", False,
                          "全部课程、大纲、内容、任务与用量记录", "db", False,
                          note=f"DATABASE_URL={settings.database_url} 不是 sqlite:// ⇒ "
                               "请用 pg_dump 之类自行备份，本脚本不代做"))
    else:
        items.append(Item("SQLite 库", db_path, "db", False,
                          "全部课程、大纲、内容、任务与用量记录（**唯一真源，丢了回不来**）",
                          "db", True,
                          note="库旁的 -wal / -shm（WAL 模式）存在时一并打包，恢复时三个文件要放一起"))

    storage = Path(settings.storage_dir).expanduser().resolve()
    items.append(Item("图片与素材产物", storage / "images", "dir", False,
                      "上传的原图与生成的配图 / 概念图（原图丢了要不回来）", "storage/images", True))
    items.append(Item("课件产物", storage / "ppt", "dir", True,
                      "课件 pptx 与每页幻灯片图片；能从库里重新生成（要花时间）",
                      "storage/ppt", False))
    items.append(Item("导出目录", Path(settings.export_dir).expanduser().resolve(), "dir", True,
                      "笔记库导出等中间产物；能从库与知识库重新导出", "storage/exports", False))
    items.append(Item("交付包", storage / "deliveries", "dir", True,
                      "给客户的交付包；能从库里重新出一包（体积最大的那个）",
                      "storage/deliveries", False))

    vault = Path(settings.obsidian_vault_root).expanduser() if settings.obsidian_vault_root else None
    items.append(Item("Obsidian 知识库根", vault.resolve() if vault else None, "dir", False,
                      "老师手写的笔记与课程 Markdown（**唯一真源，丢了回不来**）",
                      "vault", bool(vault),
                      note="" if vault else "OBSIDIAN_VAULT_ROOT 未设置 ⇒ 跳过（不会去猜路径）"))

    items.append(Item("模型缓存（faster-whisper / HuggingFace）", MODEL_CACHE_DIR, "dir", True,
                      "语音转写模型权重；联网能重新下载（本机可能好几个 GB）",
                      "model-cache", False, note="只在 --include-model-cache 时打包"))
    return items


def _scan(items: list[Item]) -> None:
    for item in items:
        item.files = []
        item.links = 0
        item.bytes_total = 0
        if item.path is None:          # 没配 / 不适用：整项跳过（免得误扫到当前目录）
            continue
        if item.kind == "db":
            if item.path.is_file():
                item.files = [(item.path, f"{item.arc_root}/{item.path.name}")]
                for suffix in ("-wal", "-shm"):      # WAL 模式的侧车文件：少了会丢最近的写入
                    side = item.path.with_name(item.path.name + suffix)
                    if side.is_file():
                        item.files.append((side, f"{item.arc_root}/{side.name}"))
                item.bytes_total = sum(p.stat().st_size for p, _ in item.files)
            continue
        item.files, item.links = _collect_dir(item.path, item.arc_root)
        item.bytes_total = sum(p.stat().st_size for p, _ in item.files)


def _human(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def selected(items: list[Item], *, include_rebuildable: bool, include_model_cache: bool) -> list[Item]:
    out = []
    for item in items:
        if item.name.startswith("模型缓存"):
            if include_model_cache:
                out.append(item)
            continue
        if item.default_on or include_rebuildable:
            out.append(item)
    return out


def print_inventory(items: list[Item], picked: list[Item]) -> None:
    print("数据目录清单（能重建 = 丢了可以重新做出来；★不可重建 = 丢了就回不来）")
    print("=" * 100)
    for item in items:
        chosen = item in picked
        can = "可重建" if item.rebuildable else "★不可重建"
        extra = f" / 跳过符号链接 {item.links} 个" if item.links else ""
        print(f"[{'入包' if chosen else '不入包'}] {item.name} —— {can}"
              f"（{len(item.files)} 个文件 / {_human(item.bytes_total)}{extra}）")
        print(f"          路径：{item.path if item.path else '(未设置)'}")
        print(f"          丢了什么后果：{item.consequence}")
        if item.note:
            print(f"          注意：{item.note}")
    print("=" * 100)
    total = sum(i.bytes_total for i in picked)
    print(f"本次入包 {len(picked)} 项 / {sum(len(i.files) for i in picked)} 个文件 / 约 {_human(total)}"
          "（压缩后更小）")
    print("★不可重建的项默认都入包；可重建的大件（交付包 / 课件产物 / 导出）默认排除，"
          "要一起打加 --include-rebuildable。")


def run_backup(out_dir: Path, items: list[Item], picked: list[Item]) -> Path:
    out_dir = out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime(STAMP_FORMAT)
    zip_path = out_dir / f"coursegenie-backup-{stamp}.zip"

    manifest = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "tool": "scripts/backup_data.py（任务 117）",
        "note": "只含数据；**不含** backend/.env 等任何密钥文件 —— 恢复到新机器后请单独配 Key。",
        "llm_calls": 0,
        "entries": [
            {"name": i.name, "source": str(i.path) if i.path else "", "arc_root": i.arc_root,
             "files": len(i.files), "bytes": i.bytes_total, "skipped_symlinks": i.links,
             "rebuildable": i.rebuildable, "consequence": i.consequence}
            for i in picked
        ],
        "totals": {"entries": len(picked), "files": sum(len(i.files) for i in picked),
                   "bytes": sum(i.bytes_total for i in picked)},
        "restore_steps": RESTORE_STEPS,
    }

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        zf.writestr(README_NAME, RESTORE_STEPS)
        for item in picked:
            for path, arcname in item.files:
                zf.write(path, arcname)
    return zip_path


def use_backend_as_base_dir() -> None:
    """把工作目录切到 ``backend/``（任务 123 §二.2 的**首选**做法）—— 让「在仓库根跑」与「在 backend 里跑」完全一样。

    为什么必须切：`.env` 是**相对 cwd** 读的（`Settings.env_file == ".env"`），`DATABASE_URL=sqlite:///…`
    这类相对路径也相对 cwd 解析；而应用本身永远是「站在 `backend\\` 下起服务」。脚本照同一口径来，
    才不会在仓库根跑时读不到 `.env`、把默认路径指到 `<仓库>/data` 与 `<仓库>/storage`
    （表现就是「什么都没找到」，还容易被误当成「你没有数据」）。
    """
    if Path.cwd().resolve() != BACKEND_DIR:
        os.chdir(BACKEND_DIR)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="CourseGenie 数据备份：把「丢了就回不来」的部分打成一个带时间戳的 zip。")
    parser.add_argument("--out", default=str(REPO_ROOT / "backups"),
                        help="备份输出目录（默认 <仓库>/backups）")
    parser.add_argument("--list", action="store_true", help="只打印数据目录清单，不写任何文件")
    parser.add_argument("--include-rebuildable", action="store_true",
                        help="连可重建的大件（交付包 / 课件产物 / 导出）一起打")
    parser.add_argument("--include-model-cache", action="store_true",
                        help="连模型缓存（HuggingFace，可能好几个 GB）一起打")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要打包的内容，不写 zip")
    args = parser.parse_args(argv)

    # --out 的相对路径按「你敲命令时所在的目录」算（先定住，再切基准目录，免得被切走）
    out_dir = Path(args.out).expanduser().resolve()
    use_backend_as_base_dir()

    settings = get_settings()
    items = build_inventory(settings)
    _scan(items)
    picked = selected(items, include_rebuildable=args.include_rebuildable,
                      include_model_cache=args.include_model_cache)

    if args.list:
        print_inventory(items, picked)
        print("\n（--list 不写任何文件；要真的备份去掉 --list，见 --help）")
        return 0

    print_inventory(items, picked)
    if args.dry_run:
        print("\n--dry-run：没有写任何文件。")
        return 0
    if not any(i.files for i in picked):
        print("\n没有找到任何可备份的数据（库 / storage / 知识库都为空或未设置）—— 先跑起来用一次再备份。")
        print(f"  基准目录：{Path.cwd()}（脚本已按应用口径切到 backend\\；配置读的就是同目录的 .env）")
        print("  若你确实有数据：看看上面三项的「路径」对不对 —— 脚本只按 backend\\.env 里的 "
              "DATABASE_URL / STORAGE_DIR / OBSIDIAN_VAULT_ROOT 找，**不会去猜路径**。")
        return 0

    zip_path = run_backup(out_dir, items, picked)
    size = zip_path.stat().st_size
    print(f"\n备份完成：{zip_path}")
    print(f"  文件数 {sum(len(i.files) for i in picked)} / 原始 {_human(sum(i.bytes_total for i in picked))}"
          f" / zip {_human(size)}")
    print()
    print(RESTORE_STEPS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())