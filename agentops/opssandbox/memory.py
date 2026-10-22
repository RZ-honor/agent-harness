# -*- coding: utf-8 -*-
"""
排障 Agent 的记忆层：跨事件保留"已排除的假设"与"同类故障处置经验"。

为什么这一层必须有
----------------
Agent 应用岗的六层里，Memory 是拼多多 JD 明确写出的（"长期/短期记忆"）。
只有上下文压缩、没有记忆，面试官会判定"这是个压缩器，不是 Agent"。

两类记忆，对应两种真实需求
------------------------
1. **短期（单次事件内）**：已经排除过的假设。
   排障的本质是排除法；不记住"我已查过 mysql 且正常"，Agent 会反复查同一处，
   把工具预算烧光——这是实测过的退化模式（miniagent 里必须用代码拦截重复调用）。

2. **长期（跨事件）**：同类故障的历史处置。
   同一类故障会重复发生。记住"上次 cpu 打满是因为线程池无界队列"，
   下次可以优先验证该假设，而不是从零开始。

与 memsys 的关系
---------------
memsys 已有 Fact / 冲突策略 / user_id 隔离，但那套是**对话事实**模型。
排障记忆的实体不是"用户属性"，而是"假设-证据-结论"三元组，
所以这里单独实现，不强行复用（强行复用会导致属性模型扭曲）。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Hypothesis:
    """一条被检验过的假设。"""
    service: str
    fault_type: str
    verdict: str          # 'excluded' | 'confirmed' | 'pending'
    evidence: str = ''
    event_id: str = ''


@dataclass
class EventMemory:
    """
    单次事件的短期记忆：已查服务、已排除假设、已取证。

    作用：给 Agent 一个"我已经知道什么"的显式视图，
    避免重复查询，也避免在同一假设上反复打转。
    """
    event_id: str
    queried_services: list[str] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    def mark_queried(self, service: str) -> None:
        if service not in self.queried_services:
            self.queried_services.append(service)

    def already_queried(self, service: str) -> bool:
        return service in self.queried_services

    def add_hypothesis(self, h: Hypothesis) -> None:
        self.hypotheses.append(h)

    def excluded(self) -> list[Hypothesis]:
        return [h for h in self.hypotheses if h.verdict == 'excluded']

    def open_services(self, all_services: list[str]) -> list[str]:
        """还没查过的服务，供 Agent 决定下一步。"""
        return [s for s in all_services if s not in self.queried_services]

    def render(self) -> str:
        """给模型看的记忆摘要。体量小，不参与压缩。"""
        lines = [f'# 本次事件已排除 {len(self.excluded())} 个假设']
        for h in self.excluded():
            lines.append(f'  已排除：{h.service} / {h.fault_type}'
                         + (f'（{h.evidence}）' if h.evidence else ''))
        if self.queried_services:
            lines.append(f'  已查询服务：{", ".join(self.queried_services)}')
        return '\n'.join(lines)


class LongTermMemory:
    """
    跨事件长期记忆：同类故障的处置经验。

    命中方式用「故障类型」而非语义相似度：故障类型是有限枚举，
    精确匹配即可，引入向量检索反而增加不可解释性。
    """

    def __init__(self):
        self.records: list[Hypothesis] = []

    def remember(self, h: Hypothesis) -> None:
        if h.verdict == 'confirmed':
            self.records.append(h)

    def recall(self, fault_type: str) -> list[Hypothesis]:
        return [r for r in self.records if r.fault_type == fault_type]

    def priors(self, fault_type: str) -> str:
        """返回该故障类型的历史处置摘要，供 Agent 优先验证。"""
        hits = self.recall(fault_type)
        if not hits:
            return ''
        svcs = sorted({h.service for h in hits})
        return (f'历史上 {fault_type} 故障出现在：{", ".join(svcs)}；'
                f'共 {len(hits)} 次。可优先检查这些位置。')

    def render(self) -> str:
        if not self.records:
            return '# 长期记忆：暂无历史记录'
        by_type: dict[str, int] = {}
        for r in self.records:
            by_type[r.fault_type] = by_type.get(r.fault_type, 0) + 1
        lines = ['# 长期记忆：历史故障统计']
        for ft, n in sorted(by_type.items(), key=lambda x: -x[1]):
            lines.append(f'  {ft}: {n} 次')
        return '\n'.join(lines)
