# -*- coding: utf-8 -*-
"""
压缩率扫描：找出上下文压缩的拐点。

实验设计
--------
横轴：单条观测保留比例（100% 不压缩 → 10%）
纵轴：端到端成功率、平均 token、平均步数

为什么用 ScriptedLLM 而不是真模型
-------------------------------
真模型在相同输入下输出会漂移，压缩率的影响会和模型随机性混在一起，无法归因。
因此这里用一个**确定性的"称职排障者"脚本**：它按拓扑追根因、读根因服务日志、
引用证据。它的行为固定，唯一变量就是**它看到的观测被压缩成什么样**。

这样测出来的差异，可以干净地归因到压缩本身——这正是我们想回答的问题。
代价是绝对值不代表真实模型水平，只代表"同等能力下压缩的影响"。这个限制
会写在结论里，不掩盖。
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from miniagent.core import AgentConfig, MiniAgent, ObservationCompressor
from miniagent.llm import Reply, ScriptedLLM, tool_call

from agentops.opssandbox import scenario, scoring
from agentops.opssandbox.operator import CompetentOperator
from agentops.opssandbox.tools import build_registry, task_of


# ---------------------------------------------------------------------------
# 确定性"称职排障者"

# ---------------------------------------------------------------------------
# 单次运行
# ---------------------------------------------------------------------------
@dataclass
class RunResult:
    cid: str
    ratio: float
    score: scoring.Score
    prompt_tokens: int
    steps: int
    tool_calls: int
    obs_raw: int
    obs_kept: int
    stop_reason: str

    @property
    def compress_ratio(self) -> float:
        return self.obs_kept / max(self.obs_raw, 1)


def run_one(case: scenario.Case, keep_chars: int | None) -> RunResult:
    """
    keep_chars=None 表示不压缩（基线）。否则作为单条观测字符上限。
    """
    reg = build_registry(case)
    if keep_chars is None:
        cfg = AgentConfig(compress_observations=False, max_steps=8,
                          max_tool_calls=12, force_answer_after=6)
    else:
        cfg = AgentConfig(compress_observations=True, obs_limit_chars=keep_chars,
                          obs_window_chars=max(60, keep_chars // 6),
                          max_steps=8, max_tool_calls=12, force_answer_after=6)
    model = CompetentOperator(case)
    agent = MiniAgent(model, reg, cfg)
    res = agent.run(task_of(case))
    sc = scoring.score_answer(case, res.answer)
    raw = sum(s.obs_chars_raw for s in res.steps)
    kept = sum(s.obs_chars for s in res.steps)
    return RunResult(case.cid, 0.0, sc, res.prompt_tokens, len(res.steps),
                     res.tool_calls, raw, kept, res.stop_reason)


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------
LEVELS = [
    ('100%(不压缩)', None),
    ('50%', 10000),
    ('20%', 4000),
    ('10%', 2000),
    ('5%', 1000),
    ('2%', 400),
    ('1%', 200),
]


def sweep(cases: list[scenario.Case] | None = None) -> list[dict]:
    cases = cases or scenario.CASES
    rows = []
    for name, keep in LEVELS:
        results = [run_one(c, keep) for c in cases]
        n = len(results)
        solved = sum(r.score.solved for r in results)
        root = sum(r.score.root_hit for r in results)
        ev = sum(r.score.evidence_hit for r in results)
        avg_tok = sum(r.prompt_tokens for r in results) / n
        avg_steps = sum(r.steps for r in results) / n
        raw = sum(r.obs_raw for r in results)
        kept = sum(r.obs_kept for r in results)
        rows.append({
            'level': name,
            'keep_chars': keep,
            'n': n,
            'solved': solved,
            'success_rate': solved / n,
            'root_hit': root,
            'evidence_hit': ev,
            'avg_tokens': avg_tok,
            'avg_steps': avg_steps,
            'obs_raw': raw,
            'obs_kept': kept,
            'obs_ratio': kept / max(raw, 1),
        })
    return rows


def render(rows: list[dict]) -> str:
    head = (f'{"保留比例":<14}{"实际观测比":>10}{"成功率":>10}{"根因":>8}'
            f'{"证据":>8}{"avg_token":>11}{"avg_steps":>11}')
    lines = [head, '-' * len(head)]
    for r in rows:
        lines.append(
            f'{r["level"]:<14}{r["obs_ratio"]:>9.1%}'
            f'{r["success_rate"]:>9.1%}'
            f'{r["root_hit"]:>8}/{r["n"]}'
            f'{r["evidence_hit"]:>8}'
            f'{r["avg_tokens"]:>11.0f}{r["avg_steps"]:>11.1f}')
    return '\n'.join(lines)


def main():
    import argparse
    p = argparse.ArgumentParser(description='压缩率扫描')
    p.add_argument('--json', action='store_true', help='输出 JSON')
    p.add_argument('--limit', type=int, default=0)
    args = p.parse_args()
    cases = scenario.CASES[:args.limit] if args.limit else scenario.CASES
    rows = sweep(cases)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(render(rows))


if __name__ == '__main__':
    main()
