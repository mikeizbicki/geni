"""Persistence: the sqlite message tree, attachment blobs, session pointers,
and the parsed configuration cache that config.py fills.

Everything in dic that touches the disk lives here, and nothing that dic
prints: colour, errors, the cost line and the meters are all tty.py.  The
rest of the program sees only the provider-neutral intermediate
representation of a conversation: a list of turns

    {"role": "user"|"assistant", "blocks": [block, ...], "raw": <provider json>}

where a block is {"type": "text"|"image"|"tool_call"|"tool_result"|"thinking",
...}.  "raw" is present only when the stored turn was produced by the api_type
we are about to call again, in which case the adaptor replays it verbatim and
full fidelity (signatures, reasoning, cache prefix) is preserved; otherwise
the adaptor converts the blocks and provider-opaque ones are dropped whole.

A call's other output is its usage: a disjoint dict of what the API counted, in
the names a price table has rules for, stored beside the cost those rules made
of it.  `tokens` and `openai_usage` are how an adaptor builds one.
"""
import base64, hashlib, json, mimetypes, os, sqlite3, time, urllib.parse

from dic.tty import DicError

# The schema this file writes, stamped into the database's user_version.  No
# version of dic migrates another one: dic is pre-release, so an older file is
# not upgraded but reported with the rm that removes it, because a database
# that is only nearly right fails later as a confusing sqlite error.
SCHEMA_VERSION = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    mid TEXT PRIMARY KEY,
    round INTEGER,               -- which API call of this user turn the row is
    user TEXT,
    system TEXT,
    response TEXT,
    response_raw TEXT,
    prev_mid TEXT,
    attachments TEXT,
    outputs TEXT,                -- the files this turn produced: {path, mime_type}
    tool_results TEXT,           -- what this round's tools answered, and how long
    model_id TEXT,
    api_type TEXT,
    session TEXT,                -- the DIC_SESSION this row was written under
    provider TEXT,               -- the upstream a router chose, NULL if none
    status INTEGER,              -- HTTP status, NULL if we never got one
    error TEXT,                  -- the server's body when status <> 200
    usage TEXT,                  -- disjoint counts: "in.cache_read" -> 8000
    cost REAL,                   -- dollars billed, as rated at the time
    cost_items TEXT,             -- the price rules that produced cost
    price_hash TEXT,             -- which price table they were read from
    t_start INTEGER,             -- ns, before any import but `time`
    t_connect INTEGER,           -- TCP+TLS up
    t_request INTEGER,           -- request body written: end of our overhead
    t_headers INTEGER,           -- response headers in: queue + prefill start
    t_first INTEGER,             -- first token printed
    t_last INTEGER,              -- stream closed
    t_done INTEGER,              -- row written, just before exit
    -- The token counts stay, as views onto usage: a count that is a function
    -- of a count is not stored, and everything outside this file goes on
    -- reading the three columns it always did.  The paths are quoted because
    -- a usage name contains a dot, which unquoted means "go one level down".
    tokens_input INTEGER GENERATED ALWAYS AS
        (json_extract(usage, '$."in"')) VIRTUAL,
    tokens_output INTEGER GENERATED ALWAYS AS
        (json_extract(usage, '$."out"')) VIRTUAL,
    tokens_reasoning INTEGER GENERATED ALWAYS AS
        (json_extract(usage, '$."out.reasoning"')) VIRTUAL,
    -- the hit count, beside the three: a cache read is the one derived
    -- number a caller asks for by name, and it is not part of `in`
    tokens_cache_read INTEGER GENERATED ALWAYS AS
        (json_extract(usage, '$."in.cache_read"')) VIRTUAL);
CREATE INDEX IF NOT EXISTS messages_prev_mid ON messages(prev_mid);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session);
-- Every derived number is a subtraction of two stored instants, so the view
-- is a view: nothing is materialized, nothing can go stale, and no python
-- computes a statistic.  `head` is the first name of the model_id chain --
-- the provider entry a model inherits from -- and `provider` is the upstream
-- that actually answered, which only a router reports.
CREATE VIEW IF NOT EXISTS stats AS SELECT
    mid, round, model_id, api_type, status, provider,
    cost, price_hash,
    coalesce(json_array_length(tool_results), 0) AS tools,
    substr(model_id, 1, instr(model_id || '+', '+') - 1) AS head,
    t_start / 1000000000 AS time,
    (t_connect - t_start)  / 1e6 AS ms_connect,
    (t_request - t_start)  / 1e6 AS ms_overhead,
    (t_headers - t_request)/ 1e6 AS ms_wait,
    (t_first   - t_start)  / 1e6 AS ms_ttft,
    (t_last    - t_first)  / 1e6 AS ms_stream,
    (t_done    - t_last)   / 1e6 AS ms_teardown,
    (t_done    - t_start)  / 1e6 AS ms_total,
    tokens_input, tokens_output, tokens_reasoning, tokens_cache_read,
    1e9 * (tokens_output + coalesce(tokens_reasoning, 0))
        / nullif(t_last - t_first, 0) AS tok_per_sec
  FROM messages;
-- An attachment is a file, and the file is the truth: the bytes are never
-- copied into the database, so a video exists once however many turns name
-- it, and the hash is kept to notice a file that changed under us.
CREATE TABLE IF NOT EXISTS attachments (
    aid TEXT PRIMARY KEY,
    path TEXT,
    hash TEXT,
    mime_type TEXT);
-- The byte-rate samples one round's streams produced, one row per round that
-- was traced and none for a round that was not: a plot is a separate read of
-- this table and never a column of `messages`, so an untraced install carries
-- no bytes at all and no reader of the message tree pays for the indirection.
CREATE TABLE IF NOT EXISTS trace (
    mid TEXT PRIMARY KEY,
    samples TEXT);
CREATE TABLE IF NOT EXISTS config (
    id TEXT PRIMARY KEY,
    parent TEXT,
    keys TEXT,
    abstract INTEGER,
    alias TEXT,
    pos INTEGER,
    source TEXT);
CREATE INDEX IF NOT EXISTS config_parent ON config(parent);
CREATE TABLE IF NOT EXISTS config_meta (
    path TEXT PRIMARY KEY,
    mtime INTEGER,
    size INTEGER);
"""

# The columns a row is written with, in the order messages declares them.  The
# generated columns are not among them and cannot be: sqlite refuses to have
# them written, which is the point of them.
COLUMNS = ("mid", "round", "user", "system", "response", "response_raw",
           "prev_mid", "attachments", "outputs", "tool_results", "model_id",
           "api_type", "session", "provider", "status", "error", "usage", "cost",
           "cost_items", "price_hash", "t_start", "t_connect", "t_request",
           "t_headers", "t_first", "t_last", "t_done")
INSERT = (f"INSERT INTO messages ({', '.join(COLUMNS)})"  # noqa: S608
          f" VALUES ({', '.join('?' * len(COLUMNS))})")

# What `dic --stats` prints: usage frequency and runtime performance are the
# same aggregate over the same rows, so they are one query.  Averages ignore
# failed calls, which are counted separately.
STATS = """
SELECT model_id, count(*) AS n, sum(status <> 200) AS errors,
       sum(round > 0) AS tool_rounds, sum(tools) AS tool_calls,
       round(avg(ms_overhead) FILTER (WHERE status = 200), 1) AS overhead,
       round(avg(ms_wait)     FILTER (WHERE status = 200), 1) AS wait,
       round(avg(ms_ttft)     FILTER (WHERE status = 200), 1) AS ttft,
       round(max(ms_ttft)     FILTER (WHERE status = 200), 1) AS ttft_max,
       round(avg(tok_per_sec) FILTER (WHERE status = 200), 1) AS tok_s,
       round(avg(ms_total)    FILTER (WHERE status = 200), 1) AS total,
       sum(tokens_input) AS tin, sum(tokens_output) AS tout
       , round(sum(cost), 4) AS cost
       , count(DISTINCT price_hash) AS price_versions
       , sum(tokens_cache_read) AS tcache
       , round(100.0 * sum(tokens_cache_read)
               / nullif(sum(tokens_input) + sum(tokens_cache_read), 0), 1)
         AS cache_hit_pct
  FROM stats GROUP BY model_id ORDER BY n DESC
"""

# Which tools ran, and which of them failed: one row per tool call, read out of
# the JSON that recorded it, because a tool result is a fact about a call and
# not a table dic writes to.  A model and a tool, then their counts -- and a
# duration, which is a subtraction like every other one here.
TOOL_STATS = """
SELECT model_id, json_extract(value, '$.name') AS tool,
       count(*) AS n, sum(NOT json_extract(value, '$.ok')) AS errors,
       round(avg((json_extract(value, '$.t_end')
                  - json_extract(value, '$.t_start')) / 1e6), 1) AS ms
  FROM messages, json_each(messages.tool_results)
 GROUP BY model_id, tool ORDER BY n DESC
"""

# Which upstream a router chose, and how it did: the aggregates --stats prints,
# one level deeper, over the rows that named one.  A provider that answers
# directly sends no `provider` field and so has no rows here at all, which is
# why this is a query of its own rather than a column of --stats -- the
# question is not how a model performed but which route served it.  A model
# whose rows were served by two upstreams at two ttfts is then visible rather
# than averaged, and an empty MODEL is every model.
PROVIDERS = """
SELECT model_id, provider, count(*) AS n, sum(status <> 200) AS errors,
       round(avg(ms_overhead) FILTER (WHERE status = 200), 1) AS overhead,
       round(avg(ms_ttft)     FILTER (WHERE status = 200), 1) AS ttft,
       round(avg(tok_per_sec) FILTER (WHERE status = 200), 1) AS tok_s,
       round(sum(cost), 4) AS cost,
       sum(tokens_cache_read) AS tcache,
       round(100.0 * sum(tokens_cache_read)
               / nullif(sum(tokens_input) + sum(tokens_cache_read), 0), 1)
         AS cache_hit_pct
  FROM stats WHERE provider IS NOT NULL AND (? = '' OR model_id = ?)
 GROUP BY model_id, provider ORDER BY model_id, n DESC
"""

# What `dic --log` prints into a completion picker: one row per message, in
# the order `git log` uses, so fzf reads top-down.  The mid is column one, so
# the picker hides it with --with-nth=2.. and still hands {1} to --show.
# prev_mid comes with each row, because --graph draws the forest from it.
LOG = """
WITH RECURSIVE chain(mid, prev_mid, model_id, usage, user, t_start, status) AS (
    SELECT mid, prev_mid, model_id, usage, user, t_start, status
      FROM messages WHERE mid = ?
  UNION ALL
    SELECT m.mid, m.prev_mid, m.model_id, m.usage, m.user, m.t_start, m.status
      FROM messages m JOIN chain c ON m.mid = c.prev_mid)
SELECT mid, prev_mid, t_start, model_id, user, status,
       coalesce((SELECT sum(value) FROM json_each(usage)), 0) AS tokens
  FROM chain ORDER BY t_start DESC LIMIT ?
"""

LOG_ALL = """
SELECT mid, prev_mid, t_start, model_id, user, status,
       coalesce((SELECT sum(value) FROM json_each(usage)), 0) AS tokens
  FROM messages ORDER BY t_start DESC LIMIT ?
"""

# Every message written under a session and its sub-sessions: the same subtree
# --cost-session sums.  A harness that runs its children under
# DIC_SESSION=parent/scruta-N therefore lists one run here, and not only the
# one conversation its pointer holds.
LOG_SESSION = """
SELECT mid, prev_mid, t_start, model_id, user, status,
       coalesce((SELECT sum(value) FROM json_each(usage)), 0) AS tokens
  FROM messages WHERE session = ? OR session LIKE ? || '/%'
 ORDER BY t_start DESC LIMIT ?
"""

# What `dic --models` prints: every non-abstract id, the environment
# variable its api_key_name resolves to, and whether that variable is set.
# The chain is walked here and not by resolve() because --models asks about
# every model at once, and the key that wins is the one the nearest ancestor
# that states one gives, exactly as json_patch merge would produce.  A model
# whose key is missing is marked rather than hidden: a user chasing a model
# that will not run needs the id and the export it names, and the id stays
# in column one so `tail -n +2 | cut -f1` is what a completion reads.
MODELS = """
WITH RECURSIVE
  have(name) AS (SELECT value FROM json_each(?)),
  anc(root, id, keys, depth) AS (
    SELECT id, id, keys, 0 FROM config WHERE abstract = 0
  UNION ALL
    SELECT anc.root, c.id, c.keys, anc.depth + 1
      FROM config c JOIN anc ON c.id = anc.parent WHERE anc.depth < 32),
  kname(root, depth, name) AS (
    SELECT root, depth, json_extract(keys, '$.api_key_name')
      FROM anc WHERE json_extract(keys, '$.api_key_name') IS NOT NULL),
  nearest(root, name) AS (
    SELECT root, name FROM kname k
     WHERE depth = (SELECT min(depth) FROM kname k2 WHERE k2.root = k.root))
SELECT c.id, n.name AS api_key, have.name IS NOT NULL AS ok
  FROM config c
  LEFT JOIN nearest n ON n.root = c.id
  LEFT JOIN have ON have.name = n.name
 WHERE c.abstract = 0 AND c.pos IS NOT NULL
 ORDER BY c.pos
"""

B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
# The cost of a session and every session nested inside it.  A session name
# is a path -- a subagent that runs under DIC_SESSION=parent/scruta-1 is a
# child of `parent` -- so one index answers the whole subtree, and the slash
# is in the LIKE so that `p` does not swallow `px`.
SESSION_COST = """
SELECT coalesce(sum(cost), 0.0) FROM messages
 WHERE session = ? OR session LIKE ? || '/%'
"""

# The cost of one conversation: a walk of prev_mid, which is the replay
# lineage.  The two are orthogonal -- a session says what a harness run
# cost and a conversation says what a thread cost -- and a subagent that
# deliberately forks a new context appears in the first and not the second.
CONVERSATION_COST = """
WITH RECURSIVE chain(mid) AS (
    SELECT mid FROM messages WHERE mid = ?
  UNION ALL
    SELECT prev_mid FROM messages m JOIN chain c ON m.mid = c.mid
     WHERE m.prev_mid IS NOT NULL)
SELECT coalesce(sum(cost), 0.0) FROM messages
 WHERE mid IN (SELECT mid FROM chain)
"""

# The per-session breakdown of one subtree: each (sub)session's own total,
# so that a tree can be rendered from it.  The rollup is python's job, for
# the reason --stats is: neither is ever on the latency path.
COST_TREE = """
SELECT session, count(*) AS n, coalesce(sum(cost), 0.0) AS cost
  FROM messages
 WHERE session = ? OR session LIKE ? || '/%'
 GROUP BY session ORDER BY session
"""



def ulid():
    """A ULID: 48 bits of millisecond time then 80 random bits, Crockford base32.

    Message ids therefore sort lexicographically by creation time.

    >>> u = ulid()
    >>> len(u), set(u) <= set(B32)
    (26, True)
    """
    n = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(B32[(n >> (5 * i)) & 31] for i in range(25, -1, -1))


class Trace:
    """Byte totals of a round's streams, sampled on a 100ms grid.

    A sample is `[t_ms, thinking, response]`: how many bytes of reasoning
    and how many bytes of answer had arrived by that point of the call, in
    milliseconds since the round's request went out, so that a plot of the
    running total -- or of the rate between two grid points -- is a
    subtraction.  Gridded and not one sample per chunk because a long answer
    is thousands of chunks and ten samples a second is the same picture at a
    hundredth of the bytes.  A round nobody traced has no row, which is not
    an empty one.

    >>> t = Trace(0)
    >>> t.add(50 * 10**6, "response", 5)     # grid 0
    >>> t.add(150 * 10**6, "thinking", 3)    # grid 1 opens
    >>> t.add(190 * 10**6, "thinking", 7)    # still grid 1
    >>> t.add(250 * 10**6, "response", 2)    # grid 2 opens
    >>> t.json()
    '[[0, 0, 5], [100, 10, 5], [200, 10, 7]]'
    >>> Trace(0).json() is None
    True
    """
    GRID_NS = 100 * 10**6

    def __init__(self, t0):
        self.t0 = t0
        self.grid = -1
        self.thinking = 0
        self.response = 0
        self.samples = []

    def add(self, t_ns, kind, nbytes):
        """Record nbytes of the given stream at t_ns.

        A blob is the answer, so it counts as response and not as a stream
        of its own: a video has no tokens to plot and no rate to read.
        """
        if not nbytes:
            return
        if kind == "thinking":
            self.thinking += nbytes
        else:
            self.response += nbytes
        grid = max(0, (t_ns - self.t0) // self.GRID_NS)
        sample = [grid * 100, self.thinking, self.response]
        if grid == self.grid:
            self.samples[-1] = sample
        else:
            self.grid = grid
            self.samples.append(sample)

    def json(self):
        """The samples as JSON, or None when this stream delivered nothing."""
        return json.dumps(self.samples) if self.samples else None


def data_url(block):
    """An image block as an RFC 2397 data: URL, the form both OpenAI APIs take.

    >>> data_url({"type": "image", "mime_type": "image/png", "data": b"hi"})
    'data:image/png;base64,aGk='
    """
    encoded = base64.b64encode(block["data"]).decode()
    return f"data:{block['mime_type']};base64,{encoded}"


def tokens(**counts):
    """The counts that happened, as a usage dict: name -> quantity.

    Names are dotted and disjoint: 'in' is the input a provider charged at its
    usual rate and 'in.cache_read' is the part it served from its cache, so no
    two names describe the same token and a cost is a plain sum over the names
    a price rule matches.  A count that is missing or zero is left out, because
    a usage dict should say what happened and not what did not.

    >>> tokens(**{"in": 1000, "out": 0, "in.cache_read": None})
    {'in': 1000}
    """
    return {name: qty for name, qty in counts.items() if qty}


def openai_usage(usage):
    """An OpenAI usage object as a disjoint dict, its subsets split out.

    Both OpenAI-shaped protocols report what was cached and what was reasoning
    *inside* the totals they also report, so the subsets have to be subtracted
    here: leaving them in would charge a cached prompt twice, once at each of
    the two rates that are supposed to describe different tokens.

    >>> openai_usage({"prompt_tokens": 1000, "completion_tokens": 500,
    ...               "prompt_tokens_details": {"cached_tokens": 800},
    ...               "completion_tokens_details": {"reasoning_tokens": 200}})
    {'in': 200, 'in.cache_read': 800, 'out': 300, 'out.reasoning': 200}
    >>> openai_usage({})
    {}
    """
    prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
    completion = usage.get("completion_tokens", usage.get("output_tokens"))
    cached = (usage.get("prompt_tokens_details")
              or usage.get("input_tokens_details") or {}).get("cached_tokens")
    reasoning = (usage.get("completion_tokens_details")
                 or usage.get("output_tokens_details") or {}).get("reasoning_tokens")
    return tokens(**{
        "in": None if prompt is None else prompt - (cached or 0),
        "in.cache_read": cached,
        "out": None if completion is None else completion - (reasoning or 0),
        "out.reasoning": reasoning})


def multipart(fields, files, field="image"):
    """A multipart/form-data body, as (bytes, content-type).

    /images/edits and /videos take their parameters as form fields and their
    reference files as file parts, so those two are the only requests dic
    cannot send as JSON -- and this is the one place it builds a body by
    hand.  `files` is IR blocks, whose bytes and mime type are already in
    hand; their names go into each part so the server sees what was uploaded.

    >>> body, ctype = multipart({"model": "m"}, [
    ...     {"type": "image", "mime_type": "image/png",
    ...      "path": "a.png", "data": b"hi"}])
    >>> b"name=\\"model\\"" in body, ctype.startswith("multipart/form-data; boundary=")
    (True, True)
    """
    boundary = "----dic" + os.urandom(12).hex()
    out = bytearray()
    for name, value in fields.items():
        out += (f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n").encode()
    for block in files:
        name = os.path.basename(block.get("path") or field)
        out += (f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{field}";'
                f' filename="{name}"\r\n'
                f"Content-Type: {block['mime_type']}\r\n\r\n").encode()
        out += block["data"] + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def http_url(url):
    """The url, if its scheme is http or https; a DicError otherwise.

    urlopen takes file: and ftp: as well, and an adaptor fetches a URL
    that arrived from a provider's own response -- the asset a fal or a
    videos job names -- so without this a hostile response could read a
    local file into an answer.  Every adaptor's urlopen goes through here.

    >>> http_url("https://x/y")
    'https://x/y'
    >>> http_url("file:///etc/passwd")
    Traceback (most recent call last):
    ...
    dic.tty.DicError: refusing non-http url: file:///etc/passwd
    """
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise DicError(f"refusing non-http url: {url}")
    return url


# ---------------------------------------------------------------- sqlite

def config_dir(env):
    """The directory holding dic's database and the user's configuration.

    >>> config_dir({"HOME": "/home/u"})
    '/home/u/.config/fac'
    """
    home = env.get("HOME")
    return (os.path.join(home, ".config", "fac") if home
            else os.path.expanduser("~/.config/fac"))


def db_path(env):
    """The sqlite file holding the message tree and the parsed config cache."""
    return os.path.join(config_dir(env), "dic.db")


def db(env):
    """Open the database named by env, creating the schema when it is new.

    There are no migrations: a file written against any other version of this
    schema is not upgraded, it is deleted.  The version the file carries must
    be the one this build writes, and a file that disagrees is an error naming
    that file and the rm that fixes it -- a database that is only nearly right
    would otherwise fail somewhere later as a confusing sqlite error.
    """
    path = db_path(env)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    stamped = conn.execute("SELECT 1 FROM sqlite_master"
                           " WHERE type='table' AND name='messages'").fetchone()
    if stamped and version != SCHEMA_VERSION:
        conn.close()
        raise DicError(f"{path}: schema mismatch: found version {version},"
                       f" expected {SCHEMA_VERSION}; this is probably due to upgrading/reinstalling dic; you can fix this problem by removing the old database with the command: rm {path}")
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


def history(conn, mid):
    """The ancestor chain of mid, oldest first (one recursive query, one trip).

    `model_id` is carried so that -c and --mid can continue the thread with the
    model that produced its last turn, unless -m names another.
    """
    columns = ("mid,round,user,system,response,response_raw,prev_mid,model_id,"
               "api_type,attachments,outputs,tool_results")
    qualified = ",".join(f"m.{c}" for c in columns.split(","))
    rows = conn.execute(
        f"WITH RECURSIVE chain({columns}) AS ("  # noqa: S608
        f"  SELECT {columns} FROM messages WHERE mid=?"
        "  UNION ALL"
        f"  SELECT {qualified} FROM messages m JOIN chain c ON m.mid=c.prev_mid)"
        " SELECT * FROM chain",
        (mid,)).fetchall()
    return list(reversed(rows))


def resolve_ref(conn, ref, env):
    """A --mid value as a full mid: a git-like ref, or a ULID or its prefix.

    `HEAD` and `@` name the message this session's pointer holds, `HEAD~1`
    its parent and `HEAD~n` n steps above that, so a ref walks the same tree
    -c walks, one prev_mid per step.  Any other value is a mid, or an
    unambiguous prefix of one: a caller may paste the first column of
    `dic --log` without its other twenty-five characters, and a prefix that
    names two messages is an error rather than a guess.
    """
    if not ref:
        return ref
    ref = ref.strip()
    if ref in ("HEAD", "@") or ref.startswith(("HEAD~", "@~")):
        _, _, depth = ref.partition("~")
        mid = session_read(env)
        if not mid:
            raise DicError(
                f"{ref}: no conversation in this session"
                f" (DIC_SESSION={env.get('DIC_SESSION', 'global')})")
        for _ in range(int(depth) if depth else 0):
            row = conn.execute("SELECT prev_mid FROM messages WHERE mid = ?",
                               (mid,)).fetchone()
            if not row or not row["prev_mid"]:
                raise DicError(f"{ref}: {mid} has no parent")
            mid = row["prev_mid"]
        return mid
    row = conn.execute("SELECT mid FROM messages WHERE mid = ?",
                       (ref,)).fetchone()
    if row:
        return row["mid"]
    rows = conn.execute("SELECT mid FROM messages WHERE mid LIKE ?"
                        " ORDER BY mid LIMIT 2", (ref + "%",)).fetchall()
    if len(rows) == 1:
        return rows[0]["mid"]
    if not rows:
        raise DicError(f"no such mid: {ref}")
    raise DicError(f"{ref}: ambiguous between {rows[0]['mid']}"
                   f" and {rows[1]['mid']}")


def file_block(path, mime_type=None):
    """A file as an IR block: its name and type, and its bytes read now.

    The file is the truth, so it is read here and never stored; a path that
    has since moved raises rather than becoming a turn that quietly lost its
    video.

    >>> file_block(__file__)["mime_type"]
    'text/x-python'
    """
    mime_type = (mime_type or mimetypes.guess_type(path)[0]
                 or "application/octet-stream")
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise DicError(f"cannot read {path}: {e}") from None
    return {"type": "image", "mime_type": mime_type, "path": path, "data": data}


def store_attachment(conn, path):
    """Record a file as an attachment and return it as an IR block.

    The file is never copied: an attachment and an output are the same kind of
    thing, so both are a path, a mime type and a hash, and both are re-read
    from disk when the conversation is rebuilt.  The hash is kept so that a
    file edited between two turns is visible in the row rather than silently
    sent as if it had not changed.

    >>> import tempfile
    >>> with tempfile.TemporaryDirectory() as d:
    ...     p = os.path.join(d, "x.png")
    ...     _ = open(p, "wb").write(b"hi")
    ...     store_attachment(db({"HOME": d}), p)["path"] == p
    True
    """
    mime_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    aid = ulid()
    conn.execute("INSERT INTO attachments VALUES (?,?,?,?)",
                 (aid, path, digest, mime_type))
    return dict(file_block(path, mime_type), aid=aid)


def turns_from_rows(conn, rows, api_type):
    """History rows to IR turns, keeping "raw" where the api_type still matches.

    A prompt's attachments and a reply's outputs are both files the row names
    and both are read back here, so continuing a conversation carries
    everything that was in it -- including a video a later turn is asked
    about -- and a file that has since gone is an error, not a dropped block.
    A round that asked for tools is followed by the turn its tools answered
    with, but only while the calls themselves survive: a result whose call was
    dropped in conversion is a turn the API would reject.
    """
    turns = []
    for row in rows:
        blocks = []
        for aid in json.loads(row["attachments"] or "[]"):
            att = conn.execute("SELECT path,mime_type FROM attachments WHERE aid=?",
                               (aid,)).fetchone()
            if att:
                blocks.append(file_block(att["path"], att["mime_type"]))
        blocks.append({"type": "text", "text": row["user"] or ""})
        turns.append({"role": "user", "blocks": blocks})
        reply = {"role": "assistant",
                 "blocks": [{"type": "text", "text": row["response"] or ""}]}
        for out in json.loads(row["outputs"] or "[]"):
            reply["blocks"].append(file_block(out["path"], out["mime_type"]))
        same = row["api_type"] == api_type and row["response_raw"]
        if same:
            reply["raw"] = json.loads(row["response_raw"])
        turns.append(reply)
        results = json.loads(row["tool_results"] or "[]") if same else []
        if results:
            turns.append({"role": "user", "blocks": [
                {"type": "tool_result", "id": r["id"], "content": r["content"]}
                for r in results]})
    return turns


def normalize(turns):
    """Drop empty text blocks and merge consecutive same-role converted turns.

    Turns replayed verbatim are never merged or rewritten.

    >>> normalize([{"role": "user", "blocks": [{"type": "text", "text": ""}]},
    ...            {"role": "user", "blocks": [{"type": "text", "text": "a"}]},
    ...            {"role": "user", "blocks": [{"type": "text", "text": "b"}]}])
    [{'role': 'user', 'blocks': [{'type': 'text', 'text': 'a'}, {'type': 'text', 'text': 'b'}]}]
    >>> normalize([{"role": "assistant", "blocks": [], "raw": ["opaque"]}])
    [{'role': 'assistant', 'blocks': [], 'raw': ['opaque']}]
    """
    out = []
    for turn in turns:
        if "raw" in turn:
            out.append(turn)
            continue
        blocks = [b for b in turn["blocks"] if b["type"] != "text" or b.get("text")]
        if not blocks:
            continue
        if out and out[-1]["role"] == turn["role"] and "raw" not in out[-1]:
            out[-1]["blocks"] = out[-1]["blocks"] + blocks
        else:
            out.append(dict(turn, blocks=blocks))
    return out


# ---------------------------------------------------------------- session

def session_filename(name):
    """A DIC_SESSION name as the one filename that stores its pointer.

    A session name is a path -- `parent/scruta-1` is a child of `parent` --
    so an unescaped '/' would make the pointer of the parent a directory
    and the pointer of the child a file inside it: two things the same
    path cannot be at once.  Escaping is injective ('%' first, then '/'),
    so a name stays one file however deeply it nests.

    >>> session_filename("global"), session_filename("a/b"), session_filename("a%2Fb")
    ('global', 'a%2Fb', 'a%252Fb')
    """
    return name.replace("%", "%25").replace("/", "%2F")


def session_path(env):
    """The tmpfs file holding this shell session's last mid.

    One file per DIC_SESSION value, wiped on logout: no sessions table, no
    garbage collection, no locking, and the mtime is "last used" for free.
    """
    runtime = env.get("XDG_RUNTIME_DIR")
    base = (os.path.join(runtime, "fac", "dic") if runtime
            else f"/tmp/fac-{os.getuid()}/dic")  # noqa: S108
    return os.path.join(base, session_filename(env.get("DIC_SESSION", "global")))


def session_read(env):
    """The mid that -c continues, or None when this session has no conversation.

    None is not a new chat: the caller reports the missing pointer on its own
    stderr, because only the caller knows which stream that is.
    """
    try:
        with open(session_path(env)) as f:
            return f.read().strip()
    except OSError:
        return None


def session_write(mid, env):
    """Point this session at mid, atomically.

    The temporary name is unique to this write and not merely to this
    process: two concurrent calls in one process share a pid, so a batch
    that finished two conversations at once would have had both rename the
    one temp file, the loser finding it already gone.
    """
    path = session_path(env)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    with open(tmp, "w") as f:
        f.write(mid)
    os.replace(tmp, path)
