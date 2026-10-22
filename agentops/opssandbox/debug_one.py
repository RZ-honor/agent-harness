# -*- coding: utf-8 -*-
"""单案例诊断：打印观测、作答、评分，定位证据为何未命中。"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from miniagent.core import AgentConfig, MiniAgent
from agentops.opssandbox import scenario, scoring
from agentops.opssandbox.operator import CompetentOperator
from agentops.opssandbox.tools import build_registry, task_of


def main():
    cid = sys.argv[1] if len(sys.argv) > 1 else 'ops01'
    keep = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    case = scenario.CASE_BY_ID[cid]
    reg = build_registry(case)
    cfg = (AgentConfig(compress_observations=False)
           if keep == 0 else
           AgentConfig(compress_observations=True, obs_limit_chars=keep,
                       obs_window_chars=max(60, keep // 6)))
    model = CompetentOperator(case)
    res = MiniAgent(model, reg, cfg).run(task_of(case))

    print('=' * 70)
    print(f'case={cid}  fault={case.fault_type}  root={case.root_entity}')
    print(f'chain={case.chain}  alert_service={case.alert_service}')
    print(f'stop={res.stop_reason} steps={len(res.steps)} tools={res.tool_calls}')
    print('-' * 70)
    print('工具调用序列：')
    for s in res.steps:
        print(f'  [{s.index}] {s.kind:6} {s.tool:16} {s.args}  '
              f'obs {s.obs_chars_raw}->{s.obs_chars}')
    print('-' * 70)
    print('最终作答：')
    print(res.answer)
    print('-' * 70)
    sc = scoring.score_answer(case, res.answer)
    print(f'predicted_root={sc.predicted_root!r} (期望 {case.root_entity!r})')
    print(f'predicted_type={sc.predicted_type!r} (期望 {case.fault_type!r})')
    print(f'root_hit={sc.root_hit} type_hit={sc.type_hit} evidence_hit={sc.evidence_hit}')
    print(f'total={sc.total:.3f} solved={sc.solved} note={sc.note}')
    print('期望证据：')
    for e in case.evidence:
        print(f'  - {e}')
    print('命中证据：')
    for e in sc.evidence_matched:
        print(f'  + {e}')


if __name__ == '__main__':
    main()
