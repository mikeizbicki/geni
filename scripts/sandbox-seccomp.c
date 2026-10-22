/* sandbox-seccomp.c -- the syscall filter sandbox4.sh installs.
 *
 * bwrap(1) cannot express a syscall policy: --seccomp takes a filter that is
 * already compiled.  This program is therefore the policy.  Run it with no
 * arguments and it writes one struct sock_filter array to stdout, which is
 * the exact shape bwrap reads back from the descriptor it is handed.  It
 * does nothing else, takes no options, and keeps no state.
 *
 * The filter allows by default.  A syscall that no rule names falls through
 * to SECCOMP_RET_ALLOW, so everything a normal program needs keeps working
 * and only what is listed below is refused.  A denial returns EPERM instead
 * of killing the process, so a program that needs one of these calls fails
 * in a way it can report, and a wall is never mistaken for a crash.  The
 * single exception is the architecture check, for the reason in build().
 *
 * The order of the rules is the part that is easy to get wrong.
 *
 *   1. The architecture, first.  Every rule after it compares a syscall
 *      number, and a syscall number means a different thing under a
 *      different ABI.  A payload from another ABI would be judged by
 *      numbers it does not use, so it is killed rather than filtered.
 *      x32 shares x86_64's architecture word, so this check cannot see it:
 *      its mark is the bit its numbers carry, tested once one is loaded.
 *
 *   2. io_uring, next.  io_uring performs file and socket work in kernel
 *      context.  It does not call open(2) or socket(2), so a rule that
 *      watches those calls is walked around by it completely.  A filter
 *      that means to constrain a program by syscall has to close this hole
 *      before anything under it is worth having.
 *
 *   3. Everything else, which is kernel surface a payload has no use for
 *      and an exploit has plenty.
 *
 * Nothing here restricts which files can be opened.  That is the mount
 * map's job, in sandbox4.sh, and a file that was never mounted cannot be
 * named in the first place.
 */

#define _GNU_SOURCE
#include <errno.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/syscall.h> /* __NR_*; without it every rule below vanishes */
#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/seccomp.h>

/* The ABI this filter knows.  Anything else is killed, so the list has to
 * be complete for the architectures this is meant to run on; a port to a
 * new one is a compile error rather than a filter that quietly allows. */
#if defined(__x86_64__)
# define ARCH AUDIT_ARCH_X86_64
#elif defined(__aarch64__)
# define ARCH AUDIT_ARCH_AARCH64
#elif defined(__riscv) && __riscv_xlen == 64
# define ARCH AUDIT_ARCH_RISCV64
#elif defined(__powerpc64__) && defined(__LITTLE_ENDIAN__)
# define ARCH AUDIT_ARCH_PPC64LE
#elif defined(__s390x__)
# define ARCH AUDIT_ARCH_S390X
#else
# error "sandbox-seccomp.c: add this architecture's AUDIT_ARCH_* constant"
#endif

#ifndef SECCOMP_RET_KILL_PROCESS
# define SECCOMP_RET_KILL_PROCESS 0x80000000U
#endif

/* EPERM in the encoding seccomp wants: the action in the high bits and the
 * error number in the low ones. */
#define DENIED (SECCOMP_RET_ERRNO | (EPERM & SECCOMP_RET_DATA))

/* The bit an x32 syscall number carries.  x32 reports x86_64's
 * AUDIT_ARCH_*, so the architecture check above cannot separate the two,
 * and the bit in the number is the only mark that can.  On every other
 * architecture no syscall number sets it, so the rule built from it is
 * dead there rather than wrong. */
#define X32_BIT 0x40000000U

/* Room for the rules below twice over, and a fixed array so that writing a
 * few hundred bytes never allocates. */
#define MAX_FILTER 256

static struct sock_filter filter[MAX_FILTER];
static size_t n;

/* Refuse one syscall.  The accumulator already holds the syscall number,
 * loaded once in build(); a comparison that does not match steps over the
 * return that follows it. */
static void
deny (int nr)
{
  if (n + 2 > MAX_FILTER)
    {
      fputs ("sandbox-seccomp: filter overflow\n", stderr);
      exit (1);
    }
  filter[n++] = (struct sock_filter) BPF_JUMP (BPF_JMP | BPF_JEQ | BPF_K,
                                               nr, 0, 1);
  filter[n++] = (struct sock_filter) BPF_STMT (BPF_RET | BPF_K, DENIED);
}

/* Every syscall named below is written out and never guarded.  A guard that
 * is false skips its rule, and a filter missing a rule is still a filter: it
 * installs, it runs, and nothing says what it stopped checking.  A number
 * the headers do not have is therefore a compile error, which means the
 * headers are older than the kernel the filter is for.  iopl and ioperm are
 * the exception rather than the rule: no architecture but x86 and ia64
 * defines them, and there the call really does not exist. */
static const int surface[] = {
  /* io_uring: the hole is the subsystem, so all three go together, and
   * nothing below this point is worth having while a caller can reach it. */
  __NR_io_uring_setup,
  __NR_io_uring_enter,
  __NR_io_uring_register,

  /* Faulting and counting memory the caller was never given. */
  __NR_userfaultfd,
  __NR_perf_event_open,

  /* The kernel's own program interface, and loading code into it. */
  __NR_bpf,
  __NR_kexec_load,
  __NR_kexec_file_load,
  __NR_init_module,
  __NR_finit_module,
  __NR_delete_module,

  /* Reaching into another process.  pidfd_getfd takes an open file
   * descriptor from one, which is a way around every mount rule there is. */
  __NR_ptrace,
  __NR_process_vm_readv,
  __NR_process_vm_writev,
  __NR_pidfd_getfd,
  __NR_open_by_handle_at,

  /* The kernel keyring, which is somewhere to keep a secret the mount map
   * was meant to put out of reach. */
  __NR_add_key,
  __NR_request_key,
  __NR_keyctl,

  /* setns is how a process would *leave* the namespace it was put in, so
   * it stays refused.  Mounting and namespace creation are how a nested
   * jail is made, and they are allowed: this filter loads after the outer
   * bwrap has finished, so the payload already has a private user and
   * mount namespace, and a mount or an unshare here reaches nothing the
   * outer jail did not already contain -- it builds a smaller room inside
   * the one it is in.  A test that itself calls sandbox, run under
   * `sandbox pytest`, is the case that needs both. */
  __NR_setns,

  /* Machine-wide state, which a payload has no business changing. */
  __NR_reboot,
  __NR_swapon,
  __NR_swapoff,
  __NR_quotactl,
  __NR_acct,
  __NR_adjtimex,
  __NR_settimeofday,
  __NR_clock_settime,
  __NR_clock_adjtime,
  __NR_syslog,

  /* x86 and ia64 only; on every other architecture the call is not there. */
#ifdef __NR_iopl
  __NR_iopl,
#endif
#ifdef __NR_ioperm
  __NR_ioperm,
#endif
};

/* Lay the program out.  bwrap installs the filter in the child after the
 * mounts are done, so nothing here has to leave room for bwrap's own work
 * or for the exec that follows. */
static void
build (void)
{
  size_t i;

  n = 0;

  filter[n++] = (struct sock_filter) BPF_STMT (BPF_LD | BPF_W | BPF_ABS,
                                               offsetof (struct seccomp_data,
                                                         arch));
  filter[n++] = (struct sock_filter) BPF_JUMP (BPF_JMP | BPF_JEQ | BPF_K,
                                               ARCH, 1, 0);
  filter[n++] = (struct sock_filter) BPF_STMT (BPF_RET | BPF_K,
                                               SECCOMP_RET_KILL_PROCESS);

  filter[n++] = (struct sock_filter) BPF_STMT (BPF_LD | BPF_W | BPF_ABS,
                                               offsetof (struct seccomp_data,
                                                         nr));

  /* x32 reports x86_64's architecture word, so the check above lets it
   * through, and sets a bit in every syscall number it makes that no rule
   * below names -- every comparison misses and the payload reaches the
   * final ALLOW.  The number is where the bit is tested, because the
   * architecture word carries the same bit for its own reasons. */
  filter[n++] = (struct sock_filter) BPF_JUMP (BPF_JMP | BPF_JSET | BPF_K,
                                               X32_BIT, 0, 1);
  filter[n++] = (struct sock_filter) BPF_STMT (BPF_RET | BPF_K,
                                               SECCOMP_RET_KILL_PROCESS);

  for (i = 0; i < sizeof surface / sizeof *surface; i++)
    deny (surface[i]);

  if (n + 1 > MAX_FILTER)
    {
      fputs ("sandbox-seccomp: filter overflow\n", stderr);
      exit (1);
    }
  filter[n++] = (struct sock_filter) BPF_STMT (BPF_RET | BPF_K,
                                               SECCOMP_RET_ALLOW);
}

/* Write the whole program.  stdout is a file under sandbox4.sh, so a short
 * write is unlikely, but the loop costs nothing and removes the question. */
static int
emit (void)
{
  const char *p = (const char *) filter;
  size_t left = n * sizeof filter[0];

  while (left > 0)
    {
      ssize_t w = write (STDOUT_FILENO, p, left);

      if (w < 0)
        {
          if (errno == EINTR)
            continue;
          return -1;
        }
      p += w;
      left -= (size_t) w;
    }
  return 0;
}

int
main (void)
{
  build ();
  if (emit () != 0)
    {
      perror ("sandbox-seccomp: write");
      return 1;
    }
  return 0;
}
