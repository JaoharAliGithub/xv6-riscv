#!/usr/bin/env python3
"""Summarize an xv6 scheduler trace captured from the QEMU console.

    python3 analyze.py run.log                # last tracedump run in the log
    python3 analyze.py run.log --run 0        # first run (negative = from end)
    python3 analyze.py run.log --csv out.csv  # also write clean burst CSV
    python3 analyze.py run.log --alphas 0.3,0.5,0.8

Reads the raw console capture (`make qemu CPUS=1 | tee run.log`, or
`qemurun.py`). Anything that isn't trace CSV (shell prompts, workload
output) is ignored, except `spawn,pid,type,seed`, `calib,...` and
`sched,...` lines from the workload generator and schedctl.

All times are reported in microseconds (trace units are 100 ns).

Sections:
  runs        how many tracedump runs the log holds, and which one is used
  slices      per dispatch: count / median / p90 / max, and how they ended
  bursts      per CPU burst (slices between two blocking events, with the
              preempted pieces merged): count / median / p90 / max
  ready wait  time from becoming RUNNABLE (fork, wake, preempt) to running
  processes   per process: arrival, turnaround, CPU time, time blocked,
              time waiting in the ready queue
  baseline    offline error of exponential-averaging burst prediction,
              scored on merged bursts (so nothing is censored)
  summary     one line of the headline numbers, for comparing runs
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
    pending_spawn, pending_calib, pending_sched = {}, None, None
    cur = None
    with open(path, errors="replace") as f:
        for raw in f:
            line = raw.strip().replace("\r", "")
            # a prompt can be glued to the front: "$ spawn,..."
            if line.startswith("$ "):
                line = line[2:]
            if line.startswith("sched,"):
                pending_sched = line.split(",")[2:]
            elif line.startswith("calib,"):
                pending_calib = line.split(",")[-1]
            elif line.startswith("spawn,"):
                _, pid, typ, seed = line.split(",")[:4]
                pending_spawn[int(pid)] = (typ, seed)
            elif line.startswith("type,pid,cpu,t,len,aux,name"):
                cur = {"bursts": [], "forks": [], "wakes": [], "drops": 0,
                       "spawn": pending_spawn, "calib": pending_calib,
                       "sched": pending_sched}
                runs.append(cur)
                pending_spawn, pending_calib = {}, None
            elif cur is not None and line.startswith(("burst,", "fork,",
                                                      "wake,", "drop,")):
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
                    elif kind == "wake":
                        cur["wakes"].append(dict(pid=int(pid), t=int(t),
                                                 name=name))
                    else:
                        cur["drops"] += int(aux)
                except ValueError:
                    continue  # garbled by interleaved console output
    return runs


# ---------------------------------------------------------------- derived

def merge_bursts(bursts):
    """Coalesce each process's preempted slices into whole CPU bursts.

    A burst runs from the first slice after a blocking event to the slice
    that ends in sleep or exit; its length is the sum of the slices' CPU
    time. A trailing run of preempted slices with no completing slice is
    dropped (the burst was still running when the trace ended).
    """
    per = defaultdict(list)
    for b in sorted(bursts, key=lambda b: b["t"]):
        per[b["pid"]].append(b)
    merged = []
    for pid, bs in per.items():
        acc, start, n = 0, None, 0
        for b in bs:
            if start is None:
                start = b["t"]
            acc += b["len"]
            n += 1
            if b["why"] != "preempt":
                merged.append(dict(pid=pid, name=b["name"], t=start,
                                   len=acc, why=b["why"], slices=n,
                                   end=b["t"] + b["len"]))
                acc, start, n = 0, None, 0
    merged.sort(key=lambda b: b["t"])
    return merged


def ready_waits(bursts, forks, wakes):
    """Attach to each slice the time its process spent RUNNABLE before it.

    A process becomes RUNNABLE at fork, at a wake record, or at the end of
    a preempted slice. Slices whose ready time is unknown get wait=None.
    """
    events = defaultdict(list)  # pid -> [(t, kind)]
    for f in forks:
        events[f["pid"]].append((f["t"], "ready"))
    for w in wakes:
        events[w["pid"]].append((w["t"], "ready"))
    for b in bursts:
        events[b["pid"]].append((b["t"], "run", b))
        if b["why"] == "preempt":
            events[b["pid"]].append((b["t"] + b["len"], "ready"))
    for pid, ev in events.items():
        ev.sort(key=lambda e: (e[0], e[1] == "run"))  # ready before run
        ready = None
        for e in ev:
            if e[1] == "ready":
                ready = e[0] if ready is None else ready
            else:
                e[2]["wait"] = None if ready is None else e[0] - ready
                ready = None


# -------------------------------------------------------------- reporting

def pct(sorted_vals, q):
    if not sorted_vals:
        return float("nan")
    return sorted_vals[min(len(sorted_vals) - 1, int(q * (len(sorted_vals) - 1)))]


def fmt_us(units):
    return f"{units / US:,.0f}"


def by_type(bursts, title):
    groups = defaultdict(list)
    for b in bursts:
        groups[b["name"]].append(b)
    print(f"\n== {title} by type (us) ==")
    print(f"{'name':10} {'n':>6} {'median':>9} {'p90':>9} {'max':>10}  "
          f"{'preempt':>7} {'sleep':>6} {'exit':>5}")
    for name in sorted(groups):
        L = sorted(b["len"] for b in groups[name])
        c = Counter(b["why"] for b in groups[name])
        print(f"{name:10} {len(L):6} {fmt_us(st.median(L)):>9} "
              f"{fmt_us(pct(L, .9)):>9} {fmt_us(L[-1]):>10}  "
              f"{c['preempt']:7} {c['sleep']:6} {c['exit']:5}")


def ready_wait_by_type(bursts):
    groups = defaultdict(list)
    for b in bursts:
        if b.get("wait") is not None:
            groups[b["name"]].append(b["wait"])
    if not groups:
        print("\n(no wake records: ready-queue wait not available)")
        return
    print(f"\n== ready-queue wait per dispatch, by type (us) ==")
    print(f"{'name':10} {'n':>6} {'mean':>9} {'median':>9} {'p90':>9} {'max':>10}")
    for name in sorted(groups):
        W = sorted(groups[name])
        print(f"{name:10} {len(W):6} {fmt_us(st.mean(W)):>9} "
              f"{fmt_us(st.median(W)):>9} {fmt_us(pct(W, .9)):>9} "
              f"{fmt_us(W[-1]):>10}")


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
        waits = [b["wait"] for b in bs if b.get("wait") is not None]
        wait = sum(waits) if len(waits) == len(bs) else None
        rows.append(dict(pid=pid, name=bs[-1]["name"], arrive=arrive[pid],
                         done=done, ta=ta, cpu=cpu, wait=wait, n=len(bs)))

    if not rows:
        print("\n(no complete processes in this run)")
        return []
    t0 = min(r["arrive"] for r in rows)
    rows.sort(key=lambda r: r["arrive"])

    print(f"\n== processes (us, arrival relative to first) ==")
    print(f"{'pid':>4} {'name':10} {'arrive':>10} {'turnaround':>12} "
          f"{'cpu':>11} {'blocked':>11} {'waiting':>11} {'slices':>6}")
    for r in rows:
        w = r["wait"]
        blocked = fmt_us(r["ta"] - r["cpu"] - w) if w is not None else "?"
        print(f"{r['pid']:4} {r['name']:10} {fmt_us(r['arrive'] - t0):>10} "
              f"{fmt_us(r['ta']):>12} {fmt_us(r['cpu']):>11} "
              f"{blocked:>11} {fmt_us(w) if w is not None else '?':>11} "
              f"{r['n']:6}")

    tas = [r["ta"] for r in rows]
    span = max(r["done"] for r in rows) - t0
    print(f"\nmean turnaround {fmt_us(st.mean(tas))} us | "
          f"p90 {fmt_us(pct(sorted(tas), .9))} us | "
          f"makespan {fmt_us(span)} us | {len(rows)} processes")

    if spawn:
        missing = sorted(set(spawn) - {r["pid"] for r in rows})
        if missing:
            print(f"warning: spawned but incomplete in trace: {missing}")
    return rows


def baseline(merged, alphas):
    """Offline exponential averaging: tau_{n+1} = a*t_n + (1-a)*tau_n.

    Scored on merged bursts, i.e. whole CPU demands between blocking
    events, so preemption no longer censors anything. tau_0 is the
    process's first burst, which is not scored. This is the same update
    the kernel's SJF policy runs online (with alpha = SCHED_ALPHA/256).
    """
    per = defaultdict(list)
    for b in merged:
        per[b["pid"]].append(b)

    print(f"\n== baseline: exponential averaging (scored on merged bursts) ==")
    print(f"{'alpha':>6} {'MAE us':>10} {'median AE':>10} {'within 2x':>10} {'n':>6}")
    for a in alphas:
        errs, within = [], 0
        for bs in per.values():
            tau = None
            for b in bs:
                if tau is not None:
                    e = abs(tau - b["len"])
                    errs.append(e)
                    lo, hi = sorted((max(tau, 1), max(b["len"], 1)))
                    within += hi <= 2 * lo
                tau = b["len"] if tau is None else a * b["len"] + (1 - a) * tau
        if errs:
            print(f"{a:6.2f} {fmt_us(st.mean(errs)):>10} "
                  f"{fmt_us(st.median(errs)):>10} "
                  f"{100 * within / len(errs):9.1f}% {len(errs):6}")


def write_csv(path, merged, spawn):
    """One row per merged CPU burst, in time order: training data."""
    seed = {pid: s for pid, (_, s) in spawn.items()}
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pid", "name", "seed", "t_us", "len_us", "why", "slices"])
        for b in merged:
            w.writerow([b["pid"], b["name"], seed.get(b["pid"], ""),
                        b["t"] / US, b["len"] / US, b["why"], b["slices"]])
    print(f"\nwrote {len(merged)} bursts to {path}")


SHORT = ("w_int", "w_pipe", "w_io")  # types whose jobs SJF should favour


def summary(run, rows, bursts):
    """One line of headline numbers, for comparing runs side by side."""
    sched = run["sched"] or ["?", "?", "?"]
    policy = sched[0]
    quantum = f"{int(sched[1]) // 1000}ms" if sched[1] not in ("?", "-1") else "?"
    aging = sched[2] if len(sched) > 2 else "?"
    tas = [r["ta"] for r in rows]
    short = [r["ta"] for r in rows if r["name"] in SHORT]
    long_ = [r["ta"] for r in rows if r["name"] not in SHORT]
    waits = [b["wait"] for b in bursts if b.get("wait") is not None]
    span = max(r["done"] for r in rows) - min(r["arrive"] for r in rows)
    print(f"\n== summary ==")
    print(f"policy={policy} quantum={quantum} aging={aging} | "
          f"turnaround mean {fmt_us(st.mean(tas))} p90 {fmt_us(pct(sorted(tas), .9))} | "
          f"short-job mean {fmt_us(st.mean(short)) if short else '?'} | "
          f"long-job mean {fmt_us(st.mean(long_)) if long_ else '?'} | "
          f"ready wait mean {fmt_us(st.mean(waits)) if waits else '?'} "
          f"p90 {fmt_us(pct(sorted(waits), .9)) if waits else '?'} | "
          f"makespan {fmt_us(span)} | {len(bursts)} slices")


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

    for w in run["wakes"]:
        w["name"] = final.get(w["pid"], w["name"])

    skip = {s for s in args.exclude.split(",") if s}
    bursts = [b for b in run["bursts"] if b["name"] not in skip]
    forks = run["forks"]
    wakes = [w for w in run["wakes"] if w["name"] not in skip]
    print(f"{len(bursts)} slices after excluding {sorted(skip)}")
    if run["sched"]:
        print(f"scheduler: policy={run['sched'][0]} "
              f"quantum_us={run['sched'][1]} aging={run['sched'][2:]}")

    ready_waits(bursts, forks, wakes)
    merged = merge_bursts(bursts)

    by_type(bursts, "slices (one per dispatch)")
    by_type(merged, "CPU bursts (preempted slices merged)")
    ready_wait_by_type(bursts)
    rows = processes(bursts, forks, run["spawn"])
    baseline(merged, [float(a) for a in args.alphas.split(",")])
    if rows:
        summary(run, rows, bursts)
    if args.csv:
        write_csv(args.csv, merged, run["spawn"])


if __name__ == "__main__":
    main()