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
    # argument the source branch names it; with one, that name is used
    # verbatim -- no timestamp, no random tail -- so a second caller
    # asking for the same name lands in the same worktree.  An
    # existing worktree of that name is entered and not recreated.
    # The worktree lives under .git/geni/<token>, so it is invisible
    # to `git status` and to a walk of the working tree, and it is on
    # the same filesystem as the repository in the usual case.
    #
    # Creating a worktree requires being on a branch and a clean tree:
    # a detached HEAD names no branch to merge back into, and
    # uncommitted changes are excluded from a worktree branched from
    # HEAD by construction.  Entering an existing one makes neither
    # requirement, so the checks run only on the create path.
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

    if [[ -n $name ]]; then
        token=$name
    else
        token=$(worktree-token "$branch")
    fi
    wt_branch="geni/$token"
    common_dir=$(git rev-parse --path-format=absolute --git-common-dir)
    wt_dir="$common_dir/geni/$token"

    # An existing worktree of this name -- a second terminal got there
    # first -- is entered rather than refused.  `git worktree list`
    # and not `[[ -d ]]` decides, so a stray directory under
    # .git/geni/ is not mistaken for one.
    if git worktree list --porcelain | grep -qxF "worktree $wt_dir"; then
        cd "$wt_dir" || {
            echo "worktree-error: could not enter $wt_dir" >&2
            return 1
        }
        worktree-prompt "$token"
        return 0
    fi

    dic-git-clean worktree || return 1

    if ! dic-run git worktree add --quiet -b "$wt_branch" "$wt_dir" HEAD; then
        echo "worktree-error: could not create worktree" >&2
        return 1
    fi

    cd "$wt_dir" || {
        echo "worktree-error: could not enter $wt_dir" >&2
        return 1
    }
    worktree-prompt "$token"
}

worktree-prompt() {
    # Name the worktree in the prompt: the token in purple, then a
    # green $.  A shell variable and not a substring test on $PS1:
    # kitty wraps $PS1 in its own escape sequences on the way to the
    # prompt, so a prefix placed at the front of the string leaves the
    # front, a substring test then fails, and the prefix is added a
    # second time on every prompt -- which is the [launch] bug this
    # also closes.  _LAUNCH_MARK is what tells launch.sh's
    # launch-prompt, which launch-init leaves appended to
    # PROMPT_COMMAND and which runs again before every prompt, that
    # its own prefix is already accounted for.
    _LAUNCH_MARK=1
    PS1='\[\e[0;35m\]['"${1:-}"']\[\e[0m\] \[\e[0;32m\]$\[\e[0m\] '
}
