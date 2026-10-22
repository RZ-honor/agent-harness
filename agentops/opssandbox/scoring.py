# -*- coding: utf-8 -*-
"""
排障结果评分器（确定性规则，不调 LLM）。

为什么坚持确定性
---------------
"根因是否命中"如果用 LLM 打分，就会随模型版本漂移，同一份 trace 今天 0.8
明天 0.6，无法回归、无法比较压缩率的影响。因此这里全部用字符串/规则判定。

三层判分（对齐阿里云 RCA Benchmark 的加权思路，但简化为可解释的三项）：
    root_hit     根因服务是否正确          权重 0.5
    type_hit     故障类型是否正确          权重 0.2
    evidence_hit 关键证据是否被引用        权重 0.3

只报 root_hit 会高估：模型可能"猜对服务但说不出理由"（RCA Benchmark 称之为
evaluation aliasing——命中告警附近的服务即被判对）。因此 evidence 必须单独计分。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from agentops.opssandbox import scenario

# 服务名别名：模型可能写 order-svc / ordersvc / 订单服务
ALIASES = {
    'order-svc': ['order-svc', 'ordersvc', '订单服务', 'order service'],
    'payment-svc': ['payment-svc', 'paymentsvc', '支付服务', 'payment service'],
    'inventory-svc': ['inventory-svc', 'inventorysvc', '库存服务', 'inventory service'],
    'user-svc': ['user-svc', 'usersvc', '用户服务', 'user service'],
    'mysql-main': ['mysql-main', 'mysqlmain', 'mysql 主库', 'mysql主库', 'mysql', '数据库'],
    'redis-cache': ['redis-cache', 'rediscache', 'redis', '缓存服务', '缓存'],
    'mq-order': ['mq-order', 'mqorder', '消息队列', 'mq', 'kafka', 'rocketmq'],
    'gateway': ['gateway', '网关', 'api gateway'],
}

# 故障类型别名
TYPE_ALIASES = {
    'cpu_saturation': ['cpu_saturation', 'cpu 饱和', 'cpu饱和', 'cpu 打满', 'cpu打满',
                       'cpu 使用率过高', 'cpu 飙高', '线程池拒绝'],
    'memory_leak': ['memory_leak', '内存泄漏', '内存泄露', 'oom', '堆内存'],
    'disk_full': ['disk_full', '磁盘写满', '磁盘满', '磁盘空间不足', 'no space'],
    'network_delay': ['network_delay', '网络延迟', '网络抖动', '丢包', 'rtt'],
    'dns_failure': ['dns_failure', 'dns 故障', 'dns解析失败', 'dns 解析失败', 'dns'],
    'conn_pool_exhaust': ['conn_pool_exhaust', '连接池耗尽', '连接池打满', '连接池不足'],
    'slow_query': ['slow_query', '慢查询', '慢 sql', '慢sql', '全表扫描'],
    'cache_avalanche': ['cache_avalanche', '缓存雪崩', '缓存击穿', '缓存失效'],
    'mq_backlog': ['mq_backlog', '消息积压', '消息堆积', '消费积压', 'lag'],
    'config_error': ['config_error', '配置错误', '配置变更错误', '参数配置'],
    'cert_expiry': ['cert_expiry', '证书过期', '证书到期', 'tls 证书'],
    'pod_oomkill': ['pod_oomkill', 'oomkilled', 'pod 重启', '容器被杀', 'exit code 137'],
}


@dataclass
class Score:
    cid: str
    root_hit: bool = False
    type_hit: bool = False
    evidence_hit: bool = False
    evidence_matched: list[str] = field(default_factory=list)
    predicted_root: str = ''
    predicted_type: str = ''
    note: str = ''

    @property
    def total(self) -> float:
        return 0.5 * self.root_hit + 0.2 * self.type_hit + 0.3 * self.evidence_hit

    @property
    def solved(self) -> bool:
        """端到端成功 = 根因对 且 有证据支撑。只看根因会高估。"""
        return self.root_hit and self.evidence_hit


_FIELD_RE = {
    'root': re.compile(r'根因服务\s*[:：]\s*(.+)'),
    'type': re.compile(r'故障类型\s*[:：]\s*(.+)'),
    'evidence': re.compile(r'关键证据\s*[:：]\s*(.+?)(?=\n\s*(?:排查路径|根因服务|故障类型)\s*[:：]|\Z)',
                           re.S),
}


def _norm(s: str) -> str:
    return (s or '').lower().replace(' ', '').replace('_', '').replace('-', '')


def _find_root(answer: str, case: scenario.Case) -> str:
    m = _FIELD_RE['root'].search(answer or '')
    raw = (m.group(1).strip() if m else '')
    # 在声明行里找服务别名；找不到再全文找
    for svc, names in ALIASES.items():
        if any(_norm(n) in _norm(raw) for n in names):
            return svc
    for svc, names in ALIASES.items():
        if any(_norm(n) in _norm(answer) for n in names):
            return svc
    return ''


def _find_type(answer: str, case: scenario.Case = None) -> str:
    m = _FIELD_RE['type'].search(answer or '')
    raw = (m.group(1).strip() if m else '')
    for ft, names in TYPE_ALIASES.items():
        if any(_norm(n) in _norm(raw) for n in names):
            return ft
    for ft, names in TYPE_ALIASES.items():
        if any(_norm(n) in _norm(answer) for n in names):
            return ft
    return ''


def _norm_ev(s: str) -> str:
    """证据比对前归一化：去空白、去标点，避免因排版差异误判。"""
    return re.sub(r'[\s，,。;；:：、（）()\[\]"\']', '', (s or '')).lower()


def score_answer(case: scenario.Case, answer: str) -> Score:
    sc = Score(cid=case.cid)
    sc.predicted_root = _find_root(answer, case)
    sc.predicted_type = _find_type(answer, case)

    sc.root_hit = (sc.predicted_root == case.root_entity)
    sc.type_hit = (sc.predicted_type == case.fault_type)

    ev = _FIELD_RE['evidence'].search(answer or '')
    ev_text = _norm_ev(ev.group(1) if ev else '')
    # 证据判定：任一 ground-truth 证据串的"核心片段"出现在作答中即算命中。
    # 取核心片段而非全文，是因为模型常引用半句；取过短又会被噪声命中，
    # 故要求核心片段 >= 10 字符（已实测：<10 时误判率显著上升）。
    for e in case.evidence:
        core = _norm_ev(e)
        if len(core) >= 10 and core[:max(10, int(len(core) * 0.6))] in ev_text:
            sc.evidence_matched.append(e)
    sc.evidence_hit = bool(sc.evidence_matched)

    if sc.root_hit and not sc.evidence_hit:
        sc.note = '根因猜对但无证据（evaluation aliasing）'
    elif not sc.root_hit and sc.evidence_hit:
        sc.note = '引用了正确证据但根因判错'
    return sc


def render_summary(scores: list[Score]) -> str:
    n = len(scores)
    if not n:
        return '（无结果）'
    root = sum(s.root_hit for s in scores)
    typ = sum(s.type_hit for s in scores)
    ev = sum(s.evidence_hit for s in scores)
    solved = sum(s.solved for s in scores)
    avg = sum(s.total for s in scores) / n
    lines = [
        f'样本数：{n}',
        f'根因命中：{root}/{n}（{root / n:.1%}）',
        f'类型命中：{typ}/{n}（{typ / n:.1%}）',
        f'证据命中：{ev}/{n}（{ev / n:.1%}）',
        f'端到端成功：{solved}/{n}（{solved / n:.1%}）',
        f'加权得分：{avg:.3f}',
    ]
    alias = [s.cid for s in scores if s.root_hit and not s.evidence_hit]
    if alias:
        lines.append(f'仅猜对根因无证据：{len(alias)} 条 → {", ".join(alias[:6])}')
    return '\n'.join(lines)
