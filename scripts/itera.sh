# This file defines the `itera` loop.
#
# `committe` turns one prompt into one commit, and the first commit is
# likely to have problems.  `itera` runs `committe`, then the tests, and
# feeds each failing round's output back to committe until the tests pass.
#
# The first round passes the user's own arguments to committe, so `-m`,
# `-c` and `--mid` reach the model; every later round passes only `-c`,
# which continues the conversation the first round started.  The request
# is not repeated, because `-c` already carries it in the history.
#
# Only the previous round's test output is passed back.  The tree is not:
# on a large project `files-to-prompt .` is huge, and the failure the
# model has to read is a few lines.
#
# The test runs in a sandbox with a read-only view of the repository and
# no network: it is the only thing in the loop that runs code the model
# wrote, and it must be assumed compromised.

function itera-usage() {
    cat <<'EOF'
usage: itera [flags] [REQUEST...]

Run committe, then the tests, then committe again, until the tests pass.
The first round passes REQUEST, every other argument, and itera's own
stdin on to committe; each later round passes only -c and the previous
round's failing test output, so the loop continues the conversation the
first round began.

The repository must already be green: itera checks this before it starts.
The sandbox mounts the repository read-only and has no network.

flags:
  --test CMD    the test command (default: $ITERA_TEST, or ./test.sh).
  --max N       the round budget (default: $ITERA_MAX, or 8).
  -h, --help    show this help.

Every other argument is passed on to committe's first round.
EOF
}

itera-detect-test() {
    # do not sandbox if the user specifies a command
    [[ -n ${ITERA_TEST:-} ]] && { echo "$ITERA_TEST"; return; }

    # sandbox autodetected test commands
    [[ -f pyproject.toml || -f pytest.ini || -f setup.cfg || -d tests ]] \
                                              && { echo 'sandbox pytest -q'; return; }
    echo 'itera-error: cannot detect tests; set ITERA_TEST or --test' >&2
    return 1
}

function itera() {
    local test_cmd="$(itera-detect-test)"
    local max="${ITERA_MAX:-8}"
    local -a request=()

    while (( $# )); do
        case "$1" in
            -h|--help) itera-usage; return 0 ;;
            --test)    test_cmd="$2"; shift 2 ;;
            --max)     max="$2"; shift 2 ;;
            --)        shift; request=("$@"); break ;;
            *)         request=("$@"); break ;;
        esac
    done

    # committe and sandbox are defined, because dic.sh sources them.
    # Save the caller's stdout on fd 3, so the tests below can stream to
    # it live while `$(...)` captures the same bytes for the next round.
    exec 3>&1

    # The tree must already be green.  Otherwise the loop would be fixing
    # pre-existing failures, and a green tree would otherwise end the loop
    # before it had made the change being asked for.
    # `local pre=$(...)` would read as a success whatever the test did:
    # local returns its own status and not the command substitution's.  So
    # the assignment is a statement of its own and the if reads the test's.
    local pre
    if ! pre=$(set -o pipefail; dic-run $test_cmd 2>&1 </dev/null | tee /dev/fd/3); then
        echo 'itera-error: tests do not pass before starting' >&2
        return 1
    fi

    local i=0 out=
    while (( i < max )); do
        i=$((i + 1))
        local cols=${COLUMNS:-$(tput cols 2>/dev/null || echo 80)}
        local rule; printf -v rule '%*s' "$cols" ''; rule=${rule// /-}
        printf '%s\nitera: round %d/%d\n%s\n' "$rule" "$i" "$max" "$rule" >&2

        # Round one is the user's; every later round continues the
        # conversation it started.
        local -a args
        if (( i == 1 )); then
            args=("${request[@]}")
        else
            args=(-c)
        fi

        # Round one has no failure to feed back, and running it under a
        # pipe of its own would replace itera's stdin with an empty one:
        # a heredoc, a `files-to-prompt`, or any pipe into itera is the
        # request, and committe -- and so dic -- must read it.  Every
        # later round does feed the previous round's failure back, which
        # is the only thing the model needs; a project's whole tree is
        # not.
        local status=
        if (( i == 1 )); then
            committe "${args[@]}" >&2
            status=$?
        else
            { [[ -n "$out" ]] && printf 'The tests fail with:\n\n%s\n' "$out"; } \
                | committe "${args[@]}" >&2
            status=${PIPESTATUS[1]}
        fi
        if (( status != 0 )); then
            echo 'itera-error: committe failed' >&2
            return 1
        fi

        # The tests run after committe, so a round that changes nothing and
        # a round that fixes the failure are told apart by the run after it.
        if out=$(set -o pipefail; dic-run $test_cmd 2>&1 </dev/null | tee /dev/fd/3); then
            printf 'itera: green after %d round(s)\n' "$i" >&2
            return 0
        fi
    done

    printf 'itera: hit ITERA_MAX=%d\n' "$max" >&2
    return 1
}
