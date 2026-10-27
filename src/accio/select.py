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

Two rankers sit beside the lenses and are the same for every lens that
has more than one list to look at: `rrf` fuses ranked lists without
needing a shared score space, and `pagerank` spreads a fused signal
along the parent edges the extract and the index already record.
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


# RRF's constant.  The SPEC's 60: the point past which a lower-ranked
# list adds little, so a chunk high on one list and low on another
# cannot be washed out by the depth of the list it is low on.
RRF_K = 60

# Personalized PageRank's constants, from the SPEC's "twenty
# iterations" and the classical damping.  LAMBDA says how much of the
# final score the graph contributes: the seed is the query's reading
# of a chunk and pagerank is the tree's friendly correction.
ALPHA = 0.15
ITERATIONS = 20
LAMBDA = 0.5


def rrf(*lists, k=RRF_K):
    """Reciprocal rank fusion of ranked lists, ties by (path, chunk_id).

    The score is `sum over lists of 1/(k + rank)`, with rank one-based.
    A chunk that is first on two lists scores twice what a chunk that is
    first on one scores, which is what "hybrid beats either alone" means
    at the arithmetic.  The tie on (path, chunk_id) is the same tie
    every other ranker breaks, and it is what keeps the output
    byte-identical between two runs over the same tree.

    >>> from accio.extract import lines
    >>> a = lines("a.py", "x")[0]
    >>> b = lines("b.py", "y")[0]
    >>> [c.path for c in rrf([a, b])]
    ['a.py', 'b.py']
    >>> # a is first on one list and last on the other; the scores are
    >>> # equal and the tie is broken on path.
    >>> [c.path for c in rrf([a, b], [b, a])]
    ['a.py', 'b.py']
    """
    score = {}
    seen = {}
    for lst in lists:
        for rank, chunk in enumerate(lst, start=1):
            cid = chunk.chunk_id
            score[cid] = score.get(cid, 0.0) + 1.0 / (k + rank)
            seen.setdefault(cid, chunk)
    return sorted(seen.values(),
                  key=lambda c: (-score[c.chunk_id], c.path, c.chunk_id))


def pagerank(chunks, seeds, alpha=ALPHA, iterations=ITERATIONS):
    """Personalized PageRank over the parent edges of the chunk tree.

    The graph is what the extract and the index already say: a class
    and a def under the file, a docstring under its def, and a file
    under the READMEs the walk summoned above it.  `seeds` is a
    `chunk_id -> weight` map of what already matched -- the fused list
    -- and the return is a `chunk_id -> score` map summing to one.

    The edges are followed in both directions.  A def that matches a
    query is not more relevant than the file it lives in because the
    query happened to name the def, and a README that matches is worth
    something to every file beneath it.  A directed walk would give
    one of those and not the other, and the SPEC calls pagerank a
    redistribution of an existing signal, which is what symmetric
    edges make it.

    Iteration: `new = alpha*seed + (1-alpha)*W^T r`, then normalized to
    one so a dangling chunk -- one with no parent and no child --
    loses no mass.  Twenty iterations is the SPEC's number and one at
    which a thousand-chunk graph is at its fixed point.
    """
    chunks = list(chunks)
    ids = [c.chunk_id for c in chunks]
    known = set(ids)
    roots = {c.path: c.chunk_id for c in chunks if c.kind == "file"}
    neighbours = {cid: [] for cid in ids}
    for chunk in chunks:
        cid = chunk.chunk_id
        parent_path = chunk.meta.get("parent_path")
        for source in (chunk.parent, roots.get(parent_path)):
            if not source or source == cid or source not in known:
                continue
            neighbours[cid].append(source)
            neighbours[source].append(cid)
    total = sum(seeds.values()) or 1.0
    seed = {cid: seeds.get(cid, 0.0) / total for cid in ids}
    r = dict(seed)
    for _ in range(iterations):
        new = {cid: alpha * seed[cid] for cid in ids}
        for cid, peers in neighbours.items():
            if not peers:
                continue
            share = (1.0 - alpha) * r.get(cid, 0.0) / len(peers)
            for peer in peers:
                new[peer] += share
        norm = sum(new.values()) or 1.0
        r = {cid: value / norm for cid, value in new.items()}
    return r


def select(chunks):
    """The identity lens: everything traversed is selected.

    >>> list(select([1, 2, 3]))
    [1, 2, 3]
    """
    return chunks


def unique(chunks):
    """Drop a chunk this run has already yielded, keyed by (path, chunk_id).

    The walk dedupes the paths it was given and every extractor returns a
    tree, so this is the invariant stated once: whatever reaches the
    prompt names each file once.  The key is the pair and not either
    half -- a content hash alone merges two files that happen to hold the
    same bytes, and a path alone drops the chunks of a file the caller
    asked to see into.

    >>> from accio.extract import lines, python
    >>> [c.breadcrumb for c in unique(list(lines("a.py", "x")) * 2)]
    ['a.py']
    >>> [c.breadcrumb for c in unique(python("a.py", "def f():\\n    pass\\n"))]
    ['a.py', 'a.py::f']
    """
    seen = set()
    for chunk in chunks:
        key = (chunk.path, chunk.chunk_id)
        if key in seen:
            continue
        seen.add(key)
        yield chunk


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
        force=False, embed_model=None, expand=0, expand_model=None,
        extractor=None):
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

    With an embedder, the ranking is the SPEC's ladder as far as it is
    funded: BM25, then the vector list, fused with RRF, then a
    personalized PageRank over the fused list.  Without one it is BM25
    alone, which is free.

    A tie in BM25 is broken on `(path, chunk_id)`, which is what keeps
    the output byte-identical between two runs over the same tree.
    """
    conn = index.open_(paths, index_dir)
    index.refresh(conn, paths, readmes, patterns, gitignore, force=force,
                  extractor=extractor)
    if expand:
        query = rewrite(query, model=expand_model, times=expand)
    if embed_model is None:
        return index.search(conn, query)
    index.embed(conn, embed_model)
    lexical = list(index.search(conn, query))
    vector = list(index.veclist(conn, embed_model, query))
    fused = rrf(lexical, vector)
    if not fused:
        return iter([])
    # the fused list's depth is a weight: a chunk near the top of both
    # lists seeds pagerank more than a chunk near the top of one.
    seeds = {c.chunk_id: float(len(fused) - i)
             for i, c in enumerate(fused)}
    top = max(seeds.values()) or 1.0
    spread = pagerank(index.all_chunks(conn), seeds)
    ranked = sorted(
        fused,
        key=lambda c: (-(seeds[c.chunk_id] / top
                         + LAMBDA * spread.get(c.chunk_id, 0.0)),
                       c.path, c.chunk_id))
    return iter(ranked)
