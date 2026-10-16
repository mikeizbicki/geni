# This file defines the minimal coding agent `committe`.
# It takes a request / conversation and generates a git commit.
# It is intended as a beginner-friendly intro to the "unix philosophy"
# and how AI coding agents work.
#
# committe is a four-step harness (see scripts/AGENTS.md):
#
#   committe-validate   pure: is this run possible at all
#   committe-prepare    fork a child session, record the parent's head
#   committe-run        call dic in the child session
#   committe-record     apply, commit, merge the child's ref into the parent
#
# and committe-fail, which is not a step: it prints the resume line.
#
# The fork is what makes a failure safe.  dic advances the *child's* ref on
# every call, so the parent's ref is untouched until committe-record moves
# it, with dic update-ref's OLD as the compare-and-swap.  A failed apply
# therefore leaves the parent exactly where it was, with nothing to clean
# up: the child is a leftover branch and not a corrupt parent.

# dic-run, dic-git-clean and dic-llm live in dic.sh, which sources
# this file in turn.  A caller that sources this file directly -- a
# test, say -- gets the helpers by sourcing dic.sh first; when dic.sh
# is the entry point, _DIC_LOADED is already set and this line does
# nothing, so dic.sh sourcing this file does not loop.
[[ -n ${_DIC_LOADED:-} ]] || source "${BASH_SOURCE[0]%/*}/dic.sh"

# ------------------------------------------------------------------- entry

function committe() {
    # exit code meaning:
    # 0  committed
    # 1  nothing could be applied
    #    (diff malformed and request should be repeated/manually fixed)
    # 2  the model asked a question instead of making a change
    #    (no diff provided)
    # 64 usage error (no request given, on a terminal)

    # committe's own flags are parsed first; the first `--` ends them and
    # everything after is passed to the model command.  A flag committe does
    # not know is passed through too, so a new dic/llm flag works unedited.
    local force=0 retries="${COMMITTE_RETRIES:-1}"
    local retry_mode="${COMMITTE_RETRY_MODE:-bytes}"
    local applier="${COMMITTE_APPLIER:-auto}"
    local dry_run=0
    local -a llm_args=()
    while (( $# )); do
        case "$1" in
            -f|--force)   force=1; shift ;;
            --retries)    retries=$2; shift 2 ;;
            --retry-mode) retry_mode=$2; shift 2 ;;
            --applier)    applier=$2; shift 2 ;;
            -n|--dry-run) dry_run=1; shift ;;
            --)           shift; llm_args+=("$@"); break ;;
            *)            llm_args+=("$1"); shift ;;
        esac
    done

    # The state the four steps share.  Bash has no lexical scope across
    # functions, so the steps read these rather than taking every one as
    # an argument; `local` keeps them invisible outside this call.
    local COMMITTE_FORCE=$force
    local COMMITTE_RETRIES=$retries
    local COMMITTE_RETRY_MODE=$retry_mode
    local COMMITTE_APPLIER=$applier
    local COMMITTE_DRY_RUN=$dry_run
    local -a COMMITTE_ARGS=("${llm_args[@]}")
    local COMMITTE_PARENT COMMITTE_CHILD

    committe-validate || return $?
    committe-prepare || return 1

    # A bytes retry resends the request and so gets a fresh sample; a
    # patch retry resends the same bytes to a less strict applier and
    # never re-calls the model.  The second lives inside
    # committe-record, because it is the apply and not the call that is
    # repeated.
    local attempt=0
    while :; do
        if ! committe-run; then
            committe-fail "the model call failed"
            return 1
        fi
        if ! committe-has-patch; then
            committe-question
            return 2
        fi
        if (( COMMITTE_DRY_RUN )); then
            dic-info "committe-info: dry run; the patch was not applied"
            dic-hint "committe-hint: the patch is at $(committe-patchfile)"
            return 0
        fi
        if committe-record; then
            return 0
        fi
        if [[ $COMMITTE_RETRY_MODE != bytes ]] \
                || (( attempt >= COMMITTE_RETRIES )); then
            break
        fi
        attempt=$((attempt + 1))
        dic-warn "committe-warning: retrying the request ($attempt/$COMMITTE_RETRIES)"
    done

    committe-fail "could not apply the patch"
    return 1
}

# ---------------------------------------------------------------- validate

function committe-validate() {
    # Pure: nothing is written and no model is called.  A run with no
    # request on a terminal is a usage error and not a failed call, so it
    # is 64 and not 1; a dirty tree is a failed run and is 1.
    if (( ${#COMMITTE_ARGS[@]} == 0 )) && [[ -t 0 ]]; then
        dic-error "committe-error: no request"
        return 64
    fi
    if (( ! COMMITTE_FORCE )); then
        dic-git-clean committe || return 1
    fi
    return 0
}

# ----------------------------------------------------------------- prepare

function committe-headfile() {
    # The mid the parent session pointed at when the child was forked.
    # committe-record passes it as update-ref's OLD, so a merge-back
    # refuses if the parent moved meanwhile -- the guarantee `git merge
    # --ff-only` gives, from the same place.
    echo "$(git rev-parse --git-dir)/geni/committe/head"
}

function committe-prepare() {
    # Fork the child session and record the fork point.  The child's ref
    # is a *copy* of the parent's and not a link, so -c, --mid HEAD and a
    # bare dic all resolve in the child exactly as they did in the
    # parent, and the parent's own ref is not touched at all.
    COMMITTE_PARENT="${DIC_SESSION:-global}"
    COMMITTE_CHILD="$COMMITTE_PARENT/committe-$(worktree-token "$COMMITTE_PARENT")"

    local headfile; headfile=$(committe-headfile)
    mkdir -p "$(dirname "$headfile")"

    if dic rev-parse "$COMMITTE_PARENT" > "$headfile" 2>/dev/null; then
        dic-run dic branch "$COMMITTE_CHILD" "$COMMITTE_PARENT" || return 1
    else
        # No parent ref yet: the child starts from nothing, and the
        # empty headfile is what tells the merge-back that the parent
        # must still be empty.
        : > "$headfile"
    fi

    export DIC_SESSION="$COMMITTE_CHILD"
    return 0
}

# --------------------------------------------------------------------- run

function committe-patchfile() {
    # Output the absolute path to the temporary file that will store the patch.
    # Everything goes under .git/geni/committe so it survives across
    # invocations and can be inspected when debugging a failed patch, and
    # it is worktree-local because each checkout has its own patch to apply.
    # `git rev-parse --git-dir` works even from subdirectories of the repo,
    # and any worktrees will automatically have different paths.
    echo "$(git rev-parse --git-dir)/geni/committe/patchfile"
}

function committe-message() {
    # The commit message: everything above the first "diff --git" line.
    sed -e '/^diff --git/,$d' -e '/./,$!d' "$(committe-patchfile)"
}

function committe-run() {
    # `dic` is a more efficient version of simonw's `llm` command, but
    # either satisfies the caller; dic.sh resolves which, so the choice
    # is made in one place.
    local llm_command
    llm_command=$(dic-llm) || return 1

    mkdir -p "$(dirname "$(committe-patchfile)")"

    # The user's request is the prompt, and the instructions that say what
    # to do with it are prepended above it, so the two are named apart and
    # neither can be read as the other's arguments.  Those instructions
    # belong in the user turn and not in the system prompt: the system
    # prompt is the head of every request dic makes, so rewriting it
    # throws away the cached prefix that -c exists to hit.
    #
    # The model is asked once per attempt and never retried inside this
    # function.  stdin is part of the request when it is not a terminal,
    # and a pipe reads once: a retry that inherited it would find EOF and
    # ask the model without the request it was sent.  The bytes retry in
    # committe() re-runs this function with the same arguments and the
    # same fd, which is why it works only when the request is a word and
    # not a pipe -- and why it resends the same request and not the same
    # response: a second sample of a prompt is a second patch.  A
    # provider that made the call deterministic would need `-o seed=N`,
    # at the cost of the prompt cache.
    dic-run "$llm_command" --prepend-prompt "$(committe-prompt)" \
        "${COMMITTE_ARGS[@]}" > "$(committe-patchfile)"
}

function committe-has-patch() {
    # Whether the model made a change at all, or only answered.
    grep -q '^diff --git ' "$(committe-patchfile)"
}

function committe-question() {
    # The model asked instead of changing anything: print its words in
    # the payload role, and merge the turn back anyway so that -c
    # continues from the question and the user can answer it.
    dic-info "committe-question: the model made no change; it asks:"
    sed 's/^#![[:space:]]*//' "$(committe-patchfile)" | dic-paint payload
    committe-merge 2>/dev/null || true
    dic-hint "committe-hint: continue with: committe -c '...'"
}

# ------------------------------------------------------------------ record

function committe-apply-patch() {
    # One traced step: the applier picks which tool is tried first, and
    # an `auto` fallback says so on stderr rather than in a second trace.
    # git-apply-fuzzy's per-hunk report belongs to the model and not to
    # the terminal, so it is quiet here.
    local patch_file=$1 applier=$2
    case $applier in
        exact)
            git apply --quiet --index --recount --ignore-whitespace "$patch_file"
            ;;
        fuzzy)
            git-apply-fuzzy -q "$patch_file"
            ;;
        *)
            if ! git apply --quiet --index --recount --ignore-whitespace "$patch_file" 2>/dev/null; then
                dic-warn "committe-warning: git apply failed; retrying with git-apply-fuzzy"
                git-apply-fuzzy -q "$patch_file"
            fi
            ;;
    esac
}

function committe-record() {
    # Apply, commit, and merge -- the only step that moves a ref.  The
    # applier escalation for --retry-mode patch lives here and not in
    # committe(), because it repeats the apply and never the call.
    local applier=$COMMITTE_APPLIER
    local attempt=0
    while :; do
        if dic-run committe-apply-patch "$(committe-patchfile)" "$applier"; then
            break
        fi
        if [[ $COMMITTE_RETRY_MODE != patch ]] \
                || (( attempt >= COMMITTE_RETRIES )); then
            return 1
        fi
        attempt=$((attempt + 1))
        applier=fuzzy
        dic-warn "committe-warning: retrying the apply ($attempt/$COMMITTE_RETRIES)"
    done

    # `git apply` staged the changes, so no separate `git add` is required.
    # We tag the commits by modifying the subject with [geni]
    # and setting the committer fields.
    local commit_message
    commit_message="[geni] $(committe-message)"
    if ! GIT_COMMITTER_NAME='committe' GIT_COMMITTER_EMAIL='committe@agent' \
         dic-run git commit --quiet -m "$commit_message"; then
        dic-error "committe-error: git commit failed"
        return 1
    fi

    committe-merge || return 1

    # Show a short summary of the commit we just made.
    git show HEAD --stat --format='%h %s'
}

function committe-merge() {
    # Move the parent's ref to where the child's is, with the fork point
    # as the compare-and-swap, and then drop the child.  This is the
    # merge and it is the whole of it: the child's mid is already in the
    # message tree, so there is no content to copy -- only the ref.
    local old new
    old=$(cat "$(committe-headfile)")
    new=$(dic rev-parse "$COMMITTE_CHILD") || {
        dic-error "committe-error: no turn in $COMMITTE_CHILD to merge"
        return 1
    }

    if [[ -z $old ]]; then
        # The parent had no ref at prepare time, so it must still have
        # none: something else started a conversation under this session
        # name while we were running if it does.
        if dic rev-parse "$COMMITTE_PARENT" >/dev/null 2>&1; then
            dic-error "committe-error: $COMMITTE_PARENT moved meanwhile"
            return 1
        fi
        dic-run dic update-ref "$COMMITTE_PARENT" "$new" || return 1
    else
        dic-run dic update-ref "$COMMITTE_PARENT" "$new" "$old" || return 1
    fi

    # The commit is reachable from the parent now, so the child's ref is
    # bookkeeping.  A failure to remove it is not a failure to merge.
    dic update-ref -d "$COMMITTE_CHILD" 2>/dev/null || true
}

# -------------------------------------------------------------------- fail

function committe-fail() {
    # Not a step: the resume line, and the one place the child's name is
    # printed.  Nothing is cleaned up here -- the parent's ref is already
    # where it was, and the child is a leftover branch the user may
    # resume or ignore.
    dic-error "committe-error: $1"
    if [[ -n ${COMMITTE_CHILD:-} ]]; then
        dic-hint "committe-hint: the turn is in session $COMMITTE_CHILD"
        dic-hint "committe-hint: resume it with: DIC_SESSION=$COMMITTE_CHILD dic -c '...'"
    fi
}

# ------------------------------------------------------------------ prompt


function committe-prompt() {
    # Print the instructions committe puts in front of the request, in the
    # user turn and not in the system prompt, so that a continued -c reads
    # the conversation's cache instead of rewriting its head.
    # It is a global function so that users can always run it to inspect the prompt.
    # All commands used in constructing the prompt must be side effect free.
    cat <<EOF
You are a coding agent.
The user describes a change they want made to a git repository.
You respond with:
1. a commit message (Tim Pope style)
2. a patch.
Here is an example:

\`\`\`
fix the foobar bug

diff --git a/path/to/file b/path/to/file
--- a/path/to/file
+++ b/path/to/file
@@ -<old_start>,<old_count> +<new_start>,<new_count> @@
 context line
-removed line
+added line
 context line
\`\`\`

Rules:
- No other content.
    - Do NOT wrap your response in markdown code fences.
    - Do NOT include any prose other than the commit message
- The commit message uses Tim pope style
    - imperative header (50 char max)
    - optional body explaining the changes
        - should be used only on complex patches
- Use standard unified diff syntax with '--- a/...' and '+++ b/...' headers.
    - For new files use '--- /dev/null' and '+++ b/path'.
    - You must also specify the mode of the new file
      (Add the text "new file mode 100644")
    - For deleted files use '--- a/path' and '+++ /dev/null'.
- The patch will be applied with \`git apply --recount\`
    - Hunk line numbers do not have to be exact,
      but the context lines must be recognizable in the current file.
    - Include 2-3 lines of unchanged context around each change.
    - These context lines must exactly match the original document.
      (Including whitespace, quotation marks, and other punctuation.)
- Prefer small, focused patches.
- State a structural change the way git states it, never as content.
    - To move a file, state the rename and no hunks.
      For example:
          diff --git a/old b/new
          similarity index 100%
          rename from old
          rename to new
      A move that also edits the file states those two rename lines and
      then the hunks, which are the change against the old contents.
    - To delete a file, state the mode and no hunks:
          diff --git a/old b/old
          deleted file mode 100644
    - To change a file's mode and nothing else:
          diff --git a/script b/script
          old mode 100644
          new mode 100755
    - A new file states its mode: 100644, or 100755 when it is executable.
    - A symlink is a new file of mode 120000 whose one added line is the
      path it points at.
- If the change cannot or should not be made yet -- the request is
  ambiguous, the tree does not support it, or you need a decision the user
  has not made -- write no patch and reply with your question alone.
    - It is printed and nothing is committed.

Use the following information to help you write the code:

$ git ls-files
$(git ls-files)

The request follows.
EOF
}
