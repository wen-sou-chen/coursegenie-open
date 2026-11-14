"""任务 63：LLM 坏 JSON 的自动重试 + 「串内半角引号」修复。

对应 HANDOFF「任务 63 说明」§C 的四条单测：
① 串内半角引号可修复（含今天两处真机现场原文），且日志里有「使用了引号修复」；
② 合法 JSON（含中文全角引号“”）不受影响 —— 修复路径根本不介入；
③ 第一次坏、第二次好 ⇒ 最终成功，且用量记两次；
④ 两次都坏 ⇒ 抛出的错误里带 pos 与现场片段（类型仍是 json.JSONDecodeError，判据不变）。
"""
from __future__ import annotations

import json
import logging

import pytest

from app.services import usage as usage_log
from app.services.llm import (_BAD_JSON_HINT, BaseLLMClient, extract_json,
                              record_call, repair_inner_quotes)
from app.services.lecture import _failure_reason

LOGGER = "app.services.llm"

# 真机现场（2026-09-19 发版总闸第 3 轮 server.log 原文，修完 LaTeX 反斜杠仍是坏 JSON）
LIVE_QUOTE = ('{"concepts": [{"name": "夸张", "quote": '
              '"二年级学生第一次接触"夸张"这一修辞,可用"三千尺"与真实瀑布高度对比引入;"}]}')
LIVE_EXPLANATION = ('{"concepts": [{"name": "望天门山", "explanation": '
                    '"《望天门山》中的水是"碧水东流至此回"，表现出江水的流动和回旋；"}]}')

# 「不是合法 JSON」且引号修复也救不回来（真正缺的是内容 / 逗号）—— 用于重试用例
BROKEN_TRUNCATED = '{"nodes": [{"name": "光合作用"'
BROKEN_MISSING_COMMA = '{"concepts": [{"name": "光合作用" "aliases": []}]}'


class _ScriptedClient(BaseLLMClient):
    """按剧本回文本；照真实客户端的做法，每次 complete 都记一条用量。"""

    name = "scripted"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    def complete(self, system: str, user: str, *, max_tokens: int = 2000) -> str:
        self.calls.append({"system": system, "user": user})
        text = self.replies.pop(0) if self.replies else "{}"
        record_call(provider=self.name, model="scripted-1",
                    prompt_tokens=None, completion_tokens=None,
                    system=system, user=user, content=text, latency_ms=1)
        return text


def test_inner_quotes_repaired_with_log(caplog):
    """① 串内半角引号：修复后值里保留「引号套引号」原样，并留下可观测日志。"""
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        data = extract_json('{"a": "他说"你好"就走了"}')
    assert data == {"a": '他说"你好"就走了'}
    assert "使用了引号修复" in caplog.text


@pytest.mark.parametrize("raw", [LIVE_QUOTE, LIVE_EXPLANATION])
def test_live_bad_json_samples_are_repaired(raw, caplog):
    """① 今天两次整步失败的现场原文（一字未改）都要能被自动修复。"""
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        data = extract_json(raw)
    row = data["concepts"][0]
    value = row.get("quote") or row["explanation"]
    assert '"' in value and '\\"' not in value
    assert "使用了引号修复" in caplog.text


def test_valid_json_never_enters_repair_path(caplog):
    """② 合法 JSON（含全角引号、已转义引号、空串）不受影响：修复函数一动不动。"""
    text = ('{"a": "他说“你好”就走了", "b": "he said \\"hi\\"", "c": "", '
            '"d": [1, 2], "e": "末尾"}')
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        data = extract_json(text)
    assert data["a"] == "他说“你好”就走了"
    assert data["b"] == 'he said "hi"'
    assert repair_inner_quotes(text) == text
    assert "使用了引号修复" not in caplog.text


def test_retry_after_bad_json_succeeds_and_records_twice(caplog):
    """③ 第一次坏、第二次好 ⇒ 成功；重试带上纠错提示，且用量记两条。"""
    client = _ScriptedClient([BROKEN_TRUNCATED, '{"nodes": [{"name": "光合作用"}]}'])
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        with usage_log.collect() as collector:
            data = client.complete_json("CONCEPT_JSON", "请抽取概念")
    assert data == {"nodes": [{"name": "光合作用"}]}
    assert len(client.calls) == 2
    assert client.calls[1]["user"] == f"请抽取概念{_BAD_JSON_HINT}"
    summary = collector.summary()
    assert summary["llm_calls"] == 2  # 坏 JSON 那次 + 重试那次都在账上
    assert summary["total_tokens"] > 0
    assert "重试 1 次" in caplog.text


def test_retry_twice_bad_raises_with_position_and_context():
    """④ 两次都坏 ⇒ 抛错，且消息里带 pos 与现场片段（类型与判据都不变）。"""
    client = _ScriptedClient([BROKEN_MISSING_COMMA, BROKEN_MISSING_COMMA])
    with usage_log.collect() as collector:
        with pytest.raises(json.JSONDecodeError) as info:
            client.complete_json("CONCEPT_JSON", "请抽取概念")
    exc = info.value
    assert f"位置 {exc.pos}" in str(exc)
    assert "现场" in str(exc) and "光合作用" in str(exc)
    assert "Expecting" in str(exc)  # 原始解析器消息也留着
    assert len(client.calls) == 2
    assert collector.summary()["llm_calls"] == 2


def test_enriched_error_keeps_llm_json_classification():
    """④ 附带的护栏：带 pos/现场的异常仍是 JSONDecodeError ⇒ lecture 仍归 llm-json。"""
    with pytest.raises(json.JSONDecodeError) as info:
        extract_json(BROKEN_TRUNCATED)
    stage, reason = _failure_reason(info.value)
    assert stage == "llm-json"
    assert "位置" in reason and "现场" in reason
