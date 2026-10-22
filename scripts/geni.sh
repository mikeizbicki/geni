# geni.sh -- run a JSON plan as a series of itera loops.
#
# geni takes a plan file that conforms to scripts/plan.jsonschema and
# runs each of the plan's steps as its own itera loop, inside a fresh
# git worktree branched from HEAD and named for the plan.  When every
# step has landed the per-step commits are squashed into one commit
# whose message dic writes.
#
# geni does not merge back.  A successful run leaves the caller inside
# the worktree with the plan's branch checked out, so the result can
# be reviewed before deciding to merge; a failed run leaves the caller
# in the same place, with the failing step's loop branch intact, and
# itera's --continue in that worktree is the way to carry on by hand.
#
# Each step's itera is a fresh conversation.  geni never passes -c, so
# a step cannot read the conversation of a step before it.  What
# replaces that context is the prompt: the whole plan (the frame that
# gives every step its meaning), the files the step names, accio'd,
# and the diff -- `git diff $GENI_BASE_SHA` -- of every commit made
# since the plan started.  The diff, and not the log of
# conversations, is what makes a step self-contained: it reads what
# was actually done to the tree by the steps before it.
#
# The four-step harness pattern (see scripts/AGENTS.md):
#
#   geni-validate   pure: is this plan runnable in this tree
#   geni-prepare    create the worktree and cd into it
#   geni-run        one itera per step, from a prompt geni builds
#   geni-record     squash the steps and let dic write the message
#
# and geni-fail, which is not a step: it prints the resume line.

[[ -n ${_DIC_LOADED:-} ]] || source "${BASH_SOURCE[0]%/*}/dic.sh"

# ------------------------------------------------------------------ entry

function geni() {
    # exit codes:
    # 0  every step done, the plan committed
    # 1  a step failed, or the squash could not be made
    # 64 usage error

    local plan_file=
    local -a itera_args=()
    while (( $# )); do
        case $1 in
            --test|--max)
                itera_args+=("$1" "$2"); shift 2 ;;
            --allow-red)
                itera_args+=("$1"); shift ;;
            --continue|--abort|--skip)
                dic-error "geni-error: $1 does not apply to geni; every step is a fresh itera"
                return 64 ;;
            --)
                shift; itera_args+=("$@"); break ;;
            -*)
                itera_args+=("$1"); shift ;;
            *)
                if [[ -z $plan_file ]]; then
                    plan_file=$1
                else
                    dic-error "geni-error: unexpected argument: $1"
                    return 64
                fi
                shift ;;
        esac
    done

    # The state the four steps share.  Bash has no lexical scope across
    # functions, so the steps read these; `local` keeps them invisible
    # outside this call -- and the worktree path and the session name
    # are the two a resumed-by-hand run needs, which geni-fail prints.
    local GENI_PLAN_FILE=
    if [[ -n $plan_file ]]; then
        GENI_PLAN_FILE=$(realpath -- "$plan_file") || {
            dic-error "geni-error: $plan_file: cannot resolve"
            return 1
        }
    fi
    local -a GENI_ITERA_ARGS=("${itera_args[@]}")
    local GENI_NAME GENI_NSTEPS
    local GENI_WT_DIR GENI_WT_BRANCH GENI_BASE_SHA
    local GENI_SESSION
    local GENI_FAILED_STEP=

    geni-validate || return $?
    geni-prepare  || return 1

    if ! geni-run; then
        geni-cost-summary
        geni-fail
        return 1
    fi

    if ! geni-record; then
        geni-cost-summary
        geni-fail
        return 1
    fi

    geni-cost-summary
    dic-info "geni-info: done; worktree is $GENI_WT_DIR"
    dic-hint "geni-hint: review it, then merge with: git merge $GENI_WT_BRANCH"
    return 0
}

# ---------------------------------------------------------------- validate

function geni-validate() {
    # Pure: nothing is written and no model is called.  Everything that
    # makes this plan unrunnable -- a missing file, a name that is not a
    # name, a worktree that is already there -- is caught here, before a
    # directory is created or a token is spent.
    command -v jq    >/dev/null 2>&1 || { dic-error "geni-error: jq is not installed";    return 1; }
    command -v accio >/dev/null 2>&1 || { dic-error "geni-error: accio is not installed"; return 1; }

    if [[ -z $GENI_PLAN_FILE ]]; then
        dic-error "geni-error: no plan file; usage: geni [ITERA_FLAGS] PLAN.json"
        return 64
    fi
    if [[ ! -r $GENI_PLAN_FILE ]]; then
        dic-error "geni-error: $GENI_PLAN_FILE: cannot read"
        return 1
    fi
    if ! jq -e . "$GENI_PLAN_FILE" >/dev/null 2>&1; then
        dic-error "geni-error: $GENI_PLAN_FILE: not valid JSON"
        return 1
    fi

    GENI_NAME=$(jq -r '.name // empty' "$GENI_PLAN_FILE")
    if [[ -z $GENI_NAME ]]; then
        dic-error "geni-error: plan has no name"
        return 1
    fi
    if [[ $GENI_NAME == */* ]]; then
        dic-error "geni-error: plan name must not contain a slash: $GENI_NAME"
        return 1
    fi

    GENI_NSTEPS=$(jq -r '.steps | length' "$GENI_PLAN_FILE")
    if (( GENI_NSTEPS < 1 )); then
        dic-error "geni-error: plan has no steps"
        return 1
    fi

    git rev-parse --show-toplevel >/dev/null 2>&1 || {
        dic-error "geni-error: not inside a git repository"
        return 1
    }
    git symbolic-ref --quiet --short HEAD >/dev/null || {
        dic-error "geni-error: detached HEAD; check out a branch first"
        return 1
    }
    dic-git-clean geni || return 1

    # The worktree name is the plan's name, so a second run of the same
    # plan would collide.  Refuse rather than enter: geni is one-shot,
    # and a state that is halfway through is finished by hand in its
    # worktree, not by re-running geni over it.
    local common_dir
    common_dir=$(git rev-parse --path-format=absolute --git-common-dir) || return 1
    GENI_WT_DIR="$common_dir/geni/$GENI_NAME"
    if git worktree list --porcelain | grep -qxF "worktree $GENI_WT_DIR"; then
        dic-error "geni-error: worktree already exists: $GENI_WT_DIR"
        dic-hint "geni-hint: finish it there, or remove it: git worktree remove --force $GENI_WT_DIR"
        return 1
    fi
    if [[ -e $GENI_WT_DIR ]]; then
        dic-error "geni-error: path already exists: $GENI_WT_DIR"
        return 1
    fi
    return 0
}

# ----------------------------------------------------------------- prepare

function geni-prepare() {
    GENI_BASE_SHA=$(git rev-parse HEAD) || return 1
    GENI_WT_BRANCH="geni/$GENI_NAME"

    # The session name nests under the caller's, so --cost-session on
    # the caller sums this geni's spend with everything else beneath
    # it, and each itera forks its own child one level deeper.  Set on
    # the itera call and never exported: an export would replace the
    # caller's session for the rest of the shell.
    GENI_SESSION="${DIC_SESSION:-global}/geni-$GENI_NAME"

    if ! dic-run git worktree add --quiet -b "$GENI_WT_BRANCH" "$GENI_WT_DIR" HEAD; then
        dic-error "geni-error: could not create worktree"
        return 1
    fi

    cd "$GENI_WT_DIR" || {
        dic-error "geni-error: could not enter $GENI_WT_DIR"
        return 1
    }
    dic-info "geni-info: worktree $GENI_WT_DIR (branch $GENI_WT_BRANCH)"
    return 0
}

# ------------------------------------------------------------------- run

function geni-step-prompt() {
    # Build the prompt one step is handed, on stdout, so the caller
    # can route it to a file and pass that file as the call's stdin.
    # A file and not an argument: the plan, the files and the diff do
    # not fit in argv on a real project, and a file survives a crash.
    #
    # The plan is the frame: goal, acceptance criteria and sibling
    # steps tell the step why it exists and what it is one of.  The
    # step's own description is repeated last, so it is what is nearest
    # to the request and not lost below a page of diff.
    #
    # A file the step names but that is not in the tree yet -- a file
    # the step itself is to create, or the output of a step before it
    # that git reset has not yet landed -- is skipped rather than
    # passed to accio, because a step cannot be handed a context it
    # was going to write.
    local i=$1
    local step_id step_desc
    step_id=$(jq -r --argjson i "$i" '.steps[$i].id // empty'          "$GENI_PLAN_FILE")
    step_desc=$(jq -r --argjson i "$i" '.steps[$i].description // empty' "$GENI_PLAN_FILE")

    local -a existing=()
    local f
    while IFS= read -r f; do
        [[ -n $f && -e $f ]] && existing+=("$f")
    done < <(jq -r --argjson i "$i" '.steps[$i].files[]' "$GENI_PLAN_FILE")

    printf 'You are executing step %d of %d of a multi-step plan.\n\n' \
        "$((i + 1))" "$GENI_NSTEPS"
    printf 'Here is the plan:\n\n'
    cat "$GENI_PLAN_FILE"
    printf '\n\n'
    if (( ${#existing[@]} )); then
        printf 'The following files are relevant to this step:\n\n'
        accio "${existing[@]}"
        printf '\n'
    fi
    printf 'Every change made by the steps completed so far, as a diff\n'
    printf 'from the point the plan started:\n\n'
    git diff "$GENI_BASE_SHA"
    printf '\n'
    printf 'Your task, step %d:\n\n' "$((i + 1))"
    printf 'id: %s\n'          "$step_id"
    printf 'description: %s\n' "$step_desc"
}

function geni-run() {
    # One itera per step, each in its own conversation.  The prompt is
    # written to a file and fed as stdin, because it is a page of diff
    # and not an argv-sized string; a herestring would work too, at the
    # cost of holding the whole prompt in the shell's memory.
    #
    # geni's own flags -- --test, --max, --allow-red -- are passed to
    # every step unchanged: they describe the tree the plan runs in and
    # not one step of it.  ITERA_MAX's default of 5 therefore applies
    # to each step, and not to the plan: a step that cannot reach green
    # in five rounds is a step to fix by hand, not a plan to abandon.
    local i step_id
    for (( i = 0; i < GENI_NSTEPS; i++ )); do
        step_id=$(jq -r --argjson i "$i" '.steps[$i].id // empty' "$GENI_PLAN_FILE")
        dic-info "geni-info: step $((i + 1))/$GENI_NSTEPS: $step_id"

        local prompt_file
        prompt_file=$(mktemp -t geni-step.XXXXXX) || return 1

        if ! geni-step-prompt "$i" > "$prompt_file"; then
            dic-error "geni-error: could not build the prompt for step $((i + 1))"
            rm -f "$prompt_file"
            GENI_FAILED_STEP=$((i + 1))
            return 1
        fi

        local status
        DIC_SESSION="$GENI_SESSION" \
            itera "${GENI_ITERA_ARGS[@]}" < "$prompt_file"
        status=$?
        rm -f "$prompt_file"

        if (( status != 0 )); then
            GENI_FAILED_STEP=$((i + 1))
            return 1
        fi
    done
    return 0
}

# ---------------------------------------------------------------- record

function geni-squash-prompt() {
    # The instructions for the message-writing call: what shape the
    # reply must take, and nothing else, because everything the model
    # reads -- plan, commit messages, diff -- is what its stdin
    # carries and the two are named apart so neither reads as an
    # argument to the other.
    cat <<'EOF'
You are the author of one git commit that represents a whole plan.
You will be shown the plan, the messages of the commits that
implemented it, and the combined diff of everything those commits
changed.

Reply with only a commit message, in Tim Pope style:
- an imperative header, 50 characters or fewer
- an optional body, used only for a complex change
- no markdown code fences
- no prose other than the message itself
EOF
}

function geni-record() {
    # Squash the per-step commits into one, with a message dic writes
    # from the plan and the steps.  The message is fetched before the
    # tree is touched: a dic that fails leaves the per-step commits on
    # the branch, where they can be read and the loop resumed, instead
    # of an index full of changes and no commit to explain them.
    local log diff msg
    log=$(git log --format='%B' "$GENI_BASE_SHA..HEAD") || return 1
    diff=$(git diff "$GENI_BASE_SHA")                    || return 1

    local body_file
    body_file=$(mktemp -t geni-squash.XXXXXX) || return 1
    {
        printf 'Plan:\n\n'
        cat "$GENI_PLAN_FILE"
        printf '\n\nCommits made by the steps:\n\n%s\n\n' "$log"
        printf 'The combined diff:\n\n%s\n' "$diff"
    } > "$body_file"

    msg=$(DIC_SESSION="$GENI_SESSION" dic \
        --prepend-prompt "$(geni-squash-prompt)" < "$body_file")
    local status=$?
    rm -f "$body_file"

    if (( status != 0 )); then
        dic-error "geni-error: dic failed to write the commit message"
        dic-hint "geni-hint: the step commits are still on $GENI_WT_BRANCH"
        return 1
    fi

    # A model that wrapped the message in a code fence and nothing
    # else has obeyed the rule's letter and not its spirit; strip the
    # fences and any leading blank lines so the message is the message.
    # The two seds are "delete fence lines" then "drop the blank lines
    # above the first non-blank".
    msg=$(printf '%s' "$msg" | sed -e '/^```/d' -e '/./,$!d')
    if [[ -z $msg ]]; then
        dic-error "geni-error: dic returned an empty message"
        dic-hint "geni-hint: the step commits are still on $GENI_WT_BRANCH"
        return 1
    fi

    # reset --soft and not a rebase: it is the one git verb that moves
    # HEAD alone and keeps the index, so the whole plan becomes a stage
    # on top of $GENI_BASE_SHA and the commit below writes it back as
    # one commit whose parent is the plan's base.
    dic-run git reset --soft "$GENI_BASE_SHA" || {
        dic-error "geni-error: could not reset to $GENI_BASE_SHA"
        dic-hint "geni-hint: the step commits are still on $GENI_WT_BRANCH"
        return 1
    }

    if ! GIT_COMMITTER_NAME='geni' GIT_COMMITTER_EMAIL='geni@agent' \
         dic-run git commit --quiet -m "$msg"; then
        dic-error "geni-error: git commit failed; the changes are staged"
        dic-hint "geni-hint: commit them yourself with: git commit"
        return 1
    fi
    dic-run git show --stat --format= HEAD
    return 0
}

# ------------------------------------------------------------------ cost

function geni-cost-summary() {
    # One query over the subtree geni set as the session on every itera
    # call.  Each itera forks a child under it and each committe forks
    # one under that, so --tree is the whole run.  Written to stderr in
    # the cost role, because it is the run's closing fact and not part
    # of the tree the run produced.
    [[ -n ${GENI_SESSION:-} ]] || return 0
    dic cost --session "$GENI_SESSION" --tree | dic-paint cost
    return 0
}

# ------------------------------------------------------------------ fail

function geni-fail() {
    # Not a step: the resume line, and the one place the worktree path
    # is printed.  Nothing is cleaned up -- the worktree and its branch
    # are still there, with the steps that completed committed -- and
    # the way forward is itera in that worktree, which is the loop geni
    # already left running.
    if [[ -n ${GENI_FAILED_STEP:-} ]]; then
        local step_id=
        step_id=$(jq -r --argjson i "$((GENI_FAILED_STEP - 1))" \
                    '.steps[$i].id // empty' "$GENI_PLAN_FILE" 2>/dev/null)
        dic-error "geni-error: step $GENI_FAILED_STEP/$GENI_NSTEPS failed${step_id:+ ($step_id)}"
    fi
    if [[ -n ${GENI_WT_DIR:-} ]]; then
        dic-hint "geni-hint: the worktree is $GENI_WT_DIR"
        dic-hint "geni-hint: cd there and run 'itera --continue' to carry on"
        dic-hint "geni-hint: 'itera --abort' goes back to the branch the plan started from"
    fi
}
