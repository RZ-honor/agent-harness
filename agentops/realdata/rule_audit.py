# -*- coding: utf-8 -*-
"""
规则级真实数据审计：压缩器每条规则在真实 codex 会话 rollout 上的增量证据。

回答的问题：简历/文档里每条压缩规则，是否都能用真实数据证明？
  1. 体量与调用结构（315 次调用、命令类占比、top10% 字符占比）
  2. 域判档（coding vs auto vs ops）
  3. 结构锚点加分（消融：去掉锚点加分）
  4. 行级显著性两轮选行（消融：改单轮总分排序）
  5. 尾部位置加权（消融：去掉位置加权；并给出证据行相对位置分布）
  6. 首行强制保留（消融：不保留首行）
  7. 逐字可回溯 + 显式省略标记（真实输出上逐行校验）
  8. 结论门禁（真实引用放行 / 篡改拦截，复算 365/365 与 349/365）
每个消融变体与完整规则在 50%/20% 预算下对比证据存活与完全可用率。
只输出聚合统计；--save 写 rule_audit_result.json。

用法：
    python -m agentops.realdata.rule_audit <rollout.jsonl> [--save]
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
from agentops.opssandbox.verifier import ConclusionVerifier
from agentops.realdata.rollout_sweep import (
    EVIDENCE_PAT, EXIT_PAT, FILELINE_PAT, SUMMARY_PAT, ERR_STRICT,
    _norm, anchors_of, evidence_lines, extract_outputs,
)

MARKER_RE = re.compile(
    r"^...\[省略 \d+ 行\]...$|^...\[省略 \d+ 字符\]...$|"
    r"^...\[已截断\]...$|^...\[中间省略\]...$")


class VariantCompressor(ObservationCompressor):
    """带消融开关的压缩器：默认开关下行为必须与父类逐字一致。"""

    def __init__(self, limit_chars, window_chars, profile='coding',
                 two_pass=True, pos_weight=True, anchor_bonus=True,
                 first_line=True, vocab=True):
        super().__init__(limit_chars, window_chars, profile=profile)
        self.two_pass = two_pass
        self.pos_weight = pos_weight
        self.anchor_bonus = anchor_bonus
        self.first_line = first_line
        self.vocab = vocab

    def _score_line(self, line, terms, pos_ratio=0.5, profile='ops'):
        s = line.strip()
        if not s:
            return 0.0
        sc = 0.0
        if profile == 'coding':
            low = line.lower()
            if self.vocab:
                if any(k in low for k in self.STRONG_CI):
                    sc += 100.0
                if any(k in low for k in self.WEAK_CI):
                    sc += 3.0
            if self.anchor_bonus and any(p.search(line) for p in self._anchor_res):
                sc += 60.0
            if self.pos_weight:
                if pos_ratio >= 0.80:
                    sc += 30.0
                elif pos_ratio <= 0.05:
                    sc += 20.0
        else:
            if any(k in line for k in self.STRONG):
                sc += 100.0
            if any(k in line for k in self.WEAK):
                sc += 3.0
        hits = sum(1 for t in terms if t in line)
        sc += min(hits, 4) * 2.0
        if '=' in line and len(line) < 200:
            sc += 0.5
        return sc

    def compress(self, text, query, tool=''):
        raw_len = len(text or '')
        if raw_len <= self.limit_chars:
            return text, raw_len
        lines = text.splitlines()
        if not lines:
            return text[:self.limit_chars], raw_len
        if len(lines) <= 2 and all(len(ln) > self.limit_chars for ln in lines):
            return self._compress_single_line(text, raw_len, query, tool)
        terms = self._terms(query, tool)
        profile = self._effective_profile(text)
        denom = max(len(lines) - 1, 1)
        scored = [(self._score_line(ln, terms, i / denom, profile), i)
                  for i, ln in enumerate(lines)]
        keep = set()
        used = 0
        if self.first_line:
            keep = {0}
            used = len(lines[0]) + 1
        if self.two_pass:
            strong = [(sc, i) for sc, i in scored if sc >= 100]
            weak = [(sc, i) for sc, i in scored if 0 < sc < 100]
            order = [sorted(strong, key=lambda x: (-x[0], x[1])),
                     sorted(weak, key=lambda x: (-x[0], x[1]))]
        else:
            order = [sorted([(sc, i) for sc, i in scored if sc > 0],
                            key=lambda x: (-x[0], x[1]))]
        for group in order:
            for sc, i in group:
                if i == 0 and self.first_line:
                    continue
                cost = len(lines[i]) + 1
                if used + cost <= self.limit_chars:
                    keep.add(i)
                    used += cost
        keep_idx = sorted(keep)
        if not keep_idx:
            return text[:self.limit_chars] + '\n...[已截断]', raw_len
        out, prev = [], -2
        for i in keep_idx:
            if i != prev + 1:
                gap = i - prev - 1 if prev >= 0 else i
                if gap > 0:
                    out.append(f'...[省略 {gap} 行]...')
            out.append(lines[i])
            prev = i
        tail_gap = len(lines) - prev - 1
        if tail_gap > 0:
            out.append(f'...[省略 {tail_gap} 行]...')
        return '\n'.join(out), raw_len


def metrics_for(comp_fn, big, ratios=(0.5, 0.2)):
    rows = {}
    for ratio in ratios:
        ev_ok = ev_tot = 0
        a_ok = {"exit": 0, "fileline": 0, "errline": 0, "summary": 0}
        a_tot = {"exit": 0, "fileline": 0, "errline": 0, "summary": 0}
        usable = n_anchor = 0
        for o in big:
            limit = max(200, int(len(o) * ratio))
            c = _norm(comp_fn(o, limit))
            for e in evidence_lines(o):
                ev_tot += 1
                ev_ok += 1 if e in c else 0
            a = anchors_of(o)
            tot_here = ok_here = 0
            for k, vals in a.items():
                for v in vals:
                    a_tot[k] += 1
                    tot_here += 1
                    if v in c:
                        a_ok[k] += 1
                        ok_here += 1
            if tot_here:
                n_anchor += 1
                if ok_here == tot_here:
                    usable += 1
        rows[f"{int(ratio * 100)}%"] = {
            "evidence_survival": round(ev_ok / max(ev_tot, 1), 4),
            "usable_rate": round(usable / max(n_anchor, 1), 4),
            "exit_survival": round(a_ok["exit"] / max(a_tot["exit"], 1), 4),
            "fileline_survival": round(a_ok["fileline"] / max(a_tot["fileline"], 1), 4),
            "errline_survival": round(a_ok["errline"] / max(a_tot["errline"], 1), 4),
            "summary_survival": round(a_ok["summary"] / max(a_tot["summary"], 1), 4),
        }
    return rows


def position_stats(big):
    ev_pos, anchor_pos = [], []
    for o in big:
        lines = o.splitlines()
        if len(lines) < 2:
            continue
        denom = len(lines) - 1
        for i, ln in enumerate(lines):
            st = ln.strip()
            if not st:
                continue
            if EVIDENCE_PAT.search(ln):
                ev_pos.append(i / denom)
            if (EXIT_PAT.search(ln) or FILELINE_PAT.search(ln)
                    or SUMMARY_PAT.search(ln) or ERR_STRICT.search(st)):
                anchor_pos.append(i / denom)
    mean = lambda xs: round(sum(xs) / len(xs), 4) if xs else None
    return {
        "evidence_lines": len(ev_pos),
        "evidence_mean_relpos": mean(ev_pos),
        "evidence_share_tail_ge_0.8": round(
            sum(1 for p in ev_pos if p >= 0.8) / max(len(ev_pos), 1), 4),
        "anchor_lines": len(anchor_pos),
        "anchor_mean_relpos": mean(anchor_pos),
        "anchor_share_tail_ge_0.8": round(
            sum(1 for p in anchor_pos if p >= 0.8) / max(len(anchor_pos), 1), 4),
    }


def traceability(big):
    """逐字可回溯校验：压缩产物的每一行要么是原文完整行，要么是原文连续子串
    （单行超长分支做字符级窗口）；有删行就必须有省略标记。"""
    checked = markers = bad = sliced = 0
    for o in big:
        limit = max(200, int(len(o) * 0.5))
        comp, _ = ObservationCompressor(limit, max(60, limit // 6),
                                        profile='coding').compress(o, "", "")
        orig_lines = set(o.splitlines())
        kept = [l for l in comp.splitlines() if not MARKER_RE.match(l.strip())]
        for l in comp.splitlines():
            s = l.strip()
            if MARKER_RE.match(s):
                markers += 1
                continue
            checked += 1
            if l in orig_lines:
                continue
            if l and l in o:          # 字符级窗口：原文连续子串
                sliced += 1
                continue
            bad += 1
        if len(o.splitlines()) > len(kept) and not any(
                MARKER_RE.match(l.strip()) for l in comp.splitlines()):
            bad += 1  # 有删行却没有省略标记 = 失效模式
    return {"kept_lines_checked": checked, "marker_lines": markers,
            "verbatim_whole_lines": checked - sliced,
            "verbatim_substring_windows": sliced,
            "non_verbatim_or_unmarked": bad}


def gate_on_real(outs):
    real_ok = fake_blocked = n = 0
    for o in outs:
        lines = [l for l in o.splitlines() if len(l.strip()) >= 20]
        if not lines:
            continue
        n += 1
        line = lines[len(lines) // 2].strip()
        cv = ConclusionVerifier([o])
        ans = f"根因服务：x\n故障类型：y\n关键证据：{line}\n排查路径：z"
        real_ok += 1 if cv.gate(ans).ok else 0
        fake = line[:-8] + "（已篡改替换）"
        ans2 = f"根因服务：x\n故障类型：y\n关键证据：{fake}\n排查路径：z"
        fake_blocked += 1 if not cv.gate(ans2).ok else 0
    return {"n": n, "real_pass": real_ok, "tampered_blocked": fake_blocked}


def calls_by_tool(path):
    from collections import Counter
    cnt = Counter()
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                j = json.loads(line)
            except Exception:
                continue
            p = j.get("payload") or {}
            if p.get("type") == "function_call":
                cnt[p.get("name")] += 1
    total = sum(cnt.values())
    return {"total": total, "by_tool": dict(cnt.most_common()),
            "exec_share": round(cnt.get("exec_command", 0) / max(total, 1), 4)}


def main():
    ap = argparse.ArgumentParser(description="压缩规则级真实数据审计（只输出聚合统计）")
    ap.add_argument("rollout")
    ap.add_argument("--min-chars", type=int, default=1000)
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()

    outs = extract_outputs(args.rollout)
    big = [o for o in outs if len(o) >= args.min_chars]
    print(f"输出 {len(outs)} 条，≥{args.min_chars} 字符 {len(big)} 条")

    # 0) 忠实性：默认开关的变体必须与生产压缩器逐字一致
    base = ObservationCompressor(1000, 166, profile='coding')
    var = VariantCompressor(1000, 166, profile='coding')
    same = all(
        base.compress(o, "", "")[0] == var.compress(o, "", "")[0]
        for o in big)
    print("变体忠实性（与生产压缩器逐字一致）:", same)
    assert same

    variants = {
        "full": dict(),
        "one_pass": dict(two_pass=False),
        "no_pos_weight": dict(pos_weight=False),
        "no_anchor_bonus": dict(anchor_bonus=False),
        "no_vocab": dict(vocab=False),
        "no_first_line": dict(first_line=False),
    }
    ablation = {}
    for name, kw in variants.items():
        fn = (lambda o, limit, kw=kw: VariantCompressor(
            limit, max(60, limit // 6), profile='coding', **kw).compress(o, "", "")[0])
        ablation[name] = metrics_for(fn, big)
        r = ablation[name]["50%"]
        print(f"  {name:<16} 50%: ev={r['evidence_survival']:.4f} "
              f"usable={r['usable_rate']:.4f} | 20%: ev="
              f"{ablation[name]['20%']['evidence_survival']:.4f}")

    result = {
        "sample": {"total_outputs": len(outs), "outputs_ge_min": len(big),
                   "min_chars": args.min_chars,
                   "size_total": sum(len(o) for o in outs)},
        "calls": calls_by_tool(args.rollout),
        "position": position_stats(big),
        "traceability": traceability(big),
        "gate": gate_on_real(outs),
        "ablation": ablation,
    }

    # 与已提交产物交叉校验
    rd = json.loads((Path(__file__).resolve().parent /
                     "realdata_result.json").read_text(encoding="utf-8"))
    c50 = [r for r in rd["results"]["coding"] if r["level"] == "50%"][0]
    chk = {
        "evidence_50_matches_realdata":
            abs(ablation["full"]["50%"]["evidence_survival"]
                - c50["evidence_survival"]) < 1e-6,
        "usable_50_matches_realdata":
            abs(ablation["full"]["50%"]["usable_rate"] - c50["usable_rate"]) < 1e-6,
    }
    result["cross_checks"] = chk
    print("交叉校验:", chk)
    assert all(chk.values())

    if args.save:
        outp = Path(__file__).resolve().parent / "rule_audit_result.json"
        from agentops.jsonio import dump as _jd
        _jd(outp, result)
        print(f"已写入 {outp}")


if __name__ == "__main__":
    main()
