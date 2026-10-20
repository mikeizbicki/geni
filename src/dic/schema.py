"""JSON Schema dialects: read a --schema value and resolve the mime it makes.

A `--schema` value is read as one of:

    {   first non-space        jsonschema -- a literal JSON Schema
    an existing path           jsonschema -- read from that file
    str|int|float|bool         simonw_llm -- simonw's `llm` shorthand
    noul|choice|score|box      systemone -- the SystemOne dialect

The order is load-bearing.  `{` first, because a JSON Schema is never a
shorthand and no shorthand begins with `{`; a path second, because a
one-word shorthand can collide with a filename and the file is the less
surprising reading; the two shorthands last, and a shorthand mixing the
two type families is an error.  The dialect is inferred and never
flagged.

This module is the whole of the schema compilation and the mime rule.
`parse` reads a `--schema` value, `dialect` says which of the three
dialects it is, `mime` resolves the answer's mime type and says what a
manual `--mime-type` did to the schema it contradicts, and
`is_json_mime` and `printable` answer the two questions about a mime
type that the other two both need.

Nothing here calls a model, touches sqlite or reads a signal; `json` and
`os` are the only imports, so the doctests are the whole test.
"""
import json, os

from dic.tty import DicError

# The three dialects, as the strings the messages row stores.
JSONSCHEMA = "jsonschema"
SIMONW_LLM = "simonw_llm"
SYSTEMONE = "systemone"

# The two shorthands' leaf vocabularies.  They do not overlap, and that is
# what lets a shorthand be read as one dialect or the other without a flag.
LLM_SCALARS = {"str": "string", "int": "integer",
               "float": "number", "bool": "boolean"}
SYSTEMONE_LEAVES = ("noul", "choice", "score", "box")

# The two spellings of a JSONL answer.  `application/x-ndjson` is the
# registered one and `application/jsonl` is the de facto one; dic accepts
# both, and either is how a caller asks for one JSON value per line.
JSONL_MIMES = ("application/x-ndjson", "application/jsonl")

# The one key a JSONL answer's records are asked under.  Every provider's
# structured output requires a schema's root to be an object and none of
# them accepts a top-level array, so a stream of records is asked for as
# one object with them in it; dic unwraps that object on receipt.
JSONL_KEY = "jsonl"


def parse(text):
    """(schema, dialect) for a --schema value.

    A `{` first non-space is a JSON Schema literal and a path that exists
    is one read from that file; anything else is a shorthand, compiled by
    the dialect its type vocabulary names.

    >>> parse('{"type": "object"}')
    ({'type': 'object'}, 'jsonschema')
    >>> parse('name, age: int')
    ({'type': 'object', 'properties': {'name': {'type': 'string'}, 'age': {'type': 'integer'}}, 'required': ['name', 'age'], 'additionalProperties': False}, 'simonw_llm')
    >>> parse('billing: noul "Is this billing?"')
    ({'questions': {'billing': {'type': 'noul', 'instructions': 'Is this billing?'}}}, 'systemone')

    A shorthand mixing the two type families is an error:

    >>> parse('age: int, billing: noul "?"')
    Traceback (most recent call last):
    ...
    dic.tty.DicError: --schema: shorthand mixes the str|int|float|bool and noul|choice|score|box type families
    """
    if not text:
        raise DicError("--schema: the empty string names no schema")
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            return json.loads(stripped), JSONSCHEMA
        except ValueError as e:
            raise DicError(f"--schema: invalid JSON: {e}") from None
    if os.path.exists(text):
        try:
            with open(text) as f:
                return json.load(f), JSONSCHEMA
        except ValueError as e:
            raise DicError(f"--schema: {text}: invalid JSON: {e}") from None
    return _shorthand(stripped)


def dialect(text):
    """The dialect a --schema value is written in: see parse.

    >>> dialect('{"type": "object"}'), dialect('name: str')
    ('jsonschema', 'simonw_llm')
    >>> dialect('urgency: choice["low","high"] "How urgent?"')
    'systemone'
    """
    return parse(text)[1]


def mime(mime_type, schema, output=None):
    """(resolved mime, the schema that survived the manual --mime-type).

    A schema makes the answer `application/json`, so a manual JSON mime is
    the same request restated and a manual `text/*` is its negation: text
    drops the schema and is prose unconstrained.  Any other non-JSON mime
    with a schema is a contradiction and an error.

    >>> mime(None, {"type": "object"})
    ('application/json', {'type': 'object'})
    >>> mime("text/plain", {"type": "object"})
    ('text/plain', None)
    >>> mime("application/x-ndjson", {"type": "object"})
    ('application/x-ndjson', {'type': 'object'})
    >>> mime(None, None, "image/png")
    ('image/png', None)
    >>> mime("image/png", {"type": "object"})
    Traceback (most recent call last):
    ...
    dic.tty.DicError: --mime-type image/png contradicts a schema
    """
    if schema is None:
        return mime_type or output or "text/plain", None
    if mime_type is None:
        return "application/json", schema
    if mime_type.startswith("text/"):
        return mime_type, None
    if is_json_mime(mime_type):
        return mime_type, schema
    raise DicError(f"--mime-type {mime_type} contradicts a schema")


def is_json_mime(mime_type):
    """Whether a mime type names JSON or a JSON stream.

    >>> is_json_mime("application/json"), is_json_mime("text/plain")
    (True, False)
    >>> is_json_mime("application/x-ndjson"), is_json_mime("application/jsonl")
    (True, True)
    >>> is_json_mime("application/problem+json")
    True
    >>> is_json_mime("image/png")
    False
    """
    return (mime_type in JSONL_MIMES
            or mime_type.endswith("/json")
            or mime_type.endswith("+json"))


def printable(mime_type):
    """Whether an answer of this mime type has a terminal form.

    >>> printable("text/plain"), printable("application/json")
    (True, True)
    >>> printable("application/x-ndjson"), printable("image/png")
    (True, False)
    """
    return mime_type.startswith("text/") or is_json_mime(mime_type)


def wrap(schema, mime_type):
    """(schema, container): the schema as sent, and whether an object wraps it.

    A schema is JSON Schema, which cannot describe a stream of documents,
    and no provider's structured output accepts a top-level array: a JSONL
    answer is therefore asked for as one object with the records under
    `jsonl`, and each element is what the caller's schema constrains.
    `container` is 1 when the wrap ran and None when it did not, so the row
    can say the wire shape differed from what the caller wrote.

    >>> wrap({"type": "object"}, "application/json")
    ({'type': 'object'}, None)
    >>> wrap({"type": "object"}, "text/plain")
    ({'type': 'object'}, None)
    >>> wrap(None, "application/x-ndjson")
    (None, None)
    >>> wrapped, container = wrap({"type": "object"}, "application/jsonl")
    >>> container, wrapped["properties"]["jsonl"]["items"]
    (1, {'type': 'object'})
    """
    if schema is None or mime_type not in JSONL_MIMES:
        return schema, None
    return {"type": "object",
            "properties": {JSONL_KEY: {
                "type": "array", "items": schema,
                "description": "One object per item the prompt asks for."}},
            "required": [JSONL_KEY],
            "additionalProperties": False}, 1


# ------------------------------------------------------------- the shorthands


def _shorthand(text):
    """(schema, dialect) for a shorthand, its dialect inferred from its leaves."""
    parsed = [_field(part) for part in _fields(text)]
    if not parsed:
        raise DicError("--schema: an empty shorthand names no fields")
    families = {family for family in
                (_family_of(field) for field in parsed) if family}
    if len(families) > 1:
        raise DicError("--schema: shorthand mixes the str|int|float|bool"
                       " and noul|choice|score|box type families")
    which = families.pop() if families else SIMONW_LLM
    if which == SIMONW_LLM:
        return _llm_schema(parsed), SIMONW_LLM
    return _systemone_schema(parsed), SYSTEMONE


def _field(text):
    """One shorthand field as a dict of its parts."""
    head, criteria = _split_group(text, "{", "}")
    name, rest, optional = _split_name(head)
    if not name:
        raise DicError(f"--schema: {text!r} has no field name")
    box = _criteria(criteria) if criteria is not None else None
    enum = _enum_values(rest)
    if enum is not None:
        return {"name": name, "optional": optional, "type": ("enum", enum),
                "description": None, "criteria": box}
    rest, description = _split_quote(rest)
    return {"name": name, "optional": optional, "type": rest.strip(),
            "description": description, "criteria": box}


def _family_of(field):
    """simonw_llm, systemone, or None when the field's leaf does not say."""
    if isinstance(field["type"], tuple):
        return SIMONW_LLM
    return _family(field["type"], field["description"], field["criteria"],
                   field["name"])


def _family(type_text, description, criteria, name):
    """The shorthand family one field's type belongs to.

    >>> _family("int", None, None, "age")
    'simonw_llm'
    >>> _family("noul", "Is it?", None, "billing")
    'systemone'
    >>> _family("", "Is it?", None, "billing")
    'systemone'
    >>> _family("", None, None, "name") is None
    True
    >>> _family("nope", None, None, "x")
    Traceback (most recent call last):
    ...
    dic.tty.DicError: --schema: x: 'nope' names no type
    """
    if type_text in LLM_SCALARS:
        return SIMONW_LLM
    if type_text in SYSTEMONE_LEAVES:
        return SYSTEMONE
    if type_text.startswith("choice[") or type_text.startswith("box("):
        return SYSTEMONE
    if type_text:
        raise DicError(f"--schema: {name}: {type_text!r} names no type")
    if description is not None or criteria is not None:
        return SYSTEMONE
    return None


def _llm_schema(parsed):
    """The JSON Schema of a simonw_llm shorthand: what `strict: true` wants."""
    properties, required = {}, []
    for field in parsed:
        if field["description"] is not None or field["criteria"] is not None:
            raise DicError(f"--schema: {field['name']}: a description belongs"
                           " to the systemone dialect")
        properties[field["name"]] = _llm_leaf(field)
        if not field["optional"]:
            required.append(field["name"])
    return {"type": "object", "properties": properties,
            "required": required, "additionalProperties": False}


def _llm_leaf(field):
    """One simonw_llm property: a scalar, or an enum of strings."""
    if isinstance(field["type"], tuple):
        return {"enum": field["type"][1]}
    if not field["type"]:
        return {"type": "string"}
    return {"type": LLM_SCALARS[field["type"]]}


def _systemone_schema(parsed):
    """The questions dict of a systemone shorthand."""
    questions = {}
    for field in parsed:
        if field["optional"]:
            raise DicError(f"--schema: {field['name']}: every systemone"
                           " question is required (?name is an error)")
        questions[field["name"]] = _question(field)
    return {"questions": questions}


def _question(field):
    """One systemone question, compiled from its field."""
    kind, arg = _systemone_leaf(field)
    if kind == "box":
        instructions = _box_instruction(field["description"] or "", arg)
    else:
        instructions = field["description"] or ""
    question = {"type": kind, "instructions": instructions}
    if kind == "choice":
        question["choices"] = arg
    if kind == "noul" and field["criteria"]:
        question["criteria"] = field["criteria"]
    return question


def _systemone_leaf(field):
    """(type, argument) of one systemone question: noul, choice, score or box."""
    type_text = field["type"]
    if not type_text:
        return "noul", None
    if type_text in ("noul", "score"):
        return type_text, None
    if type_text.startswith("choice["):
        return "choice", _bracket_values(type_text[len("choice["):-1])
    if type_text.startswith("box("):
        return "box", type_text[len("box("):-1].strip()
    raise DicError(f"--schema: {field['name']}: {type_text!r} names no"
                   " systemone type")


def _box_instruction(description, asset):
    """A box question's instruction, the asset's name appended if absent.

    >>> _box_instruction("Is anything weird?", "pic.png")
    'Is anything weird?  pic.png'
    >>> _box_instruction("Is anything weird in pic.png?", "pic.png")
    'Is anything weird in pic.png?'
    """
    text = description.rstrip()
    return text if asset in text else f"{text}  {asset}".strip()


def _criteria(body):
    """A `{true: "...", false: "..."}` group as a dict of the two reasons.

    >>> _criteria('true: "advertising", false: "conversation"')
    {'true': 'advertising', 'false': 'conversation'}
    """
    out = {}
    for part in _fields(body):
        key, sep, value = part.partition(":")
        if not sep:
            raise DicError(f"--schema: criteria has no key:value: {part!r}")
        out[_unquote(key)] = _unquote(value)
    return out


def _bracket_values(body):
    """The list of values inside a `["low","high"]` group.

    >>> _bracket_values('"low","high"')
    ['low', 'high']
    """
    return [_unquote(piece) for piece in _fields(body)]


def _unquote(text):
    """text with one surrounding quote pair removed, if it has one.

    >>> _unquote('"a"'), _unquote("plain")
    ('a', 'plain')
    """
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


# --------------------------------------------------------------- the tokens


def _fields(text):
    """A shorthand split into its fields, a bracket's commas respected.

    >>> _fields('a, b: int, c "why"')
    ['a', 'b: int', 'c "why"']
    >>> _fields('x: choice["a","b"] "?"')
    ['x: choice["a","b"] "?"']
    >>> _fields('a, b, c')
    ['a', 'b', 'c']
    """
    out, depth, quote, piece = [], 0, None, ""
    for ch in text:
        if quote:
            piece += ch
            if ch == quote:
                quote = None
        elif ch in "\"'":
            piece += ch
            quote = ch
        elif ch in "([{":
            piece += ch
            depth += 1
        elif ch in ")]}":
            piece += ch
            depth -= 1
        elif ch == "," and depth == 0:
            if piece.strip():
                out.append(piece.strip())
            piece = ""
        else:
            piece += ch
    if piece.strip():
        out.append(piece.strip())
    return out


def _split_name(text):
    """(name, rest, optional) at the head of a field.

    >>> _split_name('age: int')
    ('age', 'int', False)
    >>> _split_name('?bio: str')
    ('bio', 'str', True)
    >>> _split_name('name "why"')
    ('name', '"why"', False)
    >>> _split_name('bare')
    ('bare', '', False)
    """
    text = text.strip()
    optional = text.startswith("?")
    if optional:
        text = text[1:].lstrip()
    for i, ch in enumerate(text):
        if ch == ":":
            return text[:i].strip(), text[i + 1:].strip(), optional
        if ch.isspace():
            return text[:i], text[i + 1:].strip(), optional
    return text, "", optional


def _split_quote(text):
    """(head, contents) where contents is a trailing quoted string.

    >>> _split_quote('score "How sure?"')
    ('score', 'How sure?')
    >>> _split_quote('plain')
    ('plain', None)
    """
    text = text.strip()
    if not text or text[-1] not in "\"'":
        return text, None
    quote = text[-1]
    i = len(text) - 2
    while i >= 0 and text[i] != quote:
        i -= 1
    if i < 0:
        return text, None
    return text[:i].rstrip(), text[i + 1:-1]


def _split_group(text, opener, closer):
    """(head, body) where body is a trailing opener...closer group.

    >>> _split_group('noul "?" {true: "a", false: "b"}', "{", "}")
    ('noul "?"', 'true: "a", false: "b"')
    >>> _split_group('plain text', "{", "}")
    ('plain text', None)
    """
    text = text.strip()
    if not text or text[-1] != closer:
        return text, None
    depth, quote = 0, None
    for i in range(len(text) - 1, -1, -1):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            continue
        if ch == closer:
            depth += 1
        elif ch == opener:
            depth -= 1
            if depth == 0:
                return text[:i].rstrip(), text[i + 1:-1]
    return text, None


def _enum_values(text):
    """The values of a `"a" or "b"` type, or None if text is not that form.

    >>> _enum_values('"a" or "b"')
    ['a', 'b']
    >>> _enum_values('score') is None, _enum_values('"a"') is None
    (True, True)
    """
    text = text.strip()
    if not (text.startswith('"') and text.endswith('"') and " or " in text):
        return None
    values = []
    for piece in text.split(" or "):
        piece = piece.strip()
        if not (piece.startswith('"') and piece.endswith('"')
                and len(piece) >= 2):
            return None
        values.append(piece[1:-1])
    return values
