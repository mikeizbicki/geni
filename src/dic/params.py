"""Per-model facts for --help: one sidecar sqlite, fetched once per model.

A model's params are not a configuration fact -- they come from the
provider's own catalog, they change when the provider changes the
model, and they cost one HTTP round trip per model to fetch.  They are
therefore not in models.json: that file is read on every call and adding
fetched data to it would make every call pay for a lookup no call uses.
They are cached in a sqlite file beside dic.db, keyed by the resolved
``model_id``, and read back on the next ``--help``.

The cache has one version and refuses an old file by rebuilding it, not
by migrating: the file is disposable, so a version check costs one
pragma and a drop, and a file whose shape an old build wrote cannot
lie about what the current build would have stored.

    dic/params.py    the sidecar cache
    dic/__main__.py  model_help -- which asks, and prints
"""
import json, os, sqlite3

from dic.store import state_dir

# The shape this file writes.  A rebuild is one DROP and one CREATE, so
# there is nothing to gain from a migration and one bug to lose by it.
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS details(dic_id TEXT PRIMARY KEY, json TEXT);
"""


def details_path(env):
    """The sidecar file: $XDG_STATE_HOME/geni/details.db.

    State and not config, for the reason models.json is state: the file
    is dic's own output and not one a user edits.

    >>> details_path({"HOME": "/u"}).endswith("geni/details.db")
    True
    """
    return os.path.join(state_dir(env), "details.db")


def open_db(env):
    """The sidecar connection; a file of another version is rebuilt."""
    path = details_path(env)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        conn.executescript("DROP TABLE IF EXISTS details")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.executescript(SCHEMA)
    return conn


def cached(env, dic_id):
    """The cached params for one id, or None when there are none yet.

    None is a miss and not an empty answer: a model whose fetch returned
    no params at all stored ``{}``, and this returns that dict, not None.

    >>> import tempfile
    >>> with tempfile.TemporaryDirectory() as d:
    ...     cached({"HOME": d}, "x") is None
    True
    """
    conn = open_db(env)
    try:
        row = conn.execute("SELECT json FROM details WHERE dic_id = ?",
                           (dic_id,)).fetchone()
    finally:
        conn.close()
    return json.loads(row["json"]) if row else None


def store(env, dic_id, table):
    """Record one id's params, replacing any earlier answer for it.

    >>> import tempfile
    >>> with tempfile.TemporaryDirectory() as d:
    ...     env = {"HOME": d}
    ...     store(env, "x", {"a": {"type": "string"}})
    ...     cached(env, "x")
    {'a': {'type': 'string'}}
    """
    conn = open_db(env)
    try:
        with conn:
            conn.execute("INSERT OR REPLACE INTO details VALUES (?, ?)",
                         (dic_id, json.dumps(table)))
    finally:
        conn.close()
