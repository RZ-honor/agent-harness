# Agent Harness：执行护栏与上下文预算

一个 **Agent 运行时（Harness）** 研究项目：把工具型 Agent 上线前的两类共性障碍——观测膨胀导致上下文失控、模型误调写操作导致越权——做成**可复现、可量化、可回归**的工程验证。

全部代码仅依赖 Python 标准库，**零 API Key、零外部服务**，克隆后一条命令即可复现 README 与简历中的每一个数字。

An Agent runtime harness with execution guardrails (tiered tool permissions, circuit breaker, verbatim-quote gate) and context budget management (observation compression with a measured success-rate cliff). Fully deterministic and offline: every quantitative claim is reproducible with one command.

## 快速开始

```bash
pip install pytest
git clone <repo-url> && cd agent-harness

pytest -q                                  # 80 项测试，全绿即环境正确
python -m agentops.opssandbox.sweep        # 压缩率-成功率曲线（约 1 分钟）
python -m agentops.opssandbox.robustness   # 5 种子随机扰动重跑
python -m agentops.opssandbox.boundary_diag# 失败模式归因
python -m agentops.opssandbox.metrics      # 记忆/门禁/护栏收益测量
python -m agentops.opssandbox.debug_one 3  # 回放单个案例的完整 trace
```

## 简历数字 ↔ 复现命令对照

| 量化结论 | 复现入口 |
|---|---|
| 20 例多步排障任务，验证三类约束 | `sweep.py` 基线档（100% 观测）→ 20/20 |
| 观测保留 33% 时 token 降 63%、通过 19/20 | `python -m agentops.opssandbox.sweep` 输出 20% 预算档 |
| 保留率跌破 10% 后通过数跌至 11/20（工作点留边界） | 同上，5% 档；`boundary_diag.py` 给出失败归因 |
| 5 次随机扰动重跑拐点稳定、临界区极差 35 个百分点 | `python -m agentops.opssandbox.robustness` |
| 根因判定各档位稳定、误差集中在证据保留 | `python -m agentops.opssandbox.boundary_diag` |
| 3% 预算下保留全部关键证据（行级显著性两轮选行） | `miniagent/core.py::ObservationCompressor` + `test_opssandbox.py` |
| 真实引用 20/20 放行、篡改引用 20/20 拦截 | `python -m agentops.opssandbox.metrics`（结论门禁节） |
| 重复查询 80 次 → 25 次（记忆去重） | 同上（记忆节） |
| 71 次调用、日志工具约四成调用量贡献超 95% 观测体积 | `metrics.py` 的工具维度统计 |
| 连续失败 3 次熔断、写操作须确认、危险操作拒绝 | `test_breaker_tiers.py` |
| 压实-重取循环防护（大输出调用保留在去重检查内） | `test_compaction_coupling.py` |

## 架构

```
miniagent/                    运行时核心（模型无关）
├── core.py                   有界 ReAct 循环、三重预算（步数/工具调用/token）、
│                             ObservationCompressor（行级显著性两轮选行）、
│                             ToolExecutor（幂等+重复拦截）、压实与 pin 机制
├── llm.py                    ChatModel 协议 + ScriptedLLM（确定性策略代理）
│                             + LLMClient（OpenAI 兼容，可选接入真实模型）
└── tools.py                  工具注册表、Schema 校验、ToolResult 协议

agentops/opssandbox/          运维排障实验场（确定性合成故障）
├── scenario.py               8 微服务、12 类故障、20 个案例，四层真值
│                             （故障类型/根因服务/调用链/证据行）
├── operator.py               决策策略：先查依赖拓扑定方向，按可疑度逐跳取证，
│                             证据不足时说明缺什么而非硬猜
├── scoring.py                确定性评分：根因 0.5 + 类型 0.2 + 证据 0.3
├── memory.py                 短期假设排除 + 长期故障模式记忆
├── verifier.py               动作门禁（读/写/危险三级分权+确认）
│                             结论门禁（引用逐字可回溯）
├── breaker.py                工具熔断器：连续 3 次失败打开、300 秒冷却
└── sweep / robustness / boundary_diag / metrics   四组实验
```

## 设计决策

1. **为什么用确定性脚本代理做实验**：Harness 的评测对象是运行时机制（压缩、护栏、记忆、门禁），不是模型能力。固定策略代理把模型方差归零，压缩率-成功率曲线的每个百分点都可归因到机制本身；`llm.py` 的 ChatModel 协议允许换接真实模型做二次验证。
2. **压缩只针对日志**：71 次调用的成本归因显示观测体积高度集中在单一工具（约四成调用贡献超 95% 体积），因此压缩决策按工具维度定制，而非一刀切。
3. **工作点选择**：33% 保留率来自 6 档扫描 + 5 种子扰动，拐点位置稳定但临界区成功率极差 35 个百分点——工作点必须留在悬崖之前。
4. **压实与去重耦合**：大输出调用在压实后必须保留在重复检查内，否则触发"压实→重取→再压实"死循环（HolmesGPT 同款问题，`test_compaction_coupling.py` 固化）。

## 已知边界（诚实声明）

- 故障场景为程序化合成的确定性数据（`scenario.py` 种子可控），非真实生产日志；结论刻画的是该场景分布下机制的行为边界。
- 脚本策略代理代表"称职模型"行为上界；接入真实 LLM 时曲线形状预期保持，绝对数值会变。
- 压缩器为纯规则实现（行级显著性），不调用摘要模型——这是成本约束下的主动选择。

## 测试

```
pytest -q          # 80 passed
├── miniagent/tests/test_miniagent.py          运行时核心
├── agentops/tests/test_idempotency.py         幂等与并发单飞
└── agentops/opssandbox/test_*.py              沙箱/分层/熔断/压实耦合
```

## License

MIT
