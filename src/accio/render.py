"""Rendering: the selected chunks, in the encoding the caller asked for.

The default is a list of `@@path` mentions, one per selected chunk and
no contents: `dic` reads `@@` back as a filename and hoists the file
into a header block at the head of the request, so `accio src/ | dic
'...'` arrives with the file contents `dic` was given.  `--format
prompt` is the self-contained text encoding: a chunk's breadcrumb, a
`---` fence, its text and a closing fence, so a reader -- a model, a
human, `less` -- can see where one chunk stops, and so that the output
is the encoding files-to-prompt already produces.  `paths`, `objects`
and `wc` are projections of the same selection, and `FORMATS` is their
catalog: the selection decides what, and this module decides only how.
"""
import json, re


def _one(chunk):
    """The rendered form of one chunk.

    One place writes a chunk -- the breadcrumb line, the fence, the text,
    the newline below the text that is written and never assumed, and the
    closing fence -- so that the counter a budget reads and the bytes a
    reader sees cannot drift apart.
    """
    text = chunk.text if chunk.text.endswith("\n") else chunk.text + "\n"
    return f"{chunk.breadcrumb}\n---\n{text}---\n"


def render(chunks, out):
    """Write selected chunks as the one format: breadcrumb, fences, text.

    A reader -- a model, a human, `less` -- can see where one chunk stops
    because the fences close it, and so the output is the encoding
    files-to-prompt already produces.  A fence closes every chunk, so the
    last line of the whole run is always `---`.  The newline below the
    text is written and never assumed: a chunk whose last line has no
    newline of its own must not run into the closing fence.

    >>> import io
    >>> from accio.extract import lines
    >>> stream = io.StringIO()
    >>> render(lines("a.py", "x = 1\\n"), stream)
    >>> stream.getvalue()
    'a.py\\n---\\nx = 1\\n---\\n'
    >>> stream = io.StringIO()
    >>> render(lines("b.txt", "no newline"), stream)
    >>> stream.getvalue()
    'b.txt\\n---\\nno newline\\n---\\n'
    """
    for chunk in chunks:
        out.write(_one(chunk))


def elide(chunks, what):
    """A rewrite: drop the named part of each chunk and keep the node.

    `what="body"` drops the body of any chunk that carries a head -- a
    def or a class -- so a Python file renders as an API summary: the
    signature, the docstring, and none of the statements below them.  A
    chunk with no head renders as itself.  A rewrite decides what text
    each chunk shows, so the node, its chunk_id, its order and its count
    are untouched, and the elided stream is the same selection rendered
    differently and not a new one.

    >>> from accio.extract import python
    >>> src = "def f(a):\\n    '''doc.'''\\n    return a\\n"
    >>> [(c.breadcrumb, c.text) for c in elide(python("a.py", src), "body")]
    [('a.py', ''), ('a.py::f', "def f(a):\\n    '''doc.'''\\n"), ('a.py::f#doc', "    '''doc.'''\\n")]
    """
    for chunk in chunks:
        if what == "body":
            head = chunk.meta.get("head")
            if head is not None:
                yield chunk._replace(text=head)
                continue
        yield chunk


def paths(chunks, out):
    """Write the breadcrumb of each selected chunk, and no contents.

    >>> import io
    >>> from accio.extract import lines
    >>> stream = io.StringIO()
    >>> paths(lines("a.py", "x = 1\\n") + lines("b.txt", "y"), stream)
    >>> print(stream.getvalue(), end="")
    a.py
    b.txt
    """
    for chunk in chunks:
        out.write(f"{chunk.breadcrumb}\n")


def at_paths(chunks, out):
    """Write each selected chunk as an @@ mention, and no contents.

    The mention is the chunk's *path*, not its breadcrumb: dic reads
    `@@a.py` as a filename and hoists that whole file into a header
    block at the head of the request, and `@@a.py::parse` names no file.
    A file with several chunks therefore names its path several times;
    dic's dedup is by content hash and hoists it once.

    >>> import io
    >>> from accio.extract import lines, python
    >>> stream = io.StringIO()
    >>> at_paths(lines("a.py", "x = 1\\n"), stream)
    >>> print(stream.getvalue(), end="")
    @@a.py
    >>> stream = io.StringIO()
    >>> at_paths(lines("a.py", "x = 1\\n") + lines("b.txt", "y"), stream)
    >>> print(stream.getvalue(), end="")
    @@a.py
    @@b.txt
    >>> stream = io.StringIO()
    >>> at_paths(python("a.py", "def f():\\n    pass\\n"), stream)
    >>> print(stream.getvalue(), end="")
    @@a.py
    @@a.py
    >>> stream = io.StringIO()
    >>> at_paths([], stream)
    >>> stream.getvalue()
    ''
    """
    for chunk in chunks:
        out.write(f"@@{chunk.path}\n")


def objects(chunks, out):
    """Write one JSON object per selected chunk, one to a line.

    The record an object carries is the chunk's own fields, so a caller
    that wants `(path, chunk_id)` pairs reads them as data and not by
    parsing fences.  One line per chunk keeps the stream a stream: the
    objects arrive as the walk proceeds, and a reader that closes the
    pipe early still has a prefix that parses on its own.

    The keys are written in this order and the escaping is ASCII, so the
    same tree prints the same bytes and two runs can still be diffed.

    >>> import io, json
    >>> from accio.extract import lines
    >>> stream = io.StringIO()
    >>> objects(lines("a.py", "x = 1\\n"), stream)
    >>> parsed = json.loads(stream.getvalue())
    >>> (parsed["path"], parsed["breadcrumb"], parsed["kind"], parsed["ord"])
    ('a.py', 'a.py', 'file', 0)
    >>> parsed["text"]
    'x = 1\\n'
    >>> len(parsed["chunk_id"])
    16
    """
    for chunk in chunks:
        out.write(json.dumps({"path": chunk.path,
                              "chunk_id": chunk.chunk_id,
                              "breadcrumb": chunk.breadcrumb,
                              "kind": chunk.kind,
                              "ord": chunk.ord,
                              "text": chunk.text}) + "\n")


def wc(chunks, out):
    """Write selected chunks as `wc` does, and one token estimate.

    The three numbers are the three `wc -l -w -c` reports, counted on the
    *rendered* chunk and not on the file it came from: the breadcrumb,
    the two fences and the newline below the text are lines and bytes the
    prompt pays for, and the budget that reads this same counter is
    truncating a prompt and not a file.

    The token count is not knowable here: it depends on the tokenizer and
    the model, and neither is accio's business.  `bytes/4` is the usual
    rule of thumb for English and is exact on nothing; it is printed as
    an estimate, marked with `~`, and is a proxy for size rather than a
    prediction of cost.

    >>> import io
    >>> from accio.extract import lines
    >>> stream = io.StringIO()
    >>> wc(lines("a.py", "x = 1\\n") + lines("b.txt", "no newline"), stream)
    >>> print(stream.getvalue(), end="")
          4       6      19 a.py
          4       5      25 b.txt
          8      11      44 total
    ~11 tokens (bytes/4)
    """
    lines = words = size = 0
    for chunk in chunks:
        n = measure(chunk, "lines")
        w = measure(chunk, "words")
        b = measure(chunk, "bytes")
        out.write(f"{n:7d} {w:7d} {b:7d} {chunk.breadcrumb}\n")
        lines += n
        words += w
        size += b
    out.write(f"{lines:7d} {words:7d} {size:7d} total\n")
    out.write(f"~{size // 4} tokens (bytes/4)\n")


# What `--target` can say, and how much a chunk costs.  A count is on the
# *rendered* stream -- the breadcrumb, the two fences and the newline
# below the text included -- because that is the number that costs, and
# one counter here means `--format wc` and `--target` cannot disagree
# about what they are measuring.
_SCALES = {"": 1, "k": 1024, "m": 1024 * 1024}
_UNITS = {"": "bytes", "B": "bytes", "T": "tokens", "W": "words",
          "L": "lines", "C": "chunks"}
_SIZE = re.compile(r"^(\d+(?:\.\d+)?)([kKmM]?)([A-Z]?)$")


def parse_target(text):
    """Parse a `SIZE[UNIT]` argument into a count and a unit.

    Bytes are the default and `k` and `m` scale by 1024, so the case of
    the suffix distinguishes the scale from the unit: `8k` is eight
    kibibytes and `8kT` is eight thousand and change tokens.  The unit
    letters are uppercase because `b` would be a bit, and nobody means
    bits here.

    >>> parse_target("8192")
    (8192, 'bytes')
    >>> parse_target("8k")
    (8192, 'bytes')
    >>> parse_target("8kT")
    (8192, 'tokens')
    >>> parse_target("200L")
    (200, 'lines')
    >>> parse_target("50C")
    (50, 'chunks')
    """
    match = _SIZE.match(text.strip())
    if match is None:
        raise ValueError(f"not a size: {text!r}")
    number, scale, unit = match.groups()
    if unit not in _UNITS:
        raise ValueError(f"not a size: {text!r}")
    count = float(number) * _SCALES[scale.lower()]
    if count == int(count):
        count = int(count)
    return count, _UNITS[unit]


def measure(chunk, unit="bytes"):
    """How much of the prompt one chunk costs, in the named unit.

    A chunk is measured as the `prompt` encoding even when another
    `--format` is chosen: the number a budget cares about is the number
    a model would be given.  `chunks` is the unit that says the selection
    is bounded by a count and not by a size.

    >>> from accio.extract import lines
    >>> c = lines("a.py", "x = 1\\n")[0]
    >>> measure(c, "bytes"), measure(c, "lines"), measure(c, "tokens")
    (19, 4, 4)
    >>> measure(c, "chunks")
    1
    """
    if unit == "chunks":
        return 1
    rendered = _one(chunk)
    if unit == "lines":
        return rendered.count("\n")
    if unit == "words":
        return len(rendered.split())
    size = len(rendered.encode("utf-8"))
    if unit == "bytes":
        return size
    if unit == "tokens":
        return size // 4
    raise ValueError(f"not a unit: {unit!r}")


def budget(chunks, target, unit="bytes", fudge=1/4):
    """Truncate the selected list to about `target` worth of prompt.

    Chunks are emitted in the order they arrived -- rank order, for a
    `--rag` -- while the running total is at or below the target.  The
    chunk that crosses is included iff the overshoot is at or below
    `fudge` of the target and dropped otherwise, and either way the run
    stops there, so what is emitted is a prefix of the ranked list and
    not a scattered selection from it.  The first chunk is always
    emitted, so a run is never empty and `--target 1C` still says
    something.

    >>> from accio.extract import lines
    >>> a = lines("a.py", "x = 1\\n")[0]
    >>> b = lines("b.py", "y = 2\\n")[0]
    >>> [c.path for c in budget([a, b], 20, "bytes", 0)]
    ['a.py']
    >>> [c.path for c in budget([a, b], 35, "bytes", 0.25)]
    ['a.py', 'b.py']
    >>> [c.path for c in budget([a, b], 1, "bytes", 0)]
    ['a.py']
    """
    total = 0
    for index, chunk in enumerate(chunks):
        cost = measure(chunk, unit)
        if index == 0 or total + cost <= target:
            total += cost
            yield chunk
            continue
        if total + cost - target <= fudge * target:
            yield chunk
        return


# What `--format` can name, and the only place the encodings are listed:
# a new projection is a new function and a key here, and nothing in the
# CLI changes to make it reachable.
FORMATS = {"prompt": render, "paths": paths, "json": objects, "wc": wc,
           "at-paths": at_paths}
