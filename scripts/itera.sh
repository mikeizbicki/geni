# itera: repeat until the tests pass.
#
# The loop is the shape: a pre-flight proves the tree starts green, a round
# asks committe for the fix, a re-run decides whether it worked, and the
# budget caps the asking.  committe is what talks to the model, so this file
# never reaches a provider itself.
#
#     itera [--max N] [--test CMD] [--allow-red] 'request'
#     itera --continue
#     itera --abort
#     itera --skip
#
# itera is a four-step harness (see scripts/AGENTS.md):
#
#   itera-validate   pure: is this run possible at all
#   itera-prepare    branch and record where the run started
#   itera-run        the round loop
#   itera-record     checkout the original and land the loop as one commit
#
# and itera-fail, which is not a step: it prints the resume line.
#
# The loop branch is what isolates a run.  HEAD moves to geni/itera-<token>
# at prepare, the rounds commit there, and itera-record moves HEAD back to
# the original and squashes them into one commit.  A failure leaves the
# user on the loop branch with the commits still there -- nothing is
# cleaned up on a failure path -- and --continue and --abort are how they
# carry on or get back.
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
# committe is called in the tree, so it commits each round; round 1 is handed
# itera's own stdin, which is why a heredoc into itera is a request and not
# a pipe.  A later round replaces that with the failing round's own output,
# because the request is already in the history --c replays.

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

# ---------------------------------------------------------------- state

# Where a run keeps the facts about itself.  Worktree-local and not
# repo-shared, because the original branch and the original head are facts
# about this checkout and not about the project.  One directory and not one
# per token: a second itera in the same worktree would be a second HEAD,
# which git does not allow, so the directory names the worktree.
itera-state-dir() {
    local dir
    dir=$(git rev-parse --git-dir 2>/dev/null) || return 1
    printf '%s\n' "$dir/geni/itera"
}

# The request on itera's own stdin, buffered once at entry.  itera's
# pre-flight test runs before committe reads fd 0, and a test that reads
# stdin -- or that hands it to a program which does -- would leave
# committe at EOF and the model with only its own instructions.  The
# buffer lives in the state directory --continue and --abort already
# read, and round 1 is the only round that reads it.
itera-stdinfile() {
    local dir
    dir=$(itera-state-dir) || return 1
    printf '%s\n' "$dir/stdin"
}

# Record what a later --continue or --abort needs.  One file per fact, so a
# half-written state is a missing file and not a malformed one.
itera-state-write() {
    mkdir -p "$ITERA_RUNDIR" || return 1
    printf '%s\n' "$ITERA_ORIGINAL_BRANCH" > "$ITERA_RUNDIR/original-branch"
    printf '%s\n' "$ITERA_ORIGINAL_HEAD" > "$ITERA_RUNDIR/original-head"
    printf '%s\n' "$ITERA_BRANCH" > "$ITERA_RUNDIR/branch"
    printf '%s\n' "$test_cmd" > "$ITERA_RUNDIR/test-cmd"
    printf '%s\n' "$ITERA_REQUEST" > "$ITERA_RUNDIR/request"
}

# Read what itera-prepare recorded, or refuse.  $1 names what the caller
# wanted to do, so the message says which command has nothing to work on.
itera-state-read() {
    ITERA_RUNDIR=$(itera-state-dir) || return 1
    local f
    for f in original-branch original-head branch test-cmd; do
        if [[ ! -f "$ITERA_RUNDIR/$f" ]]; then
            dic-error "itera-error: no itera state; nothing to $1"
            return 1
        fi
    done
    ITERA_ORIGINAL_BRANCH=$(cat "$ITERA_RUNDIR/original-branch")
    ITERA_ORIGINAL_HEAD=$(cat "$ITERA_RUNDIR/original-head")
    ITERA_BRANCH=$(cat "$ITERA_RUNDIR/branch")
    test_cmd=$(cat "$ITERA_RUNDIR/test-cmd")
    ITERA_REQUEST=$(cat "$ITERA_RUNDIR/request" 2>/dev/null || echo "")
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

# Branch and record.  The loop branch is what isolates a run: the original
# stays where it is, the rounds commit on the branch, and itera-record
# moves HEAD back and squashes the rounds.  Nothing is forked in
# DIC_SESSION -- each round's committe does that itself -- so round 1's -c
# reads the session the user was already in.
itera-prepare() {
    local branch
    branch=$(git symbolic-ref --quiet --short HEAD) || {
        dic-error 'itera-error: detached HEAD; check out a branch first'
        return 1
    }
    ITERA_ORIGINAL_BRANCH=$branch
    ITERA_ORIGINAL_HEAD=$(git rev-parse HEAD) || return 1
    ITERA_BRANCH="geni/itera-$(worktree-token "$branch")"
    ITERA_RUNDIR=$(itera-state-dir) || return 1

    itera-state-write || return 1

    dic-run git checkout --quiet -b "$ITERA_BRANCH" || {
        dic-error "itera-error: could not create branch $ITERA_BRANCH"
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

# On success, go back to the original branch and land the loop's work on
# it as one commit.  The rounds are a search -- round 2's commit is a fix
# for round 1's -- so the result and the first round's message are what is
# worth keeping; the later subjects name the bug fixes the first one
# already implies.
#
# `merge --squash` stages the branch as one change and makes no commit of
# its own, so the message is the one chosen here and not the "Squashed
# commit of the following:" git would have written.  It is also why there
# is no --no-edit: --squash never opens an editor.
#
# The oldest commit is the one to keep, and `rev-list --reverse` is how it
# is named: -n is applied before the reverse, so `log -1 --reverse` gives
# the newest instead.  An empty range is itera-continue's green tree with
# nothing committed, and there is then nothing to land.
#
# A conflicting squash leaves the index conflicted with no MERGE_HEAD, so
# `git merge --abort` cannot undo it; `git reset --hard` can.
itera-record() {
    dic-run git checkout --quiet "$ITERA_ORIGINAL_BRANCH" || return 1

    local first msg
    first=$(git rev-list --reverse \
        "$ITERA_ORIGINAL_HEAD..$ITERA_BRANCH" | head -1)
    if [[ -n $first ]]; then
        msg=$(git log -1 --format=%B "$first")
        if ! dic-run git merge --squash "$ITERA_BRANCH"; then
            dic-error "itera-error: squash into $ITERA_ORIGINAL_BRANCH failed"
            dic-hint "itera-hint: the loop branch is $ITERA_BRANCH"
            dic-hint "itera-hint: git reset --hard $ITERA_ORIGINAL_HEAD"
            return 1
        fi
        if ! dic-run git commit --quiet -m "$msg"; then
            dic-error "itera-error: could not commit the squashed loop"
            dic-hint "itera-hint: the loop branch is $ITERA_BRANCH"
            return 1
        fi
    fi

    # -D and not -d: the squashed commits are not ancestors of the new
    # commit, so git does not count the loop branch as merged.
    git branch -D "$ITERA_BRANCH" >/dev/null 2>&1 || true
    rm -rf "$ITERA_RUNDIR" 2>/dev/null || true
    return 0
}

# ------------------------------------------------------------------ fail

# Not a step: the resume line, and the one place the loop branch is named.
# Nothing is cleaned up here -- the loop branch already holds the work, and
# the user is standing on it.
itera-fail() {
    if [[ -n ${ITERA_BRANCH:-} ]]; then
        dic-hint "itera-hint: the loop branch is $ITERA_BRANCH"
        dic-hint "itera-hint: 'itera --continue' to keep going, 'itera --abort' to go back"
    fi
}

# ------------------------------------------------------ resume and abort

# Back to the original branch, with the loop branch left as it is.  A
# failure leaves the work on the loop branch and the user standing on it;
# this is how they get back without losing it.
itera-abort() {
    itera-state-read abort || return 1
    dic-run git checkout --quiet "$ITERA_ORIGINAL_BRANCH" || return 1
    dic-info "itera-info: back on $ITERA_ORIGINAL_BRANCH"
    dic-hint "itera-hint: the loop branch $ITERA_BRANCH is still there"
    dic-hint "itera-hint: remove it with: git branch -D $ITERA_BRANCH"
    return 0
}

# Re-enter the loop from where the last run stopped.  The tests are run
# once here, so a tree that has since gone green is recorded without asking
# the model anything; a tree that is still red enters the loop at round 2,
# which is the round that says `committe -c`.
itera-continue() {
    itera-state-read continue || return 1
    if itera-run-tests; then
        itera-record
        return $?
    fi
    if itera-run 2; then
        itera-record
        return $?
    fi
    itera-fail
    return 1
}

# Commit the working tree and continue.  A manual checkpoint: the user
# knows better than the tests whether the tree is worth saving, and this is
# how a round the model left half-done becomes a commit the next round can
# build on.
itera-skip() {
    itera-state-read skip || return 1
    dic-run git add -A || return 1
    if ! dic-run git commit --quiet -m '[geni] itera: manual checkpoint'; then
        dic-info "itera-info: nothing to commit"
    else
        dic-run git show --stat --format= HEAD
    fi
    if itera-run 2; then
        itera-record
        return $?
    fi
    itera-fail
    return 1
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
    local mode=run
    local -a llm_args=()
    while (( $# )); do
        case $1 in
            --max)       ITERA_MAX=$2; shift 2 ;;
            --test)      ITERA_TEST=$2; shift 2 ;;
            --allow-red) ITERA_ALLOW_RED=1; shift ;;
            --continue)  mode=continue; shift ;;
            --abort)     mode=abort; shift ;;
            --skip)      mode=skip; shift ;;
            --)          shift; llm_args+=("$@"); break ;;
            *)           llm_args+=("$1"); shift ;;
        esac
    done
    ITERA_ARGS=("${llm_args[@]}")
    ITERA_REQUEST="${ITERA_ARGS[*]}"

    case $mode in
        abort)    itera-abort;    return $? ;;
        continue) itera-continue; return $? ;;
        skip)     itera-skip;     return $? ;;
    esac

    if (( ${#ITERA_ARGS[@]} == 0 )) && [[ -t 0 ]]; then
        dic-error 'itera-error: no request'
        return 64
    fi

    # Buffer the request before the pre-flight test can read it.  Same
    # rationale as committe-prepare's buffering of its own stdin, and the
    # same shape: the bytes are read once here, so a test that reads fd 0
    # finds it at EOF and round 1 hands the buffer to committe and not
    # the drained pipe.  The file is removed first, so a tty-run that
    # follows a heredoc-run does not inherit the heredoc's bytes.
    local ITERA_STDINFILE
    ITERA_STDINFILE=$(itera-stdinfile) || return 1
    rm -f "$ITERA_STDINFILE"
    if [[ ! -t 0 ]]; then
        mkdir -p "$(dirname "$ITERA_STDINFILE")" || return 1
        cat > "$ITERA_STDINFILE" || return 1
    fi

    itera-validate || return $?
    itera-prepare  || return $?

    itera-run
    local status=$?
    if (( status == 0 )); then
        itera-record
        return $?
    fi
    itera-fail
    return "$status"
}

# Run itera when sourced by a shell that called it, and leave the functions
# for a test that sourced this file to call.
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    itera "$@"
fi
