"""Extractors: a file's text, as the tree of chunks that compose it.

A file is a tree.  Its root is the file itself and its children are the
sections, functions, tables and paragraphs that compose it, so that every
consumer -- the walk as much as the retriever -- reads the same structure
and a selection can name a piece of a file and not only the whole of it.

An extractor is a function `(path, text) -> [Chunk]` returning the tree
as a flat list in tree order: the root first, then each child by `ord`.
There is no registry; importing is the registration.  The default is
chosen by extension -- `.py` gets the Python tree, everything else the
universal fallback `lines`, which makes a file one chunk and nothing
else -- a format with no extractor of its own is still a tree, just a
tree with only a root.

Chunk identity is a content hash of what the chunk says:

    chunk_id = sha256(breadcrumb + "\\n" + text)[:16]

so a chunk that did not change keeps its id and therefore its vector, and
a rename costs one new path on a chunk that is otherwise the same.  The
breadcrumb is the path from the root to the node -- `path > heading > key`
for prose, `path::Name` for code -- and stands in for the path wherever a
chunk is printed, so a citation a model makes is still a path with a name
inside it.
"""
import ast, hashlib
from typing import NamedTuple


class Chunk(NamedTuple):
    """One node of a file's tree.

    A file is the root; a section, a function, a paragraph are children.
    `parent` is the chunk_id of the enclosing node, or None for the root,
    so a vector survives a reshuffle of the tree and a chunk that is
    re-parented keeps its id.  `ord` is the position among siblings.
    `meta` is a small dict the extractor fills with what its own format
    already knows: kind, name, line, heading path, RFC 6901 pointer.
    """
    path: str
    chunk_id: str
    breadcrumb: str
    text: str
    kind: str
    ord: int
    parent: str | None
    meta: dict


def identify(breadcrumb, text):
    """The content hash that names a chunk.

    A chunk is named by what it says and not by where it sits.  The
    breadcrumb is the path from the root to the node and the text is the
    chunk's own contents; the first sixteen hex digits are more than
    enough to make a collision impossible in practice.

    >>> identify("a.py", "x = 1\\n") == identify("a.py", "x = 1\\n")
    True
    >>> identify("a.py", "x = 1\\n") == identify("a.py", "x = 2\\n")
    False
    """
    data = (breadcrumb + "\n" + text).encode("utf-8", "replace")
    return hashlib.sha256(data).hexdigest()[:16]


def lines(path, text):
    """The universal fallback: a file is one chunk, and that chunk is the file.

    A format with no extractor of its own is still a tree; the tree is
    just the root and nothing else, so a file prints and hashes exactly
    as it did before there were extractors at all.  The breadcrumb of a
    root is the path itself, which is what makes that true.

    >>> [(c.breadcrumb, c.kind, c.ord, c.parent) for c in lines("a.py", "x")]
    [('a.py', 'file', 0, None)]
    """
    return [Chunk(path=path, chunk_id=identify(path, text),
                  breadcrumb=path, text=text, kind="file", ord=0,
                  parent=None, meta={})]


def _source(text, node, src):
    """The source of an AST node, sliced by its own line numbers.

    `ast.get_source_segment` needs a `col_offset` on every node, which
    the outer node of a decorator and some synthesized nodes do not
    carry; the line numbers always are there, and a chunk is a region of
    lines and not of columns.
    """
    return "".join(src[node.lineno - 1:node.end_lineno])


def _kind_of(node):
    """The chunk kind an AST node makes, or None if it makes no chunk.

    A module-level `if` or `try` is not a section of the tree; a class
    and a function are the two the SPEC's `file -> class -> def` names,
    and everything else stays where it is.
    """
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return "def"
    if isinstance(node, ast.ClassDef):
        return "class"
    return None


def _docstring_node(node):
    """The docstring expression of a def, class or module, or None.

    Python says a docstring is the first statement of the body when that
    statement is a bare string literal; the AST says that is an `Expr`
    wrapping a `Constant` wrapping a `str`, and all three are needed to
    tell it from `"".join([])` handing a string to a call.
    """
    if not node.body:
        return None
    first = node.body[0]
    if not isinstance(first, ast.Expr):
        return None
    value = first.value
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
        return None
    return first


def _head(text, node, src):
    """A def's signature and docstring, without the statements below them.

    That is what `--elide kind=body` leaves behind: a signature says how
    to call it and a docstring says what it does, and the statements
    between them are the implementation a summary does not need.  A def
    with no code body has no body to drop, so its head is its whole
    source and the elision is a no-op.
    """
    body = node.body
    if not body:
        return _source(text, node, src)
    i = 1 if _docstring_node(node) is not None else 0
    if i >= len(body):
        return _source(text, node, src)
    cut = body[i].lineno - 1
    if cut <= node.lineno - 1:
        return _source(text, node, src)
    return "".join(src[node.lineno - 1:cut])


def _tail(text, module, src):
    """The file's own lines: everything no def or class covers.

    Module a file is a root and the defs it holds are its children, so
    the root cannot be the whole text -- that would say every definition
    twice.  What is left after the definitions are lifted out is the
    imports, the module docstring and the top-level code, which is what
    a reader of the file wants above the definitions anyway.
    """
    covered = set()
    for node in module.body:
        if _kind_of(node) is not None:
            covered.update(range(node.lineno - 1, node.end_lineno))
    return "".join(line for i, line in enumerate(src) if i not in covered)


def _gather(out, path, text, src, nodes, prefix, parent):
    """Walk one body and append a chunk for every class or def it holds."""
    for nth, node in enumerate(nodes):
        kind = _kind_of(node)
        if kind is None:
            continue
        body = _source(text, node, src)
        crumb = f"{prefix}::{node.name}"
        cid = identify(crumb, body)
        meta = {"name": node.name, "line": node.lineno}
        head = _head(text, node, src)
        if head != body:
            meta["head"] = head
        out.append(Chunk(path=path, chunk_id=cid, breadcrumb=crumb,
                         text=body, kind=kind, ord=nth, parent=parent,
                         meta=meta))
        doc = _docstring_node(node)
        if doc is not None:
            doc_body = _source(text, doc, src)
            doc_crumb = f"{crumb}#doc"
            out.append(Chunk(path=path,
                             chunk_id=identify(doc_crumb, doc_body),
                             breadcrumb=doc_crumb, text=doc_body,
                             kind="docstring", ord=nth, parent=cid,
                             meta={}))
        _gather(out, path, text, src, node.body, crumb, cid)


def python(path, text):
    """The Python tree: file -> class and def -> docstring.

    A syntax error falls back to `lines`, because a file Python cannot
    parse is a file the walk still has to render, and a tree with only a
    root is a better answer than no answer.

    >>> [c.kind for c in python("a.py", "def f():\\n    pass\\n")]
    ['file', 'def']
    >>> [c.breadcrumb for c in python("a.py",
    ...     "class C:\\n    def m(self):\\n        pass\\n")]
    ['a.py', 'a.py::C', 'a.py::C::m']
    """
    try:
        module = ast.parse(text)
    except SyntaxError:
        return lines(path, text)
    src = text.splitlines(keepends=True)
    tail = _tail(text, module, src)
    root = Chunk(path=path, chunk_id=identify(path, tail),
                 breadcrumb=path, text=tail, kind="file", ord=0,
                 parent=None, meta={})
    out = [root]
    _gather(out, path, text, src, module.body, path, root.chunk_id)
    return out


# What a path chooses for itself: `.py` gets the Python tree, everything
# else the universal fallback, which is always a tree, just a root and
# no children.
def by_extension(path, text):
    """The extractor an extension chooses, or `lines` for a stranger.

    >>> [c.kind for c in by_extension("a.py", "x = 1\\n")]
    ['file']
    >>> [c.kind for c in by_extension("a.txt", "hello\\n")]
    ['file']
    """
    if path.endswith(".py") or path.endswith(".pyi"):
        return python(path, text)
    return lines(path, text)
