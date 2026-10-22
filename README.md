# Agent Harness：执行护栏与上下文预算

一个 **Agent 运行时（Harness）** 研究项目：把工具型 Agent 上线前的两类共性障碍——观测膨胀导致上下文失控、模型误调写操作导致越权——做成**可复现、可量化、可回归**的工程验证。

全部代码仅依赖 Python 标准库，**零 API Key、零外部服务**，克隆后一条命令即可复现 README 与简历中的每一个数字。

An Agent runtime harness with execution guardrails (tiered tool permissions, confirmation enforcement, circuit breaker, verbatim-quote gate) and context budget management (observation compression with a measured success-rate cliff). Fully deterministic and offline: every quantitative claim is reproducible with one command.

## 快速开始

```bash
pip install pytest
git clone https://github.com/RZ-honor/agent-harness.git && cd agent-harness

pytest -q                                   # 83 项测试，全绿即环境正确（含跨 PYTHONHASHSEED 确定性回归）
python -m agentops.opssandbox.sweep --guard --save   # 压缩率-成功率曲线（护栏回路开启，秒级完成）
python -m agentops.opssandbox.guard_e2e     # 护栏回路端到端：正路等价 + 对抗路拦截
python -m agentops.opssandbox.robustness    # 5 种子随机扰动重跑
python -m agentops.opssandbox.boundary_diag # 失败模式归因（含各档根因未命中数）
python -m agentops.opssandbox.metrics       # 记忆/门禁/护栏 + 真实回路工具成本归因
python -m agentops.opssandbox.debug_one ops03   # 回放单案例完整 trace（也接受 `debug_one 3` 简写）
python -m agentops.realdata.rollout_sweep <rollout.jsonl>  # 用你自己的 codex 会话输出复测压缩器（只输出聚合统计）
```

## 简历数字 ↔ 复现命令对照

> 每个指标的实测值、验证方式与日期固化在 [VERIFICATION.md](VERIFICATION.md)（作者运行 + 独立审核双验，commit `9ac4be2`）；验证不通过的旧声明已撤回或降格，记录在该文末表。

| 量化结论 | 复现入口 |
|---|---|
| 20 例多步排障任务，护栏回路端到端通过 20/20 | `sweep --guard` 基线档 / `guard_e2e` Part 1 |
| 观测保留 33% 时 token 降 63%、19/20 通过 | `sweep --guard` 20% 档（obs_ratio 32.8%，6521→2402） |
| 保留率跌破 10% 后通过数跌至 11/20 | 同上 5% 档；`boundary_diag` 给出失败归因 |
| 工作点以上误差集中在证据保留 | `boundary_diag`：50%/20%/10% 档根因未命中 0/20，失败均为纯证据丢失 |
| 5 次随机扰动，4/5 拐点一致；临界区极差 35 个百分点 | `robustness`（seed2 拐点后移一档，输出中可见） |
| 真实引用 20/20 放行、篡改引用 20/20 拦截 | `metrics`（Verifier 层证据门禁节） |
| 写操作未确认被拦、危险操作直拒、未登记工具白名单拒绝、3 连败熔断 | `guard_e2e` Part 2 六项对抗检查 |
| 71 次调用、query_logs 占 43.7% 调用量贡献 95.3% 观测体积 | `metrics`（真实回路工具成本归因节，护栏回路实测） |
| 重复查询 80→25（查询计划重放口径） | `metrics`（Memory 层，输出明确标注口径为 plan replay 非 live loop） |
| 连续失败 3 次熔断 + 300s 冷却、三级分权 | `test_breaker_tiers.py` + `test_guard_loop.py` |
| 压实-重取循环防护（大输出调用保留在去重检查内） | `test_compaction_coupling.py` |
| 完全确定性（同命令两次输出一致、跨 hash seed 一致） | `test_determinism.py`（含子进程跨 PYTHONHASHSEED 比对） |

## 架构

```
miniagent/                    运行时核心（模型无关）
├── core.py                   有界 ReAct 循环、三重预算（步数/工具调用/token）、
│                             ObservationCompressor（行级显著性两轮选行）、
│                             ToolExecutor（幂等+并发单飞）、压实与 pin 机制
├── llm.py                    ChatModel 协议 + ScriptedLLM（确定性策略代理）
│                             + LLMClient（OpenAI 兼容，可选接入真实模型）
└── tools.py                  工具注册表、Schema 校验、ToolResult 协议

agentops/opssandbox/          运维排障实验场（确定性合成故障）
├── scenario.py               8 微服务、12 类故障、20 个案例，四层真值
│                             （故障类型/根因服务/调用链/证据行）
├── operator.py               决策策略：先查依赖拓扑定方向，按可疑度逐跳取证，
│                             证据不足时说明缺什么而非硬猜
├── tools.py                  只读工具 + 写/危险工具 + 护栏中间件
│                             （build_guarded_registry：熔断→分权→确认→执行→记账）
├── scoring.py                确定性评分：根因 0.5 + 类型 0.2 + 证据 0.3
├── memory.py                 短期假设排除 + 长期故障模式记忆
├── verifier.py               动作门禁（读/写/危险三级分权+确认强制）
│                             结论门禁（引用逐字可回溯，整段子串匹配）
├── breaker.py                工具熔断器：连续 3 次失败打开、300 秒冷却
└── sweep / guard_e2e / robustness / boundary_diag / metrics   五组实验
```

## 设计决策

1. **为什么用确定性脚本代理做实验**：Harness 的评测对象是运行时机制（压缩、护栏、记忆、门禁），不是模型能力。固定策略代理把模型方差归零，压缩率-成功率曲线的每个百分点都可归因到机制本身；`llm.py` 的 ChatModel 协议允许换接真实模型做二次验证。
2. **护栏必须活在回路里**：`--guard` 模式下每次工具调用都穿过 熔断器→ActionVerifier 三级分权→写确认强制，每个最终答案过 ConclusionVerifier。`guard_e2e` Part 1 证明护栏开启不改变正路结果（20/20、19/20 与关护栏一致），Part 2 证明对抗路径全部被拦——"零误伤 + 全拦截"同时成立才算护栏可用。
3. **压缩只针对日志**：真实回路 71 次调用的成本归因显示 query_logs 以 43.7% 调用量贡献 95.3% 观测体积，压缩决策按工具维度定制，不一刀切。
4. **工作点选择**：33% 保留率来自 6 档扫描 + 5 种子扰动。拐点在 10%→5% 之间（4/5 种子一致），临界区成功率极差 35 个百分点——工作点必须留在悬崖之前。
5. **压实与去重耦合**：上下文压实后，大输出调用必须保留在重复检查内，否则触发"压实→重取→再压实"死循环（HolmesGPT 同款问题，`test_compaction_coupling.py` 固化）。

## 已知边界（诚实声明）

- 故障场景为程序化合成的确定性数据（`scenario.py`，稳定哈希保证跨进程一致），非真实生产日志；结论刻画的是该场景分布下机制的行为边界。
- **真实数据复测发现压缩器的适用边界**：在真实编码 Agent 会话的 365 条工具输出上，当前压缩器（运维日志词表调参）劣于头尾截断（8.8% vs 53.6% 证据存活），根因已定位——详见 [VERIFICATION.md](VERIFICATION.md) 的"真实工具调用数据验证"节；域自适应词表 + 尾部加权是下一迭代首要项。
- 脚本策略代理代表"称职模型"行为上界；接入真实 LLM 时曲线形状预期保持，绝对数值会变。
- 根因判定稳定性仅在**工作点及以上档位**成立（50%/20%/10% 档根因未命中 0/20）；跌破 10% 后根因同样退化（5% 档 4/20 未命中）——这本身就是"悬崖"的一部分。
- "重复查询 80→25" 为查询计划重放口径（每案例告警+根因各查两遍的朴素计划 vs 记忆排除后），真实回路总调用为 71 次（见 `metrics` 两节分别标注）。
- 压缩器为纯规则实现（行级显著性），不调用摘要模型——成本约束下的主动选择。
- 本项目经第三方独立审核（见 `AUDIT.md`），审核发现的 4 blocker / 8 major 已逐项修复：确定性哈希、护栏接入回路、成本归因入口、结论口径收敛。

## 测试

```
pytest -q          # 83 passed
├── miniagent/tests/test_miniagent.py          运行时核心
├── agentops/tests/test_idempotency.py         幂等与并发单飞
└── agentops/opssandbox/
    ├── test_opssandbox.py                     沙箱/评分/压缩
    ├── test_layers.py                         记忆与门禁
    ├── test_breaker_tiers.py                  熔断与三级分权
    ├── test_compaction_coupling.py            压实-去重耦合
    ├── test_guard_loop.py                     护栏活在回路（对抗六项）
    └── test_determinism.py                    跨进程确定性回归
```

## License

MIT
