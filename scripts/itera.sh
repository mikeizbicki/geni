# itera: repeat until the tests pass.
#
# The loop is the shape: a pre-flight proves the tree starts green, a round
# asks committe for the fix, a re-run decides whether it worked, and the
# budget caps the asking.  committe is what talks to the model, so this file
# never reaches a provider itself.
#
#     itera [--max N] [--test CMD] [--allow-red] 'request'
#
# itera is a four-step harness (see scripts/AGENTS.md):
#
#   itera-validate   pure: is this run possible at all
#   itera-prepare    worktree and record where the run started
#   itera-run        the round loop
#   itera-record     squash the loop and fast-forward the original branch
#
# and itera-rollback, which is not a step: it prints the resume line.
#
# The loop runs in its own worktree -- git worktree add at prepare, one
# commit per round inside it, and itera-record squashes the rounds into
# one commit and fast-forwards the original branch with it.  The original
# worktree is never modified until that fast-forward, so a failure leaves
# it exactly where it started -- nothing to undo -- and the interrupted
# work in the loop worktree, which itera-rollback names.
#
# itera does not fork DIC_SESSION: each round's committe does that itself,
# and it does it at its own prepare, so round 1's -c reads the session the
# user was already in.  A forked session at itera-prepare time would be a
# second ref that nothing here needs.
#
# itera's own flags are parsed first; the first `--` ends them, and
# everything after -- including a flag itera does not know, such as -c --
# is passed through to committe, which passes it on to dic.
#
# ITERA_MAX (default 5) is the round budget; --max is the same knob on the
# command line.  ITERA_TEST is the test command, and it is what a detection
# is not asked for: when it is unset, itera-detect-test guesses one from the
# tree.  That guess is a word, `sandbox ./test.sh`, and it runs as one word
# -- a script, not an expression.
#
# committe is called in the worktree, so it commits each round there;
# round 1 is handed itera's own stdin, which is why a heredoc into itera is
# a request and not a pipe.  A later round replaces that with the failing
# round's own output, because the request is already in the history --c
# replays.

# dic-run and dic-git-clean live in dic.sh, which sources this file in
# turn.  A caller that sources this file directly -- a test, say --
# gets the helpers by sourcing dic.sh first; when dic.sh is the entry
# point, _DIC_LOADED is already set and this line does nothing.
[[ -n ${_DIC_LOADED:-} ]] || source "${BASH_SOURCE[0]%/*}/dic.sh"

# The round budget when the caller names none.
ITERA_MAX=${ITERA_MAX:-5}

# Where a finished run leaves its elapsed seconds, one per line, so the next
# run can print an estimate.  Per-repo and untracked, because a test time
# belongs to this checkout and not to the project.  A single printf to an
# O_APPEND file cannot interleave with another writer, so no lock is needed.
#
# The file is shared: --git-common-dir is the main .git, so a geni worktree
# and the tree it branched from read and write one file instead of one
# apiece.  --path-format=absolute is why the path prints absolute; the
# common dir is otherwise relative to the cwd, and in a subdirectory the
# relative form would name the wrong file.
#
# A worktree whose tests were genuinely slower would pollute the estimate,
# but the same commit is checked out in both and the machine is the same,
# so it is close enough for a progress label.  The honest fix -- record
# the commit each sample was taken on and only weight the ancestors of
# HEAD -- is not worth it for that edge case.  Left for later.
itera-times-file() {
    local dir
    dir=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || return 1
    printf '%s\n' "${ITERA_TIMES:-$dir/geni/itera/test-times}"
}

# The mean of the last 20 runs, in seconds, or empty when there is no
# history.  A mean and not an EMA: one slow run should not own the estimate,
# and the tail -n 20 is what bounds a repo's history to a file it can read
# in a millisecond.  A lone column is deliberate -- lines, not bytes, is
# what pv can infer an ETA from, and a byte count would be wrong in both
# directions on a tree that reconfigures its test output.
itera-estimate() {
    local file
    file=$(itera-times-file) || return 0
    [[ -s $file ]] || return 0
    tail -n 20 "$file" | awk '{s += $1} END {printf "%.1f", s / NR}'
}

# Record one elapsed time, given as $1 seconds.  Called only after a test
# that passed: a timeout or an interrupted run is not a completion time and
# would drag the next estimate with it.
itera-record-time() {
    local file
    file=$(itera-times-file) || return 0
    # the times file lives under .git/geni/itera/, which a fresh
    # checkout does not have yet
    mkdir -p "$(dirname "$file")"
    printf '%.3f\n' "$1" >> "$file"
}

# Where the loop worktree goes.  --git-common-dir is the main .git, so a
# run started in a geni worktree and one started in the tree it branched
# from put their loop worktrees in the same place, and a branch cut from
# one is visible to the other.  The token names the branch, and the
# branch is unique per worktree, so the path is too.
itera-worktree-path() {
    local dir
    dir=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || return 1
    printf '%s\n' "$dir/geni/itera/worktrees/$1"
}

# -------------------------------------------------------------- validate

# Pure: no model call, no ref moved.  Detects the test command, refuses a
# dirty tree, and runs the pre-flight unless --allow-red.  The clean-tree
# check is here and not in committe because itera commits every round, and
# a dirty tree would make the round's commit something other than the
# round's patch.
itera-validate() {
    test_cmd=$(itera-detect-test) || return 1
    if [[ -n $(git status --porcelain --untracked-files=no) ]]; then
        dic-error 'itera-error: working tree has uncommitted changes'
        return 1
    fi
    if ! git diff --cached --quiet; then
        dic-error 'itera-error: staging area is non-empty'
        return 1
    fi
    if (( ! ITERA_ALLOW_RED )); then
        if ! itera-run-tests; then
            dic-error 'itera-error: tests do not pass before starting'
            dic-hint 'itera-hint: pass --allow-red to start anyway'
            return 1
        fi
    fi
    return 0
}

# --------------------------------------------------------------- prepare

# Worktree and record.  The loop worktree is what isolates a run: the
# original worktree stays exactly where it is, the rounds commit in the
# loop worktree, and itera-record squashes them there and fast-forwards
# the original branch with the result.  Nothing is forked in DIC_SESSION
# -- each round's committe does that itself -- so round 1's -c reads the
# session the user was already in.
itera-prepare() {
    local branch token
    branch=$(git symbolic-ref --quiet --short HEAD) || {
        dic-error 'itera-error: detached HEAD; check out a branch first'
        return 1
    }
    ITERA_ORIGINAL_BRANCH=$branch
    ITERA_ORIGINAL_HEAD=$(git rev-parse HEAD) || return 1
    ITERA_ORIGINAL_WORKTREE=$PWD
    token=$(worktree-token "$branch")
    ITERA_BRANCH="geni/itera-$token"
    ITERA_WORKTREE=$(itera-worktree-path "$token") || return 1

    # A crashed run leaves its worktree behind, and git worktree add
    # refuses a path that already exists.  The path is unique per branch
    # and per second, so an existing one is a leftover and not a live
    # run -- nothing here says which, now that --continue and --abort are
    # gone -- and the bytes are the user's to delete, so the script names
    # the command and stops.
    git worktree prune >/dev/null 2>&1 || true
    if [[ -e $ITERA_WORKTREE ]]; then
        dic-error "itera-error: $ITERA_WORKTREE already exists"
        dic-hint  "itera-hint: remove it with: rm -rf $ITERA_WORKTREE"
        return 1
    fi

    dic-run git worktree add -b "$ITERA_BRANCH" \
        "$ITERA_WORKTREE" "$ITERA_ORIGINAL_HEAD" || {
        dic-error "itera-error: could not create worktree $ITERA_WORKTREE"
        return 1
    }
    return 0
}

# ------------------------------------------------------------------- run

# The loop: pre-flight is already done, so round 1 asks the model and a
# later round continues the conversation with the failing output.  The
# starting round is $1, so --continue can re-enter at round 2 without
# re-asking the request.  committe's own exit status is the loop's, so a
# question the model asked reaches the caller as 2 and not as a failure.
itera-run() {
    local round=${1:-1}
    local status
    while (( round <= ITERA_MAX )); do
        if (( round == 1 )); then
            # Read the request from the buffer itera() wrote at entry,
            # not from fd 0: the pre-flight test that ran between has
            # already seen fd 0, and a test that read it would leave
            # committe at EOF.  With no buffer -- a tty-run, or a
            # direct call to itera-run -- fd 0 is left alone.
            if [[ -f ${ITERA_STDINFILE:-} ]]; then
                committe "${ITERA_ARGS[@]}" < "$ITERA_STDINFILE"
            else
                committe "${ITERA_ARGS[@]}"
            fi
        else
            # -c continues the round-1 conversation, and the failed
            # round's output is the request for this one: dic appends a
            # non-tty stdin after a blank line, so the model reads what
            # went red beside the code it wrote.  A herestring is one
            # pipe and no subshell, so committe's fd 0 is stdin exactly
            # as a heredoc would make it.
            committe -c <<< "$itera_test_output"
        fi
        status=$?
        if (( status != 0 )); then
            dic-error "itera-error: committe failed"
            return "$status"
        fi
        if itera-run-tests; then
            dic-info "itera: green after $round round(s)"
            return 0
        fi
        round=$((round + 1))
    done
    dic-error "itera-error: hit ITERA_MAX=$ITERA_MAX without green"
    return 1
}

# ---------------------------------------------------------------- record

# On success, squash the loop worktree and land it on the original branch
# as one fast-forward.  The rounds are a search -- round 2's commit is a
# fix for round 1's -- so the result and the first round's message are
# what is worth keeping; the later subjects name the bug fixes the first
# one already implies.
#
# The squash happens inside the loop worktree, before the original is
# touched: `reset --soft` moves the loop branch back to the run's start
# with the whole loop still in the index, and one commit puts it back as
# one.  A failure here leaves the original worktree exactly where it
# started, and the loop branch holding the unsquashed rounds.
#
# The oldest commit is the one to keep, and `rev-list --reverse` is how it
# is named: -n is applied before the reverse, so `log -1 --reverse` gives
# the newest instead.  An empty range is itera-continue's green tree with
# nothing committed, and there is then nothing to land.
#
# The landing is `merge --ff-only`, run in the original worktree so the
# branch's ref and its working tree move together.  It is the CAS: if the
# original branch moved while the loop ran, the fast-forward is refused
# before anything is touched, and the loop worktree is left standing for
# the user to merge by hand.  A merge, not a reset --hard, so nothing
# typed in the original worktree is ever discarded.
itera-record() {
    local first msg
    first=$(git -C "$ITERA_WORKTREE" rev-list --reverse \
        "$ITERA_ORIGINAL_HEAD..$ITERA_BRANCH" | head -1)
    if [[ -n $first ]]; then
        msg=$(git -C "$ITERA_WORKTREE" log -1 --format=%B "$first")
        if ! ( cd "$ITERA_WORKTREE" &&
               dic-run git reset --soft "$ITERA_ORIGINAL_HEAD" &&
               dic-run git commit --quiet -m "$msg" ); then
            dic-error "itera-error: could not squash the loop"
            dic-hint "itera-hint: the loop worktree is $ITERA_WORKTREE"
            return 1
        fi
    fi

    if ! ( cd "$ITERA_ORIGINAL_WORKTREE" &&
           dic-run git merge --ff-only "$ITERA_BRANCH" ); then
        dic-error "itera-error: could not fast-forward $ITERA_ORIGINAL_BRANCH"
        dic-hint "itera-hint: the loop worktree is $ITERA_WORKTREE"
        dic-hint "itera-hint: merge manually: git merge $ITERA_BRANCH"
        return 1
    fi

    # The landing is complete, so a failure in the cleanup below must not
    # roll it back.
    ITERA_LIVE=0

    dic-run git worktree remove --force "$ITERA_WORKTREE" || true
    git branch -D "$ITERA_BRANCH" >/dev/null 2>&1 || true
    rm -rf "$ITERA_RUNDIR" 2>/dev/null || true
    return 0
}

# -------------------------------------------------------------- rollback

# Set while a run's worktree exists; cleared by itera-record the moment
# its landing is complete and by itera-rollback.  One flag makes the EXIT
# trap and the explicit call in itera() mutually idempotent, and it is
# what keeps a failure in itera-record's own cleanup from rolling back a
# landed run.
ITERA_LIVE=0

# Not a step: an itera's loop runs in its own worktree, so the original
# worktree is never modified until itera-record's fast-forward lands.
# Rollback is therefore a print and nothing else -- nothing to undo -- and
# the interrupted work is in the loop worktree, where --continue resumes
# from it and where the user can look.
itera-rollback() {
    local status=$?
    (( ITERA_LIVE )) || return "$status"
    ITERA_LIVE=0

    dic-warn "itera-warning: aborted; $ITERA_ORIGINAL_WORKTREE was not modified"
    dic-hint "itera-hint: the loop worktree is $ITERA_WORKTREE"
    dic-hint "itera-hint: 'itera --continue' to keep going, 'itera --abort' to discard"
    return "$status"
}

# The tail every phase shares: land the run on success, roll it back on
# failure.  $1 is the run's own status, and it is what itera() returns.
itera-finish() {
    local status=$1
    if (( status == 0 )); then
        itera-record
        status=$?
    fi
    if (( status != 0 )); then
        itera-rollback
    fi
    return "$status"
}

# Run a function with the loop worktree as its cwd.  A subshell, because
# these scripts are sourced and a cd in the caller's shell would move the
# user, and itera is the only caller: the buffering that produced the
# request has already run by the time this is called, so nothing it
# reads through the subshell is lost.
itera-run-in-worktree() {
    ( cd "$ITERA_WORKTREE" && "$@" )
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
    dic-error 'itera-error: no test command; pass --test'
    return 1
}

# Run the test once through pv and return its status.  pv repaints one line
# on stderr and the test's own output lands in itera_test_output, then is
# read back only when the status is nonzero -- a green round says nothing,
# which is what a green round should say -- and left where the loop can
# hand it to the next round's committe.  The status is the test's own and
# not pv's, so it is read from PIPESTATUS before the substitution moves it.
itera-run-tests() {
    local status estimate start elapsed
    # A standalone call has no test_cmd from itera(); detect it here, so
    # `itera-run-tests` is the run a round makes and not an empty pipeline.
    if [[ -z ${test_cmd:-} ]]; then
        test_cmd=$(itera-detect-test) || return 1
    fi
    estimate=$(itera-estimate)
    local label='test'
    [[ -n $estimate ]] && label="test (expect ~${estimate}s)"
    start=$(date +%s.%N)
    # The test's output lands in itera_test_output rather than a temp
    # file: the bytes of a red round must survive to the next round's
    # committe -c, and a variable leaves no file behind to clean up.
    # dic-run traces the call, so the merge goes inside the string: a
    # 2>&1 on dic-run itself would capture the trace as well.  eval
    # runs in this shell, so a sourced function such as sandbox is
    # still a function.  The parens make 2>&1 apply to the whole
    # expression.  --meter folds pv in, so its meter is painted in the
    # info role instead of writing to stderr uncoloured, and dic-run
    # returns the test's own status, so nothing else has to preserve
    # it.  --as names the command for the trace: the eval and the 2>&1
    # that carry it out are the harness's mechanics and not the step,
    # so the trace reads as the command the caller would have typed
    # instead of the eval that runs it.
    itera_test_output=$(dic-run --meter "$label" \
        --as "$test_cmd" -- eval "($test_cmd) 2>&1")
    status=$?
    if (( status == 0 )); then
        # Time and record here, not in itera(), so a standalone run is
        # also a data point and there is only one place a run is timed.
        elapsed=$(awk -v s="$start" -v e="$(date +%s.%N)" \
            'BEGIN { printf "%.3f", e - s }')
        itera-record-time "$elapsed"
        return 0
    fi
    printf '%s' "$itera_test_output" | dic-paint muted
    # A last line with no newline of its own would run into the next
    # trace; the transcript rule puts every traced line at column 0.
    [[ -z $itera_test_output || $itera_test_output == *$'\n' ]] \
        || printf '\n' >&2
    return "$status"
}

# ----------------------------------------------------------------- entry

# itera's own flags are parsed first; the first `--` ends them, and
# everything after is passed to committe, which passes it on to dic.
itera() {
    ITERA_ALLOW_RED=0
    ITERA_LIVE=0
    ITERA_LIVE=0
    local mode=run
    local -a llm_args=()
    while (( $# )); do
        case $1 in
            --max)       ITERA_MAX=$2; shift 2 ;;
            --test)      ITERA_TEST=$2; shift 2 ;;
            --allow-red) ITERA_ALLOW_RED=1; shift ;;
            --)          shift; llm_args+=("$@"); break ;;
            *)           llm_args+=("$1"); shift ;;
        esac
    done
    ITERA_ARGS=("${llm_args[@]}")
    ITERA_REQUEST="${ITERA_ARGS[*]}"

    if (( ${#ITERA_ARGS[@]} == 0 )) && [[ -t 0 ]]; then
        dic-error 'itera-error: no request'
        return 64
    fi

    # Scratch for this run: one mktemp directory, removed when the run
    # ends.  Not under .git, and not state for a later invocation to
    # find: --continue and --abort are gone, so nothing here outlives the
    # run and a second itera in the same worktree has nothing to collide
    # with.  Absolute, because itera-run reads the buffer after a cd into
    # the loop worktree.
    local ITERA_TMP
    ITERA_TMP=$(mktemp -d "${TMPDIR:-/tmp}/geni-itera.XXXXXXXXXX") || return 1
    trap 'rm -rf "$ITERA_TMP"' EXIT

    # Buffer the request before the pre-flight test can read it.  Same
    # rationale as committe-prepare's buffering of its own stdin: the
    # bytes are read once here, so a test that reads fd 0 finds it at
    # EOF and round 1 hands the buffer to committe and not the drained
    # pipe.  Left empty on a tty-run, and itera-run's [[ -f ]] then
    # takes the branch that lets committe inherit fd 0.
    local ITERA_STDINFILE=
    if [[ ! -t 0 ]]; then
        ITERA_STDINFILE="$ITERA_TMP/stdin"
        cat > "$ITERA_STDINFILE" || { rm -rf "$ITERA_TMP"; return 1; }
    fi

    local status
    itera-validate || { status=$?; rm -rf "$ITERA_TMP"; return "$status"; }
    itera-prepare  || { status=$?; rm -rf "$ITERA_TMP"; return "$status"; }

    # The EXIT trap is the backstop: a signal, or a subshell abort in a
    # helper, takes the shell down and this still runs.  The explicit
    # call after itera-finish covers the sourced case, where the shell
    # never exits and EXIT never fires, and ITERA_LIVE makes the two
    # idempotent: only the run that reached itera-prepare rolls back.
    ITERA_LIVE=1
    trap 'rm -rf "$ITERA_TMP"; itera-rollback' EXIT

    itera-run-in-worktree itera-run
    status=$?
    itera-finish "$status"
    status=$?
    trap - EXIT
    rm -rf "$ITERA_TMP"
    return "$status"
}

# Run itera when sourced by a shell that called it, and leave the functions
# for a test that sourced this file to call.
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    itera "$@"
fi
