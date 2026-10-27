// Scheduler trace ring buffer.
//
// The scheduler and fork() append records; user space drains them with
// the tracedump(buf, max) system call. Recording never prints, so the
// instrumentation barely perturbs the bursts it measures.
//
// When the ring is full, new records are dropped (not overwritten), so a
// drained-then-run experiment keeps its beginning intact. Drops are
// reported to user space as a single TR_DROP record on the next dump.
//
// Lock order: callers may hold p->lock; tracelock is always innermost.

#include "types.h"
#include "param.h"
#include "memlayout.h"
#include "riscv.h"
#include "spinlock.h"
#include "proc.h"
#include "defs.h"
#include "trace.h"

static struct spinlock tracelock;
static struct tracerec ring[TRACE_N];
static uint head;    // index of the oldest record
static uint count;   // number of records in the ring
static uint dropped; // records lost since the last dump

void
traceinit(void)
{
  initlock(&tracelock, "trace");
}

void
trace_record(int type, int pid, int cpu, uint64 t, uint64 len, int aux,
             char *name)
{
  acquire(&tracelock);
  if (count == TRACE_N) {
    dropped++;
    release(&tracelock);
    return;
  }
  struct tracerec *r = &ring[(head + count) % TRACE_N];
  r->t = t;
  r->len = len;
  r->pid = pid;
  r->aux = aux;
  r->type = type;
  r->cpu = cpu;
  safestrcpy(r->name, name ? name : "", sizeof(r->name));
  count++;
  release(&tracelock);
}

// Pop up to n records into dst (kernel memory). Emits a TR_DROP record
// first if anything was dropped. Returns the number popped.
static int
trace_pop(struct tracerec *dst, int n)
{
  int k = 0;

  acquire(&tracelock);
  if (dropped > 0 && k < n) {
    memset(&dst[k], 0, sizeof(dst[k]));
    dst[k].type = TR_DROP;
    dst[k].aux = dropped;
    dst[k].cpu = 0xff;
    dropped = 0;
    k++;
  }
  while (k < n && count > 0) {
    dst[k++] = ring[head];
    head = (head + 1) % TRACE_N;
    count--;
  }
  release(&tracelock);
  return k;
}

// int tracedump(struct tracerec *buf, int max)
// Drains up to max records into the user buffer.
// Returns the number of records copied, or -1 on a bad buffer.
uint64
sys_tracedump(void)
{
  uint64 ubuf;
  int max;
  struct tracerec chunk[16]; // copyout outside the spinlock, in chunks
  int total = 0;

  argaddr(0, &ubuf);
  argint(1, &max);
  if (max < 0)
    return -1;

  while (total < max) {
    int want = max - total;
    if (want > (int)NELEM(chunk))
      want = (int)NELEM(chunk);
    int got = trace_pop(chunk, want);
    if (got == 0)
      break;
    if (either_copyout(1, ubuf + (uint64)total * sizeof(chunk[0]), chunk,
                       got * sizeof(chunk[0])) < 0)
      return -1;
    total += got;
  }
  return total;
}
