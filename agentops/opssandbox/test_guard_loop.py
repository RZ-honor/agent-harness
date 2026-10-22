# -*- coding: utf-8 -*-
"""护栏必须活在执行回路里：本文件锁定 guard_e2e 的核心断言为回归测试。"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from agentops.opssandbox import scenario
from agentops.opssandbox.sweep import run_one
from agentops.opssandbox.tools import build_guarded_registry
from agentops.opssandbox.verifier import ActionVerifier, ConclusionVerifier


def test_guard_on_off_same_result_small_subset():
    cases = scenario.CASES[:5]
    for keep in (None, 4000):
        off = sum(run_one(c, keep, guard=False).passed for c in cases)
        on = sum(run_one(c, keep, guard=True).passed for c in cases)
        assert off == on, f'护栏开关改变了端到端结果: {off} vs {on}'


def test_write_needs_confirmation_enforced():
    stats, obs = {}, []
    reg = build_guarded_registry(scenario.CASES[0], stats, obs)
    t = reg.get('restart_service')
    assert not t.fn(service='order-svc').ok          # 未确认 → 拦
    assert stats['confirm_blocks'] == 1
    assert t.fn(service='order-svc', confirmed='true').ok  # 确认 → 放行执行


def test_danger_tool_blocked():
    stats, obs = {}, []
    reg = build_guarded_registry(scenario.CASES[0], stats, obs)
    assert not reg.get('drop_table').fn(table='users').ok
    assert stats['guard_blocks'] == 1


def test_unregistered_tool_blocked():
    assert not ActionVerifier().guard('rm_rf', {}).ok


def test_conclusion_gate_real_vs_fake():
    case = scenario.CASES[0]
    cv = ConclusionVerifier([scenario.logs_of(case, case.root_entity)])
    tmpl = '根因服务：{s}\n故障类型：{f}\n关键证据：{e}\n排查路径：拓扑→日志'
    assert cv.gate(tmpl.format(s=case.root_entity, f=case.fault_type,
                               e=case.evidence[0])).ok
    assert not cv.gate(tmpl.format(s=case.root_entity, f=case.fault_type,
                                   e='内存条过热导致CPU降频并触发内核页表损坏')).ok


def test_breaker_opens_after_three_failures():
    stats, obs = {}, []
    reg = build_guarded_registry(scenario.CASES[0], stats, obs)
    logs = reg.get('query_logs')
    for _ in range(3):
        assert not logs.fn(service='ghost-svc').ok
    assert not logs.fn(service='order-svc').ok       # 合法调用也被快速失败
    assert stats['breaker_fastfail'] == 1


def test_per_tool_cost_attribution_available():
    stats, obs = {}, []
    reg = build_guarded_registry(scenario.CASES[0], stats, obs)
    reg.get('query_logs').fn(service=scenario.CASES[0].root_entity)
    assert 'query_logs' in stats['per_tool']
    assert stats['per_tool']['query_logs']['chars'] > 0
