#include "types.h"
#include "riscv.h"
#include "defs.h"
#include "param.h"
#include "memlayout.h"
#include "spinlock.h"
#include "proc.h"
#include "sched.h"
#include "vm.h"

uint64
sys_exit(void)
{
  int n;
  argint(0, &n);
  kexit(n);
  return 0; // not reached
}

uint64
sys_getpid(void)
{
  return myproc()->pid;
}

uint64
sys_fork(void)
{
  return kfork();
}

uint64
sys_wait(void)
{
  uint64 p;
  argaddr(0, &p);
  return kwait(p);
}

uint64
sys_sbrk(void)
{
  uint64 addr;
  int t;
  int n;

  argint(0, &n);
  argint(1, &t);
  addr = myproc()->sz;

  if (t == SBRK_EAGER || n < 0) {
    if (growproc(n) < 0) {
      return -1;
    }
  } else {
    // Lazily allocate memory for this process: increase its memory
    // size but don't allocate memory. If the processes uses the
    // memory, vmfault() will allocate it.
    if (addr + n < addr)
      return -1;
    if (addr + n > TRAPFRAME)
      return -1;
    myproc()->sz += n;
  }
  return addr;
}

uint64
sys_pause(void)
{
  int n;
  uint ticks0;

  argint(0, &n);
  if (n < 0)
    n = 0;
  acquire(&tickslock);
  ticks0 = ticks;
  while (ticks - ticks0 < n) {
    if (killed(myproc())) {
      release(&tickslock);
      return -1;
    }
    sleep_prepare(&ticks);
    release(&tickslock);
    sleep();
    acquire(&tickslock);
  }
  release(&tickslock);
  return 0;
}

uint64
sys_kill(void)
{
  int pid;

  argint(0, &pid);
  return kkill(pid);
}

// return how many clock tick interrupts have occurred
// since start.
uint64
sys_uptime(void)
{
  uint xticks;

  acquire(&tickslock);
  xticks = ticks;
  release(&tickslock);
  return xticks;
}

// int setsched(int policy, int quantum_us, int aging)
// Sets the scheduling policy (SCHED_RR / SCHED_SJF, -1 = unchanged), the
// quantum in microseconds (<= 0 = unchanged) and the aging divisor
// (0 = pure SJF, < 0 = unchanged). Returns the previous policy. A
// quantum shorter than the 1 us host timer resolution would just
// thrash, so it is clamped to 100 us.
uint64
sys_setsched(void)
{
  int policy, quantum_us, aging;
  int old = sched_policy;

  argint(0, &policy);
  argint(1, &quantum_us);
  argint(2, &aging);
  if (aging >= 0)
    sched_aging = aging;
  if (policy == SCHED_RR || policy == SCHED_SJF)
    sched_policy = policy;
  else if (policy != -1)
    return -1;
  if (quantum_us > 0)
    sched_quantum = (uint64)(quantum_us < 100 ? 100 : quantum_us) * 10;
  return old;
}
