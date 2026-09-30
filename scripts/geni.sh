# This file defines `geni`, the isolation wrapper around `itera`.
#
# `itera` runs the committe / test loop, but it runs in whatever tree
# the caller was in.  `geni` runs it somewhere else: it creates a fresh
# git worktree branched from HEAD, cd's into it, runs `itera` there,
# and -- if the loop succeeds -- merges that branch back into the
# branch the caller started on and removes the worktree.  A failure
# leaves the caller inside the worktree, with the branch it was
# building still checked out, so the problem can be fixed in the same
# checkout the loop was using.
#
# The worktree lives under .git/geni/<token>, so it is invisible to
# `git status` and to a walk of the working tree, and it is on the
# same filesystem as the repository in the usual case.  The token is
# unique per invocation, so two geni's in the same repo run in
# parallel without colliding, and the merge order between them is
# nondeterministic on purpose: their worktrees are independent and the
# only serialization is whatever git does to the branch they land on.
#
# The caller must be on a branch and the tree must be clean.  A
# detached HEAD names no branch to merge into, and uncommitted changes
# are excluded from a worktree branched from HEAD by construction, so
# either case is an error before the worktree is created.
#
# Every argument and stdin are passed to itera, which passes them to
# committe, which passes them to dic.  geni has no flags of its own,
# so the parse-the-`--` rule from scripts/AGENTS.md does not apply
# yet; the pattern in committe and itera is what to copy when it does.

geni-token() {
    # A short token unique enough that two invocations in the same
    # second do not collide, and short enough to read in a directory
    # listing.  BASHPID separates the two halves of `geni a & geni b`,
    # which a bare $$ does not, and $RANDOM separates two shells
    # started in the same second.
    printf '%s-%s-%s\n' \
        "$(date -u +%Y%m%d%H%M%S)" \
        "${BASHPID:-$$}" \
        "$RANDOM$RANDOM"
}

function geni() {
    local toplevel branch common_dir token wt_dir wt_branch start_pwd

    # We must be in a git repository on a branch: the work is merged
    # back into the branch we start on, and a detached HEAD has no
    # name to merge into.
    toplevel=$(git rev-parse --show-toplevel 2>/dev/null) || {
        echo "geni-error: not inside a git repository" >&2
        return 1
    }
    branch=$(git symbolic-ref --quiet --short HEAD) || {
        echo "geni-error: detached HEAD; check out a branch first" >&2
        return 1
    }

    # The worktree starts from HEAD, so anything uncommitted in the
    # caller's tree would not be part of it.  `committe` would refuse
    # the same tree a moment later, so the check is repeated here to
    # keep a geni that cannot run from leaving a worktree behind.
    if ! git diff --quiet; then
        echo "geni-error: working tree has uncommitted changes" >&2
        return 1
    fi
    if ! git diff --quiet --cached; then
        echo "geni-error: staging area is non-empty" >&2
        return 1
    fi

    # One token names both the branch and the directory, so the two
    # cannot drift apart, and .git/ puts the checkout on the same
    # filesystem as the repository and out of every walk of the tree.
    token=$(geni-token)
    wt_branch="geni/$token"
    common_dir=$(git rev-parse --path-format=absolute --git-common-dir)
    wt_dir="$common_dir/geni/$token"

    if ! git worktree add --quiet -b "$wt_branch" "$wt_dir" HEAD; then
        echo "geni-error: could not create worktree" >&2
        return 1
    fi

    start_pwd=$PWD
    cd "$wt_dir" || {
        echo "geni-error: could not enter $wt_dir" >&2
        return 1
    }

    # Nest the dic session under the caller's, so `--cost-session` on
    # the caller sums this geni's spend with everything else beneath
    # it, and dic's subagent naming applies without a new mechanism.
    # The prefix is set for the one command and never for the caller,
    # whose DIC_SESSION is the one it already had.
    local session="${DIC_SESSION:-global}/geni-$token"

    # Run the loop here.  A failure -- a failed round, a committe that
    # could not apply, a ^C, anything nonzero -- leaves the caller in
    # the worktree so that the checkout the loop was using is still
    # open and `itera` can be resumed there.
    if ! DIC_SESSION="$session" itera "$@"; then
        echo "geni-error: itera failed; staying in $wt_dir" >&2
        return 1
    fi

    # Success.  Go back to the tree the source branch is checked out
    # in and merge the generated branch into it.  --no-edit keeps a
    # true merge from opening an editor; a lone geni fast-forwards and
    # never consults it.
    cd "$toplevel" || return 1
    if ! git merge --no-edit "$wt_branch"; then
        echo "geni-error: merge into $branch failed; branch $wt_branch is in $wt_dir" >&2
        cd "$wt_dir" || true
        return 1
    fi

    # The commits are reachable from the branch we are on, so the
    # worktree and the branch are bookkeeping now and not data.
    git worktree remove --force "$wt_dir" || true
    git branch -D "$wt_branch" >/dev/null 2>&1 || true

    cd "$start_pwd" || return 1
}
