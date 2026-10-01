# itera: repeat until the tests pass.
#
# The loop is the shape: a pre-flight proves the tree starts green, a round
# asks committe for the fix, a re-run decides whether it worked, and the
# budget caps the asking.  committe is what talks to the model, so this file
# never reaches a provider itself.
#
#     itera [--max N] [--test CMD] 'request'
#
# ITERA_MAX (default 5) is the round budget; --max is the same knob on the
# command line.  ITERA_TEST is the test command, and it is what a detection
# is not asked for: when it is unset, itera-detect-test guesses one from the
# tree.  That guess is a word, `sandbox ./test.sh`, and it runs as one word
# -- a script, not an expression.
#
# committe is called in the tree, so it commits each round; round 1 is handed
# itera's own stdin, which is why a heredoc into itera is a request and not
# a pipe.  A later round replaces that with the failing round's own output,
# because the request is already in the history --c replays.

# The test command when the caller names none, and the git-state check a
# round must pass.  Both are pure functions of the tree.
ITERA_MAX=${ITERA_MAX:-5}

# Where a finished run leaves its elapsed seconds, one per line, so the next
# run can print an estimate.  Per-repo and untracked, because a test time
# belongs to this checkout and not to the project.  A single printf to an
# O_APPEND file cannot interleave with another writer, so no lock is needed.
itera-ema-file() {
    local dir
    dir=$(git rev-parse --git-dir 2>/dev/null) || return 1
    printf '%s\n' "${ITERA_EMA:-$dir/itera-test-times}"
}

# The mean of the last 20 runs, in seconds, or empty when there is no
# history.  A mean and not an EMA: one slow run should not own the estimate,
# and the tail -n 20 is what bounds a repo's history to a file it can read
# in a millisecond.  A lone column is deliberate -- lines, not bytes, is
# what pv can infer an ETA from, and a byte count would be wrong in both
# directions on a tree that reconfigures its test output.
itera-estimate() {
    local file
    file=$(itera-ema-file) || return 0
    [[ -s $file ]] || return 0
    tail -n 20 "$file" | awk '{s += $1} END {printf "%.1f", s / NR}'
}

# Record one elapsed time, given as $1 seconds.  Called only after a test
# that passed: a timeout or an interrupted run is not a completion time and
# would drag the next estimate with it.
itera-record-test() {
    local file
    file=$(itera-ema-file) || return 0
    printf '%.3f\n' "$1" >> "$file"
}

# Whether the tree is clean enough for a round to commit into.  Printed and
# refused, because committe commits, and a dirty tree makes its diff
# something other than the round's.
itera-require-clean-tree() {
    if [[ -n $(git status --porcelain --untracked-files=no) ]]; then
        echo 'itera: working tree has uncommitted changes' >&2
        return 1
    fi
    if ! git diff --cached --quiet; then
        echo 'itera: staging area is non-empty' >&2
        return 1
    fi
}

# Guess the test command from the tree: ./test.sh wins because a project
# that writes one has said what its tests are, then the languages whose
# runners are conventional.  A tree with nothing is an error and not a
# skip, because a round with no test is a green run that covered nothing.
itera-detect-test() {
    if [[ -n ${ITERA_TEST:-} ]]; then
        printf '%s\n' "$ITERA_TEST"
        return 0
    fi
    if [[ -x ./test.sh ]]; then
        printf 'sandbox ./test.sh\n'
        return 0
    fi
    if [[ -f pyproject.toml || -f setup.py ]]; then
        printf 'sandbox pytest -q\n'
        return 0
    fi
    if [[ -f package.json ]]; then
        printf 'sandbox npm test\n'
        return 0
    fi
    if [[ -f Cargo.toml ]]; then
        printf 'sandbox cargo test\n'
        return 0
    fi
    if [[ -f go.mod ]]; then
        printf 'sandbox go test ./...\n'
        return 0
    fi
    echo 'itera: no test command; pass --test' >&2
    return 1
}

# Run the test once through pv and return its status.  pv repaints one line
# on stderr and the test's own output is captured, then read back only when
# the status is nonzero -- a green round says nothing, which is what a green
# round should say.  PIPESTATUS[0] is the test's status; pv's is its own,
# and it is not the one this function returns.
itera-run-tests() {
    local captured status estimate start elapsed
    # A standalone call has no test_cmd from itera(); detect it here, so
    # `itera-run-tests` is the run a round makes and not an empty pipeline.
    if [[ -z ${test_cmd:-} ]]; then
        test_cmd=$(itera-detect-test) || return 1
    fi
    captured=$(mktemp) || return 1
    estimate=$(itera-estimate)
    local label='test'
    [[ -n $estimate ]] && label="test (expect ~${estimate}s)"
    start=$(date +%s.%N)
    # shellcheck disable=SC2086  # $test_cmd is a word: `sandbox pytest -q`
    $test_cmd 2>&1 | pv -l -t -N "$label" >"$captured"
    status=${PIPESTATUS[0]}
    if (( status == 0 )); then
        # Time and record here, not in itera(), so a standalone run is
        # also a data point and there is only one place a run is timed.
        elapsed=$(awk -v s="$start" -v e="$(date +%s.%N)" \
            'BEGIN { printf "%.3f", e - s }')
        itera-record-test "$elapsed"
        rm -f "$captured"
        return 0
    fi
    printf '\n' >&2                    # close pv's line before the dump
    cat "$captured" >&2
    rm -f "$captured"
    return "$status"
}

# The loop: pre-flight, then rounds until green or the budget runs out.
itera() {
    local max=${ITERA_MAX} arg
    while (( $# )); do
        case $1 in
            --max) max=$2; shift 2 ;;
            --test) ITERA_TEST=$2; shift 2 ;;
            --) shift; break ;;
            -*) echo "itera: unknown flag: $1" >&2; return 64 ;;
            *) break ;;
        esac
    done
    (( $# )) || { echo 'itera: no request' >&2; return 64; }
    local request=$1

    test_cmd=$(itera-detect-test) || return 1

    # Pre-flight: the tree must be green before anyone touches it, or a red
    # round says nothing about the model's change.
    if ! itera-run-tests; then
        echo 'itera: tests do not pass before starting' >&2
        return 1
    fi
    itera-require-clean-tree || return 1

    local round=1
    while (( round <= max )); do
        if (( round == 1 )); then
            if ! committe "$request"; then
                echo 'itera: committe failed' >&2
                return 1
            fi
        else
            if ! committe -c; then
                echo 'itera: committe failed' >&2
                return 1
            fi
        fi
        if itera-run-tests; then
            echo "itera: green after $round round(s)"
            return 0
        fi
        round=$((round + 1))
    done
    echo "itera: hit ITERA_MAX=$max without green" >&2
    return 1
}

# Run itera when sourced by a shell that called it, and leave the functions
# for a test that sourced this file to call.
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    itera "$@"
fi
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

# dic-run lives in dic.sh, which sources this file in turn.  A caller
# that sources this file directly -- a test, say -- gets the helper by
# sourcing dic.sh first; when dic.sh is the entry point, _DIC_LOADED
# is already set and this line does nothing.
[[ -n ${_DIC_LOADED:-} ]] || source "${BASH_SOURCE[0]%/*}/dic.sh"

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

    # a tree that ships its own test entry point says what its tests are
    [[ -x ./test.sh ]] && { echo 'sandbox ./test.sh'; return; }

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
