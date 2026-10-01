#!/usr/bin/env bats
#
# Tests for scripts/itera.sh.  committe is stubbed and dic is not reached at
# all: itera's own job is the shape of the loop -- when a round is enough,
# what a later round is handed, when to stop -- and committe's own behaviour
# against the fake dic is what committe.bats tests.  The tree is a real
# temporary git repository, so `git rev-parse` and the round markers are what
# a user would see.

setup() {
    source "$BATS_TEST_DIRNAME/../../scripts/itera.sh"
    # itera refuses to start without a sandbox command, and the real function
    # is what it looks for; no round here reaches it
    source "$BATS_TEST_DIRNAME/../../scripts/sandbox.sh"

    repo="$BATS_TEST_TMPDIR/repo"
    mkdir -p "$repo"
    cd "$repo"
    git init -q
    git config user.email test@test
    git config user.name test
    git commit --allow-empty -qm init

    export COMMITTE_ARGS="$BATS_TEST_TMPDIR/args"
    export COMMITTE_STDIN="$BATS_TEST_TMPDIR/stdin"
    export ITERA_STATE="$BATS_TEST_TMPDIR/state"
    export ITERA_TEST="$BATS_TEST_DIRNAME/../fixtures/itera/pass.sh"
    : > "$COMMITTE_ARGS"
    : > "$COMMITTE_STDIN"

    # committe, stubbed: every call is recorded with the arguments it was
    # given and the round's failure as it arrived on stdin, and its status is
    # whatever the test asks for through COMMITTE_STATUS.
    committe() {
        local input; input=$(cat)
        printf '%s\n' "$*" >> "$COMMITTE_ARGS"
        printf '%s\n' "$input" >> "$COMMITTE_STDIN"
        return "${COMMITTE_STATUS:-0}"
    }
}

# The arguments the Nth call to committe was given.
call_args() { sed -n "$1p" "$COMMITTE_ARGS"; }

@test "a green tree and a green round are one round" {
    run itera 'a request'
    [ "$status" -eq 0 ]
    [[ "$output" == *"green after 1 round(s)"* ]]
    [ "$(call_args 1)" = "a request" ]
}

@test "a tree that is red before any round is refused" {
    export ITERA_TEST="$BATS_TEST_DIRNAME/../fixtures/itera/fail.sh"
    run itera 'a request'
    [ "$status" -eq 1 ]
    [[ "$output" == *"tests do not pass before starting"* ]]
    [ ! -s "$COMMITTE_ARGS" ]        # the model was never asked
}

@test "a round that fixed nothing is continued with -c and its own output" {
    export ITERA_TEST="$BATS_TEST_DIRNAME/../fixtures/itera/fail-once.sh"
    run itera 'a request'
    [ "$status" -eq 0 ]
    [[ "$output" == *"green after 2 round(s)"* ]]
    [ "$(call_args 1)" = "a request" ]
    [ "$(call_args 2)" = "-c" ]      # the request is in the history, not here
    # and the failing round is what the second call was handed
    grep -q 'the second call is the red one' "$COMMITTE_STDIN"
}

@test "the round budget is a ceiling and not a target" {
    export ITERA_TEST="$BATS_TEST_DIRNAME/../fixtures/itera/fail-forever.sh"
    run itera --max 2 'a request'
    [ "$status" -eq 1 ]
    [[ "$output" == *"hit ITERA_MAX=2"* ]]
    [ -n "$(call_args 2)" ] && [ -z "$(call_args 3)" ]
}

@test "a committe that fails is the loop's failure too" {
    export COMMITTE_STATUS=1
    run itera 'a request'
    [ "$status" -eq 1 ]
    [[ "$output" == *"committe failed"* ]]
}

@test "detection finds a python project without a word from the user" {
    unset ITERA_TEST
    touch pyproject.toml
    run itera-detect-test
    [ "$status" -eq 0 ]
    [ "$output" = "sandbox pytest -q" ]
}

@test "detection prefers a project's own test entry point" {
    unset ITERA_TEST
    touch pyproject.toml      # both are here; ./test.sh is the one that wins
    printf '#!/bin/bash\nexit 0\n' > test.sh
    chmod +x test.sh
    run itera-detect-test
    [ "$status" -eq 0 ]
    [ "$output" = "sandbox ./test.sh" ]
}

@test "detection with nothing to find names the way out" {
    unset ITERA_TEST
    run itera-detect-test
    [ "$status" -eq 1 ]
    [[ "$output" == *"--test"* ]]
}

@test "ITERA_TEST is what a detection is not asked for" {
    touch pyproject.toml
    run itera-detect-test
    [ "$status" -eq 0 ]
    [ "$output" = "$ITERA_TEST" ]
}
