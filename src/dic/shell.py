"""The shell grammar of a sandboxed command: a word list, its flags, its argv.

A command under the sandbox dic ships is named as a word list and never as
a shell line.  The split is dic's and not the shell's, which is why a
target with a flag in it is written as one quoted string, and the words
before the program's own name are the sandbox's own: its flags, so
`--share-net mcp-fetch` is a server that may reach the network while
`mcp-fetch` is one that may not; and its assignments, so
`GH_TOKEN=x mcp-server-github` is a server whose own credential is set in
the jail, whose environment is otherwise empty because the sandbox clears
it.  A flag may take arguments, and ARITY says how many, which is bwrap's
own grammar and not a guess: `--ro-bind src dest cmd` is one option and a
command, and reading `src` as the command -- which is what the run was
before there was a table -- was never right.  `--` is bwrap's own
end-of-options and ends the run unambiguously, which is how a command
whose own name begins with `-` is reached.
"""
import os, shlex

from dic.paths import scripts_dir
from dic.tty import DicError

# The characters a shell reads as syntax: a `|` is a pipe, a `;` separates
# commands, a `>` is a redirection, a `$(...)` is a substitution.  A word
# that is *nothing but* these is one of them, the caller wrote a line, and
# dic cannot honor it: the target is an argument list, and a pipe is a
# second command outside whatever jail the caller asked for.  A word that
# merely CONTAINS one is an argument -- a python script passed to `python3
# -c` is full of semicolons and braces, and inside an argument they mean
# what the script means.
#
# `=` is not here, because `FOO=bar` is an argument; `-` is not, because a
# leading flag is how the wrapper is told what has to be relaxed.
SHELL = frozenset("|&;<>()[]{}`$*?~!")


# How many words each option of bwrap's own takes after it.  A word that
# begins with `-` and is not here takes none, which is what the whole run
# was assumed to be before there was a table: one flag, one word, and the
# next word was the command.  That is right for `--share-net` and wrong
# for `--ro-bind`, which names two paths before the command does, so
# `--ro-bind src dest cmd` read `src` as the server's name and had it
# run `dest` instead.  `--envs` is dic's own and is here for the same
# reason: the name after it is the flag's and not the command's.
ARITY = {
    # a source and a destination
    "--bind": 2, "--bind-try": 2, "--dev-bind": 2, "--dev-bind-try": 2,
    "--ro-bind": 2, "--ro-bind-try": 2, "--symlink": 2,
    # an fd and a destination
    "--file": 2, "--bind-data": 2, "--ro-bind-data": 2,
    # a destination
    "--tmpfs": 1, "--dir": 1, "--proc": 1, "--dev": 1, "--mqueue": 1,
    "--remount-ro": 1, "--lock-file": 1,
    # a name and a value, or a name
    "--setenv": 2, "--unsetenv": 1,
    # an fd, or a size
    "--seccomp": 1, "--size": 1,
    # dic has one of its own; see scripts/sandbox.sh
    "--envs": 1,
}


def words(target):
    """(flags, argv) of one --mcp command: its leading --words are the jail.

    A command is a word list and not a shell line, and the split is dic's
    and not the shell's, which is why a target with a flag in it is written
    as one quoted string.  The words before the program's own name are the
    sandbox's own, and they are of two kinds: its flags, so
    `--share-net mcp-fetch` is a server that may reach the network while
    `mcp-fetch` is one that may not; and its assignments, so
    `GH_TOKEN=x mcp-server-github` is a server whose own credential is set
    in the jail, whose environment is otherwise empty because the sandbox
    clears it.  A flag may take arguments, and ARITY says how many, which
    is bwrap's own grammar and not a guess: `--ro-bind src dest cmd` is
    one option and a command, and reading `src` as the command -- which is
    what the run was before there was a table -- was never right.  `--` is
    bwrap's own end-of-options and ends the run unambiguously, which is
    how a command whose own name begins with `-` is reached.  Only the
    leading run is read that way, so a program's own `-y` stays its own.
    What is left worth refusing is the word that is nothing but
    metacharacters -- a line whose author read it as a line -- because
    handing a program named `|` to bwrap is a failure that does not say
    what went wrong.

    >>> words("mcp-fetch --port 3")
    ([], ['mcp-fetch', '--port', '3'])
    >>> words("--share-net mcp-fetch")
    (['--share-net'], ['mcp-fetch'])
    >>> words("--ro-bind /etc/hosts /etc/hosts mcp-fetch")
    (['--ro-bind', '/etc/hosts', '/etc/hosts'], ['mcp-fetch'])
    >>> words("--tmpfs /run mcp-fetch")
    (['--tmpfs', '/run'], ['mcp-fetch'])
    >>> words("--share-net -- mcp-fetch --port 3")
    (['--share-net'], ['mcp-fetch', '--port', '3'])
    >>> words("GH_TOKEN=x --share-net mcp-server-github")
    (['GH_TOKEN=x', '--share-net'], ['mcp-server-github'])
    >>> words("'a b' c")
    ([], ['a b', 'c'])
    >>> words("python3 -c 'import json; print(1)'")
    ([], ['python3', '-c', 'import json; print(1)'])
    >>> words("a | b")
    Traceback (most recent call last):
    ...
    dic.tty.DicError: --mcp: '|' in 'a | b': a command is a word list, not a shell line (use a script for pipes and expansions)
    >>> words("")
    Traceback (most recent call last):
    ...
    dic.tty.DicError: --mcp: an empty command
    >>> words("--share-net")
    Traceback (most recent call last):
    ...
    dic.tty.DicError: --mcp: no command in '--share-net'
    """
    if not target.strip():
        raise DicError("--mcp: an empty command")
    tokens = shlex.split(target)
    for word in tokens:
        if word and set(word) <= SHELL:
            raise DicError(f"--mcp: {word[0]!r} in {target!r}: a command is a"
                           " word list, not a shell line (use a script for"
                           " pipes and expansions)")
    flags = []
    i = 0
    while i < len(tokens):
        word = tokens[i]
        if word == "--":             # bwrap's own end of the leading run
            i += 1
            break
        if word.startswith("-"):
            n = ARITY.get(word, 0)
            if i + n >= len(tokens):
                raise DicError(f"--mcp: {word}: {n} argument(s) expected")
            flags.extend(tokens[i:i + n + 1])
            i += n + 1
            continue
        assigned = "=" in word and word.partition("=")[0].isidentifier()
        if not assigned:
            break
        flags.append(word)
        i += 1
    argv = tokens[i:]
    if not argv:
        raise DicError(f"--mcp: no command in {target!r}")
    return flags, argv


def command(target):
    """The argv that runs one --mcp command under the sandbox dic ships.

    dic puts the sandbox in front of every stdio server it starts: the jail
    is not the caller's to forget, and not the caller's to switch off by
    redefining a function or exporting a variable, because the file that
    defines it is dic's own, named by its absolute path, and a child whose
    `sandbox` is not a function there exits rather than runs.  The one way
    to widen the jail is to say so in the target, whose leading --words are
    the sandbox's own.

    The target is that program's argument list and not a line: `"$@"` is
    never re-expanded, so a `|` or a `$(...)` in it is an argument and not
    syntax.  The script's own last line is `sandbox "$@"`, and what that
    receives is the caller's words with one `--` between the leading run and
    the command, so a command beginning with `-` is never read as another of
    the sandbox's own options.

    >>> command("server")[0]
    'bash'
    >>> command("server")[4:]
    ['--', 'server']
    >>> command("--share-net server")[4:]
    ['--share-net', '--', 'server']
    """
    flags, argv = words(target)
    path = shlex.quote(os.path.join(scripts_dir(), "dic.sh"))
    script = (f"builtin source {path} 2>/dev/null\n"
              "builtin declare -F sandbox >/dev/null"
              ' || { echo "dic: --mcp: the packaged sandbox is missing" >&2;'
              " exit 127; }\n")
    return ["bash", "-c", script + 'sandbox "$@"', "--", *flags, "--", *argv]