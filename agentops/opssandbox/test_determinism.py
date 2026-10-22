# -*- coding: utf-8 -*-
"""确定性回归：同进程两次一致；跨 PYTHONHASHSEED 进程输出一致。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from agentops.opssandbox import scenario
from agentops.opssandbox.sweep import sweep


def _rows(limit=4):
    return sweep(scenario.CASES[:limit])


def test_same_process_repeat_identical():
    a, b = _rows(), _rows()
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_cross_hashseed_identical():
    """换 PYTHONHASHSEED 起子进程跑同一扫描，结果必须逐字节一致。"""
    code = (
        "import json,sys;"
        "sys.path.insert(0, r'{}');"
        "from agentops.opssandbox import scenario;"
        "from agentops.opssandbox.sweep import sweep;"
        "print(json.dumps(sweep(scenario.CASES[:4]), sort_keys=True))"
    ).format(str(BASE))
    outs = []
    for seed in ('0', '12345'):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        try:
            r = subprocess.run([sys.executable, '-c', code], capture_output=True,
                               text=True, timeout=120, env=env, cwd=str(BASE))
        except Exception as e:  # 受限环境下无法起子进程则跳过
            import pytest
            pytest.skip(f'subprocess unavailable: {e}')
        assert r.returncode == 0, r.stderr[-400:]
        outs.append(r.stdout.strip())
    assert outs[0] == outs[1], 'PYTHONHASHSEED 影响了输出：仍有不稳定哈希'
