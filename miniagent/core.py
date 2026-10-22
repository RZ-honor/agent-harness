# -*- coding: utf-8 -*-
"""
mini Agent 核心循环。

结构
----
    user
      ↓
    规划（模型决定下一步）
      ↓
    工具调用 → 执行器（含幂等） → 观测
      ↓
    观测压缩（改进点 B）
      ↓
    回填上下文 → 继续循环（受预算控制）
      ↓
    终止 → 最终回答

两个改进点挂在链路的两个不同位置：
  - **改进点 B（观测压缩）**：在"观测 → 回填"之间，控制进入上下文的体积
  - **改进点 C（工具幂等）**：在"工具调用 → 执行"之间，控制重复副作用

设计原则
--------
- 循环是可控的：步数、工具调用数、token 预算三重上限，任一触顶即收敛。
- 每一次工具调用都留 trace，可回放、可统计。
- 模型接口可替换（真实 LLM / 脚本化假模型），循环逻辑不感知。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from miniagent.llm import ChatModel, Reply, ToolCall, estimate_tokens
from miniagent.tools import ToolRegistry, ToolResult


# ---------------------------------------------------------------------------
# 配置与结果
# ---------------------------------------------------------------------------
@dataclass
class AgentConfig:
    max_steps: int = 8                 # 最多几轮模型调用
    max_tool_calls: int = 12           # 最多几次工具调用
    token_budget: int = 60000          # 累计 prompt token 上限
    compress_observations: bool = True # 改进点 B 开关（便于消融）
    obs_limit_chars: int = 1200        # 单条观测进入上下文的字符上限
    obs_window_chars: int = 220        # 压缩时每个命中点保留的窗口
    idempotent_tools: bool = True      # 改进点 C 开关（便于消融）
    # 压缩与重复调用拦截的耦合（参照 HolmesGPT 的 reset 语义）：
    # 压缩后是否重置"重复调用检查"。默认 True——历史已被摘要替换，
    # 旧的重复记录失去参照。但大输出调用会被 pin 住不被重置，见 _pin_threshold。
    reset_dedup_after_compact: bool = True
    # 超过该字符数的观测视为"大输出"，其调用在压缩后仍保留在去重表里。
    # 理由：重复取回大输出会立刻再次撑爆刚压下去的上下文。
    pin_threshold_chars: int = 4000
    # 强制作答：累计工具调用达到该数后，下一轮不再提供工具，
    # 逼迫模型用已有信息作答。默认 0 表示不启用。
    force_answer_after: int = 0
    verbose: bool = False


@dataclass
class Step:
    index: int
    kind: str                          # 'tool' | 'final' | 'stop'
    tool: str = ''
    args: dict = field(default_factory=dict)
    ok: bool = True
    obs_chars: int = 0
    obs_chars_raw: int = 0
    obs_tokens: int = 0
    latency_ms: int = 0
    note: str = ''
    duplicate: bool = False            # 是否因与上一次调用完全相同而被跳过


@dataclass
class AgentResult:
    answer: str
    steps: list[Step]
    stop_reason: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: int = 0
    idempotent_hits: int = 0
    duplicate_side_effects_prevented: int = 0

    @property
    def obs_saved_chars(self) -> int:
        return sum(s.obs_chars_raw - s.obs_chars for s in self.steps
                   if s.obs_chars_raw > s.obs_chars)

    def summary(self) -> str:
        lines = [
            f'终止原因：{self.stop_reason}',
            f'步数：{len(self.steps)}　工具调用：{self.tool_calls}',
            f'prompt {self.prompt_tokens} tok / completion {self.completion_tokens} tok',
        ]
        if self.obs_saved_chars:
            raw = sum(s.obs_chars_raw for s in self.steps)
            kept = sum(s.obs_chars for s in self.steps)
            lines.append(f'观测压缩：{raw:,} → {kept:,} 字符'
                         f'（节省 {1 - kept / max(raw, 1):.1%}）')
        if self.idempotent_hits:
            lines.append(f'幂等命中：{self.idempotent_hits} 次，'
                         f'避免重复副作用 {self.duplicate_side_effects_prevented} 次')
        return '\n'.join(lines)


# ---------------------------------------------------------------------------
# 观测压缩（改进点 B 的载体）
# ---------------------------------------------------------------------------
class ObservationCompressor:
    """
    把工具返回的长文本压到预算内，尽量保留"有信息量"的行。

    为什么从"关键词窗口"改成"行级显著性"
    ------------------------------------
    原实现按关键词在原文里开窗。实测在真实日志上失效：日志里的服务标识是
    包名（c.o.web.OrderController），与拓扑里的服务名（order-svc）不同构，
    关键词命中数为 0 → 窗口列表为空 → 退化成头尾截断 → 证据行在日志中段，
    被系统性丢掉。15,335 字符压到 369 字符时，3 条证据全部丢失。

    因此改为**按行打分再选行**，不再依赖"关键词必须出现"这个强假设：
        1. 强信号行（ERROR/异常类名/明确阈值）优先
        2. 弱信号行（WARN/指标数值）次之
        3. 关键词命中作为加权，而不是必要条件
        4. 保留首行（通常含 service/结构标识），否则压缩后无法判断归属

    仍坚持规则裁剪而非 LLM 摘要：Agent 需要能引用原文，摘要不可复现、
    答错后无法回溯是哪一步被改坏。
    """

    # 强信号：几乎总是异常证据。
    # 注意 'expired' 这类词不能裸匹配——正常日志里的
    # "evict expired entries" 也含 expired，实测会把预算全部吃掉。
    # 因此强信号一律用**带上下文的短语**或明确的异常类名。
    STRONG = ('ERROR', 'FATAL', 'Exception', 'OutOfMemory', 'OOMKilled',
              'No space left', 'certificate has expired', 'connection refused',
              'timed out', 'not available', 'UnknownHost', 'pool exhausted',
              'RejectedExecution', 'handshake failed', 'is full writing',
              'panic:', 'ERROR:')
    # 弱信号：可能是证据，需要关键词佐证
    WEAK = ('WARN', 'WARNING', 'retry', 'slow', 'lag', 'throttl', 'degrad',
            'circuit', 'fallback', 'rtt', 'latency', 'usage', '%')

    def __init__(self, limit_chars: int = 1200, window_chars: int = 220):
        self.limit_chars = limit_chars
        self.window_chars = window_chars

    def _terms(self, query: str, tool: str) -> list[str]:
        import re
        return [t for t in re.findall(
            r'[\u4e00-\u9fff]{2,6}|[A-Za-z][\w\-]{2,}', f'{query} {tool}')
            if len(t) >= 2]

    def _score_line(self, line: str, terms: list[str]) -> float:
        s = line.strip()
        if not s:
            return 0.0
        sc = 0.0
        # 强/弱信号必须拉开档次：实测同权重下，29 条 WARN 噪声会挤掉
        # 中段的 ERROR 证据行。这里让强信号 >= 100，弱信号 <= 3，
        # 保证"含 ERROR 的行"在任何预算下都先于 WARN 行入选。
        if any(k in line for k in self.STRONG):
            sc += 100.0
        if any(k in line for k in self.WEAK):
            sc += 3.0
        # 关键词命中：加权而非必要条件
        hits = sum(1 for t in terms if t in line)
        sc += min(hits, 4) * 2.0
        # 结构行（含 = 或 : 的键值对）通常携带指标，轻微加权
        if ('=' in line and len(line) < 200):
            sc += 0.5
        return sc

    def _compress_single_line(self, text: str, raw_len: int, query: str,
                              tool: str) -> tuple[str, int]:
        """
        单行超长文本的压缩：围绕关键词开字符窗口。

        保留这个分支而不是一律头尾截断，是因为单行文本常见于 JSON/单条
        超长日志——证据可能在任意位置，头尾截断会系统性丢掉中段。
        """
        import re
        terms = self._terms(query, tool)
        spans: list[tuple[int, int]] = []
        for t in terms:
            for m in re.finditer(re.escape(t), text):
                spans.append((max(0, m.start() - self.window_chars),
                              min(raw_len, m.end() + self.window_chars)))
        if not spans:
            head = max(1, self.limit_chars // 3 * 2)
            tail = max(0, self.limit_chars - head)
            body = text[:head]
            body += ('\n...[中间省略]...\n' + text[-tail:]) if tail else '\n...[已截断]...'
            return body, raw_len

        spans.sort()
        merged = [list(spans[0])]
        for s, e in spans[1:]:
            if s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])

        out, used = [], 0
        for s, e in merged:
            seg = text[s:e]
            if used + len(seg) > self.limit_chars:
                remain = self.limit_chars - used
                if remain > 60:
                    out.append(seg[:remain])
                    used += remain
                break
            out.append(seg)
            used += len(seg)
        body = '\n...[中间省略]...\n'.join(out)
        omitted = raw_len - sum(len(x) for x in out)
        if omitted > 0:
            body += f'\n...[省略 {omitted} 字符]...'
        return body, raw_len

    def compress(self, text: str, query: str, tool: str = '') -> tuple[str, int]:
        """返回 (压缩后文本, 原始长度)。"""
        raw_len = len(text or '')
        if raw_len <= self.limit_chars:
            return text, raw_len

        lines = text.splitlines()
        if not lines:
            return text[:self.limit_chars], raw_len

        # 单行超长文本（无换行可分行）无法做行级选择，改用字符级窗口：
        # 有关键词就围绕关键词开窗，没有才头尾截断。
        # 必须显式标注省略，否则模型会误以为观测完整——这是压缩最危险的
        # 失效模式：它不知道自己在看残缺信息。
        if len(lines) <= 2 and all(len(ln) > self.limit_chars for ln in lines):
            return self._compress_single_line(text, raw_len, query, tool)

        terms = self._terms(query, tool)
        scored = [(self._score_line(ln, terms), i) for i, ln in enumerate(lines)]

        # 首行保留（通常是 "# service=..." 这类结构标识），
        # 否则压缩后无法判断这段观测属于哪个服务。
        keep: set[int] = {0}
        used = len(lines[0]) + 1

        # 分两轮选行：先保证所有强信号行入选，再用剩余预算填弱信号行。
        # 一轮按总分排序不够——弱信号行数量多，累计后会先吃掉预算。
        strong = [(sc, i) for sc, i in scored if sc >= 100]
        weak = [(sc, i) for sc, i in scored if 0 < sc < 100]
        for sc, i in sorted(strong, key=lambda x: (-x[0], x[1])):
            if i == 0:
                continue
            cost = len(lines[i]) + 1
            if used + cost <= self.limit_chars:
                keep.add(i)
                used += cost
        for sc, i in sorted(weak, key=lambda x: (-x[0], x[1])):
            if i == 0:
                continue
            cost = len(lines[i]) + 1
            if used + cost <= self.limit_chars:
                keep.add(i)
                used += cost

        keep_idx = sorted(keep)
        if not keep_idx:
            return text[:self.limit_chars] + '\n...[已截断]', raw_len

        # 连续行合并成段，段间用省略标记，避免模型误以为日志连续
        out, prev = [], -2
        omitted_total = 0
        for i in keep_idx:
            if i != prev + 1:
                gap = i - prev - 1 if prev >= 0 else i
                if gap > 0:
                    omitted_total += gap
                    out.append(f'...[省略 {gap} 行]...')
            out.append(lines[i])
            prev = i
        tail_gap = len(lines) - prev - 1
        if tail_gap > 0:
            omitted_total += tail_gap
            out.append(f'...[省略 {tail_gap} 行]...')
        return '\n'.join(out), raw_len


# ---------------------------------------------------------------------------
# 幂等执行器（改进点 C 的载体）
# ---------------------------------------------------------------------------
class ToolExecutor:
    """
    工具执行器。写操作走幂等路径：

        幂等键 = hash(tool_name + canonical(args))

    key 用**规范化参数**生成，所以模型把相同内容用不同 key 顺序表达时也能命中。
    只对 is_write 的工具启用——读工具天然幂等，缓存反而会返回过期数据。
    """

    def __init__(self, idempotent: bool = True):
        self.idempotent = idempotent
        self._cache: dict[str, ToolResult] = {}
        self._inflight: dict[str, Any] = {}
        self.hits = 0
        self.prevented = 0

    @staticmethod
    def idem_key(tool_name: str, args: dict) -> str:
        import hashlib
        import json
        canon = json.dumps(args, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(f'{tool_name}\x00{canon}'.encode()).hexdigest()[:16]

    def execute(self, tool, args: dict) -> ToolResult:
        if not (self.idempotent and tool.is_write):
            return tool.fn(**args)

        key = self.idem_key(tool.name, args)
        if key in self._cache:
            self.hits += 1
            self.prevented += 1
            cached = self._cache[key]
            return ToolResult(True, f'{cached.text}\n（幂等命中：该写入此前已执行，'
                                     f'未重复执行）', dict(cached.data, idempotent_hit=True))
        res = tool.fn(**args)
        if res.ok:
            self._cache[key] = res
        return res


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    '你是一个严谨的任务执行 Agent。你可以调用工具来查证事实与完成操作。\n'
    '规则：\n'
    '1. 需要外部信息时必须调用工具，不要凭记忆编造。\n'
    '2. **每轮只发起 1 个工具调用**。需要多处信息时，先拿到结果再决定下一步，'
    '不要同时发多个同类检索。\n'
    '3. 同一个查询不要重复检索；检索不到就换关键词，最多换 2 次。\n'
    '4. 工具返回不足时，说明"资料不足"即可，不要反复试探。\n'
    '5. 回答要注明依据来自哪个文档/片段。\n'
    '6. 涉及计算时用 calculator，不要口算。\n'
    '7. **信息足够就立即给出最终答案，不要再调用任何工具。**'
)


class MiniAgent:
    def __init__(self, model: ChatModel, tools: ToolRegistry,
                 config: AgentConfig | None = None):
        self.model = model
        self.tools = tools
        self.cfg = config or AgentConfig()
        self.compressor = ObservationCompressor(self.cfg.obs_limit_chars,
                                                self.cfg.obs_window_chars)
        self.executor = ToolExecutor(self.cfg.idempotent_tools)
        # 已执行过的（工具, 参数）组合。用于拦截重复调用——
        # 只靠 prompt 约束不住：实测真实模型会连续 6 轮反复检索同一批关键词，
        # 把 12 次工具预算全部烧完才被迫收尾。必须在代码层拦截。
        self._seen_calls: dict[str, str] = {}
        self._seen_obs: dict[str, bool] = {}
        # 重复调用检查的"是否重置"状态。
        # 参照 HolmesGPT 的 RESET_REPEATED_TOOL_CALL_CHECK_AFTER_COMPACTION：
        # 压缩后历史已被替换，旧的重复记录不再有意义，应重置；
        # 但**输出体积很大**的那些调用必须保留——因为重复取回同一大输出
        # 会立刻把刚压下去的上下文再次撑爆，这正是压缩与去重的耦合点。
        self._reset_after_compact = True
        # 被保留在重复检查里的调用签名（大输出调用）
        self._pinned_calls: dict[str, str] = {}

    # -- 对外入口 ---------------------------------------------------------
    def run(self, task: str) -> AgentResult:
        self._seen_calls.clear()
        self._seen_obs.clear()
        self._pinned_calls.clear()
        self._obs_buffer.clear()
        messages: list[dict] = [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': task},
        ]
        steps: list[Step] = []
        tool_calls_used = 0
        prompt_tokens = 0
        stop_reason = 'max_steps'
        self._dup_streak = 0
        self.compactions = 0

        for i in range(self.cfg.max_steps):
            if prompt_tokens > self.cfg.token_budget:
                stop_reason = 'token_budget'
                break

            # 上下文压实事件：当累计 prompt token 超过预算的一半时触发一次
            # "压实"，并按 HolmesGPT 的语义处理去重表的重置。
            # 放在循环顶部而不是工具返回处，是因为这里能观测到累计体积。
            if (self.cfg.reset_dedup_after_compact
                    and prompt_tokens > self.cfg.token_budget // 2
                    and self.compactions == 0):
                self.compactions += 1
                self._compact_history(messages)

            # 预算提示：把"还剩多少"显式告诉模型，比静默截断更容易收敛
            force_answer = (self.cfg.force_answer_after > 0
                            and tool_calls_used >= self.cfg.force_answer_after)
            if force_answer:
                # 不再提供工具 = 强制收敛。
                # 实测仅靠 prompt 说"信息够了就回答"约束不住：真实模型会
                # 连续 8~9 轮换关键词检索。移除工具描述是最可靠的结构性约束。
                messages.append({
                    'role': 'user',
                    'content': '工具已不可用。请立即仅基于以上已有检索结果给出最终答案，'
                               '并注明依据来自哪个文档；若确实不足，直接说明缺什么。',
                })
                reply = self.model.chat(messages, [])
                content = (reply.content or '').strip()
                if not content:
                    # 模型偶尔返回空 content（尤其在有 tool 历史时）。
                    # 重试一次并明确要求纯文本，仍空则如实报告失败。
                    messages.append({'role': 'user',
                                     'content': '请输出纯文本答案，不要调用任何工具。'})
                    reply = self.model.chat(messages, [])
                    content = (reply.content or '').strip()
                steps.append(Step(index=i, kind='final',
                                  note='达到工具调用阈值，强制作答'))
                return self._finish(content or '（模型未给出文本答案）',
                                    steps, 'forced_answer', prompt_tokens,
                                    tool_calls_used)

            reply: Reply = self.model.chat(messages, self.tools.schemas())
            prompt_tokens = getattr(self.model, 'prompt_tokens', 0) or prompt_tokens

            if not reply.wants_tools:
                steps.append(Step(index=i, kind='final'))
                return self._finish(reply.content or '（空回复）', steps,
                                    'final', prompt_tokens, tool_calls_used)

            if tool_calls_used + len(reply.tool_calls) > self.cfg.max_tool_calls:
                steps.append(Step(index=i, kind='stop', note='工具调用预算耗尽'))
                stop_reason = 'tool_budget'
                messages.append({'role': 'assistant',
                                 'content': '（工具调用次数已达上限，请基于现有信息作答）'})
                # 再给一次机会让模型收尾
                final = self.model.chat(messages, [])
                return self._finish(final.content or '（预算耗尽）', steps,
                                    'tool_budget', prompt_tokens, tool_calls_used)

            # 回填 assistant 的工具请求（OpenAI 协议要求原样带回）
            messages.append({
                'role': 'assistant',
                'content': reply.content or '',
                'tool_calls': [{
                    'id': c.id, 'type': 'function',
                    'function': {'name': c.name,
                                 'arguments': _dumps(c.arguments)},
                } for c in reply.tool_calls],
            })

            for call in reply.tool_calls:
                tool_calls_used += 1
                step = self._run_one(call, task, i)
                steps.append(step)
                messages.append({
                    'role': 'tool',
                    'tool_call_id': call.id,
                    'content': step.note if not step.ok else self._obs_buffer.pop(call.id, ''),
                })
                # 连续重复调用达到阈值 → 强制收敛。
                # 只看"跳过"不够：模型可能换不同参数继续刷，因此还配合
                # tool_calls 预算；这里针对"原样重复"这一最常见退化模式加速止损。
                if getattr(step, 'duplicate', False):
                    self._dup_streak += 1
                else:
                    self._dup_streak = 0
                if self._dup_streak >= 2:
                    stop_reason = 'stalled'
                    messages.append({
                        'role': 'user',
                        'content': '检测到重复调用，已停止检索。请仅基于已有信息给出最终答案。',
                    })
                    final = self.model.chat(messages, [])
                    return self._finish(final.content or '（信息不足，未能完成）',
                                        steps, 'stalled', prompt_tokens,
                                        tool_calls_used)

        return self._finish('（达到最大步数，未能完成）', steps, stop_reason,
                            prompt_tokens, tool_calls_used)

    # -- 内部 -------------------------------------------------------------
    _obs_buffer: dict[str, str] = {}

    def _run_one(self, call: ToolCall, task: str, idx: int) -> Step:
        t0 = time.time()
        tool = self.tools.get(call.name)
        if tool is None:
            msg = f'不存在工具 {call.name}。可用：{", ".join(self.tools.names)}'
            return Step(idx, 'tool', call.name, call.arguments, ok=False,
                        note=msg, latency_ms=0)

        err = tool.validate(call.arguments)
        if err:
            return Step(idx, 'tool', call.name, call.arguments, ok=False, note=err,
                        latency_ms=int((time.time() - t0) * 1000))

        # 重复调用拦截：同一工具 + 同一参数只真正执行一次。
        # 真实模型会反复检索同一关键词，把预算烧光，因此必须在循环里挡掉，
        # 而不是指望 prompt 自觉。
        sig = ToolExecutor.idem_key(call.name, call.arguments)
        if sig in self._seen_calls or sig in self._pinned_calls:
            same = self._pinned_calls.get(sig) or self._seen_calls[sig]
            # 观测不能为空（OpenAI 协议要求 tool 消息有内容），
            # 且要明确告知模型"别再来一次"。
            msg = (f'该调用与之前完全相同，已跳过执行。此前结果：\n{same[:600]}\n'
                   f'（请基于已有信息作答，不要重复检索）')
            # 写工具被拦下来，等价于避免了一次重复副作用，计入同一指标
            if tool.is_write:
                self.executor.prevented += 1
            return Step(idx, 'tool', call.name, call.arguments, ok=True,
                        obs_chars=len(msg), obs_chars_raw=len(msg),
                        obs_tokens=estimate_tokens(msg),
                        note=msg, latency_ms=0, duplicate=True)

        # 幂等键：写工具用规范化参数；读工具不缓存
        res: ToolResult = self.executor.execute(tool, call.arguments)

        text = res.text if res.ok else f'工具执行失败：{res.text}'
        raw_len = len(text)
        if self.cfg.compress_observations:
            # 压缩的定位词 = 任务原文 + **本次工具参数**。
            # 只用任务原文是不够的：任务里多是元话语（"请定位根因服务"），
            # 这些词在日志里几乎不出现，窗口定位会全部落空，压缩退化成
            # 头尾截断——而证据往往在日志中段，于是被系统性丢掉。
            # 把服务名/检索词这类参数带进去，才能把窗口锚在真正相关的位置。
            hint = task + ' ' + ' '.join(str(v) for v in call.arguments.values())
            text, raw_len = self.compressor.compress(text, hint, tool.name)

        # 观测级去重：不同参数可能返回**完全相同的内容**（例如连续读同一个
        # 大文件的不同片段但被截断到同一段）。只按调用参数去重抓不到这种情况，
        # 实测真实模型会因此白烧 2~3 轮预算。因此再按观测内容哈希去重一次。
        import hashlib
        obs_hash = hashlib.sha1(text.encode('utf-8', 'ignore')).hexdigest()[:16]
        if obs_hash in self._seen_obs and res.ok:
            self._seen_obs[obs_hash]
            if tool.is_write:
                self.executor.prevented += 1
            note = ('该观测与之前某次调用的结果完全相同，内容无新增。\n'
                    '（请基于已有信息作答，或更换检索角度）')
            self._obs_buffer[call.id] = note
            return Step(idx, 'tool', call.name, call.arguments, ok=True,
                        obs_chars=len(note), obs_chars_raw=len(note),
                        obs_tokens=estimate_tokens(note),
                        note=note, latency_ms=0, duplicate=True)
        self._seen_obs[obs_hash] = True

        # 记录原始观测（用于重复调用时回填未压缩版本的关键部分）
        self._seen_calls[sig] = text
        # 大输出调用额外 pin 一份：即使压缩后重置去重表，它的签名也要保留。
        # 原因见 AgentConfig.pin_threshold_chars 的注释——重复取回大输出
        # 会立刻把刚压下去的上下文再次撑爆，这是压缩与去重的真实耦合点。
        if raw_len >= self.cfg.pin_threshold_chars:
            self._pinned_calls[sig] = text
        # 用 tool_call_id 关联，多工具并行时不会串
        self._obs_buffer[call.id] = text

        return Step(
            idx, 'tool', call.name, call.arguments, ok=res.ok,
            obs_chars=len(text), obs_chars_raw=raw_len,
            obs_tokens=estimate_tokens(text),
            latency_ms=int((time.time() - t0) * 1000),
        )

    def _compact_history(self, messages: list[dict]) -> None:
        """
        压实历史，并处理去重表的重置语义。

        为什么重置是必要的
        ------------------
        压实后历史被替换，旧观测已不在上下文里，此时若仍拒绝"重复调用"，
        模型实际上失去了那部分信息却又拿不回来——会把任务卡死。
        所以默认放开重试。

        为什么大输出调用不能一起放开
        ---------------------------
        重复取回一个大输出（实测单条可达 1.5 万字符）会立刻把刚压下去的
        上下文再次撑爆，触发第二次压实，形成"压实→重取→再压实"的死循环。
        因此这类调用的签名保留在 _pinned_calls 里，重复请求仍被拒绝。

        这正对应 HolmesGPT 的
        RESET_REPEATED_TOOL_CALL_CHECK_AFTER_COMPACTION 与
        TOOL_MAX_ALLOCATED_CONTEXT_WINDOW_PCT 的耦合设计。
        """
        kept = [m for m in messages
                if m.get('role') == 'system' or m is messages[1]]
        summary = (f'（上下文已压实：此前 {len(messages) - len(kept)} 条消息'
                   f'被折叠。已查询过的内容请勿重复检索。）')
        kept.append({'role': 'user', 'content': summary})
        messages[:] = kept

        # 重置去重表，但保留大输出调用
        pinned = dict(self._pinned_calls)
        self._seen_calls.clear()
        self._seen_obs.clear()
        self._pinned_calls = pinned
        self._dup_streak = 0

    def _finish(self, answer: str, steps: list[Step], reason: str,
                prompt_tokens: int, tool_calls: int) -> AgentResult:
        return AgentResult(
            answer=answer, steps=steps, stop_reason=reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=getattr(self.model, 'completion_tokens', 0),
            tool_calls=tool_calls,
            idempotent_hits=self.executor.hits,
            duplicate_side_effects_prevented=self.executor.prevented,
        )


def _dumps(obj: dict) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)
