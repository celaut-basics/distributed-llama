#!/usr/bin/env python3
"""The table of BENCHMARK.md from bench/results/raw: the median of the runs of each case.

    summarize.py <raw dir>   (runs are <case>-r<N>.json, with .cpu.jsonl and .ledger.json)

Prints a Markdown table and writes <raw dir>/../summary.json.
"""

import glob
import json
import os
import re
import statistics
import sys


def median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def cpu_in_window(path, start, end):
    """CPU seconds of each instance (id8) between start and end, from the samples."""
    if not os.path.exists(path) or start is None or end is None:
        return {}
    samples = [json.loads(line) for line in open(path) if line.strip()]
    before = [s for s in samples if s["t"] <= start]
    after = [s for s in samples if s["t"] >= end]
    if not before or not after:
        return {}
    a, b = before[-1]["procs"], after[0]["procs"]
    span = after[0]["t"] - before[-1]["t"]
    return {k: round((b[k] - a.get(k, 0)) / span, 2) for k in b}  # cores busy, average


def load_run(path):
    run = json.load(open(path))
    base = path[:-len(".json")]
    meta = run.get("meta", {})
    t = run.get("t", {})
    cpu = cpu_in_window(base + ".cpu.jsonl", t.get("progress"), t.get("done"))
    ledger = {}
    if os.path.exists(base + ".ledger.json"):
        try:
            ledger = json.load(open(base + ".ledger.json"))
        except ValueError:
            pass

    def num(key):
        try:
            return float(meta[key])
        except (KeyError, ValueError):
            return None

    return {
        "pp_tps": num("prompt_tps"), "tg_tps": num("gen_tps"),
        "prompt_tokens": num("prompt_tokens"), "gen_tokens": num("gen_tokens"),
        "ttft_s": run.get("ttft_s"), "server_ttft_s": (num("ttft_ms") or 0) / 1000 or None,
        "cold_start_s": run.get("cold_start_s"), "chat_up_s": run.get("chat_up_s"),
        "load_tx_mb": (num("load_net_tx_bytes") or 0) / 1e6,
        "answer_net_mb": ((num("net_tx_bytes") or 0) + (num("net_rx_bytes") or 0)) / 1e6,
        "cpu_cores": cpu, "cpu_total_cores": round(sum(cpu.values()), 2) if cpu else None,
        "spent_mu": ledger.get("spent_mu"), "mode": meta.get("mode"),
        "workers": meta.get("workers"), "status": run.get("status"), "text": run.get("text"),
    }


def main():
    raw = sys.argv[1]
    cases = {}
    for path in sorted(glob.glob(os.path.join(raw, "*-r[0-9].json"))):
        case = re.sub(r"-r\d$", "", os.path.basename(path)[:-5])
        cases.setdefault(case, []).append(load_run(path))
    summary = {}
    rows = []
    for case, runs in cases.items():
        s = {k: median([r[k] for r in runs]) for k in
             ("pp_tps", "tg_tps", "ttft_s", "server_ttft_s", "cold_start_s", "load_tx_mb",
              "answer_net_mb", "cpu_total_cores", "spent_mu", "prompt_tokens", "gen_tokens")}
        s["runs"] = len(runs)
        s["modes"] = sorted({r["mode"] or "-" for r in runs})
        s["statuses"] = sorted({r["status"] or "-" for r in runs})
        s["same_text"] = len({r["text"] for r in runs}) == 1
        summary[case] = s
        f = lambda v, d=1: "-" if v is None else ("%.*f" % (d, v))
        rows.append("| %s | %d | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            case, len(runs), f(s["pp_tps"]), f(s["tg_tps"]), f(s["ttft_s"], 2),
            f(s["cold_start_s"], 1), f(s["load_tx_mb"], 0), f(s["cpu_total_cores"], 2),
            f(s["spent_mu"] / 1e6 if s["spent_mu"] is not None else None, 2), ",".join(s["modes"])))
    print("| case | runs | pp tok/s | tg tok/s | TTFT s | cold start s | RPC load MB | CPU cores (gen) | MU spent (M) | mode |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    print("\n".join(rows))
    json.dump(summary, open(os.path.join(raw, "..", "summary.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
