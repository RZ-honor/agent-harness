# -*- coding: utf-8 -*-
"""
真实工具调用数据验证：在 codex CLI 会话记录（rollout JSONL）上复测压缩策略。

回答两个问题
----------
1. 压缩器在真实编码输出上还剩多少证据（对比运维词表档 / 编码词表档 / 头尾截断）；
2. 压缩后的观测**还能不能被下一阶段模型正常使用**——用四类"下一跳必备锚点"
   （退出码、file:line 定位、错误行、测试汇总计数）是否幸存来判定。

脱敏设计
--------
只输出聚合统计：保留率、存活率、条数、字节数。原文内容、路径、命令文本
都不进入输出 JSON，保存前强制断言扫描。

用法
----
    python -m agentops.realdata.rollout_sweep <rollout.jsonl> --compare --save
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

FILELINE_PAT = re.compile(
    r"[\w./\\-]+\.(?:py|ts|tsx|js|jsx|json|md|toml|ya?ml|go|rs|java|c|cc|cpp|h|hpp|sh|ps1|sql):\d+")
EXIT_PAT = re.compile(r"(?i)(?:exited with code|exit code)\s*[:=]?\s*(-?\d+)")
SUMMARY_PAT = re.compile(r"(?i)\b\d+\s+(?:passed|failed|errors?|skipped)\b")

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


def anchors_of(text: str) -> dict:
    """下一阶段模型要用的四类锚点。"""
    a = {"exit": [], "fileline": [], "errline": [], "summary": []}
    m = EXIT_PAT.search(text)
    if m:
        a["exit"].append(_norm(m.group(0)))
    seen = set()
    for m in FILELINE_PAT.finditer(text):
        fl = _norm(m.group(0))
        if fl not in seen:
            seen.add(fl)
            a["fileline"].append(fl)
        if len(a["fileline"]) >= 3:
            break
    for line in text.splitlines():
        if line.strip() and EVIDENCE_PAT.search(line):
            a["errline"].append(_norm(line)[:100])
            break
    for m in SUMMARY_PAT.finditer(text):
        v = _norm(m.group(0))
        if v not in a["summary"]:
            a["summary"].append(v)
    return a


def naive_head(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n...[截断]..."


def naive_headtail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = max(1, (limit - 20) // 2)
    return text[:half] + "\n...[截断]...\n" + text[-half:]


def make_compressor(limit: int, profile: str):
    window = max(60, limit // 6)
    if profile in ("ops", "coding", "auto"):
        return lambda t: ObservationCompressor(limit, window, profile=profile).compress(t, "", "")[0]
    if profile == "head":
        return lambda t: naive_head(t, limit)
    if profile == "headtail":
        return lambda t: naive_headtail(t, limit)
    raise ValueError(profile)


def run(path: str, min_chars: int, profiles: list[str]) -> dict:
    outs = extract_outputs(path)
    big = [o for o in outs if len(o) >= min_chars]
    stats = {
        "total_outputs": len(outs), "outputs_ge_min": len(big),
        "min_chars": min_chars,
        "size_total": sum(len(o) for o in outs),
        "size_max": max((len(o) for o in outs), default=0),
        "size_p50": sorted((len(o) for o in outs))[len(outs) // 2] if outs else 0,
        "results": {},
    }
    for prof in profiles:
        rows = []
        for ratio, name in LEVELS:
            kept = raw = 0
            ev_ok = ev_tot = 0
            anchor_ok = {"exit": 0, "fileline": 0, "errline": 0, "summary": 0}
            anchor_tot = {"exit": 0, "fileline": 0, "errline": 0, "summary": 0}
            usable = 0
            n_with_anchor = 0
            for o in big:
                limit = max(200, int(len(o) * ratio))
                comp = make_compressor(limit, prof)(o)
                kept += len(comp)
                raw += len(o)
                c = _norm(comp)
                for e in evidence_lines(o):
                    ev_tot += 1
                    ev_ok += 1 if e in c else 0
                a = anchors_of(o)
                total_here = 0
                ok_here = 0
                for k, vals in a.items():
                    for v in vals:
                        anchor_tot[k] += 1
                        total_here += 1
                        if v in c:
                            anchor_ok[k] += 1
                            ok_here += 1
                if total_here:
                    n_with_anchor += 1
                    if ok_here == total_here:
                        usable += 1
            rows.append({
                "level": name, "ratio": ratio,
                "actual_obs_ratio": kept / max(raw, 1),
                "token_saving": 1 - kept / max(raw, 1),
                "evidence_survival": ev_ok / max(ev_tot, 1),
                "exit_survival": anchor_ok["exit"] / max(anchor_tot["exit"], 1),
                "fileline_survival": anchor_ok["fileline"] / max(anchor_tot["fileline"], 1),
                "errline_survival": anchor_ok["errline"] / max(anchor_tot["errline"], 1),
                "summary_survival": anchor_ok["summary"] / max(anchor_tot["summary"], 1),
                "usable_rate": usable / max(n_with_anchor, 1),
                "outputs_with_anchors": n_with_anchor,
            })
        stats["results"][prof] = rows
    return stats


def sanitize_check(stats: dict) -> None:
    s = json.dumps(stats, ensure_ascii=False)
    assert not re.search(r"[A-Za-z]:[\\/]", s), "输出含盘符路径"
    assert "Users" not in s and "sessions" not in s, "输出含用户目录痕迹"
    assert not re.search(r"(?i)(api[_-]?key|token[\"']?\s*[:=]\s*[A-Za-z0-9]{8,})", s), "输出含疑似密钥"


def render(stats: dict) -> None:
    print("=" * 92)
    print(f"真实会话输出 {stats['total_outputs']} 条（≥{stats['min_chars']} 字符 {stats['outputs_ge_min']} 条），"
          f"总字符 {stats['size_total']}，单条最大 {stats['size_max']}")
    for prof, rows in stats["results"].items():
        print("-" * 92)
        print(f"[{prof}]")
        print(f'{"保留档":<10}{"观测比":>8}{"省token":>9}{"证据存活":>9}'
              f'{"退出码":>8}{"文件行号":>9}{"错误行":>8}{"测试汇总":>9}{"完全可用":>9}')
        for r in rows:
            print(f'{r["level"]:<10}{r["actual_obs_ratio"]:>8.1%}{r["token_saving"]:>9.1%}'
                  f'{r["evidence_survival"]:>9.1%}{r["exit_survival"]:>8.1%}'
                  f'{r["fileline_survival"]:>9.1%}{r["errline_survival"]:>8.1%}'
                  f'{r["summary_survival"]:>9.1%}{r["usable_rate"]:>9.1%}')


def main():
    ap = argparse.ArgumentParser(description="真实工具输出压缩验证（只输出聚合统计）")
    ap.add_argument("rollout")
    ap.add_argument("--min-chars", type=int, default=1000)
    ap.add_argument("--profile", default="ops", help="ops / coding / auto / head / headtail")
    ap.add_argument("--compare", action="store_true", help="对比 ops/coding/auto/head/headtail")
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()

    profiles = ["ops", "coding", "auto", "head", "headtail"] if args.compare else [args.profile]
    st = run(args.rollout, args.min_chars, profiles)
    sanitize_check(st)
    render(st)
    if args.save:
        out = Path(__file__).resolve().parent / "realdata_result.json"
        from agentops.jsonio import dump as _jd
        _jd(out, st)
        print(f"\n已写入 {out}（仅聚合统计，无原文）")


if __name__ == "__main__":
    main()