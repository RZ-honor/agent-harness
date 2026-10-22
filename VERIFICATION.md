# 指标验证记录（VERIFICATION）

本文档固化仓库对外声明的每一个指标的**实测值、复现命令、验证方式与日期**。
凡列不出实测值的声明，不进入 README 与简历。

- 验证环境：Python 3.10.20 / Windows / 无 GPU / 无任何 API Key
- 验证 commit：`9ac4be2`（2026-10-22）
- 验证方式：作者运行 + 独立审核 Agent 复算（见 `AUDIT.md`）双重确认；
  确定性由 `test_determinism.py` 保证（同命令两次运行字节一致，跨 PYTHONHASHSEED 一致）

## 指标总表

| # | 对外声明 | 实测值 | 复现命令 | 状态 |
|---|---|---|---|---|
| 1 | 单次工具观测可达 1.5 万字符 | query_logs 均值 15,350 字符（475,845/31 次） | `python -m agentops.opssandbox.metrics` | ✅ 双验 |
| 2 | 20 例多步排障任务 | 20 案例、8 服务、12 故障类、四层真值 | `python -c "from agentops.opssandbox import scenario; print(len(scenario.CASES))"` | ✅ 双验 |
| 3 | 基线 20/20 通过 | 100% 档 solved 20/20，root 20/20，avg_token 6521 | `python -m agentops.opssandbox.sweep --guard` | ✅ 双验 |
| 4 | 观测保留 33% 时 token 降 63%、19/20 通过 | 20% 档：obs_ratio 32.8%，6521→2402（-63.2%），solved 19/20 | 同上 | ✅ 双验 |
| 5 | 保留率跌破 10% 后跌至 11/20 | 5% 档：obs_ratio 9.6%，solved 11/20，token 906 | 同上 | ✅ 双验 |
| 6 | 工作点以上误差集中在证据保留 | 50%/20%/10% 档根因未命中 0/20，失败均为纯证据丢失（1/1/2 例） | `python -m agentops.opssandbox.boundary_diag` | ✅ 双验 |
| 7 | 跌破 10% 根因同样退化 | 5% 档根因未命中 4/20（整体丢失） | 同上 | ✅ 已声明为悬崖组成 |
| 8 | 5 次扰动 4 次拐点一致 | seed 0/1/3/4 拐点 10%→5%；seed 2 拐点 5%→2% | `python -m agentops.opssandbox.robustness` | ✅ 双验 |
| 9 | 临界区成功率极差 35 个百分点 | 5% 档五种子 [55,70,90,55,55]，极差 35.0pp；其余档 ≤5pp | 同上 | ✅ 双验 |
| 10 | 10% 预算档证据保留 18/20 | sweep 10% 档 evidence 18/20 | `python -m agentops.opssandbox.sweep` | ✅ 双验 |
| 11 | 真实引用 20/20 放行、篡改引用 20/20 拦截 | metrics Verifier 节：20/20 + 20/20，准确率 100% | `python -m agentops.opssandbox.metrics` | ✅ 双验 |
| 12 | 重复查询 80→25（查询计划重放口径） | naive 80 → with_memory 25，节省 68.8%；真实回路 71 次单列 | 同上（输出自带口径标注） | ✅ 双验 |
| 13 | 71 次调用、日志占约四成调用量贡献超 95% 体积 | 71 次；query_logs 31 次 43.7% / 475,845 字符 95.3% | 同上 | ✅ 双验 |
| 14 | 写须确认、危险拒绝、未登记拒绝、3 连败熔断 | guard_e2e Part2 六项对抗全过；20 例正路 0 误伤 | `python -m agentops.opssandbox.guard_e2e` | ✅ 作者验+测试锁 |
| 15 | 护栏开关不改变正路结果 | 100% 档 20/20 vs 20/20；20% 档 19/20 vs 19/20 | 同上 Part1 | ✅ 作者验+测试锁 |
| 16 | 压实-去重耦合防死循环 | `test_compaction_coupling.py` 5 项 | `pytest agentops/opssandbox/test_compaction_coupling.py` | ✅ 测试锁 |
| 17 | 完全确定性（跨进程、跨 hash seed） | `test_determinism.py` 双进程 PYTHONHASHSEED=0/12345 输出一致 | `pytest agentops/opssandbox/test_determinism.py` | ✅ 测试锁 |
| 18 | 83 项测试全绿 | `83 passed in 5.67s` | `pytest -q` | ✅ |

## 撤回与降格的声明（曾经存在、验证不通过、已删除或改写）

| 原声明 | 处置 | 依据 |
|---|---|---|
| "3% 预算下仍保留全部关键证据" | **撤回**，改为 #10 口径 | 独立审核实测 400 字符档仅 8/20 案例证据全存活 |
| "根因判定在各档位均稳定" | **降格**为 #6（工作点以上） | #7 实测 5% 档根因未命中 4/20 |
| "5 次扰动确认拐点稳定" | **改写**为 #8（4/5 一致） | seed 2 拐点后移一档 |
| "成功率无损" | **删除** | 20% 档 19/20 相对基线 20/20 有 1 例差异，README 与简历统一为"通过 19/20" |
| "80 项测试" | 更新为 83 且承诺文案不写死 | 护栏与确定性测试新增 9 项 |

## 与简历的一致性

简历（监管条文审查 Agent / Agent 运行时 Harness 两项目）中出现的全部量化表述
与本表逐条对应；本表之外的数字不允许出现在简历与 README。
简历侧同步记录见 `docs/resume_fulltext.txt`（外部工作区，不随仓库发布）。
