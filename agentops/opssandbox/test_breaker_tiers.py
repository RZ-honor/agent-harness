# -*- coding: utf-8 -*-
"""
熔断与权限分级的测试。

对应社区实践：
· Datawhale：3 次失败熔断，300 秒后恢复
· 一线开发者：让模型自由调工具"差点把测试库给清了" → 写操作必须拴绳
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from agentops.opssandbox.breaker import ToolCircuitBreaker
from agentops.opssandbox.verifier import (TIER_DANGER, TIER_READ, TIER_WRITE,
                                          ActionVerifier)


# ---------------------------------------------------------------------------
# 熔断器
# ---------------------------------------------------------------------------
def test_breaker_opens_after_threshold():
    b = ToolCircuitBreaker(threshold=3, cooldown_s=300)
    assert not b.is_open('query_logs')
    for _ in range(3):
        b.record_failure('query_logs', now=1000.0)
    assert b.is_open('query_logs', now=1000.0)
    assert b.tripped_events == 1


def test_breaker_below_threshold_stays_closed():
    b = ToolCircuitBreaker(threshold=3)
    b.record_failure('t', now=1000.0)
    b.record_failure('t', now=1001.0)
    assert not b.is_open('t', now=1001.0)


def test_breaker_recovers_after_cooldown():
    b = ToolCircuitBreaker(threshold=3, cooldown_s=300)
    for _ in range(3):
        b.record_failure('t', now=1000.0)
    assert b.is_open('t', now=1100.0), '冷却未到应仍熔断'
    assert not b.is_open('t', now=1301.0), '冷却结束应恢复'


def test_breaker_success_resets_streak():
    """只有连续失败才熔断，中间有成功应清零。"""
    b = ToolCircuitBreaker(threshold=3)
    b.record_failure('t', now=1.0)
    b.record_failure('t', now=2.0)
    b.record_success('t')
    b.record_failure('t', now=3.0)
    assert not b.is_open('t', now=3.0)


def test_breaker_isolates_tools():
    """一个工具熔断不应影响其他工具。"""
    b = ToolCircuitBreaker(threshold=2)
    b.record_failure('bad', now=1.0)
    b.record_failure('bad', now=2.0)
    assert b.is_open('bad', now=2.0)
    assert not b.is_open('good', now=2.0)


def test_breaker_remaining_time():
    b = ToolCircuitBreaker(threshold=1, cooldown_s=300)
    b.record_failure('t', now=100.0)
    assert b.remaining_s('t', now=100.0) == 300.0
    assert b.remaining_s('t', now=250.0) == 150.0
    assert b.remaining_s('t', now=500.0) == 0.0


# ---------------------------------------------------------------------------
# 权限分级
# ---------------------------------------------------------------------------
def test_read_tier_passes_without_confirmation():
    g = ActionVerifier().guard('query_logs', {'service': 'order-svc'})
    assert g.ok and g.tier == TIER_READ and not g.needs_confirmation


def test_write_tier_requires_confirmation():
    """写操作放行但必须人工确认——这是"拴绳"的落点。"""
    g = ActionVerifier().guard('restart_service', {'service': 'order-svc'})
    assert g.ok and g.tier == TIER_WRITE
    assert g.needs_confirmation, '写操作必须要求确认'


def test_danger_tier_blocks():
    """危险操作直接拒绝。"""
    g = ActionVerifier().guard('drop_table', {})
    assert not g.ok and g.tier == TIER_DANGER


def test_unknown_tool_blocked_whitelist():
    """未登记工具按白名单拒绝。"""
    g = ActionVerifier().guard('some_new_tool', {})
    assert not g.ok


def test_all_read_tools_are_registered():
    """已注册的只读工具应全部放行。"""
    av = ActionVerifier()
    for name in ActionVerifier.READ_ONLY:
        g = av.guard(name, {'service': 'order-svc'})
        assert g.ok, name


def test_injected_command_still_blocked():
    g = ActionVerifier().guard('run_diagnostic',
                               {'service': 'order-svc',
                                'command': 'top; unsafe'})
    assert not g.ok