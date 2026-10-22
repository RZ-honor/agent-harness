# -*- coding: utf-8 -*-
"""
边界诊断：拐点之后到底发生了什么？

要回答的不是"成功率降了多少"，而是**失败的性质**：
  - 是证据被切掉（信息丢失，压缩器的错）
  - 还是根因判错（推理退化，不是压缩的错）
  - 还是 agent 空转/触顶（控制流的错）

这三者的修复方向完全不同，混在一起报一个"成功率下降"是没有信息量的。
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from agentops.opssandbox import scenario
from agentops.opssandbox.sweep import LEVELS, run_one


def classify(r) -> str:
    """把一次失败归到具体原因。"""
    if r.score.solved:
        return 'ok'
    if r.score.root_hit and not r.score.evidence_hit:
        return 'evidence_lost'          # 根因对，证据被压没了
    if not r.score.root_hit and r.score.evidence_hit:
        return 'root_misjudged'         # 有证据却判错根因
    if not r.score.root_hit and not r.score.evidence_hit:
        return 'both_lost'              # 信息整体丢失
    return 'other'


def main():
    print('=' * 76)
    print('边界诊断：失败性质随压缩档的变化')
    print('=' * 76)
    head = f'{"保留档":<14}{"成功":>7}{"证据丢失":>10}{"根因误判":>10}{"整体丢失":>10}{"token":>9}{"步数":>8}'
    print(head)
    print('-' * len(head))

    detail = {}
    for name, keep in LEVELS:
        results = [run_one(c, keep) for c in scenario.CASES]
        n = len(results)
        cnt = Counter(classify(r) for r in results)
        avg_tok = sum(r.prompt_tokens for r in results) / n
        avg_steps = sum(r.steps for r in results) / n
        detail[name] = {
            'ok': cnt['ok'], 'evidence_lost': cnt['evidence_lost'],
            'root_misjudged': cnt['root_misjudged'], 'both_lost': cnt['both_lost'],
            'tokens': avg_tok, 'steps': avg_steps,
            'failed_cases': [r.cid for r in results if not r.score.solved],
        }
        print(f'{name:<14}{cnt["ok"]:>7}/{n}'
              f'{cnt["evidence_lost"]:>10}{cnt["root_misjudged"]:>10}'
              f'{cnt["both_lost"]:>10}{avg_tok:>9.0f}{avg_steps:>8.1f}')

    print()
    print('=' * 76)
    print('拐点两侧的边际收益（每多省 1% 观测，损失多少成功率）')
    print('=' * 76)
    rows = []
    for name, keep in LEVELS:
        results = [run_one(c, keep) for c in scenario.CASES]
        n = len(results)
        raw = sum(r.obs_raw for r in results)
        kept = sum(r.obs_kept for r in results)
        succ = sum(r.score.solved for r in results) / n
        rows.append((name, kept / max(raw, 1), succ,
                     sum(r.prompt_tokens for r in results) / n))
    for i in range(1, len(rows)):
        pn, pr, ps, pt = rows[i - 1]
        cn, cr, cs, ct = rows[i]
        d_ratio = (pr - cr) * 100
        d_succ = (ps - cs) * 100
        d_tok = pt - ct
        if d_ratio > 0.5:
            cost = d_succ / d_ratio
            print(f'  {pn:>12} → {cn:<12} 压缩 +{d_ratio:5.1f}pp  '
                  f'成功率 {d_succ:+6.1f}pp  省 token {d_tok:5.0f}  '
                  f'边际代价 {cost:+6.2f}pp/pp')

    print()
    print('=' * 76)
    print('结论')
    print('=' * 76)
    base = detail['100%(不压缩)']
    at20 = detail['20%']
    at5 = detail['5%']
    print(f'  基线 token {base["tokens"]:.0f}，成功率 20/20')
    print(f'  20% 档：token {at20["tokens"]:.0f}（省 {1 - at20["tokens"] / base["tokens"]:.0%}），'
          f'成功率 {at20["ok"]}/20（未损）')
    print(f'  5%  档：token {at5["tokens"]:.0f}（省 {1 - at5["tokens"] / base["tokens"]:.0%}），'
          f'成功率 {at5["ok"]}/20（已损）')
    print(f'  5% 档失败构成：证据丢失 {at5["evidence_lost"]}、'
          f'根因误判 {at5["root_misjudged"]}、整体丢失 {at5["both_lost"]}')

    Path('agentops/opssandbox/boundary_result.json').write_text(
        json.dumps(detail, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
