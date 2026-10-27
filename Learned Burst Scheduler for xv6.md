# Learned Burst Scheduler for xv6

Oct 26, 2026 · @jaohar

## Goal

Make xv6's scheduler run short jobs first by predicting each process's next CPU burst, then move that predictor into hardware as a tiny neural network behind a custom RISC-V instruction.

The claim the project should be able to defend: a small learned predictor, in N LUTs and M cycles per call, predicts bursts better than classic exponential averaging, and that better prediction improves turnaround and tail latency. Where it does not help is part of the result.

The work lives on the `burst-sched` branch of an `mit-pdos/xv6-riscv` clone, run in QEMU on macOS with `make qemu CPUS=1`.

## Background

xv6 schedules by blind round-robin: `scheduler()` in `kernel/proc.c` sweeps the 64-slot process table and runs the next `RUNNABLE` process it finds. A process gives up the CPU when it blocks (disk, pipe, `pause`), when the 100 ms timer preempts it, or when it exits. The scheduler knows nothing about how long anything will run.

Shortest Job First (SJF) runs the ready process with the shortest upcoming burst. It is provably optimal for average waiting time: three jobs of 10, 1 and 1 ms average 11 ms to finish when the long one goes first, 5 ms when it goes last. SRTF is the preemptive variant.

The catch is that the next burst is unknown, so SJF is only as good as its prediction. The textbook predictor is exponential averaging, τ(n+1) = α·t(n) + (1 − α)·τ(n). This project replaces that predictor with a small MLP, keeping the scheduler rule fixed so the comparison is fair.

## Built so far

The measurement setup is complete; the scheduler itself is still stock round-robin. Data flows: kernel records each burst → ring buffer → `tracedump` syscall → console → `run.log` → `analyze.py`.

| Piece | Where | What it does |
| --- | --- | --- |
| Burst trace | `burst-trace.patch`: `kernel/trace.c`, `kernel/trace.h`, hooks in `scheduler()` and `kfork()` | Times every CPU burst with `r_time()` around `swtch`, records why it ended (preempt, sleep, exit) from the process state, and logs fork times. 4096-record ring; drops new records when full and reports the count. |
| Dump syscall | `tracedump(buf, max)` + `user/tracedump.c` | `tracedump cmd args` clears the trace, runs the command, then prints CSV: `type,pid,cpu,t,len,aux,name`. Snapshots before printing so its own console writes don't feed back in. |
| Workload generator | `workload.patch`: `user/workload.c`, one line in `kernel/start.c` | `workload <seed> [scale]` spawns 10 processes (2 CPU, 3 I/O, 2 interactive, 1 pipe pair, 2 phase-changing) in a seeded, shuffled order. Children run as `w_cpu`, `w_io`, etc. so type shows in the trace. Spin loops are calibrated with user-mode `rdtime`. |
| Analyzer | `analyze.py` (Python stdlib only) | Parses the console log, splits runs, and reports burst stats by type, per-process turnaround, and the offline accuracy of exponential averaging. `--csv` writes clean bursts for training. |

Units: trace timestamps are 100 ns ticks of QEMU's 10 MHz timebase; on macOS the effective resolution is 1 µs.

## First results (seed 42, round-robin)

Round-robin makes short jobs wait almost as long as long ones: a `w_int` process with about 35 ms of total CPU took 7.3 s to finish, a `w_cpu` with 2.5 s took 7.3 s. That gap is what SJF should close. The run produced 1,709 bursts with no drops.

| Type | Bursts | Median | p90 | How bursts end |
| --- | --- | --- | --- | --- |
| w\_cpu | 109 | 111 µs | 100 ms | preempted at 100 ms, then exit storm |
| w\_io | 667 | 209 µs | 1.3 ms | sleep on disk |
| w\_pipe | 283 | 265 µs | 431 µs | sleep on pipe |
| w\_int | 140 | 8 µs | 1.6 ms | sleep in pause |
| w\_phase | 448 | 223 µs | 1.7 ms | 100 ms preempts, then short sleeps |

Mean turnaround was 7.6 s and makespan 8.6 s across 12 processes.

Baseline predictor, exponential averaging scored offline on uncensored bursts:

| α | Mean error | Median error | Within 2× |
| --- | --- | --- | --- |
| 0.25 | 1,730 µs | 121 µs | 54.4% |
| 0.50 | 1,113 µs | 68 µs | 65.1% |
| 0.75 | 870 µs | 52 µs | 71.2% |

It is good at the median and bad in the tail: those large misses are the target for the MLP.

Observations that shape the next steps:

- **Preempted bursts are censored.** xv6's timer fires every 100 ms from the last interrupt, not from dispatch, so a `preempt` record is only a lower bound on the true burst. Training on them as exact values teaches underprediction.
- **Exit storms.** Exiting processes call `begin_op()`, which sleeps while the file-system log commits; `wakeup(&log)` wakes every waiter, so an exiting process produces dozens of \~3 µs sleep–wake bursts under I/O load. A process's bursts depend on kernel contention, not only its own code.
- **Off-CPU time is not split.** The trace has no wakeup events, so time blocked and time waiting in the ready queue are lumped together.

## Next steps

The immediate milestone is SJF with exponential averaging in `scheduler()`, compared against round-robin on the same seeded workload. Everything after it plugs a different predictor into the same scheduler rule.

1. **Phase 0: classic SJF baseline.** Add burst-prediction fields to `struct proc` (predicted τ, last burst, timestamps). Split `scheduler()` into a `predict(p)` function and a selection rule: scan for the lowest τ, then re-acquire the chosen process and confirm it is still `RUNNABLE` before `swtch`. Run `tracedump workload 42` under round-robin and SJF and compare with `analyze.py`.
   - Gate: SJF lowers mean turnaround for short jobs versus round-robin.
   - Likely needed: re-arm the timer at dispatch and drop the quantum toward 1 ms, so preemption is under the scheduler's control (`kernel/trap.c:179`, `kernel/start.c`).
   - Worth adding: a `TR_WAKE` record in `wakeup()`, so ready-queue wait can be measured directly.
2. **Phase 1: MLP in software.** Export traces with `--csv` across many seeds. Choose features (last k bursts, what ended each burst, what the process slept on), handle censored preempt bursts, train a small MLP offline in Python, and score it against exponential averaging on held-out seeds. Quantize it to integers and run it inside the kernel as `predict(p)`.
   - Gate: the MLP beats exponential averaging on prediction error, and the better prediction shows up as better scheduling. If not, change features or the decision before touching hardware.
3. **Phase 2: hardware unit.** Implement the quantized MLP in Verilog with fixed weights as a `custom-0` RISC-V instruction (PicoRV32's PCPI interface or VexRiscv). Measure cycles per inference in Verilator against software inference on the same core. Add a functional model to QEMU so xv6 can call the instruction.
4. **Phase 3, stretch: FPGA.** Synthesize the unit and report LUT and DSP usage. Running the full kernel on a soft core needs an MMU-capable core (VexRiscv via LiteX).
5. **Write-up.** Prediction accuracy, scheduling gains, hardware versus software inference cost, area, and a section on the predictor as a side channel.

## Open questions and risks

- **The gain may be modest.** Exponential averaging is already within 2× of the true burst 71% of the time. A learned predictor that only matches it is still a valid, reportable result.
- **Censoring.** How to treat preempted bursts in training: drop them, use them as lower bounds, or remove the problem by re-arming the timer at dispatch.
- **Workload realism.** The generator's types are synthetic. The model could learn the generator rather than real program behaviour; held-out seeds and at least one real program (for example `usertests`) should be in the evaluation.
- **Emulated time.** QEMU burst lengths shift with host load. Compare schedulers only on the same seed under similar host conditions, and say so in the write-up.
- **Side channel.** Any process can steer the burst features a hardware predictor sees, much as branch predictors enabled Spectre. The write-up should address this directly.
- **Multicore.** All experiments run with `CPUS=1`. The two-pass selection must stay race-free if the work later moves to several harts.
