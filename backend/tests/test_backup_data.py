"""任务 117 §二.B：备份脚本的清单 / 内容 / 恢复，以及 Key 不外泄。

① 备份包内容符合清单：**含**不可重建的三样（SQLite 库 / 图片 / 知识库根）、
   **排除**可重建的大件（交付包 / 课件产物 / 导出 / 模型缓存），且**绝不含 `.env`**；
② 恢复到一个空目录后**能被应用正常打开**（另起一个真进程 `TestClient` 真开一次，不是模拟）；
③ Key 不出现在能力接口返回里（扫一遍返回值）。

脚本与「起一次应用」都走**子进程**：既证明「命令可执行」，也避免污染本进程已缓存的配置。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
SCRIPT = REPO_ROOT / "scripts" / "backup_data.py"
PYTHON = sys.executable

CANARY_KEY = "sk-canary-117-backup-do-not-leak-4d1c"

# 在独立进程里起一次应用：返回健康检查、课程列表与「模型配置可不可用」。
APP_PROBE = """
import json
from fastapi.testclient import TestClient
from app.main import app
with TestClient(app) as c:
    health = c.get("/api/health")
    courses = c.get("/api/course/")
    caps = health.json()["capabilities"]["items"]
    print(json.dumps({
        "health": health.status_code,
        "courses": courses.status_code,
        "llm_available": next(i["available"] for i in caps if i["key"] == "llm"),
        "titles": [row["title"] for row in courses.json()],
    }, ensure_ascii=False))
"""

CREATE_COURSE = """
import json
from fastapi.testclient import TestClient
from app.main import app
with TestClient(app) as c:
    resp = c.post("/api/course/", json={"title": "备份恢复用例-117", "subject": "生物",
                                        "audience": "初中生", "duration_minutes": 90})
    assert resp.status_code == 200, resp.text
    print(json.dumps({"id": resp.json()["id"]}))
"""


def _env(root: Path) -> dict[str, str]:
    """临时五件套 + 与测试一致的无副作用开关（不会碰真库 / 真知识库）。"""
    env = dict(os.environ)
    env.update({
        "DATABASE_URL": f"sqlite:///{(root / 'db' / 'coursegenie.db').as_posix()}",
        "STORAGE_DIR": str(root / "storage"),
        "EXPORT_DIR": str(root / "storage" / "exports"),
        "PPT_WORK_DIR": str(root / "storage" / "ppt"),
        "OBSIDIAN_VAULT_ROOT": str(root / "vault"),
        "LLM_PROVIDER": "mock",
        "IMAGE_PROVIDER": "mock",
        "AUTH_ENABLED": "false",
        "DASHI_PPT_FORCE_FALLBACK": "true",
        "PPT_SLIDE_IMAGES": "false",
        "PYTHONIOENCODING": "utf-8",
    })
    return env


def _run_script(args: list[str], env: dict[str, str], cwd: Path = REPO_ROOT):
    return subprocess.run([PYTHON, str(SCRIPT), *args], cwd=str(cwd), env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=600)


def _run_app(code: str, env: dict[str, str]) -> dict:
    proc = subprocess.run([PYTHON, "-c", code], cwd=str(BACKEND_DIR), env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=900)
    assert proc.returncode == 0, proc.stderr[-4000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _seed_sources(root: Path) -> None:
    """造出一份「有该有的、也有该排除的」数据目录。"""
    (root / "db").mkdir(parents=True, exist_ok=True)
    (root / "db" / "coursegenie.db").write_bytes(b"SQLite format 3\x00 pretend-db")
    for rel in ("storage/images", "storage/ppt/deck", "storage/exports", "storage/deliveries/pkg"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    (root / "storage" / "images" / "upload.png").write_bytes(b"png-bytes")
    (root / "storage" / "ppt" / "deck" / "slides.pptx").write_bytes(b"PK\x03\x04 pptx")
    (root / "storage" / "exports" / "kb-export.md").write_text("导出", encoding="utf-8")
    (root / "storage" / "deliveries" / "pkg" / "manifest.json").write_text("{}", encoding="utf-8")
    (root / "vault" / "课程").mkdir(parents=True, exist_ok=True)
    (root / "vault" / "课程" / "笔记.md").write_text("老师的笔记", encoding="utf-8")


def _names(zip_path: Path) -> list[str]:
    with zipfile.ZipFile(zip_path) as zf:
        return zf.namelist()


def _manifest(zip_path: Path) -> dict:
    with zipfile.ZipFile(zip_path) as zf:
        return json.loads(zf.read("manifest.json"))


def test_gbk_console_does_not_crash_and_still_writes_the_zip(tmp_path):
    """① 默认控制台（GBK / cp936）下跑完不崩、zip 照样产出（任务 123 §二.1）。

    触发条件与真机一致：**读不到 `.env`**（这里用「不设知识库根」等价模拟）⇒ 清单里会打印
    「OBSIDIAN_VAULT_ROOT 未设置 ⇒ 跳过」这一行；`⇒` 不在 GBK 里，未包 UTF-8 时直接
    `UnicodeEncodeError`（zip 都来不及产出）。
    """
    root = tmp_path / "data"
    _seed_sources(root)
    out = tmp_path / "backups"
    env = _env(root)
    env["OBSIDIAN_VAULT_ROOT"] = ""      # 打印带 ⇒ 的注意行（真机「仓库根跑、没读到 .env」同一触发条件）
    env["PYTHONIOENCODING"] = "gbk"      # 默认控制台编码（本机 cp936）

    proc = _run_script(["--out", str(out)], env)

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "注意：" in proc.stdout and "⇒" in proc.stdout, proc.stdout[-2000:]
    zips = list(out.glob("coursegenie-backup-*.zip"))
    assert len(zips) == 1, f"GBK 下没产出 zip：{zips}"
    names = _names(zips[0])
    assert "db/coursegenie.db" in names and "storage/images/upload.png" in names


def test_repo_root_and_backend_invocations_agree(tmp_path):
    """② 从**仓库根**与从 **backend** 调用，行为一致（任务 123 §二.2 首选做法）。

    脚本自己按应用口径把 cwd 切到 `backend\\` ⇒ `.env` 读得到、相对路径按 `backend\\` 解析；
    `--list` 的输出逐字节一致，真备份的入包口径也一致（入包项 / 文件数 / 体积）。
    """
    root = tmp_path / "data"
    _seed_sources(root)
    env = _env(root)
    out_root, out_backend = tmp_path / "out-root", tmp_path / "out-backend"

    listing_root = _run_script(["--list", "--out", str(out_root)], env)
    listing_backend = _run_script(["--list", "--out", str(out_backend)], env, cwd=BACKEND_DIR)
    assert listing_root.returncode == 0, listing_root.stderr[-2000:]
    assert listing_backend.returncode == 0, listing_backend.stderr[-2000:]
    assert listing_root.stdout == listing_backend.stdout, "两处调用的清单不一致"

    run_root = _run_script(["--out", str(out_root)], env)
    run_backend = _run_script(["--out", str(out_backend)], env, cwd=BACKEND_DIR)
    assert run_root.returncode == 0 and run_backend.returncode == 0
    man_root = _manifest(next(out_root.glob("coursegenie-backup-*.zip")))
    man_backend = _manifest(next(out_backend.glob("coursegenie-backup-*.zip")))
    assert man_root["totals"] == man_backend["totals"]
    assert ([r["arc_root"] for r in man_root["entries"]]
            == [r["arc_root"] for r in man_backend["entries"]] == ["db", "storage/images", "vault"])


def test_empty_sources_points_at_the_backend_base_dir(tmp_path):
    """空数据时给人话：基准目录在哪、配置从哪个 `.env` 读（免得被误当成「你没有数据」）。"""
    root = tmp_path / "empty"
    root.mkdir()
    out = tmp_path / "backups"

    proc = _run_script(["--out", str(out)], _env(root))

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "没有找到任何可备份的数据" in proc.stdout
    assert "基准目录" in proc.stdout and "backend" in proc.stdout and ".env" in proc.stdout
    assert not list(out.glob("*.zip"))


def test_list_prints_inventory_and_writes_nothing(tmp_path):
    """`--list` 就是那份「数据目录清单」，且不写任何文件。"""
    root = tmp_path / "data"
    _seed_sources(root)
    out = tmp_path / "backups"
    proc = _run_script(["--list", "--out", str(out)], _env(root))
    assert proc.returncode == 0, proc.stderr
    assert "数据目录清单" in proc.stdout
    assert "★不可重建" in proc.stdout and "可重建" in proc.stdout
    assert "SQLite 库" in proc.stdout and "Obsidian 知识库根" in proc.stdout
    assert "丢了什么后果" in proc.stdout
    assert not out.exists(), "--list 不该写任何文件"


def test_default_backup_keeps_only_the_irreproducible_parts(tmp_path):
    """① 默认只打包「丢了回不来」的三样；交付包 / 课件产物 / 导出 / 模型缓存排除；`.env` 永不进包。

    ⚠️ 末行「`.env` 永不进包」是**未来护栏**（任务 129 §A.9），**不是回归证据**：实现里没有收集
    `.env` 的路径（清单只有库 / 图片 / 知识库 + 可重建大件）⇒ 这条断言**走不到**；**不许删** ——
    留着盯住「以后谁把 `.env` 加进备份清单」这种改法（那时它会立刻红）。
    """
    root = tmp_path / "data"
    _seed_sources(root)
    out = tmp_path / "backups"
    proc = _run_script(["--out", str(out)], _env(root))
    assert proc.returncode == 0, proc.stderr

    zips = list(out.glob("coursegenie-backup-*.zip"))
    assert len(zips) == 1, zips
    names = _names(zips[0])

    assert "db/coursegenie.db" in names                 # 不可重建：库
    assert "storage/images/upload.png" in names         # 不可重建：图
    assert "vault/课程/笔记.md" in names                 # 不可重建：知识库
    assert "storage/ppt/deck/slides.pptx" not in names  # 可重建：课件产物
    assert "storage/exports/kb-export.md" not in names  # 可重建：导出
    assert "storage/deliveries/pkg/manifest.json" not in names  # 可重建：交付包
    assert "RESTORE.md" in names and "manifest.json" in names
    # ⚠️ 未来护栏（任务 129 §A.9）：现在没有收 `.env` 的路径 ⇒ 走不到；**不许删**（盯住以后别加进来）。
    assert not [n for n in names if n.endswith(".env")], "Key 文件不许进备份"

    with zipfile.ZipFile(zips[0]) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        restore = zf.read("RESTORE.md").decode("utf-8")
    entries = {row["arc_root"]: row for row in manifest["entries"]}
    assert set(entries) == {"db", "storage/images", "vault"}
    assert entries["db"]["rebuildable"] is False and entries["vault"]["rebuildable"] is False
    assert manifest["totals"]["entries"] == 3
    assert ".env" in manifest["note"]                   # 明确写了不含密钥文件
    assert "三步" in restore and "OBSIDIAN_VAULT_ROOT" in restore and "Key" in restore


def test_include_rebuildable_adds_the_big_reproducible_dirs(tmp_path):
    """① `--include-rebuildable` 才把交付包 / 课件产物 / 导出一起打（模型缓存另有开关）。"""
    root = tmp_path / "data"
    _seed_sources(root)
    out = tmp_path / "backups"
    proc = _run_script(["--out", str(out), "--include-rebuildable"], _env(root))
    assert proc.returncode == 0, proc.stderr
    names = _names(next(out.glob("coursegenie-backup-*.zip")))
    assert "storage/ppt/deck/slides.pptx" in names
    assert "storage/exports/kb-export.md" in names
    assert "storage/deliveries/pkg/manifest.json" in names
    assert not [n for n in names if n.startswith("model-cache/")]  # 模型缓存仍需单独开关


def test_include_model_cache_really_packs_the_cache(tmp_path):
    """① `--include-model-cache` 的**正向**护栏：开了之后包里真的带上模型缓存目录。

    任务 129 §A.8：以前只有否定断言（默认不带）⇒ 把开关接到别处也不会响。模型缓存按
    `Path.home()/".cache"/"huggingface"` 定位，所以这里把子进程的 `USERPROFILE` / `HOME`
    指到一个**假 home**，绝不碰真机上的 `~/.cache`。
    """
    root = tmp_path / "data"
    _seed_sources(root)
    fake_home = tmp_path / "home"
    cache = fake_home / ".cache" / "huggingface" / "models--Systran--faster-whisper-small"
    cache.mkdir(parents=True)
    (cache / "model.bin").write_bytes(b"weights")
    out = tmp_path / "backups"
    env = _env(root)
    env["USERPROFILE"] = str(fake_home)      # Path.home() 在子进程里跟着这两个变量走
    env["HOME"] = str(fake_home)

    proc = _run_script(["--out", str(out), "--include-model-cache"], env)

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert any(line.startswith("[入包] 模型缓存") for line in proc.stdout.splitlines()), proc.stdout[-2000:]
    zip_path = next(out.glob("coursegenie-backup-*.zip"))
    assert "model-cache/models--Systran--faster-whisper-small/model.bin" in _names(zip_path)
    entry = {row["arc_root"]: row for row in _manifest(zip_path)["entries"]}["model-cache"]
    assert entry["files"] == 1 and entry["rebuildable"] is True


def test_empty_sources_do_not_produce_a_broken_backup(tmp_path):
    """没有任何数据时不写半个 zip（免得以后误以为「备份过了」）。"""
    root = tmp_path / "empty"
    root.mkdir()
    out = tmp_path / "backups"
    proc = _run_script(["--out", str(out)], _env(root))
    assert proc.returncode == 0, proc.stderr
    assert "没有找到任何可备份的数据" in proc.stdout
    assert not list(out.glob("*.zip"))


def test_restored_backup_can_be_opened_by_the_app(tmp_path):
    """② 端到端：造数据 → 备份 → 解到空目录 → **真起一次应用**，数据还在、接口都正常。"""
    src = tmp_path / "src"
    env = _env(src)
    created = _run_app(CREATE_COURSE, env)
    assert created["id"]
    (src / "storage" / "images").mkdir(parents=True, exist_ok=True)
    (src / "storage" / "images" / "upload.png").write_bytes(b"png-bytes")
    (src / "vault").mkdir(parents=True, exist_ok=True)
    (src / "vault" / "笔记.md").write_text("老师手写的", encoding="utf-8")

    out = tmp_path / "backups"
    proc = _run_script(["--out", str(out)], env)
    assert proc.returncode == 0, proc.stderr
    zip_path = next(out.glob("coursegenie-backup-*.zip"))

    restore = tmp_path / "restore"
    restore.mkdir()
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(restore)
    # 解出来就是「三步说明」里说的那三样
    assert (restore / "db" / "coursegenie.db").is_file()
    assert (restore / "storage" / "images" / "upload.png").is_file()
    assert (restore / "vault" / "笔记.md").is_file()

    env2 = dict(env)
    env2.update({
        "DATABASE_URL": f"sqlite:///{(restore / 'db' / 'coursegenie.db').as_posix()}",
        "STORAGE_DIR": str(restore / "storage"),
        "EXPORT_DIR": str(restore / "storage" / "exports"),
        "PPT_WORK_DIR": str(restore / "storage" / "ppt"),
        "OBSIDIAN_VAULT_ROOT": str(restore / "vault"),
    })
    opened = _run_app(APP_PROBE, env2)
    assert opened["health"] == 200 and opened["courses"] == 200
    assert opened["llm_available"] is True
    assert "备份恢复用例-117" in opened["titles"]   # 库里的数据真的回来了


def test_key_never_appears_in_capability_response(client, monkeypatch):
    """③ 扫一遍 `/api/health` 返回值：只有变量名，没有 Key（连前缀都没有）。"""
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "llm_provider", "openrouter")
    monkeypatch.setattr(settings, "openrouter_api_key", CANARY_KEY)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert CANARY_KEY not in resp.text
    assert CANARY_KEY[:12] not in resp.text
    assert "OPENROUTER_API_KEY" in resp.text            # 变量名可以说
    llm_item = next(i for i in resp.json()["capabilities"]["items"] if i["key"] == "llm")
    assert llm_item["available"] is True and llm_item["needs_key"] == "OPENROUTER_API_KEY"
