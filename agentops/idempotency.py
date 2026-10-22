# -*- coding: utf-8 -*-
"""
工具调用幂等：重试风暴下的副作用收敛。

问题
----
Agent 调用写类工具（下单、建单、扣款）时，超时或限流会触发模型重试。
如果工具不幂等，**重试就等于重复执行**，产生重复副作用。

这类 bug 的隐蔽性在于：单次调用测试永远通过，只有在超时/并发下才暴露。

两级保护
--------
1. **幂等键 → 结果缓存**：同一 key 的后续请求直接返回首次结果。
2. **并发单飞（single-flight）**：in-flight 占位 + 等待事件。

第 2 级是最容易漏的。只做结果缓存、不做执行中占位时，并发线程会同时
发现缓存未命中 → 全部进入执行 → 依然重复。这是幂等实现里最典型的错误，
本模块用一个可控实验把这个差异量化出来。
"""
from __future__ import annotations

import argparse
import threading
import time
from dataclasses import dataclass, field


@dataclass
class SideEffectStore:
    """模拟有副作用的存储（如订单表）。加锁模拟真实 DB 的串行写入。"""
    rows: list = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def write(self, payload: str) -> str:
        time.sleep(0.001)              # 模拟 IO 延迟，放大竞态窗口
        with self.lock:
            self.rows.append(payload)
            return f'row_{len(self.rows)}'


class NaiveTool:
    """基线：不幂等，每次调用都写一条。"""
    name = 'naive'

    def __init__(self):
        self.store = SideEffectStore()
        self.calls = 0
        self.hits = 0
        self._lock = threading.Lock()

    def call(self, payload: str, idem_key: str | None = None) -> str:
        with self._lock:
            self.calls += 1
        return self.store.write(payload)

    @property
    def side_effects(self) -> int:
        return len(self.store.rows)


class CacheOnlyTool:
    """
    只做结果缓存的"半幂等"实现——用于证明第 2 级保护的必要性。

    并发下多个线程同时未命中缓存，都会进入执行分支。
    """
    name = 'cache_only'

    def __init__(self):
        self.store = SideEffectStore()
        self.cache: dict[str, str] = {}
        self.calls = 0
        self.hits = 0
        self._lock = threading.Lock()

    def call(self, payload: str, idem_key: str | None = None) -> str:
        with self._lock:
            self.calls += 1
            if idem_key is not None and idem_key in self.cache:
                self.hits += 1
                return self.cache[idem_key]
        # 故意在锁外执行：模拟真实网络请求耗时期间，其他线程也会进来
        rid = self.store.write(payload)
        with self._lock:
            if idem_key is not None:
                self.cache.setdefault(idem_key, rid)
        return rid

    @property
    def side_effects(self) -> int:
        return len(self.store.rows)


class IdempotentTool:
    """完整幂等：结果缓存 + 并发单飞。"""
    name = 'idempotent'

    def __init__(self):
        self.store = SideEffectStore()
        self.cache: dict[str, str] = {}
        self.calls = 0
        self.hits = 0
        self._lock = threading.Lock()
        self._inflight: dict[str, threading.Event] = {}

    def call(self, payload: str, idem_key: str | None = None) -> str:
        with self._lock:
            self.calls += 1
            if idem_key is not None and idem_key in self.cache:
                self.hits += 1
                return self.cache[idem_key]
            ev = self._inflight.get(idem_key) if idem_key is not None else None
            if ev is None:
                ev = threading.Event()
                if idem_key is not None:
                    self._inflight[idem_key] = ev
                owner = True
            else:
                owner = False

        if not owner:
            # 同一 key 已有执行者，等它完成后直接复用结果
            ev.wait(timeout=5)
            with self._lock:
                self.hits += 1
                return self.cache.get(idem_key, '')

        try:
            rid = self.store.write(payload)
            with self._lock:
                if idem_key is not None:
                    self.cache[idem_key] = rid
            return rid
        finally:
            if idem_key is not None:
                with self._lock:
                    self._inflight.pop(idem_key, None)
            ev.set()

    @property
    def side_effects(self) -> int:
        return len(self.store.rows)


def run_retry_storm(tool, n_logical: int, retries: int, concurrent: bool) -> None:
    """
    模拟重试风暴：每个逻辑请求被放大成 retries 次实际调用。

    并发模式的建模很关键，这里踩过一次坑：
    最初写成「不同 key 之间并发、同一 key 的重试串行」，结果 cache_only
    也拿到了 200 条正确结果，完全测不出"缺并发单飞"的缺陷。原因是
    这些实现里同一 key 的多次调用被串行化了，缓存自然次次命中。

    真实场景恰恰相反：**同一逻辑请求的超时重试，是在上一次请求可能仍在
    服务端处理中时发出的**。所以在并发模式下，同一 key 的 retries 次调用
    必须同时发起——只有这样才会出现"多个线程同时未命中缓存并同时进入执行"，
    也就是并发单飞要解决的那个窗口。
    """
    keys = [f'order-{i:04d}' for i in range(n_logical)]

    def one(k: str):
        for _ in range(retries):
            tool.call(f'payload-{k}', idem_key=k)

    if concurrent:
        # 同一 key 的重试也并发：模拟"重试先于上一次完成就到达"
        ths = [threading.Thread(target=one, args=(k,)) for k in keys
               for _ in range(retries)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
    else:
        for k in keys:
            one(k)


@dataclass
class RunResult:
    tool: str
    concurrent: bool
    logical: int
    calls: int
    side_effects: int
    hits: int

    @property
    def duplicates(self) -> int:
        return self.side_effects - self.logical

    @property
    def correct(self) -> bool:
        return self.side_effects == self.logical

    def render(self) -> str:
        tag = '并发' if self.concurrent else '串行'
        return (f'[{tag}] {self.tool:<11} 调用 {self.calls:3d} 次 → '
                f'副作用 {self.side_effects:3d} 条（重复 {self.duplicates:3d}），'
                f'命中 {self.hits:3d} 次，'
                f'{"正确" if self.correct else "仍有重复"}')


def run_experiment(n_logical: int = 200, retries: int = 3) -> list[RunResult]:
    out: list[RunResult] = []
    for concurrent in (False, True):
        for cls in (NaiveTool, CacheOnlyTool, IdempotentTool):
            tool = cls()
            run_retry_storm(tool, n_logical, retries, concurrent)
            out.append(RunResult(
                tool=tool.name, concurrent=concurrent, logical=n_logical,
                calls=tool.calls, side_effects=tool.side_effects, hits=tool.hits,
            ))
    return out


def render(results: list[RunResult], n_logical: int, retries: int) -> str:
    lines = [
        f'逻辑请求 {n_logical} 个，每个最多重试 {retries} 次'
        f'（最坏 {n_logical * retries} 次实际调用）',
        '',
    ]
    for r in results:
        lines.append(r.render())
    # 收敛结论
    naive = next(x for x in results if x.tool == 'naive' and not x.concurrent)
    idem = next(x for x in results if x.tool == 'idempotent' and not x.concurrent)
    lines += [
        '',
        f'副作用收敛：{naive.side_effects} → {idem.side_effects} '
        f'（−{1 - idem.side_effects / max(naive.side_effects, 1):.1%}），'
        f'且正好等于请求数 {n_logical}。',
    ]
    cache = next((x for x in results if x.tool == 'cache_only' and x.concurrent), None)
    if cache is not None:
        lines.append(
            f'对照：只做结果缓存（无并发单飞）在并发下仍产生 '
            f'{cache.side_effects} 条副作用，证明第二级保护不可省略。')
    return '\n'.join(lines)


def main():
    p = argparse.ArgumentParser(description='工具调用幂等 / 重试风暴实验')
    p.add_argument('--n', type=int, default=200)
    p.add_argument('--retries', type=int, default=3)
    args = p.parse_args()
    print(render(run_experiment(args.n, args.retries), args.n, args.retries))


if __name__ == '__main__':
    main()
