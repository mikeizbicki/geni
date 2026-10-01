# This file configures dic.  It should be sourced from .bashrc.

# One source defines everything: this file defines the dic helpers and
# sources every sibling script, so a caller may assume dic-run,
# dic-git-clean, dic-llm, committe, itera, geni and sandbox are all
# available.  The guard keeps a second source (a .bashrc that names the
# file twice, say) from re-registering the completion and re-binding
# the keys below.
#
# A second source is a no-op by default, so an edited file does not
# take effect until a new shell.  `source dic.sh -f` re-runs the file
# in the shell that asks, so the reload reaches the functions and the
# bindings the first source installed.  Any other argument is refused,
# so a typo does not read as a reload that happened.
if [[ -n ${_DIC_LOADED:-} ]]; then
    case ${1:-} in
        -f)  unset _DIC_LOADED ;;
        '')  echo "dic.sh: already sourced; source ${BASH_SOURCE[0]} -f to reload" >&2
             return 1 ;;
        *)   echo "dic.sh: unknown argument: $1" >&2
             echo "dic.sh: source ${BASH_SOURCE[0]} -f to reload" >&2
             return 1 ;;
    esac
fi
_DIC_LOADED=1

export DIC_MODEL=groq+qwen
export DIC_SYSTEM="Keep your response short, between 1-20 lines. Focus on a high signal to noise ratio (audience has strong math/cs background). If the question is about a computer, respond for: $(uname -a)."
[[ $- == *i* ]] && export DIC_SESSION="$$"

# Record per-round byte-rate samples into dic.db, so that a B/s over time
# plot is a SQL query.  Off by default in dic; on here
export DIC_TRACE=1

alias qwen='dic -m groq+qwen'
alias fable='dic -m anthropic+fable'
alias opus='dic -m anthropic+opus'
alias sonnet='dic -m anthropic+sonnet'
alias haiku='dic -m anthropic+haiku'
alias deepseek='dic -m openrouter+deepseek'
alias gemini='dic -m openrouter+gemini'

# --- shared helpers -----------------------------------------------------

# Single-quote an argument for the audit line.  The whole string sits
# inside ' ', so a prose prompt reads as written instead of as a run of
# escaped spaces, while a lone identifier -- a SHA, a path, a mid --
# still reads as one token.  An embedded quote becomes the four-
# character close-reopen '\'' that every POSIX shell has used forever.
# %q is not this: it escapes whitespace one character at a time.
_dic_quote() {
    local s=$1
    s=${s//\'/\'\\\'\'}
    printf "'%s'" "$s"
}

# Echo a command to stderr and then run it.  This is the audit trail
# Quote a word only when it would not survive a re-parse as-is: an
# empty argument, or one carrying whitespace or a shell metacharacter.
# A path, a SHA, or a flag prints bare, so the audit line reads the way
# the caller typed it; a prose prompt gets ' ', with any embedded
# quote written as the POSIX close-reopen '\''.  The optional $2 is an
# elision marker -- <...> -- which follows the word when it printed
# bare and sits inside the quotes when it did not, so it never reads
# as a shell word of its own.
_dic_quote() {
    local s=$1
    if [[ -n $s && $s != *[^A-Za-z0-9_@%+=:,./-]* ]]; then
        printf '%s%s' "$s" "${2:+ $2}"
    else
        local q=\' e="'\\''"
        printf "'%s%s'" "${s//$q/$e}" "${2:+ $2}"
    fi
}

# these scripts do not get from `set -x`: xtrace is a shell-wide flag
# that would print the wrappers' own internals, and several call sites
# here -- git apply in committe's retry, itera's pre-check test -- are
# calls that are allowed to fail, so a shell option that aborts on a
# nonzero status would abort them too.  dic-run prints only the calls a
# caller names.
#
# fd 2 and not fd 1: fd 1 is the payload's channel and it varies per
# call site (dic's reply, itera's tee of the tests), so a trace written
# there would be read back by the pipe's next stage.  The color is
# dropped when fd 2 is not a terminal and when NO_COLOR is set, so the
# same function fills an interactive terminal and, one `2>log` away,
# the file a background geni will log to.
#
# The tag names the latin scripts this call is nested under, read from
# the shell's own FUNCNAME rather than counted from a baseline depth:
# the depth at which dic.sh was sourced depends on who sourced it, and
# a helper added or renamed in between would move a count without
# moving what the line means.  FUNCNAME[0] is dic-run itself and is
# skipped; the loop walks outwards, so each match is prepended and the
# tag reads outermost first.
dic-run() {
    local f names=() tag= red= reset= a
    for f in "${FUNCNAME[@]:1}"; do
        case $f in geni|itera|committe) names=("$f" "${names[@]}");; esac
    done
    (( ${#names[@]} )) && tag="[${names[*]// /:}] "
    if [[ -t 2 && -z ${NO_COLOR:-} ]]; then red=$'\e[31m'; reset=$'\e[0m'; fi
    {
        printf '%s%s+' "$red" "$tag"
        for a in "$@"; do
            # A multi-line argument -- a heredoc pasted as one -- is
            # summarized by its first line, which its author wrote as
            # the summary: whole when it fits in a terminal line, cut
            # to its first few words when it does not.  A single-line
            # argument with whitespace is prose -- a prompt -- and its
            # first few words are enough to recognize it.  An argument
            # with no whitespace is an identifier -- a SHA, a path, a
            # mid -- and every byte of it is worth showing, so it is
            # printed whole.  DIC_TRACE_WORDS and DIC_TRACE_MAX are
            # shell variables, not exports, because no child process
            # reads them.
            if [[ $a == *$'\n'* ]]; then
                local head=${a%%$'\n'*}
                if (( ${#head} >= 50 )); then
                    local -a w
                    read -ra w <<< "$head"
                    head=${w[*]:0:5}
                fi
                printf ' %s' "$(_dic_quote "$head" '<...>')"
            elif [[ $a == *[[:space:]]* && ${#a} -gt ${DIC_TRACE_MAX:-30} ]]; then
                local -a w
                read -ra w <<< "$a"
                printf ' %s' "$(_dic_quote "${w[*]:0:${DIC_TRACE_WORDS:-3}}" '<...>')"
            else
                printf ' %s' "$(_dic_quote "$a")"
            fi
        done
        printf '%s\n' "$reset"
    } >&2
    "$@"
}

# Refuse to run unless the repository is clean.  `$1` names the caller,
# so the message keeps the <cmd>-error: convention: committe checks
# this behind -f and geni checks it before making a worktree, and only
# the message differs.  The toplevel and detached-HEAD checks geni also
# needs are its own and stay in geni.
dic-git-clean() {
    local who=$1
    if ! git rev-parse --git-dir >/dev/null 2>&1; then
        echo "$who-error: not inside a git repository" >&2
        return 1
    fi
    if ! git diff --quiet --cached; then
        echo "$who-error: staging area is non-empty" >&2
        return 1
    fi
    if ! git diff --quiet; then
        echo "$who-error: working tree has uncommitted changes" >&2
        return 1
    fi
}

# Print the model command a latin script invokes: `dic` when it is on
# PATH and simonw's `llm` when it is not.  This is not a check that one
# of our scripts is present -- sourcing dic.sh guarantees that -- it is
# the one soft dependency that cannot be removed, because a user may
# have neither backend installed.
dic-llm() {
    if command -v dic >/dev/null 2>&1; then
        echo dic
    elif command -v llm >/dev/null 2>&1; then
        echo llm
    else
        echo "dic-error: neither dic nor llm installed" >&2
        return 1
    fi
}

# Source the sibling scripts, so that dic.sh is the one file a .bashrc
# names and every command in this directory comes with it.
# BASH_SOURCE[0] names this file, whoever sourced it and from wherever.
_dic_src=${BASH_SOURCE[0]}
_dic_dir=${_dic_src%/*}
[[ $_dic_dir == "$_dic_src" ]] && _dic_dir=.
_dic_dir=$(cd -- "$_dic_dir" && pwd) || _dic_dir=.
for _dic_s in committe.sh itera.sh geni.sh sandbox.sh; do
    source "$_dic_dir/$_dic_s"
done
unset _dic_src _dic_dir _dic_s

# --- tab completion -----------------------------------------------------

# What the picker lists, and so what a mid after -c may be completed to.
# DIC_LOG_SCOPE says how much of the message tree to offer:
#
#   all      every conversation in the database (the default)
#   session  every conversation written under $DIC_SESSION, children included
#   chain    this session's current conversation, and nothing else
#
# `all` and `session` are what the graph is for: the rows of several
# conversations at once only mean something with the forest drawn beside
# them, so those two scopes turn --graph on and `chain` does not.
_dic_log_flags() {
  case ${DIC_LOG_SCOPE:-all} in
    all)     printf '%s\n' --graph --all ;;
    session) printf '%s\n' --graph --session ;;
    chain)   : ;;
    *)       printf 'dic.sh: bad DIC_LOG_SCOPE: %s\n' "$DIC_LOG_SCOPE" >&2 ;;
  esac
}

# The message picker: `dic --log`'s rows into fzf, and the mid it returns.
# fzf draws on the terminal and writes the choice on stdout, so bash is what
# captures it; --with-nth hides the mid from the display while {1} still
# hands --show the full value to preview.  Without an fzf the same rows come
# back as a plain list, one mid per line.
#
# No --height: an inline picker paints over readline's line and erases it on
# exit, and readline does not know to repaint, so the half-typed command
# disappears and a cancelled picker leaves a blank line.  Full-screen fzf
# uses the terminal's alternate screen instead, which it restores whole, so
# the line a caller was typing is exactly where it left it.
#
# A line the graph draws for a merge carries no mid, and fzf has no way to
# say a line cannot be picked, so those lines are filtered out once, before
# either reader sees them.  --delimiter is a tab and not fzf's whitespace
# default, because the graph column is made of spaces and would otherwise be
# split into fields of its own.
_dic_mids() {
  local -a flags
  mapfile -t flags < <(_dic_log_flags)
  local rows
  rows=$(dic --log "${flags[@]}" 2>/dev/null \
         | awk -F'\t' 'NR == 1 || $1 != ""')
  [[ -z $rows ]] && return
  if command -v fzf >/dev/null 2>&1; then
    printf '%s\n' "$rows" | fzf \
        --reverse --no-multi --prompt='mid> ' \
        --header-lines=1 --with-nth=2.. --delimiter='\t' \
        --preview 'dic --show {1}' --preview-window=right:60% \
      | awk -F'\t' '$1 != "" { print $1; exit }'
  else
    printf '%s\n' "$rows" | tail -n +2 | cut -f1
  fi
}

# Fill COMPREPLY with one picked mid, or with the list to pick from.  A $1
# prefix turns the pick into --mid=REF, which is how -c names a message: -c
# itself takes no value, so the ref arrives beside it.
_dic_complete_mid() {
  local prefix=$1 cur=$2 list
  list=$(_dic_mids)
  if [[ -z $list ]]; then
    COMPREPLY=()
  elif [[ $list != *$'\n'* ]]; then
    COMPREPLY=( "$prefix$list" )
  else
    COMPREPLY=( $(compgen -W "$list" -- "$cur") )
  fi
}

_dic_complete() {
  local cur=${COMP_WORDS[COMP_CWORD]} prev=${COMP_WORDS[COMP_CWORD-1]}

  case $prev in
    # tab complete model names
    -m|--model)
      # --models prints a header then three columns: the id, its key
      # variable, and whether that variable is set.  A completion reads
      # the first column of every other line.
      COMPREPLY=( $(compgen -W "$(dic --models | tail -n +2 | cut -f1)" -- "$cur") )
      return ;;
    # tab complete files
    -a|--attachment|--path|--models-file)
      compopt -o default 2>/dev/null
      COMPREPLY=( $(compgen -f -- "$cur") )
      return ;;
    # tab complete a message to continue: -c has no value of its own, so the
    # pick becomes --mid=REF beside it, while --mid, --show and --from each
    # take the ref they pick directly
    -c|--continue)
      _dic_complete_mid "--mid=" "$cur"
      return ;;
    --mid|--show|--from)
      _dic_complete_mid "" "$cur"
      return ;;
  esac

  if [[ $cur == -* ]]; then
    COMPREPLY=( $(compgen -W "-m --model -s --system -a --attachment \
                              -x --extract -f --force -c --continue \
                              --mid --show --from --log --limit --all \
                              --graph --session \
                              --cache --tools --path --mime-type \
                              --pv-thinking --no-pv-thinking \
                              --clipboard --no-clipboard --trace --stats" -- "$cur") )
  fi
}
complete -F _dic_complete dic

# The same picker on a keystroke: TAB always opens it, which costs a
# caller who already knows the mid; binding it to \C-x\C-m lets that fast
# path stay fast and makes the window onto the tree opt-in.  The chosen
# mid is spliced in at the cursor as --mid=REF, which is how -c reaches a
# message -- -c takes no value of its own, so the ref travels beside it.
_dic_pick() {
  local mid=$(_dic_mids)
  [[ -z $mid ]] && return
  READLINE_LINE="${READLINE_LINE:0:$READLINE_POINT} --mid=$mid ${READLINE_LINE:$READLINE_POINT}"
  READLINE_POINT=$((READLINE_POINT + 8 + ${#mid}))
}
bind -x '"\C-x\C-m":_dic_pick'

# --- prompt-expansion widget --------------------------------------------
# This widget introduces a new syntax $$(...) for command substitution.
# When this widget encounters
#
#     $$(...)
#
# the line above will be replaced with
#
#     $ ...
#     $(...)
#
# This syntax is useful for constructing complex prompts using heredocs.
# For example:
#
#     dic <<EOF
#     $$(files-to-prompt src/)
#     $$(git diff HEAD~2)
#     $$(pytest)
#
#     Why did the previous commits cause these tests to fail?
#     EOF
#
# gets expanded to
#
#     dic <<EOF
#     $ files-to-prompt src/
#     $(files-to-prompt src/)
#     $ git diff HEAD~2
#     $(git diff HEAD~2)
#     $ pytest
#     $(pytest)
#
#     Why did the previous commits cause these tests to fail?
#     EOF
#
# The widget expansion happens immediately when the user presses enter.
# The $(...) is not run immediately and will be expanded by the shell like normal.
#
# NOTE:
# This widget expansion happens at the readline level.
# It will therefore expand *everywhere* in bash, and not just in heredocs.
# For example, it will expand in the standard repl and quoted heredocs <<'EOF'.
# This is unlikely to cause errors because $$(...) is never valid shell.
# The widget will never expand in non-interactive sessions.

_dic_expand() {
  [[ $READLINE_LINE =~ ^\$\$\((.*)\)$ ]] || return
  local cmd=${BASH_REMATCH[1]}
  READLINE_LINE=$'$ '"$cmd"$'\n$('"$cmd"$')'
  READLINE_POINT=${#READLINE_LINE}
}
bind -x '"\C-x\C-r":_dic_expand'
bind '"\C-m": "\C-x\C-r\C-j"'
