# AUDIT.md — agent-harness 技术审核报告

- 审核对象：`D:\PROJECT\github\agent-harness`（commit `6e9e989`，`main`，单提交，无 remote）
- 审核时间：2026-10-22
- 审核方式：只读审阅 + 全量独立运行。所有实验命令在仓库副本 `%TEMP%\ah_audit` 中执行（排除 `.git/__pycache__/.pytest_cache`），原仓库工作区在审核结束后 `git status` 仍为空——除本文件外未改动任何仓库文件。
- 运行环境：`D:\MINICONDA\envs\Agent\python.exe`（Python 3.10.20，pytest 9.1.0），`PYTHONIOENCODING=utf-8`
- 立场：默认所有声明不可信，只认亲手运行与阅读到的结果；未参考任何外部信息。

---

## 1. 总体判定

> **需修复后发布（Not yet publishable）。** 核心实验数字（sweep/robustness/metrics 的成功率、token 降幅、探针数）经独立复算基本属实，工程骨架真实存在、能跑、测试全绿；但 README 的"每一条数字都可一条命令复现"与"完全确定性"两条元声明**不成立**，且**执行护栏（三级分权 / 熔断 / 引用门禁 / 记忆）没有一条接进了产生这些数字的运行回路**——它们是单元测试里的孤立即整模块。审核者判定：

- 4 项 **blocker**（见 §2/§3）：测试数不实、README 官方命令直接崩溃、某条数字的"复现入口"根本不产出该数字、跨进程不确定性。
- 8 项 **major**：护栏未接入、写确认未强制、实验回路中三类约束从未触发、"3% 保留全部证据"不实、"根因各档稳定"不实、拐点"稳定"仅 4/5、记忆收益为硬编码反事实、MODULE.md 消融表与当前代码输出矛盾。
- 14 项 **minor**。

修复 blocker + M1/M3/M4/M5/M8 后，本仓库可作为"诚实的离线机制实验"发布；按现状发布，README 的对照表有 3 行经不起面试官当场执行。

**可取信部分**（独立复算通过）：场景规模 8/12/20/四层真值；sweep 三档关键数字（20/20、19/20 + token↓63.2% + 实际保留 32.8%、11/20）；robustness 5 种子与临界区 35.0pp 极差；metrics 的 20/20+20/20、80→25、6/6；"71 次调用 / 四成调用贡献超 95% 体积"的**数字本身**（实测 71 次、query_logs 31 次=43.7% 调用、占 95.29% 体积）；幂等并发单飞（含反证测试，12 次重复无闪烁）；`pip install .` 打包可用；无密钥、无硬编码绝对路径。

---

## 2. 声明核对表（README 逐条）

### 2.1 快速开始与元声明

| # | README 声明（位置） | 实测 | 判定 |
|---|---|---|---|
| 1 | `pytest -q` → "**80 项测试**，全绿即环境正确"（L15）；测试节"`pytest -q # 80 passed`"（L79） | `74 passed in 4.95s`；`--collect-only` = **74 tests collected**。分布：test_miniagent 22 / test_opssandbox 16 / test_breaker_tiers 12 / test_layers 13 / test_compaction_coupling 5 / test_idempotency 6 | **不一致（blocker）**：实际 74，声明 80，虚增 6 条。提交信息本身写的是 `(74 tests)`，README 未同步 |
| 2 | "全部代码仅依赖 **Python 标准库**，零 API Key 零外部服务"（L5） | 全仓 `.py` import 去重：`__future__/argparse/collections/dataclasses/hashlib/json/os/pathlib/random/re/sys/threading/time/typing` + `pytest`（仅测试） + **`openai`（miniagent/llm.py:90）**。`requirements.txt` 只有 pytest ✓。`openai` 为 `LLMClient.__init__` 内懒加载，`import miniagent` 确认不触发 openai ✓ | **基本一致，一处措辞过头（minor m6）**：可选的 `LLMClient` 需要非标准库 `openai`，与"全部代码"字面冲突 |
| 3 | "克隆后**一条命令即可复现 README 与简历中的每一个数字**"（L5）；"Fully deterministic"（L7） | 见 #4、§3-B4：同一条命令两次运行结果不同 | **不一致（blocker）** |
| 4 | 完全确定性 / 同种子同输出 | 同一命令、两个进程：`obs_raw 499395 vs 499404`、`avg_token 2402.95 vs 2402.85`。固定 `PYTHONHASHSEED=0` 两次输出**完全一致**；seed 0 vs seed 7 **不一致** | **不一致（blocker）**，根因见 §3-B4 |
| 5 | `python -m agentops.opssandbox.sweep # 压缩率-成功率曲线（约 1 分钟）`（L16） | 实测 **0.6s**（robustness 2.8s、boundary_diag 1.0s、metrics 0.2s、pytest 6.1s） | **不一致（minor m1）**，量级差 ~100× |
| 6 | `python -m agentops.opssandbox.debug_one 3  # 回放单个案例的完整 trace`（L20） | **崩溃**：`KeyError: '3'`（debug_one.py:21 直接 `CASE_BY_ID[cid]`，案例 ID 是 `ops01…ops20`） | **无法复现（blocker）**。`debug_one ops03` 正常回放（steps=5 / tools=4 / root=evidence 全命中 / total=1.000） |
| 7 | L18 `python -m agentops.opssandbox.boundary_diag# 失败模式归因` | `#` 前缺空格，整行复制执行会把 `boundary_diag#` 当模块名 | **不一致（minor m2）** |

### 2.2 "简历数字 ↔ 复现命令对照表" 逐行核对

| 声明行 | 声明值 | 实测值 | 判定 |
|---|---|---|---|
| 20 例多步排障任务，**验证三类约束** → 基线档 20/20 | 20/20 | sweep 100% 档 **solved 20/20、root 20/20、ev 20/20** ✓。但"三类约束"：20 案例 stop_reason **全部 = `final`**；压实触发 **0 次**；重复调用拦截 **0 步**；峰值 prompt_token 12,139 ≪ 阈值 30,000；平均工具调用 3.55 ≪ 上限 12；平均步数 4.5 ≪ 上限 8 | **数字一致，描述不一致（major M3）**：约束从未被触碰，20/20 恰恰证明约束没有生效 |
| 观测保留 **33%** 时 token 降 **63%**、通过 **19/20**（入口：20% 预算档） | 33% / ↓63% / 19/20 | 20% 档 `obs_ratio = 32.8%`；`avg_token 2403 vs 基线 6522` → **↓63.16%**；solved **19/20**（95.0%） | **一致 ✓**（在 4 个 hash seed 下均成立） |
| 保留率跌破 **10%** 后通过数跌至 **11/20**（5% 档） | 11/20 | 5% 档 `obs_ratio 9.6%`、solved **11/20**（55.0%）；2%/1% 档同为 11/20 | **一致 ✓** |
| 5 次随机扰动重跑**拐点稳定**、临界区**极差 35 个百分点** | 5 种子 / 稳定 / 35pp | 5 个种子 ✓；5% 档各种子成功率 55/70/90/55/55 → 均值 65.0%、**极差 35.0pp ✓**；拐点区间：seed 0/1/3/4 = `10%→5%`，**seed 2 = `5%→2%`**（其 5% 档仍有 90%） | **极差一致 ✓；"拐点稳定"过度陈述（major M6）**：4/5 种子，非全部 |
| **根因判定各档位稳定**、误差集中在证据保留（boundary_diag） | 各档稳定 | sweep 的 root 列：100/50/20/10% 档 = **20/20**，5/2/1% 档 = **16/20**。boundary_diag 每档打印"根因误判 0"，是因为 4 例"根因+证据双失"被归类成"整体丢失"（classify 的 `both_lost`），不进"根因误判"桶 | **部分一致（major M5）**：≥10% 档稳定成立，≤5% 档根因掉 4 例；"误差集中在证据保留"方向正确（5% 档 9 例失败=5 纯证据丢失+4 双失，0 例纯根因误判） |
| **3% 预算下保留全部关键证据**（行级显著性两轮选行） | 全部保留 | 400 字符预算（≈单条 15.1k 字符日志的 2.6%）下：**仅 8/20 案例证据全部存活**，证据条目存活 **34/60**；1200 字符→11/20、48/60。sweep 2% 档 evidence 命中 **11/20** | **不一致（major M4）**。所引 `test_opssandbox.py::test_compressor_keeps_error_lines_under_tight_budget` 只断言 `'RejectedExecution' in out` 这**一个关键词**，未断言"全部关键证据"，测试不支撑该表述 |
| 真实引用 **20/20** 放行、篡改引用 **20/20** 拦截（metrics 结论门禁节） | 20/20 + 20/20 | `真实引用放行 20/20`、`编造引用拦截 20/20`、`总判定准确率 100.0%` ✓ | **数字一致 ✓，测量效力存疑（major M7 附注）**：20 次"篡改"用的是**同一句硬编码假话**（metrics.py:37，含"内核页表损坏"这种绝不可能出现在日志里的句子），且门禁只看根因服务一条日志；这是同义反复，不是对抗性测量 |
| 重复查询 **80 次 → 25 次**（记忆去重） | 80→25 | `无记忆 80 次 → 有记忆 25 次，节省 55 次（68.8%）` ✓ | **数字一致 ✓，性质不符（major M7）**：`naive = 4 × len(CASES)` 是硬编码反事实（"告警与根因各查两遍"的假设），真实 agent 基线只有 **71 次调用（3.55 次/题）**，比"无记忆基线"低 11%——所谓"收益"从未与真实回路比较过 |
| **71 次调用**、日志工具约四成调用量贡献超 **95%** 观测体积（`metrics.py` 的工具维度统计） | 71 / 四成 / >95% | **`metrics.py` 不产出任何工具维度体积统计**（其 4 个函数：memory_gain / verifier_accuracy / action_guard / latency_profile；全仓 grep `71`、`体积` 亦无该计算）。独立测量：总工具调用 **71** ✓，query_logs **31 次 = 43.7% 调用** ✓，占观测体积 **95.29%** ✓ | **数字为真，复现入口错误（blocker B3）**：按 README 给的命令看不到这组数字，需自写脚本 |
| 连续失败 3 次熔断、写操作须确认、危险操作拒绝（test_breaker_tiers.py） | 三条均有测试 | 12 条测试存在且通过 ✓；`breaker.py` 默认 `threshold=3, cooldown_s=300.0` ✓；`drop_table` 拒绝 ✓ | **测试一致 ✓，实现半成品（major M1/M2）**：见 §3 |
| 压实-重取循环防护（大输出调用保留在去重检查内）（test_compaction_coupling.py） | 5 条测试 | 5 条测试存在且通过 ✓；`pin_threshold_chars=4000`、`_pinned_calls` 在 `_compact_history` 中被保留 ✓ | **一致 ✓，但回路中从未触发（major M3）**：所有实验档压实 0 次 |

### 2.3 scenario.py 规模与确定性

| 声明 | 实测 | 判定 |
|---|---|---|
| 8 微服务 | `ALL_SERVICES` = gateway, order-svc, payment-svc, inventory-svc, user-svc, mysql-main, redis-cache, mq-order = **8** | **一致 ✓** |
| 12 类故障 | `METRIC_OF` 12 键；20 案例实际出现 12 个不同 `fault_type`（cpu_saturation/memory_leak/disk_full/network_delay/dns_failure/conn_pool_exhaust/slow_query/cache_avalanche/mq_backlog/config_error/cert_expiry/pod_oomkill） | **一致 ✓** |
| 20 个案例 | `len(CASES)=20`，`cid` 唯一（`test_case_count_and_uniqueness` 亦断言） | **一致 ✓** |
| 四层真值 | 每案例含 `fault_type / root_entity / chain / evidence`（+`anomaly`）；`test_every_case_has_four_layer_ground_truth`、`test_evidence_lines_actually_present_in_root_logs` 全通过 | **一致 ✓** |
| 确定性（同种子同输出） | 两次进程输出字节级不同（见 §2.1 #4） | **不一致（blocker B4）** |

### 2.4 metrics/debug 其余实测值（供交叉核对）

- 动作探针：**6/6 正确**（含 `ghost-svc` 拒绝、`a;b` 注入拒绝、`drop_database` 拒绝）✓
- 工具耗时：`n=20 mean=0.7ms p50=0.9ms p95=2.0ms max=2.1ms`（本机另一次 0.49/0.83/1.18/1.73）——**每次不同**，属墙钟测量，可接受
- `debug_one ops03`：正常回放；`debug_one ops09 1000`：复现"根因猜对但无证据"（`note=根因猜对但无证据（evaluation aliasing）`）✓ 与 boundary_diag 的失败归因一致

### 2.5 实测 sweep 关键数字表（本审核独立运行，`PYTHONHASHSEED` 默认）

| 保留档 | keep_chars | 实际观测比 | 成功率 | solved | root | evidence | avg_token | avg_steps | 相对基线 token |
|---|---|---|---|---|---|---|---|---|---|
| 100%(不压缩) | None | 100.0% | 100.0% | 20/20 | 20/20 | 20 | 6522 | 4.5 | — |
| 50% | 10000 | 60.9% | 95.0% | 19/20 | 20/20 | 19 | 4201 | 4.5 | ↓35.6% |
| **20%** | 4000 | **32.8%** | **95.0%** | **19/20** | 20/20 | 19 | **2403** | 4.5 | **↓63.2%** |
| 10% | 2000 | 19.3% | 90.0% | 18/20 | 20/20 | 18 | 1537 | 4.5 | ↓76.4% |
| **5%** | 1000 | 9.6% | **55.0%** | **11/20** | **16/20** | 11 | 906 | 4.5 | ↓86.1% |
| 2% | 400 | 3.8% | 55.0% | 11/20 | 16/20 | 11 | 519 | 4.5 | ↓92.0% |
| 1% | 200 | 2.3% | 55.0% | 11/20 | 16/20 | 11 | 422 | 4.5 | ↓93.5% |

跨 4 个 `PYTHONHASHSEED`（0/7/42/123）成功率/root/evidence 计数**完全一致**，仅 avg_token 在小数末位漂移（6521.3–6521.9）。

---

## 3. 实现与描述一致性发现

### BLOCKER

**B1 — 测试数虚报：README 80，实际 74。**
`python -m pytest -q` → `74 passed`；`--collect-only -q` → `74 tests collected`。README L15 与 L79 两处写 80，`miniagent/MODULE.md` L62 写的是准确的 22（该文件自洽）。提交信息也是 74。这是 README 的"环境正确性自检门槛"，属于最容易被当场戳穿的数字。

**B2 — README 官方复现命令直接崩溃。**
`python -m agentops.opssandbox.debug_one 3` → `KeyError: '3'`（`debug_one.py:21`）。快速开始第 6 条命令按文档执行即报错，需改为 `debug_one ops03`。

**B3 — 对照表某行的"复现入口"不产出该行数字。**
"71 次调用、约四成调用贡献超 95% 体积"标注入口为 `metrics.py 的工具维度统计`；实测 `metrics.py` 全文无工具维度体积统计（只有 memory / verifier / action_guard / latency 四节，输出里不存在 71 或任何百分比体积）。数字经独立测量为真，但按文档命令**无法复现**，属于"入口—结论"错配。

**B4 — "完全确定性"不成立（跨进程不稳定）。**
- 证据：同一命令两个进程输出不同；固定 `PYTHONHASHSEED=0` 两次一致、seed 0 vs 7 不一致。
- 根因（三处）：
  - `agentops/opssandbox/scenario.py:322` `seed=hash(case.cid) & 0xFFFF` — 对 `str` 取 `hash`，受 PEP 456 每进程哈希随机化影响 → 噪声日志内容随进程变化；
  - `scenario.py:345` `random.Random((hash(case.cid) ^ hash(service)) & 0xFFFF)` — 同上；
  - `miniagent/llm.py:171` `tool_call()` 的 `id = f'call_{name}_{abs(hash(...))}'` → id 数字位数变化写进 assistant 消息，污染 prompt token 统计。
- 影响：README L5/L7 的"完全确定性/每个数字可复现"、场景"同种子同输出"均被证伪；committed `sweep_result.json / boundary_result.json / layers_result.json` 与重生成结果**字节不一致**（成功率一致，token/延迟小数不同）。
- 说明：headline 成功率（本文 §2.5）在测过的 4 个 seed 下未变，所以核心结论未被推翻，被推翻的是"确定性"这一元声明。

### MAJOR

**M1 — 三类护栏（三级分权、熔断、引用门禁）与记忆层全部未接入运行回路。**
grep 证实 `breaker` / `ActionVerifier` / `ConclusionVerifier` / `EventMemory` / `LongTermMemory` 在 `miniagent/core.py`、`agentops/opssandbox/tools.py`、`sweep.py`、`robustness.py`、`boundary_diag.py`、`operator.py` 中**零引用**；仅出现在 `verifier.py/breaker.py/memory.py` 自身定义 + `metrics.py` 的合成调用 + 单元测试。
后果：README 标题级卖点"执行护栏"的所有对外数字（20/20、19/20、11/20、35pp）都是在**没有任何护栏生效**的回路上测出来的；`agentops/opssandbox/tools.py::build_registry` 只注册 6 个只读工具，`TIERS` 里的 `restart_service/scale_replicas/update_config/drop_table` 从未注册，故分权与写幂等在实验场景里无从触发。属于典型"做了但没跑/测了但没接"。

**M2 — "写操作须确认"只有标志，没有强制。**
`verifier.py:107-109` 对 write 档返回 `GuardResult(ok=True, needs_confirmation=True)`；没有任何代码要求调用方携带确认位，也没有任何消费 `needs_confirmation` 的地方。实测 `ActionVerifier().guard('restart_service', {'service':'order-svc'})` → `ok=True`，与携带 `confirmed=True` 时**结果完全相同**。
而 `docs/guardrail_notes.md` L5-L7 明确声称："写工具**必须携带确认标记，否则执行器拒绝并返回可解释的拒绝原因**" —— 该行为在代码中不存在（文档 > 代码）。

**M3 — 实验回路从未触达"三重预算/重复拦截/压实"，与"验证三类约束"的表述冲突。**
实测：所有档位 20 案例 `stop_reason` 全为 `final`；`MiniAgent.compactions` 合计 0；`duplicate=True` 步数 0；峰值 prompt_token 12,139（压实阈值 = `token_budget//2` = 30,000）；工具调用上限 12 与 `force_answer_after=6` 均未触发。
连带影响：README 设计决策 4 与 `docs/guardrail_notes.md` L17-L20 讲述的"压实→重取→再压实死循环"，在本仓库的量化结果里**没有任何经验证据**（只有单元测试 `test_pinned_calls_survive_compaction` 靠 `token_budget=10` 人工制造压实）。

**M4 — "3% 预算下保留全部关键证据"与实测矛盾。**
400 字符（≈2.6%）预算下逐案例核对：证据全存活 **8/20**，条目级 **34/60**；sweep 2% 档 evidence 命中 11/20。所引测试仅断言单个关键词。该行为是把"某一条 ERROR 行没被丢"表述成"全部关键证据保留"。

**M5 — "根因判定各档位稳定"与 sweep 自身数据矛盾，且诊断脚本用措辞掩盖。**
sweep root 列：100/50/20/10% = 20/20，5/2/1% = **16/20**。`boundary_diag.py` 的 `classify()` 把"根因错+证据丢"归入 `both_lost`，于是每档打印"根因误判 0"，读者会误读为"根因从不判错"。更严重的是 `boundary_diag.py:97-101` 的结论区：
- `print(f'  基线 token …，成功率 20/20')` — **"20/20" 是硬编码字面量**，不来自 `detail` 变量；
- `成功率 {at20["ok"]}/20（未损）` — **"未损"是硬编码标签**，而 `at20["ok"]=19`，与基线 20/20 相比就是损失了 1 例；`docs/compression_notes.md` L19 同样写作"任务成功率**无损**"。

**M6 — "拐点稳定"仅在 4/5 种子上成立。**
`robustness.py` 打印：seed 0/1/3/4 为"仍可用 10% / 已失效 5%"，**seed 2 为"仍可用 5% / 已失效 2%"**（其 5% 档成功率 90%）。README L30 写"5 次随机扰动重跑拐点稳定"未加此限定；README L67 的"拐点位置稳定"同样过度（同处的"极差 35pp"是准确的）。committed `sweep_result.json` 里也有同样的 seed 2 数据，作者自己的产物即可反驳该表述。

**M7 — 记忆"80→25"是构造的反事实，且 `memory_notes.md` 声称的另两项测量不存在。**
`metrics.py:21` `naive = 4 * len(scenario.CASES)` 直接假定"每题 4 次查询"；`with_mem` 仅统计 `(alert_service, root_entity)` 去重后的 25。真实回路平均 3.55 次调用/题、共 71 次——**"无记忆基线"高于真实用量**，因此 55 次"节省"不能归因于记忆层。
另外 `docs/memory_notes.md` L6-L7 声称"摆动次数与步数浪费由 metrics 脚本测量"，`memory_notes.md` L18-L19 声称重复拦截收益"由 metrics 在固定场景上复现"——全仓无任何"摆动/oscillation"计数代码。

**M8 — `miniagent/MODULE.md` 的"实测数据"与当前代码不符（且含不存在的引用）。**
- MODULE.md L120-L125 消融表：✗✗=1,656 / ✓✗=1,277 / ✗✓=1,678 / ✓✓=1,299、幂等命中 1。实测 `python -m miniagent.cli --ablate` 四种组合**全部 `raw=1,012 → 1,012`、节省 0.0%、幂等命中 0**，脚本自己打印"两个改进：观测 1,012 字符（节省 0.0%）"。即消融演示当前**测不出任何改进效果**。
- MODULE.md L43 架构图仍写"观测压缩：关键词窗口裁剪"，与 `core.py`（行级显著性两轮选行）和 `docs/compression_notes.md` L11-L15（明确说第一代关键词窗口已废弃）相矛盾——文档描述的是被自己否定的旧实现。
- MODULE.md L107-L116 "真实模型（qwen3.8-flash）…prompt 7104 tok / 10,073→3,656 节省 63.7%"：需 API Key，离线不可复现，与 README"每一个数字可离线复现"冲突（未标注为不可复现）。
- `cli.py:32-33` demo 断言答案来源 `docs/三个Agent项目讲解.md`，该文件不在仓库（`docs/` 只有 3 篇 notes），且"126,001→54,420、56.8%"在本仓无任何可复现来源。

### MINOR

- **m1** sweep 文档耗时"约 1 分钟" vs 实测 0.6s。
- **m2** README L18 缺空格导致复制即失败。
- **m3** `robustness.py:155` / `boundary_diag.py:105` / `metrics.py:129` 用 **cwd 相对路径**写结果 JSON：当包可导入但 cwd≠仓库根（`PYTHONPATH` 方式）时 `FileNotFoundError`，退出码 1；在仓库根运行则会**覆盖已提交的 result JSON**（复现动作必然污染工作区）。另外 `robustness.py` 写的是 `sweep_result.json`（文件名与产出者不符，`sweep.py` 反而不写文件）。
- **m4** `README` L67"6 档扫描"、`docs/compression_notes.md` L19"扫描六档压缩率"，实际 `sweep.LEVELS` 为 **7 档**（含 100% 基线）。
- **m5** `docs/compression_notes.md` L25-L27 声称"金标锚点丢失率被固化为回归门限"——代码中不存在锚点丢失率指标，也没有相应门限测试（只有单案例单关键词断言）。
- **m6** `miniagent/llm.py:90` `from openai import OpenAI`（第三方，未列入 requirements，懒加载）与"全部代码仅依赖标准库"字面不符；同文件 L124-L125 `for…else` 分支不可达（循环必 break/raise）。
- **m7** `.miniagent_out/t.txt`（内容 `hi`）**被 git 跟踪**，是 `test_miniagent.py::test_agent_idempotent_write_collapsed` 写到工作区相对路径留下的产物；`.gitignore` 未包含 `.miniagent_out/`，任何人跑 pytest 都会新增未跟踪目录。该测试的 docstring 自述这样做是为规避"本机受限环境 pytest 临时根目录 PermissionError"——属机器相关的写法选择，把环境妥协固化成了仓库内容。
- **m8** `robustness.py:75-90` 为不可达死代码（`return` 之后的整段复制粘贴，含第二份 `finally`）。
- **m9** 工具参数装饰化：`query_metrics` 忽略 `metric`、`search_tickets` 忽略 `keyword`、`query_changes` 忽略 `service`（始终返回全部 6 条变更）；`query_logs` 描述"最近约 60 分钟的完整日志"，实际生成 220 行、时间戳跨约 7 分钟。
- **m10** `core.py:495` `_obs_buffer` 定义为 **类属性**（所有 MiniAgent 实例共享同一个 dict，`run()` 里 `.clear()` 清的是类级状态）；`_dup_streak`/`compactions` 只在 `run()` 内初始化（未 run 直接调内部方法会 AttributeError）；同一回复中两次同名同参 `tool_call` 会生成相同 id → 第二次 `_obs_buffer.pop()` 返回空串（空 tool 消息违反自家注释里的协议要求）。
- **m11** `idempotency.py`（并发单飞，README 测试节有声明）实现与反证测试均真实有效，但**未出现在 README 架构节**，也无对照表复现入口；`miniagent/cli.py`、`MODULE.md` 同样不在 README 架构树中。
- **m12** `operator.py` 自述"只读观测文本，不读 ground truth"（L14-L15）不严格成立：`_build_plan` 起点与 `_pick_root` 兜底直接用 `self.case.alert_service`（Case 字段，未从告警文本解析）。同时 L53-L54"证据不足时说明缺什么而非硬猜"与实测不符：喂空观测时 `_pick_root([])` → `'inventory-svc'`（= alert_service 兜底），仍输出"根因服务：inventory-svc"，只在证据位写"（压缩后未保留可引用的异常行）"——即**仍在硬猜根因**。
- **m13** `scoring.render_summary`（唯一输出"类型命中/加权得分"的函数）无任何调用者；声明的 0.2 类型权重从未进入任何实验输出，`solved` 实际只看 `root and evidence`。
- **m14** `pyproject.toml` 缺 `license`/`authors`/`readme`/`classifiers`/`dependencies` 元数据（`packages` 路径经实测正确：`pip install --no-build-isolation --no-deps --target` 成功，产出 `miniagent` + `agentops` + `agent_harness-1.0.0.dist-info`）。

### 一致（正面确认）

- 三级分权字典、白名单拒绝未登记工具、shell 注入正则拦截、危险档拒绝：`verifier.py:40-111` + `test_breaker_tiers.py` 6 条分级测试 + `test_layers.py` 4 条 —— 单元层面**名实相符**。
- 熔断 3 次/300 秒：`breaker.py:45` 默认值与 README 一致，`test_breaker_tiers.py` 覆盖打开/未达阈值/冷却恢复/成功清零/按工具隔离/剩余时间 6 条 —— 单元层面**名实相符**（缺的是接线）。
- 引用逐字回溯 + 整段子串：`verifier.py:156-164`（注释还诚实记录了"取前 40 字 probe 导致 20 条里 7 条假阴性"的历史），`_norm` 归一化空白与标点；`test_layers.py` 4 条覆盖真引用放行/幻觉拦截/部分可回溯降级/短引用不计 —— 与 README 表述**一致**。
- 行级显著性两轮选行：`core.py:252-269`（强信号 `sc>=100` 先选、弱信号填余量）、首行保留、显式省略标记（`:275-289`）、单行超长走字符窗口分支 —— 与 README/docs **一致**。
- 压实后大输出调用保留在去重检查内：`core.py:56-58, 562-566, 597-609` + `test_compaction_coupling.py` —— 与 README **一致**（但未在实验回路触发，见 M3）。
- 幂等 + 并发单飞：`idempotency.py` 三级对照（naive/cache_only/idempotent）+ `test_idempotency.py::test_cache_only_concurrent_same_key_duplicates` 反证；实测 `[并发] cache_only 600 条副作用 vs idempotent 200 条`，`run_experiment` 连跑 12 轮无闪烁 —— **真实且可复现**。
- 无密钥、无硬编码绝对路径（全仓 grep `[A-Za-z]:\` 只命中一个 `https://dashscope…` URL），无 `D:\` 依赖；`scenario/sweep/robustness/metrics/debug_one` 均用 `Path(__file__).resolve().parents[N]` 注入 sys.path，跨机器可跑（除 m3 的输出写路径）；`pytest`/`pip install` 均无需本机特殊条件。
- 已知边界声明（README L70-L74：合成数据、脚本代理代表上界、纯规则压缩）—— **诚实且与代码一致**，值得保留。

---

## 4. 质量与诚实性发现

**总体倾向：作者有真实的诚实意识（专门写了"已知边界"、注释里保留失败历史、提交信息写的是准确的 74 tests），但 README 的对照表把"单元级实现"包装成了"运行时护栏"，并把若干文档级措辞提到了实测撑不住的高度。**

分级列出夸大/错误陈述：

| 类型 | 具体 | 证据 |
|---|---|---|
| 数字对不上 | "80 项测试 / 80 passed" | 实测 74；提交信息亦为 74 |
| 文档命令失效 | `debug_one 3` | `KeyError: '3'` |
| 入口错配 | "71 次调用…→ metrics.py 的工具维度统计" | metrics.py 无此统计；数字需自写脚本才得到（值本身正确） |
| 元声明过头 | "完全确定性""每个数字一条命令复现" | 跨进程字节级不同，随 `PYTHONHASHSEED` 漂移 |
| 结论被自身数据反驳 | "根因判定各档位稳定" | sweep root 在 ≤5% 档 16/20 |
| 措辞掩盖 | boundary_diag 打印"根因误判 0""成功率 20/20""（未损）" | 前两者是硬编码/分类桶效应；19/20 被标为"未损"，docs 进一步写成"无损" |
| 断言超出测试 | "3% 预算下保留全部关键证据" | 实测 8/20 全存活、34/60 条目；测试只查 1 个关键词 |
| 反事实当收益 | "重复查询 80→25（记忆去重）" | `naive=4×20` 硬编码，真实用量 71 次；从未与真实回路对比 |
| 同义反复的对抗测试 | "篡改引用 20/20 拦截" | 20 次用同一句硬编码荒诞假话；门禁只看一条日志 |
| 稳定限定缺失 | "5 次扰动拐点稳定" | 1/5 种子拐点后移一档 |
| 文档-代码不一致 | guardrail_notes"执行器拒绝未确认写"；memory_notes"摆动次数由 metrics 测量"；compression_notes"锚点丢失率固化为回归门限"；MODULE.md"关键词窗口裁剪"+ 消融表 1,656/1,277/1,678/1,299 | 代码无该行为/该指标；`--ablate` 实测四组全 1,012、节省 0.0% |
| 测试名不副实 | `test_no_compaction_when_disabled` | 只 `assert cfg.reset_dedup_after_compact is False`，未运行任何行为 |
| 弱断言 | `test_agent_stops_at_max_steps` | 接受 3 种 stop 原因之一，实际命中 `stalled`/`final` 无法区分（作者已在 docstring 中说明，算半诚实） |
| 仓库卫生 | `.miniagent_out/t.txt` 被提交；`.gitignore` 缺 `.miniagent_out/`；三个 result JSON 提交后不可字节复现；`__pycache__/`+`.pytest_cache/` 留在工作区（已被 ignore，未提交） | `git ls-files` 命中；重生成品与提交品 diff |
| 不可离线复现的实测 | MODULE.md"真实模型 qwen3.8-flash…7104 tok / 节省 63.7%"；cli demo"126,001→54,420 / 56.8%"；引用不存在的 `docs/三个Agent项目讲解.md` | 需 API Key / 引用文件不在仓库 / 全仓无可复现来源 |
| 代码瑕疵 | `_obs_buffer` 类属性共享；`ToolExecutor._inflight` 定义未用（单飞在 `idempotency.py`）；`robustness.py` 死代码；`logs_of` 注释"中段约 50%"对下游服务实为 33%；`llm.py` 不可达 `for-else` | 逐处见文件行号 |
| 良好实践 | 无外部服务、无密钥、无绝对路径；`git status` 干净；MIT LICENSE 完整（署名 Zhou Rui (RZ-honor)）；`pip install .` 可用；requirements 仅 pytest；测试跨 4 个 hash seed 全绿；幂等并发测试 12 轮无闪烁 | 实测 |

---

## 5. 修复建议清单（按优先级，具体到文件与行为）

### P0 — blocker，发布前必须修

1. **README L15 / L79：80 → 74**（或补 6 条真实测试把数补上）。建议直接写"74 项测试"，并与 `miniagent/MODULE.md` L62 的"22 项"保持同源。文件：`README.md`。
2. **README L20：`debug_one 3` → `debug_one ops03`**；同时把 `debug_one.py:19-21` 改成对非法 ID 给出友好错误（`if cid not in CASE_BY_ID: sys.exit(f'未知案例 {cid}，可选：ops01…ops20')`），而不是裸 `KeyError`。文件：`README.md`、`agentops/opssandbox/debug_one.py`。
3. **恢复真确定性**（任选其一，并加回归门限）：
   - `agentops/opssandbox/scenario.py:322` `hash(case.cid)` → `zlib.crc32(case.cid.encode()) & 0xFFFF`（或 `int(hashlib.sha1(...).hexdigest()[:4],16)`）；`:345` 同理；
   - `miniagent/llm.py:171` `abs(hash(...))` → `hashlib.sha1(f'{name}{sorted(args)}'.encode()).hexdigest()[:8]`；
   - 新增测试：`test_scenario_is_deterministic_across_hash_seeds` —— 用 `subprocess` 以两个不同 `PYTHONHASHSEED` 跑 `sweep --json`，断言 stdout 逐字节相等。当前该断言必然失败（已验证）。
4. **README L5/L7 的"完全确定性/每个数字可复现"**：在 3 修完前先降级为"同一进程内确定，跨进程 token 级有 ±0.05% 漂移"；并在文档注明 `debug_one`/`sweep` 的 token 数字受估算器与消息 id 长度影响。文件：`README.md`。
5. **README L35 的复现入口**：要么在 `metrics.py` 真加一个 `tool_cost_attribution()`（按工具统计调用次数、调用占比、观测体积占比，输出 `71 / 31 (43.7%) / 95.29%`）并打印，要么把该行入口改为"见 `sweep.py` + 自行统计"。**建议前者**——加 20 行即可让该行名副其实。文件：`agentops/opssandbox/metrics.py`、`README.md`。

### P1 — major，影响可信度

6. **把护栏接进回路（或明确降级为"单元机制"）**（`miniagent/core.py` + `agentops/opssandbox/tools.py`）：
   - `MiniAgent._run_one` 执行前调用 `ActionVerifier.guard(name, args)`：`not ok → Step(ok=False)`；`needs_confirmation and 参数无 confirm 位 → 拒绝执行`；
   - 工具执行失败路径调用 `breaker.record_failure(name)`，进入调用前 `if breaker.is_open(name): 快速失败`；
   - 终答前调用 `ConclusionVerifier(all_obs).gate(answer)`，失败则按 README 所述拦截；
   - `build_registry` 至少注册 1 个 write + 1 个 danger 工具，让三级分权在实验里真被行使。
   - 若不改代码：README L36/L38/L59 与 `docs/guardrail_notes.md` 必须改写为"单元级实现，未在 20 例回路上启用"。
7. **实现 M2 的确认强制**：`GuardResult` 之外加真实门槛——`ActionVerifier.guard(tool,args)` 对 write 档要求 `args.get('confirm')==True`（或独立的 `confirmed=True` 形参），否则 `ok=False`；并补一条测试断言"未确认的写操作被拒绝"。文件：`agentops/opssandbox/verifier.py`、`test_breaker_tiers.py`。
8. **让约束在实验里真被触碰**（否则删除"验证三类约束"表述）：在 `sweep.py` 增加一个 `--tight-budget` 档（`token_budget=15000`、`max_tool_calls=4`、`force_answer_after=3`），并在 `boundary_diag.py` 的表里加一列 `stop_reason` 分布与压实/重复拦截计数，把"护栏/预算生效"变成可见证据。文件：`agentops/opssandbox/sweep.py`、`boundary_diag.py`。
9. **修正 M4/M5/M6/M7 的表述**：
   - README L32 → "400 字符（≈3%）预算下 **8/20** 案例全部证据存活、条目级 **34/60**；ERROR 行在紧预算下优先入选"；
   - README L31 → "≥10% 档根因稳定（20/20）；≤5% 档根因降至 16/20，失败以证据丢失为主（5 纯证据丢失 + 4 双失，0 纯根因误判）"；
   - README L30/L67 → "拐点 4/5 种子位于 10%→5%，1 个种子后移至 5%→2%；临界区极差 35.0pp"；
   - README L34 → 标注"模型化反事实（假设每案例 4 次查询）；真实回路为 71 次调用/3.55 次每案例"。
10. **删除硬编码结论**：`boundary_diag.py:97` 用 `base['ok']` 计算而非字面 `20/20`；`:98-101` 的"（未损）/（已损）"改为按 `base['ok']-at20['ok']` 判定；并在 `classify()` 里把 `both_lost` 单列一栏"根因错误"计数，避免"根因误判 0"的误读。文件：`agentops/opssandbox/boundary_diag.py`。
11. **修 MODULE.md**：L120-L125 消融表要么重跑并写入当前真实值（四组均 1,012 / 节省 0% / 命中 0，并解释为何当前不可测），要么删除该表；L43 "关键词窗口裁剪" → "行级显著性两轮选行"；L107-L116 的真实模型段加"需 API Key，离线不可复现"标注；`cli.py:32-33` 移除对 `docs/三个Agent项目讲解.md` 的引用与不可复现的 56.8%。文件：`miniagent/MODULE.md`、`miniagent/cli.py`。
12. **文档-代码对齐**：`docs/compression_notes.md` L19"成功率无损"→"19/20"、L25-L27"锚点丢失率固化为回归门限"→实现该指标（逐案例统计证据存活率并入测试）或删句；`docs/memory_notes.md` L6-L7"摆动次数与步数浪费由 metrics 测量"→实现或删除。文件：`docs/*.md`。

### P2 — minor，仓库卫生与可用性

13. **输出路径去 cwd 依赖**：`robustness.py:155`、`boundary_diag.py:105`、`metrics.py:129` 改用 `Path(__file__).with_name('...')`；`robustness.py` 的产物改名 `robustness_result.json`；三个 JSON 建议**不再提交**（加进 `.gitignore`），避免"提交品不可字节复现"。
14. **`.gitignore` 增补** `.miniagent_out/`；`git rm --cached .miniagent_out/t.txt`；把 `test_agent_idempotent_write_collapsed` 的写入目标改为 `tempfile`/`monkeypatch` 的可控目录（用 `tmp_path`，或 `tmpdir` factory；若本机确有限制，用 `tempfile.mkdtemp()`），别把产物写进工作区。文件：`.gitignore`、`miniagent/tests/test_miniagent.py`。
15. **删死代码**：`robustness.py:75-90`（不可达的重复块）、`llm.py:124-125`（不可达 `for-else`）、`core.py:309`（未使用的 `_inflight`）、`scenario.py:395 tickets_text` 的未用参数、`query_metrics/query_changes/search_tickets` 的装饰性参数（实现之或从 `params` 移除）。
16. **修 `core.py:495` `_obs_buffer` 类属性 → 实例属性**（在 `__init__` 里建），并把 `_dup_streak`/`compactions` 也在 `__init__` 初始化；`tool_call()` id 改为对 `name+canonical(args)+序号` 取哈希，保证同回复内不重复。
17. **README 微调**：L18 补空格；L16"约 1 分钟"→"约 1 秒（本机 3.10）"；L67/docs"六档"→"七档（含 100% 基线）"；L35/L53 架构树补 `idempotency.py`、`cli.py`、`MODULE.md`；`query_logs` 描述"约 60 分钟"→"约 220 行 / 约 7 分钟"。
18. **`pyproject.toml`** 补 `license = {text = "MIT"}`、`authors`、`readme`、`classifiers`，并加 `[project.optional-dependencies] llm = ["openai"]`，让"仅标准库 + 可选 openai"在元数据里就成立。
19. **`test_no_compaction_when_disabled` 改成行为测试**：跑一次 `reset_dedup_after_compact=False` 的 `run()` 并断言 `ag.compactions == 0` 且去重表未被清空（现在只断言配置字段值）。文件：`agentops/opssandbox/test_compaction_coupling.py`。
20. **`operator.py` 去掉 ground-truth 泄漏**：从 `task_of(case)` 的告警文本解析服务名（或把 alert_service 显式放进任务参数）作为 DFS 起点；`_pick_root` 无异常标记时返回 `''` 并在 `_answer` 中输出"信息不足，缺 X"而非兜底猜告警服务——否则 README L53-L54"证据不足时说明缺什么而非硬猜"应删除。

---

## 附录 A：本审核实际执行的命令与结果

```powershell
# 1) 测试
python -m pytest -q                      -> 74 passed in 4.95s
python -m pytest --collect-only -q       -> 74 tests collected

# 2) sweep（含 --json 两次对拉 + 4 个 PYTHONHASHSEED 对比）
python -m agentops.opssandbox.sweep      -> 见 §2.5 表
两次进程 JSON diff                       -> 不一致（obs_raw 499395 vs 499404）
PYTHONHASHSEED=0 两次                    -> 一致；seed 0 vs 7 -> 不一致

# 3) robustness
python -m agentops.opssandbox.robustness -> 5 种子；5% 档 55/70/90/55/55，均值 65.0%，极差 35.0%
                                            拐点：seed0/1/3/4 = 10%→5%，seed2 = 5%→2%

# 4) boundary_diag
python -m agentops.opssandbox.boundary_diag -> 各档 root_misjudged=0，5% 档 ev=5/both=4
                                              结论区硬编码打印 "成功率 20/20" 与 "（未损）"
python -m agentops.opssandbox.boundary_diag  (cwd≠仓库根, PYTHONPATH 方式)
                                            -> FileNotFoundError，exit 1

# 5) metrics
python -m agentops.opssandbox.metrics    -> 80→25(68.8%)；20/20+20/20；6/6；latency
                                            未输出任何"71 次调用/体积占比"统计

# 6) debug_one
python -m agentops.opssandbox.debug_one 3      -> KeyError: '3'，exit 1   ← README 命令失效
python -m agentops.opssandbox.debug_one ops03  -> 正常（5 步 / 4 次调用 / total=1.000）

# 7) 其它
python -m agentops.idempotency           -> 并发 naive 1800 / cache_only 600 / idempotent 200
python -m miniagent.cli --demo           -> 正常（但答案引用不存在的 docs/三个Agent项目讲解.md）
python -m miniagent.cli --ablate         -> 四组全部 1,012→1,012，节省 0.0%，幂等命中 0
pip install --no-build-isolation --no-deps --target <tmp> .   -> 成功，exit 0
独立测量（71 次调用归因 / 证据存活 / 写确认 / 护栏引用）  -> 见 §2.2、§3

# 8) 稳定性
pytest 在 PYTHONHASHSEED=0/7/42/123 各 1 轮 -> 全部 74 passed
test_idempotency.py 连跑 12 轮 -> 0 失败
```

## 附录 B：审核未触碰的部分

- 未运行 `miniagent.cli --task`（需 `DASHSCOPE_API_KEY`，属外部服务，超出"零 API Key"复现范围）。
- 未在真实 LLM 上二次验证曲线形状（README L73 自己也把它列为已知边界）。
- 未做 `pip install` 后的完整端到端复现（仅验证安装可成功、包路径正确）。


---

# 处置状态（作者复核，2026-10-22）

审核方为独立子 Agent，判定"需修复后发布"。逐条处置如下：

| 编号 | 处置 | 说明 |
|---|---|---|
| B1 测试数不实 | **已修** | README 更正为 83（护栏接入+确定性回归新增 9 项）；并承诺不再在文案中写死会漂移的测试数 |
| B2 `debug_one 3` 崩 | **已修** | 接受数字简写（自动补零规范化），README 示例同步 |
| B3 71 次调用无复现入口 | **已修** | `metrics.py` 新增"真实回路工具成本归因"节：71 次、query_logs 43.7%/95.3%，与审核独立测量一致 |
| B4 确定性被证伪 | **已修** | 三处 `hash()` 全部换 `zlib.crc32`（scenario×2、llm tool_call id）；新增 `test_determinism.py` 含跨 PYTHONHASHSEED 子进程比对，已绿 |
| M1 护栏未接入回路 | **已修** | `build_guarded_registry`：每次工具调用穿过 熔断→三级分权→写确认；`sweep --guard` 与 `guard_e2e.py` 端到端验证：正路开关等价（20/20、19/20），对抗六项全拦 |
| M2 写确认无强制 | **已修** | 中间件强制：未带 confirmed=true 的写调用直接拒绝（test_guard_loop 锁定） |
| M3 三类约束从未触发 | **已修（口径修正）** | 压实/重复拦截在 20 例正路确实 0 触发——这是设计使然（峰值 12k tok < 30k 阈值），README 不再声称"20 例验证了三类约束"，改为：正路零误伤 + 对抗路全拦截 + 耦合场景由专项测试覆盖 |
| M4 "3% 保留全部证据"不实 | **已修** | 撤回该表述；改为实测口径"10% 预算档证据保留 18/20"（README、简历、招呼语三处同步） |
| M5 "各档根因稳定"矛盾 | **已修** | boundary_diag 打印各档根因未命中数；README 限定"工作点及以上档位根因 0/20 未命中，跌破 10% 后根因同样退化（4/20）"；删除硬编码"未损/20-20" |
| M6 拐点 4/5 非 5/5 | **已修** | README 与简历改为"5 次扰动 4 次拐点一致" |
| M7 80→25 为计划重放 | **已修（口径标注）** | metrics 输出明确标注 "query-plan replay (not live loop)"，真实回路 71 次单独成节；README 对照表两行分开 |
| M8 MODULE.md 过时 | **已修** | 重写：删除过时消融表与第一代架构描述，数字统一指向 opssandbox 实验 |
| minor 若干 | **已修** | 结果 JSON 全部改写模块目录（robustness 不再覆盖 sweep_result.json）；`.miniagent_out/` 删除并 gitignore；sweep 耗时描述更正 |

审核的价值：四条 blocker 全部真实，M1（护栏不在回路）是本项目发布前必须解决的设计缺陷而非文案问题——现已用 `guard_e2e` 的"正路零误伤 + 对抗全拦截"补齐证据链。
