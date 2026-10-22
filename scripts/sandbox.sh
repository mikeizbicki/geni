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
#     sandbox --envs=GH_TOKEN --share-net -- gh-mcp
#     sandbox NO_COLOR=1 -- make -j
#
# bwrap applies its arguments from left to right and the last one wins, so
# the function lays down a strict default and you add back exactly the path
# you need.  Every argument is a bwrap(1) argument but the leading --envs
# and NAME=value, which the function reads for itself and which are the
# only words here that are not bwrap's own.
#
# The words the function reads for itself are a leading --envs=NAME[,NAME],
# or `--envs NAME` repeated, and a leading NAME=value -- the form env(1)
# and sudo(8) also accept.  It reads the run of them the way bash reads a
# run: the run ends at the first word that is neither.  --envs names a
# variable of the shell that called the function and NAME=value gives the
# value outright; either way the value is written to a file -- not to a
# --setenv, whose value is a word of its own, and a word is what `ps`
# prints -- and handed to bwrap on fd 9.  The file is unlinked before bwrap
# starts, so a secret typed here is not a secret left in the process table
# for as long as the sandbox runs: what the process table shows is a
# descriptor and a name and nothing else.  A name the calling shell does
# not have is an error; a name it has set to the empty string is a value,
# and it travels.
#
# Nothing the calling shell exported reaches the payload.  The payload's
# own environment is emptied by --clearenv, and bwrap's -- the one
# /proc/1/environ shows from inside, because bwrap is PID 1 of the
# namespace the jail mounts /proc in -- is emptied at the exec that
# starts it, so --envs is the one route a variable has in.
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
        dic-error "sandbox-error: $src: not found"
        return 1
    }

    sum=$(cksum <"$src") || return 1
    sum=${sum// /-}
    cache=${XDG_CACHE_HOME:-$HOME/.cache}/geni
    bin=$cache/sandbox-seccomp-$sum
    blob=$bin.bpf

    if [[ ! -s $blob ]]; then
        mkdir -p "$cache" || return 1
        dic-info "sandbox-info: building $blob"
        ${CC:-cc} -std=gnu99 -O2 -Wall -Wextra -o "$bin.$$" "$src" ||
            return 1
        "$bin.$$" >"$blob.$$" || return 1
        mv -f "$bin.$$" "$bin" || return 1
        mv -f "$blob.$$" "$blob" || return 1
    fi

    printf '%s\n' "$blob"
}

# True when a word is a name a shell can export: a shell identifier, and
# nothing else.  A `--`, a command name and a bwrap option are none of
# those, so a walk that stops at the first word this rejects stops exactly
# where the run of --envs names stops.  sandbox() and sandbox-complete()
# both use it, so completion can never offer a name the wrapper refuses.
sandbox-name() {
    [[ ${1:-} =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]
}

# True when a word is a leading NAME=value assignment: a shell identifier,
# an equals sign, and a value.  A `--`, a command name and a bwrap option
# are none of those, so a walk that stops at the first word this rejects
# stops exactly where bash's own assignment prefix stops.
sandbox-assign() {
    [[ ${1:-} == *=* && ${1%%=*} =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]
}

# sandbox-complete -- bash completion for sandbox().
#
# The payload is the first word that is neither a leading --envs (which
# takes a name that sandbox-name recognizes, the same predicate
# sandbox() peels with, so completion and the wrapper agree on where
# the run ends) nor a bwrap option (a word that begins with -).  A `--`
# is bwrap's own
# end-of-options marker: the word after it is the payload whether or
# not it begins with -, and the walk stops there.
#
# Completion at the payload is command completion: PATH is widened
# to the sbin directories, the same widening sudo(8)'s completion
# does at the word after its own options, and bash's compgen -A
# command lists the command names there.  Everywhere else the word
# is a bwrap option or the value of a leading assignment, and file
# completion is what a caller typing one of those wants.  Nothing
# here is read from bash-completion, so the function stands on its
# own in a shell that has never sourced it.
sandbox-complete() {
    local cur=${COMP_WORDS[COMP_CWORD]}
    local cword=$COMP_CWORD

    # Walk the words left to right; the loop stops at the payload's
    # index.  Each word before it is a leading --envs, a leading
    # NAME=value or a bwrap option.  A `--` is bwrap's own, and the
    # word after it is the payload.
    local i=1
    while (( i <= cword )); do
        if sandbox-assign "${COMP_WORDS[i]}"; then
            ((i++))
        elif [[ ${COMP_WORDS[i]} == --envs ]]; then
            # the word after it names one of this shell's variables, and
            # the names of this shell are what is offered for it
            if (( i + 1 == cword )); then
                mapfile -t COMPREPLY < <(compgen -v -- "$cur")
                return
            fi
            ((i+=2))
        elif [[ ${COMP_WORDS[i]} == --envs=* ]]; then
            ((i++))
        elif [[ ${COMP_WORDS[i]} == -- ]]; then
            ((i++))
            break
        elif [[ ${COMP_WORDS[i]} == -* ]]; then
            ((i++))
        else
            break
        fi
    done

    # At the payload word, complete command names from the widened
    # PATH.
    if (( i == cword )); then
        local PATH=$PATH:/sbin:/usr/sbin:/usr/local/sbin
        mapfile -t COMPREPLY < <(compgen -A command -- "$cur" | sort -u)
        return
    fi

    # Still inside the option run.  A bwrap option in progress names no
    # file, so the fallback is a path; a --envs= in progress names a
    # variable, and a path is never one of those.
    [[ $cur == --envs=* ]] && return
    mapfile -t COMPREPLY < <(compgen -f -- "$cur")
}

complete -o filenames -F sandbox-complete sandbox

sandbox() {
    # --envs is a name read from this shell, and a leading NAME=value is a
    # value given as a word; both are values an argument list must not
    # hold, so both travel the same way and neither becomes a --setenv.
    # Peel the leading run of either off before anything else looks at the
    # argument list, so what is left is the same list bwrap would have
    # been handed without them.
    local -a want=() given=()
    local list name word
    while (( $# )); do
        if sandbox-assign "$1"; then
            given+=("$1")
            shift
            continue
        fi
        case $1 in
            --envs=*) list=${1#--envs=} ;;
            --envs)
                shift
                if (( ! $# )); then
                    dic-error "sandbox-error: --envs needs a name"
                    return 1
                fi
                list=$1 ;;
            *) break ;;
        esac
        if [[ -z $list ]]; then
            dic-error "sandbox-error: --envs needs a name"
            return 1
        fi
        # A comma list.  An empty element is not a name, and the check
        # below is what turns `--envs=a,,b` into an error rather than
        # into a variable nothing could export.
        local -a names=()
        IFS=, read -ra names <<<"$list"
        for name in "${names[@]}"; do
            if ! sandbox-name "$name"; then
                dic-error "sandbox-error: --envs: '$name' is not a name"
                return 1
            fi
            if [[ ! -v $name ]]; then
                dic-error "sandbox-error: --envs $name: not set in this shell"
                return 1
            fi
            want+=("$name")
        done
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

    # The directory the shell is already in, and the one path of the
    # caller's own tree the jail mounts.  The bind states the path and the
    # read-only bind over it takes the writing away, because bwrap applies
    # its arguments left to right and the last one wins -- so the tree is
    # in the map, a tool call is side effect free, and a caller who wants
    # to write adds the bind back with its own arguments, which come last.
    # bwrap creates the missing parents inside the jail, so
    # /home/me/proj appears on its own and the rest of /home/me does not.
    args+=(--bind "$PWD" "$PWD")
    args+=(--ro-bind "$PWD" "$PWD")

    # The interpreter can live outside $PWD: in a git worktree it sits in
    # the main worktree, and bwrap then cannot execvp the test runner
    # because that path was never mounted.  Bring in that one path
    # read-only, unless it is already inside $PWD and mounted with it.
    if [[ -n ${VIRTUAL_ENV:-} && -d $VIRTUAL_ENV ]]; then
        case $VIRTUAL_ENV/ in
            "$PWD"/*) ;;
            *) args+=(--ro-bind "$VIRTUAL_ENV" "$VIRTUAL_ENV") ;;
        esac
    fi

    # Empty the environment, then put back only what a program needs to find
    # itself.  An exported API key is not a variable the payload can read
    # here; --envs is how one is handed over when a build actually needs it.
    args+=(--clearenv --setenv HOME /tmp)
    for p in PATH TERM LANG LC_ALL TZ; do
        [[ -v $p ]] && args+=(--setenv "$p" "${!p}")
    done

    # The variables peeled off the front go in through a file and not
    # through --setenv, because --setenv takes its value as a word of its
    # own and a word is exactly what `ps` prints.  The pairs are NUL
    # separated, so a value may hold anything but a NUL, and mktemp makes
    # the file 0600 before it holds one.  --tmpfs /run above is what makes
    # /run/dic-env a path nothing could have bound over; bwrap reads the
    # descriptor and not that path, and the file is unlinked in the
    # subshell below before bwrap starts, so neither the process table nor
    # a directory on the host ever holds the bytes.
    local envfile=
    if (( ${#want[@]} || ${#given[@]} )); then
        envfile=$(mktemp "${TMPDIR:-/tmp}/dic-env.XXXXXXXX") || {
            dic-error "sandbox-error: cannot make a file for --envs"
            return 1
        }
        chmod 600 "$envfile"
        for name in "${want[@]}"; do
            printf '%s\0%s\0' "$name" "${!name}"
        done >"$envfile"
        for word in "${given[@]}"; do
            printf '%s\0%s\0' "${word%%=*}" "${word#*=}"
        done >>"$envfile"
        args+=(--file 9 /run/dic-env)
        # The payload is wrapped in a shell that reads the pairs back into
        # its own environment and then becomes the command, so what runs is
        # the command itself and not a shell in front of it.
        set -- bash -c 'while IFS= read -r -d "" n && IFS= read -r -d "" v; do
            export "$n=$v"
        done < /run/dic-env
        exec "$@"' -- "$@"
    fi

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

    # --file takes a descriptor the same way --seccomp does, and it is
    # opened here, inside the subshell, so it belongs to the sandbox and
    # not to the shell that asked for it; the file loses its name before
    # bwrap starts.  A call with no --envs opens /dev/null instead, which
    # is what keeps this one line and one process.
    #
    # env -i is the scrub the header describes: bwrap is what /proc/1
    # names from inside the jail, so the environment this shell was
    # started with would be readable there.  PATH survives it so that
    # bwrap can execvp the payload, and TERM so that a program which reads
    # it has something sane before the payload's own --setenv.
    ( exec 9<"${envfile:-/dev/null}"
      if [[ -n $envfile ]]; then
          rm -f "$envfile"
      fi
      exec env -i PATH="${PATH:-/usr/bin:/bin}" TERM="${TERM:-dumb}" \
          bwrap "${args[@]}" --seccomp 3 "$@" 3<"$blob" )
}
