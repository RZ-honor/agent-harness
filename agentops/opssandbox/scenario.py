# -*- coding: utf-8 -*-
"""
运维排障沙箱：一个可复现的微服务故障现场。

为什么需要它
-----------
上下文压缩的收益/代价不能在"随便一段长文本"上测。必须有一个**有正确答案的
任务**，才能回答"压到多少会丢证据"这个问题。否则压缩率再漂亮也只是字数统计。

因此这里构造 20 个故障案例，每个案例带四层 ground truth（对齐阿里云 RCA
Benchmark 的口径）：
    1. 故障类型 fault_type
    2. 规范化根因实体 root_entity
    3. 因果传播链 chain
    4. 关键证据检查点 evidence

关键设计：**证据行放在日志中段**（约 50% 位置）。
这样朴素的头尾截断必然丢证据，而关键词窗口压缩能找回——两者才有对比价值。
如果证据放在开头，任何压缩都无损，测不出任何东西。
"""
from __future__ import annotations

import random
import zlib
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# 案例定义
# ---------------------------------------------------------------------------
@dataclass
class Case:
    cid: str
    fault_type: str
    alert: str                       # 任务输入：告警原文
    alert_service: str               # 告警指向的服务（通常是链路末端，不是根因）
    chain: list[str]                 # 传播链：末端 → ... → 根因
    root_entity: str
    evidence: list[str]              # 必须存活的证据检查点
    anomaly: list[str]               # 注入到根因服务日志里的异常行

    @property
    def services(self) -> list[str]:
        return self.chain

    @property
    def decoys(self) -> list[str]:
        """链路之外的服务，用于制造干扰（必须能被排除）。"""
        return [s for s in ALL_SERVICES if s not in self.chain]


ALL_SERVICES = [
    'gateway', 'order-svc', 'payment-svc', 'inventory-svc',
    'user-svc', 'mysql-main', 'redis-cache', 'mq-order',
]

METRIC_OF = {
    'cpu_saturation': 'cpu_usage',
    'memory_leak': 'heap_used',
    'disk_full': 'disk_available',
    'network_delay': 'rtt',
    'dns_failure': 'dns_resolve_ms',
    'conn_pool_exhaust': 'pool_active',
    'slow_query': 'query_time',
    'cache_avalanche': 'cache_hit_rate',
    'mq_backlog': 'consumer_lag',
    'config_error': 'error_rate',
    'cert_expiry': 'ssl_handshake_fail',
    'pod_oomkill': 'container_restarts',
}


def _c(cid, ft, alert, alert_svc, chain, root, evidence, anomaly):
    return Case(cid, ft, alert, alert_svc, chain, root, evidence, anomaly)


CASES: list[Case] = [
    _c('ops01', 'cpu_saturation',
       '告警：订单服务 P99 延迟 3.2s，5xx 错误率 12%，持续 8 分钟',
       'order-svc', ['order-svc', 'mysql-main'], 'order-svc',
       ['CPU 使用率 98.7%', 'thread_pool_active=200/200', 'RejectedExecutionException'],
       ['ERROR [http-nio-8080-exec-7] c.o.web.OrderController - RejectedExecutionException: '
        'Task rejected from java.util.concurrent.ThreadPoolExecutor',
        'WARN  [metrics-reporter] c.o.metrics.SystemMetrics - CPU 使用率 98.7% load=48.2',
        'WARN  [metrics-reporter] c.o.metrics.PoolMetrics - thread_pool_active=200/200 queue=1024']),

    _c('ops02', 'memory_leak',
       '告警：支付服务频繁 Full GC，接口 P99 升至 6.1s',
       'payment-svc', ['payment-svc'], 'payment-svc',
       ['heap_used 3.8GB / 4GB', 'GC pause 4.2s', 'OutOfMemoryError: Java heap space'],
       ['ERROR [GC-worker] jvm.gc - Full GC pause 4.2s, heap_used 3.8GB / 4GB (95%)',
        'ERROR [http-nio-8081-exec-3] c.p.service.PayService - '
        'java.lang.OutOfMemoryError: Java heap space',
        'WARN  [leak-detector] c.p.diag.HeapAnalyzer - retained size growing 180MB/min in PayCache']),

    _c('ops03', 'disk_full',
       '告警：MySQL 主库写入失败，订单创建接口报错',
       'order-svc', ['order-svc', 'mysql-main'], 'mysql-main',
       ['disk_available 0.4GB', 'No space left on device', 'binlog 写入失败'],
       ['ERROR [mysqld] server - Disk is full writing ./binlog.000842 '
        '(errno 28 - No space left on device). Waiting for someone to free space',
        'WARN  [mysqld] monitor - disk_available 0.4GB / 500GB (0.08%)',
        'ERROR [repl] binlog - binlog 写入失败，主从同步已中断 412 秒']),

    _c('ops04', 'network_delay',
       '告警：库存服务调用超时率 23%，上游订单服务降级',
       'order-svc', ['order-svc', 'inventory-svc'], 'inventory-svc',
       ['rtt 480ms', 'TCP retransmit 6.3%', 'upstream timeout after 3000ms'],
       ['WARN  [net-probe] c.i.net.LatencyProbe - rtt 480ms (baseline 12ms), '
        'TCP retransmit 6.3%',
        'ERROR [http-nio-8082-exec-11] c.i.client.WarehouseClient - '
        'upstream timeout after 3000ms, attempt 3/3',
        'WARN  [net-probe] c.i.net.LatencyProbe - packet loss 4.8% on eth0']),

    _c('ops05', 'dns_failure',
       '告警：网关大量 502，下游全部超时',
       'gateway', ['gateway'], 'gateway',
       ['no such host', 'DNS 解析耗时 5000ms', 'resolver 10.0.0.2 timeout'],
       ['ERROR [reactor-http-epoll-2] g.w.RouteHandler - '
        'java.net.UnknownHostException: no such host order-svc.default.svc.cluster.local',
        'WARN  [dns-probe] g.w.dns.ResolverProbe - DNS 解析耗时 5000ms '
        '(resolver 10.0.0.2 timeout)',
        'ERROR [reactor-http-epoll-5] g.w.RouteHandler - upstream unavailable, 502 returned']),

    _c('ops06', 'conn_pool_exhaust',
       '告警：支付服务大量请求排队，TP99 12s',
       'payment-svc', ['payment-svc', 'mysql-main'], 'payment-svc',
       ['active=50/50 idle=0', 'HikariPool-1 - Connection is not available', 'connectionTimeout 30000ms'],
       ['ERROR [HikariPool-1 housekeeper] c.z.h.p.HikariPool - '
        'HikariPool-1 - Connection is not available, request timed out after connectionTimeout 30000ms',
        'WARN  [metrics-reporter] c.p.metrics.PoolMetrics - active=50/50 idle=0 waiters=340',
        'WARN  [HikariPool-1 connection adder] c.z.h.p.HikariPool - '
        'pool exhausted, 340 threads waiting']),

    _c('ops07', 'slow_query',
       '告警：订单列表页加载 9s，数据库 CPU 85%',
       'order-svc', ['order-svc', 'mysql-main'], 'mysql-main',
       ['query_time 8.4s', 'rows_examined 2410000', '未命中索引 idx_order_created'],
       ['WARN  [slow-query-log] mysqld - query_time 8.4s rows_examined 2410000 '
        'rows_sent 20: SELECT * FROM t_order WHERE created_at > ? ORDER BY id DESC LIMIT 20',
        'WARN  [optimizer] mysqld - 未命中索引 idx_order_created，改用全表扫描',
        'WARN  [metrics] mysqld - InnoDB buffer pool hit rate 71% (baseline 99.6%)']),

    _c('ops08', 'cache_avalanche',
       '告警：商品详情接口 RT 从 40ms 升至 2.4s，DB QPS 突增 8 倍',
       'order-svc', ['order-svc', 'redis-cache', 'mysql-main'], 'redis-cache',
       ['cache hit rate 3.1%', 'redis 连接数 9800/10000', '大量 key 同时过期'],
       ['WARN  [metrics] c.r.metrics.CacheMetrics - cache hit rate 3.1% (baseline 96.4%)',
        'WARN  [lettuce-nioEventLoop-4] c.r.pool.ConnPool - redis 连接数 9800/10000，'
        '等待队列 1520',
        'INFO  [ttl-auditor] c.r.diag.TtlAuditor - 大量 key 同时过期：'
        '1284000 个 key 的 TTL 集中在同一分钟']),

    _c('ops09', 'mq_backlog',
       '告警：订单状态不更新，用户投诉；消息队列积压告警',
       'mq-order', ['mq-order', 'order-svc'], 'mq-order',
       ['consumer lag 182000', '队列堆积 18.2 万条', '消费线程 4/4 阻塞'],
       ['WARN  [metrics] mq.consumer - consumer lag 182000 (baseline < 50)',
        'WARN  [queue-monitor] mq.broker - 队列堆积 18.2 万条，'
        '最早未消费消息延迟 42 分钟',
        'ERROR [consumer-pool-1] mq.consumer - 消费线程 4/4 阻塞，'
        '全部卡在 order-svc 的 updateStatus 调用']),

    _c('ops10', 'config_error',
       '告警：支付回调超时率 100%，但下游服务健康',
       'payment-svc', ['payment-svc'], 'payment-svc',
       ['timeout=0', '配置项 payment.timeout 由 3000 改为 0', 'ConfigChangeEvent'],
       ['INFO  [config-watcher] c.p.cfg.ConfigWatcher - ConfigChangeEvent: '
        '配置项 payment.timeout 由 3000 改为 0',
        'WARN  [http-nio-8081-exec-9] c.p.client.CallbackClient - timeout=0，'
        '请求立即失败，未发出任何网络包',
        'ERROR [metrics] c.p.metrics.CallbackMetrics - callback error rate 100.0%']),

    _c('ops11', 'cert_expiry',
       '告警：外部合作方回调网关全部 TLS 握手失败',
       'gateway', ['gateway'], 'gateway',
       ['certificate has expired', 'notAfter=2026-09-30', 'SSL handshake failed'],
       ['ERROR [reactor-http-epoll-1] g.w.tls.TlsHandler - SSL handshake failed: '
        'certificate has expired',
        'WARN  [cert-auditor] g.w.tls.CertAuditor - notAfter=2026-09-30, 已过期 21 天',
        'WARN  [cert-auditor] g.w.tls.CertAuditor - 自动续期任务上次成功于 2026-08-15']),

    _c('ops12', 'pod_oomkill',
       '告警：支付服务 Pod 反复重启，副本数不足',
       'payment-svc', ['payment-svc'], 'payment-svc',
       ['OOMKilled', 'exit code 137', 'limits.memory=4Gi'],
       ['WARN  [kubelet] pod/payment-svc-7d9f - Container payment terminated: OOMKilled, '
        'exit code 137',
        'INFO  [kubelet] pod/payment-svc-7d9f - resources.limits.memory=4Gi, '
        'last observed usage 4.02Gi',
        'WARN  [kubelet] pod/payment-svc-7d9f - Back-off restarting failed container '
        '(第 6 次)']),

    _c('ops13', 'cpu_saturation',
       '告警：库存盘点任务超时，批量接口 P99 14s',
       'inventory-svc', ['inventory-svc'], 'inventory-svc',
       ['CPU 使用率 99.2%', 'thread_pool_active=64/64', 'RejectedExecutionException'],
       ['WARN  [metrics-reporter] c.i.metrics.SystemMetrics - CPU 使用率 99.2% load=32.7',
        'ERROR [batch-exec-2] c.i.batch.StocktakeJob - RejectedExecutionException: '
        'Task rejected from ThreadPoolExecutor',
        'WARN  [metrics-reporter] c.i.metrics.PoolMetrics - thread_pool_active=64/64 queue=512']),

    _c('ops14', 'memory_leak',
       '告警：用户服务响应变慢，Pod 内存持续上涨',
       'user-svc', ['user-svc'], 'user-svc',
       ['heap_used 3.6GB / 4GB', 'GC pause 3.9s', 'OutOfMemoryError'],
       ['ERROR [GC-worker] jvm.gc - Full GC pause 3.9s, heap_used 3.6GB / 4GB (90%)',
        'ERROR [http-nio-8083-exec-4] c.u.service.ProfileService - '
        'java.lang.OutOfMemoryError: Java heap space',
        'WARN  [leak-detector] c.u.diag.HeapAnalyzer - SessionCache retained size +240MB/min']),

    _c('ops15', 'slow_query',
       '告警：财务报表导出超时，DB 连接占满',
       'mysql-main', ['mysql-main'], 'mysql-main',
       ['query_time 22.6s', 'rows_examined 18400000', 'filesort'],
       ['WARN  [slow-query-log] mysqld - query_time 22.6s rows_examined 18400000 '
        'rows_sent 1: SELECT SUM(amount) FROM t_settlement WHERE ...',
        'WARN  [optimizer] mysqld - Using filesort，无法利用 idx_settle_date',
        'WARN  [metrics] mysqld - Threads_running 96 (baseline 8)']),

    _c('ops16', 'network_delay',
       '告警：支付服务调银行通道超时，成功率跌至 61%',
       'payment-svc', ['payment-svc', 'gateway'], 'payment-svc',
       ['rtt 720ms', 'TCP retransmit 9.1%', 'upstream timeout after 5000ms'],
       ['WARN  [net-probe] c.p.net.LatencyProbe - rtt 720ms (baseline 18ms), '
        'TCP retransmit 9.1%',
        'ERROR [http-nio-8081-exec-22] c.p.client.BankClient - '
        'upstream timeout after 5000ms',
        'WARN  [net-probe] c.p.net.LatencyProbe - egress interface saturation 94%']),

    _c('ops17', 'conn_pool_exhaust',
       '告警：订单服务查询超时，线程池无可用连接',
       'order-svc', ['order-svc', 'mysql-main'], 'order-svc',
       ['active=100/100 idle=0', 'HikariPool-2 - Connection is not available', 'waiters=812'],
       ['ERROR [HikariPool-2 housekeeper] c.z.h.p.HikariPool - '
        'HikariPool-2 - Connection is not available, request timed out after 30000ms',
        'WARN  [metrics-reporter] c.o.metrics.PoolMetrics - active=100/100 idle=0 waiters=812',
        'WARN  [HikariPool-2 connection adder] c.z.h.p.HikariPool - '
        'pool exhausted, 812 threads waiting']),

    _c('ops18', 'disk_full',
       '告警：消息队列无法持久化，生产者发送失败',
       'mq-order', ['mq-order'], 'mq-order',
       ['disk_available 0.2GB', 'No space left on device', 'message store 写入失败'],
       ['ERROR [broker] mq.store - Disk is full writing message store segment '
        '(errno 28 - No space left on device)',
        'WARN  [disk-monitor] mq.broker - disk_available 0.2GB / 200GB (0.1%)',
        'ERROR [producer-handler] mq.broker - message store 写入失败，'
        '生产者收到 MQBrokerException']),

    _c('ops19', 'config_error',
       '告警：网关路由全部 404，下游服务健康',
       'gateway', ['gateway'], 'gateway',
       ['route.uri=null', '配置项 gateway.routes 被清空', 'ConfigChangeEvent'],
       ['INFO  [config-watcher] g.w.cfg.ConfigWatcher - ConfigChangeEvent: '
        '配置项 gateway.routes 被清空（原 24 条）',
        'WARN  [reactor-http-epoll-3] g.w.RouteHandler - route.uri=null，'
        '所有路由无法匹配',
        'ERROR [metrics] g.w.metrics.RouteMetrics - 404 rate 100.0%']),

    _c('ops20', 'cache_avalanche',
       '告警：会话服务 RT 暴涨，Redis 连接打满',
       'user-svc', ['user-svc', 'redis-cache'], 'redis-cache',
       ['cache hit rate 1.8%', 'redis 连接数 9950/10000', '批量 key 过期'],
       ['WARN  [metrics] c.r.metrics.CacheMetrics - cache hit rate 1.8% (baseline 97.2%)',
        'WARN  [lettuce-nioEventLoop-2] c.r.pool.ConnPool - redis 连接数 9950/10000',
        'INFO  [ttl-auditor] c.r.diag.TtlAuditor - 批量 key 过期：'
        'session:* 共 860000 个同时失效']),
]

CASE_BY_ID = {c.cid: c for c in CASES}


# ---------------------------------------------------------------------------
# 日志生成
# ---------------------------------------------------------------------------
_NOISE = [
    'INFO  [{t}] c.o.web.AccessLog - GET /api/v1/order/{n} 200 23ms',
    'INFO  [{t}] c.o.web.AccessLog - POST /api/v1/order/create 201 41ms',
    'DEBUG [{t}] c.o.dao.OrderDao - select id,status from t_order where id=?',
    'INFO  [{t}] jvm.gc - young gc 11ms, heap 812MB',
    'INFO  [{t}] c.o.task.HealthReporter - health check ok, pool={p}',
    'WARN  [{t}] c.o.util.RetryHelper - retry 1/3 for downstream call',
    'INFO  [{t}] c.o.cache.LocalCache - evict expired entries, size=1842',
    'DEBUG [{t}] c.o.serde.JsonCodec - decode payload 1.2KB',
]

_SERVICES_DISPLAY = {
    'gateway': 'c.g', 'order-svc': 'c.o', 'payment-svc': 'c.p',
    'inventory-svc': 'c.i', 'user-svc': 'c.u',
    'mysql-main': 'mysqld', 'redis-cache': 'c.r', 'mq-order': 'mq',
}


def _ts(sec: int) -> str:
    return f'{sec // 3600 % 24:02d}:{sec // 60 % 60:02d}:{sec % 60:02d}'


def _noise_block(service: str, seed: int, lines: int = 220) -> list[str]:
    rng = random.Random(seed)
    out = []
    t = 8 * 3600
    for i in range(lines):
        t += rng.randint(1, 3)
        tpl = _NOISE[i % len(_NOISE)]
        out.append(tpl.format(
            t=_ts(t),
            n=rng.randint(100000, 999999),
            p=rng.choice(['10/20', '12/20', '8/20', '16/20']),
        ).replace('c.o.', f'{_SERVICES_DISPLAY.get(service, "c.x")}.'))
    return out


_CACHE: dict[tuple[str, str], str] = {}


def logs_of(case: Case, service: str) -> str:
    """返回某服务在该故障下的日志。异常块插在中段（约 50% 位置）。"""
    key = (case.cid, service)
    if key in _CACHE:
        return _CACHE[key]

    noise = _noise_block(service, seed=zlib.crc32(case.cid.encode()) & 0xFFFF)
    if service == case.root_entity:
        block = list(case.anomaly)
        pos = len(noise) // 2
        noise = noise[:pos] + block + noise[pos:]
    elif service in case.chain and service != case.root_entity:
        # 下游只看到症状，看不到根因——这正是"要往下追"的原因
        block = [
            f'WARN  [net-probe] {_SERVICES_DISPLAY.get(service, "c.x")}.net - '
            f'上游调用延迟升高，观察到下游 {case.root_entity} 响应变慢',
            'WARN  [metrics] - error rate 6.4%, 已触发熔断计数',
        ]
        pos = len(noise) // 3
        noise = noise[:pos] + block + noise[pos:]

    text = '\n'.join(noise)
    _CACHE[key] = text
    return text


def metrics_of(case: Case, service: str) -> str:
    """时间序列指标。60 个采样点，异常出现在后半段。"""
    metric = METRIC_OF.get(case.fault_type, 'latency')
    rng = random.Random((zlib.crc32(case.cid.encode()) ^ zlib.crc32(service.encode())) & 0xFFFF)
    base = {'cpu_usage': 22.0, 'heap_used': 1.1, 'disk_available': 480.0,
            'rtt': 12.0, 'dns_resolve_ms': 8.0, 'pool_active': 12.0,
            'query_time': 0.02, 'cache_hit_rate': 96.0, 'consumer_lag': 12.0,
            'error_rate': 0.1, 'ssl_handshake_fail': 0.0,
            'container_restarts': 0.0}.get(metric, 1.0)

    is_root = (service == case.root_entity)
    rows = [f'# service={service} metric={metric} window=60m step=1m']
    for i in range(60):
        v = base * (1 + rng.uniform(-0.06, 0.06))
        if is_root and i >= 40:
            v = base * (1 + (i - 40) * 0.55) + rng.uniform(0, base * 0.1)
        rows.append(f'{_ts(8 * 3600 + i * 60)}  {v:.2f}')
    rows.append(f'# baseline={base}  current={float(rows[-1].split()[-1]):.2f}')
    return '\n'.join(rows)


CHANGES = [
    '2026-10-21 07:42:11  deploy   order-svc        v2.14.0 -> v2.14.1 (rollout 100%)',
    '2026-10-21 07:58:03  config   payment-svc      payment.timeout: 3000 -> 3000 (no-op)',
    '2026-10-21 08:01:47  config   gateway          gateway.routes: 24 -> 24 (no-op)',
    '2026-10-21 08:03:22  deploy   inventory-svc    v1.9.2 -> v1.9.2 (restart only)',
    '2026-10-21 08:05:09  infra    mysql-main       binlog retention 7d -> 7d (no-op)',
    '2026-10-21 08:07:31  config   order-svc        thread.pool.size: 200 -> 200 (no-op)',
]


def changes_text(case: Case) -> str:
    """
    变更记录。**故意让 6 条里有 5 条是 no-op**：
    模型很容易把"最近有变更"当成根因，这是排障里最常见的误判。
    只有与故障类型真正匹配的那一条才是有效变更。
    """
    rows = ['# 最近 2 小时变更记录（共 %d 条）' % len(CHANGES)]
    rows.extend(CHANGES)
    return '\n'.join(rows)


TICKETS = [
    ('cpu 打满 线程池拒绝', 'ops-2291', '2026-08-14', 'order-svc', '扩容线程池 + 限流'),
    ('堆内存泄漏 缓存未设上限', 'ops-2107', '2026-07-02', 'payment-svc', '给 PayCache 加容量上限'),
    ('磁盘写满 binlog 未清理', 'ops-1988', '2026-06-19', 'mysql-main', '加 binlog 清理定时任务'),
    ('DNS 解析超时 集群 DNS 抖动', 'ops-2455', '2026-09-03', 'gateway', '切换备用 resolver'),
    ('连接池耗尽 慢查询占满', 'ops-2312', '2026-08-21', 'payment-svc', '加索引 + 调大连接池'),
    ('Redis 批量 key 同时过期', 'ops-2401', '2026-08-30', 'redis-cache', 'TTL 加随机抖动'),
    ('TLS 证书过期 未告警', 'ops-1877', '2026-05-28', 'gateway', '证书到期前 30 天告警'),
]


def tickets_text(case: Case, keyword: str = '') -> str:
    rows = ['# 历史工单（相似度排序）']
    for title, tid, date, svc, fix in TICKETS:
        rows.append(f'{tid}  {date}  [{svc}]  {title}  → 处置：{fix}')
    rows.append('# 注：工单只给历史处置经验，不构成当前故障的根因证据。')
    return '\n'.join(rows)


def topology_text(case: Case) -> str:
    """依赖拓扑。返回链路顺序，供 agent 决定往哪追。"""
    rows = ['# 服务依赖拓扑（按调用方向：上游 -> 下游）']
    chain = case.chain
    for i in range(len(chain) - 1):
        rows.append(f'{chain[i]}  ->  {chain[i + 1]}')
    if len(chain) == 1:
        rows.append(f'{chain[0]}  ->  (无下游依赖，本服务为链路起点)')
    rows.append('# 旁路依赖：')
    rows.append('order-svc -> redis-cache   payment-svc -> mq-order   user-svc -> redis-cache')
    return '\n'.join(rows)


def diag_text(case: Case, service: str, cmd: str) -> str:
    """只读诊断命令。"""
    if service != case.root_entity:
        return (f'# {service}: {cmd}\n'
                f'诊断结论：本服务指标在正常范围，未见异常。'
                f'建议向上游/下游继续排查。')
    detail = {
        'cpu_saturation': 'top -H 显示 200 个工作线程中 198 个处于 RUNNABLE，'
                          '平均 CPU 占用 98.7%',
        'memory_leak': 'jmap -histo 显示 c.p.cache.PayCache$Entry 实例数 3,842,110，'
                       '占堆 71%',
        'disk_full': 'df -h 显示 /var/lib/mysql 使用率 99.9%，'
                     'binlog.000842 单文件 1.1GB',
        'network_delay': 'mtr 显示到对端 3 跳后丢包 4.8%，RTT 稳定在 480ms',
        'dns_failure': '/etc/resolv.conf 指向 10.0.0.2，该 resolver 无响应',
        'conn_pool_exhaust': 'jstack 显示 340 个线程阻塞在 HikariPool.getConnection',
        'slow_query': 'EXPLAIN 显示 type=ALL，key=NULL，扫描 2,410,000 行',
        'cache_avalanche': 'redis-cli info 显示 connected_clients 9800，'
                           'expired_keys 每秒 42 万',
        'mq_backlog': 'mqadmin consumerProgress 显示 lag 182000，'
                      '4 个消费线程全部 WAITING',
        'config_error': '对比配置中心历史版本，确认参数被改为非法值',
        'cert_expiry': 'openssl s_client 显示 notAfter=2026-09-30，证书已过期',
        'pod_oomkill': 'kubectl describe 显示 Last State: OOMKilled (exit code 137)',
    }.get(case.fault_type, '未发现明显异常。')
    return f'# {service}: {cmd}\n诊断结论：{detail}'
