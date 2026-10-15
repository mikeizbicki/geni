"""The CLI half of dic: one Flag declaration per knob.

A parameter of a command function is annotated with a Flag, and the Flag
supplies the environment variable, the command-line argument, the --help
line and the --help section the argument lives in, so a new knob is one
annotation and nothing else.  The dest of every generated argument is the
parameter name, so argparse and the function cannot drift apart.

A Flag names the command it belongs to.  `command=""` is the call, whose
function is `client.dic`; `command="*"` is a global and goes on the parent
parser every command shares; any other value names a readout, and argparse
builds that readout a subparser of its own.  Exclusivity is therefore
structural: at most one command is parsed, so there is no mode check and no
"pick one" message.

`client.py` imports `Flag` and `resolve` from here, so nothing expensive
may be imported at module level: `inspect`, which reads a signature, is
imported inside `flags()`, and `argparse` only by `parser()`.
"""
import json

# The --help sections.
# CALL is the default group of every flag and the heading the call itself
# uses; a readout's flags are not API options, and are listed under the
# command's own name instead.
CALL = "API request options"
OUTPUT = "output postprocessing"
DEBUG = "debug"

# A flag every command accepts; it lives on the shared parent parser.
GLOBAL = "*"


class Flag:
    """How one parameter of a command function is spelled on the command line.

    The parameter's own default is the not-given value -- None or False --
    and the Flag says how to name it: `short` and `long` are the option
    strings, `action` is "append", "count", "bool", "?" or "yes/no" when the
    argument is not a plain value, and `env` is False for a knob whose
    variable means something other than the argument itself.  `group` is
    the --help section the option is listed under, and `command` names the
    command the option belongs to: "" is the call, "*" is every command.
    """
    __slots__ = ("action", "command", "env", "group", "help", "long",
                 "metavar", "short", "type")

    def __init__(self, help="", short="", long="", action="", type=str,  # noqa: A002
                 metavar="", env="", group=CALL, command=""):
        self.help = help
        self.short = short
        self.long = long
        self.action = action
        self.type = type
        self.metavar = metavar
        self.env = env
        self.group = group
        self.command = command


flag = Flag

# What "not given" looks like for each action; a knob still holding one of
# these falls back to its environment variable.
EMPTY = {"": None, "append": None, "count": None, "bool": False,
         "yes/no": None, "?": None}


def _name(name):
    """The long option string of a parameter name.

    >>> _name("pv_thinking"), _name("models_file")
    ('--pv-thinking', '--models-file')
    """
    return "--" + name.replace("_", "-")


def _variable(name, f):
    """The environment variable that supplies the parameter name, or None.

    >>> _variable("model", flag()), _variable("verbosity", flag(env=False))
    ('DIC_MODEL', None)
    """
    return None if f.env is False else (f.env or "DIC_" + name.upper())


def _decode(value):
    """An environment variable's string as the type it names, when it names one.

    >>> _decode("2"), _decode("true"), _decode("groq+qwen")
    (2, True, 'groq+qwen')
    """
    try:
        return json.loads(value)
    except ValueError:
        return value


_FLAGS = {}


def flags(func=None):
    """{parameter: Flag} for one command function's knobs, in signature order.

    `flags()` with no argument is the call's: `from_env` and `resolve` are
    about the call, and their declarations do not change with the command.
    `inspect` is imported here and not at module level, and the result is
    cached per function because `from_env`, `resolve` and `parser` each
    want one and none of them changes a signature.
    """
    if func is None:
        from dic.client import dic
        func = dic
    cached = _FLAGS.get(func)
    if cached is None:
        import inspect
        cached = {name: p.annotation
                  for name, p in inspect.signature(func).parameters.items()
                  if isinstance(p.annotation, Flag)}
        _FLAGS[func] = cached
    return cached


def from_env(mapping):
    """Every $DIC_<NAME> that is set, as `dic()` keyword arguments.

    A value is decoded as JSON when it parses, so DIC_EXTRACT=true and
    DIC_OPTION='["temperature=0"]' arrive with their proper types; anything
    else is the string it is.

    >>> from_env({"DIC_MODEL": "groq+qwen", "DIC_EXTRACT": "true", "HOME": "/"})
    {'model': 'groq+qwen', 'extract': True}
    """
    out = {}
    for name, f in flags().items():
        variable = _variable(name, f)
        if variable and mapping and variable in mapping:
            out[name] = _decode(mapping[variable])
    return out


def resolve(knobs, mapping):
    """knobs with each variable filling in the value that was not given.

    This is the whole of `argument > $DIC_<NAME> > model config`: a knob that
    still holds its not-given value is left to from_env(), and one that was
    given is kept.  It is one loop over the declarations, so a new knob needs
    no new line here.

    >>> k = resolve({"model": None, "extract": False}, {"DIC_MODEL": "x"})
    >>> k["model"], k["extract"], k["system"]
    ('x', False, None)
    """
    out = from_env(mapping)
    for name, f in flags().items():
        if knobs.get(name, EMPTY[f.action]) != EMPTY[f.action]:
            out[name] = knobs[name]
        else:
            out.setdefault(name, EMPTY[f.action])
    return out


def parser():
    """The argparse parser generated from the command functions' signatures.

    Each command is a subparser and the globals are on a parent parser every
    subparser shares, so `-v` works wherever it is typed.  The --help
    sections are made there too, in the order they are to print, because a
    subparser takes its parent's groups during the parents= merge and
    anything it makes afterwards is listed below them.  The command name
    must be argv[0]: argparse's subparsers reject an unknown first word,
    which is why the entry point pre-dispatches and prepends "call" when the
    first word is not a command name.
    """
    import argparse
    from dic import commands
    from dic.client import dic

    def add(where, name, f):
        options = ([f.short] if f.short else []) + [f.long or _name(name)]
        variable = _variable(name, f)
        help_text = f.help + (f" (env: {variable})" if f.help and variable else "")
        empty = EMPTY[f.action]
        if f.action == "yes/no":
            where.add_argument(*options, dest=name, default=empty,
                               help=help_text,
                               action=argparse.BooleanOptionalAction)
        elif f.action == "bool":
            where.add_argument(*options, dest=name, default=empty,
                               help=help_text, action="store_true")
        elif f.action == "count":
            where.add_argument(*options, dest=name, default=empty,
                               help=help_text, action="count")
        elif f.action == "?":
            where.add_argument(*options, dest=name, default=empty,
                               help=help_text,
                               nargs="?", const="", metavar=f.metavar or None)
        else:
            where.add_argument(
                *options, dest=name, default=empty, help=help_text,
                type=f.type, metavar=f.metavar or None,
                action="append" if f.action == "append" else "store")

    # Every heading is made on the parent, in the order it is to print: a
    # subparser takes its parent's groups during the parents= merge, which
    # runs before its own loop can make anything, so the order here is the
    # order everywhere.  A group nothing is added to prints nothing at all.
    parent = argparse.ArgumentParser(add_help=False)
    groups = {title: parent.add_argument_group(title)
              for title in (CALL, OUTPUT, DEBUG)}
    for name, f in flags(dic).items():
        if f.command == GLOBAL:
            add(groups[f.group], name, f)

    p = argparse.ArgumentParser(prog="dic", description="talk to a chat model",
                                parents=[parent])
    sub = p.add_subparsers(dest="command")
    for command, func in commands.COMMANDS.items():
        where = sub.add_parser(command, parents=[parent])
        if command == "call":
            where.add_argument("prompt", nargs="?",
                               help="the prompt, or stdin")
        if command == "show":
            where.add_argument("ref", nargs="?", help="the message to print")
        # the merge above already made the parent's groups here, so they are
        # found by title: a second group of the same name would print the
        # heading twice, because argparse does not look before it appends
        sections = {group.title: group for group in where._action_groups}
        # CALL is the default group of every flag, so a flag that still names
        # it belongs under this command's own heading rather than under the
        # API call's
        own = CALL if command == "call" else command
        for name, f in flags(func).items():
            if f.command == GLOBAL:
                continue
            heading = own if f.group == CALL else f.group
            if not heading:     # group="" is a flag with no section of its own
                add(where, name, f)
                continue
            if heading not in sections:
                sections[heading] = where.add_argument_group(heading)
            add(sections[heading], name, f)
    return p
