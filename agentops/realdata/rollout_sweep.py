# -*- coding: utf-8 -*-
"""
真实工具调用数据验证：在 codex CLI 会话记录（rollout JSONL）上复测压缩器。

背景
----
合成沙箱（opssandbox）回答"机制边界在哪"；本脚本回答"真实世界的工具输出
长什么样、压缩器在真实分布上还剩多少证据"。数据源是编码 Agent 的真实会话
记录：exec_command 等工具的全部返回（构建日志、测试输出、文件内容、命令报错）。

脱敏设计（重要）
--------------
脚本只读取、只输出**聚合统计**：保留率、证据行存活率、条数、字节数。
任何原文内容、文件路径、命令文本都不进入输出 JSON。输出前强制扫描断言。

证据行定义
----------
与压缩器的 STRONG/WEAK 词表**解耦**（避免自证）：用通用编码场景标记
（error/failed/exception/traceback/exit code≠0/warning/cannot/invalid…）
定义"后续推理需要看到的行"。

用法
----
    python -m agentops.realdata.rollout_sweep <rollout.jsonl> [--save] [--min-chars 1000]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from miniagent.core import ObservationCompressor

EVIDENCE_PAT = re.compile(
    r"(?i)(error|fail(ed|ure)?|exception|traceback|panic|fatal|denied|"
    r"cannot|could not|not found|missing|invalid|exit(ed)? with code [1-9]|"
    r"warning|timed out|refused|✗)")

LEVELS = [(1.0, "100%(不压缩)"), (0.5, "50%"), (0.2, "20%"),
          (0.1, "10%"), (0.05, "5%"), (0.02, "2%"), (0.01, "1%")]


def extract_outputs(path: str) -> list[str]:
    outs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                j = json.loads(line)
            except Exception:
                continue
            p = j.get("payload") or {}
            if p.get("type") in ("function_call_output", "custom_tool_call_output"):
                body = p.get("output") or p.get("content") or ""
                if isinstance(body, (dict, list)):
                    body = json.dumps(body, ensure_ascii=False)
                body = str(body)
                if body.strip():
                    outs.append(body)
    return outs


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def evidence_lines(text: str) -> list[str]:
    return [_norm(l) for l in text.splitlines()
            if l.strip() and EVIDENCE_PAT.search(l)]


def naive_truncate(text: str, limit: int) -> str:
    """基线：头尾各留一半（多数框架的默认截断方式）。"""
    if len(text) <= limit:
        return text
    half = max(1, (limit - 20) // 2)
    return text[:half] + "\n...[截断]...\n" + text[-half:]


def survival(orig_text: str, compressed: str) -> tuple[int, int]:
    evs = evidence_lines(orig_text)
    if not evs:
        return 0, 0
    c = _norm(compressed)
    kept = sum(1 for e in evs if e in c)
    return kept, len(evs)


def run(path: str, min_chars: int) -> dict:
    outs = extract_outputs(path)
    big = [o for o in outs if len(o) >= min_chars]
    stats = {"total_outputs": len(outs), "outputs_ge_min": len(big),
             "min_chars": min_chars,
             "size_p50": sorted(len(o) for o in outs)[len(outs) // 2] if outs else 0,
             "size_max": max((len(o) for o in outs), default=0),
             "size_total": sum(len(o) for o in outs),
             "levels": []}
    with_ev = [o for o in big if evidence_lines(o)]
    stats["outputs_with_evidence"] = len(with_ev)
    for ratio, name in LEVELS:
        c_keep = c_raw = n_keep = n_surv = n_ev = 0
        worst = 1.0
        for o in big:
            limit = max(200, int(len(o) * ratio))
            comp, _ = ObservationCompressor(limit, max(60, limit // 6)).compress(o, "", "")
            naive = naive_truncate(o, limit)
            c_keep += len(comp); c_raw += len(o)
            n_keep += len(naive)
            k, tot = survival(o, comp)
            nk, ntot = survival(o, naive)
            if tot:
                n_surv += k; n_ev += tot
                rate = k / tot
                worst = min(worst, rate)
            stats["_naive_surv"] = stats.get("_naive_surv", 0)  # placeholder
        row = {"level": name, "ratio": ratio,
               "actual_obs_ratio": c_keep / max(c_raw, 1),
               "evidence_survival": n_surv / max(n_ev, 1),
               "evidence_total": n_ev,
               "worst_output_survival": worst if n_ev else None,
               "token_saving_vs_raw": 1 - c_keep / max(c_raw, 1)}
        # naive 证据存活单独算（同循环代价低，但保持代码简单单独再扫一遍小样本）
        ns = ntot2 = 0
        for o in with_ev[:80]:
            limit = max(200, int(len(o) * ratio))
            k, tot = survival(o, naive_truncate(o, limit))
            ns += k; ntot2 += tot
        row["naive_evidence_survival"] = ns / max(ntot2, 1)
        stats["levels"].append(row)
    stats.pop("_naive_surv", None)
    return stats


def sanitize_check(stats: dict) -> None:
    """输出只允许聚合统计：任何盘符路径、用户目录痕迹、长标识串一律拦截。"""
    s = json.dumps(stats, ensure_ascii=False)
    assert not re.search(r"[A-Za-z]:[\\/]", s), "输出含盘符路径"
    assert "Users" not in s and "sessions" not in s, "输出含用户目录痕迹"
    assert not re.search(r"(?i)(api[_-]?key|token[\"']?\s*[:=]\s*[A-Za-z0-9]{8,})", s), "输出含疑似密钥"


def main():
    ap = argparse.ArgumentParser(description="真实工具输出压缩验证（只输出聚合统计）")
    ap.add_argument("rollout", help="codex rollout JSONL 路径（不进入仓库）")
    ap.add_argument("--min-chars", type=int, default=1000)
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()

    st = run(args.rollout, args.min_chars)
    sanitize_check(st)

    print("=" * 78)
    print(f"真实会话数据：输出 {st['total_outputs']} 条（≥{st['min_chars']} 字符 {st['outputs_ge_min']} 条，"
          f"含证据行 {st['outputs_with_evidence']} 条），"
          f"总字符 {st['size_total']}，单条最大 {st['size_max']}")
    head = (f'{"保留档":<12}{"实际观测比":>10}{"token节省":>10}'
            f'{"证据存活(压缩)":>14}{"证据存活(头尾)":>14}{"最差单条":>10}')
    print(head)
    for r in st["levels"]:
        print(f'{r["level"]:<12}{r["actual_obs_ratio"]:>10.1%}{r["token_saving_vs_raw"]:>10.1%}'
              f'{r["evidence_survival"]:>13.1%}{r["naive_evidence_survival"]:>13.1%}'
              f'{(r["worst_output_survival"] if r["worst_output_survival"] is not None else 1):>9.0%}')
    if args.save:
        out = Path(__file__).resolve().parent / "realdata_result.json"
        out.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入 {out}（仅聚合统计，无原文）")


if __name__ == "__main__":
    main()
