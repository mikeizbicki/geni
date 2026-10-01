"""Tools: the python functions that --tools hands to the model.

A tool is a plain function, and dic never asks you to describe one twice.
`--tools pkg.mod:fn` imports `fn` and offers the model what the function
already says about itself: its name, the first paragraph of its docstring as
the description, and a JSON Schema built from its type annotations.  There is
no registry, no decorator and no plugin, because a function you can import is
a function you can call.

    --tools dic.tools.fs:ls      one function
    --tools dic.tools.fs:*       every public function in one module
    --tools pkg.mod.fn           dotted, when the module path is unambiguous

The dotted form is resolved by importing, longest path first, so `pkg.mod.fn`
is `pkg.mod` plus `fn` when `pkg.mod` is a module; `:` says the same thing
without the guess.  A module's `*` is what the module declares -- its
`__all__` when it has one, and otherwise the functions it defines -- never
everything its namespace holds, which is every name it imported.

Types are read, not guessed: a parameter with no annotation, a `*args`, or a
type with no JSON form is an error naming the function, because a parameter
the model cannot see the type of is one it will fill in wrongly.  A result
that json cannot encode is an error too, rather than a repr the model would
have to parse back.  A tool that *fails*, though, is not a failed call: its
message is handed to the model as the result, so the model can correct itself.

`dic.tool` is imported only when --tools is given, so an invocation that
offers no tools pays for none of this.  An MCP server is these same three
things -- a name, a description, a schema -- arriving over a pipe instead of
over an import, and is TODO: see SPEC.md, "Tools".
"""
import importlib, inspect, json, types
from pathlib import Path
from typing import (Annotated, Literal, Union, get_args, get_origin,
                    get_type_hints)

from dic.tty import DicError

# The JSON form of a python type.  A tool's signature is the model's API, so
# the types a tool may use are the ones JSON has; anything else is an error
# and not a silently wrong "string".
SCALARS = {str: "string", int: "integer", float: "number", bool: "boolean",
           Path: "string"}


def schema_of(annotation):
    """The JSON Schema of one annotation, or DicError if it has no JSON form.

    >>> schema_of(str), schema_of(list[int])
    ({'type': 'string'}, {'type': 'array', 'items': {'type': 'integer'}})
    >>> schema_of(Annotated[int, "how many"])
    {'type': 'integer', 'description': 'how many'}
    >>> schema_of(Literal["fast", "slow"]), schema_of(int | None)
    ({'enum': ['fast', 'slow']}, {'type': 'integer'})
    >>> schema_of(bytes)
    Traceback (most recent call last):
    ...
    dic.tty.DicError: bytes: no JSON form (use str, int, float, bool, list[X], dict, or Annotated[X, "why"])
    """
    origin = get_origin(annotation)
    if origin is Annotated:
        base, *meta = get_args(annotation)
        schema = schema_of(base)
        text = next((m for m in meta if isinstance(m, str)), None)
        return dict(schema, description=text) if text else schema
    if origin is Literal:
        return {"enum": list(get_args(annotation))}
    if origin is Union or origin is types.UnionType:
        # Optional[X] and X | None mean "not required", which the parameter's
        # own default already says; two real types have no one JSON form
        rest = [a for a in get_args(annotation) if a is not type(None)]
        if len(rest) != 1:
            raise DicError(f"{annotation}: no JSON form")
        return schema_of(rest[0])
    if annotation in SCALARS:
        return {"type": SCALARS[annotation]}
    if origin is list or annotation is list:
        args = get_args(annotation)
        return {"type": "array", "items": schema_of(args[0]) if args else {}}
    if origin is dict or annotation is dict:
        return {"type": "object"}
    raise DicError(f"{getattr(annotation, '__name__', annotation)}: no JSON"
                   " form (use str, int, float, bool, list[X], dict, or"
                   ' Annotated[X, "why"])')


def signature_schema(fn):
    """(description, parameters) of one tool function.

    The description is the first paragraph of the docstring: the rest of it is
    usage notes and doctests, which are written for a human.

    >>> def look(who: str, times: int = 1) -> list[str]:
    ...     '''Look someone up.
    ...
    ...     Details a model does not need to read.
    ...     '''
    >>> signature_schema(look)
    ('Look someone up.', {'type': 'object', 'properties': {'who': {'type': 'string'}, 'times': {'type': 'integer'}}, 'required': ['who'], 'additionalProperties': False})
    """
    description = (inspect.getdoc(fn) or "").split("\n\n", 1)[0].strip()
    if not description:
        raise DicError(f"{fn.__name__}: a tool needs a docstring, because the"
                       " model reads it to decide when to call it")
    try:
        hints = get_type_hints(fn)
    except Exception as e:
        raise DicError(f"{fn.__name__}: cannot read its annotations: {e}") from None
    properties, required = {}, []
    for name, parameter in inspect.signature(fn).parameters.items():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            raise DicError(f"{fn.__name__}: a tool is called by keyword, so it"
                           f" cannot take {name}")
        if name not in hints:
            raise DicError(f"{fn.__name__}: {name} has no type annotation, so"
                           " the model cannot be told what it takes")
        properties[name] = schema_of(hints[name])
        if parameter.default is parameter.empty:
            required.append(name)
    return description, {"type": "object", "properties": properties,
                         "required": required, "additionalProperties": False}


def describe(fn):
    """fn as a tool record: what the model is told, and how it is called.

    >>> def f(x: int) -> int:
    ...     '''Double x.'''
    ...     return 2 * x
    >>> describe(f)["name"], describe(f)["description"]
    ('f', 'Double x.')
    """
    description, parameters = signature_schema(fn)
    return {"name": fn.__name__, "description": description,
            "parameters": parameters, "fn": fn}


def module_tools(module):
    """Every tool a module declares, in source order.

    `__all__` when the module has one, and otherwise the functions the module
    defines: a module's namespace holds everything it imported, and `*` must
    not turn a helper `import json` into a tool.

    >>> import dic.tools.fs
    >>> [t["name"] for t in module_tools(dic.tools.fs)]
    ['ls']
    """
    names = (list(module.__all__) if hasattr(module, "__all__") else
             [name for name, value in vars(module).items()
              if inspect.isfunction(value)
              and value.__module__ == module.__name__])
    return [describe(getattr(module, name)) for name in names]


def split(spec):
    """(module, attribute) of one --tools value.

    `pkg.mod:fn` says it outright.  In the dotted form the boundary is found
    by importing: `pkg.mod.fn` is `pkg.mod` plus `fn` when `pkg.mod` imports
    and `pkg.mod.fn` does not.

    >>> split("dic.tools.fs:ls"), split("dic.tools.fs.ls"), split("dic.tools.fs.*")
    (('dic.tools.fs', 'ls'), ('dic.tools.fs', 'ls'), ('dic.tools.fs', '*'))
    """
    if ":" in spec:
        module, _, attribute = spec.partition(":")
        if not module or not attribute:
            raise DicError(f"bad --tools value: {spec} (expected MODULE:FUNC)")
        return module, attribute
    parts = spec.split(".")
    if len(parts) < 2:
        raise DicError(f"bad --tools value: {spec} (expected MODULE:FUNC)")
    for i in range(len(parts) - 1, 0, -1):
        name = ".".join(parts[:i])
        try:
            importlib.import_module(name)
        except ImportError as e:
            missing = getattr(e, "name", None) or ""
            if missing and not (name.startswith(missing)
                                or missing.startswith(name)):
                # a dependency of a module that does exist, not the module
                raise DicError(f"cannot import {spec}: {e}") from None
            continue
        return name, ".".join(parts[i:])
    raise DicError(f"cannot import a module of {spec}")


def load(module_name, spec):
    """The module of one --tools value, or DicError naming the value."""
    try:
        return importlib.import_module(module_name)
    except ImportError as e:
        raise DicError(f"cannot import {module_name} (from {spec}): {e}") from None


def one(spec):
    """The tools one --tools value names: one function, or a module's worth.

    >>> [t["name"] for t in one("dic.tools.fs:ls")]
    ['ls']
    """
    module_name, attribute = split(spec)
    module = load(module_name, spec)
    if attribute == "*":
        found = module_tools(module)
        if not found:
            raise DicError(f"{module_name}: no public functions to offer"
                           " as tools")
        return found
    obj = module
    for part in attribute.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            raise DicError(f"{module_name} has no {attribute}")
    if not inspect.isfunction(obj):
        raise DicError(f"{spec}: {type(obj).__name__}, and a tool is a function")
    return [describe(obj)]


def resolve(specs):
    """Every tool the --tools values name, in order, each name only once.

    Two tools of one name cannot both be offered -- the model asks by name, so
    one of them would silently answer -- and that is an error.

    >>> [t["name"] for t in resolve(["dic.tools.fs:*"])]
    ['ls']
    >>> resolve(["dic.tools.fs:ls", "dic.tools.fs:ls"])
    Traceback (most recent call last):
    ...
    dic.tty.DicError: ls: offered twice (dic.tools.fs:ls)
    """
    out, seen = [], {}
    for spec in specs:
        for record in one(spec):
            if record["name"] in seen:
                raise DicError(f"{record['name']}: offered twice"
                               f" ({seen[record['name']]})")
            seen[record["name"]] = spec
            out.append(record)
    return out


def call(record, arguments):
    """Run one tool and return the text the model reads back.

    A string is passed through as itself -- the common case is prose, and
    quoting it would make it json the model has to unescape -- and everything
    else is JSON.  Anything json cannot encode is an error, because the model
    can only read what json has a form for.

    >>> call({"name": "ls", "fn": lambda: ["a", "b"]}, {})
    '["a", "b"]'
    >>> call({"name": "say", "fn": lambda: "a b"}, {})
    'a b'
    >>> call({"name": "oops", "fn": lambda: b"x"}, {})
    Traceback (most recent call last):
    ...
    dic.tty.DicError: oops: its result is not JSON: Object of type bytes is not JSON serializable
    """
    result = record["fn"](**arguments)
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        raise DicError(f"{record['name']}: its result is not JSON: {e}") from None
