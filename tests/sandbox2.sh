#!/bin/bash
#
# tests/sandbox2.sh -- the sandbox policy, tested as behaviour.
#
# Every claim below is one command run under a real bwrap, and the
# answer is whether it ran or was refused.  Nothing here names a flag,
# so a rewrite of scripts/sandbox.sh that keeps its promises keeps this
# file green, and a rewrite that breaks one cannot hide behind it.
#
# Run it by hand, outside any sandbox:
#
#     $ tests/sandbox2.sh
#
# It refuses to run inside one, and that is not a convenience: a nested
# jail is a smaller room inside the one it is in, so what the inner
# filter refuses the outer one refuses too, and every denial here would
# pass whichever filter was doing the refusing -- a jail that never
# started reads exactly like one that held.

set -u

here=$(cd -- "$(dirname -- "$0")" && pwd)
root=$(cd -- "$here/.." && pwd)

# --- the bouncer --------------------------------------------------------

grep -q bwrap /proc/1/cmdline && { echo "run this outside the sandbox" >&2; exit 1; }
bwrap --unshare-all --ro-bind / / -- true ||
    { echo "no working bwrap" >&2; exit 1; }

. "$root/scripts/sandbox.sh" || exit 1

cd "$(mktemp -d)" && echo hello > hello.txt

# --- the vocabulary -----------------------------------------------------

n=0
bad() { printf 'FAIL  %s\n' "$*"; n=$((n+1)); }
allow() { sandbox -- sh -c "$1" >/dev/null 2>&1 || bad "$1: refused, should run"; }
deny()  { sandbox -- sh -c "$1" >/dev/null 2>&1 && bad "$1: ran, should refuse"; }
allow_net() { sandbox --share-net -- sh -c "$1" >/dev/null 2>&1 || bad "$1: refused"; }

# --- the policy ---------------------------------------------------------

# the jail is live: /tmp is writable, so "deny" below means something
allow 'echo x > /tmp/canary'
allow 'cat hello.txt'

# the cwd is readable and not writable -- THE REGRESSION, one line
deny  'echo x > canary'
deny  'rm hello.txt'

# the credentials are not in the map, so no permission bit has to hold
deny  'cat ~/.ssh/id_rsa'
deny  'cat /etc/shadow'
allow 'cat /etc/passwd'          # control: /etc is there

# no route off the machine, and --share-net is the one knob
deny  'getent hosts example.com'
allow_net 'getent hosts example.com'

# one process; bwrap is PID 1
allow 'test "$(ls /proc | grep -c ^[0-9])" -lt 5'
deny  'cat /proc/1/environ | grep -q SECRET'

exit $((n > 0))
