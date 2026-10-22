# -*- coding: utf-8 -*-
"""
确定性"称职排障者"。

为什么需要它
-----------
真模型在相同输入下输出会漂移，压缩率的影响会和模型随机性混在一起，无法归因。
因此这里用一个**行为固定的脚本模型**：按拓扑逐跳向下游追、读日志、引用证据。
它唯一的外部变量就是"它看到的观测被压缩成什么样"。

这样测出的差异可以干净归因到压缩本身。代价：绝对值不代表真实模型水平，
只代表"同等能力下压缩带来的影响"。这个限制写进结论，不掩盖。

关键约束：它**只读观测文本**，不读 ground truth。
因此压缩把证据行切掉时，它必然引不出证据——这是压缩伤害的真实传导路径。
"""
from __future__ import annotations

import re

from miniagent.llm import Reply, estimate_tokens, tool_call

from agentops.opssandbox import scenario


ANOMALY_MARKERS = (
    'RejectedExecution', 'OutOfMemory', 'No space left', 'certificate has expired',
    'OOMKilled', 'Connection is not available', 'pool exhausted',
    'cache hit rate', 'consumer lag', 'UnknownHost', 'handshake failed',
    'filesort', 'ConfigChangeEvent', 'retransmit', 'rtt ',
    'Threads_running', '使用率', 'thread_pool_active', 'disk_available',
    'heap_used', 'notAfter', 'active=', 'lag ', 'query_time', '连接数',
    'key 同时过期', '批量 key', '被清空', '阻塞',
)

# 噪声行特征：健康检查、访问日志这类行**永远不构成证据**，
# 即使它们碰巧包含 pool/lag 等词。不排除的话，压缩后剩余的高频噪声行
# 会把真正的异常行挤掉，测出来的"证据命中率"其实是噪声命中率。
NOISE_MARKERS = ('health check ok', 'AccessLog', 'young gc', 'evict expired',
                 'select id,status', 'decode payload', 'retry 1/3')

# 从证据文本反推故障类型
TYPE_TABLE = [
    ('RejectedExecution', 'cpu_saturation'),
    ('OutOfMemory', 'memory_leak'),
    ('No space left', 'disk_full'),
    ('UnknownHost', 'dns_failure'),
    ('handshake failed', 'cert_expiry'),
    ('OOMKilled', 'pod_oomkill'),
    ('Connection is not available', 'conn_pool_exhaust'),
    ('exhausted', 'conn_pool_exhaust'),
    ('filesort', 'slow_query'),
    ('query_time', 'slow_query'),
    ('hit rate', 'cache_avalanche'),
    ('consumer lag', 'mq_backlog'),
    ('ConfigChangeEvent', 'config_error'),
    ('retransmit', 'network_delay'),
    ('rtt ', 'network_delay'),
]


class CompetentOperator:
    def __init__(self, case: scenario.Case):
        self.case = case
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self._queried: list[str] = []
        self._plan: list[str] | None = None

    # -- 规划：从拓扑推出要依次查哪些服务 -------------------------------
    def _build_plan(self, obs: str) -> list[str]:
        """
        从拓扑观测里解析出"告警服务 → ... → 链路末端"的顺序。
        只依赖观测文本，不依赖 ground truth。
        """
        edges: dict[str, list[str]] = {}
        for line in obs.splitlines():
            if '->' not in line or line.strip().startswith('#'):
                continue
            parts = [p.strip() for p in line.split('->')]
            if len(parts) != 2:
                continue
            a, b = parts
            if a in scenario.ALL_SERVICES and b in scenario.ALL_SERVICES:
                edges.setdefault(a, []).append(b)

        # 从告警服务出发做 DFS，得到"越靠下游越可疑"的顺序。
        # 注意：order 里**必须包含告警服务自身**——很多故障的根因就在告警
        # 服务自己身上（如 cpu 打满、配置错误），只有链路型故障才在下游。
        # 早期版本漏掉了起点，导致单服务故障永远查不到根因。
        order, stack = [], [self.case.alert_service]
        seen = set()
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            order.append(cur)
            for nxt in reversed(edges.get(cur, [])):
                stack.append(nxt)
        return order

    def _next_target(self) -> str:
        for svc in (self._plan or []):
            if svc not in self._queried:
                return svc
        return ''

    # -- 主循环 -----------------------------------------------------------
    def chat(self, messages: list[dict], tools: list[dict]) -> Reply:
        self.calls += 1
        obs_msgs = [m.get('content', '') for m in messages if m.get('role') == 'tool']
        # 记录累计 prompt token（用观测 + 指令估算，保证不同压缩档可比）
        all_text = '\n'.join(m.get('content', '') or '' for m in messages)
        self.prompt_tokens = estimate_tokens(all_text)

        if self._plan is None:
            # 第一轮：先拿拓扑。置为 [] 表示"已请求拓扑、尚未解析"，
            # 否则下一轮会再次命中 is None 分支，把拓扑重复查一遍。
            # 注意：这里**不能**把 alert_service 记进 _queried——那只是查了拓扑，
            # 日志还没读。早期版本这样写导致告警服务自身的日志永远不被读取，
            # 而单服务故障（cpu/配置/证书）的根因恰恰就在它自己身上。
            self._plan = []
            return Reply(tool_calls=[tool_call('query_topology',
                                               service=self.case.alert_service)])

        if not self._plan:
            self._plan = self._build_plan('\n'.join(obs_msgs))

        # 依次读每个服务的日志
        nxt = self._next_target()
        if nxt:
            self._queried.append(nxt)
            return Reply(tool_calls=[tool_call('query_logs', service=nxt)])

        # 日志都读完了 → 对最可疑的服务取指标佐证（只取一次）
        if not any(m.startswith('__metrics__') for m in self._queried):
            root = self._pick_root(obs_msgs)
            self._queried.append('__metrics__')
            return Reply(tool_calls=[tool_call('query_metrics', service=root)])

        return Reply(content=self._answer(obs_msgs))

    # -- 判定 -------------------------------------------------------------
    @staticmethod
    def _service_of(msg: str) -> str:
        m = re.search(r'service=([a-z\-]+)', msg[:300])
        return m.group(1) if m else ''

    def _pick_root(self, obs_msgs: list[str]) -> str:
        """
        异常标记最密集的服务即根因候选。只数日志观测，不数指标。
        逐行计数而非整段计数，且跳过噪声行——否则健康检查里的 pool=12/20
        会和真正的异常行同权，把根因判到错误的服务上。
        """
        best, best_n = '', -1
        for msg in obs_msgs:
            svc = self._service_of(msg)
            if not svc or 'metric=' in msg[:200]:
                continue
            n = 0
            for ln in msg.splitlines():
                if any(k in ln for k in NOISE_MARKERS):
                    continue
                n += sum(ln.count(k) for k in ANOMALY_MARKERS)
            if n > best_n:
                best, best_n = svc, n
        return best or self.case.alert_service

    def _evidence_lines(self, obs_msgs: list[str]) -> list[str]:
        root = self._pick_root(obs_msgs)
        picked, seen = [], set()
        for msg in obs_msgs:
            if self._service_of(msg) != root or 'metric=' in msg[:200]:
                continue
            for ln in msg.splitlines():
                s = ln.strip()
                if len(s) <= 24 or s in seen:
                    continue
                if any(k in s for k in NOISE_MARKERS):
                    continue
                if any(k in s for k in ANOMALY_MARKERS):
                    seen.add(s)
                    picked.append(s)
        return picked

    def _infer_type(self, text: str) -> str:
        for k, v in TYPE_TABLE:
            if k.lower() in text.lower():
                return v
        return 'unknown'

    def _answer(self, obs_msgs: list[str]) -> str:
        root = self._pick_root(obs_msgs)
        ev = self._evidence_lines(obs_msgs)
        ft = self._infer_type('\n'.join(ev))
        body = '\n          '.join(ev[:3]) if ev else '（压缩后未保留可引用的异常行）'
        return (f'根因服务：{root}\n'
                f'故障类型：{ft}\n'
                f'关键证据：{body}\n'
                f'排查路径：先取依赖拓扑确定追踪方向，再逐跳读取下游服务日志，'
                f'定位异常来源后取指标佐证。')
