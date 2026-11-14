"""任务 48v：``GET /api/course/``（列表）不再返回导出清单 ``meta.kb``，详情与 kb 接口照旧完整。

背景（DSH 2026-09-17 字节级实测）：``meta.kb.manifest``（逐文件 sha1）+ ``meta.kb.incr``（增量游标）
把列表响应撑到 732 KB —— AI 课一门就 568.6 KB，而列表页只用到
``id/title/subject/audience/duration_minutes/status/k12``，其余 99% 是白下的。
列表走 ``CourseListItem`` 投影（整块去掉 ``meta.kb``），``meta`` 其它键照旧。
"""
from __future__ import annotations

import json

MANIFEST_ENTRIES = 300  # 每条约 110 B ⇒ manifest + incr 合计约 66 KB，足够撑爆 20 KB 目标


def _heavy_meta(entries: int = MANIFEST_ENTRIES) -> dict:
    """造一门「带大导出清单」的课：kb 里有 manifest + incr，另带列表页可能用到的其它 meta 键。"""
    rels = [f"课程/演示课/01-第一章/1.{i}-一个挺长的知识点标题.md" for i in range(entries)]
    return {
        "k12": {"stage": "小学", "grade": "二年级", "textbook": "部编版"},
        "style_anchor": "口语化、例子多",
        "terminology": {"幻觉": "模型一本正经地胡说"},
        "kb": {
            "vault_root": "D:\\DemoVault",
            "output_rel": "课程/演示课",
            "exported_at": "2026-09-19T09:03:19+00:00",
            "synced_at": "2026-09-17T07:18:22+00:00",
            "manifest": {rel: f"{i:040d}" for i, rel in enumerate(rels)},
            "incr": {rel: f"{i:040x}" for i, rel in enumerate(rels)},
        },
    }


def _create(client, title: str, meta: dict) -> str:
    resp = client.post("/api/course/", json={
        "title": title, "subject": "语文", "audience": "小学生",
        "duration_minutes": 40, "meta": meta})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _list_item(client, course_id: str) -> dict:
    resp = client.get("/api/course/")
    assert resp.status_code == 200, resp.text
    return next(row for row in resp.json() if row["id"] == course_id)


# ① 列表：没有 meta.kb，但 meta 的其它键一个不少
def test_list_drops_kb_keeps_other_meta_keys(client, course_id):
    assert "kb" not in _list_item(client, course_id)["meta"]

    cid = _create(client, "48v-其它键", _heavy_meta(entries=3))
    meta = _list_item(client, cid)["meta"]
    assert "kb" not in meta, "列表不该回导出清单"
    assert meta["style_anchor"] == "口语化、例子多"
    assert meta["terminology"] == {"幻觉": "模型一本正经地胡说"}
    assert meta["k12"]["stage"] == "小学"


# ② 列表：k12 归一化字段与列表页要用到的列照旧
def test_list_columns_and_k12_still_present(client):
    cid = _create(client, "48v-列齐全", _heavy_meta(entries=3))
    item = _list_item(client, cid)
    assert (item["title"], item["subject"], item["audience"]) == ("48v-列齐全", "语文", "小学生")
    assert item["duration_minutes"] == 40
    assert item["status"] == "draft"
    assert item["k12"] == {"stage": "小学", "grade": "二年级", "textbook": "部编版"}


# ③ 详情：完整 meta.kb 照旧（面板要用）
def test_detail_keeps_full_kb(client):
    cid = _create(client, "48v-详情完整", _heavy_meta(entries=5))
    resp = client.get(f"/api/course/{cid}")
    assert resp.status_code == 200, resp.text
    kb = resp.json()["meta"]["kb"]
    assert len(kb["manifest"]) == 5 and len(kb["incr"]) == 5
    assert kb["vault_root"] == "D:\\DemoVault"
    assert kb["synced_at"] == "2026-09-17T07:18:22+00:00"


# ④ 专门的 kb 接口不动
def test_kb_endpoint_still_serves_manifest(client):
    cid = _create(client, "48v-kb接口", _heavy_meta(entries=4))
    resp = client.get(f"/api/kb/course/{cid}")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["kb"]["manifest"]) == 4


# ⑤ 体量验收：一门带 66 KB 导出清单的课，在列表里只占几百字节（旧码 ~70 KB）
def test_list_entry_stays_under_20kb_while_detail_stays_heavy(client):
    """口径说明：测试库是**会话级共享**的（别的用例也在建课，整份响应会累积几百门），
    所以不断言「整份响应 < 20 KB」，改量这门课给列表带来的**增量** —— 与真实部署同一个口径，
    且不受用例执行顺序影响。真实部署的整份响应字节数见 ``docs/task48v-course-list-payload.md``。
    """
    before = len(client.get("/api/course/").content)

    cid = _create(client, "48v-大清单", _heavy_meta())
    detail_bytes = len(client.get(f"/api/course/{cid}").content)
    assert detail_bytes > 20 * 1024, f"夹具不够重（详情才 {detail_bytes} B），列表断言没意义"

    resp = client.get("/api/course/")
    assert resp.status_code == 200, resp.text
    item = next(row for row in resp.json() if row["id"] == cid)
    dumped = json.dumps(item, ensure_ascii=False)
    item_bytes = len(dumped.encode("utf-8"))
    growth = len(resp.content) - before

    assert "kb" not in item["meta"]
    assert "manifest" not in dumped and "incr" not in dumped
    assert item_bytes < 20 * 1024, f"这门课在列表里占了 {item_bytes} B"
    assert growth < 20 * 1024, f"加这门课后列表响应涨了 {growth} B（旧码这里是 6.7 万 B 量级）"
