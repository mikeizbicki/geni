"""The CLI half of dic: one Flag declaration per knob.

A parameter of `dic()` is annotated with a Flag, and the Flag supplies the
environment variable, the command-line argument and the --help line, so a new
knob is one annotation and nothing else.  The dest of every generated
argument is the parameter name, so argparse and `dic()` cannot drift apart.

`client.py` imports `Flag` and `resolve` from here, so nothing expensive may
be imported at module level: `inspect`, which reads the signature, is
imported inside `flags()`, and `argparse` only by the entry point.
"""
import json


class Flag:
    """How one parameter of `dic()` is spelled on the command line and in $ENV.

    The parameter's own default is the not-given value -- None or False --
    and the Flag says how to name it: `short` and `long` are the option
    strings, `action` is "append", "count", "bool" or "yes/no" when the
    argument is not a plain value, and `env` is False for a knob whose
    variable means something other than the argument itself.
    """
    __slots__ = ("action", "env", "help", "long", "metavar", "short", "type")

    def __init__(self, help="", short="", long="", action="", type=str,  # noqa: A002
                 metavar="", env=""):
        self.help = help
        self.short = short
        self.long = long
        self.action = action
        self.type = type
        self.metavar = metavar
        self.env = env


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


def flags():
    """{parameter: Flag} for every knob of `dic()`, in signature order.

    `inspect` is imported here and not at module level: this is the CLI's
    generator, and a library call must not pay for it.
    """
    import inspect
    from dic.client import dic
    return {name: p.annotation
            for name, p in inspect.signature(dic).parameters.items()
            if isinstance(p.annotation, Flag)}


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
    """The argparse parser generated from `dic()`'s signature.

    Defaults are the not-given values and the environment is applied by
    `dic()` itself, so the command line, the environment and the model config
    meet in exactly one place.
    """
    import argparse
    p = argparse.ArgumentParser(prog="dic", description="talk to a chat model")
    p.add_argument("prompt", nargs="*", help="the prompt, or stdin")
    for name, f in flags().items():
        options = ([f.short] if f.short else []) + [f.long or _name(name)]
        variable = _variable(name, f)
        help_text = f.help + (f" (env: {variable})" if f.help and variable else "")
        empty = EMPTY[f.action]
        if f.action == "yes/no":
            p.add_argument(*options, dest=name, default=empty, help=help_text,
                           action=argparse.BooleanOptionalAction)
        elif f.action == "bool":
            p.add_argument(*options, dest=name, default=empty, help=help_text,
                           action="store_true")
        elif f.action == "count":
            p.add_argument(*options, dest=name, default=empty, help=help_text,
                           action="count")
        elif f.action == "?":
            p.add_argument(*options, dest=name, default=empty, help=help_text,
                           nargs="?", const="", metavar=f.metavar or None)
        else:
            p.add_argument(*options, dest=name, default=empty, help=help_text,
                           type=f.type, metavar=f.metavar or None,
                           action="append" if f.action == "append" else "store")
    return p
