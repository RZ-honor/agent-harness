# -*- coding: utf-8 -*-
"""
Verifier 层：在动作前和结论后各设一道确定性校验。

为什么需要它
-----------
Agent 应用岗把 Verifier 列为独立一层，因为 LLM 的输出天然不可信：
  · 动作前：模型可能调用不存在的服务、传越界参数、对只读系统发写操作
  · 结论后：模型可能给出根因，但引用的"证据"根本不在观测里（幻觉引用）

两道闸都是**确定性规则**，不调 LLM——用 LLM 校验 LLM 会引入同样的不可靠性。

关键设计：证据门禁要求引用**逐字可回溯**
--------------------------------------
"证据存在"的标准不是"措辞相似"，而是引用片段必须能在本轮观测文本中找到原文
（归一化空白后子串匹配）。这是二值的，可程序校验，不受模型措辞漂移影响。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from agentops.opssandbox import scenario


@dataclass
class Verdict:
    ok: bool
    reason: str = ''
    severity: str = 'error'      # 'error' | 'warning'


# 权限分级：读 / 写 / 危险
# 依据一线开发者复盘的真实事故——"让模型自由调工具，差点把测试库给清了"，
# 结论是"凡是动数据的操作，都给它拴上绳"。
TIER_READ = 'read'
TIER_WRITE = 'write'
TIER_DANGER = 'danger'

TIERS = {
    'query_logs': TIER_READ,
    'query_metrics': TIER_READ,
    'query_changes': TIER_READ,
    'query_topology': TIER_READ,
    'search_tickets': TIER_READ,
    'run_diagnostic': TIER_READ,
    'restart_service': TIER_WRITE,
    'scale_replicas': TIER_WRITE,
    'update_config': TIER_WRITE,
    'drop_table': TIER_DANGER,
}


@dataclass
class GuardResult:
    """动作校验结果，含权限分级与是否需人工确认。"""
    ok: bool
    tier: str = TIER_READ
    needs_confirmation: bool = False
    reason: str = ''
    severity: str = 'pass'

    def to_verdict(self) -> 'Verdict':
        return Verdict(self.ok, self.reason, self.severity)


class ActionVerifier:
    """
    动作前置校验：拦截高危/非法调用，并按权限分级决定是否需要人工确认。

    三档处理：
      读操作        → 直接放行
      写操作        → 放行但标记 needs_confirmation（人工确认后才执行）
      危险操作      → 直接拒绝
    """

    READ_ONLY = {'query_logs', 'query_metrics', 'query_changes',
                 'query_topology', 'search_tickets', 'run_diagnostic'}

    def check(self, tool_name: str, args: dict) -> Verdict:
        return self.guard(tool_name, args).to_verdict()

    def guard(self, tool_name: str, args: dict) -> GuardResult:
        tier = TIERS.get(tool_name)
        if tier is None:
            # 未登记的工具一律拒绝——白名单而非黑名单，
            # 因为"忘记登记"比"忘记拉黑"更常见。
            return GuardResult(False, TIER_DANGER, False,
                               f'未登记的工具，拒绝执行：{tool_name}', 'error')

        svc = args.get('service')
        if svc and svc not in scenario.ALL_SERVICES:
            return GuardResult(False, tier, False,
                               f'服务不存在：{svc}', 'error')

        # 只读诊断命令白名单：防止注入 shell
        if tool_name == 'run_diagnostic':
            cmd = (args.get('command') or 'inspect')
            if not re.fullmatch(r'[A-Za-z0-9_\- ]{1,40}', cmd):
                return GuardResult(False, tier, False,
                                   f'诊断命令含非法字符：{cmd!r}', 'error')

        if tier == TIER_DANGER:
            return GuardResult(False, TIER_DANGER, True,
                               f'危险操作，拒绝执行：{tool_name}', 'error')

        if tier == TIER_WRITE:
            return GuardResult(True, TIER_WRITE, True,
                               f'写操作，需人工确认：{tool_name}', 'warning')

        return GuardResult(True, TIER_READ, False, '', 'pass')


@dataclass
class EvidenceCheck:
    claimed: list[str] = field(default_factory=list)
    verified: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.verified)


def _norm(s: str) -> str:
    """归一化：去空白与常见标点，用于子串比对。"""
    return re.sub(r'[\s，,。;；:：、（）()\[\]"\']', '', (s or '')).lower()


class ConclusionVerifier:
    """
    结论后置校验：结论必须有观测里真实存在的证据支撑。

    这是防止"evaluation aliasing"的关键闸门——
    RCA Benchmark 指出，命中告警附近的服务就被判对，是评测中最常见的误判。
    要求逐字引用可回溯，能把这个漏洞堵住。
    """

    MIN_LEN = 8           # 过短的引用容易误命中；低于此长度不参与判定

    def __init__(self, observations: list[str]):
        self.obs = _norm('\n'.join(observations))

    def check(self, answer: str) -> EvidenceCheck:
        res = EvidenceCheck()
        m = re.search(r'关键证据\s*[:：]\s*(.+?)(?=\n\s*(?:排查路径|根因服务|故障类型)\s*[:：]|\Z)',
                      answer or '', re.S)
        raw = m.group(1) if m else ''
        # 证据常以分号/换行分隔多条
        parts = [p.strip() for p in re.split(r'[;；\n]', raw) if p.strip()]
        res.claimed = parts
        for p in parts:
            n = _norm(p)
            if len(n) < self.MIN_LEN:
                continue
            # 逐字可回溯：整段做子串匹配。
            # 早期版本取前 40 字做 probe，会误伤那些"证据串本身很短但确实是原文"
            # 的情况（如 "CPU 使用率 98.7%"、"rtt 480ms"），实测 20 条里有 7 条
            # 真实证据被判为不可回溯。改为整段匹配即可消除这类假阴性。
            if n in self.obs:
                res.verified.append(p)
            else:
                res.unsupported.append(p)
        return res

    def gate(self, answer: str) -> Verdict:
        chk = self.check(answer)
        if not chk.ok:
            return Verdict(False, f'结论无任何可回溯证据（声称 {len(chk.claimed)} 条，'
                                  f'验证 0 条）', 'error')
        if chk.unsupported:
            # 有可回溯证据但也混入了无法回溯的：放行但降级为 warning。
            # 不能因为一条不实的引用就整体否决——真实排障里模型常附带推测性描述。
            return Verdict(True, f'{len(chk.unsupported)} 条证据不可回溯，已标记', 'warning')
        return Verdict(True, '全部证据逐字可回溯', 'pass')
