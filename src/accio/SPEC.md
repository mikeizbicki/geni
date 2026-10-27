# accio SPEC

`accio` is a minimalist CLI tool for gathering the contents of files into one prompt.

"Accio" is Latin for "I summon", and is the word of the Summoning Charm: the thing you want is not in your hand, and one word brings it.  `accio` is that word for a directory of documents.

`accio` is the sibling of `dic`.  `dic` speaks to a model; `accio` decides what it is told.  They are separate programs to the shell and one process to the interpreter: whatever `accio` needs a model for -- an embedding, a query rewrite, nothing else -- it asks of `dic` as a library and never as a subprocess.  `dic` records the call in its ledger and `accio` does not keep a second one.

The composition at the shell is the design:

    accio src/ | dic "summarize this module"
    accio --rag "where is the retry budget enforced?" src/ | dic -

## The model

`accio` traverses a corpus, keeps what is *relevant*, orders what it keeps, cuts it to a *budget*, and renders it.  One pipeline, and every flag is a step of it:

    traverse -> select -> render

The walk alone is the identity selection: relevance is everything, the order is the walk, the budget is infinite.  `accio src/` is that identity and nothing else, which is why it feels like no flag at all.  `--rag 'q'` changes exactly one clause of the sentence -- what *relevant* means -- and nothing else about the program.

A **lens** is a pair `(predicate, ranking)` over the corpus.  A predicate keeps or drops; a ranking orders what survives.  Predicates intersect and rankings fuse, so the default lens is `(true, walk-order)` and a query is a lens of its own.  Every future semantic choice is a new lens, and adding one is adding a function, not a branch.

Lenses compose:

    (p1, r1) . (p2, r2) = (p1 & p2, RRF(r1, r2))

which is what makes `--rag --recent 7d` mean something without a new mechanism.

Three tools sit beside the lens and are not lenses:

- **Rewrite.**  A post-ranking transformation of node text.  `--elide kind=body` drops a function body after the function has been ranked; sqlite row elision is the same operation on a different corpus.
- **Budget.**  `--target` and `--fudge` truncate the ranked list.
- **Projection.**  `--format` names the encoding.  One selected set, four encodings: prompt (the default), paths, json, wc.

## Code priorities

`accio` runs before every prompt it builds, so it sits on the latency path of an invocation it does not itself make.  Its priorities are therefore dic's priorities:

1. no dependencies but the standard library, ever,
2. as few files as possible, and no class where a function will do,
3. streaming to stdout as the walk proceeds rather than assembling the whole prompt in memory first.

Rule 1 has one deliberate exception and only one: `--embed` and `--expand` import `dic` (which is Python) and its transitive dependencies.  The cost lands only on a run that already pays for a network round trip, and the walk path stays exactly stdlib.

Determinism is a requirement on the same footing as speed.  The same tree and the same lens must produce byte-identical output, because the output is the prefix of a prompt `dic --cache` may already have paid to cache, and because two runs that differ cannot be diffed.

`accio` is a package -- `accio.traverse`, `accio.select`, `accio.render`, `accio.index`, `accio.lens`, `accio.extract` -- so that the pipeline is importable stage by stage by something that wants the same bundle as data, and so that the CLI is only argument parsing.

## What is traversed

Every path named on the command line is traversed:

- a **file** is summoned as itself, whatever it is called;
- a **directory** is walked, and every file in it is summoned.

The walk sorts the entries at each level, so the order is stable, and descends depth-first.  Ignore patterns are matched against one path component at a time, so `node_modules` refuses a directory wherever it appears.

### The ancestor walk

This is the one thing `accio` does that `cat` cannot, and the reason it exists.

Before a file is kept, the files named in `--readme` that exist in each directory above it are kept first, from the outermost directory inward.  For `a/b/c.py`, with the default names:

    ./AGENTS.md
    a/README.md
    a/b/README.md
    a/b/c.py

The conventions of a subtree therefore arrive before the code that obeys them, and a file's own directory speaks immediately before the file.  Within one directory the names are tried in the order given, and every name that exists is taken, so a tree keeping both a README and an AGENTS file loses neither.

Context files are deduplicated across the whole run: the root README is printed once, with the first file that needed it, and the thousandth file beneath it does not repeat it.

In the tree, an ancestor file is a node whose `parent` is the file it was summoned above.  In the graph PageRank walks, it is the same edge.

An absolute path means context from the filesystem root down and a relative path means context from `.` down.

### What is skipped

1. **Binary files**, recognised by a NUL byte in the first 8 KiB -- the test `git`, `grep(1)` and `file(1)` all begin with, and the one that needs no table of extensions, because a text file of any language has none.  A skipped file is skipped in **silence**: a warning about a file the caller never named is noise, and `accio` prints the prompt and nothing else.  It must never warn into the stream the prompt is on.
1. **The default ignores**: `.git`, `__pycache__`, the caches, `.venv`, `venv`, `node_modules`, `*.pyc`, `*.egg-info`, `.DS_Store`.
1. **Whatever git ignores.**  The rules are git's and are asked of git -- `git ls-files -co --exclude-standard`, one subprocess per repository -- rather than reimplemented, because a hand-written matcher gets `/`-anchoring and `!` negation wrong in exactly the cases nobody notices until a secret file is inside a prompt.  The repository consulted is the one containing the *path* and not the one containing the working directory, so `..` out of a tree means the rules of wherever it lands, and a path with no repository above it is filtered by the defaults alone.
1. **Submodules, and checkouts nested inside a checkout.**  A directory that is itself a repository is not entered: it is a different tree with different rules.  Naming it is how the caller asks for it.
1. **Whatever the caller added with `--ignore`**, on top of those.

A file named outright on the command line is kept whatever it is called: ignoring is about what a walk stumbles into, and not about what the caller asked for.  A directory named outright is walked, and what the walk finds under it is filtered like anything else.  A file that cannot be read -- permissions, a dangling symlink, a race -- is skipped in the same silence, because a prompt is better for what it has than absent for what it has not.  Saying so is what `-v` is for.

## The chunk

Chunking is where the quality is, and it is where a file's format is understood.  The chunker returns a **tree**, and every consumer -- the walk as much as the retriever -- reads that tree and nothing else:

    chunk(path, bytes) -> [(chunk_id, text, ord, parent, meta)]

A file is the root; a section, a function, a table, a column, a paragraph are its children.  `parent` points at the enclosing node; `meta` is a small dict the extractor fills with what its own formats already know (kind, name, line, heading path, RFC 6901 pointer).  `ord` is the position among siblings.

Three things are separate because they were separate in practice:

- the **chunk** is a node of the tree;
- the **breadcrumb** is the path from the root to the node -- `path > heading > key`, `path::Name` for code -- and is prepended to the text at index time and at prompt time both;
- the **parent** is metadata, not embedded text, so a vector survives a reshuffle of the tree and a chunk that is re-parented keeps its id.

Chunk identity is a content hash:

    chunk_id = sha256(breadcrumb + "\n" + text)[:16]

A chunk is named by what it says, not by where it sits.  Chunking is a pure function `path, bytes -> tree`, so the index compares hashes rather than guessing at staleness; a chunk that did not change keeps its id and therefore its vector, and a rename costs one new path on a chunk that is otherwise the same.  Three things invalidate a chunk -- the file, the extractor version, the embedder -- and only the last is expensive.  Because rank fusion is rank-based and not score-based, vectors from two embedding models fuse with no cross-space alignment, so an embedder change is a background rebuild and not downtime.

### Extractors

An extractor is a function `bytes, path -> [(chunk_id, text, ord, parent, meta)]`, imported by name with the same longest-first resolution `dic --tools` already uses:

    --extractor accio.extract.code:definitions
    --extractor pkg.mod:fn

An extractor is a function and not a class, and there is no registry: importing is the registration.  The default is a chain chosen by extension -- `.py` gets the Python tree, `.json` a JSON-pointer tree, an mbox a per-message tree, anything else a line tree -- and `--extractor` overrides the chain.

### Selection and elision are rendering

What the tree makes possible, and what a flat list could not express, is that "keep the docstrings and elide the bodies", "the schema of the database but not the rows", and "the READMEs above but not the code" are the *same* operation applied to different corpora:

- a Python file's tree has a `def` node whose body is inside its text; eliding the body is `elide(chunk, "body")` on the promoted text;
- a sqlite database's tree has a `table` node whose rows are its children; keeping the schema is `--elide kind=row`;
- the ancestor walk is a `parent` edge that was always there; `--readme` names the nodes it keeps at load time.

A **lens** chooses which nodes matter.  A **rewrite** decides what text each one shows.  Both are rendering decisions, both are per-corpus, and neither is a schema change, an index rebuild, or a CLI change.  That is what makes "we do not know what is best yet" survivable here.

## The index

An index is one sqlite file per corpus.

Its home is the repository:

    $(git rev-parse --git-common-dir)/accio/index.db

so everything about a repo lives in one place and deleting the repo deletes its metadata.  The **common** dir and not the per-worktree git dir, because a worktree and the tree it branched from share one repository and should share one index; `itera-ema-file` already looks up the same path in the same way.

A corpus with no repository is not a special case, only a different home:

    ${XDG_CACHE_HOME:-$HOME/.cache}/accio/<hash-of-realpaths>/index.db

`~/.cache` and not `~/.config`, because an index is regenerable and `~/.config` is read as configuration.  `--index-dir DIR` and `ACCIO_INDEX_DIR` move either one, for a monorepo whose `.git` is on a small volume.

Two tables, and only the second costs money:

- **FTS5** -- the BM25 index, built by the walk and free.
- **`chunks(id, path, breadcrumb, text, meta, embedder, vector, build_mid)`** -- content-addressed rows; the vector is null until `--embed` fills it and `build_mid` records the `dic` call that bought it.

### Staleness

The index is opened and refreshed by every `--rag`:

1. traverse the paths,
2. for each file, compare `(mtime, size)` to the row the index holds,
3. re-chunk only the changed files -- chunk ids are content hashes, so the delta is exact,
4. delete the gone rows and insert the new ones.

The cost is wall clock and no money, and it is proportional to the *change*, not to the corpus.  `--refresh` forces the whole rebuild for the one case the stat filter gets wrong: a `git checkout` sets mtime to now on unchanged content and a file watched by `(mtime, size)` looks new.  `--prune` deletes the index and exits.  `--explain` prints what would be indexed, what the index already has, and the delta between them -- the whole of staleness made legible, and the debugger for the implicit refresh.

### What costs money

FTS5 does not.  Embedding does.  `--embed` is the only verb that spends, so the default `--rag 'q' src/` never does and a typo cannot.

    accio --embed src/                      build the vector index, print cost, exit
    accio --rag 'q' --embed src/ | dic -    build what is missing, then rank with it

The two are the same behavior with an optional ranking step after, and the paid part is the *delta*: a fresh corpus embeds everything in batched requests, a current one embeds nothing and the query is free.

Batch size dominates every other cost decision: 128 chunks a request is about a hundred times cheaper per chunk than one request per chunk.

## Ranking

The ladder is BM25, then a bi-encoder, then reciprocal rank fusion, then a cross-encoder rerank of the top fifty, then personalized PageRank.

    bm25(q) -> bi_encoder(q) -> rrf -> cross_encoder(top 50) -> pagerank

**Ranking starts with BM25, not with vectors.**  sqlite's FTS5 ships `bm25()` in the binary `dic` already depends on: no imports, no API key, no embedding cost, and a query plan a human can read.  A `--rag` with no `--embed` is BM25 and is free.

Vectors are a second list, added when `--embed` paid for them.  The two are fused with reciprocal rank fusion,

    score = sum over lists of 1 / (60 + rank)

which needs no training and no score normalization.  Hybrid beats either alone, and BM25 alone beats badly chunked vectors, which is what a first version would have.

**PageRank is personalized**, seeded by what already matched; the global version answers "what is the important file in this repo", which is not the question.  `r = alpha*seed + (1-alpha)*W^T r`, twenty iterations, `score = bm25_norm + lambda*r`.  It redistributes an existing signal and does not create one, so it is never a substitute for the reranker.  The graph edges are what the formats already say: ancestor containment (`a/README.md -> a/b/c.py`, the ancestor walk becoming an edge type), `ctags --output-format=json` for definitions, markdown links and HTML `href`.

## The lens catalog

The lenses the tool ships, and what each is composed of:

| invocation | predicate | ranking |
| ---------- | --------- | ------- |
| `accio src/` | true | walk order |
| `accio --rag 'q' src/` | true | bm25(q) |
| `--embed` | true | + bi-encoder(q), rrf |
| `--expand [N]` | true | + N rewrites of q, rrf |
| `--recent 7d` | mtime within 7d | mtime desc |
| `--rev A..B` / `--diff` | touched in range | recency |
| `--dirty` / `--staged` | uncommitted | walk |
| `--sym NAME` | defines or refers to NAME | ctags |
| `--depends-on X` | reachable from X | walk |
| `--select kind=K` | node kind is K | walk |
| `--readme NAME` | a context file above a kept file | walk |

Hints on `--rag` and not lenses of their own:

- `--embed [MODEL]` -- build the vector index and rank with it.
- `--expand [N]` -- rewrite the query with the generation model, N times.
- `--fudge F` -- how much the last chunk may overshoot `--target`.

Rewrites, not lenses:

- `--elide kind=K` -- drop the text of nodes of that kind, keep the node.

## Output

The default is a list of mentions, one per selected chunk, and no
contents:

    @@a/b/c.py

`dic` scans the `@@` back into a filename and hoists that file into a
header block at the head of the request, so `accio src/ | dic '...'`
arrives with the file contents `dic` was given.  Which files are named
is the walk's decision and where in the request they land is `dic`'s,
and a file named twice in the stream is hoisted once.  The default is a
projection of the walk and not the walk's bytes, which keeps the walk
on the cheap path: `accio` reads no file to name one.

The mention is the chunk's path and not its breadcrumb, because `@@`
reads as a filename and `@@a/b/c.py::parse` names no file; a file the
extract split into chunks is named once per chunk, which `dic`'s
content-hash dedup collapses to one hoist.

### The mention grammar

The `@@` is not markup: it is the mention primitive, and `dic` is the
program that reads it.  A mention is a sigil and a key that runs to its
terminator, and the terminator is what tells one kind from the other.

`@@` names a file.  The sigil sits at the start of the string or after
a whitespace character, and the path runs to the next whitespace, so it
is written mid-sentence and the sentence is not interrupted: `see
@@a.py.` mentions `a.py.` and not `a.py`.  A `@@` with no path, or one
followed by whitespace, is a literal `@@`.

`%%` runs a command.  The sigil sits at column 0 -- the start of the
string or the first character after a newline -- and the command runs
to the end of its line, with the leading whitespace stripped.  Column 0
is the whole of the reason a `%%` in running prose is a percent sign
and not a command.  An empty `%%` is an error rather than a literal,
because a caller who wrote one meant to write a command.

Neither is rescanned: an `@@` or `%%` inside a path or a command is
literal text, and a header built from one is never scanned again.  The
escapes are `\@@` and `\%%`, consumed before the prompt is sent, so a
literal sigil reaches the model and the backslash does not.

A command is a word list and not a shell line.  `%%` runs its
remainder as an argv under the sandbox dic ships -- the same sandbox a
`--mcp` server runs under, and the one `dic` itself is wrapped in when
an untrusted program is inside it -- and the split is dic's and not the
shell's.  A `|` is a word, a `$(...)` is a word, and a word that is
nothing but metacharacters is refused by name, because a program named
`|` is a failure that does not say so.  The leading `--flag` and
`NAME=VALUE` words are the sandbox's own and widen the jail for that one
child: `%%--share-net curl example.com` fetches and `%%curl
example.com` does not, and `%%GH_TOKEN=x gh api user` is a command whose
own credential is the one it reads.  A non-zero exit fails the call
before the request body is built, because there is no output worth
sending and a prompt that quietly lost a command is worse.

A command produces the same header block a file does, with the command
text in the path slot:

    ls -la
    ---
    total 4
    ---

which is what makes a transcript self-describing: the model reads the
command that answered and not the command's bytes.  Both mentions
deduplicate across a conversation by identity -- a file's `(path,
sha256)`, a command's `("cmd", key, sha256)` -- and `dic` records the
identities a turn hoisted in the row it writes, so a later turn whose
mention matches one an ancestor carries is neither run nor sent.

`--format prompt` is the self-contained text encoding, the one
files-to-prompt uses:

    a/b/c.py
    ---
    <contents of c.py>
    ---

The path is on a line of its own and the text follows it between `---` fences, so a reader -- a model, a human, `less` -- can see where one file stops.  The format is files-to-prompt's, kept so that a reader that already knows that encoding does not have to learn a second one, and a fence closes every file, so the last line of the run is always `---`.  The newline below the text is written and never assumed: a file whose last line has no newline of its own must not run into the closing fence.

A **chunk** is printed the same way, with its breadcrumb in the path slot:

    a/b/c.py::parse
    ---
    <the definition of parse>
    ---

The breadcrumb names what is inside the file and not the whole file, so a citation a model makes is still a path with a name inside it.  A run that mixes whole files and chunks -- a context file beside a chunk of what it explains -- prints both as they are, because the reader can already see the difference.

There is no escaping and no quoting.  A file whose content contains a line that is itself a path is ambiguous, and the answer is `--format json`, not a longer escape.

`--format paths` prints the breadcrumb of each selected chunk, one per line, and no contents: it is `at-paths` without the doubled at-sign, and with the name a chunk has inside its file where the extract gives it one.  `--format json` prints one object per selected node:

    {"path": "a/b/c.py", "chunk_id": "...", "breadcrumb": "a/b/c.py::parse",
     "kind": "def", "ord": 3, "text": "..."}

which is the interface `dic` reaches for when it wants `(path, hash)` pairs and not rendered bytes.  `--format wc` prints line, word and byte counts per path and an estimated token count for the whole prompt.

`DIC_MENTIONS=off` moves the default format from `at-paths` to
`prompt`, because dic has stopped reading the `@@` stream that
`at-paths` writes.  A named `--format at-paths` is still honoured: an
explicit request is a request, and this is a default and not a
refusal.

## Budget

`--target SIZE[UNIT]` truncates the selected list to an amount of prompt.  Bytes are the default, and the suffix is uppercase for units that are not bytes:

    --target 8192        bytes
    --target 8k          8 KiB
    --target 200L        200 lines
    --target 8kT         8k tokens
    --target 8kW         8k words
    --target 50C         50 chunks

The count is on the *rendered* stream -- path header and fences included -- because that is the number that costs.  `--format wc` reads the same counter, and `50C` is unit-free so it is testable.

The cap is soft.  Chunks are emitted in rank order while the running total is at or below the target; a chunk that crosses is included iff the overshoot is at or below `--fudge` (default 1/4) and dropped otherwise.  The top chunk is always emitted, so a run is never empty.  `--fudge 0` is the strict special case of the same rule and not a second flag.

## Options

| short | long | meaning |
| ----- | ---- | ------- |
| `-r` | `--readme NAME` | context file to keep from every directory above a kept file; repeatable.  The default is `README README.md AGENTS.md` |
|      | `--no-readme` | keep no file above the ones named |
| `-i` | `--ignore PATTERN` | walk past a name matching this shell pattern; repeatable, on top of the defaults |
|      | `--no-gitignore` | do not consult git: keep what `.gitignore` excludes, and walk into submodules |
|      | `--extractor MOD:FN` | use this extractor for the paths it can read; repeatable |
|      | `--rag QUERY` | rank by relevance to QUERY (bm25 unless `--embed`) |
|      | `--embed [MODEL]` | build the vector index and rank with it; the only paid path |
|      | `--expand [N]` | rewrite the query with the generation model, N times |
|      | `--select KIND=K` | keep only nodes of the named kind; repeatable |
|      | `--elide KIND=K` | drop the text of nodes of the named kind, keep the node |
|      | `--recent DUR` | keep nodes modified within DUR of now |
|      | `--rev A..B` | keep nodes touched in the commit range |
|      | `--dirty` | keep uncommitted changes |
|      | `--staged` | keep staged changes |
|      | `--target SIZE[UNIT]` | truncate the rendered prompt to about SIZE |
|      | `--fudge F` | how much the last chunk may overshoot `--target` |
|      | `--format FMT` | `at-paths` (default), `prompt`, `paths`, `json`, `wc` |
| `-w` | `--wc` | `--format wc` |
|      | `--refresh` | rebuild the index from the walk; free |
|      | `--prune` | delete the index and exit |
|      | `--explain` | print what would be indexed and what is |
|      | `--index-dir DIR` | where the index lives; `$ACCIO_INDEX_DIR` is the default |
| `-v` | `--verbose` | say on stderr which file was skipped and why |

A missing path is an error naming it and exits nonzero; everything else is a walk that prints what it finds.

`--embed` with no `--rag` builds the index and prints cost; `--embed` with `--rag` builds what is missing and ranks with the result.  Both are the same behavior with an optional ranking step after.

There is no config file.  A shell startup file already is one, and `accio` is a command inside a pipeline rather than a session of its own.

## The dic boundary

`accio` never holds an API key and never sends a prompt.  Everything it needs a model for -- an embedding, a query rewrite, nothing else -- is asked of `dic`, imported as a library:

    from dic import embed, chat

    vecs     = embed(model, [texts])                  # one batched call, ledgered
    rewrites = chat(model, expand_prompt(q, n))       # one small call, ledgered

The call is `dic`'s and the ledger is `dic`'s.  `--cost-session` includes the embedding; `--cost-of` on a later turn does not, because a build is not attributable to one turn; `--stats` shows embedding spend beside generation spend; and each vector row carries the `build_mid` that bought it, so a stale vector traces to its call.  There is one ledger and this is not a second one.

A subprocess of `dic` would work as well at the shell and worse inside it: the child would spawn with the parent's `$DIC_SESSION` and the parent would read the child's cost from stdout, which is parsing to recover what a function call returns.  The rule is that no Python in this repository subprocesses `dic` and always imports it, and this file is where that rule sits for `accio`.

## Text-to-SQL

A database is a corpus, and a dump is a text file:

    sqlite3 db '.dump' | accio --extractor accio.extract.sqlite:tree -
    accio --elide kind=row db.sqlite | dic 'summarize the schema'
    accio --elide kind=row db.sqlite | dic --tools db:query 'Q3 revenue for X'

There is no `--schema` mode.  "The schema and not the rows" is `--elide kind=row`, which is the same operation as "the docstrings and not the bodies".  There is no database connector either: `sqlite3 db '.dump'`, `pg_dump` and `mysqldump` all turn a database into text, so the interface is "ingest text on stdin" and "arbitrary databases" becomes "whatever can dump to a file".  Schema first and rows second is what the tree says anyway.

`sql()` itself -- the tool `dic --tools db:query` names -- is SELECT-only (`PRAGMA query_only=1`), with a statement timeout and a row cap, and that is the tool's job and not the model's.  Once it holds, the model's query is one more tool call under the one threat model, with no `--mode` to remember and no second place to keep the safety rule.

## TODO

### 1. Extract the seam

Split `gather.py` into `traverse` -> `select` -> `render`, with `select` today being the identity.  No behavior change; every existing test passes.  This is the commit that makes the rest additive, and it must not be skipped: everything after is a new lens and not a new pipeline.

### 2. The tree-shaped chunker

Change `chunk(path, bytes)` to return a tree.  Ship a line/byte chunker as the universal fallback and make the tree the only representation.  Index nothing yet.

### 3. `--format json`

The machine interface, so that `--rag`'s output is testable without parsing fences and so `dic`-as-a-library can reach `(path, chunk_id, hash)` as data rather than as rendered text.  Forces the record shape to be real.

### 4. The index and BM25

Move the index to `$(git rev-parse --git-common-dir)/accio/`, add the XDG fallback, `--refresh`, `--prune`, `--explain`, and the `(mtime, size)` staleness check `dic/config.py` already uses for its sources.  `--rag` becomes usable with zero model calls.  This is the first commit a user would feel.

### 5. `--target` and `--fudge`

Needs the render counter from 3.

### 6. The Python extractor

The first real tree: file -> class -> def -> docstring, with `--elide kind=body`.  Validates the tree design against a corpus that has structure.

### 7. `dic` as a library

`--embed` and `--expand`, ledgered by `dic`.  Only now does anything cost money.

### 8. RRF and personalized PageRank

Both are rankers over lists the earlier commits already produce.  The graph edges are ancestor containment and ctags, which 6 has begun to emit.

### 9. The sqlite extractor

Database -> tables -> columns and rows.  `--elide kind=row` is the schema view.  First non-git, non-text corpus.

### 10. Evaluation

Build the harness before the ranker is tuned.  Twenty queries with known-correct chunks, and `--eval` printing recall@k and mean reciprocal rank.  Without it every later choice is a feeling, and chunk size will be tuned by feeling and be wrong.

### 11. Query expansion

`--expand [N]`.  The literal query is the fast path; `--expand 1` is a single rewrite, `--expand 3` is three, and the result lists fuse with RRF, which is already there.  Cache by query hash the way the SPEC already says to cache the query embedding; it is the same round trip with the same cacheability.

### 12. Storage size

Vector size per hundred thousand chunks is the difference between a cache and a problem:

| form | size |
| ---- | ---- |
| 1536d fp32 | 614 MB |
| 1024d fp16 | 205 MB |
| 256d fp16, Matryoshka-truncated | 51 MB |
| 256d int8 | 26 MB |

Truncating to 256 dimensions costs one or two percent of recall on the models that support it and buys twelve times the space.  Brute-force dot products over a hundred thousand rows take about 15 ms, so there is no ANN index and no `sqlite-vec` until the corpus is in the millions.  `numpy` is acceptable **here** and only here: the zero-dependency rule protects `dic`'s time to first token, and this is not on that path.  The argument must not leak back into `dic`.

### 13. Chunking by format

| input | chunk at | chunk identity |
| ----- | -------- | -------------- |
| prose | paragraph, about 500 tokens, 15% overlap | `path#heading` |
| code | a `def` or `class` boundary | `path::Name` |
| json/yaml | leaf node, siblings batched | RFC 6901 pointer |
| csv | header row plus groups of N rows | `path#rows[100:200]` |
| html | readability-extracted block | `url#selector` |
| email | message boundary | `path#msgid` |

Every chunk carries a **breadcrumb** -- `path > heading path > key path` -- prepended to its text at index time and at prompt time both.  It is ten lines of code and a larger effect on retrieval than any choice of ranker.

### 14. Web

Fetching is a separate step from indexing.  A fetcher -- robots.txt, a rate limit, a cache, `lynx -dump` or `pandoc` for html to text -- writes a directory of text plus a manifest of provenance.  The indexer never learns that a page was ever a page, so politeness and retrieval stay different concerns with different failure modes.

### 15. Deduplicating a continued conversation

See `scripts/TODO-dedupe.md`.  The mechanism is a `--exclude-seen` lens: a chunk the session already carries is dropped from the selection, keyed by `(kind, key, hash)`.  The `seen` set inside one `accio` run is unaffected -- it is the cross-run case this adds.

## Differences from files-to-prompt

`accio` is the same idea with four deliberate changes:

1. **The ancestor walk.**  It is above, and it is the whole reason to have a separate program.
1. **The tree and the lens.**  A file is a tree and a query is a lens over it; the walk is the lens that takes everything.
1. **Silence about binaries.**  A skipped file is not a warning.  Nothing that is not part of the prompt goes to stdout, and nothing at all goes to stderr unless `-v` asked.
1. **Reproducibility as a requirement** rather than a side effect: sorted walks, content-addressed chunks, and a documented output format.

`--format markdown` and `--format xml` are the compatibility shims for the files-to-prompt encodings, and both are TODO.

## OUT OF SCOPE

1. **Holding an API key.**  `accio` builds a prompt and never sends one.  Model calls made on behalf of a search -- embeddings, reranking, a query rewrite -- are made by `dic` and recorded by `dic`, imported as a library and never as a subprocess.

1. **Prompt templates, fragments and role markup.**  These belong in programs that generate text, and `accio`'s output is meant to be piped into whatever does.

1. **A plugin system with per-format registration.**  There is one binary, it reads files, and a format it cannot chunk is chunked by lines.  An extractor module is importable by name and needs no registry; that is the whole extension mechanism.

1. **Guessing relevance without an index.**  `accio` will not heuristically pick the important files by reading everything and looking for keywords.  A model can do that badly for free, or the index can do it well for money, and there is no third option worth writing.

1. **Embedding or ranking at walk time.**  Plain `accio src/` is a filesystem walk and a write, and it stays one.  Anything with a query in it is the select stage and pays for itself explicitly.

1. **A `--schema` mode.**  The schema is `--elide kind=row`, the same operation as `--elide kind=body`, and a mode that stands in for a predicate is a mode that will grow a cousin.
