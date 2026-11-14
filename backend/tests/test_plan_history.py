"""P3-1 回归测试：大纲历史版本（快照 / diff / 回滚）。

背景：同参两次生成，19 个节点只有 2 个标题相同；旧大纲被直接删掉，
没有对比与回滚。
"""
from __future__ import annotations


def _generate(client, course_id: str, *, num_chapters: int = 2, goal: str = "光合作用") -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": goal, "num_chapters": num_chapters})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _history(client, course_id: str) -> dict:
    resp = client.get(f"/api/plan/{course_id}/history")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_second_generation_keeps_the_first_version(client, course_id):
    first = _generate(client, course_id, num_chapters=2)
    assert _history(client, course_id)["versions"] == []

    _generate(client, course_id, num_chapters=3, goal="让初中生理解光合作用")
    report = _history(client, course_id)
    assert len(report["versions"]) == 1, report
    version = report["versions"][0]
    assert version["reason"] == "regenerate"
    assert version["params"]["num_chapters"] == 2
    assert version["params"]["goal"] == "光合作用"
    assert version["nodes"] == len(first["nodes"])
    assert version["at"] and version["id"]
    assert report["current"]["params"]["num_chapters"] == 3


def test_version_diff_reports_added_removed_and_renamed(client, course_id):
    _generate(client, course_id, num_chapters=2)
    version_id = _history(client, course_id)["versions"][0]["id"] if _history(
        client, course_id)["versions"] else None
    # 第一次生成后历史为空，再生成一次才有可对比的版本
    _generate(client, course_id, num_chapters=4)
    version_id = _history(client, course_id)["versions"][0]["id"]

    detail = client.get(f"/api/plan/{course_id}/history/{version_id}").json()
    assert detail["id"] == version_id
    diff = detail["diff"]
    assert diff["added"] or diff["removed"] or diff["renamed"], diff
    assert isinstance(diff["unchanged"], int)
    assert detail["impact"]["nodes"] >= 0


def test_restore_requires_confirmation_then_rolls_back(client, course_id):
    first = _generate(client, course_id, num_chapters=2)
    _generate(client, course_id, num_chapters=3)
    version_id = _history(client, course_id)["versions"][0]["id"]

    blocked = client.post(f"/api/plan/{course_id}/history/{version_id}/restore", json={})
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["impact"]["notice"]

    ok = client.post(f"/api/plan/{course_id}/history/{version_id}/restore",
                     json={"confirm": True})
    assert ok.status_code == 200, ok.text
    assert ok.json()["restored"] == version_id
    assert ok.json()["nodes"] == len(first["nodes"])

    plan = client.get(f"/api/plan/{course_id}").json()
    assert [n["key"] for n in plan["nodes"]] == [n["key"] for n in first["nodes"]]
    assert [n["title"] for n in plan["nodes"]] == [n["title"] for n in first["nodes"]]
    # 回滚后需重新「大纲通过」；且现版已存进历史，可再回滚
    after = _history(client, course_id)
    assert after["current"]["status"] == "planned"
    assert after["current"]["params"]["num_chapters"] == 2
    assert after["versions"][0]["reason"] == "before_restore"
    assert after["versions"][1]["id"] == version_id


def test_unknown_version_is_404(client, course_id):
    _generate(client, course_id)
    assert client.get(f"/api/plan/{course_id}/history/nope").status_code == 404
    assert client.post(f"/api/plan/{course_id}/history/nope/restore",
                       json={"confirm": True}).status_code == 404
