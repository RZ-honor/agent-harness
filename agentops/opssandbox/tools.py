# -*- coding: utf-8 -*-
"""
把排障沙箱包装成 Agent 工具。

工具设计的两个关键点
------------------
1. **日志返回的是"整个服务的全量日志"**（约 220 行，2 万字符级）。
   这不是为了刁难模型，而是还原真实可观测性系统：日志就是这么大。
   于是"怎么把它塞进上下文"必然成为一个真问题——这正是项目二的深度点。

2. **只有根因服务的日志里才有异常行**，下游服务只有症状。
   所以 agent 必须先用拓扑判断往哪追，不能靠猜。
"""
from __future__ import annotations

from pathlib import Path

from miniagent.tools import Tool, ToolRegistry, ToolResult

from agentops.opssandbox import scenario


def build_registry(case: scenario.Case) -> ToolRegistry:
    reg = ToolRegistry()

    def _query_logs(service: str = '', keyword: str = '') -> ToolResult:
        if service not in scenario.ALL_SERVICES:
            return ToolResult(False, f'未知服务 {service}。可用：{", ".join(scenario.ALL_SERVICES)}')
        text = scenario.logs_of(case, service)
        if keyword:
            lines = [l for l in text.splitlines() if keyword in l]
            if not lines:
                return ToolResult(False, f'{service} 日志中未找到包含 "{keyword}" 的行。',
                                  {'service': service, 'lines': 0})
            text = '\n'.join(lines)
        # 首行带 service 标记：观测被压缩后仍能识别归属，否则压缩会把
        # 服务标识一起切掉，导致后续无法判断"哪条观测属于哪个服务"。
        text = f'# service={service}\n' + text
        return ToolResult(True, text, {'service': service,
                                       'chars': len(text),
                                       'lines': len(text.splitlines())})
    reg.add(Tool(
        name='query_logs',
        description='查询某个服务的日志。返回该服务最近约 60 分钟的完整日志，'
                    '体量较大（数万字符）。可用 keyword 过滤。',
        params={'service': f'服务名，可选：{", ".join(scenario.ALL_SERVICES)}',
                'keyword': '可选，只返回包含该字符串的行'},
        required=['service'], fn=_query_logs, is_write=False))

    def _query_metrics(service: str = '', metric: str = '') -> ToolResult:
        if service not in scenario.ALL_SERVICES:
            return ToolResult(False, f'未知服务 {service}。')
        text = scenario.metrics_of(case, service)
        return ToolResult(True, text, {'service': service, 'chars': len(text)})
    reg.add(Tool(
        name='query_metrics',
        description='查询某个服务近 60 分钟的时间序列指标（60 个采样点）。',
        params={'service': '服务名', 'metric': '可选，指标名'},
        required=['service'], fn=_query_metrics, is_write=False))

    def _query_changes(service: str = '') -> ToolResult:
        text = scenario.changes_text(case)
        return ToolResult(True, text, {'chars': len(text)})
    reg.add(Tool(
        name='query_changes',
        description='查询最近 2 小时的变更记录（发布/配置/基础设施）。'
                    '注意：大多数变更与故障无关。',
        params={'service': '可选，只看该服务的变更'},
        required=[], fn=_query_changes, is_write=False))

    def _query_topology(service: str = '') -> ToolResult:
        text = scenario.topology_text(case)
        return ToolResult(True, text, {'chars': len(text)})
    reg.add(Tool(
        name='query_topology',
        description='查询服务依赖拓扑，用于判断故障应向上游还是下游追踪。',
        params={'service': '可选，以该服务为中心的拓扑'},
        required=[], fn=_query_topology, is_write=False))

    def _search_tickets(keyword: str = '') -> ToolResult:
        text = scenario.tickets_text(case, keyword)
        return ToolResult(True, text, {'chars': len(text)})
    reg.add(Tool(
        name='search_tickets',
        description='检索历史工单，参考过去同类故障的处置经验。'
                    '注意：工单只提供经验，不是当前故障的证据。',
        params={'keyword': '可选，检索关键词'},
        required=[], fn=_search_tickets, is_write=False))

    def _run_diag(service: str = '', command: str = 'inspect') -> ToolResult:
        if service not in scenario.ALL_SERVICES:
            return ToolResult(False, f'未知服务 {service}。')
        text = scenario.diag_text(case, service, command or 'inspect')
        return ToolResult(True, text, {'service': service, 'chars': len(text)})
    reg.add(Tool(
        name='run_diagnostic',
        description='在指定服务上执行只读诊断命令（不修改任何状态）。',
        params={'service': '服务名', 'command': '诊断命令，默认 inspect'},
        required=['service'], fn=_run_diag, is_write=False))

    return reg


TASK_TEMPLATE = (
    '你是运维排障 Agent。以下是当前告警：\n\n'
    '  {alert}\n\n'
    '请定位**根因服务**与**故障类型**，并给出支撑结论的关键证据（引用原文片段）。\n'
    '注意：告警指向的服务往往只是链路末端，真正根因可能在其下游。\n'
    '最后按以下格式作答：\n'
    '  根因服务：<服务名>\n'
    '  故障类型：<类型>\n'
    '  关键证据：<1-3 条原文片段>\n'
    '  排查路径：<依次查了什么，为什么>'
)


def task_of(case: scenario.Case) -> str:
    return TASK_TEMPLATE.format(alert=case.alert)
