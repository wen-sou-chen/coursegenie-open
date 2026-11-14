"""LLM 客户端与 JSON 提取测试。"""
from __future__ import annotations

import pytest

from app.services.llm import MockLLMClient, extract_json


def test_extract_json_with_code_fence():
    text = "```json\n{\"a\": 1}\n```"
    assert extract_json(text) == {"a": 1}


def test_extract_json_with_prose():
    text = "好的，结果如下：{\"nodes\": []} 完毕"
    assert extract_json(text) == {"nodes": []}


def test_extract_json_repairs_single_backslash_frac():
    """任务 48w：模型少写一个反斜杠时，\\frac 不许被吃成换页符 U+000C。"""
    data = extract_json('{"markdown": "$\\frac{1}{2}$"}')
    assert data["markdown"] == "$\\frac{1}{2}$"
    assert "\x0c" not in data["markdown"]


def test_extract_json_repairs_illegal_escapes_instead_of_raising():
    """\\alpha / \\times 在 JSON 里是非法转义：修复后不再抛错（48c 那类解析错同源）。"""
    data = extract_json('{"a": "$\\alpha + \\times b$"}')
    assert data["a"] == "$\\alpha + \\times b$"


@pytest.mark.parametrize("cmd", ["theta", "neq", "nabla", "frac"])
def test_extract_json_keeps_latex_commands(cmd):
    data = extract_json('{"a": "$\\%s$"}' % cmd)
    assert data["a"] == "$\\%s$" % cmd


def test_extract_json_keeps_real_newlines_and_tabs():
    """回归护栏：\\t / \\n 后面是汉字、数字、标点时是真换行/真制表，不许一刀切。"""
    assert extract_json('{"a": "第一行\\n第二行"}')["a"] == "第一行\n第二行"
    assert extract_json('{"a": "a\\t1"}')["a"] == "a\t1"
    assert extract_json('{"a": "结尾：\\n- 列表项"}')["a"] == "结尾：\n- 列表项"


def test_extract_json_keeps_compliant_double_backslash():
    """合规输出（双反斜杠）与真转义不受修复影响。"""
    assert extract_json('{"a": "$\\\\frac{1}{2}$"}')["a"] == "$\\frac{1}{2}$"
    assert extract_json('{"a": "\\u4e2d"}')["a"] == "中"
    assert extract_json('{"a": "he said \\"hi\\" \\\\ ok"}')["a"] == 'he said "hi" \\ ok'


def test_extract_json_escapes_raw_control_chars_inside_strings():
    """真机实测的第三类坏 JSON：模型把真换行直接写进字符串（Invalid control character）。"""
    assert extract_json('{"a": "第一行\n第二行"}')["a"] == "第一行\n第二行"
    assert extract_json('{"a": "a\tb"}')["a"] == "a\tb"
    assert extract_json('{"a": "坏\x0c字符"}')["a"] == "坏\x0c字符"
    # 字符串外的换行是合法空白，不许动
    assert extract_json('{\n"a": 1,\n"b": "x"\n}') == {"a": 1, "b": "x"}


def test_extract_json_only_repairs_known_latex_commands_after_n_t():
    """\\n / \\t 只在后面是**已知 LaTeX 命令名**（或 `{`）时才补字面量。

    规格原话是「后跟字母一律补」，但既有用例证明这条会误伤正常换行：代码块首行
    ```mermaid\\nflowchart TD``` 的换行后面正好是字母 `f`，补成字面量后流程图
    整块识别不出来（`tests/test_publish_packages.py` 实测变红）⇒ 收窄成白名单。
    """
    assert extract_json('{"a": "$\\theta + \\nabla$"}')["a"] == "$\\theta + \\nabla$"
    assert extract_json('{"a": "3\\times 2"}')["a"] == "3\\times 2"
    assert extract_json('{"a": "\\text{分子}"}')["a"] == "\\text{分子}"
    # 不是已知命令 ⇒ 保持真换行（汉字 / 英文单词 / 代码块首行都算）
    assert extract_json('{"a": "步骤：\\n中文"}')["a"] == "步骤：\n中文"
    assert extract_json('{"a": "步骤：\\nthe rule"}')["a"] == "步骤：\nthe rule"
    assert extract_json('{"a": "步骤：\\ntimes 2"}')["a"] == "步骤：\ntimes 2"
    fence = '{"a": "```mermaid\\nflowchart TD\\n  A --> B\\n```"}'
    assert extract_json(fence)["a"] == "```mermaid\nflowchart TD\n  A --> B\n```"


def test_mock_outline_json():
    client = MockLLMClient()
    data = client.complete_json("OUTLINE_JSON", "生物课程\n章节数：3", max_tokens=2000)
    assert data["title"]
    assert len(data["nodes"]) == 3
    assert data["nodes"][0]["prerequisites"] == []


def test_mock_quiz_json():
    client = MockLLMClient()
    data = client.complete_json("QUIZ_JSON", "光合作用\n题量：5\n难度：medium")
    assert data["items"]
    assert data["items"][0]["question"]
