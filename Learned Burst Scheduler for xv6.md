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

The measurement setup is complete and Phase 0 (classic SJF with exponential averaging, switchable at run time against round-robin) is done; results are below. Data flows: kernel records each burst → ring buffer → `tracedump` syscall → console → log → `analyze.py`.

| Piece | Where | What it does |
| --- | --- | --- |
| Burst trace | `burst-trace.patch`: `kernel/trace.c`, `kernel/trace.h`, hooks in `scheduler()` and `kfork()` | Times every CPU burst with `r_time()` around `swtch`, records why it ended (preempt, sleep, exit) from the process state, and logs fork times. 4096-record ring; drops new records when full and reports the count. |
| Dump syscall | `tracedump(buf, max)` + `user/tracedump.c` | `tracedump cmd args` clears the trace, runs the command, then prints CSV: `type,pid,cpu,t,len,aux,name`. Snapshots before printing so its own console writes don't feed back in. |
| Workload generator | `workload.patch`: `user/workload.c`, one line in `kernel/start.c` | `workload <seed> [scale]` spawns 10 processes (2 CPU, 3 I/O, 2 interactive, 1 pipe pair, 2 phase-changing) in a seeded, shuffled order. Children run as `w_cpu`, `w_io`, etc. so type shows in the trace. Spin loops are calibrated with user-mode `rdtime`. |
| Analyzer | `analyze.py` (Python stdlib only) | Parses the console log, splits runs, and reports per-dispatch slices, whole CPU bursts (preempted slices merged), ready-queue wait, per-process turnaround split into CPU / blocked / waiting, the offline accuracy of exponential averaging on merged bursts, and a one-line summary for comparing runs. `--csv` writes merged bursts for training. |
| Scheduler policy | `kernel/proc.c`: `pick()`, `burst_update()`, `burst_key()`; `kernel/sched.h` | `scheduler()` is split into selection and dispatch. Selection is round-robin or SJF by `sched_policy`. After every slice `burst_update()` accumulates preempted time and, when the burst completes (sleep or exit), folds it into τ with α = 0.75 (fixed point, 192/256). The SJF key is max(τ, CPU time already used in the current burst) minus (ready-queue wait / aging); ties go in table order after the last dispatched process, so equal keys round-robin. Selection drops every lock before dispatch and re-checks `RUNNABLE` under the lock, so it stays race-free on several harts. |
| Timer | `kernel/trap.c`: `clockintr()`, `timer_arm()` | The 100 ms tick that drives `pause` and `uptime` is kept on its own schedule per hart. The quantum is armed at each dispatch and the timer fires at whichever of the two comes first; the trap handler yields only on quantum expiry. Quantum default 10 ms. |
| Control | `setsched(policy, quantum_us, aging)` syscall, `user/schedctl.c` | `schedctl rr\|sjf [quantum_ms [aging]]` prints `sched,old,new,quantum_us,aging` so the setting lands in the log; `analyze.py` picks it up. Ctrl-P also prints τ, accumulated burst, burst count and ready time per process. |
| Wake trace | `TR_WAKE` in `wakeup()` | One record per SLEEPING → RUNNABLE transition, so ready-queue wait is measured directly. Ring is 32,768 records (1.5 MB) to hold a 1 ms-quantum run. |
| Driver | `qemurun.py` | `python3 qemurun.py out.log 'schedctl sjf 10' 'tracedump workload 42'` boots QEMU on a scratch copy of `fs.img`, types the commands at the shell, saves the console and quits. Each run starts from a clean file system and does not collide with an interactive `make qemu`. |

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

## Phase 0 results (seed 42, all on the new timer code)

Five configurations of the same kernel, same workload, run back to back with `qemurun.py`; logs are in `results/`. "Short jobs" are the seven `w_int`, `w_pipe` and `w_io` processes; "long" are the two `w_cpu`, two `w_phase` and the `workload` parent. Ready wait is per dispatch, over all traced processes.

| Policy | Quantum | Aging | Mean turnaround | p90 | Short-job mean | Long-job mean | Mean ready wait | Makespan | Dispatches |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| RR | 100 ms | – | 7.24 s | 8.28 s | 6.93 s | 7.66 s | 19.2 ms | 8.85 s | 1,644 |
| RR | 10 ms | – | 6.87 s | 7.60 s | 6.55 s | 7.33 s | 12.1 ms | 8.52 s | 2,032 |
| SJF | 10 ms | 1 | 4.96 s | 8.01 s | 2.87 s | 7.88 s | 12.2 ms | 8.89 s | 1,878 |
| SJF, pure | 10 ms | off | 6.39 s | 8.78 s | 4.71 s | 8.75 s | 11.6 ms | 9.41 s | 2,248 |
| SJF | 1 ms | 1 | 3.73 s | 7.36 s | 1.01 s | 7.54 s | 3.0 ms | 9.19 s | 7,713 |

The Phase 0 gate holds: SJF cuts short-job turnaround from 6.5 s to 2.9 s at the same 10 ms quantum, and to 1.0 s at 1 ms, while the long jobs finish within about 5% of when they did under round-robin. Per type, with SJF at 1 ms versus RR at 10 ms: `w_pipe` 0.12 s instead of 6.4–7.1 s, `w_io` 0.7–1.0 s instead of 6.0–7.3 s, `w_int` 2.0–2.2 s instead of 6.4–6.5 s. The `w_int` figure is at its floor: each of its 15–25 iterations ends in `pause(1)`, which waits for the next 100 ms tick, so about 2 s of its turnaround is unavoidable blocked time.

What the runs showed beyond the headline:

- **Round-robin's real cost is an I/O convoy, not just queueing.** Under RR at 100 ms, both `w_int` processes sat blocked for 1.5 s at exit while a `w_cpu` ran 105 ms slices. They were waiting in `begin_op()` for a log commit that a `w_io` process was driving, and that process got the CPU for 0.3 ms once per round-robin cycle: every disk round trip of the commit cost a full cycle. The disk was idle most of the time. SJF, or any short quantum, dispatches the I/O process as soon as the disk interrupt wakes it, which is why makespan does not get worse even though the long jobs are deferred.
- **Pure SJF starves and loses.** With aging off, usertests' `reparent` test fails: a chain of forks whose children start with key 0 outranks `init` forever, orphans fill the process table and `fork` fails. On the workload it also does worse than aged SJF for `w_int` (5.3 s and 6.8 s against 2.7 s). The likely mechanism is priority inversion through the file-system log: a `w_phase` process leaving its CPU phase carries a stale τ of about 0.8 s, waits up to 373 ms to be dispatched while holding a log operation, and everyone waiting in `begin_op()` waits with it. Aging bounds that wait. The kernel keeps aging as a knob (`schedctl sjf 10 0` is pure SJF) so the effect can be shown, but aged SJF is the baseline from here on. With aging all 54 usertests pass under SJF.
- **Quantum matters at least as much as the predictor.** Going from 10 ms to 1 ms under SJF took short-job turnaround from 2.9 s to 1.0 s and mean ready wait from 12 ms to 3 ms, at 7,713 dispatches instead of 1,878 and a makespan 4–8% longer than the 10 ms runs, which is inside the run-to-run noise of QEMU on this host (the two 10 ms RR/SJF runs differ by 4% in makespan with the same amount of work). Phase 1 should be evaluated at 1 ms.
- **Exponential averaging, scored on whole bursts.** With preempted slices merged there is no censoring, and α = 0.75 lands within 2× of the true burst on 71–73% of bursts in every aged run, with a median error of 52–75 µs and a mean of 4–11 ms. The mean is the tail: `w_phase` goes from an 800 ms burst to 250 µs bursts and back, and `w_cpu`'s only long burst is its last. The `max(τ, used)` key makes an underprediction self-correcting while the process runs, so the misses that cost something are the overpredictions right after a phase change, which is the specific thing a learned predictor with more history should fix.

Design decisions worth keeping in the write-up: a burst is the CPU time between two blocking events, so preemption slices it but does not end it (this is what removed the censoring problem both in the kernel and in the training data); a new process has τ = 0 and so runs first once; τ₀ is the first completed burst, matching the offline scorer; and `tau`, `burst_acc`, `nbursts`, `readyat` live in `struct proc` under `p->lock`.

## Next steps

Phase 0 is done. Everything after it plugs a different predictor into the same scheduler rule (`burst_update()` produces τ; `burst_key()` and `pick()` stay fixed).

1. **Phase 0: classic SJF baseline.** Done, see results above. Left open: a second seed and a real program (`usertests`, `grind`) under both policies, to show the gain is not a property of seed 42.
2. **Phase 1: MLP in software.** Export traces with `--csv` across many seeds (the CSV already holds whole bursts, so nothing is censored). Choose features: last k bursts, what ended each burst, what the process slept on (add the channel class to `TR_BURST`), and time since the burst pattern last changed, since phase changes are where exponential averaging loses. Train a small MLP offline in Python and score it against α = 0.75 on held-out seeds, weighting the tail. Quantize it to integers and run it inside the kernel in place of the exponential-averaging line in `burst_update()`. Evaluate at a 1 ms quantum with aging on.
   - Gate: the MLP beats exponential averaging on prediction error, and the better prediction shows up as better scheduling. If not, change features or the decision before touching hardware.
3. **Phase 2: hardware unit.** Implement the quantized MLP in Verilog with fixed weights as a `custom-0` RISC-V instruction (PicoRV32's PCPI interface or VexRiscv). Measure cycles per inference in Verilator against software inference on the same core. Add a functional model to QEMU so xv6 can call the instruction.
4. **Phase 3, stretch: FPGA.** Synthesize the unit and report LUT and DSP usage. Running the full kernel on a soft core needs an MMU-capable core (VexRiscv via LiteX).
5. **Write-up.** Prediction accuracy, scheduling gains, hardware versus software inference cost, area, and a section on the predictor as a side channel.

## Open questions and risks

- **The gain may be modest.** Exponential averaging is already within 2× of the true burst 71% of the time. A learned predictor that only matches it is still a valid, reportable result.
- **Censoring.** Resolved: a burst is the CPU time between two blocking events, so preempted slices are merged both in the kernel's predictor and in `analyze.py`'s training CSV. Nothing in the data is a lower bound any more.
- **Priority inversion through the log.** Any predictor that overpredicts a process holding a file-system operation stalls every `begin_op()` waiter. Aging bounds it; a learned predictor should also be scored on how fast it recovers after a phase change.
- **Workload realism.** The generator's types are synthetic. The model could learn the generator rather than real program behaviour; held-out seeds and at least one real program (for example `usertests`) should be in the evaluation.
- **Emulated time.** QEMU burst lengths shift with host load. Compare schedulers only on the same seed under similar host conditions, and say so in the write-up.
- **Side channel.** Any process can steer the burst features a hardware predictor sees, much as branch predictors enabled Spectre. The write-up should address this directly.
- **Multicore.** All experiments run with `CPUS=1`. `pick()` drops every lock before dispatch and `scheduler()` re-checks `RUNNABLE` under `p->lock`, so selection is race-free on several harts; the global tick is counted on hart 0 only, as before.
