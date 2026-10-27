// Scheduler policy knobs, shared between the kernel and user space.
// Set at run time with setsched(policy, quantum_us); see user/schedctl.c.

#define SCHED_RR  0 // round-robin over the process table (stock xv6)
#define SCHED_SJF 1 // shortest predicted CPU burst first

// Default quantum, in microseconds. The timer tick that drives pause()
// and uptime() stays at 100 ms regardless of the quantum.
#define SCHED_QUANTUM_US 10000

// Exponential-averaging weight for the burst predictor, out of 256:
// tau = (ALPHA*t + (256-ALPHA)*tau) / 256. 192 is alpha = 0.75.
#define SCHED_ALPHA 192
