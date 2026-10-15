"""Extractors: a file's text, as the tree of chunks that compose it.

A file is a tree.  Its root is the file itself and its children are the
sections, functions, tables and paragraphs that compose it, so that every
consumer -- the walk as much as the retriever -- reads the same structure
and a selection can name a piece of a file and not only the whole of it.

An extractor is a function `(path, text) -> [Chunk]` returning the tree
as a flat list in tree order: the root first, then each child by `ord`.
There is no registry; importing is the registration.  The default is
`lines`, the universal fallback, which makes a file one chunk and nothing
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
import hashlib
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
