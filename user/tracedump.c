// tracedump              drain the scheduler trace and print it as CSV
// tracedump cmd [args]   discard the trace, run cmd, wait, then print
//
// CSV columns: type,pid,cpu,t,len,aux,name
//   type: fork | burst | wake | drop
//   t, len: r_time() units (100 ns on QEMU virt)
//   aux: burst -> preempt|sleep|exit|other; fork -> parent pid;
//        drop -> number of records lost

#include "kernel/types.h"
#include "kernel/trace.h"
#include "user/user.h"

// Room for a full ring plus one TR_DROP record.
static struct tracerec buf[TRACE_N + 1];

// Snapshot the whole trace before printing anything: printing makes this
// process sleep on the console, which itself produces burst records, and
// draining while printing would feed those back into the output.
static void
drain(int print)
{
  int n = 0, got = 0;
  while (n < TRACE_N + 1 &&
         (got = tracedump(buf + n, TRACE_N + 1 - n)) > 0)
    n += got;
  if (got < 0) {
    fprintf(2, "tracedump: syscall failed\n");
    exit(1);
  }
  if (print) {
    for (int i = 0; i < n; i++) {
      struct tracerec *r = &buf[i];
      if (r->type == TR_BURST) {
        char *why = r->aux == TR_PREEMPT ? "preempt"
                  : r->aux == TR_SLEEP   ? "sleep"
                  : r->aux == TR_EXIT    ? "exit"
                                         : "other";
        printf("burst,%d,%d,%lu,%lu,%s,%s\n", r->pid, r->cpu, r->t, r->len,
               why, r->name);
      } else if (r->type == TR_WAKE) {
        printf("wake,%d,%d,%lu,0,,%s\n", r->pid, r->cpu, r->t, r->name);
      } else if (r->type == TR_FORK) {
        printf("fork,%d,,%lu,0,%d,%s\n", r->pid, r->t, r->aux, r->name);
      } else if (r->type == TR_DROP) {
        printf("drop,,,,0,%d,\n", r->aux);
        fprintf(2, "tracedump: %d records dropped\n", r->aux);
      }
    }
  }
}

int
main(int argc, char *argv[])
{
  if (argc > 1) {
    drain(0); // start the experiment from an empty trace
    int pid = fork();
    if (pid < 0) {
      fprintf(2, "tracedump: fork failed\n");
      exit(1);
    }
    if (pid == 0) {
      exec(argv[1], argv + 1);
      fprintf(2, "tracedump: exec %s failed\n", argv[1]);
      exit(1);
    }
    wait(0);
  }
  printf("type,pid,cpu,t,len,aux,name\n");
  drain(1);
  exit(0);
}
