# launch.sh -- open a new terminal window and run a command in it.
#
# Source this file and it defines one function, `launch`:
#
#     source scripts/launch.sh
#     launch make -j
#     launch bash
#
# Every argument is the command.  The new window starts in the caller's
# directory and with the caller's environment, so a command that reads
# $PATH, $VIRTUAL_ENV or $PWD reads the values it would have read here.
# `kitty --copy-env` cannot carry the environment on its own: it reads
# the source process's environment out of /proc, which the kernel froze
# at that process's execve, so a variable an activation script set in
# this shell after it started is not there.  `env -0` is exec'd now, so
# what it sees is what the child gets.
#
# The file also defines `launch-init`, which the new window runs before
# the caller's command.  Everything that marks a window as a launched
# one lives there, so `launch` does not gain a line per marker.
# `launch-init` calls `launch-termbg` to read the terminal's background
# and `launch-blend` to tint it, then sets `PS1`.
#
# Opening the window is kitty-specific, because `kitty @ launch` is the
# mechanism that reaches a running kitty server; there is no portable
# spelling of "open a top-level window", so a port to another terminal
# replaces the one `kitty @ launch` line and leaves the rest alone.
# The marking half is not terminal-specific -- OSC 11 is what every
# terminal in common use answers -- so `launch-init`, `launch-termbg`
# and `launch-blend` survive a port unedited.
#
# Sourcing this file defines functions and does nothing else.  It does
# not source dic.sh and does not depend on it, so the child, which
# sources this file to reach `launch-init`, pays for nothing else.

launch-termbg() {
    # Read the terminal's current background as #rrggbb.
    #
    # The query is OSC 11, which xterm, kitty, wezterm, foot, alacritty,
    # gnome-terminal and iTerm2 all implement, so the answer is the
    # window's own effective background -- whatever theme, override or
    # default put it there.  The reply comes back on the same tty as a
    # byte string with no newline, so the read is on the tty directly,
    # in raw mode, with a short timeout; the settings are restored
    # before the function returns.
    local tty saved reply c fd
    tty=$(tty) || return 1
    exec {fd}<>"$tty" || return 1
    saved=$(stty -g <&$fd) || { exec {fd}>&-; return 1; }
    # `min 1 time 0` makes each read block for one byte, so a byte the
    # terminal has not sent yet is waited for and not dropped.
    stty raw -echo min 1 time 0 <&$fd
    printf '\033]11;?\033\\' >&$fd
    # No single delimiter covers the reply: the terminator is BEL on
    # some terminals and ST on others.  Read a byte at a time and stop
    # at whichever arrives; -t 1 bounds a terminal that sends nothing.
    reply=
    while IFS= read -r -t 1 -n 1 c <&$fd; do
        reply+=$c
        [[ $c == $'\a' ]] && break
        if [[ $reply == *rgb:* && $c == $'\033' ]]; then
            # ST is `ESC \`; the `\` is still queued and would be read
            # as shell input in the new window, so consume it here.
            IFS= read -r -t 1 -n 1 c <&$fd
            break
        fi
    done
    stty "$saved" <&$fd
    exec {fd}>&-
    # The reply is `ESC ] 11 ; rgb:RR../GG../BB.. ST`, terminated with
    # either BEL or ST.  xterm pads each channel to four hex digits and
    # other terminals use two; taking the top byte of each reads both.
    [[ $reply =~ rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+) ]] || return 1
    printf '#%s%s%s\n' \
        "${BASH_REMATCH[1]:0:2}" "${BASH_REMATCH[2]:0:2}" "${BASH_REMATCH[3]:0:2}"
}

launch-blend() {
    # Blend color $2 over color $1 at weight $3 parts in a thousand, and
    # print the result.  Both colors are #rrggbb; the default weight is
    # 500, an even mix.  Per mille rather than parts in ten, so that the
    # weights in use -- a tint laid on nearly full -- are stated exactly.
    # There is no alpha: kitty's `background_opacity`
    # composites against the desktop and not against the terminal's own
    # background, so a translucent window would show wallpaper rather
    # than a grayed theme color.  A blend computed here is what
    # "overlay at 0.9 alpha" actually means.
    local r=$((16#${1:1:2})) g=$((16#${1:3:2})) b=$((16#${1:5:2}))
    local w=${3:-500}
    printf '#%02x%02x%02x' \
        $(( (16#${2:1:2}*w + r*(1000-w))/1000 )) \
        $(( (16#${2:3:2}*w + g*(1000-w))/1000 )) \
        $(( (16#${2:5:2}*w + b*(1000-w))/1000 ))
}

launch-init() {
    # Everything that marks this window as a launched one.  Runs in the
    # child, in a shell that has launch.sh sourced, before the command
    # the caller named.  A new marker is added here, never in `launch`.
    #
    # The tint is blended in rather than the background replaced, so
    # the window stays readable in a light theme and in a dark one:
    # with the defaults, white becomes #787878 and black becomes
    # #757575.  The two knobs below are shell variables, not exports,
    # because only this function reads them.
    local bg tint
    if bg=$(launch-termbg) &&
       tint=$(launch-blend "$bg" "${DIC_LAUNCH_TINT:-#777777}" \
                            "${DIC_LAUNCH_ALPHA:-050}"); then
        # OSC 11 sets the background.  Written to stdout, which is the
        # child's pty, so this does not have to name a fd.
        printf '\033]11;%s\033\\' "$tint"
    fi

    # A prompt that names the window, so the marking survives an
    # interactive command that repaints the screen over the tint.
    #
    # The assignment is made here and then made again before every
    # prompt.  It cannot be made once: writing PS1 is not enough to
    # keep it, because bash initializes PS1 itself when an interactive
    # shell starts, and the startup files such a shell reads around
    # this call -- /etc/bash.bashrc on Debian, a --rcfile passed by a
    # caller -- write it afterwards, whichever runs last winning.
    # PROMPT_COMMAND runs after all of them and immediately before the
    # prompt is expanded, so a marker set there is the one the user
    # sees.
    launch-prompt() {
        [[ ${PS1-} == '[launch] '* ]] || PS1='[launch] '"${PS1:-\$ }"
    }
    launch-prompt
    # Appended, so that a PROMPT_COMMAND which computes a prompt of
    # its own runs first and the marker lands on what it computed.
    PROMPT_COMMAND="${PROMPT_COMMAND:+$PROMPT_COMMAND; }launch-prompt"
}

launch() {
    # Open a new window and run $@ in it.  The command may be a program
    # or a shell function.  A program is exec'd, so it owns the window
    # and the window closes when it exits.  A function is run and then
    # the shell stays interactive, so `launch worktree newfeature`
    # opens a window, creates a worktree and cd's into it, and leaves
    # the caller able to keep typing there.  A failure of
    # `kitty @ launch` is the only failure this function can report;
    # once the window exists, the command's success or failure is the
    # child's business.
    #
    # The choice of branch cannot be made here: a function only lives
    # inside a running shell, and this one has not been sourced in the
    # child yet.  So the file the child reads makes the choice, and
    # both branches are written into it below.

    # The child sources this file to reach launch-init, so the path has
    # to survive the trip: BASH_SOURCE names the file even when a caller
    # sourced it by a relative path or through a symlink, and realpath
    # makes it absolute so the child can source it from any cwd.  The
    # rcfile below names the file too, so the path is resolved before
    # the arguments are inspected.
    local self rc
    self=$(realpath "${BASH_SOURCE[0]}")

    # The child's interactive bash reads this file.  `source ~/.bashrc`
    # is what gives it dic's functions and aliases, and a function
    # named on the command line is one of dic's, so this line is why
    # `launch` can take one at all.  `source $self` gives it
    # launch-init, and the call puts the marker on a bash that has
    # stopped resetting PS1.  The file erases itself once it has been
    # read, so the caller needs no trap to clean up after it.
    #
    # The final branch is the whole point.  `type -t` names what the
    # first word is, and a function is run -- in this shell, so a cd
    # it makes persists -- while anything else is exec'd, so the
    # window closes with the program.  With no argument at all the
    # file stops after launch-init and bash presents a prompt, which
    # is the interactive shell the no-argument case opened before.
    rc=$(mktemp -t launch.XXXXXX) || return 1
    {
        printf '[ -f ~/.bashrc ] && source ~/.bashrc\n'
        printf 'source %q\n' "$self"
        printf 'launch-init\n'
        printf 'rm -f %q\n' "$rc"
        if (( $# )); then
            printf 'if [[ "$(type -t -- %q)" == function ]]; then\n' "$1"
            printf '  '
            printf '%q ' "$@"
            printf '\nelse\n'
            printf '  exec '
            printf '%q ' "$@"
            printf '\nfi\n'
        fi
    } > "$rc" || { rm -f "$rc"; return 1; }

    # `env -0` separates on NUL, so a value with a newline in it is one
    # environment variable and not several.  The loop turns the current
    # environment into a list of `--env K=V` options; `env` is exec'd
    # now, so it sees this shell's environment, which reading
    # /proc/<pid>/environ would not.
    local -a e=() v
    while IFS= read -r -d '' v; do e+=(--env "$v"); done < <(env -0)

    # --cwd=current makes $PWD follow: it is the foreground process's
    # directory in the source window, which is this shell.  --type=os-
    # window asks for a top-level window and not a split.  The `--`
    # ends kitty's options, so a command that starts with `-` is not
    # read as one.  stdout goes to /dev/null because `kitty @ launch`
    # prints the new window's id and the caller did not ask for it.
    # bash reads the rcfile written above and does the rest.
    kitty @ launch --type=os-window --cwd=current "${e[@]}" -- \
        bash --rcfile "$rc" -i \
        >/dev/null
}
