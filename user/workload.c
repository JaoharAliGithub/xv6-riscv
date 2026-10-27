// workload: a reproducible mixed workload for scheduler experiments.
//
//   workload [seed] [scale]      seed: rng seed (default 1)
//                                scale: percent of default sizes (default 100)
//
// Run it under the tracer:   tracedump workload 42
//
// The parent calibrates a busy loop against the real-time clock, then
// spawns a shuffled mix of process types with staggered arrivals and
// waits for all of them. Each child execs a link to this binary named
// after its type (w_cpu, w_io, ...), so the type shows up in the trace's
// name column. The parent also prints one "spawn,pid,type,seed" line per
// child to stdout, for joining with the trace by pid.
//
// Same seed + same scale => same process mix, arrival order, burst
// targets and iteration counts. (Actual burst lengths still depend on
// the scheduler, which is the point.)
//
// Process types:
//   w_cpu    one long compute stretch; bursts end by preemption
//   w_io     short compute, then a synchronous file write (blocks on disk)
//   w_int    short compute of variable length, then pause(1)
//   w_pipe   two processes ping-ponging one byte over a pair of pipes
//   w_phase  CPU-bound, then I/O-bound, then CPU-bound again

#include "kernel/types.h"
#include "kernel/fcntl.h"
#include "user/user.h"

// ---------------------------------------------------------------- utils

static uint64 rng;

static uint64
xorshift(void)
{
  rng ^= rng << 13;
  rng ^= rng >> 7;
  rng ^= rng << 17;
  return rng;
}

// uniform in [lo, hi]
static uint64
rnd(uint64 lo, uint64 hi)
{
  return lo + xorshift() % (hi - lo + 1);
}

static uint64
rdtime(void)
{
  uint64 x;
  asm volatile("rdtime %0" : "=r"(x));
  return x;
}

static uint64
atou64(const char *s)
{
  uint64 n = 0;
  while (*s >= '0' && *s <= '9')
    n = n * 10 + (*s++ - '0');
  return n;
}

static void
u64toa(uint64 n, char *buf) // buf needs 21 bytes
{
  char tmp[21];
  int i = 0;
  do {
    tmp[i++] = '0' + n % 10;
    n /= 10;
  } while (n > 0);
  int j = 0;
  while (i > 0)
    buf[j++] = tmp[--i];
  buf[j] = 0;
}

static uint64 scale = 100; // percent

static uint64
sc(uint64 x) // apply scale to a count or duration, at least 1
{
  uint64 y = x * scale / 100;
  return y > 0 ? y : 1;
}

// ------------------------------------------------------------- busy loop

// Busy work is measured in loop iterations, not wall time, so a burst
// that gets preempted still owes the same amount of CPU when it resumes.
static uint64 iters_per_ms;

static void
spin(uint64 iters)
{
  for (volatile uint64 i = 0; i < iters; i++)
    ;
}

static void
spin_us(uint64 us)
{
  spin(us * iters_per_ms / 1000);
}

// Time a fixed loop several times and keep the fastest run, so a timer
// interrupt landing inside one run doesn't skew the estimate.
// rdtime runs at 10 MHz on QEMU virt: 10000 units per ms.
static void
calibrate(void)
{
  uint64 n = 2000000, best = ~0ULL;
  for (int k = 0; k < 7; k++) {
    uint64 t0 = rdtime();
    spin(n);
    uint64 dt = rdtime() - t0;
    if (dt > 0 && dt < best)
      best = dt;
  }
  iters_per_ms = n * 10000 / best;
}

// ------------------------------------------------------- process bodies

static void
do_cpu(uint64 ms)
{
  spin_us(ms * 1000);
}

static void
do_io(int iters, uint64 lo_us, uint64 hi_us)
{
  char name[16] = "wio";
  u64toa(getpid(), name + 3);
  char block[512];
  memset(block, 'x', sizeof(block));

  int fd = open(name, O_CREATE | O_TRUNC | O_WRONLY);
  for (int i = 0; i < iters; i++) {
    spin_us(rnd(lo_us, hi_us));
    write(fd, block, sizeof(block)); // log commit => waits on disk
    if (i % 32 == 31) {              // keep the file small
      close(fd);
      fd = open(name, O_CREATE | O_TRUNC | O_WRONLY);
    }
  }
  close(fd);
  unlink(name);
}

static void
do_int(int iters)
{
  for (int i = 0; i < iters; i++) {
    spin_us(rnd(100, 2000));
    pause(1);
  }
}

static void
do_pipe(int msgs)
{
  int ab[2], ba[2];
  char c = 0;
  pipe(ab);
  pipe(ba);
  int pid = fork();
  if (pid == 0) {
    xorshift(); // diverge the partner's stream from the parent's
    for (int i = 0; i < msgs; i++) {
      read(ab[0], &c, 1);
      spin_us(rnd(50, 300));
      write(ba[1], &c, 1);
    }
    exit(0);
  }
  for (int i = 0; i < msgs; i++) {
    spin_us(rnd(50, 300));
    write(ab[1], &c, 1);
    read(ba[0], &c, 1);
  }
  wait(0);
}

static void
do_phase(void)
{
  do_cpu(sc(rnd(500, 1000)));
  do_io(sc(rnd(20, 40)), 100, 600);
  do_cpu(sc(rnd(500, 1000)));
}

// ---------------------------------------------------------- dispatching

enum { CPU, IO, INT, PIPE, PHASE, NTYPES };
static char *tname[NTYPES] = {"w_cpu", "w_io", "w_int", "w_pipe", "w_phase"};

// The default mix. The parent shuffles it to set the arrival order.
static int mix[] = {CPU, CPU, IO, IO, IO, INT, INT, PIPE, PHASE, PHASE};
#define NMIX (int)(sizeof(mix) / sizeof(mix[0]))

static void
child_main(int type)
{
  switch (type) {
  case CPU:   do_cpu(sc(rnd(1500, 3000)));   break;
  case IO:    do_io(sc(rnd(40, 60)), 200, 800); break;
  case INT:   do_int(sc(rnd(15, 25)));       break;
  case PIPE:  do_pipe(sc(100));              break;
  case PHASE: do_phase();                    break;
  }
}

static char *
basename(char *path)
{
  char *b = path;
  for (char *p = path; *p; p++)
    if (*p == '/')
      b = p + 1;
  return b;
}

int
main(int argc, char *argv[])
{
  // Child mode: argv[0] names the type; argv = {type, seed, iters_per_ms, scale}
  char *me = basename(argv[0]);
  for (int t = 0; t < NTYPES; t++) {
    if (strcmp(me, tname[t]) == 0) {
      if (argc < 4) {
        fprintf(2, "%s: run via workload\n", me);
        exit(1);
      }
      rng = atou64(argv[1]);
      iters_per_ms = atou64(argv[2]);
      scale = atou64(argv[3]);
      child_main(t);
      exit(0);
    }
  }

  // Parent mode.
  uint64 seed = argc > 1 ? atou64(argv[1]) : 1;
  scale = argc > 2 ? atou64(argv[2]) : 100;
  rng = seed ? seed : 1; // xorshift must not start at 0

  calibrate();
  printf("calib,iters_per_ms,%lu\n", iters_per_ms);

  // Links so each child's exec'd name is its type.
  for (int t = 0; t < NTYPES; t++) {
    char path[20] = "/";
    strcpy(path + 1, tname[t]);
    link("/workload", path); // fails harmlessly if it already exists
  }

  // Fisher-Yates shuffle of the arrival order.
  int order[NMIX];
  for (int i = 0; i < NMIX; i++)
    order[i] = mix[i];
  for (int i = NMIX - 1; i > 0; i--) {
    int j = rnd(0, i);
    int tmp = order[i];
    order[i] = order[j];
    order[j] = tmp;
  }

  char sseed[21], sipms[21], sscale[21];
  u64toa(iters_per_ms, sipms);
  u64toa(scale, sscale);

  for (int i = 0; i < NMIX; i++) {
    int t = order[i];
    uint64 cseed = xorshift() | 1; // per-child stream, never 0
    u64toa(cseed, sseed);
    char path[20] = "/";
    strcpy(path + 1, tname[t]);
    char *cargv[] = {tname[t], sseed, sipms, sscale, 0};

    int pid = fork();
    if (pid < 0) {
      fprintf(2, "workload: fork failed\n");
      exit(1);
    }
    if (pid == 0) {
      exec(path, cargv);
      fprintf(2, "workload: exec %s failed\n", path);
      exit(1);
    }
    printf("spawn,%d,%s,%lu\n", pid, tname[t], cseed);

    // Stagger arrivals by 0-20 ms of parent CPU time.
    spin_us(rnd(0, 20000));
  }

  for (int i = 0; i < NMIX; i++)
    wait(0);
  printf("done\n");
  exit(0);
}
