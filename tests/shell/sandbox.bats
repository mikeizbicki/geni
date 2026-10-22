#!/usr/bin/env bats
#
# Tests for scripts/sandbox.sh.  bwrap is faked, so what is checked is the
# argument list sandbox() hands it -- the whole contract, since bwrap is what
# does the jailing -- and the syscall filter, which is compiled from the C
# beside the script and is the one part of the sandbox that is not an
# argument.  See tests/bin/bwrap for why a real jail is not made here.

setup() {
    source "$BATS_TEST_DIRNAME/../../scripts/sandbox.sh"

    export FAKE_BWRAP_LOG="$BATS_TEST_TMPDIR/bwrap-args"
    export FAKE_BWRAP_ENV="$BATS_TEST_TMPDIR/bwrap-env"
    : > "$FAKE_BWRAP_LOG"
    : > "$FAKE_BWRAP_ENV"

    # The fake is wrapped rather than used directly: the sandbox hands
    # bwrap an environment scrubbed of the caller's, so a fake that found
    # its own log path there would write nowhere -- which is the thing
    # being tested.  The wrapper carries that path inside itself, writes
    # down the environment it was handed, and passes the argument list on
    # to the shared fake.
    mkdir -p "$BATS_TEST_TMPDIR/bin"
    cat > "$BATS_TEST_TMPDIR/bin/bwrap" <<EOF
#!/bin/bash
env >> "$FAKE_BWRAP_ENV"
export FAKE_BWRAP_LOG="$FAKE_BWRAP_LOG"
exec "$BATS_TEST_DIRNAME/../bin/bwrap" "\$@"
EOF
    chmod +x "$BATS_TEST_TMPDIR/bin/bwrap"

    repo="$BATS_TEST_TMPDIR/repo"
    mkdir -p "$repo"
    cd "$repo"
}

# Hand every bwrap invocation to the fake.  A real jail cannot be made from
# inside the jail `sandbox pytest` is: the filter the outer sandbox installs
# refuses unshare and mount, and those are what a jail is made of.
fake_bwrap() {
    export PATH="$BATS_TEST_TMPDIR/bin:$BATS_TEST_DIRNAME/../bin:$PATH"
    export SANDBOX_SECCOMP_BLOB=/dev/null
}

# Whether bwrap was handed this exact word.
given() { grep -qxF -- "$1" "$FAKE_BWRAP_LOG"; }

@test "every kind of shared kernel object is unshared" {
    fake_bwrap
    run sandbox -- true
    [ "$status" -eq 0 ]
    given --die-with-parent
    given --new-session
    for flag in user ipc pid uts cgroup net; do
        given "--unshare-$flag"
    done
    given --cap-drop
    given ALL
}

@test "the environment is emptied and only the loader's own names go back" {
    fake_bwrap
    run sandbox -- true
    given --clearenv
    given --setenv
    given HOME
    given /tmp
    given PATH
    given "$PATH"
}

@test "the tree the shell stands in is the only writable path" {
    fake_bwrap
    run sandbox -- true
    given --bind
    given "$PWD"
}

@test "the credentials under /etc stay outside" {
    fake_bwrap
    run sandbox -- true
    ! given /etc/shadow
    ! given /etc/ssh
}

@test "a caller's arguments come after the filter" {
    fake_bwrap
    run sandbox --share-net -- true
    # the line numbers, with no brace of their own: the braces that open and
    # close a test are what tells the bats reader where its body ends
    seccomp=$(grep -nx -- --seccomp "$FAKE_BWRAP_LOG" | cut -d: -f1)
    share=$(grep -nx -- --share-net "$FAKE_BWRAP_LOG" | cut -d: -f1)
    [ -n "$seccomp" ]
    [ -n "$share" ]
    [ "$seccomp" -lt "$share" ]
}

@test "the syscall filter is compiled from the source beside the script" {
    unset SANDBOX_SECCOMP_BLOB
    export XDG_CACHE_HOME="$BATS_TEST_TMPDIR/cache"
    blob=$(sandbox-seccomp-blob)
    [ -s "$blob" ]
}

@test "a caller's own environment does not reach bwrap" {
    fake_bwrap
    export SECRET=hunter2
    run sandbox -- true
    [ "$status" -eq 0 ]
    ! grep -q '^SECRET=' "$FAKE_BWRAP_ENV"
}

@test "a --envs value is handed over by file and never in a word" {
    fake_bwrap
    export GH_TOKEN=ghp_hunter2
    run sandbox --envs=GH_TOKEN -- true
    [ "$status" -eq 0 ]
    given --file
    given /run/dic-env
    ! grep -q 'ghp_hunter2' "$FAKE_BWRAP_LOG"
}

@test "a leading NAME=value is handed over the same way" {
    fake_bwrap
    run sandbox NO_COLOR=hunter2 -- true
    [ "$status" -eq 0 ]
    given --file
    ! grep -q 'hunter2' "$FAKE_BWRAP_LOG"
}

@test "--envs takes a variable that is set to nothing" {
    fake_bwrap
    export EMPTY_VAR=
    run sandbox --envs=EMPTY_VAR -- true
    [ "$status" -eq 0 ]
    given --file
}

@test "--envs refuses a name the calling shell does not have" {
    fake_bwrap
    run sandbox --envs=NO_SUCH_VARIABLE_X -- true
    [ "$status" -ne 0 ]
    [ ! -s "$FAKE_BWRAP_LOG" ]
}
