"""任务 48w：LaTeX 单反斜杠被 JSON 吃掉 ⇒ 公式损坏 + Word 交付整篇失败。

三层修复（规格见 `AI协作区\\HANDOFF.md` 文末「任务 48w 说明」）：

1. ``llm.extract_json`` 预解析修转义 —— ``\\frac`` 不再变 U+000C、``\\alpha``/``\\times``
   不再抛错，真换行/真制表原样保留；
2. ``docx_export`` 渲染前把 XML 不允许的控制字符换成可见占位（␌，U+2400 区），
   单篇不再整篇失败；
3. 解析结果里有控制字符就记 warning（带 purpose）并写进 ``meta.content_issues``，
   **不加第 11 项判据、不动 CHECK_KEYS/_WEIGHT/QC_VERSION**。
"""
from __future__ import annotations

import logging

from app.services import docx_export
from app.services import lecture as lecture_mod
from app.services import usage as usage_log
from app.services.llm import extract_json

# 模型偷懒版的原始输出：JSON 文本里是**单反斜杠**（`\f` = 合法转义 ⇒ 换页符）。
RAW_LECTURE_JSON = (
    '{"markdown": "## 分数的意义\\n\\n把一张饼平均分成 2 份，每份是 $\\frac{1}{2}$。\\n\\n'
    '## 小结\\n- 分子在上、分母在下。", "summary": "分数的初步认识", '
    '"keywords": ["分数", "分子", "分母"], "quizzes": []}'
)


def _plan(client, course_id: str) -> dict:
    resp = client.post("/api/plan/generate", json={
        "course_id": course_id, "goal": "小学分数初步认识", "num_chapters": 1,
    })
    assert resp.status_code == 200, resp.text
    assert client.post("/api/plan/approve", json={
        "course_id": course_id, "approved": True}).status_code == 200
    return resp.json()


def _node(plan: dict) -> str:
    leaf = next((n for n in plan["nodes"] if "." in n["key"]), None)
    return (leaf or plan["nodes"][0])["id"]


class _RawJsonLLM:
    """只回原始文本：讲义正文走完整的「LLM 文本 → extract_json」链路。"""

    name = "raw-json"

    def __init__(self, text: str) -> None:
        self.text = text

    def complete(self, system: str, user: str, *, max_tokens: int = 2000) -> str:
        return self.text

    def complete_json(self, system: str, user: str, *, max_tokens: int = 2000) -> dict:
        return extract_json(self.text)


class _ControlCharLLM:
    """直接返回带真控制字符的解析结果（模拟历史内容 / 别的来源混进来）。"""

    name = "ctrl-char"

    def complete_json(self, system: str, user: str, *, max_tokens: int = 2000) -> dict:
        return {
            "markdown": "## 分数\n\n本该是 $\\frac{1}{2}$，但这里已经是被吃掉的换页符：\x0c 结束。",
            "summary": "摘要",
            "keywords": [],
            "quizzes": [],
        }


class _CleanLLM:
    name = "clean"

    def complete_json(self, system: str, user: str, *, max_tokens: int = 2000) -> dict:
        return {"markdown": "## 干净\n\n正文。", "summary": "摘要", "keywords": [], "quizzes": []}


def test_lecture_keeps_latex_from_raw_llm_json(client, course_id, gen_lecture_row, monkeypatch):
    """模型写单反斜杠的 `\\frac`：正文里保留字面 LaTeX，且一处 U+000C 都没有。"""
    plan = _plan(client, course_id)
    monkeypatch.setattr(lecture_mod, "build_llm_client", lambda: _RawJsonLLM(RAW_LECTURE_JSON))
    row = gen_lecture_row(course_id, _node(plan))

    md = str(row["body"]["markdown"])
    assert "$\\frac{1}{2}$" in md, md
    assert "\x0c" not in md, "单反斜杠的 \\frac 被吃成换页符了"
    assert row["meta"]["content_issues"] == [], row["meta"]["content_issues"]
    # Word 侧拿到的是真 LaTeX 文本，不再是控制字符
    assert "\\frac{1}{2}" in docx_export.docx_text(docx_export.markdown_to_docx(md))


def test_lecture_records_content_issues_for_real_control_chars(client, course_id,
                                                              gen_lecture_row, monkeypatch):
    """正文真残留控制字符时：写进 meta.content_issues（只记不判）。"""
    plan = _plan(client, course_id)
    monkeypatch.setattr(lecture_mod, "build_llm_client", lambda: _ControlCharLLM())
    row = gen_lecture_row(course_id, _node(plan))

    md = str(row["body"]["markdown"])
    assert "\x0c" in md
    issues = row["meta"]["content_issues"]
    assert len(issues) == 1 and issues[0]["kind"] == "control-char", issues
    assert issues[0]["count"] == 1 and "U+000C" in issues[0]["chars"], issues
    # 不再进质检判据：10 项口径与权重不动
    assert "content_issues" not in (row["meta"].get("qc") or {})


def test_complete_json_logs_control_chars_with_purpose(caplog):
    """第三层可观测：按 purpose 记 warning；干净输出不记。"""
    with caplog.at_level(logging.WARNING):
        usage_log.complete_json(_ControlCharLLM(), "sys", "user", purpose="lecture")
    hits = [r for r in caplog.records if "控制字符" in r.getMessage()]
    assert hits and "purpose=lecture" in hits[0].getMessage(), [r.getMessage() for r in hits]

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        usage_log.complete_json(_CleanLLM(), "sys", "user", purpose="lecture")
    assert not [r for r in caplog.records if "控制字符" in r.getMessage()]


def test_docx_sanitizes_control_chars_into_visible_placeholder():
    """第二层兜底：XML 不允许的控制字符 → 可见占位 ␌，docx 照常渲染。"""
    md = "## 分数\n\n每份是 $\frac{1}{2}$。\n\n下一段。"
    data = docx_export.markdown_to_docx(md, title="测\f试")
    text = docx_export.docx_text(data)
    assert "\x0c" not in text
    assert "␌rac{1}{2}" in text, text
    assert text.startswith("测␌试")


def test_docx_keeps_tab_and_newline():
    """\t / \n 是 XML 合法字符，不许被清洗。"""
    text = docx_export.docx_text(docx_export.markdown_to_docx("a\tb\n\n第二段"))
    assert "a\tb" in text and "第二段" in text
