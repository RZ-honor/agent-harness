# -*- coding: utf-8 -*-
"""幂等工具实验的回归保护：并发单飞与纯缓存的差异必须可复现。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agentops.idempotency import (CacheOnlyTool, IdempotentTool, NaiveTool,
                                  run_experiment, run_retry_storm)


def test_naive_tool_duplicates():
    """基线不幂等：调用几次就产生几条副作用。"""
    t = NaiveTool()
    for _ in range(3):
        t.call('p', idem_key='k1')
    assert t.side_effects == 3


def test_cache_only_serial_is_idempotent():
    """串行下结果缓存足够——正因为如此，才容易误以为实现已经正确。"""
    t = CacheOnlyTool()
    for _ in range(3):
        t.call('p', idem_key='k1')
    assert t.side_effects == 1


def test_idempotent_serial():
    t = IdempotentTool()
    for _ in range(3):
        t.call('p', idem_key='k1')
    assert t.side_effects == 1
    assert t.hits == 2


def test_idempotent_concurrent_same_key():
    """并发下同 key 只能产生 1 条副作用。这是并发单飞存在的唯一理由。"""
    t = IdempotentTool()
    run_retry_storm(t, n_logical=30, retries=4, concurrent=True)
    assert t.side_effects == 30


def test_cache_only_concurrent_same_key_duplicates():
    """反证：只做结果缓存、不做并发单飞，并发下仍会重复。"""
    t = CacheOnlyTool()
    run_retry_storm(t, n_logical=30, retries=4, concurrent=True)
    assert t.side_effects > 30, '未复现并发窗口：cache_only 不应通过'


def test_experiment_side_effects_match_request_count():
    res = run_experiment(n_logical=50, retries=2)
    idem = [r for r in res if r.tool == 'idempotent']
    assert idem and all(r.side_effects == 50 for r in idem)
    assert all(r.correct for r in idem)
