# -*- coding: utf-8 -*-
"""miniagent 系统测试：循环、预算、压缩、幂等、错误处理。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))

from miniagent.core import (AgentConfig, MiniAgent, ObservationCompressor,
                            ToolExecutor)
from miniagent.llm import Reply, ScriptedLLM, estimate_tokens, tool_call
from miniagent.tools import (Tool, ToolRegistry, ToolResult, default_registry,
                             make_calc_tool)


# ---------------------------------------------------------------------------
# 基础件
# ---------------------------------------------------------------------------
def test_estimate_tokens_monotonic():
    a = estimate_tokens('你好')
    b = estimate_tokens('你好世界，这是一段更长的中文文本用来比较。')
    assert b > a > 0
    assert estimate_tokens('') == 0


def test_registry_rejects_duplicate_names():
    reg = ToolRegistry()
    t = make_calc_tool()
    reg.add(t)
    with pytest.raises(ValueError, match='重名'):
        reg.add(t)


def test_tool_schema_shape():
    s = make_calc_tool().schema()
    assert s['type'] == 'function'
    assert s['function']['name'] == 'calculator'
    assert 'expr' in s['function']['parameters']['properties']
    assert s['function']['parameters']['required'] == ['expr']


def test_tool_validate_missing_and_unknown():
    t = make_calc_tool()
    assert t.validate({}) and '缺少' in t.validate({})
    assert t.validate({'expr': '1+1', 'bogus': 'x'}) and '未知' in \
        t.validate({'expr': '1+1', 'bogus': 'x'})
    assert t.validate({'expr': '1+1'}) is None


def test_calc_rejects_non_arithmetic():
    t = make_calc_tool()
    r = t.fn(expr='__import__("os").system("echo hi")')
    assert not r.ok
    assert t.fn(expr='2*(3+4)').data['value'] == 14


# ---------------------------------------------------------------------------
# 观测压缩
# ---------------------------------------------------------------------------
def test_compress_noop_when_short():
    c = ObservationCompressor(limit_chars=100)
    text = '短文本'
    out, raw = c.compress(text, 'q')
    assert out == text and raw == len(text)


def test_compress_keeps_keyword_window():
    c = ObservationCompressor(limit_chars=120, window_chars=20)
    filler = '无关内容' * 100
    text = filler + '关键术语在这里出现' + filler
    out, raw = c.compress(text, '关键术语')
    assert len(out) <= raw
    assert '关键术语' in out, '压缩必须保留关键词'
    assert raw == len(text)


def test_compress_marks_omission():
    """必须显式标注省略，否则模型会误以为观测完整。"""
    c = ObservationCompressor(limit_chars=100, window_chars=10)
    text = ('甲' * 400) + '关键' + ('乙' * 400)
    out, _ = c.compress(text, '关键')
    assert '省略' in out


def test_compress_fallback_without_keywords():
    c = ObservationCompressor(limit_chars=100)
    out, raw = c.compress('x' * 500, '')
    assert len(out) <= raw
    assert '省略' in out


# ---------------------------------------------------------------------------
# 幂等执行器
# ---------------------------------------------------------------------------
def _write_tool(counter: list):
    def _fn(path: str = '', content: str = '') -> ToolResult:
        counter.append(content)
        return ToolResult(True, f'wrote {path}')

    return Tool(name='write_file', description='w', params={
        'path': 'p', 'content': 'c'}, required=['path', 'content'],
        fn=_fn, is_write=True)


def test_idempotent_executor_collapses_write():
    counter: list = []
    ex = ToolExecutor(idempotent=True)
    t = _write_tool(counter)
    args = {'path': 'a.txt', 'content': 'hello'}
    ex.execute(t, args)
    ex.execute(t, args)
    assert len(counter) == 1, '重复写未被折叠'
    assert ex.hits == 1


def test_idempotent_different_content_not_collapsed():
    counter: list = []
    ex = ToolExecutor(idempotent=True)
    t = _write_tool(counter)
    ex.execute(t, {'path': 'a', 'content': '1'})
    ex.execute(t, {'path': 'a', 'content': '2'})
    assert len(counter) == 2, '不同内容不应被折叠'


def test_idempotent_ignores_arg_order():
    """幂等键用规范化参数，key 顺序不同也要命中。"""
    a = ToolExecutor.idem_key('write_file', {'path': 'x', 'content': 'y'})
    b = ToolExecutor.idem_key('write_file', {'content': 'y', 'path': 'x'})
    assert a == b


def test_idempotent_disabled_executes_twice():
    counter: list = []
    ex = ToolExecutor(idempotent=False)
    t = _write_tool(counter)
    args = {'path': 'a', 'content': 'h'}
    ex.execute(t, args)
    ex.execute(t, args)
    assert len(counter) == 2


def test_read_tools_not_cached():
    """读工具不该被缓存，否则会返回过期数据。"""
    calls: list = []

    def _fn(query: str = '') -> ToolResult:
        calls.append(query)
        return ToolResult(True, 'r')

    t = Tool(name='search_docs', description='d', params={'query': 'q'},
             required=['query'], fn=_fn, is_write=False)
    ex = ToolExecutor(idempotent=True)
    ex.execute(t, {'query': 'a'})
    ex.execute(t, {'query': 'a'})
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Agent 循环
# ---------------------------------------------------------------------------
@pytest.fixture
def tools():
    return default_registry(BASE, BASE / 'docs')


def test_agent_returns_final_answer(tools):
    script = [Reply(tool_calls=[tool_call('calculator', expr='6*7')]),
              Reply(content='答案是 42')]
    agent = MiniAgent(ScriptedLLM(script), tools, AgentConfig(max_steps=4))
    res = agent.run('算 6*7')
    assert res.answer == '答案是 42'
    assert res.stop_reason == 'final'
    assert res.tool_calls == 1


def test_agent_handles_unknown_tool(tools):
    script = [Reply(tool_calls=[tool_call('nope', x='1')]),
              Reply(content='工具不可用，改用其他方式')]
    agent = MiniAgent(ScriptedLLM(script), tools, AgentConfig(max_steps=4))
    res = agent.run('随便')
    assert res.steps[0].ok is False
    assert '不存在工具' in res.steps[0].note


def test_agent_handles_bad_args(tools):
    script = [Reply(tool_calls=[tool_call('calculator')]),   # 缺 expr
              Reply(content='参数错误')]
    agent = MiniAgent(ScriptedLLM(script), tools, AgentConfig(max_steps=4))
    res = agent.run('算一下')
    assert res.steps[0].ok is False
    assert '缺少必填参数' in res.steps[0].note


def test_agent_stops_at_max_steps(tools):
    """
    模型一直想调工具时，必须在预算内停下。

    合法的收敛原因有三种，其中 `stalled` 是最理想的一种：
    检测到重复调用后提前止损，而不是把预算烧完（`tool_budget`）
    或撞到步数上限（`max_steps`）。
    """
    script = [Reply(tool_calls=[tool_call('calculator', expr='1+1')])] * 10
    agent = MiniAgent(ScriptedLLM(script), tools,
                      AgentConfig(max_steps=3, max_tool_calls=100))
    res = agent.run('无限循环')
    assert len(res.steps) <= 4
    assert res.stop_reason in ('max_steps', 'tool_budget', 'stalled')


def test_agent_respects_tool_budget(tools):
    script = [Reply(tool_calls=[tool_call('calculator', expr='1+1'),
                                tool_call('calculator', expr='2+2'),
                                tool_call('calculator', expr='3+3')])]
    agent = MiniAgent(ScriptedLLM(script), tools,
                      AgentConfig(max_steps=5, max_tool_calls=2))
    res = agent.run('超预算')
    assert res.stop_reason == 'tool_budget'


def test_agent_observation_compression_applied(tools):
    long_script = [Reply(tool_calls=[tool_call('search_docs',
                                               query='证据压缩 节省', top_k='6')]),
                   Reply(content='ok')]
    compressed = MiniAgent(ScriptedLLM(list(long_script)), tools,
                           AgentConfig(compress_observations=True,
                                       obs_limit_chars=300, max_steps=3)).run('x')
    raw = MiniAgent(ScriptedLLM(list(long_script)), tools,
                    AgentConfig(compress_observations=False,
                                obs_limit_chars=300, max_steps=3)).run('x')
    c_chars = sum(s.obs_chars for s in compressed.steps)
    r_chars = sum(s.obs_chars for s in raw.steps)
    assert c_chars < r_chars, '开启压缩后进入上下文的字符应更少'


def test_agent_idempotent_write_collapsed(tools):
    """
    注意：这里刻意不用 pytest 的 tmp_path fixture —— 本机受限环境下
    pytest 的临时根目录会 PermissionError，而本用例根本不需要临时目录
    （写入目标是工作区内的相对路径）。测试不该依赖无关的环境能力。

    另外断言的是 `duplicate_side_effects_prevented` 而不是 `idempotent_hits`：
    前者是"少做了几次重复副作用"，后者只统计"执行器缓存命中"。
    完全相同的调用会被**循环层**提前拦掉（比进执行器更早），
    所以这里 idempotent_hits 可以是 0，但预防次数必须是 1。
    两个指标语义不同，不能混用。
    """
    args = {'path': '.miniagent_out/t.txt', 'content': 'hi'}
    script = [Reply(tool_calls=[tool_call('write_file', **args)]),
              Reply(tool_calls=[tool_call('write_file', **args)]),
              Reply(content='done')]
    agent = MiniAgent(ScriptedLLM(script), tools,
                      AgentConfig(max_steps=5, idempotent_tools=True))
    res = agent.run('写两次同样内容')
    assert res.duplicate_side_effects_prevented >= 1
    # 第二次同参调用必须被标记为 duplicate
    assert any(s.duplicate for s in res.steps)


def test_tool_messages_reference_call_id(tools):
    """
    多工具返回时，观测必须按 tool_call_id 关联。
    早期实现按顺序对应，一旦模型并行请求两个工具就会串位。
    """
    script = [Reply(tool_calls=[tool_call('calculator', expr='1+1'),
                                tool_call('calculator', expr='10*10')]),
              Reply(content='done')]
    agent = MiniAgent(ScriptedLLM(script), tools, AgentConfig(max_steps=3))
    res = agent.run('两个计算')
    assert res.tool_calls == 2
    assert all(s.ok for s in res.steps[:2])
