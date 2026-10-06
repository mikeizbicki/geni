"""The CLI half of dic: one Flag declaration per knob.

A parameter of `dic()` is annotated with a Flag, and the Flag supplies the
environment variable, the command-line argument, the --help line and the
--help section the argument lives in, so a new knob is one annotation and
nothing else.  The dest of every generated argument is the parameter name,
so argparse and `dic()` cannot drift apart.

A Flag that is a *readout* -- a question about the database dic answers
without calling a model -- says `mode=True`, and that one declaration then
supplies the exclusive check that refuses two readouts at once and the
--help section where they are visibly a family.  A new readout is a flag
and a function; nothing here has to be taught about it.

`client.py` imports `Flag`, `resolve` and `modes` from here, so nothing
expensive may be imported at module level: `inspect`, which reads the
signature, is imported inside `flags()`, and `argparse` only by the entry
point.
"""
import json

from dic.tty import DicError

# The --help sections, and the partition the mode check reads: a call flag
# belongs to a request, an output flag to the file the answer lands in, and
# a readout flag to a question the database answers without a model at all.
CALL = "the call"
OUTPUT = "where the answer goes"
MODE = "readouts (do not call a model)"


class Flag:
    """How one parameter of `dic()` is spelled on the command line and in $ENV.

    The parameter's own default is the not-given value -- None or False --
    and the Flag says how to name it: `short` and `long` are the option
    strings, `action` is "append", "count", "bool" or "yes/no" when the
    argument is not a plain value, and `env` is False for a knob whose
    variable means something other than the argument itself.  `group` is
    the --help section the option is listed under, and `mode` marks the
    flag as a readout: a question asked of the database, and never a call.
    """
    __slots__ = ("action", "env", "group", "help", "long", "metavar", "mode",
                 "short", "type")

    def __init__(self, help="", short="", long="", action="", type=str,  # noqa: A002
                 metavar="", env="", group=CALL, mode=False):
        self.help = help
        self.short = short
        self.long = long
        self.action = action
        self.type = type
        self.metavar = metavar
        self.env = env
        self.group = group
        self.mode = mode


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


_FLAGS = None


def flags():
    """{parameter: Flag} for every knob of `dic()`, in signature order.

    `inspect` is imported here and not at module level: this is the CLI's
    generator, and a library call must not pay for it.  The result is
    cached because `from_env`, `modes` and `parser` each want it and none
    of them changes what `dic()`'s signature says.
    """
    global _FLAGS
    if _FLAGS is None:
        import inspect
        from dic.client import dic
        _FLAGS = {name: p.annotation
                  for name, p in inspect.signature(dic).parameters.items()
                  if isinstance(p.annotation, Flag)}
    return _FLAGS


def modes(knobs):
    """The mode flags that were given, in signature order.

    A mode flag is a readout: it asks a question of the database and prints
    the answer, and it never calls a model.  The commands are mutually
    exclusive -- the answer to two is not one table -- so this list is at
    most one long, and `resolve` refuses a longer one.  The same list is
    `commands.BY_FLAG`'s key, so the help, the exclusion and the dispatch
    cannot disagree about which readout was asked.

    >>> modes({"stats": True, "models": False, "aliases": None})
    ['stats']
    """
    return [name for name, f in flags().items()
            if f.mode and knobs.get(name) not in (None, False)]


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
    no new line here.  Two readouts at once is the one combination that has
    no meaning, so it is refused here, once, with the names of both.

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
    given = modes(out)
    if len(given) > 1:
        raise DicError("pick one: " + ", ".join(
            "--" + name.replace("_", "-") for name in given))
    return out


def parser():
    """The argparse parser generated from `dic()`'s signature.

    Defaults are the not-given values and the environment is applied by
    `dic()` itself, so the command line, the environment and the model config
    meet in exactly one place.  Each flag is listed under its Flag's `group`,
    so --help's sections are the same partition the mode check reads and a
    readout is visibly not a knife to combine with a call.
    """
    import argparse
    p = argparse.ArgumentParser(prog="dic", description="talk to a chat model")
    p.add_argument("prompt", nargs="*", help="the prompt, or stdin")
    sections = {}

    def section(f):
        if not f.group:
            return p
        if f.group not in sections:
            sections[f.group] = p.add_argument_group(f.group)
        return sections[f.group]

    for name, f in flags().items():
        options = ([f.short] if f.short else []) + [f.long or _name(name)]
        variable = _variable(name, f)
        help_text = f.help + (f" (env: {variable})" if f.help and variable else "")
        empty = EMPTY[f.action]
        if f.action == "yes/no":
            section(f).add_argument(*options, dest=name, default=empty,
                                    help=help_text,
                                    action=argparse.BooleanOptionalAction)
        elif f.action == "bool":
            section(f).add_argument(*options, dest=name, default=empty,
                                    help=help_text, action="store_true")
        elif f.action == "count":
            section(f).add_argument(*options, dest=name, default=empty,
                                    help=help_text, action="count")
        elif f.action == "?":
            section(f).add_argument(*options, dest=name, default=empty,
                                    help=help_text,
                                    nargs="?", const="", metavar=f.metavar or None)
        else:
            section(f).add_argument(
                *options, dest=name, default=empty, help=help_text,
                type=f.type, metavar=f.metavar or None,
                action="append" if f.action == "append" else "store")
    return p
