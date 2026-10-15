# accio SPEC

`accio` is a minimalist CLI tool for gathering the contents of files into one prompt.

"Accio" is Latin for "I summon", and is the word of the Summoning Charm: the thing you want is not in your hand, and one word brings it.  `accio` is that word for a directory of documents.

`accio` is the sibling of `dic`.  `dic` speaks to a model; `accio` decides what it is told.  They are separate programs because dic's SPEC says so -- a mechanism for building a prompt belongs in a separate program that builds one and passes it in -- and because they have different costs and different failure modes.  The composition is the design:

    accio src/ | dic "summarize this module"
    accio --rag "where is the retry budget enforced?" src/ | dic -

## Code Priorities

`accio` runs before every prompt it builds, so it sits on the latency path of an invocation it does not itself make.  Its priorities are therefore dic's priorities:

1. no dependencies but the standard library, ever,
1. as few files as possible, and no class where a function will do,
1. streaming to stdout as the walk proceeds rather than assembling the whole prompt in memory first.

Determinism is a requirement on the same footing as speed.  The same tree must produce byte-identical output, because the output is the prefix of a prompt `dic --cache` may already have paid to cache, and because two runs that differ cannot be diffed or compared at all.  Sorted walks, no timestamps, and no absolute path where a relative one was given.

`accio` is a package -- `accio.gather`, `accio.__main__` -- so that the walk can be imported as a library by something that wants the same bundle as a list of pairs, and so that the CLI is only argument parsing.

## What is summoned

Every path named on the command line is summoned:

- a **file** is summoned as itself, whatever it is called;
- a **directory** is walked, and every file in it is summoned.

The walk sorts the entries at each level, so the order is stable, and descends depth-first.  Ignore patterns are matched against one path component at a time, so `node_modules` refuses a directory wherever it appears.

## The ancestor walk

This is the one thing `accio` does that `cat` cannot, and the reason it exists.

Before a file is summoned, the files named in `--readme` that exist in each directory above it are summoned first, from the outermost directory inward.  For `a/b/c.py`, with the default names:

    ./AGENTS.md
    a/README.md
    a/b/README.md
    a/b/c.py

The conventions of a subtree therefore arrive before the code that obeys them, and a file's own directory speaks immediately before the file.  Within one directory the names are tried in the order given, and every name that exists is taken, so a tree keeping both a README and an AGENTS file loses neither.

Context files are deduplicated across the whole run: the root README is printed once, with the first file that needed it, and the thousandth file beneath it does not repeat it.

An absolute path means context from the filesystem root down and a relative path means context from `.` down, so `accio /etc/hosts` will summon `/README.md` if some unfortunate person has one.

## Output

One format, and it is text:

    a/b/c.py
    ---
    <contents of c.py>
    ---

The path is on a line of its own and the text follows it between `---` fences, so a reader -- a model, a human, `less` -- can see where one file stops.  The format is files-to-prompt's, kept so that a reader that already knows that encoding does not have to learn a second one, and a fence closes every file, so the last line of the run is always `---`.  The newline below the text is written and never assumed: a file whose last line has no newline of its own must not run into the closing fence.

There is no escaping and no quoting.  A file whose content contains a line that is itself a path is ambiguous, and the answer is `--format markdown` or `--format xml` (TODO), not a longer escape.

## What is skipped

1. **Binary files**, recognised by a NUL byte in the first 8 KiB -- the test `git`, `grep(1)` and `file(1)` all begin with, and the one that needs no table of extensions, because a text file of any language has none.  A skipped file is skipped in **silence**: a warning about a file the caller never named is noise, and `accio` prints the prompt and nothing else.  It must never warn into the stream the prompt is on.
1. **The default ignores**: `.git`, `__pycache__`, the caches, `.venv`, `venv`, `node_modules`, `*.pyc`, `*.egg-info`, `.DS_Store`.
1. **Whatever git ignores.**  The rules are git's and are asked of git -- `git ls-files -co --exclude-standard`, one subprocess per repository -- rather than reimplemented, because a hand-written matcher gets `/`-anchoring and `!` negation wrong in exactly the cases nobody notices until a secret file is inside a prompt.  The repository consulted is the one containing the *path* and not the one containing the working directory, so `..` out of a tree means the rules of wherever it lands, and a path with no repository above it is filtered by the defaults alone.
1. **Submodules, and checkouts nested inside a checkout.**  A directory that is itself a repository is not entered: it is a different tree with different rules.  Naming it is how the caller asks for it, and the walk that starts there starts at its own root and obeys its own ignores.
1. **Whatever the caller added with `--ignore`**, on top of those.

A file named outright on the command line is summoned whatever it is called: ignoring is about what a walk stumbles into, and not about what the caller asked for.  A directory named outright is walked, and what the walk finds under it is filtered like anything else, so naming `build/` does not open the files that `.gitignore` closed.

A file that cannot be read -- permissions, a dangling symlink, a race -- is skipped in the same silence, because a prompt is better for what it has than absent for what it has not.  Saying so is what `-v` is for (TODO).

## Options

| short | long | meaning |
| ----- | ---- | ------- |
| `-r` | `--readme NAME` | a file to summon from every directory above a summoned file; repeatable.  The default is `README README.md AGENTS.md` |
| | `--no-readme` | summon no file above the ones named |
| `-i` | `--ignore PATTERN` | walk past a name matching this shell pattern; repeatable, on top of the defaults |
| | `--no-gitignore` | do not consult git at all: summon what `.gitignore` excludes, and walk into submodules |
| `-l` | `--paths-only` | print the paths that would be summoned, one per line, and no contents |
| `-w` | `--wc` | print line, word and byte counts per path and an estimated token count for the whole prompt, and no contents |

A missing path is an error naming it, and exits nonzero; everything else is a walk that prints what it finds.

There is no config file.  A shell startup file already is one, and `accio` is a command inside a pipeline rather than a session of its own.

## TODO

### 1. .gitignore

The rules are git's and not ours: `git ls-files -co --exclude-standard` is
the only matcher that gets nested files, `!` negation and `/` anchoring
right, so it is what the walk asks.  Three things are left.  The listing
is proportional to the repository and is held in memory for the run.  An
ignored *directory* is still walked and its files rejected one at a time,
which is the wrong cost for a `dist/` with a hundred thousand entries and
whose proper answer is `git check-ignore`, a subprocess per directory.
And a working tree is found by looking for `.git` upward, which is what
git does and is not what `GIT_DIR` or `GIT_WORK_TREE` says.
`--no-gitignore` is the way out of all three.

### 2. Search

The other half of the program: `accio` should answer "which parts of this corpus are relevant to this question" and emit those parts as a prompt in the same format the walk uses.

    extract -> chunk -> index -> rank -> assemble

Only the first two vary by file format; ranking, storage and assembly are shared.  That is why this is one tool and not one tool per format.

    accio --index src/                   build or refresh the index
    accio --rag "question" src/ | dic -  the ranked chunks as a prompt

**Ranking starts with BM25, not with vectors.**  sqlite's FTS5 ships `bm25()` in the binary `dic` already depends on: no imports, no API key, no embedding cost, and a query plan a human can read.  Vectors are a second index, added later, fused with reciprocal rank fusion, which needs no training and no score normalization:

    score = sum over lists of 1 / (60 + rank)

Hybrid beats either alone, and BM25 alone beats badly chunked vectors, which is what a first version would have.

**Chunking is where the quality is.**  Per format:

| input | chunk at | chunk identity |
| ----- | -------- | -------------- |
| prose | paragraph, about 500 tokens, 15% overlap | `path#heading` |
| code | a `def` or `class` boundary | `path::Name` |
| json/yaml | leaf node, siblings batched | RFC 6901 pointer |
| csv | header row plus groups of N rows | `path#rows[100:200]` |
| html | readability-extracted block | `url#selector` |

Every chunk carries a **breadcrumb** -- `path > heading path > key path` -- prepended to its text both at index time and at prompt time.  It is ten lines of code and a larger effect on retrieval than any choice of ranker.

**Whole documents are a different operation.**  "Extract this document" is selection and not similarity: filter documents first, with a document-level index or a glob, then chunk-retrieve within the survivors.

### 3. Ranking

- The query embedding is the one unavoidable round trip.  Cache it by query hash, and give `--bm25` as a fast path so that a script doing a thousand lookups is not made to pay 200 ms each.
- The ladder is BM25 (free, about 1 ms), then a bi-encoder (one embedding call, +200 ms), then reciprocal rank fusion (free), then a cross-encoder rerank of the top fifty (one generation call, +1 s), then personalized PageRank (free, +50 ms).
- PageRank is **personalized**, seeded by what already matched; the global version answers "what is the important file in this repo", which is not the question.  `r = alpha*seed + (1-alpha)*W^T r`, twenty iterations, `score = bm25_norm + lambda*r`.  It redistributes an existing signal and does not create one, so it is never a substitute for the reranker.
- The graph edges are what the formats already say: ancestor containment (`a/README.md -> a/b/c.py`, which is the ancestor walk becoming an edge type), `ctags --output-format=json` for definitions, markdown links and HTML `href`, and inferable csv and json foreign keys.

### 4. Cost

An index build costs money once and nothing thereafter, and that spend belongs in the ledger `dic` already keeps.  **Do not add a second ledger.**  An embedding batch is a row in `messages`, with `api_type: openai-embeddings`, `round: 0`, `prev_mid: NULL`, a `session` naming the build, and the batch's usage priced by the embedding model's own rules.  Then, with no new query:

- `dic --cost-session` includes the build;
- `dic --cost-of <mid>` does **not**, because a build is not attributable to one turn -- the dic SPEC's two rollups being orthogonal, and right;
- `dic --stats` shows embedding spend beside generation spend;
- each vector row carries the `build_mid` that bought it, so a stale vector traces to its call.

Batch size dominates every other cost decision: 128 chunks per request is about a hundred times cheaper per chunk than one request per chunk.

### 5. Subagents

A tool that asks a model a question *is* a subagent, and it must not be reimplemented here.  Text-to-SQL is two halves: a deterministic `sql(query) -> rows`, and a nondeterministic `question -> query` that is exactly what `dic` already does:

    DIC_SESSION="$DIC_SESSION/sql-$(ulid)" dic --tools db:query -s "$schema" "$question"

That is dic SPEC TODO 5 used as written.  The child is a subtree, so the parent's `--cost-session` counts it by construction, and `--cost-of` on the parent's mid does not, which is correct; the tool returns its child's cost in `tool_results.content`, so `dic --show` on the parent row says what the round cost; and the inner call may pass `--tools` again, so retry-with-feedback is dic's existing tool loop rather than new code here.

`sql()` itself is SELECT-only (`PRAGMA query_only=1`), with a statement timeout and a row cap, and that is the tool's job and not the model's.  A `DIC_AGENT_DEPTH` gate stops recursion, and `DIC_COST_BUDGET` -- already inherited and already checked before every request -- is what stops a runaway at the wallet instead.

### 6. Storage

    ~/.config/fac/index/<hash-of-corpus-root>.idx.db

One sqlite file per corpus, keyed by `(chunker_version, content_hash)` and validated with the mtime-and-size staleness check `dic/config.py` already uses for its sources.  Reuse that pattern; do not invent a second one.

**Not `dic.db`.**  Every `-c`, `--log`, `--show` and `--stats` opens that file and scans `messages`, and a half-gigabyte vector table beside an append-only, non-regenerable message tree is a corruption risk with no upside.

Vector size per hundred thousand chunks is the difference between a cache and a problem:

| form | size |
| ---- | ---- |
| 1536d fp32 | 614 MB |
| 1024d fp16 | 205 MB |
| 256d fp16, Matryoshka-truncated | 51 MB |
| 256d int8 | 26 MB |

Truncating to 256 dimensions costs one or two percent of recall on the models that support it and buys twelve times the space.  Brute-force dot products over a hundred thousand rows take about 15 ms, so there is no ANN index and no `sqlite-vec` until the corpus is in the millions.  `numpy` is acceptable **here** and only here: the zero-dependency rule protects dic's time to first token, and this is not on that path.  The argument must not leak back into `dic`.

The invariant that makes all of it cheap is that a chunk is named by its content:

    chunk_id = sha256(breadcrumb + "\n" + text)[:16]

Chunking is then a pure function `path, bytes -> [(chunk_id, text, ord)]` and validity is a hash comparison rather than an mtime guess.  It buys deduplication across files, incremental re-indexing (an unchanged chunk keeps its id and therefore its vector), survival across renames, and correctness when a breadcrumb changes (a new id, because the breadcrumb is part of the embedded text).  There are three levels of invalidation -- document changed, chunker version, embedder changed -- and only the last is expensive.  Because reciprocal rank fusion is rank-based and not score-based, vectors from two different embedding models fuse with no cross-space alignment at all, so an embedder change is a background rebuild and not downtime.  That is the strongest argument for RRF over score normalization.

### 7. Extractors

An extractor is a function `bytes, path -> [(chunk_id, text, ord)]`, imported by name with the same longest-first resolution `dic --tools` already uses:

    --extractor accio.extract.code:definitions
    --extractor pkg.mod:fn

An extractor is a function and not a class, and there is no registry: importing is the registration.  Chunking, ranking, storage and validity stay in the one tool, because three subtly different chunkers is how a retriever starts returning things nobody can explain.

**Databases are not connectors.**  `sqlite3 db '.dump'`, `pg_dump` and `mysqldump` all turn a database into text, so the interface is "ingest text on stdin" and "arbitrary databases" becomes "whatever can dump to a file" -- Unicode, and consistent with the rest of this project.  Schema first, rows second.

**Structured data often wants a query and not a search.**  "What was Q3 revenue for customer X" over a csv is a `SELECT`; letting the model write `jq` or SQL against a schema-only view composes with `--tools` and beats top-k retrieval on structured input.  Text-to-SQL is a different tool with a different threat model, and it is not smuggled in here.

### 8. Web

Fetching is a separate step from indexing.  A fetcher -- robots.txt, a rate limit, a cache, `lynx -dump` or `pandoc` for html to text -- writes a directory of text plus a manifest of provenance.  The indexer never learns that a page was ever a page, so politeness and retrieval stay different concerns with different failure modes.

### 9. Evaluation

Build the harness before the ranker.  Twenty queries with known-correct chunks, and `--eval` printing recall@k and mean reciprocal rank.  Without it every later choice is a feeling, and chunk size will be tuned by feeling and be wrong.

## Differences from files-to-prompt

`accio` is the same idea with three deliberate changes:

1. **The ancestor walk.**  It is above, and it is the whole reason to have a separate program.
1. **Silence about binaries.**  A skipped file is not a warning.  Nothing that is not part of the prompt goes to stdout, and nothing at all goes to stderr unless `-v` asked.
1. **Reproducibility as a requirement** rather than a side effect: sorted walks, content-addressed chunks, and a documented output format.

`--format markdown` and `--format xml` are the compatibility shims for anyone who wants files-to-prompt's other encodings, and both are TODO.

## OUT OF SCOPE

1. **Talking to a model.**  `accio` builds a prompt and never sends one.  The day it holds an API key is the day it has become `dic`, and the composition that keeps both simple is gone.  Model calls made on behalf of a search -- embeddings, reranking, a text-to-SQL subagent -- are made by `dic` and recorded by `dic` (TODO 4 and 5), invoked as a subprocess and never imported.

1. **Prompt templates, fragments and role markup.**  Same reasoning as dic's SPEC: these belong in programs that generate text, and `accio`'s output is meant to be piped into whatever does.

1. **A plugin system with per-format registration.**  There is one binary, it reads files, and a format it cannot chunk is chunked by lines.  An extractor module is importable by name and needs no registry; that is the whole extension mechanism.

1. **Guessing relevance without an index.**  `accio` will not heuristically pick the important files by reading everything and looking for keywords.  A model can do that badly for free, or the index can do it well for money, and there is no third option worth writing.

1. **Embedding or ranking at walk time.**  Plain `accio src/` is a filesystem walk and a write, and it stays one.  Anything with a query in it is the search half of the program and pays for itself explicitly.
