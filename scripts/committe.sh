# This file defines the minimal coding agent `committe`.
# It takes a request / conversation and generates a git commit.
# It is intended as a beginner-friendly intro to the "unix philosophy"
# and how AI coding agents work.

function committe() {
    # exit code meaning:
    # 0 committed
    # 1 nothing could be applied
    #   (diff malformed and request should be repeated/manually fixed)
    # 2 the model asked a question instead of making a change
    #   (no diff provided)

    # committe's own flags are parsed first; the first `--` ends them and
    # everything after is passed to the model command.  A flag committe does
    # not know is passed through too, so a new dic/llm flag works unedited.
    local force=0 retries="${COMMITTE_RETRIES:-1}"
    local -a llm_args=()
    while (( $# )); do
        case "$1" in
            -f|--force) force=1; shift ;;
            --retries)  retries=$2; shift 2 ;;
            --)         shift; llm_args+=("$@"); break ;;
            *)          llm_args+=("$1"); shift ;;
        esac
    done

    # The prompt goes into the user turn in front of whatever the user wrote,
    # so an invocation with no request would send the instructions alone and
    # make a change nobody asked for.  A pipe on stdin is a request, and a
    # continuation such as -c carries one already.  This does not try to tell
    # a request from a flag's value, which would mean parsing dic's options
    # here, so it only catches the invocation with nothing at all.
    if (( ${#llm_args[@]} == 0 )) && [[ -t 0 ]]; then
        echo "committe-error: no request" >&2
        return 1
    fi

    # only allow committe to run if the repo is clean, unless -f was given
    if (( ! force )); then
        dic-git-clean committe || return 1
    fi

    # generate and apply the patch
    committe-mkpatch "${llm_args[@]}" || return $?
    committe-apply "$retries"
}

function committe-patchfile() {
    # Output the absolute path to the temporary file that will store the patch.
    # Everything goes under .git so it survives across invocations
    # and can be inspected when debugging a failed patch.
    # `git rev-parse --git-dir` works even from subdirectories of the repo,
    # and any worktrees will automatically have different paths.
    echo "$(git rev-parse --git-dir)/committe-patchfile"
}

function committe-message() {
    # The commit message: everything above the first "diff --git" line.
    sed -e '/^diff --git/,$d' -e '/./,$!d' "$(committe-patchfile)"
}

function committe-mkpatch() {
    # `dic` is a more efficient version of simonw's `llm` command, but
    # either satisfies the caller; dic.sh resolves which, so the choice
    # is made in one place.
    local llm_command
    llm_command=$(dic-llm) || return 1

    # We pass the user's request as positional args to llm_command,
    # preceded by the instructions that say what to do with it.  Those
    # instructions belong in the user turn and not in the system prompt:
    # the system prompt is the head of every request dic makes, so
    # rewriting it throws away the cached prefix that -c exists to hit.
    #
    # The model is asked once and never retried.  stdin is part of the
    # request when it is not a terminal, and a pipe reads once: a retry
    # that inherited it would find EOF and ask the model without the
    # request it was sent.  The caller's fd 0 is inherited here and goes
    # to $llm_command unchanged.
    if dic-run "$llm_command" "$(committe-prompt)" "$@" > "$(committe-patchfile)"; then
        return 0
    fi
    echo "committe-error: $llm_command failed" >&2
    return 1
}

function committe-apply() {
    local patch_file=$(committe-patchfile)

    # A reply with no patch at all is the model asking a question instead of
    # making a change, so there is nothing to apply and nothing to commit
    if ! grep -q '^diff --git ' "$patch_file"; then
        echo "committe-question: the model made no change; it asks:" >&2
        sed 's/^#![[:space:]]*//' "$patch_file" >&2
        echo "committe-hint: continue with: committe -c '...'" >&2
        return 2
    fi

    # We directly run `git apply` on the output of the llm.
    # `git apply` ignores any text before the first "diff --git" line,
    # so the commit message above the patch is skipped over.
    # Finally, it either fully succeeds or leaves the tree untouched.
    # So on error, the repo remains exactly as if nothing had happened.
    local i
    for (( i = 0; i <= retries; i++ )); do
        dic-run git apply --quiet --index --recount --ignore-whitespace "$patch_file" \
            && break

        # `git apply` needs every context line to match exactly, which the
        # model does not always manage; `git-apply-fuzzy` retries the patch
        # and tolerates small mismatches in the context lines.
        echo "committe-warning: git apply failed, retrying with git-apply-fuzzy" >&2
        dic-run git-apply-fuzzy -q "$patch_file" && break

        if (( i >= retries )); then
            echo "committe-error: git apply and git-apply-fuzzy both failed" >&2
            echo "committe-hint: fix the raw patch at: $patch_file" >&2
            echo "committe-hint: after fixing, rerun committe-apply" >&2
            return 1
        fi
        echo "committe-warning: retrying patch ($((i + 1))/$retries)" >&2
    done

    # `git apply` staged the changes, so no separate `git add` is required.
    # We tag the commits by modifying the subject with [geni]
    # and setting the committer fields.
    local commit_message
    commit_message="[geni] $(committe-message)"
    if ! GIT_COMMITTER_NAME='committe' GIT_COMMITTER_EMAIL='committe@agent' \
         dic-run git commit --quiet -m "$commit_message"; then
        echo "committe-error: git commit failed" >&2
        return 1
    fi

    # Show a short summary of the commit we just made.
    git show HEAD --stat --format='%h %s'
}

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
