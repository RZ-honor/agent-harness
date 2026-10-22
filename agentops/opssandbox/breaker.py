# -*- coding: utf-8 -*-
"""
失败熔断：连续失败达阈值后暂停该工具，窗口期后自动恢复。

依据
----
Datawhale《Agent 应用开发实践踩坑与经验分享》明确的做法：
「3 次失败熔断，300 秒后恢复」。

为什么需要
----------
Agent 在某个工具上连续失败时，模型常见的退化行为是**换个参数继续重试**，
把工具预算烧光。按参数去重拦不住这种（每次参数都不同）。
熔断按**工具维度**统计失败，才能止损。

与既有机制的关系
----------------
· 幂等执行器：按（工具, 参数）去重 —— 拦"完全相同的调用"
· 重复调用拦截：按调用签名去重 —— 拦"同一调用的重复"
· 观测去重：按内容哈希去重 —— 拦"不同参数返回相同内容"
· **熔断：按工具名统计失败** —— 拦"同一工具被反复试错"
四者是不同维度，互补而非重复。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class BreakerState:
    failures: int = 0
    opened_at: float = 0.0
    tripped_count: int = 0


class ToolCircuitBreaker:
    """
    按工具名的失败熔断器。

    threshold: 连续失败多少次后熔断
    cooldown_s: 熔断后多久自动恢复
    """

    def __init__(self, threshold: int = 3, cooldown_s: float = 300.0):
        self.threshold = max(1, threshold)
        self.cooldown_s = cooldown_s
        self._state: dict[str, BreakerState] = {}
        self.tripped_events = 0          # 熔断触发次数，用于统计

    def _st(self, tool: str) -> BreakerState:
        return self._state.setdefault(tool, BreakerState())

    def is_open(self, tool: str, now: float | None = None) -> bool:
        """是否处于熔断中（调用应被拒绝）。超过冷却期则自动恢复。"""
        st = self._st(tool)
        if st.failures < self.threshold:
            return False
        t = time.time() if now is None else now
        if t - st.opened_at >= self.cooldown_s:
            # 冷却结束：半开状态，清零计数放行一次
            st.failures = 0
            st.opened_at = 0.0
            return False
        return True

    def record_failure(self, tool: str, now: float | None = None) -> None:
        st = self._st(tool)
        st.failures += 1
        if st.failures == self.threshold:
            st.opened_at = time.time() if now is None else now
            st.tripped_count += 1
            self.tripped_events += 1

    def record_success(self, tool: str) -> None:
        """成功即清零：只有**连续**失败才熔断，避免累计误伤。"""
        st = self._st(tool)
        st.failures = 0
        st.opened_at = 0.0

    def remaining_s(self, tool: str, now: float | None = None) -> float:
        st = self._st(tool)
        if st.failures < self.threshold or not st.opened_at:
            return 0.0
        t = time.time() if now is None else now
        return max(0.0, self.cooldown_s - (t - st.opened_at))

    def render(self) -> str:
        if not self._state:
            return '熔断器：无记录'
        lines = ['熔断器状态：']
        for name, st in sorted(self._state.items()):
            flag = 'OPEN' if self.is_open(name) else 'closed'
            lines.append(f'  {name}: 连续失败 {st.failures}/{self.threshold} [{flag}]')
        return '\n'.join(lines)