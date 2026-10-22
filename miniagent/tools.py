# -*- coding: utf-8 -*-
"""
工具层：注册表 + 内置工具。

设计要点
--------
1. **工具自带元数据**：是否写操作（is_write）决定了要不要走幂等执行路径。
   这个标记不能靠调用方判断，否则迟早漏。
2. **参数校验前置**：进函数前先校验必填项，错误信息回给模型而不是抛栈。
3. **返回结构化 + 可读文本**：文本给模型看，结构给上层统计用。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# 单次工具输出进入上下文的硬上限（超过则触发压缩评估）
DEFAULT_OBS_LIMIT = 1200


@dataclass
class ToolResult:
    ok: bool
    text: str
    data: dict = field(default_factory=dict)

    def render(self) -> str:
        return self.text


@dataclass
class Tool:
    name: str
    description: str
    params: dict[str, str]              # 参数名 -> 说明
    required: list[str]
    fn: Callable[..., ToolResult]
    is_write: bool = False

    def schema(self) -> dict:
        """OpenAI function-calling 格式。"""
        props = {k: {'type': 'string', 'description': v}
                 for k, v in self.params.items()}
        return {
            'type': 'function',
            'function': {
                'name': self.name,
                'description': self.description,
                'parameters': {
                    'type': 'object',
                    'properties': props,
                    'required': self.required,
                },
            },
        }

    def validate(self, args: dict) -> str | None:
        """返回错误信息，None 表示通过。"""
        if '__raw__' in args:
            return f'参数不是合法 JSON：{args["__raw__"][:80]}'
        missing = [k for k in self.required if not str(args.get(k, '')).strip()]
        if missing:
            return f'缺少必填参数：{", ".join(missing)}'
        unknown = set(args) - set(self.params)
        if unknown:
            return f'未知参数：{", ".join(sorted(unknown))}'
        return None


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------
class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f'工具重名：{tool.name}')
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self._tools.values()]

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def __len__(self) -> int:
        return len(self._tools)


# ---------------------------------------------------------------------------
# 内置工具
# ---------------------------------------------------------------------------
def _chunk_markdown(text: str, size: int = 700, overlap: int = 120) -> list[str]:
    """按段落聚合切块，带 overlap，保证命中片段有上下文。"""
    paras = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
    out, buf = [], ''
    for p in paras:
        if len(buf) + len(p) + 1 <= size:
            buf = f'{buf}\n\n{p}' if buf else p
        else:
            if buf:
                out.append(buf)
            buf = p if len(p) <= size else p[:size]
    if buf:
        out.append(buf)
    return out


def make_search_tool(doc_dir: Path) -> Tool:
    """
    文档检索工具：在指定目录的 .md/.txt 里做关键词打分。

    故意返回**多个较长的片段**——这样上下文压缩才有真实的用武之地；
    如果工具只回一两百字，压缩无从谈起，也就演示不出改进点。
    """
    def _search(query: str = '', top_k: str = '3') -> ToolResult:
        k = max(1, min(int(top_k or 3), 6))
        terms = [t for t in re.findall(r'[\u4e00-\u9fff]{2,6}|[A-Za-z][\w\-]{2,}',
                                       query or '') if len(t) >= 2]
        if not terms:
            return ToolResult(False, '检索词为空，请给出更具体的关键词。')
        files = sorted([p for p in doc_dir.rglob('*')
                        if p.suffix.lower() in ('.md', '.txt')])
        if not files:
            return ToolResult(False, f'目录 {doc_dir} 下没有可检索的文档。')

        scored = []
        for p in files:
            try:
                text = p.read_text(encoding='utf-8', errors='replace')
            except Exception:
                continue
            blocks = _chunk_markdown(text)
            for bi, b in enumerate(blocks):
                score = sum(b.count(t) for t in terms)
                if score:
                    scored.append((score, p.name, bi, b))
        if not scored:
            return ToolResult(False, f'未检索到与 {terms} 相关的内容。')
        scored.sort(key=lambda x: -x[0])
        picked = scored[:k]
        body = '\n\n'.join(
            f'[{i + 1}] {name}#段{bi}（命中 {sc} 次）\n{blk}'
            for i, (sc, name, bi, blk) in enumerate(picked))
        return ToolResult(
            True, body,
            {'hits': [{'doc': n, 'block': bi, 'score': s} for s, n, bi, _ in picked],
             'terms': terms,
             'raw_chars': len(body)},
        )
    return Tool(
        name='search_docs',
        description='在本项目的文档目录中按关键词检索，返回若干较长片段。'
                    '用于查证事实、找实现细节。',
        params={'query': '检索关键词，空格分隔', 'top_k': '返回片段数，默认 3'},
        required=['query'],
        fn=_search,
        is_write=False,
    )


def make_read_tool(root: Path) -> Tool:
    def _read(path: str = '', max_chars: str = '4000') -> ToolResult:
        p = (root / path).resolve()
        # 越界保护：不允许读 root 之外的文件
        try:
            p.relative_to(root.resolve())
        except ValueError:
            return ToolResult(False, f'拒绝访问工作目录之外的路径：{path}')
        if not p.exists():
            return ToolResult(False, f'文件不存在：{path}')
        n = max(200, min(int(max_chars or 4000), 20000))
        text = p.read_text(encoding='utf-8', errors='replace')
        truncated = len(text) > n
        return ToolResult(True, text[:n] + ('\n...[已截断]' if truncated else ''),
                          {'chars': len(text), 'truncated': truncated})
    return Tool(
        name='read_file',
        description='读取工作目录内的文本文件。',
        params={'path': '相对工作目录的路径', 'max_chars': '最多读取字符数'},
        required=['path'],
        fn=_read,
    )


def make_write_tool(root: Path) -> Tool:
    """写文件工具：标记 is_write=True，执行时走幂等路径。"""
    def _write(path: str = '', content: str = '') -> ToolResult:
        p = (root / path).resolve()
        try:
            p.relative_to(root.resolve())
        except ValueError:
            return ToolResult(False, f'拒绝写入工作目录之外的路径：{path}')
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding='utf-8')
        return ToolResult(True, f'已写入 {path}（{len(content)} 字符）',
                          {'path': str(p), 'chars': len(content)})
    return Tool(
        name='write_file',
        description='把内容写入工作目录内的文件（覆盖写）。这是写操作，'
                    '失败重试会被幂等机制折叠。',
        params={'path': '相对工作目录的路径', 'content': '要写入的完整文本'},
        required=['path', 'content'],
        fn=_write,
        is_write=True,
    )


def make_calc_tool() -> Tool:
    """只允许数字与四则运算的安全计算器（不 eval 任意代码）。"""
    allowed = re.compile(r'^[\d\s\.\+\-\*/%\(\)]+$')

    def _calc(expr: str = '') -> ToolResult:
        e = (expr or '').strip()
        if not e or not allowed.match(e):
            return ToolResult(False, '只支持数字与 + - * / % ( ) 组成的算式。')
        try:
            val = eval(e, {'__builtins__': {}}, {})   # 已由正则限死字符集
        except ZeroDivisionError:
            return ToolResult(False, '除数为零。')
        except Exception as ex:
            return ToolResult(False, f'算式无法计算：{ex}')
        return ToolResult(True, f'{e} = {val}', {'value': val})
    return Tool(
        name='calculator',
        description='计算四则表达式，避免模型口算出错。',
        params={'expr': '如 (1234+5678)/2'},
        required=['expr'],
        fn=_calc,
    )


def default_registry(workdir: Path, doc_dir: Path | None = None) -> ToolRegistry:
    reg = ToolRegistry()
    reg.add(make_search_tool(doc_dir or workdir / 'docs'))
    reg.add(make_read_tool(workdir))
    reg.add(make_write_tool(workdir))
    reg.add(make_calc_tool())
    return reg
