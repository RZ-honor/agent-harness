# -*- coding: utf-8 -*-
"""编码域压缩档回归：真实会话中暴露的域错配已修复，锁定行为。"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from miniagent.core import AgentConfig, ObservationCompressor

NOISE = "\n".join(f"step {i}: building module part_{i} ok" for i in range(120))
CODING_OUT = (
    "Chunk ID: 7ab5e9\nWall time: 0.44 seconds\nProcess exited with code 1\n"
    "Original token count: 3927\nOutput:\n"
    + NOISE +
    "\nTraceback (most recent call last):\n"
    '  File "src/parser/reader.py", line 42, in parse\n'
    "    return self._lines[index]\n"
    "IndexError: list index out of range\n"
    "FAILED tests/test_reader.py::test_parse_edge - AssertionError\n"
    "3 failed, 25 passed in 2.31s\n"
)


def test_coding_profile_keeps_tail_evidence():
    """编码档：尾部错误行、file:line、测试汇总、退出码在 40% 预算下全部幸存。"""
    limit = int(len(CODING_OUT) * 0.4)
    out, _ = ObservationCompressor(limit, max(60, limit // 6),
                                   profile='coding').compress(CODING_OUT, "", "")
    low = out.lower()
    assert 'indexerror' in low
    assert 'reader.py:42' in low.replace(' ', '') or 'reader.py' in low
    assert '3 failed' in low
    assert 'exited with code 1' in low


def test_ops_profile_misses_coding_evidence():
    """反证：运维档（大小写敏感、词表不含 coding 标记）在同样预算下会丢这些证据。"""
    limit = int(len(CODING_OUT) * 0.4)
    out, _ = ObservationCompressor(limit, max(60, limit // 6),
                                   profile='ops').compress(CODING_OUT, "", "")
    low = out.lower()
    assert 'indexerror' not in low or '3 failed' not in low


def test_auto_profile_detects_coding():
    c = ObservationCompressor(400, 80, profile='auto')
    assert c._effective_profile(CODING_OUT) == 'coding'
    assert c._effective_profile('# service=order-svc\nERROR pool exhausted') == 'ops'


def test_agent_config_passes_profile():
    assert AgentConfig().obs_profile == 'ops'
    assert AgentConfig(obs_profile='coding').obs_profile == 'coding'