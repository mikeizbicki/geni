"""Selection: which of the traversed chunks survive.

The identity is the default -- every chunk the walk yields is a chunk
that is rendered, which is why `accio src/` feels like no flag at all.
`rag` is the first non-identity lens: the same walk, ranked by BM25
relevance to a query through the index `accio.index` keeps.  Both
produce chunks, so everything after them -- the renderer, the
projections -- is unchanged, and a later lens is a new function and
not a new pipeline.
"""
from accio import index


def select(chunks):
    """The identity lens: everything traversed is selected.

    >>> list(select([1, 2, 3]))
    [1, 2, 3]
    """
    return chunks


def rag(query, paths, readmes, patterns, gitignore, index_dir=None,
        force=False):
    """Rank the chunks of the given paths by BM25 relevance to query.

    The index is opened and refreshed from the same walk that plain
    `accio` would do, so `--rag 'q' src/` sees exactly the tree
    `accio src/` would print, only ordered by relevance instead of by
    walk.  The query is answered by sqlite's FTS5 -- the binary `dic`
    already depends on -- so there is no import, no API key and no
    embedding cost; a typo in a flag cannot make this spend.

    A tie in BM25 is broken on `(path, chunk_id)`, which is what keeps
    the output byte-identical between two runs over the same tree.
    """
    conn = index.open_(paths, index_dir)
    index.refresh(conn, paths, readmes, patterns, gitignore, force=force)
    return index.search(conn, query)
