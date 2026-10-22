# -*- coding: utf-8 -*-
"""记忆层与校验层的测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[2]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from agentops.opssandbox import scenario
from agentops.opssandbox.memory import EventMemory, Hypothesis, LongTermMemory
from agentops.opssandbox.verifier import ActionVerifier, ConclusionVerifier


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------
def test_short_term_avoids_repeat_query():
    m = EventMemory('ops01')
    assert not m.already_queried('mysql-main')
    m.mark_queried('mysql-main')
    assert m.already_queried('mysql-main')


def test_open_services_excludes_queried():
    m = EventMemory('ops01')
    m.mark_queried('order-svc')
    left = m.open_services(scenario.ALL_SERVICES)
    assert 'order-svc' not in left
    assert len(left) == len(scenario.ALL_SERVICES) - 1


def test_excluded_hypotheses_render():
    m = EventMemory('ops01')
    m.add_hypothesis(Hypothesis('mysql-main', 'slow_query', 'excluded', '指标正常'))
    m.add_hypothesis(Hypothesis('order-svc', 'cpu_saturation', 'confirmed'))
    assert len(m.excluded()) == 1
    text = m.render()
    assert 'mysql-main' in text
    assert 'order-svc' not in text


def test_long_term_only_confirmed():
    lt = LongTermMemory()
    lt.remember(Hypothesis('a', 'cpu_saturation', 'excluded'))
    lt.remember(Hypothesis('order-svc', 'cpu_saturation', 'confirmed'))
    assert len(lt.records) == 1
    assert lt.records[0].service == 'order-svc'


def test_priors_hit_and_miss():
    lt = LongTermMemory()
    lt.remember(Hypothesis('order-svc', 'cpu_saturation', 'confirmed'))
    assert 'order-svc' in lt.priors('cpu_saturation')
    assert lt.priors('dns_failure') == ''


# ---------------------------------------------------------------------------
# ActionVerifier
# ---------------------------------------------------------------------------
def test_action_rejects_unknown_service():
    v = ActionVerifier().check('query_logs', {'service': 'not-a-service'})
    assert not v.ok and v.severity == 'error'


def test_action_rejects_unknown_tool():
    v = ActionVerifier().check('delete_everything', {})
    assert not v.ok


def test_action_rejects_injected_command():
    """诊断命令含 shell 元字符必须被拦下。"""
    v = ActionVerifier().check('run_diagnostic',
                               {'service': 'order-svc',
                                'command': 'top; unsafe-inject'})
    assert not v.ok


def test_action_allows_normal_calls():
    av = ActionVerifier()
    assert av.check('query_logs', {'service': 'order-svc'}).ok
    assert av.check('run_diagnostic',
                    {'service': 'order-svc', 'command': 'top -H'}).ok
    assert av.check('query_topology', {}).ok


# ---------------------------------------------------------------------------
# ConclusionVerifier
# ---------------------------------------------------------------------------
def _obs_of(cid: str, svc: str) -> list[str]:
    c = scenario.CASE_BY_ID[cid]
    return [scenario.logs_of(c, svc)]


def test_conclusion_passes_on_real_quote():
    obs = _obs_of('ops01', 'order-svc')
    ans = ('根因服务：order-svc\n故障类型：cpu_saturation\n'
           '关键证据：RejectedExecutionException: Task rejected from '
           'java.util.concurrent.ThreadPoolExecutor\n排查路径：x')
    v = ConclusionVerifier(obs).gate(ans)
    assert v.ok and v.severity == 'pass'


def test_conclusion_blocks_fabricated_evidence():
    """幻觉引用的证据必须被拦下——这是防 evaluation aliasing 的关键。"""
    obs = _obs_of('ops01', 'order-svc')
    ans = ('根因服务：order-svc\n故障类型：cpu_saturation\n'
           '关键证据：数据库连接池耗尽的告警日志在这里\n排查路径：x')
    v = ConclusionVerifier(obs).gate(ans)
    assert not v.ok


def test_conclusion_marks_partial_verification():
    obs = _obs_of('ops01', 'order-svc')
    ans = ('根因服务：order-svc\n故障类型：cpu_saturation\n'
           '关键证据：RejectedExecutionException: Task rejected from '
           'java.util.concurrent.ThreadPoolExecutor\n'
           '以及一条并不存在的推测性描述内容\n排查路径：x')
    v = ConclusionVerifier(obs).gate(ans)
    assert v.ok and v.severity == 'warning'


def test_short_quote_not_accepted():
    """过短引用容易误命中，不应算作已验证。"""
    obs = _obs_of('ops01', 'order-svc')
    chk = ConclusionVerifier(obs).check('关键证据：ERROR\n排查路径：x')
    assert not chk.ok
