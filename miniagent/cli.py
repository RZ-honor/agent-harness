# -*- coding: utf-8 -*-
"""
mini Agent 的命令行入口。

用法：
  python -m miniagent.cli --task "在本仓库文档里查证据压缩的实测节省率"
  python -m miniagent.cli --demo            # 离线脚本化演示（不调用 API）
  python -m miniagent.cli --ablate          # 两个改进点的消融实验
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from miniagent.core import AgentConfig, MiniAgent
from miniagent.llm import LLMClient, Reply, ScriptedLLM, tool_call
from miniagent.tools import default_registry


def build(demo: bool, cfg: AgentConfig):
    reg = default_registry(BASE, BASE / 'docs')
    if demo:
        script = [
            Reply(tool_calls=[tool_call('search_docs', query='证据压缩 节省',
                                        top_k='3')]),
            Reply(tool_calls=[tool_call('calculator', expr='(126001-54420)/126001*100')]),
            Reply(content='证据压缩实测节省约 56.8%（126,001→54,420 字符），'
                          '依据 docs/三个Agent项目讲解.md 中的测量段落。'),
        ]
        return MiniAgent(ScriptedLLM(script), reg, cfg)
    return MiniAgent(LLMClient(), reg, cfg)


def run_ablation(cfg: AgentConfig):
    """
    消融：分别关掉两个改进点，看指标怎么变。

    为了让对照公平，用同一个脚本化模型序列（固定控制流），
    只改变压缩/幂等开关——否则模型每次输出不同，差异归因不清。
    """
    from miniagent.tools import Tool, ToolResult

    def scenario(compress: bool, idem: bool):
        c = AgentConfig(**{**cfg.__dict__,
                           'compress_observations': compress,
                           'idempotent_tools': idem,
                           'max_steps': 6})
        reg = default_registry(BASE, BASE / 'docs')
        # 让 write_file 被调用两次（模拟模型重试）以检验幂等
        script = [
            Reply(tool_calls=[tool_call('search_docs', query='工具调用 幂等 重试',
                                        top_k='3')]),
            Reply(tool_calls=[tool_call('write_file',
                                        path='.miniagent_out/report.md',
                                        content='临时产物：幂等测试')]),
            Reply(tool_calls=[tool_call('write_file',
                                        path='.miniagent_out/report.md',
                                        content='临时产物：幂等测试')]),
            Reply(content='完成。'),
        ]
        agent = MiniAgent(ScriptedLLM(script), reg, c)
        return agent.run('消融实验：检索并写报告')

    print('=' * 68)
    print('消融实验：两个改进点各自的作用')
    print('=' * 68)
    rows = []
    for compress in (False, True):
        for idem in (False, True):
            r = scenario(compress, idem)
            raw = sum(s.obs_chars_raw for s in r.steps)
            kept = sum(s.obs_chars for s in r.steps)
            rows.append((compress, idem, raw, kept, r.idempotent_hits))
            print(f'\n压缩={compress}  幂等={idem}')
            print(f'  观测字符 raw={raw:,} → 入上下文 {kept:,}')
            print(f'  幂等命中 {r.idempotent_hits} 次')
            print('  ' + r.summary().replace('\n', '\n  '))

    print('\n' + '-' * 68)
    base = next(x for x in rows if not x[0] and not x[1])
    full = next(x for x in rows if x[0] and x[1])
    print(f'无改进：观测 {base[2]:,} 字符，幂等命中 {base[4]}')
    print(f'两个改进：观测 {full[2]:,} 字符（节省 '
          f'{1 - full[3] / max(full[2], 1):.1%}），幂等命中 {full[4]} 次')


def main():
    p = argparse.ArgumentParser(description='mini Agent')
    p.add_argument('--task', default='在本仓库文档里查证据压缩的实测节省率，并给出结论。')
    p.add_argument('--demo', action='store_true', help='离线脚本化演示')
    p.add_argument('--ablate', action='store_true', help='改进点消融实验')
    p.add_argument('--max-steps', type=int, default=8)
    p.add_argument('--force-answer-after', type=int, default=4,
                   help='累计几次工具调用后强制作答（0=不启用）')
    p.add_argument('--no-compress', action='store_true')
    p.add_argument('--no-idem', action='store_true')
    p.add_argument('-v', '--verbose', action='store_true')
    args = p.parse_args()

    cfg = AgentConfig(max_steps=args.max_steps,
                      force_answer_after=args.force_answer_after,
                      compress_observations=not args.no_compress,
                      idempotent_tools=not args.no_idem,
                      verbose=args.verbose)

    if args.ablate:
        run_ablation(cfg)
        return

    agent = build(args.demo, cfg)
    res = agent.run(args.task)
    print('=' * 68)
    print('最终回答')
    print('=' * 68)
    print(res.answer)
    print()
    print('-' * 68)
    print(res.summary())
    if args.verbose:
        print('\n步骤明细：')
        for s in res.steps:
            print(f'  [{s.index}] {s.kind:<5} {s.tool:<12} ok={s.ok} '
                  f'obs {s.obs_chars_raw}→{s.obs_chars} 字符 {s.latency_ms}ms '
                  f'{s.note[:60]}')


if __name__ == '__main__':
    main()
