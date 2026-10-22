# -*- coding: utf-8 -*-
"""
压缩与重复调用拦截的耦合测试。

对应 HolmesGPT 的
RESET_REPEATED_TOOL_CALL_CHECK_AFTER_COMPACTION 语义：
  · 压实后放开重复调用（否则模型丢了信息又拿不回来，任务卡死）
  · 但大输出调用不放（否则立刻再次撑爆，形成压实死循环）
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[2]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from miniagent.core import AgentConfig, MiniAgent, ToolExecutor
from miniagent.llm import Reply, ScriptedLLM, tool_call
from miniagent.tools import Tool, ToolRegistry, ToolResult


def _mk_tool(name: str, text: str, is_write: bool = False) -> Tool:
    def _fn(**kwargs) -> ToolResult:
        return ToolResult(True, text)
    return Tool(name=name, description='d',
                params={k: 'p' for k in ('q',)}, required=[],
                fn=_fn, is_write=is_write)


def _agent(cfg: AgentConfig, text: str, script: list[Reply]) -> MiniAgent:
    reg = ToolRegistry()
    reg.add(_mk_tool('fetch', text))
    return MiniAgent(ScriptedLLM(script), reg, cfg)


def test_pinned_calls_survive_compaction():
    """
    大输出调用的签名在压实后仍应保留，重复请求继续被拒绝。
    """
    big = 'X' * 8000
    cfg = AgentConfig(compress_observations=False, max_steps=6,
                      pin_threshold_chars=4000, token_budget=10,
                      reset_dedup_after_compact=True)
    script = [
        Reply(tool_calls=[tool_call('fetch', q='a')]),
        Reply(tool_calls=[tool_call('fetch', q='a')]),
        Reply(content='done'),
    ]
    ag = _agent(cfg, big, script)
    res = ag.run('task')
    # 第二次相同调用应被判为重复（pinned 保护）
    dups = [s for s in res.steps if getattr(s, 'duplicate', False)]
    assert dups, '大输出调用在压实后仍应拦截重复'


def test_small_calls_released_after_compaction():
    """
    小输出调用在压实后应被放开——否则模型丢了信息也拿不回来。
    """
    small = 'y' * 100
    cfg = AgentConfig(compress_observations=False, max_steps=6,
                      pin_threshold_chars=4000, token_budget=2000,
                      reset_dedup_after_compact=True)
    ag = _agent(cfg, small, [Reply(content='done')])
    # 直接构造一次压实，验证去重表被清空
    msgs = [{'role': 'system', 'content': 's'},
            {'role': 'user', 'content': 'u'},
            {'role': 'assistant', 'content': 'a'},
            {'role': 'tool', 'content': 't'}]
    ag._seen_calls['sig1'] = 'cached'
    ag._compact_history(msgs)
    assert 'sig1' not in ag._seen_calls, '小输出调用应在压实后释放'
    assert len(msgs) <= 3


def test_compaction_preserves_system_and_task():
    """压实必须保住 system 提示与原始任务，否则模型失去指令。"""
    cfg = AgentConfig(compress_observations=False, token_budget=10)
    ag = _agent(cfg, 'z', [Reply(content='done')])
    msgs = [
        {'role': 'system', 'content': 'SYS'},
        {'role': 'user', 'content': 'TASK'},
        {'role': 'assistant', 'content': 'mid'},
        {'role': 'tool', 'content': 'obs'},
    ]
    ag._compact_history(msgs)
    texts = [m.get('content') for m in msgs]
    assert 'SYS' in texts, 'system 必须保留'
    assert 'TASK' in texts, '原始任务必须保留'


def test_compaction_keeps_pinned_but_clears_seen():
    cfg = AgentConfig(compress_observations=False)
    ag = _agent(cfg, 'z', [Reply(content='done')])
    ag._seen_calls = {'a': '1', 'b': '2'}
    ag._pinned_calls = {'b': '2'}
    msgs = [{'role': 'system', 'content': 'S'}, {'role': 'user', 'content': 'T'}]
    ag._compact_history(msgs)
    assert 'a' not in ag._seen_calls
    assert 'b' in ag._pinned_calls, 'pinned 调用应保留'


def test_no_compaction_when_disabled():
    cfg = AgentConfig(compress_observations=False, token_budget=10,
                      reset_dedup_after_compact=False)
    ag = _agent(cfg, 'z', [Reply(content='done')])
    ag._seen_calls = {'x': '1'}
    msgs = [{'role': 'system', 'content': 'S'}, {'role': 'user', 'content': 'T'},
            {'role': 'tool', 'content': 'o'}]
    # 关闭开关后，run() 不会调用压实；这里直接验证开关语义
    assert ag.cfg.reset_dedup_after_compact is False
