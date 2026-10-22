# miniagent —— 模型无关的 Agent 运行时核心

`agent-harness` 的运行时层。只负责三件事：**有界执行循环、观测压缩、幂等工具执行**。
业务场景（排障沙箱）在 `agentops/opssandbox/`，量化实验与复现入口见根目录 README。

## 组成

| 文件 | 职责 |
|---|---|
| `core.py` | `MiniAgent`：有界 ReAct 循环（max_steps / max_tool_calls / token_budget 三重预算，force_answer_after 防呆）；`ObservationCompressor`：行级显著性两轮选行（强信号行优先、弱信号行补预算、超预算行做字符窗口截断并显式标注省略）；`ToolExecutor`：写工具幂等（sha1 规范键 + 并发单飞占位），读工具不缓存 |
| `llm.py` | `ChatModel` 协议（`reply(messages, schemas) -> Reply`）；`ScriptedLLM` 确定性脚本代理（测试与受控实验用）；`LLMClient` OpenAI 兼容客户端（可选接真实模型）；`tool_call` id 使用 crc32 稳定哈希，保证跨 PYTHONHASHSEED 确定性 |
| `tools.py` | `Tool` / `ToolRegistry`（Schema 校验、重名拒绝）；内置 `search_docs`（段落聚合切块+overlap）、`calculator`、读写文件工具（越界路径拒绝） |
| `cli.py` | 交互式入口：`python -m miniagent.cli --demo`（离线确定性演示）、`--task/--ablate`；接真实模型需自备 OpenAI 兼容端点，默认不需要 |

## 与压实相关的两个耦合约束（踩坑固化）

1. **pin 机制**：超过 `pin_threshold_chars` 的大输出调用在上下文压实后仍保留在重复检查集合内，防止"压实→模型看不见→重取→再压实"死循环（对应开关在 HolmesGPT 中名为 `RESET_REPEATED_TOOL_CALL_CHECK_AFTER_COMPACTION`）。
2. **省略显式化**：压缩产生的每一处跳行都插入 `...[省略 N 行]...` 标记，避免模型把不连续日志误读为连续。

以上两条分别由 `tests/test_miniagent.py` 与 `agentops/opssandbox/test_compaction_coupling.py` 回归保护。

## 量化结果在哪里看

本模块不单独放数字表——所有对外数字统一由 `agentops/opssandbox/` 的五个实验脚本产生（见根 README 对照表），避免文档数字与代码漂移。
