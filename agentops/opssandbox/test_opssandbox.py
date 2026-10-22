# -*- coding: utf-8 -*-
"""
排障沙箱与压缩器的不变量测试。

测的不是"跑通"，而是**结论依赖的那些性质**：
  - 评分器不能把噪声判成证据
  - 压缩器在小预算下必须优先保留 ERROR 行
  - 压缩率单调性：预算越小，保留字符越少
  - 拐点必须存在（否则"拐点"这个结论是假的）
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[2]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from miniagent.core import ObservationCompressor
from agentops.opssandbox import scenario, scoring
from agentops.opssandbox.operator import CompetentOperator
from agentops.opssandbox.sweep import LEVELS, run_one, sweep


# ---------------------------------------------------------------------------
# 场景完整性
# ---------------------------------------------------------------------------
def test_case_count_and_uniqueness():
    assert len(scenario.CASES) == 20
    assert len({c.cid for c in scenario.CASES}) == 20


def test_every_case_has_four_layer_ground_truth():
    """四层 ground truth 缺一层，评分就会失真。"""
    for c in scenario.CASES:
        assert c.fault_type, c.cid
        assert c.root_entity in scenario.ALL_SERVICES, c.cid
        assert c.chain and c.chain[0] == c.alert_service, c.cid
        assert c.root_entity in c.chain, c.cid
        assert len(c.evidence) >= 1, c.cid
        assert c.anomaly, c.cid


def test_evidence_lines_actually_present_in_root_logs():
    """证据必须真的出现在根因服务的日志里，否则永远不可能命中。"""
    for c in scenario.CASES:
        logs = scenario.logs_of(c, c.root_entity)
        for ev in c.evidence:
            assert ev in logs, f'{c.cid}: 证据 {ev!r} 不在日志中'


def test_alert_service_often_not_root():
    """
    如果所有案例的告警服务都等于根因，任务就没有难度，
    "沿拓扑向下追"这个能力也测不出来。
    """
    diff = [c for c in scenario.CASES if c.alert_service != c.root_entity]
    assert len(diff) >= 5, f'只有 {len(diff)} 个案例根因在告警服务之外'


# ---------------------------------------------------------------------------
# 评分器
# ---------------------------------------------------------------------------
def test_scoring_rejects_guessed_root_without_evidence():
    """只猜对根因不给证据，不能算解决（evaluation aliasing）。"""
    c = scenario.CASE_BY_ID['ops01']
    ans = '根因服务：order-svc\n故障类型：cpu_saturation\n关键证据：无\n排查路径：猜的'
    sc = scoring.score_answer(c, ans)
    assert sc.root_hit and not sc.evidence_hit
    assert not sc.solved
    assert 'aliasing' in sc.note


def test_scoring_accepts_alias_forms():
    c = scenario.CASE_BY_ID['ops01']
    ans = '根因服务：订单服务\n故障类型：CPU 打满\n关键证据：RejectedExecutionException'
    sc = scoring.score_answer(c, ans)
    assert sc.root_hit, '中文别名应能识别'
    assert sc.type_hit


def test_scoring_full_marks_on_reference_answer():
    c = scenario.CASE_BY_ID['ops01']
    ans = ('根因服务：order-svc\n故障类型：cpu_saturation\n'
           '关键证据：RejectedExecutionException: Task rejected; '
           'CPU 使用率 98.7% load=48.2; thread_pool_active=200/200 queue=1024\n'
           '排查路径：拓扑→日志→指标')
    sc = scoring.score_answer(c, ans)
    assert sc.root_hit and sc.type_hit and sc.evidence_hit
    assert sc.total == pytest.approx(1.0)


def test_noise_lines_are_not_evidence():
    """健康检查行不能算证据。"""
    c = scenario.CASE_BY_ID['ops01']
    ans = ('根因服务：order-svc\n故障类型：cpu_saturation\n'
           '关键证据：health check ok, pool=12/20')
    sc = scoring.score_answer(c, ans)
    assert not sc.evidence_hit


# ---------------------------------------------------------------------------
# 压缩器
# ---------------------------------------------------------------------------
def test_compressor_keeps_error_lines_under_tight_budget():
    """
    核心不变量：预算紧到原文的 2% 时，ERROR 行仍应被保留。
    这是"压缩不能丢证据"这个论断的技术前提。
    """
    c = scenario.CASE_BY_ID['ops01']
    text = '# service=order-svc\n' + scenario.logs_of(c, 'order-svc')
    comp = ObservationCompressor(400, 66)
    out, raw = comp.compress(text, 'order-svc query_logs', 'query_logs')
    assert raw > 10000
    assert 'RejectedExecution' in out, 'ERROR 行被压缩丢掉了'
    assert len(out) < raw


def test_compressor_does_not_treat_expired_evict_as_strong():
    """
    回归测试：'evict expired entries' 曾命中 'expired' 强信号，
    把预算全吃掉，导致证据 0/3。改为短语匹配后不应再发生。
    """
    c = scenario.CASE_BY_ID['ops01']
    text = '# service=order-svc\n' + scenario.logs_of(c, 'order-svc')
    comp = ObservationCompressor(1000, 166)
    out, _ = comp.compress(text, 'query_logs', 'query_logs')
    # 该噪声行可以被选，但不能把全部预算占满
    assert 'RejectedExecution' in out


def test_compressor_monotonic_in_budget():
    """预算越小，输出不应变长。"""
    c = scenario.CASE_BY_ID['ops01']
    text = '# service=order-svc\n' + scenario.logs_of(c, 'order-svc')
    sizes = []
    for lim in (4000, 2000, 1000, 400):
        out, _ = ObservationCompressor(lim, max(60, lim // 6)).compress(
            text, 'query_logs', 'query_logs')
        sizes.append(len(out))
    assert sizes == sorted(sizes, reverse=True), sizes


def test_compressor_passthrough_when_under_budget():
    comp = ObservationCompressor(10000, 200)
    out, raw = comp.compress('short text', 'q', 't')
    assert out == 'short text' and raw == len('short text')


# ---------------------------------------------------------------------------
# 端到端
# ---------------------------------------------------------------------------
def test_baseline_solves_all_cases():
    """不压缩时应当全解——否则说明沙箱或 operator 有 bug，曲线不可信。"""
    results = [run_one(c, None) for c in scenario.CASES]
    solved = sum(r.score.solved for r in results)
    assert solved == len(results), f'基线只有 {solved}/{len(results)}'


def test_there_is_a_cliff_not_a_gentle_slope():
    """
    结论的核心：压缩存在拐点，而不是线性缓降。
    若将来改动让曲线变平，这个断言会失败，提醒我们结论需要重写。
    """
    rows = sweep(scenario.CASES)
    by = {r['level']: r['success_rate'] for r in rows}
    assert by['100%(不压缩)'] == pytest.approx(1.0)
    assert by['20%'] >= 0.85, by
    assert by['5%'] <= 0.70, by
    assert by['100%(不压缩)'] - by['20%'] <= 0.10, '前段应该是平缓的'
    assert by['10%'] - by['5%'] >= 0.20, '后段应该出现陡降'


def test_compression_saves_tokens_monotonically():
    rows = sweep(scenario.CASES)
    toks = [r['avg_tokens'] for r in rows]
    assert toks == sorted(toks, reverse=True), toks


def test_operator_does_not_read_ground_truth():
    """
    operator 只能通过工具观测拿信息。这里用一个"观测全空"的假消息序列，
    验证它在没有信息时不会凭空报出正确答案。
    """
    c = scenario.CASE_BY_ID['ops13']
    op = CompetentOperator(c)
    reply = op.chat([{'role': 'tool', 'content': ''}], [])
    # 第一轮只会去查拓扑，不应直接给答案
    assert reply.tool_calls
