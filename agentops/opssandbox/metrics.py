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
    """重复查询抑制：无记忆 vs 有记忆。"""
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
            'rate': (naive - with_mem) / max(naive, 1)}


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
        ('drop_database', {}, False),
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
           'action_guard': action_guard(), 'latency': latency_profile()}

    print('=' * 68)
    print('Memory 层 · 重复查询抑制')
    print('=' * 68)
    m = rep['memory']
    print(f'  无记忆 {m["naive"]} 次 → 有记忆 {m["with_memory"]} 次'
          f'，节省 {m["saved"]} 次（{m["rate"]:.1%}）')

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

    out = Path('agentops/opssandbox/layers_result.json')
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n已写入 {out}')


if __name__ == '__main__':
    main()
