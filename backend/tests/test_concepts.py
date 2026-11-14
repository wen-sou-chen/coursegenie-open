"""概念库（原子笔记 v1）测试：一概念一篇、幂等、双链 0 坏链、来源可追溯、手改保留。"""
from __future__ import annotations

import json
import re
import tempfile
import time
from pathlib import Path

from app.services.llm import BaseLLMClient, MockLLMClient
from app.services import concept as concept_service


def _root() -> Path:
    return Path(tempfile.mkdtemp(prefix="coursegenie_concepts_"))


def _bind(client, course_id: str, root: Path, out_rel: str = "课程/光合作用") -> None:
    resp = client.post(f"/api/kb/course/{course_id}/bind", json={
        "vault_root": str(root), "output_rel": out_rel,
    })
    assert resp.status_code == 200, resp.text


def _plan_and_approve(client, course_id: str, num_chapters: int = 2) -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "让初中生理解光合作用", "num_chapters": num_chapters,
    })
    assert resp.status_code == 200, resp.text
    approved = client.post("/api/plan/approve", json={"course_id": course_id, "approved": True})
    assert approved.status_code == 200, approved.text
    return resp.json()


def _wait_job(client, job_id: str, timeout: float = 30.0) -> dict:
    state: dict | None = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = client.get(f"/api/concept/jobs/{job_id}").json()
        if state["status"] in ("success", "failed"):
            return state
        time.sleep(0.1)
    raise AssertionError(f"任务超时未结束：{state}")


def _generate(client, course_id: str) -> dict:
    resp = client.post(f"/api/concept/course/{course_id}/generate")
    assert resp.status_code == 200, resp.text
    queued = resp.json()
    assert queued["status"] == "running", queued
    state = _wait_job(client, queued["job_id"])
    assert state["status"] == "success", state
    return state["result"]


def _concepts(client, course_id: str) -> dict:
    resp = client.get(f"/api/concept/course/{course_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _links(text: str) -> list[str]:
    return [m.strip() for m in re.findall(r"(?<!!)\[\[([^\]|#]+)", text)]


def _chapter_pages(out: Path) -> list[Path]:
    return [p for d in out.iterdir() if d.is_dir() and d.name[:2].isdigit() for p in d.glob("*.md")]


def _concept_pages(out: Path) -> list[Path]:
    """概念库里的概念页：``_`` 开头的是课程级产出（如 ``_图谱.md``），不算概念页。

    与 ``kb.course_asset_health`` 的 ``concept_pages`` 口径一致（任务 24-A 起概念库里
    多了 ``_图谱.md`` / ``_图谱.canvas``）。
    """
    return sorted(p for p in (out / "概念库").glob("*.md") if not p.name.startswith("_"))


def test_concept_generation_is_idempotent(client, course_id):
    """重复生成不重复建篇，业务 id 稳定（M5 意见挂载 / 版本 diff 的前向兼容）。"""
    _plan_and_approve(client, course_id)
    first = _generate(client, course_id)
    assert first["created"] == first["concepts"] > 0
    before = _concepts(client, course_id)["concepts"]
    ids_before = {c["name"]: c["concept_id"] for c in before}
    assert all(str(cid).startswith("cpt-") for cid in ids_before.values()), ids_before

    second = _generate(client, course_id)
    after = _concepts(client, course_id)["concepts"]
    assert second["created"] == 0 and second["removed"] == 0, second
    assert len(after) == len(before)
    assert {c["name"]: c["concept_id"] for c in after} == ids_before


def test_cross_chapter_concept_merges_chapters_only(client, course_id):
    """跨章复用的概念只追加 chapters，仍然只有一篇（单一真相源）。"""
    _plan_and_approve(client, course_id, num_chapters=3)
    _generate(client, course_id)
    rows = _concepts(client, course_id)["concepts"]
    shared = [c for c in rows if c["name"] == "发酵"]
    assert len(shared) == 1, [c["name"] for c in rows]
    assert shared[0]["chapters"] == ["1", "2", "3"]
    assert "fermentation" in shared[0]["aliases"]
    assert len({c["name"] for c in rows}) == len(rows)


def test_export_concept_pages_have_zero_broken_links(client, course_id):
    root = _root()
    _bind(client, course_id, root)
    _plan_and_approve(client, course_id)
    _generate(client, course_id)
    content_job = client.post("/api/content/generate", json={"course_id": course_id}).json()["job_id"]
    assert client.get(f"/api/content/jobs/{content_job}").json()["status"] == "success"

    resp = client.post(f"/api/kb/course/{course_id}/export")
    assert resp.status_code == 200, resp.text
    out = root / resp.json()["output_rel"]
    pages = _concept_pages(out)
    assert pages, "导出应生成 概念库/ 目录"
    stems = {q.stem for q in out.rglob("*.md")}  # 双链目标必须是真实存在的笔记文件名
    for page in pages:
        text = page.read_text(encoding="utf-8")
        assert 'type: "concept"' in text
        assert "cg_fingerprint" in text
        assert "## 来源" in text
        for target in _links(text):
            assert target in stems, f"{page.name} 坏链：{target}"

    assert _concepts(client, course_id)["stats"]["broken_links"] == 0

    chapter = [p for p in _chapter_pages(out) if "本章概念索引" in p.read_text(encoding="utf-8")]
    assert chapter, "章节页底部应生成「本章概念索引」"
    for page in chapter:
        for target in _links(page.read_text(encoding="utf-8")):
            assert target in stems, f"{page.name} 坏链：{target}"


def test_manual_edit_concept_page_is_preserved(client, course_id):
    root = _root()
    _bind(client, course_id, root)
    _plan_and_approve(client, course_id)
    _generate(client, course_id)
    first = client.post(f"/api/kb/course/{course_id}/export").json()
    out = root / first["output_rel"]
    page = _concept_pages(out)[0]
    rel = page.relative_to(root).as_posix()
    page.write_text(page.read_text(encoding="utf-8") + "\n<!-- teacher manual edit -->\n",
                    encoding="utf-8")

    second = client.post(f"/api/kb/course/{course_id}/export").json()
    kept = {(p["file"], p["reason"]) for p in second["preserved"]}
    assert (rel, "manual-edit") in kept, second["preserved"]
    assert "teacher manual edit" in page.read_text(encoding="utf-8")

    third = client.post(f"/api/kb/course/{course_id}/export?force=true").json()
    assert not third["preserved"]
    assert "teacher manual edit" not in page.read_text(encoding="utf-8")


def test_edited_concept_survives_regeneration(client, course_id):
    """网页端手改过的概念，重抽时保留不覆盖。"""
    _plan_and_approve(client, course_id)
    _generate(client, course_id)
    row = _concepts(client, course_id)["concepts"][0]
    patched = client.patch(f"/api/concept/{row['id']}", json={"definition": "教师改写的定义"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["meta"]["edited"] is True

    again = _generate(client, course_id)
    assert again["kept_edited"] >= 1, again
    rows = {c["id"]: c for c in _concepts(client, course_id)["concepts"]}
    assert rows[row["id"]]["body"]["definition"] == "教师改写的定义"


def test_web_edit_concept_relations(client, course_id):
    """网页端可直接改「关系」（任务 24-补）：立刻生效、去重去空、清空某一类也生效、并标记人工编辑。"""
    _plan_and_approve(client, course_id)
    _generate(client, course_id)
    rows = _concepts(client, course_id)["concepts"]
    assert len(rows) >= 2
    target, other = rows[0], rows[1]
    before = dict(target["body"].get("relations") or {})

    patched = client.patch(f"/api/concept/{target['id']}", json={
        "relations": {"broader": [other["name"], other["name"], "   "], "related": []},
    })
    assert patched.status_code == 200, patched.text
    relations = patched.json()["body"]["relations"]
    assert relations["broader"] == [other["name"]], relations          # 去重 + 去空白
    assert relations["related"] == [], relations                       # 显式清空生效
    assert patched.json()["meta"]["edited"] is True

    # 未在请求里出现的类型保持原样
    assert relations.get("narrower") == before.get("narrower")

    rows_after = {c["id"]: c for c in _concepts(client, course_id)["concepts"]}
    assert rows_after[target["id"]]["body"]["relations"]["broader"] == [other["name"]]

    # 改完关系后图谱仍能生成，且节点数 = 概念数
    graph = client.post(f"/api/concept/course/{course_id}/graph")
    assert graph.status_code == 200, graph.text
    assert graph.json()["stats"]["nodes"] == len(rows)


def test_concept_sources_are_traceable(client, course_id):
    """每条来源都能落到真实存在的讲义 / 大纲 / 素材，且导出页上的来源双链 0 坏链。"""
    root = _root()
    _bind(client, course_id, root)
    _plan_and_approve(client, course_id)
    _generate(client, course_id)
    content_job = client.post("/api/content/generate", json={"course_id": course_id}).json()["job_id"]
    assert client.get(f"/api/content/jobs/{content_job}").json()["status"] == "success"
    node_keys = {n["key"] for n in client.get(f"/api/plan/{course_id}").json()["nodes"]}

    rows = _concepts(client, course_id)["concepts"]
    assert rows
    for row in rows:
        sources = row["body"]["sources"]
        assert sources, f"{row['name']} 缺少来源"
        for src in sources:
            assert src["kind"] in {"lecture", "outline", "material"}
            assert src["ref"] in node_keys, src
            assert row["chapters"], f"{row['name']} 未关联章节"

    out = root / client.post(f"/api/kb/course/{course_id}/export").json()["output_rel"]
    stems = {q.stem for q in out.rglob("*.md")}
    for page in _concept_pages(out):
        text = page.read_text(encoding="utf-8")
        linked = [ln for ln in text.splitlines() if "讲义" in ln or "大纲" in ln]
        assert any("[[" in ln for ln in linked), f"{page.name} 来源没有双链：{linked}"
        for target in _links(text):
            assert target in stems, f"{page.name} 坏链：{target}"


def test_render_concept_structure_and_figures():
    """一篇原子笔记的完整结构：frontmatter + 定义 + 要点 + 关系 + 误区 + 图示 + 来源。"""
    from app.models.course import ContentItem, Course

    figures = {
        "1": ["图-流程图-1-01.svg", "图-思维导图-1-01.svg"],
        "2": ["图-柱状图-2-01.svg"],
    }
    meta = {"chapters": ["1", "2"], "aliases": ["chloroplast"], "concept_id": "cpt-x"}
    picked = concept_service.concept_figures(meta, figures)
    assert picked[0] == "图-思维导图-1-01.svg", picked      # 思维导图优先
    assert len(picked) <= concept_service.MAX_FIGURES

    course = Course(id="c1", title="光合作用入门", subject="生物",
                    audience="初中生", duration_minutes=90)
    row = ContentItem(
        id="i1", course_id="c1", kind="concept", title="叶绿体",
        body={
            "definition": "光合作用发生的细胞器。",
            "points": ["含叶绿素", "吸收光能", "放出氧气"],
            "relations": {"broader": ["细胞器"], "narrower": ["类囊体"], "related": ["发酵"]},
            "pitfalls": ["把它当成细胞核"],
            "sources": [{"kind": "outline", "ref": "1", "quote": "叶绿体"}],
        },
        meta=meta,
    )
    text = concept_service.render_concept(
        course, row, node_stems={"1": "1-第一章"}, material_pages={},
        figures=figures,
        resolve=lambda name: {"细胞器": "细胞器", "类囊体": "类囊体", "发酵": "发酵"}.get(name),
    )
    for heading in ("## 一句话定义", "## 要点", "## 概念关系", "## 常见误区", "## 图示", "## 来源"):
        assert heading in text, heading
    assert 'type: "concept"' in text and 'aliases: ["chloroplast"]' in text
    assert 'concept_id: "cpt-x"' in text
    for line in ("- 上位：[[细胞器]]", "- 下位：[[类囊体]]", "- 相关：[[发酵]]"):
        assert line in text, line
    assert "![[图-思维导图-1-01.svg]]" in text
    assert "大纲：[[1-第一章]]" in text

def test_merge_raw_drops_untraceable_sources():
    """来源校验：非法 ref / 非法 kind 一律丢弃，合法素材来源保留。"""
    corpus = {
        "nodes": [
            {"key": "1.1", "title": "光合作用", "chapter": "1"},
            # 任务 41：概念要跨 ≥2 个知识点才升级为概念页 → 这里 1.2 提到叶绿体
            {"key": "1.2", "title": "光合作用的场所", "chapter": "1",
             "points": ["叶绿体含叶绿素", "叶绿体能吸收光能"]},
        ],
        "materials": [{"title": "光合作用讲义"}],
    }
    raw = [
        {
            "name": "叶绿体",
            "definition": "光合作用发生的细胞器。",
            "points": ["含叶绿素", "能吸收光能", "产生氧气"],
            "chapters": ["1.1", "9.9"],
            "sources": [
                {"kind": "lecture", "ref": "1.1", "quote": "叶绿体"},
                {"kind": "lecture", "ref": "9.9", "quote": "不存在"},
                {"kind": "material", "ref": "光合作用讲义", "quote": "叶绿体"},
                {"kind": "material", "ref": "不存在", "quote": ""},
                {"kind": "bogus", "ref": "1.1", "quote": ""},
            ],
        },
        {"name": "叶绿体", "aliases": ["chloroplast"], "chapters": ["1.1"]},
    ]
    out = concept_service.merge_raw(raw, corpus)
    assert len(out) == 1
    assert out[0]["chapters"] == ["1.1", "1.2"]
    assert out[0]["aliases"] == ["chloroplast"]
    refs = {(s["kind"], s["ref"]) for s in out[0]["sources"]}
    assert ("lecture", "1.1") in refs
    assert ("material", "光合作用讲义") in refs
    assert ("lecture", "9.9") not in refs
    assert ("material", "不存在") not in refs


def test_extraction_failure_is_reported(client, course_id, monkeypatch):
    """LLM 失败必须显式上报：任务 failed + error 明确，且不写入空概念库。"""
    _plan_and_approve(client, course_id)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("LLM 密钥缺失")

    monkeypatch.setattr(concept_service, "build_llm_client", _boom)
    resp = client.post(f"/api/concept/course/{course_id}/generate")
    assert resp.status_code == 200, resp.text
    state = _wait_job(client, resp.json()["job_id"])
    assert state["status"] == "failed", state
    assert "LLM 调用失败" in (state["error"] or "")
    assert _concepts(client, course_id)["count"] == 0


def test_bad_json_failure_reason_carries_position_and_context(client, course_id, monkeypatch):
    """任务 63：LLM 返回坏 JSON 时，job 的失败原因里要能看到位置与现场片段。"""
    _plan_and_approve(client, course_id)

    class _BadJSONClient(BaseLLMClient):
        name = "bad-json"

        def complete(self, system: str, user: str, *, max_tokens: int = 2000) -> str:
            return '{"concepts": [{"name": "光合作用" "aliases": []}]}'

    monkeypatch.setattr(concept_service, "build_llm_client", lambda: _BadJSONClient())
    resp = client.post(f"/api/concept/course/{course_id}/generate")
    assert resp.status_code == 200, resp.text
    state = _wait_job(client, resp.json()["job_id"])
    assert state["status"] == "failed", state
    error = state["error"] or ""
    assert "LLM 返回了坏 JSON" in error
    assert "位置" in error and "现场" in error and "光合作用" in error
    assert _concepts(client, course_id)["count"] == 0


def test_generate_requires_approved_outline(client, course_id):
    resp = client.post(f"/api/concept/course/{course_id}/generate")
    assert resp.status_code == 409


def test_quality_filter_rejects_titles_sentences_and_stopwords():
    """概念质量约束：章节/知识点标题同名、像句子、停用词、超长一律不是概念。"""
    corpus = {
        "nodes": [
            {"key": "1", "title": "发酵原理与微生物基础", "chapter": "1"},
            {"key": "1.1", "title": "面筋网络与发酵对象", "chapter": "1",
             "points": ["乳酸菌在厌氧环境下产酸"]},
            {"key": "1.2", "title": "温度与发酵的关系", "chapter": "1",
             "points": ["乳酸菌在 30℃ 左右最活跃"]},
        ],
        "materials": [],
    }
    sentence = "发酵其实是一个很复杂的过程，需要控制温度和湿度"
    raw = [
        {"name": "发酵原理与微生物基础", "chapters": ["1"]},            # 章标题
        {"name": "面筋网络与发酵对象", "chapters": ["1.1"]},            # 知识点标题
        {"name": sentence, "chapters": ["1.1"]},                        # 句子
        {"name": "知识点", "chapters": ["1.1"]},                        # 停用词
        {"name": "面筋网络过度氧化的影响因素", "chapters": ["1.1"]},      # 超长
        {"name": "乳酸菌", "aliases": ["LAB"], "definition": "发酵的主力菌",
         "points": ["厌氧环境产酸", "受温度影响"], "chapters": ["1.1"]},
        {"name": "酵母菌", "chapters": ["1.1"]},                        # 只出现 1 个知识点
    ]
    dropped: list[dict] = []
    out = concept_service.merge_raw(raw, corpus, dropped)
    assert [c["name"] for c in out] == ["乳酸菌"]
    # 跨 2 个知识点（LLM 只报了 1.1，语料证据补上 1.2）
    assert out[0]["chapters"] == ["1.1", "1.2"]
    reasons = {d["name"]: d["reason"] for d in dropped}
    assert reasons["发酵原理与微生物基础"] == "与章节/知识点标题同名"
    assert reasons["面筋网络与发酵对象"] == "与章节/知识点标题同名"
    assert "句子" in reasons[sentence]
    assert "停用词" in reasons["知识点"]
    assert "过长" in reasons["面筋网络过度氧化的影响因素"]
    assert "1 个知识点" in reasons["酵母菌"]
    assert [t["name"] for t in concept_service.terminology_entries(dropped)] == ["酵母菌"]


def test_concept_admission_requires_two_knowledge_points():
    """任务 41 准入：只在 1 个知识点出现过的名字不建概念页，记 meta.terminology。"""
    corpus = {
        "nodes": [
            {"key": "1.1", "title": "发酵原理", "chapter": "1",
             "points": ["乳酸菌在厌氧环境产酸"]},
            {"key": "1.2", "title": "面团的变化", "chapter": "1",
             "points": ["乳酸菌让面团变酸", "面筋网络被撑开"]},
        ],
        "materials": [],
    }
    raw = [
        {"name": "乳酸菌", "definition": "发酵的主力菌", "chapters": ["1.1"]},
        {"name": "面筋网络", "definition": "面团的骨架", "chapters": ["1.2"]},
    ]
    dropped: list[dict] = []
    out = concept_service.merge_raw(raw, corpus, dropped)

    assert [c["name"] for c in out] == ["乳酸菌"]
    assert out[0]["chapters"] == ["1.1", "1.2"]
    terminologies = concept_service.terminology_entries(dropped)
    assert [t["name"] for t in terminologies] == ["面筋网络"]
    assert terminologies[0]["chapters"] == ["1.2"]
    assert terminologies[0]["definition"] == "面团的骨架"


def test_supplement_bypasses_admission():
    """老师点名补抽的概念不受「跨 ≥2 个知识点」限制（人工确认优先于自动准入）。"""
    corpus = {"nodes": [{"key": "1.1", "title": "发酵原理", "chapter": "1"}], "materials": []}
    raw = [{"name": "酵母菌", "definition": "单细胞真菌", "chapters": ["1.1"]}]
    assert concept_service.merge_raw(raw, corpus) == []
    out = concept_service.merge_raw(raw, corpus, enforce_admission=False)
    assert [c["name"] for c in out] == ["酵母菌"]


class _ConceptStub(MockLLMClient):
    """只返回指定概念（其余调用走 mock），用于验证准入结论真的落库。"""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def complete(self, system: str, user: str, *, max_tokens: int = 2000) -> str:
        if "CONCEPT_JSON" not in system:
            return super().complete(system, user, max_tokens=max_tokens)
        return json.dumps({"concepts": self._rows}, ensure_ascii=False)


def test_single_point_terms_land_in_course_meta_terminology(client, course_id, monkeypatch):
    """只出现一次的术语不建概念页，但要记进 ``course.meta.terminology``（可追溯、不重复）。"""
    _plan_and_approve(client, course_id, num_chapters=2)
    rows = [
        {"name": "发酵", "definition": "微生物分解有机物", "chapters": ["1", "2"]},
        {"name": "面筋网络", "definition": "面团的骨架", "chapters": ["1"]},
    ]
    monkeypatch.setattr(concept_service, "build_llm_client", lambda: _ConceptStub(rows))

    result = _generate(client, course_id)
    assert result["names"] == ["发酵"] and result["concepts"] == 1
    assert result["terminology"] == ["面筋网络"] and result["terminology_count"] == 1
    assert "meta.terminology" in next(d["reason"] for d in result["filtered"]
                                      if d["name"] == "面筋网络")

    course = client.get(f"/api/course/{course_id}").json()
    terminology = course["meta"]["terminology"]
    assert [row["name"] for row in terminology] == ["面筋网络"]
    assert terminology[0]["chapters"] == ["1"] and terminology[0]["first_seen"]

    # 再抽一次：同名术语只留一条，不重复登记
    _generate(client, course_id)
    again = client.get(f"/api/course/{course_id}").json()["meta"]["terminology"]
    assert [row["name"] for row in again] == ["面筋网络"]


def test_mock_extraction_never_uses_node_titles_as_concepts(client, course_id):
    """集成：抽取结果里不能出现与章节/知识点标题同名的概念。"""
    _plan_and_approve(client, course_id)
    result = _generate(client, course_id)
    titles = {n["title"] for n in client.get(f"/api/plan/{course_id}").json()["nodes"]}
    assert result["names"], result
    assert not (set(result["names"]) & titles), result["names"]
    assert isinstance(result["filtered"], list)


def test_prefixed_relation_targets_resolve_or_stay_plain():
    """带章号前缀的关系目标（1-发酵原理与微生物基础）解析到真实文件名；解析不到不写链接。"""
    norm = concept_service.normalize_key
    index = {norm("发酵原理与微生物基础"): "发酵原理与微生物基础", norm("乳酸菌"): "乳酸菌"}
    assert concept_service.resolve_target("1-发酵原理与微生物基础", index) == "发酵原理与微生物基础"
    assert concept_service.resolve_target("1.2-乳酸菌", index) == "乳酸菌"
    assert concept_service.resolve_target("第 3 章 乳酸菌", index) == "乳酸菌"
    assert concept_service.resolve_target("《乳酸菌》", index) == "乳酸菌"
    assert concept_service.resolve_target("3D打印", {norm("3D打印"): "3D打印"}) == "3D打印"
    assert concept_service.resolve_target("库里没有的概念", index) is None


def test_relation_links_resolve_to_real_files_with_chapter_prefix(client, course_id):
    """导出后：带章号前缀的关系目标必须指向真实概念页，关系坏链数为 0。"""
    from app.db.session import SessionLocal
    from app.models.course import ContentItem

    root = _root()
    _bind(client, course_id, root)
    _plan_and_approve(client, course_id)
    _generate(client, course_id)
    content_job = client.post("/api/content/generate", json={"course_id": course_id}).json()["job_id"]
    assert client.get(f"/api/content/jobs/{content_job}").json()["status"] == "success"

    with SessionLocal() as db:
        rows = (db.query(ContentItem)
                .filter(ContentItem.course_id == course_id, ContentItem.kind == "concept")
                .order_by(ContentItem.created_at).all())
        assert len(rows) >= 2, [r.title for r in rows]
        target_title = rows[1].title
        body = dict(rows[0].body or {})
        body["relations"] = {"broader": [f"1-{target_title}"], "narrower": [],
                             "related": ["库里没有的概念"]}
        rows[0].body = body
        db.commit()

    stats = _concepts(client, course_id)["stats"]
    assert stats["broken_links"] == 0, stats
    assert stats["unresolved_relations"] == 1, stats
    assert stats["unresolved"][0]["target"] == "库里没有的概念"

    out = root / client.post(f"/api/kb/course/{course_id}/export").json()["output_rel"]
    stems = {q.stem for q in out.rglob("*.md")}
    resolved = False
    for page in _concept_pages(out):
        text = page.read_text(encoding="utf-8")
        relations = [ln for ln in text.splitlines() if ln.startswith(("上位：", "下位：", "相关："))]
        assert not any("[[1-" in ln for ln in relations), f"{page.name} 关系里仍有带章号前缀的坏链"
        if f"[[{target_title}]]" in text:
            resolved = True
        for link in _links(text):
            assert link in stems, f"{page.name} 坏链：{link}"
    assert resolved, f"带章号前缀的关系目标应解析成 [[{target_title}]]"
