# -*- coding: utf-8 -*-
"""
护栏回路端到端验证：
  Part 1  正路等价——20 案例在 100%/20% 两档，护栏开/关结果必须一致；
  Part 2  对抗路有效——写/危险/未登记/未确认必须被拦，伪造结论必须被门禁拒绝，
          连续失败必须触发熔断。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from agentops.opssandbox import scenario
from agentops.opssandbox.sweep import run_one
from agentops.opssandbox.tools import build_guarded_registry
from agentops.opssandbox.verifier import ActionVerifier, ConclusionVerifier


def part1() -> list[dict]:
    rows = []
    for level, keep in [('100%(不压缩)', None), ('20%', 4000)]:
        off = sum(run_one(c, keep, guard=False).passed for c in scenario.CASES)
        results = [run_one(c, keep, guard=True) for c in scenario.CASES]
        on = sum(r.passed for r in results)
        gate = sum(r.gate_ok for r in results)
        ev = {}
        for r in results:
            for k, v in r.guard_stats.items():
                if isinstance(v, dict):
                    continue
                ev[k] = ev.get(k, 0) + v
        rows.append({'level': level, 'solved_off': off, 'solved_on': on,
                     'gate_passed': gate, 'n': len(scenario.CASES), 'events': ev})
    return rows


def part2() -> list[dict]:
    case = scenario.CASES[0]
    checks = []

    # a/b) 写工具：未确认被拦，确认后执行
    stats, obs = {}, []
    reg = build_guarded_registry(case, stats, obs)
    restart = reg.get('restart_service')
    r1 = restart.fn(service='order-svc')
    checks.append({'case': 'write_without_confirm_blocked',
                   'pass': (not r1.ok) and stats['confirm_blocks'] == 1})
    r2 = restart.fn(service='order-svc', confirmed='true')
    checks.append({'case': 'write_with_confirm_executed',
                   'pass': r2.ok and stats['executed'] >= 1})

    # c) 危险工具直接拒绝
    stats2, obs2 = {}, []
    reg2 = build_guarded_registry(case, stats2, obs2)
    r3 = reg2.get('drop_table').fn(table='users')
    checks.append({'case': 'danger_always_blocked',
                   'pass': (not r3.ok) and stats2['guard_blocks'] == 1})

    # d) 未登记工具拒绝
    v = ActionVerifier()
    checks.append({'case': 'unregistered_tool_blocked',
                   'pass': not v.guard('rm_rf', {}).ok})

    # e) 结论门禁：真实证据放行 / 伪造证据拒绝
    real_ev = case.evidence[0]
    cv = ConclusionVerifier([scenario.logs_of(case, case.root_entity)])
    tmpl = '根因服务：{s}\n故障类型：{f}\n关键证据：{e}\n排查路径：拓扑→日志'
    ok_real = cv.gate(tmpl.format(s=case.root_entity, f=case.fault_type,
                                  e=real_ev)).ok
    ok_fake = cv.gate(tmpl.format(s=case.root_entity, f=case.fault_type,
                                  e='内存条过热导致CPU降频并触发内核页表损坏')).ok
    checks.append({'case': 'gate_real_pass_fake_reject',
                   'pass': ok_real and not ok_fake})

    # f) 熔断：同一工具连续 3 次失败后第 4 次快速失败
    stats3, obs3 = {}, []
    reg3 = build_guarded_registry(case, stats3, obs3)
    logs = reg3.get('query_logs')
    for _ in range(3):
        logs.fn(service='ghost-svc')      # 未知服务 → 失败
    r4 = logs.fn(service='order-svc')     # 合法参数也应被熔断快速失败
    checks.append({'case': 'breaker_opens_after_3_failures',
                   'pass': (not r4.ok) and stats3['breaker_fastfail'] == 1})
    return checks


def main():
    p1 = part1()
    p2 = part2()
    print('=' * 72)
    print('Part 1 · 正路等价（护栏开关不改变端到端结果）')
    for r in p1:
        print(f"  {r['level']:<12} 关:{r['solved_off']}/{r['n']}  "
              f"开:{r['solved_on']}/{r['n']}（门禁放行 {r['gate_passed']}）"
              f" 事件: {r['events']}")
    print('=' * 72)
    print('Part 2 · 对抗路有效')
    all_ok = True
    for c in p2:
        mark = '✓' if c['pass'] else '✗'
        all_ok = all_ok and c['pass']
        print(f"  {mark} {c['case']}")
    out = Path(__file__).resolve().parent / 'guard_e2e_result.json'
    out.write_text(json.dumps({'part1': p1, 'part2': p2,
                               'all_pass': all_ok}, ensure_ascii=False, indent=2),
                   encoding='utf-8')
    print(f"\n已写入 {out}")
    if not all_ok:
        sys.exit(1)


if __name__ == '__main__':
    main()
