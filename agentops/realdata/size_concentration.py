# -*- coding: utf-8 -*-
"""真实会话工具输出的体量集中度（为简历里"命令类占 88%、一成输出占 62% 字符"背书）。

用法：
    python -m agentops.realdata.size_concentration <rollout.jsonl> [--save]

只输出聚合统计，不回显任何原文。
"""
from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agentops.realdata.rollout_sweep import extract_outputs  # noqa: E402


def concentration(outs: list[str]) -> dict:
    sizes = sorted((len(o) for o in outs), reverse=True)
    n = len(sizes)
    tot = sum(sizes) or 1
    med = statistics.median(sizes)
    out = {
        "n_outputs": n,
        "total_chars": tot,
        "max_chars": sizes[0],
        "median_chars": med,
        "mean_chars": round(tot / n, 1) if n else 0.0,
        "top_share": {},
    }
    for frac in (0.05, 0.10, 0.20):
        k = max(1, int(round(n * frac)))
        out["top_share"]["%d%%" % int(frac * 100)] = {
            "count": k,
            "char_share": round(sum(sizes[:k]) / tot, 4),
        }
    return out


def main():
    ap = argparse.ArgumentParser(description="工具输出体量集中度（仅聚合统计）")
    ap.add_argument("rollout")
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()

    st = concentration(extract_outputs(args.rollout))
    print("输出 %d 条，共 %d 字符；最长 %d，中位 %d，均值 %.0f" % (
        st["n_outputs"], st["total_chars"], st["max_chars"],
        st["median_chars"], st["mean_chars"]))
    print()
    print(f'{"最长占比":<10}{"条数":>8}{"字符占比":>12}')
    for key, v in st["top_share"].items():
        print(f'{key:<12}{v["count"]:>8}{v["char_share"]:>11.1%}')
    if args.save:
        out = Path(__file__).resolve().parent / "size_concentration_result.json"
        from agentops.jsonio import dump as _jd
        _jd(out, st)
        print("\n已写入 %s（仅聚合统计，无原文）" % out.name)


if __name__ == "__main__":
    main()
