#!/usr/bin/env python3
"""Summarize an xv6 scheduler trace captured from the QEMU console.

    python3 analyze.py run.log                # last tracedump run in the log
    python3 analyze.py run.log --run 0        # first run (negative = from end)
    python3 analyze.py run.log --csv out.csv  # also write clean burst CSV
    python3 analyze.py run.log --alphas 0.3,0.5,0.8

Reads the raw console capture (`make qemu CPUS=1 | tee run.log`). Anything
that isn't trace CSV (shell prompts, workload output) is ignored, except
`spawn,pid,type,seed` and `calib,...` lines from the workload generator.

All times are reported in microseconds (trace units are 100 ns).

Sections:
  runs        how many tracedump runs the log holds, and which one is used
  by type     burst count / median / p90 / max, and how bursts ended
  processes   per process: arrival, completion, turnaround, CPU time,
              off-CPU time (blocked + waiting in the ready queue; the trace
              has no wakeup events yet, so these two can't be separated)
  baseline    offline error of exponential-averaging burst prediction
"""

import argparse
import csv
import statistics as st
import sys
from collections import Counter, defaultdict

US = 10  # trace units per microsecond (10 MHz timebase)
DEFAULT_EXCLUDE = "tracedump,sh,init"


# ------------------------------------------------------------------ parsing

def parse_runs(path):
    """Split the log into runs. A run starts at each CSV header line."""
    runs = []
    pending_spawn, pending_calib = {}, None
    cur = None
    with open(path, errors="replace") as f:
        for raw in f:
            line = raw.strip().replace("\r", "")
            # a prompt can be glued to the front: "$ spawn,..."
            if line.startswith("$ "):
                line = line[2:]
            if line.startswith("calib,"):
                pending_calib = line.split(",")[-1]
            elif line.startswith("spawn,"):
                _, pid, typ, seed = line.split(",")[:4]
                pending_spawn[int(pid)] = (typ, seed)
            elif line.startswith("type,pid,cpu,t,len,aux,name"):
                cur = {"bursts": [], "forks": [], "drops": 0,
                       "spawn": pending_spawn, "calib": pending_calib}
                runs.append(cur)
                pending_spawn, pending_calib = {}, None
            elif cur is not None and line.startswith(("burst,", "fork,", "drop,")):
                parts = line.split(",")
                if len(parts) < 7:
                    continue  # truncated line
                kind, pid, cpu, t, ln, aux, name = parts[:7]
                try:
                    if kind == "burst":
                        cur["bursts"].append(dict(pid=int(pid), cpu=int(cpu),
                                                  t=int(t), len=int(ln),
                                                  why=aux, name=name))
                    elif kind == "fork":
                        cur["forks"].append(dict(pid=int(pid), t=int(t),
                                                 ppid=int(aux), name=name))
                    else:
                        cur["drops"] += int(aux)
                except ValueError:
                    continue  # garbled by interleaved console output
    return runs


# -------------------------------------------------------------- reporting

def pct(sorted_vals, q):
    if not sorted_vals:
        return float("nan")
    return sorted_vals[min(len(sorted_vals) - 1, int(q * (len(sorted_vals) - 1)))]


def fmt_us(units):
    return f"{units / US:,.0f}"


def by_type(bursts):
    groups = defaultdict(list)
    for b in bursts:
        groups[b["name"]].append(b)
    print(f"\n== bursts by type (us) ==")
    print(f"{'name':10} {'n':>6} {'median':>9} {'p90':>9} {'max':>10}  "
          f"{'preempt':>7} {'sleep':>6} {'exit':>5}")
    for name in sorted(groups):
        L = sorted(b["len"] for b in groups[name])
        c = Counter(b["why"] for b in groups[name])
        print(f"{name:10} {len(L):6} {fmt_us(st.median(L)):>9} "
              f"{fmt_us(pct(L, .9)):>9} {fmt_us(L[-1]):>10}  "
              f"{c['preempt']:7} {c['sleep']:6} {c['exit']:5}")


def processes(bursts, forks, spawn):
    arrive = {f["pid"]: f["t"] for f in forks}
    per = defaultdict(list)
    for b in bursts:
        per[b["pid"]].append(b)

    rows = []
    for pid, bs in per.items():
        bs.sort(key=lambda b: b["t"])
        exits = [b for b in bs if b["why"] == "exit"]
        if pid not in arrive or not exits:
            continue  # started before the trace or didn't finish in it
        done = exits[-1]["t"] + exits[-1]["len"]
        cpu = sum(b["len"] for b in bs)
        ta = done - arrive[pid]
        rows.append(dict(pid=pid, name=bs[-1]["name"], arrive=arrive[pid],
                         done=done, ta=ta, cpu=cpu, off=ta - cpu, n=len(bs)))

    if not rows:
        print("\n(no complete processes in this run)")
        return
    t0 = min(r["arrive"] for r in rows)
    rows.sort(key=lambda r: r["arrive"])

    print(f"\n== processes (us, arrival relative to first) ==")
    print(f"{'pid':>4} {'name':10} {'arrive':>10} {'turnaround':>12} "
          f"{'cpu':>11} {'off-cpu':>11} {'bursts':>6}")
    for r in rows:
        print(f"{r['pid']:4} {r['name']:10} {fmt_us(r['arrive'] - t0):>10} "
              f"{fmt_us(r['ta']):>12} {fmt_us(r['cpu']):>11} "
              f"{fmt_us(r['off']):>11} {r['n']:6}")

    tas = [r["ta"] for r in rows]
    span = max(r["done"] for r in rows) - t0
    print(f"\nmean turnaround {fmt_us(st.mean(tas))} us | "
          f"p90 {fmt_us(pct(sorted(tas), .9))} us | "
          f"makespan {fmt_us(span)} us | {len(rows)} processes")

    if spawn:
        missing = sorted(set(spawn) - {r["pid"] for r in rows})
        if missing:
            print(f"warning: spawned but incomplete in trace: {missing}")


def baseline(bursts, alphas):
    """Offline exponential averaging: tau_{n+1} = a*t_n + (1-a)*tau_n.

    Predictions are made for every burst; error is scored only on bursts
    that ended in sleep or exit. A preempted burst's length is censored by
    the timer (the true burst was at least that long), so scoring on it
    would reward underprediction. tau_0 is the process's first burst.
    """
    per = defaultdict(list)
    for b in sorted(bursts, key=lambda b: b["t"]):
        per[b["pid"]].append(b)

    print(f"\n== baseline: exponential averaging (scored on uncensored bursts) ==")
    print(f"{'alpha':>6} {'MAE us':>10} {'median AE':>10} {'within 2x':>10} {'n':>6}")
    for a in alphas:
        errs, within = [], 0
        for bs in per.values():
            tau = None
            for b in bs:
                if tau is not None and b["why"] != "preempt":
                    e = abs(tau - b["len"])
                    errs.append(e)
                    lo, hi = sorted((max(tau, 1), max(b["len"], 1)))
                    within += hi <= 2 * lo
                tau = b["len"] if tau is None else a * b["len"] + (1 - a) * tau
        if errs:
            print(f"{a:6.2f} {fmt_us(st.mean(errs)):>10} "
                  f"{fmt_us(st.median(errs)):>10} "
                  f"{100 * within / len(errs):9.1f}% {len(errs):6}")


def write_csv(path, bursts, spawn):
    seed = {pid: s for pid, (_, s) in spawn.items()}
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pid", "name", "seed", "t_us", "len_us", "why", "cpu"])
        for b in sorted(bursts, key=lambda b: b["t"]):
            w.writerow([b["pid"], b["name"], seed.get(b["pid"], ""),
                        b["t"] / US, b["len"] / US, b["why"], b["cpu"]])
    print(f"\nwrote {len(bursts)} bursts to {path}")


# ------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("log")
    ap.add_argument("--run", type=int, default=-1,
                    help="which tracedump run to analyze (default: last)")
    ap.add_argument("--exclude", default=DEFAULT_EXCLUDE,
                    help=f"comma-separated process names to drop "
                         f"(default: {DEFAULT_EXCLUDE})")
    ap.add_argument("--alphas", default="0.25,0.5,0.75",
                    help="exponential-averaging weights to evaluate")
    ap.add_argument("--csv", help="write the run's bursts to this CSV")
    args = ap.parse_args()

    runs = parse_runs(args.log)
    if not runs:
        sys.exit("no tracedump output found in log")
    try:
        run = runs[args.run]
    except IndexError:
        sys.exit(f"--run {args.run} out of range: log has {len(runs)} runs")

    idx = args.run % len(runs)
    print(f"{len(runs)} run(s) in log; analyzing run {idx}"
          + (f" (calib {run['calib']} iters/ms)" if run["calib"] else ""))
    if run["drops"]:
        print(f"WARNING: {run['drops']} records were dropped "
              f"(ring full); results are incomplete")

    # Label every burst with its process's final name: bursts before exec
    # carry the parent's name (e.g. "workload" for a w_cpu child), but they
    # belong to the process that exec'd.
    final = {}
    for b in sorted(run["bursts"], key=lambda b: b["t"]):
        final[b["pid"]] = b["name"]
    for b in run["bursts"]:
        b["name"] = final[b["pid"]]

    skip = {s for s in args.exclude.split(",") if s}
    bursts = [b for b in run["bursts"] if b["name"] not in skip]
    forks = run["forks"]
    print(f"{len(bursts)} bursts after excluding {sorted(skip)}")

    by_type(bursts)
    processes(bursts, forks, run["spawn"])
    baseline(bursts, [float(a) for a in args.alphas.split(",")])
    if args.csv:
        write_csv(args.csv, bursts, run["spawn"])


if __name__ == "__main__":
    main()