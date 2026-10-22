#!/bin/bash
#
# tests/sandbox.sh -- the sandbox policy, tested as behaviour.
#
# shell/sandbox.bats checks the argument list sandbox() hands bwrap
# because bwrap is what does the jailing and the list is the contract.
# This file checks the jail the list builds: every claim below is a
# command run under a real bwrap, and what comes back is whether it ran
# or was refused.  Nothing here names a flag, so a rewrite of
# scripts/sandbox.sh that keeps its promises keeps this file green, and
# a rewrite that breaks one cannot hide behind it.
#
# Run it by hand, outside any sandbox:
#
#     $ tests/sandbox.sh
#
# It refuses to run inside one, and that is not a convenience.  A nested
# jail is a smaller room inside the one it is in, so what the inner
# filter refuses the outer one refuses too, and every syscall denial
# below would pass whichever filter was doing the refusing -- a jail
# that never started reads exactly like one that held.  Outside any
# jail, the filter this file compiles and installs is the only one there
# is, and every line below is about it.
#
# Nothing here is on test.sh's path: test.sh runs under `sandbox`, and
# this file cannot.

set -u

here=$(cd -- "$(dirname -- "$0")" && pwd)
root=$(cd -- "$here/.." && pwd)

# --- the bouncer --------------------------------------------------------
#
# A bwrap in PATH is not the question; a real jail is.  `--unshare-all`
# over a read-only / is the smallest jail bwrap can make, and if that
# fails there is no point making a bigger one.

if grep -qa bwrap /proc/1/cmdline 2>/dev/null; then
    echo "sandbox: run this outside the sandbox" >&2
    exit 1
fi

if ! bwrap --unshare-all --ro-bind / / -- true >/dev/null 2>&1; then
    echo "sandbox: no working bwrap here" >&2
    exit 1
fi

. "$root/scripts/sandbox.sh" || exit 1

# --- the vocabulary -----------------------------------------------------
#
# Every claim is one label and one command.  The label names a promise
# and never a flag, and each denial has an allow beside it: a jail that
# never started refuses everything, so a page of denials alone passes
# for the wrong reason, and only the allow beside one says that the
# denial beside it means something.

n=0
f=0
quiet=
ok() { n=$((n+1)); [[ $quiet ]] || printf 'ok   %s\n' "$*"; }
no() { n=$((n+1)); f=$((f+1)); [[ $quiet ]] || printf 'FAIL %s\n' "$*"; }

allow() {
    local label=$1; shift
    if sandbox "$@" >/dev/null 2>&1; then ok "$label"
    else no "$label (refused; must run)"; fi
}
deny() {
    local label=$1; shift
    if sandbox "$@" >/dev/null 2>&1; then no "$label (ran; must refuse)"
    else ok "$label"; fi
}
allownet() {
    local label=$1; shift
    if sandbox --share-net "$@" >/dev/null 2>&1; then ok "$label"
    else no "$label (refused; must run with --share-net)"; fi
}

# --- the stand ----------------------------------------------------------
#
# The jail binds $PWD, so the work happens in a directory of its own
# making with a file in it.  The first file the jail is not meant to
# reach is a real one under the shell's own $HOME, because a path that
# is not in the map is the wall and not a mode; the listener is bound on
# the host's own loopback, and --unshare-net gives the jail a loopback
# with nothing listening on it.

work=$(mktemp -d) || exit 1
secret=$HOME/.geni-sandbox-$$
listener=
cleanup() {
    [[ -n $listener ]] && kill "$listener" 2>/dev/null
    rm -rf "$work" "$secret"
}
trap cleanup EXIT

echo hunter2 > "$secret" || exit 1

cd "$work" || exit 1
echo hello > hello.txt

python3 -c '
import socket, sys
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1], flush=True)
s.listen(8)
while True:
    c, _ = s.accept()
    c.close()
' >port 2>/dev/null &
listener=$!
for _ in $(seq 1 50); do
    [[ -s port ]] && break
    sleep 0.1
done
[[ -s port ]] || { echo "sandbox: no loopback listener" >&2; exit 1; }
port=$(cat port)

cat > denied.py <<'PY'
# Exit 0 when syscall NUMBER comes back as EPERM, which is what the
# seccomp filter returns and what a running syscall never does.
import ctypes, errno, sys
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
libc.syscall.argtypes = [ctypes.c_long]
ctypes.set_errno(0)
libc.syscall(int(sys.argv[1]))
sys.exit(0 if ctypes.get_errno() == errno.EPERM else 1)
PY

# --- the policy ---------------------------------------------------------
#
# Every line below is a promise the README makes, restated as a command
# and its answer.  An allow is the control the denials beside it need; a
# deny is the promise itself.

policy() {
    # the jail is live: /tmp is a fresh tmpfs, so a write there is what
    # says the page could have run at all
    allow "the jail is live" sh -c 'echo alive > /tmp/probe'

    # a tool from /usr runs: the loader is bound, and this is the
    # control every other allow rides on
    allow "a tool from /usr/bin runs" sh -c 'command -v sh >/dev/null'

    # the tree the shell stands in: readable, and not writable.  It is
    # the whole reason to have a sandbox and the whole reason not to
    # bind it rw
    allow "the tree is readable" cat hello.txt
    deny  "the tree is not writable" sh -c 'echo x > canary'
    deny  "the tree is not removable" rm hello.txt

    # the host's own home: not in the map at all, so not behind a
    # permission bit a mode change would open
    deny  "a file in the host's home is outside" cat "$secret"

    # /etc is bound by a named list: what a program needs to start, and
    # not the credentials a package left there
    allow "/etc is bound" cat /etc/passwd
    deny  "the shadow file is outside" cat /etc/shadow

    # the host's network: unshared, so the host's own listener is
    # unreachable on a loopback the jail cannot see, and --share-net is
    # the one knob that puts it back
    deny  "the host loopback is unreachable" \
        python3 -c "import socket; socket.create_connection(('127.0.0.1', $port), timeout=3)"
    allownet "the host loopback is reachable with --share-net" \
        python3 -c "import socket; socket.create_connection(('127.0.0.1', $port), timeout=3)"

    # the exported environment is emptied.  A leading NAME=value is the
    # one route in, and it is the control the denial beside it rides on
    allow "a leading NAME=value arrives" GENI_TEST=yes \
        sh -c 'test "$GENI_TEST" = yes'
    deny  "an exported secret does not arrive" \
        sh -c 'test "$GENI_SECRET" = hunter2'
}

export GENI_SECRET=hunter2
policy
unset GENI_SECRET

# --- the syscall filter -------------------------------------------------
#
# The list is read from the C beside the script and expanded by cc, so a
# deny() added there and forgotten here is impossible, and the numbers
# are the headers' own rather than a second transcription to keep in
# step.

names=$(sed -n '/^surface\[\] = {/,/^};/p' "$root/scripts/sandbox-seccomp.c" |
        grep -o '__NR_[a-z_0-9]*' | sort -u)

{
    echo '#include <stdio.h>'
    echo '#include <sys/syscall.h>'
    echo 'int main(void) {'
    for name in $names; do
        echo "#ifdef $name"
        printf '    printf("%%s %%ld\\n", "%s", (long)%s);\n' "$name" "$name"
        echo "#endif"
    done
    echo '    return 0;'
    echo '}'
} >nrs.c
cc -std=gnu11 -o nrs nrs.c || {
    echo "sandbox: cannot build the syscall probe" >&2
    exit 1
}
./nrs >nrs.txt || exit 1

while read -r name nr; do
    deny "$name is refused" python3 denied.py "$nr"
done <nrs.txt

# The x32 alias: a 64-bit kernel reports the same AUDIT_ARCH_X86_64 for
# an x32 binary and its numbers carry a bit no rule names, so a filter
# that allows by omission lets one through.  The number from the list is
# the one to try.

while read -r name nr; do
    deny "the x32 alias of $name is refused" \
        python3 denied.py "$((0x40000000 + nr))"
done <nrs.txt

# --- the mutation self-test ---------------------------------------------
#
# A check that cannot fail is not a check, and this is how the page says
# so about itself: the rw bind the checks above were written for is put
# back into a copy of the script, and the check is asked whether it
# notices.  A mutant the page does not notice is a page that passed for
# the wrong reason.

mmk() {
    rm -rf mutant
    mkdir mutant
    cp -r "$root/scripts/." mutant/ || return 1
    python3 - <<'PY'
import sys
p = "mutant/sandbox.sh"
s = open(p).read()
s2 = s.replace('--ro-bind "$PWD" "$PWD"', '--bind "$PWD" "$PWD"')
if s2 == s:
    sys.exit(1)
open(p, "w").write(s2)
PY
}

if mmk &&
   ! ( . mutant/sandbox.sh
       quiet=1
       f=0
       deny "the tree is not writable" sh -c 'echo x > mutant-canary'
       exit "$f" ); then
    ok "the tree-not-writable check notices an rw \$PWD"
else
    no "the tree-not-writable check does not notice an rw \$PWD"
fi

# --- the summary --------------------------------------------------------

printf '\n%d checks, %d failed\n' "$n" "$f"
(( f == 0 ))
