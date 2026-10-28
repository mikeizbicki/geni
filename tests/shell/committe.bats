#!/usr/bin/env bats
#
# Tests for scripts/committe.sh.  The fake dic on PATH replays a recorded
# transcript, so there is no network and no cost; git, the temporary
# repository, and committe itself are all the real thing.

setup() {
    # The fake must beat any real dic or llm a developer has installed,
    # because committe prefers the real one when it can have it.
    export PATH="$BATS_TEST_DIRNAME/../bin:$PATH"
    export FAKE_DIC_CASE="$BATS_TEST_DIRNAME/../fixtures/transcripts/add-file"

    repo=$(mktemp -d)
    cd "$repo" || return 1
    git init -q
    git config user.email test@test
    git config user.name test
    printf 'hello\n' > hello.txt
    git add hello.txt
    git commit -qm init

    source "$BATS_TEST_DIRNAME/../../scripts/committe.sh"
}

teardown() {
    cd / || true
    rm -rf "$repo"
}

@test "committe applies a well-formed new file" {
    run committe 'add primes.py'
    [ "$status" -eq 0 ]
    [ -f primes.py ]
}

@test "committe tags the commit with [geni]" {
    run committe 'add primes.py'
    [ "$status" -eq 0 ]
    run git log -1 --format=%s
    [[ "$output" == "[geni] "* ]]
}

@test "committe deletes a file" {
    export FAKE_DIC_CASE="$BATS_TEST_DIRNAME/../fixtures/transcripts/delete"
    run committe 'remove hello.txt'
    [ "$status" -eq 0 ]
    [ ! -f hello.txt ]
}

@test "committe renames a file" {
    export FAKE_DIC_CASE="$BATS_TEST_DIRNAME/../fixtures/transcripts/rename"
    run committe 'rename hello.txt'
    [ "$status" -eq 0 ]
    [ -f goodbye.txt ]
    [ ! -f hello.txt ]
}

@test "committe exits 2 when the model asks a question" {
    export FAKE_DIC_CASE="$BATS_TEST_DIRNAME/../fixtures/transcripts/question"
    run committe 'roll a die'
    [ "$status" -eq 2 ]
    [[ "$output" == *"made no change"* ]]
}

@test "committe refuses to run on a dirty tree" {
    echo junk > hello.txt
    run committe 'add primes.py'
    [ "$status" -eq 1 ]
    [[ "$output" == *"uncommitted changes"* ]]
}

@test "committe refuses to run on a staged tree" {
    echo junk > hello.txt
    git add hello.txt
    run committe 'add primes.py'
    [ "$status" -eq 1 ]
    [[ "$output" == *"staging area is non-empty"* ]]
}

@test "-f overrides the clean check" {
    echo junk > hello.txt
    run committe -f 'add primes.py'
    [ "$status" -eq 0 ]
}

@test "a flag committe does not know passes through to the model" {
    export FAKE_DIC_LOG="$repo/args"
    run committe --some-new-flag 'add primes.py'
    [ "$status" -eq 0 ]
    grep -q -- '--some-new-flag' "$FAKE_DIC_LOG"
}

@test "committe restores the caller's session name" {
    # The child session is set on the one dic call that needs it and not
    # exported for the shell: an export here would replace the caller's
    # session for every later committe, so each fork would nest under the
    # last, and a session name is one filename -- the '/' of the hierarchy
    # is escaped -- so the name would grow past NAME_MAX after a few calls.
    export DIC_SESSION=caller
    committe 'add primes.py' >/dev/null 2>&1
    [ "$DIC_SESSION" = "caller" ]
}

@test "a retry carries the failed file and drops -c" {
    # A fake git-apply-fuzzy that always fails and blames hello.txt,
    # which exists.  The real one is exercised by every other committe
    # test; this one is about the retry path and only the retry path,
    # so it overrides the fake to make the failing case reachable.
    fake_bin="$BATS_TEST_TMPDIR/bin"
    mkdir -p "$fake_bin"
    cat > "$fake_bin/git-apply-fuzzy" <<'FAKE'
#!/bin/bash
for a in "$@"; do
    [[ $a == --failed-files ]] && failed=1
done
printf 'hello.txt\t1\n'
printf 'error: hello.txt: hunk 1 no match\n' >&2
exit 1
FAKE
    chmod +x "$fake_bin/git-apply-fuzzy"
    export PATH="$fake_bin:$PATH"
    export FAKE_DIC_LOG="$BATS_TEST_TMPDIR/dic-args"
    : > "$FAKE_DIC_LOG"

    run committe --retries 1 -c 'add primes.py'
    [ "$status" -eq 1 ]

    # two model calls: the first, and the one retry
    [ "$(grep -c '^--prepend-prompt$' "$FAKE_DIC_LOG")" -eq 2 ]
    # the retry prompt names the file the fake blamed and shows what
    # the file says, which is the whole of what the accumulator is for
    grep -q 'Earlier patch attempts were rejected' "$FAKE_DIC_LOG"
    grep -q '^=== hello.txt (near line 1) ===$' "$FAKE_DIC_LOG"
    grep -q '^hello$' "$FAKE_DIC_LOG"
    # and -c reaches the first call only: the retry rebuilds its
    # context from the block, so it does not also continue the failed
    # turn
    [ "$(grep -c '^-c$' "$FAKE_DIC_LOG")" -eq 1 ]
}
