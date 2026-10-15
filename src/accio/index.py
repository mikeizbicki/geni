"""The index: one sqlite file per corpus.

The index lives where the corpus lives, so deleting the repository
deletes its metadata.  Its home is the repository's common git dir:

    $(git rev-parse --git-common-dir)/accio/index.db

and the **common** dir and not the per-worktree one, because a worktree
and the tree it branched from share one repository and should share one
index.  A corpus with no repository is not a special case, only a
different home: `${XDG_CACHE_HOME:-$HOME/.cache}/accio/<hash>/index.db`,
keyed on the realpaths so the same corpus reached two ways has one
index.  `--index-dir` and `$ACCIO_INDEX_DIR` move either one.

Two tables, and only the second costs money:

  - `docs`, an FTS5 virtual table -- the BM25 index, built by the walk
    and free;
  - `chunks(id, path, breadcrumb, text, meta, embedder, vector,
    build_mid)`, content-addressed rows whose vector is null until
    `--embed` fills it and whose `build_mid` records the `dic` call
    that bought it.

The index is opened and refreshed by every `--rag`.  The staleness
check is (mtime, size) per file: a file whose stamp is unchanged is not
re-chunked, and a chunk that did not change has the same content hash
and therefore the same row and the same vector.  `--refresh` ignores
the stamp for the one case stat gets wrong -- a `git checkout` sets
mtime to now on unchanged content -- and `--prune` deletes the file.

Nothing here calls a model.  The BM25 query is answered by sqlite, in
the binary `dic` already depends on: no imports, no API key, no
embedding cost, and a query plan a human can read.
"""
import contextlib, hashlib, json, os, sqlite3, subprocess

from accio.extract import Chunk, by_extension
from accio.traverse import canvassed, read


SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
    chunk_id UNINDEXED,
    body
);

CREATE TABLE IF NOT EXISTS chunks(
    id         TEXT PRIMARY KEY,
    path       TEXT NOT NULL,
    breadcrumb TEXT NOT NULL,
    text       TEXT NOT NULL,
    meta       TEXT NOT NULL,
    embedder   TEXT,
    vector     BLOB,
    build_mid  TEXT
);

CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);

CREATE TABLE IF NOT EXISTS files(
    path  TEXT PRIMARY KEY,
    mtime REAL NOT NULL,
    size  INTEGER NOT NULL
);
"""


def home(paths, override=None):
    """The directory that holds the index for a set of paths.

    `--index-dir` or `$ACCIO_INDEX_DIR` moves it anywhere, which is for
    a monorepo whose `.git` is on a small volume.  Otherwise it is the
    repo's common git dir when the paths are in one, and the XDG cache
    when they are not, so everything about a repo lives in one place
    and a corpus with no repo is a different home and not a different
    case.
    """
    override = override or os.environ.get("ACCIO_INDEX_DIR")
    if override:
        return override
    root = _common(paths)
    if root is not None:
        return os.path.join(root, "accio")
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache")
    return os.path.join(cache, "accio", _key(paths))


def db_path(paths, index_dir=None):
    """The sqlite file itself, inside the home directory."""
    return os.path.join(home(paths, index_dir), "index.db")


def _key(paths):
    """A short, stable name for a corpus that has no repository."""
    joined = "\0".join(sorted(os.path.realpath(p) for p in paths))
    return hashlib.sha256(
        joined.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def _common(paths):
    """The common git dir of the repo containing the first path, or None.

    One subprocess answers.  If the paths land in different repositories
    the first one wins: the common case is one tree, and a set that
    spans two is better served by two runs.
    """
    if not paths:
        return None
    d = os.path.abspath(paths[0])
    if os.path.isfile(d):
        d = os.path.dirname(d) or "."
    try:
        done = subprocess.run(  # noqa: S603
            ["git", "-C", d, "rev-parse", "--git-common-dir"],  # noqa: S607
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    if done.returncode != 0:
        return None
    out = done.stdout.decode("utf-8", "surrogateescape").strip()
    if not out:
        return None
    return out if os.path.isabs(out) else os.path.normpath(
        os.path.join(d, out))


def open_(paths, index_dir=None):
    """Open the index for these paths, creating it and its directory.

    FTS5 is checked by the CREATE itself: a build without it answers
    every other query and fails on the one that matters, and a build
    with it pays nothing for the check because the table already
    exists.
    """
    p = db_path(paths, index_dir)
    directory = os.path.dirname(p)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
    except sqlite3.OperationalError as e:
        conn.close()
        raise SystemExit(f"accio: no FTS5 in this sqlite: {e}") from None
    return conn


def refresh(conn, paths, readmes, patterns, gitignore, force=False):
    """Bring the index up to date with what the walk sees now.  Free.

    A file whose (mtime, size) matches the row the index holds is not
    re-chunked: the walk stat()s and moves on, and the cost is
    proportional to the change and not to the corpus.  A file that
    changed is dropped and re-inserted; a file that is gone takes its
    rows with it.  `force` skips the stamp entirely, for the one case
    stat gets wrong.
    """
    wanted = {}
    for path in canvassed(paths, readmes, patterns, gitignore):
        try:
            st = os.stat(path)
        except OSError:
            continue
        wanted[path] = (st.st_mtime, st.st_size)
    known = {row["path"]: (row["mtime"], row["size"])
             for row in conn.execute("SELECT path, mtime, size FROM files")}
    for path, stamp in wanted.items():
        if not force and known.get(path) == stamp:
            continue
        text = read(path)
        if text is None:
            continue
        drop_file(conn, path)
        for chunk in by_extension(path, text):
            add_chunk(conn, chunk)
        conn.execute(
            "INSERT INTO files(path, mtime, size) VALUES(?,?,?)"
            " ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime,"
            " size=excluded.size",
            (path, stamp[0], stamp[1]))
    for path in known:
        if path not in wanted:
            drop_file(conn, path)
            conn.execute("DELETE FROM files WHERE path=?", (path,))
    conn.commit()


def add_chunk(conn, chunk):
    """Insert one chunk into both tables.  The content hash names it.

    The FTS5 body is the breadcrumb and the text, the same bytes the
    chunk_id is a hash of, because the breadcrumb is what makes a
    citation a model produces still say `path::Name` and not two
    unrelated strings.
    """
    meta = dict(chunk.meta)
    meta["kind"] = chunk.kind
    meta["ord"] = chunk.ord
    meta["parent"] = chunk.parent
    conn.execute(
        "INSERT INTO chunks(id, path, breadcrumb, text, meta)"
        " VALUES(?,?,?,?,?)",
        (chunk.chunk_id, chunk.path, chunk.breadcrumb, chunk.text,
         json.dumps(meta, sort_keys=True)))
    conn.execute("INSERT INTO docs(chunk_id, body) VALUES(?,?)",
                 (chunk.chunk_id, chunk.breadcrumb + "\n" + chunk.text))


def drop_file(conn, path):
    """Remove every chunk a path owns, from both tables, and nothing else."""
    ids = [row[0] for row in
           conn.execute("SELECT id FROM chunks WHERE path=?", (path,))]
    if ids:
        conn.executemany("DELETE FROM docs WHERE chunk_id=?",
                         [(i,) for i in ids])
    conn.execute("DELETE FROM chunks WHERE path=?", (path,))


def search(conn, query):
    """Chunks ranked by BM25 relevance to query, ties by path and id.

    sqlite's FTS5 ships `bm25()` in the binary `dic` already depends
    on.  The tie-break on `(path, chunk_id)` is what makes the same
    tree print the same bytes in the same order, which is what keeps
    the output a prefix `dic --cache` may already have paid for.
    """
    sql = ("SELECT c.id, c.path, c.breadcrumb, c.text, c.meta"
           " FROM docs JOIN chunks AS c ON c.id = docs.chunk_id"
           " WHERE docs MATCH ?"
           " ORDER BY bm25(docs), c.path, c.id")
    for row in conn.execute(sql, (query,)):
        meta = json.loads(row["meta"])
        yield Chunk(path=row["path"], chunk_id=row["id"],
                    breadcrumb=row["breadcrumb"], text=row["text"],
                    kind=meta.pop("kind", "file"),
                    ord=meta.pop("ord", 0),
                    parent=meta.pop("parent", None),
                    meta=meta)


def prune(paths, index_dir=None):
    """Delete the index and exit.  Nothing here is not regenerable."""
    p = db_path(paths, index_dir)
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            os.remove(p + suffix)


def explain(paths, readmes, patterns, gitignore, index_dir, out):
    """Print what would be indexed, what the index holds, and the delta.

    The whole of staleness made legible: the debugger for the implicit
    refresh, and the reason a `--refresh` is worth doing after a git
    checkout set mtime on content that did not change.
    """
    wanted = {}
    for path in canvassed(paths, readmes, patterns, gitignore):
        try:
            st = os.stat(path)
        except OSError:
            continue
        wanted[path] = (st.st_mtime, st.st_size)
    p = db_path(paths, index_dir)
    out.write(f"index: {p}\n")
    if not os.path.exists(p):
        out.write("index has: nothing\n")
        out.write(f"would index: {len(wanted)} files\n")
        for path in sorted(wanted):
            out.write(f"  + {path}\n")
        return
    conn = sqlite3.connect(p)
    known = {row[0]: (row[1], row[2]) for row in
             conn.execute("SELECT path, mtime, size FROM files")}
    conn.close()
    new = sorted(set(wanted) - set(known))
    gone = sorted(set(known) - set(wanted))
    changed = sorted(path for path in set(wanted) & set(known)
                     if wanted[path] != known[path])
    out.write(f"index has: {len(known)} files\n")
    out.write(f"would index: {len(wanted)} files\n")
    out.write(f"delta: +{len(new)} -{len(gone)} ~{len(changed)}\n")
    for path in new:
        out.write(f"  + {path}\n")
    for path in gone:
        out.write(f"  - {path}\n")
    for path in changed:
        out.write(f"  ~ {path}\n")
