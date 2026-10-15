"""Readouts: what dic says about itself, its history and its spend, and never
a call to a model.

One function per command, taking the open connection and the environment and
returning the whole reply as a string.  sqlite computes every number and
python joins the cells with tabs and computes nothing, so the same function
is a library call, a shell one-liner and a command of the CLI.  Each
function's options are Flag-annotated parameters, so the parser, the help
and the dispatch are generated from one declaration and cannot drift apart.

    dic/commands.py     the commands, one function each
    dic/options.py      Flag(command=): which parser an option lives on
    dic/client.py       dic() -- the one call to a model

The variants of one question -- "how much", asked of a conversation, of a
session subtree, or as a breakdown -- are one function with one `if`,
because they are one query over one index.
"""
import json, time

from dic import bps, client, config
from dic.options import flag
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


def log_rows(conn, mid, limit, everything=False, session=None, where="1",
             params=()):
    """The rows --log prints: a session's subtree, all of them, or a chain.

    A chain is walked the same way history() walks it, so a tool loop's rounds
    all appear and the picker shows the tree -c would resume.  A session is a
    name and its sub-sessions, which is the subtree --cost-session sums, so a
    harness that runs children under DIC_SESSION=parent/scruta-N lists one run
    in one place.  `where` is --grep and -S, spliced as bps.py splices its own
    --where and AND-ed with the scope, so a filter narrows and never widens.
    """
    where = where or "1"
    if session is not None:
        return conn.execute(LOG_SESSION.format(where=where),
                            (session, session, *params, limit)).fetchall()
    if everything:
        return conn.execute(LOG_ALL.format(where=where),
                            (*params, limit)).fetchall()
    if not mid:
        return []
    return conn.execute(LOG.format(where=where),
                        (mid, *params, limit)).fetchall()


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


# Git's own --oneline: the short mid and the subject, and --decorate's mark
# inline when there is one.  %D writes nothing -- not even its space -- when
# it has nothing to say, so this is git's `%h %s` until a caller asks for a
# decoration, and a caller who never asks never sees a difference.
ONELINE = "%h%D %s"

# The default format: git's --oneline, plus the two things a dic row carries
# that a git commit does not -- the model that answered and how long ago.
# The leading space of %D is git's own convention: a decoration is written
# with it and an absent one writes nothing, so a format puts %D next to its
# neighbour and gets one space when there is something to separate.
DEFAULT_LOG_FORMAT = "%h%D %a %m %s"

# What column two says, in words, for each format dic ships.  A format the
# caller wrote is not here, and it does not need to be: the format itself
# is printed in its place, which is true even when it is not readable, and
# a header that guessed at a caller's format would be wrong about the one
# thing a header is for.
HEADERS = {DEFAULT_LOG_FORMAT: "when model prompt",
           ONELINE: "subject"}


def decorations(row, head, here):
    """%D: what points at this row, git's `(HEAD -> main)`.

    The session pointer -- the message `-c` would resume -- is `HEAD`, and a
    row written by another session is named by that session, so a row read
    out of `--all` says which shell it belongs to and the one `-c` would
    continue is visible in the log rather than only in the picker.

    >>> decorations({"mid": "a", "session": "main"}, "a", "main")
    ' (HEAD)'
    >>> decorations({"mid": "b", "session": "other"}, "a", "main")
    ' (session:other)'
    >>> decorations({"mid": "b", "session": "main"}, "a", "main")
    ''
    """
    names = ["HEAD"] if head and row["mid"] == head else []
    if row["session"] and row["session"] != here:
        names.append(f"session:{row['session']}")
    return f" ({', '.join(names)})" if names else ""


# One function per %-specifier.  The letters are git's where git has one --
# %H the full mid and %h its prefix, %s the subject and %b the body -- and
# dic's where it does not: %m the model, %u the upstream a router chose, %S
# the session, %c the cost, %t the tokens.  %D is the decorations.  A
# specifier dic does not know is an error and never a literal, because a
# format string with a typo in it must not read as a quiet log: the reader
# would be reading the wrong day's work and would never see that they were.
SPECIFIERS = {
    "H": lambda row, _: row["mid"],
    "h": lambda row, _: row["mid"][:10],
    "s": lambda row, _: prompt_line(row["user"]),
    "b": lambda row, _: prompt_line(row["response"]),
    "a": lambda row, _: ago(row["t_start"]),
    "m": lambda row, _: truncate(row["model_id"] or "", 20),
    "u": lambda row, _: row["upstream"] or "",
    "S": lambda row, _: row["session"] or "",
    "c": lambda row, _: "" if row["cost"] is None else f"${row['cost']:.4f}",
    "t": lambda row, _: str(row["tokens"] or 0),
    "D": lambda row, ctx: (decorations(row, ctx["head"], ctx["here"])
                           if ctx["decorate"] else ""),
}


def expand(fmt, row, ctx):
    """fmt with each %-specifier replaced, in git's own syntax.

    %n is a newline and %xNN one byte named by its hex, so a format can build
    a table of its own; %% is a literal percent.  Anything else is an error
    naming the letters that are not, because a dropped specifier is a log
    that reads as if nothing happened.

    >>> row = {"mid": "0123456789abc", "user": "hi\\nthere", "response": "r",
    ...        "t_start": 0, "model_id": "m", "tokens": 7,
    ...        "session": "global", "upstream": None, "cost": 0.5}
    >>> ctx = {"head": None, "here": "global", "decorate": True}
    >>> expand("%h %s", row, ctx)
    '0123456789 hi'
    >>> expand("%m %t %c", row, ctx)
    'm 7 $0.5000'
    >>> expand("%%|%n|%x41", row, ctx)
    '%|\\n|A'
    >>> expand("%Z", row, ctx)
    Traceback (most recent call last):
    ...
    dic.tty.DicError: --format: %Z names no field (try %H %h %s %b %a %m %u %S %c %t %D)
    """
    out, i = [], 0
    while i < len(fmt):
        ch, i = fmt[i], i + 1
        if ch != "%":
            out.append(ch)
            continue
        if i >= len(fmt):
            raise DicError("--format: a trailing % names no field")
        key, i = fmt[i], i + 1
        if key == "%":
            out.append("%")
        elif key == "n":
            out.append("\n")
        elif key == "x" and i + 2 <= len(fmt):
            out.append(chr(int(fmt[i:i + 2], 16)))
            i += 2
        elif key in SPECIFIERS:
            out.append(SPECIFIERS[key](row, ctx))
        else:
            raise DicError(f"--format: %{key} names no field (try"
                           " %H %h %s %b %a %m %u %S %c %t %D)")
    return "".join(out)


def log_where(grep=None, pickaxe=None):
    """The WHERE --grep and -S add, and the values they bind.

    --grep matches the prompt, which is what a person wrote; -S matches the
    prompt and the answer both, because "did the model ever see this" is a
    question about either of them.  Both are LIKE and both are bound, because
    a prompt is text and not SQL, and the pattern's own % and _ stay
    wildcards.

    >>> log_where()
    ('1', [])
    >>> log_where(grep="primes")
    ('(user LIKE ?)', ['%primes%'])
    >>> log_where(pickaxe="def main")
    ('(user LIKE ? OR response LIKE ?)', ['%def main%', '%def main%'])
    """
    parts, params = [], []
    if grep:
        parts.append("(user LIKE ?)")
        params.append(f"%{grep}%")
    if pickaxe:
        parts.append("(user LIKE ? OR response LIKE ?)")
        params.extend((f"%{pickaxe}%", f"%{pickaxe}%"))
    return " AND ".join(parts) or "1", params


def log_table(rows, graph=False, fmt=None, decorate=False, head=None,
              here="global"):
    r"""The rows of --log: each row's mid, a tab, then the format expanded.

    Column one is the mid, so a picker hides it with --with-nth=2.. and
    previews a row with `dic show {1}` without a second query, and a line
    that is a merge and not a message carries no mid and is therefore not
    pickable.  The graph is drawn at the head of the second column rather
    than of the line, because the mid is what a caller pastes back and it
    must be reachable however the forest is drawn.

    The header names column two: the words dic wrote for the formats it
    ships, and the format itself for one the caller wrote, which is true
    even when it is not readable.  A header that guessed at a caller's
    format would be wrong about the one thing a header is for.

    >>> log_table([])
    'mid\twhen model prompt\n'
    >>> log_table([], fmt=ONELINE)
    'mid\tsubject\n'
    >>> log_table([], fmt="%H")
    'mid\t%H\n'
    >>> log_table([], graph=True, fmt="%H %s")
    'mid\tgraph %H %s\n'
    """
    fmt = DEFAULT_LOG_FORMAT if fmt is None else fmt
    ctx = {"head": head, "here": here, "decorate": decorate}
    lines = ["mid\t" + ("graph " if graph else "") + HEADERS.get(fmt, fmt)]
    for prefix, row in (graph_prefixes(rows) if graph
                        else [(None, row) for row in rows]):
        if row is None:             # a merge line: a graph and nothing else
            lines.append("\t" + prefix)
            continue
        lines.append(f"{row['mid']}\t"
                     f"{prefix + ' ' if prefix else ''}"
                     f"{expand(fmt, row, ctx)}")
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

def models(conn, env, *,
           aliases: flag(long="--aliases", action="bool", env=False,
                         help="print shell alias definitions for `dic.sh` to"
                              " eval, and nothing else") = False):
    """Every configured model id, the key it needs, and whether that key is set.

    A model whose key is missing is marked rather than hidden, so a user
    chasing a model that will not run sees the export it names, and the id
    stays in column one, so `tail -n +2 | cut -f1` is what shell completion
    reads.  With --aliases the same table's aliases are printed instead, so
    the shell name and the model name come from one place and cannot drift.
    """
    if aliases:
        return config.aliases(conn)
    return table(conn.execute(MODELS, (json.dumps(sorted(env)),)).fetchall())


def stats(conn, env, *,  # noqa: ARG001
          model: flag(short="-m", metavar="PAT", env=False,
                      help="only this model, or every model of this provider:"
                           " a PAT without a '+' names the head of the chain,"
                           " so `-m anthropic` narrows to every anthropic+*") = None,
          upstreams: flag(long="--upstreams", action="bool", env=False,
                          help="break each model's rows down by the upstream"
                               " a router chose, instead of by model") = False):
    """Per-model usage frequency and runtime performance, plus tool failures.

    Frequency and performance are one aggregate over the same rows, so they
    are one query; the tool rows are a second over json_each, below it,
    because a tool that failed is a turn the model had to correct and not a
    call dic made.  -m narrows every query to one model, or to every model
    whose id chain starts with one; --upstreams is that same aggregate one
    level deeper, over the rows a router named an upstream for, because a
    model served slowly by one upstream and quickly by another is otherwise
    a single average.
    """
    scope = (model or "", model or "", model or "")
    if upstreams:
        return table(conn.execute(PROVIDERS, scope).fetchall())
    text = table(conn.execute(STATS, scope).fetchall())
    tools = table(conn.execute(TOOL_STATS, scope).fetchall())
    if tools:
        text += "\n" + tools
    return text


def cost(conn, env, *,
         session: flag(long="--session", metavar="NAME", env=False,
                       help="a session's spend, its sub-sessions included"
                            " (default: $DIC_SESSION)") = None,
         tree: flag(long="--tree", action="bool", env=False,
                    help="print the per-session breakdown of a session"
                         " subtree") = False,
         of: flag(long="--of", metavar="REF", env=False,
                  help="the spend of the conversation ending at REF") = None):
    """What a conversation or a session subtree spent.

    `--of REF` is a conversation: a walk up prev_mid from one message.
    `--session` is a session subtree -- the session itself, or with --tree
    its per-session breakdown -- and defaults to $DIC_SESSION, so a bare
    flag asks about this shell.  A session is a subtree and a conversation
    is a walk, so the two scopes are orthogonal and each flag names one.
    """
    if of:
        spent = conn.execute(
            CONVERSATION_COST, (resolve_ref(conn, of, env),)).fetchone()[0]
        return f"${spent:.4f}\n"
    name = session or env.get("DIC_SESSION", "global")
    if tree:
        return session_cost_tree(conn, name)
    spent = conn.execute(SESSION_COST, (name, name)).fetchone()[0]
    return f"${spent:.4f}\n"


def log(conn, env, *,
        limit: flag(short="-n", metavar="N", type=int, env=False,
                    help="how many rows --log prints (default 200)") = None,
        all_: flag(long="--all", action="bool", env=False,
                   help="list every message, not just this session") = False,
        session: flag(long="--session", metavar="NAME", action="?", env=False,
                      help="list a session and its sub-sessions"
                           " (default: $DIC_SESSION)") = None,
        from_: flag(long="--from", metavar="REF", env=False,
                    help="list from REF instead of the session pointer") = None,
        graph: flag(long="--graph", action="bool", env=False,
                    help="draw the forest at the head of each row") = False,
        oneline: flag(long="--oneline", action="bool", env=False,
                      help="shorthand for --format='%%h %%s': the short"
                           " mid and the prompt, as `git log --oneline`"
                           " has it") = False,
        format_: flag(long="--format", metavar="FMT", env=False,
                      help="what each row says, in git's %%-syntax: %%H the"
                           " full mid, %%h its prefix, %%s the prompt,"
                           " %%b the answer, %%a the age, %%m the model,"
                           " %%u the upstream, %%S the session, %%c the"
                           " cost, %%t the tokens, %%D the decorations") = None,
        decorate: flag(long="--decorate", action="bool", env=False,
                       help="fill %%D with (HEAD) on the message -c would"
                            " resume and (session:NAME) on any other row") = False,
        grep: flag(long="--grep", metavar="TEXT", env=False,
                   help="only rows whose prompt contains TEXT") = None,
        pickaxe: flag(short="-S", metavar="TEXT", env=False,
                      help="only rows whose prompt or answer contains TEXT") = None):
    """Recent messages: this session's subtree, --all, or a chain from --from.

    The two commands a completion picker is built from: the list it reads,
    and the preview it draws for one row of it.  A session is scoped to
    itself and its children -- the same subtree --cost-session sums -- and
    --all scopes it to the whole database; a bare --session is this shell's
    own session.  --grep and -S narrow either scope rather than replacing
    it, and -S is the one that answers whether a string ever reached the
    model rather than what was asked for.  --oneline and --format are the
    two ways to say what a row prints, and naming both is an error rather
    than one of them quietly winning.
    """
    mid = (resolve_ref(conn, from_, env) if from_
           else session_read(env))
    scope = session
    if scope == "":
        scope = env.get("DIC_SESSION", "global")
    where, params = log_where(grep, pickaxe)
    if oneline and format_:
        raise DicError("pick one: --oneline or --format")
    return log_table(log_rows(conn, mid, limit or 200, all_, scope,
                              where, params),
                     graph=graph,
                     fmt=format_ or (ONELINE if oneline else None),
                     decorate=decorate,
                     head=session_read(env),
                     here=env.get("DIC_SESSION", "global"))


def show(conn, env, ref=None):
    """One message's details: the preview --log's picker draws for its row."""
    if not ref:
        raise DicError("show needs a message ref")
    return message_text(conn, resolve_ref(conn, ref, env))


# The one dispatch: which function answers for which command name.  A new
# command is a function here and one line, and --help, the parser and the
# dispatch all read the same declaration.  `client.dic` is imported at
# module level because client.py no longer imports this module: the call is
# one command among the readouts, not the other way round.
COMMANDS = {
    "call":  client.dic,
    "log":   log,
    "stats": stats,
    "models": models,
    "cost":  cost,
    "show":  show,
    "bps":   bps.bps,
}
