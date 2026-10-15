"""Selection: which of the traversed chunks survive.

The identity is the default -- every chunk the walk yields is a chunk
that is rendered, which is why `accio src/` feels like no flag at all.
`rag` is the first non-identity lens: the same walk, ranked by BM25
relevance to a query through the index `accio.index` keeps.  Both
produce chunks, so everything after them -- the renderer, the
projections -- is unchanged, and a later lens is a new function and
not a new pipeline.

Two of the flags that tune the ranking cost money.  `--embed` fills
the vector column the index keeps and ranks by it; `--expand` asks the
generation model for a second phrasing of the query.  Both speak to
`dic` imported as a library and never as a subprocess, so there is one
ledger -- dic's -- and this is not a second one.
"""
from accio import index


# The prompt `--expand` sends.  A model told to explain its reasoning
# would spend the ask and not the answer, and the answer is the ask:
# one line, no preamble, the rewritten query alone.
REWRITE = (
    "Rewrite the following search query so it matches the way a"
    " programmer would have written the code it is looking for."
    " Reply with the rewritten query alone and nothing else.\n\n"
    "{q}\n"
)


def select(chunks):
    """The identity lens: everything traversed is selected.

    >>> list(select([1, 2, 3]))
    [1, 2, 3]
    """
    return chunks


def rewrite(query, model=None, times=1):
    """Ask dic for a second phrasing of the query.  Paid.

    `--expand 1` is one rewrite and `--expand 3` is three; here they
    are joined with the original by an explicit OR, which is what
    expansion means at the FTS5 grammar: whitespace-separated terms are
    read as an implicit AND, so a rewrite sharing no token with the
    original would narrow the search instead of widening it.  Step 11
    fuses the resulting lists with RRF, which is already there by then,
    and this is the stub that proves the boundary works.
    """
    import dic  # the deliberate exception to the zero-dependency rule
    parts = [query]
    for _ in range(times):
        parts.append(dic.chat(model, REWRITE.format(q=query)))
    # An OR and not a space: FTS5 ANDs the whitespace-separated terms,
    # so a space here would *restrict* the search and not expand it.
    return " OR ".join(p.strip() for p in parts if p.strip())


def rag(query, paths, readmes, patterns, gitignore, index_dir=None,
        force=False, embed_model=None, expand=0, expand_model=None):
    """Rank the chunks of the given paths by BM25 relevance to query.

    The index is opened and refreshed from the same walk that plain
    `accio` would do, so `--rag 'q' src/` sees exactly the tree
    `accio src/` would print, only ordered by relevance instead of by
    walk.  The query is answered by sqlite's FTS5 -- the binary `dic`
    already depends on -- so there is no import, no API key and no
    embedding cost; a typo in a flag cannot make this spend.

    Two things can make it spend.  `embed_model` fills the vector
    column (what `--embed` paid for) and ranks with it, one `dic.embed`
    call for the query and a dot product per row.  `expand` rewrites
    the query `dic.chat` many times before either ranking runs; the
    rewrite is a lens of its own, and step 11 fuses the two lists with
    RRF rather than joining the text here.

    A tie in BM25 is broken on `(path, chunk_id)`, which is what keeps
    the output byte-identical between two runs over the same tree.
    """
    conn = index.open_(paths, index_dir)
    index.refresh(conn, paths, readmes, patterns, gitignore, force=force)
    if expand:
        query = rewrite(query, model=expand_model, times=expand)
    if embed_model is not None:
        index.embed(conn, embed_model)
        return index.veclist(conn, embed_model, query)
    return index.search(conn, query)
