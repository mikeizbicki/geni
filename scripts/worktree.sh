# This file defines `worktree`, which creates a git worktree branched
# from HEAD and cd's into it.  It is the workspace half of the loop
# `geni` runs: a place to edit and test before anything is merged back.
#
# The merge is the caller's -- a `git merge` in the branch this
# worktree was branched from -- so the edits under review stay visible
# in the window that started, and the work happens in the window that
# was opened.  The precondition is the same one geni checks: on a
# branch, clean tree.

# dic-run and dic-git-clean live in dic.sh, which sources this file in
# turn.  A caller that sources this file directly -- a test, say --
# gets the helpers by sourcing dic.sh first; when dic.sh is the entry
# point, _DIC_LOADED is already set and this line does nothing.
[[ -n ${_DIC_LOADED:-} ]] || source "${BASH_SOURCE[0]%/*}/dic.sh"

worktree-token() {
    # A token unique enough that two invocations in the same second do
    # not collide, and short enough to read in a directory listing.
    # BASHPID separates the two halves of `worktree a & worktree b`,
    # which a bare $$ does not, and $RANDOM separates two shells
    # started in the same second.
    #
    # It leads with the caller's source branch so a leftover is
    # recognizable before any commit exists to name it better.  The
    # branch is sanitized for both a ref and a path: a slash would
    # otherwise nest the worktree a level deeper under .git/geni/ and
    # split the branch across two path components.
    local base
    base=$(printf '%s' "${1:-anon}" | tr 'A-Z/' 'a-z-' | cut -c1-30)
    printf '%s-%s-%s\n' \
        "${base:-anon}" \
        "$(date -u +%Y%m%d%H%M%S)" \
        "${BASHPID:-$$}$RANDOM"
}

function worktree() {
    # Create a worktree branched from HEAD and cd into it.  With no
    # argument the source branch names it; with one, that name leads
    # the token.  The worktree lives under .git/geni/<token>, so it
    # is invisible to `git status` and to a walk of the working tree,
    # and it is on the same filesystem as the repository in the usual
    # case.
    #
    # The caller must be on a branch and the tree must be clean.  A
    # detached HEAD names no branch to merge back into, and
    # uncommitted changes are excluded from a worktree branched from
    # HEAD by construction, so either case is an error before the
    # worktree is created.
    local name=$1
    local branch common_dir token wt_dir wt_branch

    git rev-parse --show-toplevel >/dev/null 2>&1 || {
        echo "worktree-error: not inside a git repository" >&2
        return 1
    }
    branch=$(git symbolic-ref --quiet --short HEAD) || {
        echo "worktree-error: detached HEAD; check out a branch first" >&2
        return 1
    }
    dic-git-clean worktree || return 1

    token=$(worktree-token "${name:-$branch}")
    wt_branch="geni/$token"
    common_dir=$(git rev-parse --path-format=absolute --git-common-dir)
    wt_dir="$common_dir/geni/$token"

    if ! dic-run git worktree add --quiet -b "$wt_branch" "$wt_dir" HEAD; then
        echo "worktree-error: could not create worktree" >&2
        return 1
    fi

    cd "$wt_dir" || {
        echo "worktree-error: could not enter $wt_dir" >&2
        return 1
    }
}
