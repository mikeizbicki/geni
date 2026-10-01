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
# A failure leaves both the branch and the worktree behind, and renames
# both after the first commit geni made, so a leftover under .git/geni/
# reads as what it was trying to do instead of a bare token.
# `geni-clean` removes every leftover at once.
#
# Every argument and stdin are passed to itera, which passes them to
# committe, which passes them to dic.  geni has no flags of its own,
# so the parse-the-`--` rule from scripts/AGENTS.md does not apply
# yet; the pattern in committe and itera is what to copy when it does.

geni-token() {
    # A token unique enough that two invocations in the same second do
    # not collide, and short enough to read in a directory listing.
    # BASHPID separates the two halves of `geni a & geni b`, which a
    # bare $$ does not, and $RANDOM separates two shells started in
    # the same second.
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

geni-slug() {
    # A commit subject as one path-safe word: lowercased, runs of
    # non-alphanumerics collapsed to a dash, trimmed, truncated.
    printf '%s' "$1" | tr 'A-Z' 'a-z' |
        sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//' |
        cut -c1-40 |
        sed -E 's/-+$//'
}

geni-rename() {
    # Rename a failed run's branch and worktree after the first commit
    # geni made, so a leftover under .git/geni/ shows what it was
    # trying to do instead of only a token.  The token's random tail
    # is kept, so two runs that slugify to the same words stay
    # distinct.  A no-op -- no output -- when there is no [geni]
    # commit yet (a failure before the first round) or the subject
    # slugifies to nothing.  The caller must be at the repository top
    # level; prints the new worktree path on success.
    local old_branch=$1 old_dir=$2 common_dir=$3 base_sha=$4
    local subj slug tail new_branch new_dir
    subj=$(git -C "$old_dir" log --format=%s --reverse \
               "$base_sha"..HEAD 2>/dev/null | head -1)
    subj=${subj#\[geni\] }
    slug=$(geni-slug "$subj")
    [[ -n $slug ]] || return 0
    tail=${old_branch##*-}
    new_branch="geni/$slug-$tail"
    new_dir="$common_dir/geni/$slug-$tail"
    git branch -m "$old_branch" "$new_branch" || return 0
    git worktree move "$old_dir" "$new_dir" || return 0
    printf '%s\n' "$new_dir"
}

function geni() {
    local toplevel branch common_dir token wt_dir wt_branch start_pwd base_sha

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
    dic-git-clean geni || return 1

    # One token names both the branch and the directory, so the two
    # cannot drift apart, and .git/ puts the checkout on the same
    # filesystem as the repository and out of every walk of the tree.
    # It leads with the source branch, so a leftover is recognizable
    # before any commit exists to name it better.
    token=$(geni-token "$branch")
    wt_branch="geni/$token"
    common_dir=$(git rev-parse --path-format=absolute --git-common-dir)
    wt_dir="$common_dir/geni/$token"

    # The commit the worktree is built on.  `geni-rename` reads the
    # commits above it to name a failed run after what it was doing.
    base_sha=$(git rev-parse HEAD) || return 1

    if ! dic-run git worktree add --quiet -b "$wt_branch" "$wt_dir" HEAD; then
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
        cd "$toplevel" || true
        local renamed
        renamed=$(geni-rename "$wt_branch" "$wt_dir" "$common_dir" "$base_sha")
        if [[ -n $renamed ]]; then
            wt_dir=$renamed
            wt_branch="geni/${renamed##*/}"
        fi
        echo "geni-error: itera failed; staying in $wt_dir" >&2
        cd "$wt_dir" || true
        return 1
    fi

    # Success.  Go back to the tree the source branch is checked out
    # in and merge the generated branch into it.  --no-edit keeps a
    # true merge from opening an editor; a lone geni fast-forwards and
    # never consults it.
    cd "$toplevel" || return 1
    if ! dic-run git merge --no-edit "$wt_branch"; then
        local renamed
        renamed=$(geni-rename "$wt_branch" "$wt_dir" "$common_dir" "$base_sha")
        if [[ -n $renamed ]]; then
            wt_dir=$renamed
            wt_branch="geni/${renamed##*/}"
        fi
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

geni-clean() {
    # Remove every geni worktree and branch left over from a failure.
    # A success reaps its own worktree, so anything under .git/geni/ is
    # a failure someone chose not to finish.  Each worktree is removed
    # before its branch, because git refuses to delete a branch that is
    # still checked out somewhere.  A caller standing inside one of
    # these worktrees is the one case that fails, and it fails one
    # worktree at a time rather than all of them.
    local common_dir d name branch
    common_dir=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || {
        echo "geni-error: not inside a git repository" >&2
        return 1
    }
    for d in "$common_dir"/geni/*; do
        [[ -d $d ]] || continue
        name=${d##*/}
        branch="geni/$name"
        git worktree remove --force "$d" 2>/dev/null || {
            echo "geni-error: could not remove $d" >&2
            continue
        }
        git branch -D "$branch" >/dev/null 2>&1 || true
    done
    git worktree prune
}
