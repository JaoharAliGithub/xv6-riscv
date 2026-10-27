// Scheduler knobs, shared between the kernel and user space.

// Default quantum, in microseconds. The timer tick that drives pause()
// and uptime() stays at 100 ms regardless of the quantum.
#define SCHED_QUANTUM_US 10000
