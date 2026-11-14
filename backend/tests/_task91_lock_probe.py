# -*- coding: utf-8 -*-
"""任务 91 行为级探针（**0 LLM**）：单篇「重跑这一节」看锁 + 覆盖即摘徽标。

全流程走真实 ASGI 请求路径（fastapi.testclient，与生产同一套路由/服务代码）：
造课 → 出大纲并 approve → 生成 1 篇讲义 → 手改正文 + 「审核通过」加锁（模拟真库包子课 1.1）
→ ㈠ 批量 force=true（必须 locked_skipped、正文逐字不变、锁与原因都还在）
→ ㈡ 单篇默认（必须 409，正文与锁都不动，历史不新增）
→ ㈢ 单篇显式 allow_overwrite_locked=true（正文换新版、locked=False、locked_reason 清掉、旧版进 history）
→ ㈣ 解锁后的单篇照旧能覆盖（不回归）
→ ㈤ 同族「按质检建议重生成」也不许留徽标。

安全：真库 / 真 vault **只做只读指纹**（本脚本一个字节都不写它们）；五个环境变量必须
全部指向临时目录（CG91_TMP），否则直接拒绝运行。

用法（前端断言脚本会调它）：
    python tests/_task91_lock_probe.py <repo> [--lecture-mut <path>]
stdout = 一行 JSON。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

REVIEWED_A = "## 老师审过的版本 A\n\n只留这一句，别的一概没写。\n"
REVIEWED_B = "## 老师审过的版本 B\n\n第二篇：复核过温度窗口。\n"
REVIEWED_C = "## 老师审过的版本 C\n\n第三篇：复核过发酵时长。\n"


def sha1(text: str) -> str:
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()


def dir_fp(path: Path) -> dict:
    """目录只读指纹（文件数 / 总字节 / 最大 mtime）——证明真 vault 没被动过。"""
    if not path.exists():
        return {"exists": False}
    files = 0
    total = 0
    newest = 0.0
    stack = [path]
    while stack:
        cur = stack.pop()
        for entry in os.scandir(cur):
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    st = entry.stat()
                    files += 1
                    total += st.st_size
                    newest = max(newest, st.st_mtime)
            except OSError:
                continue
    return {"exists": True, "files": files, "bytes": total, "newest": round(newest, 3)}


def db_fp(path: Path) -> dict:
    """真库只读指纹（mtime / 字节 / content_items 行数与正文总字节）。"""
    if not path.exists():
        return {"exists": False}
    st = path.stat()
    out = {"exists": True, "mtime": round(st.st_mtime, 3), "size": st.st_size}
    try:
        con = sqlite3.connect("file:" + path.as_posix() + "?mode=ro", uri=True)
        row = con.execute("select count(*), sum(length(body)) from content_items").fetchone()
        con.close()
        out["items"], out["bytes"] = int(row[0] or 0), int(row[1] or 0)
    except Exception as exc:  # noqa: BLE001 —— 只读指纹失败不算探针失败
        out["error"] = str(exc)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--lecture-mut", default="", help="把改坏了的 lecture.py 顶进来（行为级变异）")
    ap.add_argument("--real-vault", default="")
    a = ap.parse_args()
    repo = Path(a.repo).resolve()
    sys.path.insert(0, str(repo / "backend"))

    if a.lecture_mut:
        import importlib.util
        spec = importlib.util.spec_from_file_location("app.services.lecture", a.lecture_mut)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["app.services.lecture"] = mod
        spec.loader.exec_module(mod)

    from fastapi.testclient import TestClient
    from app.core.config import get_settings
    from app.main import app

    st = get_settings()
    env = {
        "database_url": str(st.database_url),
        "storage_dir": str(st.storage_dir),
        "vault_root": str(st.obsidian_vault_root or ""),
        "ppt_work_dir": str(st.ppt_work_dir),
        "export_dir": str(st.export_dir),
        "llm_provider": st.llm_provider,
    }
    tmp = os.environ.get("CG91_TMP", "")

    def under_temp(value: str) -> bool:
        """临时目录守卫：把 sqlite:/// 前缀与斜杠方向都归一化后再比（防手滑跑真库）。"""
        v = Path(str(value).replace("sqlite:///", "")).as_posix().lower()
        return bool(tmp) and v.startswith(Path(tmp).as_posix().lower())

    watched = [env["database_url"], env["storage_dir"], env["vault_root"],
               env["ppt_work_dir"], env["export_dir"]]
    if not tmp or any(not under_temp(v) for v in watched) or env["llm_provider"] != "mock":
        print(json.dumps({"error": "五件套没有全部指向临时目录 / LLM 不是 mock，拒绝运行",
                          "env": env, "CG91_TMP": tmp}, ensure_ascii=False))
        return 2

    real_db = repo / "backend" / "data" / "coursegenie.db"
    real_vault = Path(a.real_vault) if a.real_vault else Path("D:/CourseGenie知识库")
    real_before = {"db": db_fp(real_db), "vault_courses": dir_fp(real_vault / "课程")}
    out: dict = {"env": env, "real_before": real_before}

    with TestClient(app) as c:
        cid = c.post("/api/course/", json={"title": "任务91锁探针", "subject": "生物",
                                           "audience": "初中生", "duration_minutes": 90}).json()["id"]
        c.post("/api/plan/generate", json={"course_id": cid, "goal": "初中生物光合作用",
                                           "num_chapters": 3})
        c.post("/api/plan/approve", json={"course_id": cid, "approved": True})
        nodes = c.get(f"/api/plan/{cid}").json()["nodes"]
        out["node_keys"] = [n["key"] for n in nodes]

        def lecture_of(node_id: str) -> dict:
            rows = c.get(f"/api/content/{cid}").json()
            return next(r for r in rows if r["kind"] == "lecture" and r["node_id"] == node_id)

        def gen(node_id: str, **extra):
            payload = {"course_id": cid, "node_id": node_id, "tone": "正式"}
            payload.update(extra)
            resp = c.post("/api/lecture/generate", json=payload)
            jid = resp.json().get("job_id") if resp.status_code == 200 else ""
            return resp, jid

        def wait(job_id: str) -> dict:
            for _ in range(400):
                state = c.get(f"/api/content/jobs/{job_id}").json()
                if state["status"] in ("success", "failed"):
                    return state
                time.sleep(0.05)
            raise RuntimeError(f"job timeout: {job_id}")

        def reviewed(node_id: str, text: str) -> dict:
            """生成 → 手改正文（人工编辑自动加锁）→ 显式标成「审核通过」，返回落库后的行。"""
            _resp, jid = gen(node_id)
            item = wait(jid)["result"]["item"]
            assert c.patch(f"/api/lecture/{item['id']}",
                           json={"markdown": text}).status_code == 200
            lock = c.post(f"/api/lecture/{item['id']}/lock",
                          json={"locked": True, "reason": "审核通过"})
            assert lock.status_code == 200 and lock.json()["locked"] is True
            return lecture_of(node_id)

        # ㈠ 批量（force=true）不许碰已锁的那一节
        node1 = nodes[0]
        before = reviewed(node1["id"], REVIEWED_A)
        sha_before = sha1(before["body"]["markdown"])
        resp = c.post("/api/lecture/generate-all", json={
            "course_id": cid, "tone": "正式", "force": True, "ignore_style_anchor": True})
        job = wait(resp.json()["job_id"])
        rows = job["result"]["locked_skipped"]
        after = lecture_of(node1["id"])
        out["batch_force"] = {
            "http": resp.status_code, "job": job["status"],
            "locked_skipped": [r["node_key"] for r in rows],
            "skip_reason": rows[0]["reason"] if rows else "",
            "sha1_unchanged": sha1(after["body"]["markdown"]) == sha_before,
            "locked": after["meta"].get("locked"),
            "locked_reason": after["meta"].get("locked_reason"),
            "history": len(after["meta"].get("history") or []),
        }

        # ㈡ 单篇默认：409，正文/锁/历史都不许动
        resp = c.post("/api/lecture/generate", json={
            "course_id": cid, "node_id": node1["id"], "tone": "正式", "item_id": after["id"]})
        blocked = lecture_of(node1["id"])
        out["single_blocked"] = {
            "http": resp.status_code,
            "detail": str(resp.json().get("detail", "")) if resp.status_code == 409 else str(resp.text)[:200],
            "sha1_unchanged": sha1(blocked["body"]["markdown"]) == sha_before,
            "locked": blocked["meta"].get("locked"),
            "locked_reason": blocked["meta"].get("locked_reason"),
            "history": len(blocked["meta"].get("history") or []),
        }

        # ㈡b 不带 item_id 也一样（按节点找到的就是那篇已锁的）
        resp = c.post("/api/lecture/generate", json={
            "course_id": cid, "node_id": node1["id"], "tone": "正式"})
        out["single_blocked_no_item_id"] = {"http": resp.status_code}

        # ㈢ 显式覆盖：正文换新版 + 徽标摘掉
        resp, jid = gen(node1["id"], item_id=blocked["id"], allow_overwrite_locked=True)
        job = wait(jid)
        fresh = job["result"]["item"]
        row3 = lecture_of(node1["id"])
        hist = row3["meta"].get("history") or []
        out["single_overwrite"] = {
            "http": resp.status_code, "job": job["status"],
            "unlocked": job["result"].get("unlocked"),
            "unlock_message": job["result"].get("unlock_message", ""),
            "sha1_changed": sha1(fresh["body"]["markdown"]) != sha_before,
            "body_sha1": sha1(fresh["body"]["markdown"]),
            "locked": row3["meta"].get("locked"),
            "locked_key_present": "locked" in row3["meta"],
            "reason_key_present": "locked_reason" in row3["meta"],
            "history_len": len(hist),
            "history_has_reviewed": bool(hist) and sha1(hist[0]["body"]["markdown"]) == sha_before,
        }

        # ㈣ 未锁单篇照旧能覆盖（不回归）
        node2 = nodes[1]
        row_b = reviewed(node2["id"], REVIEWED_B)
        c.post(f"/api/lecture/{row_b['id']}/lock", json={"locked": False})
        resp, jid = gen(node2["id"], item_id=row_b["id"])
        job = wait(jid) if jid else {"status": "", "result": {}}
        row_b2 = lecture_of(node2["id"])
        out["unlocked_single_rerun"] = {
            "http": resp.status_code, "job": job["status"],
            "unlocked": job["result"].get("unlocked"),
            "unlock_message": job["result"].get("unlock_message", ""),
            "sha1_changed": sha1(row_b2["body"]["markdown"]) != sha1(REVIEWED_B),
            "locked": row_b2["meta"].get("locked"),
            "reason_key_present": "locked_reason" in row_b2["meta"],
        }

        # ㈤ 同族：按质检建议重生成也不许把徽标留着
        node3 = nodes[2]
        row_c = reviewed(node3["id"], REVIEWED_C)
        resp = c.post(f"/api/lecture/{row_c['id']}/regenerate-qc", json={"tone": "正式"})
        job = wait(resp.json()["job_id"]) if resp.status_code == 200 else {"status": ""}
        row_c2 = lecture_of(node3["id"])
        out["qc_regenerate"] = {
            "http": resp.status_code, "job": job["status"],
            "sha1_changed": sha1(row_c2["body"]["markdown"]) != sha1(REVIEWED_C),
            "locked": row_c2["meta"].get("locked"),
            "reason_key_present": "locked_reason" in row_c2["meta"],
        }

    out["real_after"] = {"db": db_fp(real_db), "vault_courses": dir_fp(real_vault / "课程")}
    out["real_db_unchanged"] = out["real_before"]["db"] == out["real_after"]["db"]
    out["real_vault_unchanged"] = out["real_before"]["vault_courses"] == out["real_after"]["vault_courses"]
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())