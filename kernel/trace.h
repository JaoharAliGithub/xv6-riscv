// Scheduler trace records, shared between the kernel and user space.
// Timestamps are r_time() units: QEMU virt's timebase is 10 MHz,
// so one unit is 100 ns.

#define TRACE_N 4096 // ring capacity (records)

// record types
#define TR_FORK  1 // process created; aux = parent pid
#define TR_BURST 2 // CPU burst ended; aux = reason below
#define TR_DROP  3 // records were dropped (buffer full); aux = count

// burst end reasons (TR_BURST aux), from p->state after swtch returns
#define TR_PREEMPT 1 // timer interrupt -> yield(), still RUNNABLE
#define TR_SLEEP   2 // blocked in sleep() (I/O, pipe, wait, pause)
#define TR_EXIT    3 // called exit(), now ZOMBIE
#define TR_OTHER   4 // anything else (should not happen)

struct tracerec {
  uint64 t;      // TR_BURST: burst start; TR_FORK: creation time
  uint64 len;    // TR_BURST: burst length; otherwise 0
  int pid;
  int aux;
  uchar type;
  uchar cpu;     // hart id, 0xff if unknown
  uchar pad[6];
  char name[16]; // process name at time of record
};
