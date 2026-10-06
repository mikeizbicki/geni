#!/bin/bash
#
# sandbox4.sh -- a thin wrapper around bwrap(1), sourced into the shell.
#
# Source this file and it defines one function, `sandbox`.  Every argument
# you give that function is an argument to bwrap(1):
#
#     source scripts/sandbox4.sh
#     sandbox -- make -j
#     sandbox --ro-bind /home/me/.gitconfig /home/me/.gitconfig -- git log
#     sandbox --bind /home/me/src /home/me/src -- make -C /home/me/src
#     sandbox --share-net -- cargo build
#     sandbox NO_COLOR=1 -- make -j
#
# bwrap applies its arguments from left to right and the last one wins, so
# the function lays down a strict default and you add back exactly the path
# you need.  The function has no flags of its own, so there is nothing here
# to learn that is not already bwrap(1).
#
# The one word the function reads for itself is a leading NAME=value, the
# form env(1) and sudo(8) also accept, and it reads it the way bash does:
# a run of assignments that ends at the first word that is not one.  Each
# becomes a --setenv of its own, laid down after the default environment,
# so NO_COLOR=1 reaches the payload and PATH=/x overrides the PATH the
# defaults would have passed through.
#
# The default is a jail that sees almost nothing.  The point is that the
# files worth stealing -- ~/.ssh, ~/.aws, ~/.config/gh, a database socket
# -- are not in the jail's map at all, instead of sitting in it behind a
# permission bit: a file that was never mounted cannot be read, so it
# cannot be exfiltrated by a model that reads what a tool printed here.
#
# A mount rule is not the whole wall, so the function installs one syscall
# rule as well.  io_uring does file and socket work in kernel context: it
# never calls open(2) or socket(2), so it walks around a filter that watches
# those calls.  bwrap cannot express that -- --seccomp wants a filter that is
# already compiled -- so the C file beside this one is the filter, and it is
# built into the cache the first time the function runs.  If it cannot be
# built, the function refuses to run anything, because a jail that quietly
# drops its filter is worse than no jail at all.
#
# Sourcing this file defines the functions and registers the completion at
# the end of it, and does nothing else.  It sets no shell option, exports
# no variable and runs no other command.

# Build the filter if the cache does not have it, and print the path to the
# compiled blob.  This is a cache, not state: the blob is named after a
# checksum of the C it came from, so an edited source is a different file
# and there is no staleness to reason about.  Nothing here runs at source
# time, and nothing here touches the calling shell.
#
# SANDBOX_SECCOMP_BLOB points at a blob built on some other machine, for
# when there is no compiler; CC names the compiler.  There is no way to ask
# for no filter, because installing one is what this function is for.
sandbox-seccomp-blob() {
    local src dir sum cache bin blob

    if [[ -n ${SANDBOX_SECCOMP_BLOB:-} ]]; then
        printf '%s\n' "$SANDBOX_SECCOMP_BLOB"
        return 0
    fi

    # BASH_SOURCE[0] inside a function names the file the function was
    # defined in, which is this one, whoever sourced it and from wherever.
    src=${BASH_SOURCE[0]}
    dir=${src%/*}
    [[ $dir == "$src" ]] && dir=.
    dir=$(cd -- "$dir" && pwd) || return 1
    src=$dir/sandbox-seccomp.c
    [[ -f $src ]] || {
        printf 'sandbox: %s: not found\n' "$src" >&2
        return 1
    }

    sum=$(cksum <"$src") || return 1
    sum=${sum// /-}
    cache=${XDG_CACHE_HOME:-$HOME/.cache}/geni
    bin=$cache/sandbox-seccomp-$sum
    blob=$bin.bpf

    if [[ ! -s $blob ]]; then
        mkdir -p "$cache" || return 1
        printf 'sandbox: building %s\n' "$blob" >&2
        ${CC:-cc} -std=gnu99 -O2 -Wall -Wextra -o "$bin.$$" "$src" ||
            return 1
        "$bin.$$" >"$blob.$$" || return 1
        mv -f "$bin.$$" "$bin" || return 1
        mv -f "$blob.$$" "$blob" || return 1
    fi

    printf '%s\n' "$blob"
}

# True when a word is a leading NAME=value assignment: a shell identifier,
# an equals sign, and the value.  A `--`, a command name and a bwrap option
# are none of those, so a walk that stops at the first word this rejects
# stops exactly where bash's own assignment prefix stops.  sandbox() and
# sandbox-complete() both walk with it, so completion can never offer a
# word the wrapper would not have peeled.
sandbox-assign() {
    [[ ${1:-} == *=* && ${1%%=*} =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]
}

sandbox() {
    # A leading NAME=value is read here and nowhere else.  Peel the run of
    # them off before anything else looks at the argument list, so what is
    # left is the same list bwrap would have been handed without them.
    local -a envs=()
    local name
    while (( $# )) && sandbox-assign "$1"; do
        name=${1%%=*}
        envs+=(--setenv "$name" "${1#*=}")
        shift
    done

    # `local -a` gives this call its own array, so two calls cannot collide
    # and a shell running `set -u` is unaffected.  A bash array passes its
    # elements to bwrap as separate words, so a path with a space in it is
    # still one argument and not two.
    local -a args=(

        # Kill the sandbox when the shell that started it goes away.
        # Without this a sandbox outlives the terminal that launched it and
        # keeps running whatever it was told to do.
        --die-with-parent

        # Give the sandbox a session of its own.  Otherwise a program
        # inside it can inject keystrokes into the parent terminal with
        # TIOCSTI and make the shell outside the jail type a command.
        --new-session

        # Each --unshare-* gives the payload a private copy of one kind of
        # kernel object.  Because the two sides cannot see each other there
        # is no shared object left to meet at, so the only way in or out is
        # a path mounted on purpose, below.
        #
        # user: inside this namespace the payload can be "root" for its own
        # housekeeping, but that identity does not exist outside it, so it
        # cannot touch a host process or file that was not mounted for it.
        --unshare-user
        # ipc: no SysV shared memory, semaphore or message queue shared with
        # a host process; /dev/shm is a fresh tmpfs further down.
        --unshare-ipc
        # pid: the payload sees only itself, so it cannot list, signal or
        # inspect a process outside.  bwrap is PID 1 of this namespace, and
        # that is also what reaps the payload's orphans.
        --unshare-pid
        # uts: it may call sethostname and nobody outside notices.
        --unshare-uts
        # cgroup: it cannot see the host's cgroup tree.
        --unshare-cgroup
        # net: a network stack with no route off the machine.  A server
        # started here is reachable by nobody and nothing here can dial out.
        # `--share-net` is how you ask for the host network instead.
        --unshare-net

        # Start the payload with no capabilities.  This is belt and braces
        # after --unshare-user: even as root of its own namespace it cannot
        # mount, load a module or set the clock.
        --cap-drop ALL
    )

    # Bind in the loader and the tools.  On a merged-usr system /bin, /sbin
    # and /lib* are symlinks into /usr, so they are recreated as symlinks
    # rather than bound: binding through a symlink puts the same bytes in
    # the map twice, in two different roles.
    [[ -e /usr ]] && args+=(--ro-bind /usr /usr)
    for p in /bin /sbin /lib /lib32 /lib64 /libx32; do
        if [[ -L $p ]]; then
            args+=(--symlink "$(readlink "$p")" "$p")
        elif [[ -e $p ]]; then
            args+=(--ro-bind "$p" "$p")
        fi
    done

    # Just enough of /etc that names resolve, hosts are known and TLS
    # certificates verify.  This list is not a blanket bind of /etc on
    # purpose: /etc/ssh, /etc/shadow and whatever credential a package left
    # there stay outside.  A program that needs one of them is told to
    # mount it by name, which is a decision the caller can see.
    for p in /etc/alternatives /etc/bash.bashrc /etc/ca-certificates \
             /etc/ca-certificates.conf /etc/fonts /etc/gai.conf \
             /etc/group /etc/host.conf /etc/hosts /etc/inputrc \
             /etc/ld.so.cache /etc/ld.so.conf /etc/ld.so.conf.d \
             /etc/localtime /etc/mime.types /etc/nsswitch.conf \
             /etc/os-release /etc/passwd /etc/pki /etc/profile \
             /etc/profile.d /etc/protocols /etc/resolv.conf \
             /etc/services /etc/ssl /etc/terminfo /etc/timezone; do
        [[ -e $p ]] && args+=(--ro-bind "$p" "$p")
    done

    # A fresh, minimal /proc and /dev.  The host's /proc names every process
    # outside and the host's /dev carries every device node, so both are
    # replaced rather than bound.  bwrap fills them with what a program
    # needs to start.
    args+=(--proc /proc --dev /dev)

    # The writable places a program assumes exist.  Each one is an empty
    # filesystem mounted over whatever the host had there, so writes go to
    # memory that dies with the sandbox and never reach a disk.  /run is
    # where a daemon would put a socket, /dev/shm where two processes would
    # share memory, and /tmp where anything leaves a scratch file.
    args+=(--tmpfs /tmp --tmpfs /var/tmp --tmpfs /run --tmpfs /dev/shm)

    # The one path that is writable, and it is the directory the shell is
    # already in.  bwrap creates the missing parents inside the jail, so
    # /home/me/proj appears on its own and the rest of /home/me does not.
    args+=(--bind "$PWD" "$PWD")

    # The interpreter can live outside $PWD: in a git worktree it sits in
    # the main worktree, and bwrap then cannot execvp the test runner
    # because that path was never mounted.  Bring in that one path
    # read-only, unless it is already inside $PWD and writable there.
    if [[ -n ${VIRTUAL_ENV:-} && -d $VIRTUAL_ENV ]]; then
        case $VIRTUAL_ENV/ in
            "$PWD"/*) ;;
            *) args+=(--ro-bind "$VIRTUAL_ENV" "$VIRTUAL_ENV") ;;
        esac
    fi

    # Empty the environment, then put back only what a program needs to find
    # itself.  An exported API key is not a variable the payload can read
    # here; --setenv K=V puts one back when a build actually needs it.
    args+=(--clearenv --setenv HOME /tmp)
    for p in PATH TERM LANG LC_ALL TZ; do
        [[ -v $p ]] && args+=(--setenv "$p" "${!p}")
    done

    # The assignments peeled off the front go here, after the default
    # environment above, because bwrap applies its --setenv words in order
    # and the last one for a name wins.  That is what lets a caller's
    # PATH=/x override the PATH passed through above and NO_COLOR=1 add a
    # name the defaults never set.
    (( ${#envs[@]} )) && args+=("${envs[@]}")

    # The caller's arguments come last, so they are the exceptions to
    # everything above.  The syscall filter goes before them and not after,
    # because the caller's arguments may end with a `--` and bwrap would
    # then read --seccomp as part of the command instead of as an option.
    #
    # bwrap takes a descriptor number for --seccomp and reads the compiled
    # filter from it, so the redirection below lends it fd 3 for the life of
    # the sandbox.  The subshell is why the terminal survives: `exec` then
    # replaces that subshell and not the shell which sourced this file.
    local blob
    blob=$(sandbox-seccomp-blob) || return 1

    ( exec bwrap "${args[@]}" --seccomp 3 "$@" 3<"$blob" )
}

# --- tab completion -----------------------------------------------------

# Complete the word after the assignments, the `--` and the command name as
# that command's own word, so `sandbox NO_COLOR=1 -- git ch<TAB>` completes
# a git subcommand and `sandbox -- make <TAB>` completes a make target.
# The walk is the one sandbox() itself makes, through the same
# sandbox-assign, so the two cannot disagree about where the command is.
#
# `=` is in COMP_WORDBREAKS, so bash hands `NO_COLOR=1` to a completion as
# the three words NO_COLOR, =, 1, and a walk over COMP_WORDS stops on the
# first of them.  `_init_completion -n =` is bash-completion's way of being
# handed the words with that break suppressed; it sets `words` and `cword`
# to the reassembled list.  Copying those back over COMP_WORDS is what puts
# the offset below in the coordinates _command_offset reads, since that
# function shifts COMP_WORDS itself and cannot be told about a second list.
#
# _command_offset N drops the first N words and calls whatever completion
# is registered for the word that is now first, which is how sudo(8) and
# env(1) complete their command.  With N at the word being completed there
# is no such word, and it falls back to completing a command name, which is
# what a caller typing the command wants.  It lives in bash-completion, so
# a shell without that file gets no completion here rather than a broken
# one.
sandbox-complete() {
    local cur prev words cword split
    local i

    _init_completion -n = || return

    # word 0 is `sandbox`; skip the assignments it consumes, then the `--`
    i=1
    while (( i < cword )) && sandbox-assign "${words[i]}"; do
        ((i++))
    done
    [[ ${words[i]:-} == -- ]] && ((i++))

    COMP_WORDS=("${words[@]}")
    COMP_CWORD=$cword
    _command_offset "$i"
}

if declare -F _command_offset >/dev/null; then
    complete -F sandbox-complete sandbox
fi
