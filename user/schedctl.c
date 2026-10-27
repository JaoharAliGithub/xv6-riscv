// schedctl                              print the current policy
// schedctl rr|sjf [quantum_ms [aging]]  set the policy, quantum, aging
//
// aging: the SJF key is reduced by (ready-queue wait / aging);
//        0 = pure SJF (can starve), 1 = default.
//
// Prints "sched,<old>,<new>,<quantum_us>,<aging>" so the setting lands
// in the console log next to the trace it applies to.

#include "kernel/types.h"
#include "kernel/sched.h"
#include "user/user.h"

static char *pname[] = {"rr", "sjf"};

int
main(int argc, char *argv[])
{
  int policy = -1, quantum_us = -1, aging = -1;

  if (argc > 1) {
    if (strcmp(argv[1], "rr") == 0)
      policy = SCHED_RR;
    else if (strcmp(argv[1], "sjf") == 0)
      policy = SCHED_SJF;
    else {
      fprintf(2, "usage: schedctl [rr|sjf [quantum_ms [aging]]]\n");
      exit(1);
    }
  }
  if (argc > 2)
    quantum_us = atoi(argv[2]) * 1000;
  if (argc > 3)
    aging = atoi(argv[3]);

  int old = setsched(policy, quantum_us, aging);
  if (old < 0) {
    fprintf(2, "schedctl: setsched failed\n");
    exit(1);
  }
  int now = policy < 0 ? old : policy;
  printf("sched,%s,%s,%d,%d\n", pname[old], pname[now], quantum_us, aging);
  exit(0);
}
