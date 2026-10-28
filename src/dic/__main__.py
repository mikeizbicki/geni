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

import contextlib, os, sys


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


def verbosity_of(argv):
    """How many -v the run asked for, before argparse has counted them.

    A readout that answers before argparse -- model_help -- still wants
    to know how much to say, and argparse's own count is not available
    until it has run.  The two spellings it accepts are read here: -v
    repeated, and -vvv glued.  -q wins over all of them, the same way it
    does for a call.

    >>> verbosity_of(["-v"])
    1
    >>> verbosity_of(["-vvv"])
    3
    >>> verbosity_of([])
    0
    >>> verbosity_of(["-v", "-q"])
    0
    """
    if any(a in ("-q", "--quiet") for a in argv):
        return 0
    count = 0
    for a in argv:
        if a == "--verbose":
            count += 1
        elif a.startswith("-") and not a.startswith("--"):
            count += a.count("v")
    return count


# The flags a model's page reads itself, before argparse.  Anything
# else -- including a flag argparse would accept for a call -- is
# refused: the page has nothing to do with a call, and a flag that
# arrived unnoticed would be a `--help` that lied about what it read.
MODEL_HELP_FLAGS = frozenset({
    "-h", "--help", "-v", "--verbose", "-q", "--quiet",
    "--refresh-options",
})


def reject_unknown(argv):
    """Raise DicError for a flag the model's page does not read.

    The page is answered before argparse, so argparse's own refusal of
    an unknown flag never runs: `dic -m foo --help --typo` would print
    a page and exit 0, when the caller typed a flag that does not
    exist.  The allowlist names every flag the page reads; a value
    after -m / --model / --models-file is consumed and never looked at
    as a flag, and a glued -vvv is one flag not three.

    >>> reject_unknown(["-m", "foo", "--help", "-vv"])
    >>> reject_unknown(["-m", "foo", "--help", "--refresh-options"])
    >>> reject_unknown(["-m", "foo", "--help", "--typo"])
    Traceback (most recent call last):
    ...
    dic.tty.DicError: unknown flag: --typo
    >>> reject_unknown(["-mfoo", "--help"])
    Traceback (most recent call last):
    ...
    dic.tty.DicError: unknown flag: -mfoo
    """
    from dic.tty import DicError

    value_flags = ("-m", "--model", "--models-file")
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in value_flags:
            i += 2
            continue
        if a.startswith("--") and "=" in a:
            name = a.split("=", 1)[0]
            if name not in MODEL_HELP_FLAGS:
                raise DicError(f"unknown flag: {name}")
            i += 1
            continue
        if a.startswith("-v") and set(a[1:]) == {"v"}:
            i += 1
            continue
        if a.startswith("-") and a not in MODEL_HELP_FLAGS:
            raise DicError(f"unknown flag: {a}")
        i += 1


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


def params_text(table):
    """One model's params as a table, required first then optional.

    A required param is one a call cannot be made without, so it comes
    before the ones that have defaults or simply do not apply; the two
    groups are each sorted by name so a page reads the same way twice.

    >>> print(params_text({"prompt": {"type": "string", "required": True},
    ...                    "n": {"type": "integer", "default": 1},
    ...                    "seed": {"type": "integer"}}))
    params
      required:
        prompt  string
      optional:
        n       integer  default=1
        seed    integer
    """
    if not table:
        return ""
    required = sorted(k for k, d in table.items() if d.get("required"))
    optional = sorted(k for k, d in table.items() if not d.get("required"))
    width = max(len(name) for name in table)

    def row(name):
        d = table[name]
        text = f"    {name:<{width}}  {d.get('type') or 'string'}"
        if "default" in d:
            text += f"  default={d['default']}"
        values = d.get("values") or []
        if values:
            text += "  [" + " ".join(str(v) for v in values) + "]"
        return text

    lines = ["params"]
    if required:
        lines.append("  required:")
        lines.extend(row(name) for name in required)
    if optional:
        lines.append("  optional:")
        lines.extend(row(name) for name in optional)
    return "\n".join(lines)


def params_for(model, env, *, refresh=False, verbose=0, progress=None):
    """The params a model can be asked with: its protocol's, and its own.

    Two sources.  The wire protocol supplies its options from the adaptor
    module's ``PARAMS`` dict -- ``openai-chat``'s temperature is the same
    on every provider that speaks it -- which is static and offline.  A
    model itself may publish properties the protocol does not, and only
    its adaptor knows how to ask for them; they are fetched lazily on
    the first ``--help`` and cached in a sidecar sqlite under the
    resolved ``model_id``, so the network is touched once per model per
    machine unless ``refresh`` says otherwise.  A key that is missing
    and a fetch that failed are both a DicError.

    ``progress`` is a ``dic.tty.Line`` the fetch paints while it runs,
    so a request that hangs is a clock and not a silent wait; a request
    that beats the meter's own delay paints nothing at all.

    Returns the merged params, a model's own entry winning over the
    protocol's of the same name, or ``{}`` when the protocol has no
    per-model facts to offer at all -- a chat model's page is its
    protocol's options and that is the whole answer.  A provider whose
    adaptor declares ``model_params`` must answer, and an answer it
    cannot give is raised: a page that printed no ``params`` section
    could otherwise not tell a model with none from a failed request.
    """
    import urllib.parse

    from dic import params as cache
    from dic.client import load_adaptor
    from dic.tty import DicError, report

    api_type = model.get("api_type") or "openai-chat"
    try:
        adaptor = load_adaptor(api_type, env)
    except DicError:
        return {}

    protocol = getattr(adaptor, "PARAMS", {}) or {}
    if not hasattr(adaptor, "model_params"):
        return dict(protocol)

    if not refresh:
        cached = cache.cached(env, model["model_id"])
        if cached is not None:
            report(verbose, 1,
                   f"params: cached for {model['model_id']}", env=env)
            return {**protocol, **cached}

    key_name = model.get("api_key_name")
    key = env.get(key_name) if key_name else None
    if not key:
        raise DicError(f"{model['model_id']}: {key_name} is not set;"
                       " export it to see this model's params")

    url = (getattr(adaptor, "model_params_url", lambda _: None))(model)
    host = urllib.parse.urlparse(url).hostname if url else ""
    row = None
    if progress is not None:
        row = progress.row("checking-param-api")
        row.begin("checking-param-api", host or "")
    report(verbose, 1, f"params: GET {url or model['model_id']}", env=env)
    try:
        # the adaptor's own reporting is graded by the same number: -v is
        # the CLI's and DIC_VERBOSITY is what anything else reads, so the
        # resolved grade is put where the adaptor can see it, and -v then
        # reaches the requests a page makes and not only the page itself
        facts = adaptor.model_params(model, key,
                                     dict(env, DIC_VERBOSITY=str(verbose)))
    finally:
        if row is not None:
            row.arrived()
    report(verbose, 1, f"params: {len(facts)} param(s)", env=env)
    # a cache write is not the answer: a read-only state directory or a
    # lock held elsewhere must not stop a page that already has its data
    with contextlib.suppress(Exception):
        cache.store(env, model["model_id"], facts)
    return {**protocol, **facts}


def model_help(argv, env):
    """``dic -m NAME --help``: the resolved entry of one named model.

    Answers when a model is named and a help flag is given, before
    argparse's own ``--help`` -- which prints the whole command tree and
    knows nothing about a model -- runs.  The page is the entry a call
    would use: the model's API name, its base, the variable its key is
    read from (and whether it is exported), the wire protocol, the
    declared price rules with dic's cost_input and cost_output shorthand
    already expanded, the options ``-o`` would merge over, and the
    params the model can be asked with -- its protocol's own options
    and, for a provider that publishes them, the model's own properties.

    An id that does not resolve is a DicError named here, not a page
    of nothing: the caller learns which id config.resolve refused.

    Returns True when it answered, so the caller knows not to fall
    through to argparse's own help; False when no model was named.
    """
    import json

    from dic import config
    from dic.store import db
    from dic.tty import Line

    if not any(a in ("-h", "--help") for a in argv):
        return False
    reject_unknown(argv)
    name = flag_value(argv, ("-m", "--model"))
    if name is None:
        return False

    verbose = verbosity_of(argv)
    refresh = "--refresh-options" in argv

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
    sys.stdout.flush()
    progress = Line(err=sys.stderr, env=env)
    try:
        table = params_for(model, env, refresh=refresh,
                           verbose=verbose, progress=progress)
    finally:
        progress.close()
    if table:
        print(params_text(table))
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
