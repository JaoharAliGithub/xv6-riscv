// schedctl                 print the current scheduling policy
// schedctl rr|sjf [quantum_ms]   set the policy and, optionally, the quantum
//
// Prints "sched,<old>,<new>,<quantum_us>" so the setting lands in the
// console log next to the trace it applies to.

#include "kernel/types.h"
#include "kernel/sched.h"
#include "user/user.h"

static char *pname[] = {"rr", "sjf"};

int
main(int argc, char *argv[])
{
  int policy = -1, quantum_us = -1;

  if (argc > 1) {
    if (strcmp(argv[1], "rr") == 0)
      policy = SCHED_RR;
    else if (strcmp(argv[1], "sjf") == 0)
      policy = SCHED_SJF;
    else {
      fprintf(2, "usage: schedctl [rr|sjf [quantum_ms]]\n");
      exit(1);
    }
  }
  if (argc > 2)
    quantum_us = atoi(argv[2]) * 1000;

  int old = setsched(policy, quantum_us);
  if (old < 0) {
    fprintf(2, "schedctl: setsched failed\n");
    exit(1);
  }
  int now = policy < 0 ? old : policy;
  printf("sched,%s,%s,%d\n", pname[old], pname[now], quantum_us);
  exit(0);
}
