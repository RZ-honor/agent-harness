# -*- coding: utf-8 -*-
"""
LLM 接入层：一个真实客户端 + 一个可编排的假模型。

为什么要假模型
--------------
Agent 的循环逻辑（该调工具吗、工具返回后怎么继续、预算怎么控）必须能在
**不花钱、不联网、结果确定**的条件下测试。否则每次跑测试都要打 API，
既慢又会因模型输出漂移而随机失败。

因此这里把「模型」抽象成一个最小接口 `chat(messages, tools) -> Reply`：
  - LLMClient     真实模型（OpenAI 兼容接口，本项目用 Qwen）
  - ScriptedLLM   按脚本返回预先编排好的回复，用于测试与离线演示

两者接口一致，Agent 循环对它们无感知。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


# ---------------------------------------------------------------------------
# 消息与回复的最小表示
# ---------------------------------------------------------------------------
@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Reply:
    """一次模型回复。content 与 tool_calls 至少有一个非空。"""
    content: str = ''
    tool_calls: list[ToolCall] = field(default_factory=list)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class ChatModel(Protocol):
    def chat(self, messages: list[dict], tools: list[dict]) -> Reply: ...


# ---------------------------------------------------------------------------
# token 估算
# ---------------------------------------------------------------------------
def estimate_tokens(text: str) -> int:
    """
    粗略 token 估算，仅用于展示成本趋势，不作为计费依据。

    中英文混排下用加权：汉字约 1 字 ≈ 0.7 token，ASCII 约 4 字符 ≈ 1 token。
    这个系数是从本项目实际语料的字符/token 比反推的近似值，
    刻度上足够支撑「压缩前后对比」这类相对结论。
    """
    if not text:
        return 0
    cjk = len(re.findall(r'[\u4e00-\u9fff]', text))
    other = len(text) - cjk
    return int(cjk * 0.7 + other / 4.0) + 1


# ---------------------------------------------------------------------------
# 真实客户端
# ---------------------------------------------------------------------------
def _load_env() -> dict:
    env = dict(os.environ)
    for p in (Path.cwd() / '.env', Path(__file__).resolve().parents[1] / '.env'):
        if p.exists():
            for line in p.read_text(encoding='utf-8').splitlines():
                m = re.match(r'^\s*([A-Za-z_][\w]*)\s*=\s*(.*)$', line)
                if m:
                    env.setdefault(m.group(1), m.group(2).strip().strip('"\''))
            break
    return env


class LLMClient:
    """OpenAI 兼容接口封装（Qwen / DeepSeek / 任意兼容端点）。"""

    def __init__(self, model: str | None = None, temperature: float = 0.0,
                 max_retries: int = 2):
        from openai import OpenAI
        env = _load_env()
        key = env.get('DASHSCOPE_API_KEY') or env.get('QWEN_API_KEY') \
            or env.get('OPENAI_API_KEY')
        if not key:
            raise RuntimeError('未找到 API key（DASHSCOPE_API_KEY）')
        base = env.get('QWEN_BASE_URL',
                       'https://dashscope.aliyuncs.com/compatible-mode/v1')
        self.model = model or env.get('QWEN_MODEL', 'qwen3.8-flash')
        self.temperature = temperature
        self.max_retries = max_retries
        self._cli = OpenAI(api_key=key, base_url=base)
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def chat(self, messages: list[dict], tools: list[dict]) -> Reply:
        kwargs: dict[str, Any] = {
            'model': self.model,
            'messages': messages,
            'temperature': self.temperature,
        }
        if tools:
            kwargs['tools'] = tools
            kwargs['tool_choice'] = 'auto'
        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                r = self._cli.chat.completions.create(**kwargs)
                break
            except Exception as e:      # 网络/限流：重试而非直接失败
                last_err = e
                if attempt == self.max_retries:
                    raise RuntimeError(f'LLM 调用失败: {e}') from e
        else:                            # pragma: no cover
            raise RuntimeError(f'LLM 调用失败: {last_err}')

        self.calls += 1
        if getattr(r, 'usage', None):
            self.prompt_tokens += r.usage.prompt_tokens or 0
            self.completion_tokens += r.usage.completion_tokens or 0

        msg = r.choices[0].message
        calls: list[ToolCall] = []
        for tc in (getattr(msg, 'tool_calls', None) or []):
            try:
                args = json.loads(tc.function.arguments or '{}')
            except json.JSONDecodeError:
                # 模型偶尔产出非法 JSON：保留原文让工具层报错，不静默吞掉
                args = {'__raw__': tc.function.arguments}
            calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
        return Reply(content=msg.content or '', tool_calls=calls)


# ---------------------------------------------------------------------------
# 脚本化假模型（测试 / 离线演示）
# ---------------------------------------------------------------------------
class ScriptedLLM:
    """
    按预设脚本逐轮返回。脚本用尽后返回兜底文本。

    用途：把 Agent 的控制流固定下来，使测试可断言「第几轮调了什么工具、
    收到什么观测」。这是把"Agent 行为"变成可回归测试的前提。
    """

    def __init__(self, script: list[Reply], fallback: str = '任务结束。'):
        self.script = list(script)
        self.fallback = fallback
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def chat(self, messages: list[dict], tools: list[dict]) -> Reply:
        self.calls += 1
        if self.script:
            return self.script.pop(0)
        return Reply(content=self.fallback)


def tool_call(name: str, **arguments) -> ToolCall:
    """构造一个工具调用的便捷函数（脚本化测试用）。"""
    return ToolCall(id=f'call_{name}_{abs(hash((name, tuple(sorted(arguments.items())))))}',
                    name=name, arguments=arguments)
