# -*- coding: utf-8 -*-
"""
LLM 在环可用性验证：压缩后的观测，模型还能不能答出下一跳要用的信息？

与 rollout_sweep 的区别
--------------------
rollout_sweep 测"信息是否逐字存活"（结构代理）；本脚本把压缩后的观测直接交给
真实模型，问四个下一跳必答问题，再按**压缩前抽取的锚点**判分——
回答不出 = 压缩把信息毁掉了，这才是端到端的可用性证据。

判分口径
--------
- 严格：锚点字符串在回答中逐字出现；
- 宽松：锚点关键 token 在回答中覆盖 ≥60%（容忍改写）；
- 完全可用：该样本所有存在的锚点在宽松口径下都答对。
只输出聚合统计，原始回答与观测内容一律不入库（保存前断言扫描）。

用法
----
    python -m agentops.realdata.llm_usability <rollout.jsonl> [--sample 30] [--save]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = Path(__file__).resolve().parents[2]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from miniagent.core import ObservationCompressor
from agentops.realdata.rollout_sweep import anchors_of, extract_outputs, _norm

ENV = Path(r"D:\PROJECT\tianci\agenitic_mem\.env")
BUDGETS = [(1.0, "100%(不压缩)"), (0.5, "50%"), (0.2, "20%")]

PROMPT = """下面是一次命令行工具的输出。它可能被压缩过，其中 [省略 N 行] 表示该处有内容被删除。
只根据你能看到的内容回答；看不到就写「未提供」。不要解释，不要补充。

<输出开始>
{obs}
<输出结束>

严格按四行回答：
退出码：<数字或 未提供>
错误信息：<原文片段或 未提供>
文件行号：<文件:行号，多个用逗号分隔；或 未提供>
测试统计：<如 3 failed, 25 passed；或 未提供>"""


def _env() -> tuple[str, str, str]:
    cfg = {}
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    import os
    key = cfg.get("DASHSCOPE_API_KEY") or os.environ.get("DASHSCOPE_API_KEY", "")
    model = cfg.get("QWEN_MODEL", "qwen3.8-flash")
    base = cfg.get("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    return key, model, base


def call_model(obs: str, key: str, model: str, base: str) -> tuple[str, int]:
    import urllib.request
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": PROMPT.format(obs=obs)}],
        "max_tokens": 600, "temperature": 0,
    }).encode()
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=body,
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=240) as r:
        d = json.loads(r.read().decode())
    return d["choices"][0]["message"]["content"], d.get("usage", {}).get("prompt_tokens", 0)


def parse_answer(text: str) -> dict:
    out = {"exit": "", "errline": "", "fileline": "", "summary": ""}
    labels = (("exit", "退出码"), ("errline", "错误信息"),
              ("fileline", "文件行号"), ("summary", "测试统计"))
    for line in text.splitlines():
        s = line.strip().strip('* ')
        for k, label in labels:
            m = re.match(rf"^{label}\s*[：:]\s*(.+)$", s)
            if m:
                out[k] = m.group(1).strip()
    return out


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[A-Za-z0-9_.]{2,}|[\u4e00-\u9fff]{2,}", s or "")}


def grade(anchor: dict, ans: dict) -> dict:
    """逐锚点判定；错误行任一候选命中即算中。"""
    res = {}
    if anchor["exit"]:
        want = re.search(r"-?\d+", " ".join(anchor["exit"]))
        res["exit"] = bool(want and want.group(0) in ans["exit"])
    if anchor["errline"]:
        got = _norm(ans["errline"])
        gt = _tokens(ans["errline"])
        best_strict = any(c in got or c[:60] in got for c in anchor["errline"])
        best_len = False
        for c in anchor["errline"]:
            ta = _tokens(c)
            if ta and len(ta & gt) / len(ta) >= 0.6:
                best_len = True
                break
        res["errline"] = {"strict": best_strict, "lenient": best_strict or best_len}
    if anchor["fileline"]:
        got = _norm(ans["fileline"])
        hit = 0
        for f in anchor["fileline"]:
            name = f.split(":")[0].split("/")[-1]
            line_no = f.rsplit(":", 1)[-1]
            if name in got and line_no in got:
                hit += 1
        res["fileline"] = hit >= max(1, (len(anchor["fileline"]) + 1) // 2)
    if anchor["summary"]:
        got = _norm(ans["summary"])
        gt = _tokens(ans["summary"])
        ok = 0
        for sv in anchor["summary"]:
            if sv in got:
                ok += 1
                continue
            ta = _tokens(sv)
            if ta and len(ta & gt) / len(ta) >= 0.6:
                ok += 1
        res["summary"] = ok >= max(1, (len(anchor["summary"]) + 1) // 2)
    return res


def flatten(r: dict) -> tuple[bool, bool]:
    """(严格全对, 宽松全对)"""
    strict, lenient = True, True
    for k, v in r.items():
        if isinstance(v, dict):
            strict = strict and v["strict"]
            lenient = lenient and (v["strict"] or v["lenient"])
        else:
            strict = strict and v
            lenient = lenient and v
    return strict, lenient


def main():
    ap = argparse.ArgumentParser(description="LLM 在环压缩可用性验证（只输出聚合统计）")
    ap.add_argument("rollout")
    ap.add_argument("--sample", type=int, default=30)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()

    key, model, base = _env()
    assert key, "缺少 DASHSCOPE_API_KEY"
    outs = [o for o in extract_outputs(args.rollout) if 1000 <= len(o) <= 15000]
    outs.sort(key=len)
    if len(outs) > args.sample:      # 按体量均匀取样，覆盖分布
        step = len(outs) / args.sample
        outs = [outs[int(i * step)] for i in range(args.sample)]
    print(f"样本 {len(outs)} 条，体量 {len(outs[0])}~{len(outs[-1])} 字符；模型 {model}")

    tasks = []
    for r, name in BUDGETS:
        for i, o in enumerate(outs):
            tasks.append((name, r, i, o))

    lock = threading.Lock()
    agg = {name: {"n": 0, "strict": 0, "lenient": 0, "tokens": 0,
                  "exit": [0, 0], "errline": [0, 0], "fileline": [0, 0],
                  "summary": [0, 0]} for _, name in BUDGETS}

    def work(t):
        name, ratio, idx, orig = t
        if ratio >= 1.0:
            obs = orig
        else:
            lim = max(200, int(len(orig) * ratio))
            obs = ObservationCompressor(lim, max(60, lim // 6),
                                        profile='coding').compress(orig, "", "")[0]
        anchor = anchors_of(orig)
        has = any(anchor[k] for k in ("exit", "errline", "fileline", "summary"))
        if not has:
            return
        answer, ptok = "", 0
        for attempt in range(2):
            try:
                answer, ptok = call_model(obs, key, model, base)
                break
            except Exception as e:
                if attempt == 1:
                    with lock:
                        print(f"  ERR {name} #{idx}: {str(e)[:70]}")
                    return
        ans = parse_answer(answer)
        g = grade(anchor, ans)
        s_ok, l_ok = flatten(g)
        with lock:
            a = agg[name]
            a["n"] += 1
            a["strict"] += 1 if s_ok else 0
            a["lenient"] += 1 if l_ok else 0
            a["tokens"] += ptok
            for k in ("exit", "errline", "fileline", "summary"):
                if k in g:
                    v = g[k]
                    ok = (v["strict"] or v["lenient"]) if isinstance(v, dict) else v
                    a[k][0] += 1 if ok else 0
                    a[k][1] += 1

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(work, tasks))

    rows = []
    for _, name in BUDGETS:
        a = agg[name]
        n = max(a["n"], 1)
        rows.append({
            "budget": name, "n": a["n"],
            "answer_usable_strict": a["strict"] / n,
            "answer_usable_lenient": a["lenient"] / n,
            "exit_ok": a["exit"][0] / max(a["exit"][1], 1),
            "errline_ok": a["errline"][0] / max(a["errline"][1], 1),
            "fileline_ok": a["fileline"][0] / max(a["fileline"][1], 1),
            "summary_ok": a["summary"][0] / max(a["summary"][1], 1),
            "avg_prompt_tokens": round(a["tokens"] / n, 1) if a["n"] else 0,
        })

    print()
    print(f'{"预算档":<14}{"n":>4}{"严可用":>9}{"宽可用":>9}{"退出码":>9}'
          f'{"错误行":>9}{"文件行号":>10}{"测试汇总":>10}{"prompt tok":>11}')
    for r in rows:
        print(f'{r["budget"]:<14}{r["n"]:>4}{r["answer_usable_strict"]:>9.1%}'
              f'{r["answer_usable_lenient"]:>9.1%}{r["exit_ok"]:>9.1%}'
              f'{r["errline_ok"]:>9.1%}{r["fileline_ok"]:>10.1%}'
              f'{r["summary_ok"]:>10.1%}{r["avg_prompt_tokens"]:>11.0f}')

    if args.save:
        from agentops.jsonio import dump
        payload = {"model": model, "sample": len(outs), "rows": rows}
        s = json.dumps(payload, ensure_ascii=False)
        assert not re.search(r"[A-Za-z]:[\\/]", s) and "Users" not in s
        out = Path(__file__).resolve().parent / "llm_usability_result.json"
        dump(out, payload)
        print(f"\n已写入 {out}（仅聚合统计）")


if __name__ == "__main__":
    main()