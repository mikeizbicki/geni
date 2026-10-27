#!/usr/bin/env python3
"""dic - a minimalist CLI for chat LLMs.  See SPEC.md.

The whole CLI: turn argv and stdin into one dic() call, and leave without
waiting for the interpreter to tear down.  Everything else is a library, so
the only work left here is the work a library must not do: read the process's
stdin, and turn a DicError or a ^C into an exit status.

    dic/dic.py          arguments, stdin, os._exit
    dic/client.py       dic(), Reply -- the program
    dic/options.py      Flag: one declaration per knob, the CLI, the env vars
    dic/commands.py     the readouts: one command each, never a model call
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
    from dic.paths import scripts_dir
    import shlex
    path = os.path.join(scripts_dir(), "dic.sh")
    extra = " -f" if "-f" in sys.argv[1:] else ""
    return f"source {shlex.quote(str(path))}{extra}"


def flag_value(argv, names):
    """The value a run gives to the first of names, or None.

    Both spellings argparse accepts for an option that takes a value: the
    option and its value as two words, or NAME=VALUE as one.  A short
    option's value glued to it (``-mfoo``) is not read, because dic's own
    convention is the two-word form and a glued short is argparse's,
    not dic's.

    >>> flag_value(["-m", "x"], ("-m", "--model"))
    'x'
    >>> flag_value(["--model=x"], ("-m", "--model"))
    'x'
    >>> flag_value([], ("-m", "--model")) is None
    True
    """
    for i, a in enumerate(argv):
        for name in names:
            if a == name and i + 1 < len(argv):
                return argv[i + 1]
            if a.startswith(name + "="):
                return a[len(name) + 1:]
    return None


def rate_line(rule):
    """One price rule as a person reads it: the rate per the unit it is in.

    A token rate is quoted per million, because that is how every price
    page states it and what dic's own PER does; any other unit is quoted
    per one, which is how an image at $0.04 is priced.  The extras that
    make a rule a refinement of another -- key, tier, when -- are named
    beside it, because without them a table with two 'in' rules reads as
    a table that prices 'in' twice.

    >>> rate_line({"rate": 3.0})
    '$3.0000 / Mtoken'
    >>> rate_line({"rate": 0.04, "unit": "image"})
    '$0.0400 / image'
    >>> rate_line({"rate": 6.0, "key": "in", "when": "in_total > 200000"})
    '$6.0000 / Mtoken  (key=in, when=in_total > 200000)'
    >>> rate_line({"rate": 7.5, "key": "out", "tier": "batch"})
    '$7.5000 / Mtoken  (key=out, tier=batch)'
    """
    unit = rule.get("unit", "token")
    per = "Mtoken" if unit == "token" else unit
    line = f"${rule.get('rate', 0):.4f} / {per}"
    extras = " ".join(f"{k}={rule[k]}" for k in ("key", "tier", "when")
                      if rule.get(k))
    return line + (f"  ({extras})" if extras else "")


def model_help(argv, env):
    """``dic -m NAME --help``: the resolved entry of one named model.

    Answers when a model is named and a help flag is given, before
    argparse's own ``--help`` -- which prints the whole command tree and
    knows nothing about a model -- runs.  The page is the entry a call
    would use: the model's API name, its base, the variable its key is
    read from (and whether it is exported), the wire protocol, the
    declared price rules with dic's cost_input and cost_output shorthand
    already expanded, and the options ``-o`` would merge over.

    An id that does not resolve is a DicError named here, not a page
    of nothing: the caller learns which id config.resolve refused.

    Returns True when it answered, so the caller knows not to fall
    through to argparse's own help; False when no model was named.
    """
    import json

    from dic import config
    from dic.store import db

    if not any(a in ("-h", "--help") for a in argv):
        return False
    name = flag_value(argv, ("-m", "--model"))
    if name is None:
        return False

    conn = db(env)
    config.sync(conn, env, flag_value(argv, ("--models-file",))
                or env.get("DIC_MODELS_FILE"))
    model = config.resolve(conn, name)

    def line(label, value):
        print(f"{label:<14}{value}")

    line("model_id", model["model_id"])
    line("model_name", model.get("model_name") or "")
    line("api_type", model.get("api_type") or "openai-chat")
    line("api_base", model.get("api_base") or "")
    key_name = model.get("api_key_name")
    if key_name:
        present = "set" if env.get(key_name) else "not set"
        line("api_key_name", f"{key_name} ({present})")
    if model.get("alias"):
        line("alias", model["alias"])
    if model.get("output"):
        line("output", model["output"])
    if model.get("system"):
        line("system", repr(model["system"]))
    if model.get("options") is not None:
        line("options", json.dumps(model["options"], sort_keys=True))
    if model.get("headers"):
        line("headers", json.dumps(model["headers"], sort_keys=True))
    price = model.get("price") or {}
    if price:
        print("price")
        width = max(len(rule) for rule in price)
        for rule in sorted(price):
            print(f"  {rule:<{width}}  {rate_line(price[rule])}")
    return True


def main():
    # --init names a file and nothing else, so it is answered before the
    # client, its sqlite and its json config are imported: a .bashrc pays
    # for one importlib lookup and one print, not for the message tree.
    if "--init" in sys.argv[1:]:
        print(init_line())
        return

    from dic import commands, config
    from dic.client import dic
    from dic.options import parser
    from dic.store import db
    from dic.tty import DicError, die

    try:
        # The command name must be argv[0], because argparse's subparsers
        # reject an unknown first word: a word that is not a command name is
        # the call's prompt, and "call" is prepended to say so.
        argv = sys.argv[1:]
        # A named model deserves a page about that model and not about
        # the whole command tree: `dic -m NAME --help` prints the
        # resolved entry -- what a call would send, which key it needs,
        # what its price rules are -- and answers before argparse's own
        # --help has a chance to print the tree instead.
        if model_help(argv, os.environ):
            return
        p = parser()
        # -h and --help name the whole tree of commands, so they answer with
        # both halves of it: the list of commands, and the call's own
        # options below it, because the call is the default command and
        # `dic PROMPT` is what a reader is usually after.  argparse's own
        # --help action is what prints the second half, and it exits 0 from
        # inside the call below -- past the os._exit at the end of this
        # function, and through the interpreter's own teardown, which is
        # what flushes it.
        if argv[:1] in (["-h"], ["--help"]):
            p.print_help()
            print("\nthe `call` command, the default one --"
                  " `dic PROMPT` is `dic call PROMPT`:\n")
            p.parse_args(["call", "--help"])
        if not argv or argv[0] not in commands.COMMANDS:
            argv = ["call", *argv]
        args = vars(p.parse_args(argv))
        command = args.pop("command")
        if command == "call":
            # The prompt is one argument.  An option dic does not know is an
            # error and not a prompt word: a wrapper's own instructions travel
            # in --prepend-prompt, so a misspelled flag is never sent to a
            # model as prose.
            prompt = args.pop("prompt") or ""
            if not sys.stdin.isatty():
                piped = sys.stdin.read()
                prompt = (f"{prompt}\n\n{piped}"
                          if (prompt and piped.strip()) else (prompt or piped))
            dic(prompt, **args, t_start=T0, env=os.environ,
                out=sys.stdout, err=sys.stderr)
        else:
            # A readout answers from the database, so its connection and its
            # parsed config cache are opened here and never inside a call.
            models_file = args.pop("models_file", None)
            args.pop("verbosity", None)
            args.pop("quiet", None)
            conn = db(os.environ)
            config.sync(conn, os.environ, models_file)
            sys.stdout.write(commands.COMMANDS[command](conn, os.environ,
                                                        **args))
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
