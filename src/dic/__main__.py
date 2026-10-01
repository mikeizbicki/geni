#!/usr/bin/env python3
"""dic - a minimalist CLI for chat LLMs.  See SPEC.md.

The whole CLI: turn argv and stdin into one dic() call, and leave without
waiting for the interpreter to tear down.  Everything else is a library, so
the only work left here is the work a library must not do: read the process's
stdin, and turn a DicError or a ^C into an exit status.

    dic/dic.py          arguments, stdin, os._exit
    dic/client.py       dic(), Reply -- the program
    dic/options.py      Flag: one declaration per knob, the CLI, the env vars
    dic/config.py       model and provider config: json sources, sqlite cache
    dic/store.py        sqlite message tree, attachments, session pointers
    dic/tty.py          colour, errors, the cost line, the progress meters
    dic/tool.py         --tools: python functions the model may call
    dic/tools/*.py      the tools that ship with dic, one category each
    dic/adaptors/*.py   one wire protocol each
    dic/models.json     packaged defaults, overlaid by the user's files
"""
import time                     # first, so that T0 measures dic's own startup
T0 = time.time_ns()             # cost -- imports, config, db -- as well as the API's

import os, sys


def init_line():
    """The one line `dic --init` prints: a `source` of the packaged dic.sh.

    `eval "$(dic --init)"` is what a pip-installed dic offers instead of a
    git clone: the shell sources the real file -- not a process
    substitution, so BASH_SOURCE[0] in dic.sh is a path and the
    sibling-sourcing loop there resolves.  The aliases, the tab completion
    and the `itera` family come with it.  A `-f` in argv is forwarded, so
    the shell that asks can reread an edited dic.sh into itself.
    """
    from importlib.resources import files
    import shlex
    path = files("dic.scripts") / "dic.sh"
    extra = " -f" if "-f" in sys.argv[1:] else ""
    return f"source {shlex.quote(str(path))}{extra}"


def main():
    # --init names a file and nothing else, so it is answered before the
    # client, its sqlite and its json config are imported: a .bashrc pays
    # for one importlib lookup and one print, not for the message tree.
    if "--init" in sys.argv[1:]:
        print(init_line())
        return

    from dic.client import dic
    from dic.options import parser
    from dic.tty import DicError, die

    try:
        parsed, extra = parser().parse_known_args()
        args = vars(parsed)
        # Every word argparse did not claim is a prompt word too.  A wrapper
        # such as committe puts its own instructions in front of the request
        # it forwards, and argparse matches only one run of positionals, so
        # the words on either side of a flag would otherwise be dropped.
        prompt = " ".join(args.pop("prompt") + extra)
        if not sys.stdin.isatty():
            piped = sys.stdin.read()
            prompt = f"{prompt}\n\n{piped}" if (prompt and piped.strip()) else (prompt or piped)
        dic(prompt, **args, t_start=T0, env=os.environ, out=sys.stdout, err=sys.stderr)
        sys.stdout.flush()
    except DicError as e:
        # dic() reports a failure by raising, so the CLI is the one place
        # that decides its colour, its stream and its exit code
        die(e, err=sys.stderr, env=os.environ)
    except KeyboardInterrupt:
        # dic() lets a ^C during a reply propagate too: cancelled is a failure
        # like any other, and the exit code is the CLI's business
        die("cancelled", err=sys.stderr, env=os.environ)
    os._exit(0)   # skip interpreter teardown; the last token is already out


if __name__ == "__main__":
    main()
