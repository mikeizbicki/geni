"""bps: the byte rate, and its two renderings.

A recorded round stored byte totals on a 100ms grid (see store.Recorder);
this is the readout that turns those bytes into the rate a person actually
thinks in -- bytes of output per second -- and either hands them over as
a table or draws them as a fan of curves.

    dic bps --format=csv                       > rates.tsv
    dic bps --format=svg                       > rates.svg
    dic bps --format=svg -m anthropic
    dic bps --format=svg -m anthropic+opus --upstream anthropic
    dic bps --format=svg --where "status = 200"

The rate is cumulative and plotted against the generation itself.  For one
round, x is the bytes generated so far -- reasoning and answer together --
and y is those bytes over the decode time that produced them,
`1000 * gen_b / (t_ms - ttft_ms)`.  A call therefore starts fast and decays
toward its steady rate: the first token has already been paid for, and the
queue and prefill that preceded it are not decode time.  The asymptote,
printed in each panel's caption, is the number a person wants.

The samples are pooled, not averaged per call.  Every sample of every
matching round is cut into ten buckets of x, and each bucket reports the
median y and its interquartile range, so a bucket is a distribution across
calls rather than one line.  NTILE is a windowed rank and does what a
percentile() extension would do without one; the middle row and the
quartiles are picked with the row numbers it produces.

The x axis is bytes and not tokens on purpose.  A byte is what Trace counts
and what the wire carries, and bytes per token varies by register -- prose,
code and a thinking block each compress differently -- so a token axis
would be an assumption about the text that this readout does not need to
make.

`-m PAT` and `--upstream PAT` are the two filters a person reaches for,
and their values are *bound*: a '+' in a model id or a '/' in an upstream
is not SQL punctuation, and a shell has already decided what the quotes
mean.  A PAT without a '+' names the head of a model_id chain, so
`-m anthropic` narrows to every anthropic+* model without listing them;
with a '+' it names one model.  `--upstream PAT` matches the router's own
name for the upstream that answered, and `--upstream ''` is the rows no
router named -- a direct provider.

`--where SQL` is the escape hatch beneath them, handed to sqlite
unparsed and AND-ed with the two above: it is argv, and the same user
could open dic.db and ask the same question directly.  The names it may
use are the columns of the messages table plus `head` (the first name of
the model_id chain) and `upstream` (the router's own field).  A statement
sqlite refuses is reported with that list, because "no such column:
anthropic" is what a quoted literal was meant to prevent.

`--format` names the rendering and nothing else, so a third format is one
more branch here and no new flag.
"""
import sqlite3

from dic.options import flag
from dic.tty import DicError

# What --where may name, appended to sqlite's own message: a column that
# does not exist is sqlite's error, but a *name* meant as a literal is the
# mistake a user actually makes, and the message has to say so.
FIELDS = ("the names --where may use are the columns of messages, plus head"
          " (a model's provider, the first name of its id chain) and upstream"
          " (the router-chosen upstream); a literal must be quoted, but -m"
          " and --upstream take one without quotes")

SOURCE = """
WITH m AS (
    SELECT *,
           substr(model_id, 1, instr(model_id || '+', '+') - 1) AS head
      FROM messages
     WHERE status = 200 AND tokens_output IS NOT NULL AND t_first IS NOT NULL),
f AS (SELECT * FROM m WHERE {where}),
s AS (
    SELECT f.mid, f.model_id, f.provider,
           json_extract(j.value, '$[0]') AS t_ms,
           json_extract(j.value, '$[1]') AS think_b,
           json_extract(j.value, '$[2]') AS resp_b,
           (f.t_first - f.t_start) / 1e6 AS ttft_ms
      FROM f JOIN record r ON r.mid = f.mid,
           json_each(r.samples) j)
"""

RATES = SOURCE + """,
pt AS (
    SELECT mid, model_id, provider, t_ms, think_b, resp_b,
           (think_b + resp_b) AS gen_b,
           1000.0 * (think_b + resp_b) / (t_ms - ttft_ms) AS y
      FROM s
     WHERE t_ms > ttft_ms AND (think_b + resp_b) > 0),
flag AS (
    SELECT *, max(think_b) OVER (PARTITION BY mid) AS mthink FROM pt),
edge AS (
    SELECT mid, model_id, provider, max(gen_b) AS bound
      FROM flag WHERE think_b = mthink AND mthink > 0 GROUP BY mid),
bounds AS (
    SELECT model_id, provider, avg(bound) AS bound FROM edge GROUP BY 1, 2),
samples AS (
    SELECT pt.*,
           ntile(10) OVER (PARTITION BY model_id, provider ORDER BY gen_b)
             AS bucket
      FROM pt),
ranked AS (
    SELECT samples.*,
           count(*) OVER (PARTITION BY model_id, provider, bucket) AS n,
           row_number() OVER (PARTITION BY model_id, provider, bucket
                              ORDER BY y) AS r
      FROM samples)
SELECT r.model_id, r.provider, r.bucket,
       avg(r.gen_b) AS gen_b,
       avg(r.y) FILTER (WHERE r.r IN ((r.n + 1) / 2, (r.n + 2) / 2)) AS y,
       avg(r.y) FILTER (WHERE r.r = (r.n + 3) / 4) AS y_lo,
       avg(r.y) FILTER (WHERE r.r = (3 * r.n + 3) / 4) AS y_hi,
       count(*) AS n,
       avg(1.0 * r.think_b / r.gen_b) AS think_frac,
       coalesce(max(b.bound), 0) AS bound
  FROM ranked r
  LEFT JOIN bounds b
    ON b.model_id = r.model_id AND b.provider IS r.provider
 GROUP BY r.model_id, r.provider, r.bucket
 ORDER BY r.model_id, r.provider, r.bucket
"""  # noqa: S608

POINTS = SOURCE + """
SELECT mid, model_id, provider, (think_b + resp_b) AS gen_b,
       1000.0 * (think_b + resp_b) / (t_ms - ttft_ms) AS y
  FROM s
 WHERE t_ms > ttft_ms AND (think_b + resp_b) > 0
 ORDER BY model_id, provider, mid, gen_b
"""

COLUMNS = ("gen_b", "y", "model_id", "provider", "bucket", "n", "think_frac",
           "y_lo", "y_hi", "bound")

# The header names the two quantities in their own units and leaves the rest
# as the query returned them, so reading the TSV does not mean reading the SQL.
HEADER = "bytes\tbps\t" + "\t".join(COLUMNS[2:])


def clause(model=None, upstream=None, where=None):
    """(sql, params): --where raw, then -m and --upstream with bound values.

    --where is interpolated because it is SQL the same user could type at
    sqlite's own prompt; -m and --upstream are values and are bound,
    because a '+' or a '/' in a model name is not SQL.  The three compose
    with AND, so a narrower filter never widens the one before it.

    >>> clause()
    ('1', [])
    >>> clause("anthropic")
    ('(model_id = ? OR head = ?)', ['anthropic', 'anthropic'])
    >>> clause(upstream="")
    ('upstream IS NULL', [])
    >>> clause("a", "b", "status = 200")
    ('(status = 200) AND (model_id = ? OR head = ?) AND upstream = ?', ['a', 'a', 'b'])
    """
    parts, params = [], []
    if where:
        parts.append(f"({where})")
    if model:
        # a chain names one model; a single name names every model whose
        # chain starts with it
        parts.append("(model_id = ? OR head = ?)")
        params.extend((model, model))
    if upstream is not None:
        parts.append("upstream IS NULL" if upstream == "" else "upstream = ?")
        if upstream:
            params.append(upstream)
    return " AND ".join(parts) or "1", params


def run(conn, where, params=()):
    """Every binned median and its quartiles, as sqlite rows."""
    return execute(conn, RATES, where, params)


def points(conn, where, params=()):
    """Every sample of every matching round: the faint per-call lines."""
    return execute(conn, POINTS, where, params)


def execute(conn, sql, where, params):
    """One of the two queries, its --where spliced and its values bound.

    The only place sqlite3 may raise, so the one place that can turn "no
    such column" into the sentence that says what a name had to be.
    """
    try:
        return conn.execute(sql.format(where=where), params).fetchall()
    except sqlite3.OperationalError as e:
        raise DicError(f"{e}\n  {FIELDS}") from None


def fmt(v):
    """One cell as text: an integer as itself, a float to two places, NULL empty.

    >>> fmt(5), fmt(12.3456), fmt(None)
    ('5', '12.35', '')
    """
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def csv(conn, where, params=()):
    """The rates as TSV: one row per (bucket, model, provider).

    Long and not wide, so that `sort`, `awk` and a plotter's `using 1:2`
    are the readers it was written for.  x is bytes generated, y is bytes
    per second, and `n` is how many samples stand behind the row.  `bucket`
    is the decile of x the row's samples fell in, 1 through 10, and `y_lo`
    and `y_hi` are the quartiles around y, so the band the SVG draws behind
    its median can be drawn from this table too.
    """
    lines = [HEADER]
    lines.extend("\t".join(fmt(row[c]) for c in COLUMNS)
                 for row in run(conn, where, params))
    return "\n".join(lines) + "\n"


def fit(rows):
    """Least squares a, b of 1/y = a + b*x over the rows, or (None, None).

    >>> a, b = fit([{"gen_b": 100, "y": 100.0}, {"gen_b": 1000, "y": 50.0}])
    >>> round(a, 6), round(b, 8)
    (0.008889, 1.111e-05)
    """
    pairs = [(float(r["gen_b"]), 1.0 / r["y"]) for r in rows if r["y"]]
    if len(pairs) < 2:
        return None, None
    n = len(pairs)
    mean_x = sum(x for x, _ in pairs) / n
    mean_u = sum(u for _, u in pairs) / n
    sxx = sum((x - mean_x) ** 2 for x, _ in pairs)
    if not sxx:
        return None, None
    sxy = sum((x - mean_x) * (u - mean_u) for x, u in pairs)
    b = sxy / sxx
    return mean_u - b * mean_x, b


def shade(n):
    """The IQR band's fill opacity.

    With one or two calls behind a bucket there is no distribution, only a
    pair of points, so the band is drawn at full strength to read as the
    line it is; with more it fades, because the median is what is being
    read and the band is only its spread.

    >>> shade(1), shade(50) < shade(10) <= 0.35
    (1.0, True)
    """
    if n <= 2:
        return 1.0
    return max(0.08, 0.35 - 0.01 * n)


def svg(conn, where, params=(), width=720, height=360, margin=48):
    """The rates as an SVG: one panel per (model, provider), stacked.

    A panel is drawn in linear axes because the quantity is linear: the
    per-call samples are faint gray, their median is a bold blue line, the
    interquartile band is one filled path behind it, and a dashed gray guide
    is the fitted `1/(b*x)`.  A red tick marks where reasoning stopped, so
    the two registers a call mixes are visible.

    Nothing here rasterizes, shells out or imports a plotting library,
    because the whole answer is text on stdout and every renderer reads SVG.
    """
    rows = run(conn, where, params)
    head = (f"<svg xmlns='http://www.w3.org/2000/svg'"
            f" width='{width}' height='{height}'"
            f" viewBox='0 0 {width} {height}'>"
            f"<rect width='{width}' height='{height}' fill='#ffffff'/>")
    if not rows:
        return head + "</svg>\n"

    panels = {}
    for row in rows:
        panels.setdefault((row["model_id"], row["provider"]), []).append(row)
    calls = {}
    for point in points(conn, where, params):
        calls.setdefault((point["model_id"], point["provider"]), {}
                         ).setdefault(point["mid"], []).append(point)
    items = sorted(panels, key=lambda key: (key[0], key[1] or ""))

    top, bottom = margin, height - margin
    per = (bottom - top) / len(items)
    parts = []
    for i, key in enumerate(items):
        ptop = top + i * per
        plot_top, plot_bottom = ptop + 18, ptop + per - 24
        left, right = margin + 8, width - margin
        data = panels[key]
        lines = calls.get(key, {})
        x_max = max([r["gen_b"] for r in data]
                    + [p["gen_b"] for line in lines.values() for p in line]
                    + [1])
        y_max = max([r["y"] or 0 for r in data]
                    + [r["y_hi"] or 0 for r in data]
                    + [p["y"] or 0 for line in lines.values() for p in line]
                    + [1])
        x_span, y_span = float(x_max), float(y_max)

        def px(v, left=left, right=right, x_span=x_span):
            return left + float(v) / x_span * (right - left)

        def py(v, top=plot_top, bottom=plot_bottom, y_span=y_span):
            return bottom - float(v or 0) / y_span * (bottom - top)

        parts.append(f"<line x1='{left}' y1='{plot_bottom}' x2='{right}'"
                     f" y2='{plot_bottom}' stroke='#999999'/>")
        parts.append(f"<line x1='{left}' y1='{plot_top}' x2='{left}'"
                     f" y2='{plot_bottom}' stroke='#999999'/>")
        model_id, provider = key
        label = model_id + (f"  via {provider}" if provider else "")
        parts.append(f"<text x='{left}' y='{ptop + 12}' font-size='11'"
                     f" fill='#333333'>{label}</text>")
        parts.append(f"<text x='{right}' y='{plot_bottom + 13}' font-size='10'"
                     f" fill='#666666' text-anchor='end'>"
                     f"{x_max:.0f} generated bytes</text>")
        mid_y = (plot_top + plot_bottom) / 2
        parts.append(f"<text x='{left - 6}' y='{mid_y}' font-size='10'"
                     f" fill='#666666' text-anchor='middle'"
                     f" transform='rotate(-90 {left - 6} {mid_y})'>"
                     f"bytes/s (cumulative mean)</text>")
        parts.append(f"<text x='{left - 6}' y='{plot_top + 8}' font-size='10'"
                     f" fill='#666666' text-anchor='end'>{y_max:.0f}</text>")

        for line in lines.values():
            if len(line) < 2:
                continue
            d = "M " + " L ".join(
                f"{px(p['gen_b']):.1f},{py(p['y']):.1f}" for p in line)
            parts.append(f"<path d='{d}' fill='none' stroke='#cccccc'"
                         f" stroke-width='0.8'/>")

        band = [r for r in data if r["y_lo"] and r["y_hi"]]
        if len(band) >= 2:
            going = " L ".join(
                f"{px(r['gen_b']):.1f},{py(r['y_hi']):.1f}" for r in band)
            back = " L ".join(
                f"{px(r['gen_b']):.1f},{py(r['y_lo']):.1f}"
                for r in reversed(band))
            opacity = shade(max(r["n"] for r in band))
            parts.append(f"<path d='M {going} L {back} Z' fill='#1f77b4'"
                         f" fill-opacity='{opacity:.2f}' stroke='none'/>")

        median = [r for r in data if r["y"]]
        if len(median) >= 2:
            d = "M " + " L ".join(
                f"{px(r['gen_b']):.1f},{py(r['y']):.1f}" for r in median)
            parts.append(f"<path d='{d}' fill='none' stroke='#1f77b4'"
                         f" stroke-width='2'/>")

        a, b = fit(median)
        if b and b > 0:
            guide = []
            for j in range(1, 81):
                x = x_span * j / 80
                v = 1.0 / (b * x)
                if v <= y_span:
                    guide.append((x, v))
            if len(guide) >= 2:
                d = "M " + " L ".join(
                    f"{px(x):.1f},{py(v):.1f}" for x, v in guide)
                parts.append(f"<path d='{d}' fill='none' stroke='#888888'"
                             f" stroke-dasharray='4 3' stroke-width='1'/>")

        samples = sum(r["n"] for r in data)
        if a is not None:
            caption = (f"{samples} samples; fit"
                       f" 1/({a:.4g} + {b:.4g}x)")
        else:
            caption = f"{samples} samples"
        parts.append(f"<text x='{left}' y='{ptop + per - 8}' font-size='10'"
                     f" fill='#666666'>{caption}</text>")

        bound = max((r["bound"] or 0) for r in data)
        if 0 < bound <= x_span:
            xb = px(bound)
            parts.append(f"<line x1='{xb:.1f}' y1='{plot_top}'"
                         f" x2='{xb:.1f}' y2='{plot_bottom}'"
                         f" stroke='#d62728' stroke-dasharray='2 3'"
                         f" stroke-width='1'/>")
            parts.append(f"<text x='{xb + 3:.1f}' y='{plot_top + 9}'"
                         f" font-size='9' fill='#d62728'>thinking ends</text>")

    return head + "".join(parts) + "</svg>\n"


def bps(conn, env, *,  # noqa: ARG001
        format_: flag(long="--format", metavar="FMT", env=False,
                      help="csv (the default) or svg") = None,
        model: flag(short="-m", metavar="PAT", env=False,
                    help="only this model, or every model of this provider:"
                         " a PAT without a '+' names the head of the chain") = None,
        upstream: flag(long="--upstream", metavar="PAT", env=False,
                       help="only the rounds a router served from this"
                            " upstream; '' is the rows no router named") = None,
        where: flag(long="--where", env=False,
                    help="a raw WHERE clause, AND-ed with the two above; the"
                         " names it may use are the columns of messages,"
                         " plus head and upstream") = None):
    """The rates, as TSV or as an SVG fan of deciles over bytes generated.

    Both renderings are the same ten deciles of generated bytes per (model,
    provider): the median rate at each, the quartiles around it, and how
    many samples stand behind it.  -m and --upstream name the two rows a
    person asks about without writing SQL; --where is the escape hatch
    beneath them.
    """
    kind = format_ or "csv"
    built, params = clause(model, upstream, where)
    if kind == "csv":
        return csv(conn, built, params)
    if kind == "svg":
        return svg(conn, built, params)
    raise DicError(f"--format must be csv or svg, not {kind!r}")
