"""Readouts: what dic says about itself, its history and its spend, and never
a call to a model.

One function per readout, taking the open connection and the environment and
returning the whole reply as a string.  sqlite computes every number and
python joins the cells with tabs and computes nothing, so the same function
is a library call, a shell one-liner and the body of a mode flag.  The
variants of one question -- "how much", asked of a conversation, of a session
subtree, or as a breakdown -- are one function with one `if`, because they
are one query over one index.

    dic/commands.py     the readouts, one function each
    dic/options.py      Flag(group=, mode=): the grouping and the exclusion
    dic/client.py       dic() -- the call to a model, and the one dispatch

"--stats and --models both" is an error and not a menu: a readout is one
question asked of the database and the answer to two is not one table.
`options.modes()` is what says so; this module is what the answer to the one
that was asked looks like.
"""
import json, time

from dic import config
from dic.store import (CONVERSATION_COST, COST_TREE, LOG, LOG_ALL,
                       LOG_SESSION, MODELS, PROVIDERS, SESSION_COST, STATS,
                       TOOL_STATS, resolve_ref, session_read)
from dic.tty import DicError, summary


def table(rows):
    """Rows of sqlite cells as one tab-separated table with a header line.

    Python computes nothing here, so the output is already sort- and
    awk-shaped, and every number in it was computed by sqlite.

    >>> table([])
    ''
    """
    return "".join(
        "\t".join("" if cell is None else str(cell) for cell in row) + "\n"
        for row in ([rows[0].keys()] if rows else [])
        + [list(r) for r in rows])


def session_cost_tree(conn, name):
    """One session's subtree as a table: own cost, and the cost it rolls up.

    A session name is a path, so the subtree of `foo` is `foo` itself and
    every session that begins `foo/`.  Each row is one (sub)session; the
    `subtree` column sums it with its own descendants, so the row `foo`
    is the same number --cost-session prints.  Python does the arithmetic,
    as it does for --stats, because neither is ever on the latency path.

    >>> import sqlite3
    >>> conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    >>> _ = conn.executescript("CREATE TABLE messages(session TEXT, cost REAL);")
    >>> _ = conn.executemany("INSERT INTO messages VALUES (?, ?)",
    ...                      [("a", 1.0), ("a/b", 2.0), ("a/b/c", 4.0)])
    >>> print(session_cost_tree(conn, "a"), end="")  # doctest: +NORMALIZE_WHITESPACE
    session	n	own	subtree
    a	1	1.0000	7.0000
    a/b	1	2.0000	6.0000
    a/b/c	1	4.0000	4.0000
    """
    rows = [(r["session"], r["n"], r["cost"])
            for r in conn.execute(COST_TREE, (name, name))]
    lines = ["\t".join(("session", "n", "own", "subtree"))]
    for session, n, own in rows:
        subtree = sum(c for s, _, c in rows
                      if s == session or s.startswith(session + "/"))
        lines.append("\t".join((session, str(n), f"{own:.4f}",
                                f"{subtree:.4f}")))
    return "".join(line + "\n" for line in lines)


def prompt_line(text, width=60):
    """The first line of a prompt, whitespace collapsed and shortened.

    One list row is one line, so a multi-line prompt is flattened here rather
    than allowed to break the table a picker is reading.

    >>> prompt_line("hello\\nworld")
    'hello'
    >>> len(prompt_line("x" * 70))
    60
    """
    line = " ".join((text or "").split("\n", 1)[0].split())
    return line if len(line) <= width else line[:width - 1] + "…"


def truncate(text, width):
    """text shortened to width columns, with an ellipsis when it is longer.

    >>> truncate("groq+qwen", 20)
    'groq+qwen'
    >>> truncate("openrouter+deepseek", 10)
    'openroute…'
    """
    return text if len(text) <= width else text[:width - 1] + "…"


def ago(ns, now=None):
    """A past instant as GitHub-style elapsed time: 20s, 5m, 1hr.

    A log row is read to find the turn to continue, and "twenty minutes
    ago" locates one where a wall clock does not.

    >>> ago(0, now=20 * 10**9)
    '20s ago'
    >>> ago(0, now=5 * 60 * 10**9)
    '5m ago'
    >>> ago(0, now=3600 * 10**9)
    '1hr ago'
    >>> ago(0, now=3 * 86400 * 10**9)
    '3d ago'
    >>> ago(0, now=40 * 86400 * 10**9)
    '1mo ago'
    """
    seconds = max(0, ((time.time_ns() if now is None else now) - ns) // 10**9)
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}hr ago"
    if seconds < 30 * 86400:
        return f"{seconds // 86400}d ago"
    if seconds < 365 * 86400:
        return f"{seconds // (30 * 86400)}mo ago"
    return f"{seconds // (365 * 86400)}y ago"


def log_rows(conn, mid, limit, everything=False, session=None):
    """The rows --log prints: a session's subtree, all of them, or a chain.

    A chain is walked the same way history() walks it, so a tool loop's rounds
    all appear and the picker shows the tree -c would resume.  A session is a
    name and its sub-sessions, which is the subtree --cost-session sums, so a
    harness that runs children under DIC_SESSION=parent/scruta-N lists one run
    in one place.
    """
    if session is not None:
        return conn.execute(LOG_SESSION, (session, session, limit)).fetchall()
    if everything:
        return conn.execute(LOG_ALL, (limit,)).fetchall()
    if not mid:
        return []
    return conn.execute(LOG, (mid, limit)).fetchall()


def graph_prefixes(rows):
    r"""The graph column of --log's rows: the forest, one line at a time.

    Each entry is (prefix, row): `row` is None for the line a merge draws on
    its own, the `|/` git prints, and otherwise the row itself, with the
    drawing up to its `*` as the prefix.

    A column is a conversation still being walked: it holds the mid its next
    row must be.  A row no column waits for is a tip, and it takes the
    leftmost free column, so a forest packs leftward instead of keeping a
    column for every branch that ever existed.  A row several columns wait
    for is where conversations meet: they merge into the leftmost of them,
    and that merge gets a line of its own, because the columns it absorbs
    are gone by the time the row itself is drawn.

    >>> rows = [{"mid": "A", "prev_mid": "B"},
    ...         {"mid": "C", "prev_mid": "D"},
    ...         {"mid": "D", "prev_mid": "B"},
    ...         {"mid": "B", "prev_mid": None}]
    >>> [(p, "-" if r is None else r["mid"]) for p, r in graph_prefixes(rows)]
    [('*', 'A'), ('| *', 'C'), ('| *', 'D'), ('| /', '-'), ('*', 'B')]
    >>> [p for p, _ in graph_prefixes(
    ...     [{"mid": "a", "prev_mid": None}, {"mid": "b", "prev_mid": None}])]
    ['*', '*']
    """
    columns = []                    # the mid each column waits to see next
    out = []

    def draw(star=None, slash=()):
        """One line: `*` at star, `/` at slash, `|` where a column is live."""
        return "".join(
            "* " if i == star else
            "/ " if i in slash else
            ("| " if column is not None else "  ")
            for i, column in enumerate(columns)).rstrip()

    for row in rows:
        waiting = [i for i, column in enumerate(columns)
                   if column == row["mid"]]
        if waiting:
            primary = waiting[0]
        else:
            primary = columns.index(None) if None in columns else len(columns)
            if primary == len(columns):
                columns.append(row["mid"])
            else:
                columns[primary] = row["mid"]
            waiting = [primary]
        if len(waiting) > 1:
            out.append((draw(slash=waiting[1:]), None))
            for i in sorted(waiting[1:], reverse=True):
                del columns[i]
        out.append((draw(star=primary), row))
        columns[primary] = row["prev_mid"]
        while columns and columns[-1] is None:
            columns.pop()
    return out


def log_table(rows, graph=False):
    r"""Rows of --log as one tab-separated table: mid first, prompt last.

    The header is a row of its own, as --stats's is, so a picker skips it
    with --header-lines=1 and reads column one of every other line as a mid.
    With `graph`, column two is the forest, and the lines a merge draws carry
    no mid: they are a drawing and not a message.

    >>> log_table([])
    'mid\twhen\tmodel\ttokens\tprompt\n'
    >>> log_table([], graph=True)
    'mid\tgraph\twhen\tmodel\ttokens\tprompt\n'
    """
    header = ["mid"] + (["graph"] if graph else []) + [
        "when", "model", "tokens", "prompt"]
    lines = ["\t".join(header)]
    for prefix, row in (graph_prefixes(rows) if graph
                        else [(None, row) for row in rows]):
        if row is None:             # a merge line: a graph and nothing else
            lines.append("\t".join(["", prefix] + [""] * (len(header) - 2)))
            continue
        lines.append("\t".join([row["mid"]] + ([prefix] if graph else []) + [
            ago(row["t_start"]), truncate(row["model_id"] or "", 20),
            str(row["tokens"] or 0), prompt_line(row["user"])]))
    return "".join(line + "\n" for line in lines)


def message_text(conn, mid):
    """One message as label/value lines: what --show prints in a preview.

    Fixed labels, so a picker's pane does not jump as the selection moves,
    and `response` and not `response_raw`, because what a human decides from
    is the answer and not the wire.
    """
    row = conn.execute("SELECT * FROM messages WHERE mid = ?", (mid,)).fetchone()
    if not row:
        raise DicError(f"no such mid: {mid}")
    lines = []

    def put(label, value):
        lines.append(f"{label + ':':<10}{'' if value is None else value}")

    put("mid", row["mid"])
    put("when", time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(row["t_start"] / 1e9))
        if row["t_start"] else "")
    put("model", f"{row['model_id']} ({row['api_type']})")
    put("status", row["status"])
    _, _, cost = summary(json.loads(row["usage"] or "{}"),
                         json.loads(row["cost_items"] or "[]"),
                         None, {}, 1,
                         partial=row["cost"] is None).partition(": ")
    put("cost", cost.split(" --mid=")[0])
    put("system", prompt_line(row["system"]))
    attached = [att["path"] for att in (
        conn.execute("SELECT path FROM attachments WHERE aid = ?", (aid,))
        .fetchone() for aid in json.loads(row["attachments"] or "[]")) if att]
    put("attached", ", ".join(attached))
    put("outputs", ", ".join(o["path"]
                             for o in json.loads(row["outputs"] or "[]")))
    put("tools", ", ".join(f"{t['name']} {'ok' if t['ok'] else 'error'}"
                           for t in json.loads(row["tool_results"] or "[]")))
    put("prompt", " ".join((row["user"] or "").split()))
    lines.append("---")
    lines.append(row["response"] or "")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ readouts

def aliases(conn, env, knobs):  # noqa: ARG001
    """`alias qwen='dic -m groq+qwen'` for every entry that names an alias.

    This is what `dic.sh` evals, so the shell name and the model name come
    from the same table and cannot drift apart.
    """
    return config.aliases(conn)


def models(conn, env, knobs):  # noqa: ARG001
    """Every configured model id, the key it needs, and whether that key is set.

    A model whose key is missing is marked rather than hidden, so a user
    chasing a model that will not run sees the export it names, and the id
    stays in column one, so `tail -n +2 | cut -f1` is what shell completion
    reads.
    """
    return table(conn.execute(MODELS, (json.dumps(sorted(env)),)).fetchall())


def stats(conn, env, knobs):  # noqa: ARG001
    """Per-model usage frequency and runtime performance, plus tool failures.

    Frequency and performance are one aggregate over the same rows, so they
    are one query; the tool rows are a second over json_each, below it,
    because a tool that failed is a turn the model had to correct and not a
    call dic made.
    """
    text = table(conn.execute(STATS).fetchall())
    tools = table(conn.execute(TOOL_STATS).fetchall())
    if tools:
        text += "\n" + tools
    return text


def providers(conn, env, knobs):  # noqa: ARG001
    """The upstream each router chose, one level deeper than --stats.

    A provider that answers directly sends no `provider` field and so has no
    rows here at all, which is why this is a query of its own and not a
    column of --stats: the question is not how a model performed but which
    of its routes did.
    """
    model = knobs["providers"] or ""
    return table(conn.execute(PROVIDERS, (model, model)).fetchall())


def cost(conn, env, knobs):
    """What a conversation or a session subtree spent, at one of three scopes.

    `--cost-of REF` is a conversation: a walk up prev_mid from one message.
    `--cost-session` and `--cost-tree` are a session subtree -- the session
    itself, or its per-session breakdown -- and both default to $DIC_SESSION,
    so a bare flag asks about this shell.  A session is a subtree and a
    conversation is a walk, so the two scopes are orthogonal and each flag
    names which one it means.
    """
    if knobs["cost_of"]:
        spent = conn.execute(
            CONVERSATION_COST,
            (resolve_ref(conn, knobs["cost_of"], env),)).fetchone()[0]
        return f"${spent:.4f}\n"
    name = (knobs["cost_session"] if knobs["cost_session"] is not None
            else knobs["cost_tree"])
    if not name:
        name = env.get("DIC_SESSION", "global")
    if knobs["cost_tree"] is not None:
        return session_cost_tree(conn, name)
    spent = conn.execute(SESSION_COST, (name, name)).fetchone()[0]
    return f"${spent:.4f}\n"


def log(conn, env, knobs):
    """Recent messages: this session's subtree, --all, or a chain from --from.

    The two commands a completion picker is built from: the list it reads,
    and the preview it draws for one row of it.  A session is scoped to
    itself and its children -- the same subtree --cost-session sums -- and
    --all scopes it to the whole database; a bare --session is this shell's
    own session.
    """
    mid = (resolve_ref(conn, knobs["from_"], env) if knobs["from_"]
           else session_read(env))
    scope = knobs["log_session"]
    if scope == "":
        scope = env.get("DIC_SESSION", "global")
    return log_table(log_rows(conn, mid, knobs["limit"] or 200,
                              knobs["all_"], scope),
                     graph=knobs["graph"])


def show(conn, env, knobs):
    """One message's details: the preview --log's picker draws for its row."""
    return message_text(conn, resolve_ref(conn, knobs["show"], env))


# The one dispatch: which readout answers for which flag.  A new mode flag is
# a Flag(mode=True) in `dic()`'s signature and one line here, and --help, the
# exclusive check and this table all read the same declaration.
BY_FLAG = {
    "aliases":      aliases,
    "models":       models,
    "stats":        stats,
    "providers":    providers,
    "cost_session": cost,
    "cost_of":      cost,
    "cost_tree":    cost,
    "log":          log,
    "show":         show,
}
