# -*- coding: utf-8 -*-
"""量化 Memory / Verifier / Observability 三层的价值。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from agentops.opssandbox import scenario
from agentops.opssandbox.memory import EventMemory
from agentops.opssandbox.verifier import ActionVerifier, ConclusionVerifier


def memory_gain() -> dict:
    """重复查询抑制（查询计划重放口径）：每案例告警服务与根因服务各需查询一次，
    无记忆时按朴素计划重复取，有记忆时排除已查过的服务。
    注意：这是计划级重放，不是真实回路——真实回路统计见 tool_cost_attribution。"""
    naive = 4 * len(scenario.CASES)          # 告警与根因各查两遍
    with_mem = 0
    for c in scenario.CASES:
        mem = EventMemory(c.cid)
        for svc in (c.alert_service, c.root_entity):
            if not mem.already_queried(svc):
                mem.mark_queried(svc)
                with_mem += 1
    return {'naive': naive, 'with_memory': with_mem,
            'saved': naive - with_mem,
            'rate': (naive - with_mem) / max(naive, 1),
            'basis': 'query-plan replay (not live loop)'}


def tool_cost_attribution() -> dict:
    """真实回路的每工具调用数与观测体积归因（基线档，护栏回路记录）。"""
    from agentops.opssandbox.sweep import run_one
    per_tool: dict = {}
    total_calls = 0
    total_chars = 0
    for c in scenario.CASES:
        r = run_one(c, None, guard=True)
        for name, v in (r.guard_stats.get('per_tool') or {}).items():
            d = per_tool.setdefault(name, {'calls': 0, 'chars': 0})
            d['calls'] += v['calls']
            d['chars'] += v['chars']
            total_calls += v['calls']
            total_chars += v['chars']
    for name, d in per_tool.items():
        d['call_share'] = d['calls'] / max(total_calls, 1)
        d['char_share'] = d['chars'] / max(total_chars, 1)
    logs = per_tool.get('query_logs', {})
    return {'total_calls': total_calls, 'per_tool': per_tool,
            'query_logs_call_share': logs.get('call_share', 0),
            'query_logs_char_share': logs.get('char_share', 0)}


def verifier_accuracy() -> dict:
    """证据门禁：真实引用应放行，编造引用应拦截。"""
    tmpl = '根因服务：{s}\n故障类型：{f}\n关键证据：{e}\n排查路径：拓扑→日志'
    fake = '数据库连接池已被完全耗尽并触发级联熔断同时内核发生不可恢复的页表损坏'
    good = bad = 0
    errs = []
    for c in scenario.CASES:
        obs = [scenario.logs_of(c, c.root_entity)]
        cv = ConclusionVerifier(obs)
        if cv.gate(tmpl.format(s=c.root_entity, f=c.fault_type,
                               e=c.evidence[0])).ok:
            good += 1
        else:
            errs.append({'cid': c.cid, 'case': 'true_positive'})
        if not cv.gate(tmpl.format(s=c.root_entity, f=c.fault_type,
                                   e=fake)).ok:
            bad += 1
        else:
            errs.append({'cid': c.cid, 'case': 'false_negative'})
    n = len(scenario.CASES)
    return {'n': n, 'passed_real': good, 'blocked_fake': bad,
            'accuracy': (good + bad) / (2 * n), 'errors': errs}


def action_guard() -> dict:
    av = ActionVerifier()
    probes = [
        ('query_logs', {'service': 'order-svc'}, True),
        ('query_logs', {'service': 'ghost-svc'}, False),
        ('run_diagnostic', {'service': 'order-svc', 'command': 'top -H'}, True),
        ('run_diagnostic', {'service': 'order-svc', 'command': 'a;b'}, False),
        ('drop_table', {'table': 'users'}, False),      # 危险档：直接拒绝
        ('drop_database', {}, False),                    # 未登记：白名单拒绝
        ('query_topology', {}, True),
    ]
    rows = []
    for name, args, want in probes:
        got = av.check(name, args).ok
        rows.append({'probe': name, 'want': want, 'got': got, 'ok': got == want})
    return {'n': len(rows), 'correct': sum(r['ok'] for r in rows), 'rows': rows}


def latency_profile() -> dict:
    lat = []
    for c in scenario.CASES:
        t0 = time.perf_counter()
        for svc in c.chain:
            scenario.logs_of(c, svc)
        lat.append((time.perf_counter() - t0) * 1000)
    lat.sort()
    return {'n': len(lat), 'mean': sum(lat) / len(lat),
            'p50': lat[len(lat) // 2],
            'p95': lat[max(0, int(round(0.95 * (len(lat) - 1))))],
            'max': lat[-1]}


def main():
    rep = {'memory': memory_gain(), 'verifier': verifier_accuracy(),
           'action_guard': action_guard(), 'latency': latency_profile(),
           'tool_cost': tool_cost_attribution()}

    print('=' * 68)
    print('Memory 层 · 重复查询抑制（查询计划重放口径）')
    print('=' * 68)
    m = rep['memory']
    print(f'  无记忆 {m["naive"]} 次 → 有记忆 {m["with_memory"]} 次'
          f'，节省 {m["saved"]} 次（{m["rate"]:.1%}）')

    print()
    print('=' * 68)
    print('Observability · 真实回路工具成本归因（基线档）')
    print('=' * 68)
    tc = rep['tool_cost']
    print(f'  总调用 {tc["total_calls"]} 次')
    for name, d in sorted(tc['per_tool'].items(), key=lambda x: -x[1]['chars']):
        print(f'    {name:<16} calls={d["calls"]:>3} ({d["call_share"]:.1%})  '
              f'chars={d["chars"]:>8} ({d["char_share"]:.1%})')

    print()
    print('=' * 68)
    print('Verifier 层 · 证据门禁')
    print('=' * 68)
    v = rep['verifier']
    print(f'  真实引用放行 {v["passed_real"]}/{v["n"]}')
    print(f'  编造引用拦截 {v["blocked_fake"]}/{v["n"]}')
    print(f'  总判定准确率 {v["accuracy"]:.1%}')
    for e in v['errors'][:4]:
        print(f'    ✗ {e["cid"]} {e["case"]}')

    print()
    print('=' * 68)
    print('ActionVerifier · 动作前置拦截')
    print('=' * 68)
    a = rep['action_guard']
    print(f'  {a["correct"]}/{a["n"]} 探针正确')
    for r in a['rows']:
        print(f'    {"✓" if r["ok"] else "✗"} {r["probe"]:16} '
              f'want={r["want"]} got={r["got"]}')

    print()
    print('=' * 68)
    print('Observability · 工具耗时')
    print('=' * 68)
    l = rep['latency']
    print(f'  n={l["n"]} mean={l["mean"]:.1f}ms p50={l["p50"]:.1f}ms '
          f'p95={l["p95"]:.1f}ms max={l["max"]:.1f}ms')

    out = Path(__file__).resolve().parent / 'layers_result.json'
    from agentops.jsonio import dump as _jd
    _jd(out, rep)
    print(f'\n已写入 {out}')


if __name__ == '__main__':
    main()
