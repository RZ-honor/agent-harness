# -*- coding: utf-8 -*-
"""
稳健性检验：曲线是不是偶然？

三个问题
-------
1. 换随机种子（重新生成日志噪声）后，拐点位置会不会变？
2. 逐案例看，退化是"均匀变差"还是"少数案例突然失败"？
3. 拐点附近失败的具体原因是什么——是证据丢失，还是根因判错？

只报一条曲线是不够的：单次运行可能是巧合。这里给出分布。
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from agentops.opssandbox import scenario
from agentops.opssandbox.sweep import LEVELS, run_one


def clear_log_cache():
    scenario._CACHE.clear()


def run_seed(seed: int) -> dict:
    """
    用给定种子扰动**日志噪声的行数与内容**。

    为什么不能只换模板顺序：那样生成结果完全相同（5 个种子曲线一模一样），
    检验没有意义。真正需要扰动的是"证据行周围的噪声有多少"——这才是
    压缩器要面对的真实变化。

    做法：改变噪声行数（160~300）与模板偏移。故障内容（根因/证据）不变。
    """
    clear_log_cache()
    rng = random.Random(seed)
    n_lines = rng.choice([160, 200, 240, 280, 320])
    offset = rng.randint(0, 7)

    orig = scenario._noise_block

    def patched(service: str, seed: int, lines: int = 220):
        out = orig(service, seed + offset, n_lines)
        # 按偏移旋转，让证据行落在不同相对位置
        if offset and out:
            k = offset * len(out) // 8
            out = out[k:] + out[:k]
        return out

    scenario._noise_block = patched
    try:
        rows = {}
        for name, keep in LEVELS:
            results = [run_one(c, keep) for c in scenario.CASES]
            n = len(results)
            rows[name] = {
                'success': sum(r.score.solved for r in results) / n,
                'root': sum(r.score.root_hit for r in results) / n,
                'evidence': sum(r.score.evidence_hit for r in results) / n,
                'tokens': sum(r.prompt_tokens for r in results) / n,
                'n_lines': n_lines,
                'offset': offset,
            }
        return rows
    finally:
        scenario._noise_block = orig
        clear_log_cache()
    scenario._noise_block = patched
    try:
        rows = {}
        for name, keep in LEVELS:
            results = [run_one(c, keep) for c in scenario.CASES]
            n = len(results)
            rows[name] = {
                'success': sum(r.score.solved for r in results) / n,
                'root': sum(r.score.root_hit for r in results) / n,
                'evidence': sum(r.score.evidence_hit for r in results) / n,
                'tokens': sum(r.prompt_tokens for r in results) / n,
            }
        return rows
    finally:
        scenario._noise_block = orig
        clear_log_cache()


def per_case_breakdown(keep: int | None) -> list[dict]:
    out = []
    for c in scenario.CASES:
        r = run_one(c, keep)
        out.append({
            'cid': c.cid, 'fault': c.fault_type,
            'root_hit': r.score.root_hit,
            'evidence_hit': r.score.evidence_hit,
            'solved': r.score.solved,
            'note': r.score.note,
        })
    return out


def main():
    seeds = [0, 1, 2, 3, 4]
    all_rows = {}
    for s in seeds:
        all_rows[s] = run_seed(s)

    print('=' * 78)
    print('多种子稳健性（每档 = 成功率，5 个种子）')
    print('=' * 78)
    head = f'{"保留档":<14}' + ''.join(f'{("s" + str(s)):>8}' for s in seeds) + f'{"均值":>9}{"极差":>9}'
    print(head)
    print('-' * len(head))
    for name, _ in LEVELS:
        vals = [all_rows[s][name]['success'] for s in seeds]
        mean = sum(vals) / len(vals)
        rng_ = max(vals) - min(vals)
        print(f'{name:<14}' + ''.join(f'{v:>8.0%}' for v in vals)
              + f'{mean:>9.1%}{rng_:>9.1%}')

    print()
    print('=' * 78)
    print('拐点区间稳定性（成功率从 >=85% 跌到 <=70% 的位置）')
    print('=' * 78)
    for s in seeds:
        r = all_rows[s]
        above = [n for n, _ in LEVELS if r[n]['success'] >= 0.85]
        below = [n for n, _ in LEVELS if r[n]['success'] <= 0.70]
        print(f'  seed={s}: 仍可用档={above[-1] if above else "无"}  '
              f'已失效档={below[0] if below else "无"}')

    print()
    print('=' * 78)
    print('逐案例：20% 档（拐点前）失败的是哪些')
    print('=' * 78)
    for row in per_case_breakdown(4000):
        if not row['solved']:
            print(f'  {row["cid"]:6} {row["fault"]:18} root={row["root_hit"]} '
                  f'ev={row["evidence_hit"]}  {row["note"]}')

    print()
    print('=' * 78)
    print('逐案例：5% 档（拐点后）失败的是哪些')
    print('=' * 78)
    for row in per_case_breakdown(1000):
        if not row['solved']:
            print(f'  {row["cid"]:6} {row["fault"]:18} root={row["root_hit"]} '
                  f'ev={row["evidence_hit"]}  {row["note"]}')

    out = Path('agentops/opssandbox/sweep_result.json')
    out.write_text(json.dumps(all_rows, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n已写入 {out}')


if __name__ == '__main__':
    main()
