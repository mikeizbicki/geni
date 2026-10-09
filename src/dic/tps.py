"""tps: the token-rate trace, and its two renderings.

A traced round stored byte totals on a 100ms grid (see store.Trace);
this is the readout that turns those bytes into the rate a person
actually thinks in -- tokens of output per second -- and either hands
them over as a table or draws them as a fan chart.

    dic --tps-csv                       > rates.tsv
    dic --tps-svg                       > rates.svg
    dic --tps-svg --filter "provider LIKE 'openrouter/%'"
    dic --tps-svg --filter "head = 'anthropic' and status = 200"

The rate is per decile, not per call.  Every traced round of one model
and provider is lined up at the same 100ms grid point, and the rates at
that point are cut into ten buckets, so the picture is a distribution
as it evolves over a call rather than one average line.  NTILE is a
windowed rank and does what a percentile() extension would do without
one.  When only one call is in the set, there is only ever one decile
per grid point, and the plot is that call's line.

The filter is the WHERE clause itself, handed to sqlite unparsed.  The
names it may use are the columns of the messages table plus `head`, the
first name of the model_id chain, so `head = 'anthropic'` narrows to
every anthropic+* model without listing them.  A name that is not a
column is sqlite's own error to give, because the filter is argv and
not a boundary: the same user could open dic.db and ask the same
question directly.

See TODO-tps.md.
"""

RATES = """
WITH m AS (
    SELECT *,
           substr(model_id, 1, instr(model_id || '+', '+') - 1) AS head
      FROM messages),
s AS (
    SELECT m.mid, m.model_id, m.provider, m.session,
           m.tokens_output AS tout, m.tokens_reasoning AS tthink,
           json_extract(j.value, '$[0]') AS t_ms,
           json_extract(j.value, '$[1]') AS think_b,
           json_extract(j.value, '$[2]') AS resp_b
      FROM m JOIN trace tr ON tr.mid = m.mid,
           json_each(tr.samples) j
     WHERE {where}),
step AS (
    SELECT mid, model_id, provider, t_ms, tout, tthink,
           resp_b  - lag(resp_b,  1, 0) OVER w AS d_resp,
           think_b - lag(think_b, 1, 0) OVER w AS d_think,
           t_ms    - lag(t_ms,    1, 0) OVER w AS dt,
           max(resp_b)  OVER (PARTITION BY mid) AS resp_tot,
           max(think_b) OVER (PARTITION BY mid) AS think_tot
      FROM s
    WINDOW w AS (PARTITION BY mid ORDER BY t_ms)),
rate AS (
    SELECT mid, model_id, provider, t_ms,
           CASE WHEN dt > 0 AND resp_tot > 0 AND tout IS NOT NULL
                THEN 1000.0 * d_resp * tout / resp_tot / dt
                ELSE 0 END AS resp_tps,
           CASE WHEN dt > 0 AND think_tot > 0 AND tthink IS NOT NULL
                THEN 1000.0 * d_think * tthink / think_tot / dt
                ELSE 0 END AS think_tps
      FROM step),
binned AS (
    SELECT t_ms, model_id, provider, resp_tps, think_tps,
           ntile(10) OVER (PARTITION BY t_ms, model_id, provider
                           ORDER BY resp_tps) AS decile
      FROM rate)
SELECT t_ms, model_id, provider, decile,
       avg(resp_tps) AS resp_tps,
       avg(think_tps) AS think_tps,
       avg(resp_tps + think_tps) AS total_tps,
       count(*) AS n
  FROM binned GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4
"""

COLUMNS = ("t_ms", "model_id", "provider", "decile",
           "resp_tps", "think_tps", "total_tps", "n")

HEADER = "t_ms\tmodel_id\tprovider\tdecile\tresp_tps\tthink_tps\ttotal_tps\tn"

COLORS = ("#1f77b4", "#d62728", "#2ca02c", "#9467bd",
          "#ff7f0e", "#8c564b")


def run(conn, where):
    """Every decile of every grid point the filter kept, as sqlite rows."""
    return conn.execute(RATES.format(where=where or "1")).fetchall()


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


def csv(conn, where):
    """The rates as TSV: one row per (grid point, series, decile).

    Long and not wide, so that `sort`, `awk` and a plotter's `using 1:7`
    are the readers it was written for.
    """
    lines = [HEADER]
    lines.extend("\t".join(fmt(row[c]) for c in COLUMNS)
                 for row in run(conn, where))
    return "\n".join(lines) + "\n"


def shade(decile, n):
    """A decile's stroke opacity: the middle is solid and the tails fade.

    With one or two deciles there are no tails to fade, so every line is
    drawn at full strength -- which is what a single call's plot is.

    >>> shade(1, 1), shade(5, 10) > shade(1, 10) > 0
    (1.0, True)
    """
    if n <= 2:
        return 1.0
    mid = (n + 1) / 2
    return 0.15 + 0.85 * (1 - abs(decile - mid) / ((n - 1) / 2))


def svg(conn, where, width=720, height=360, margin=48):
    """The rates as an SVG: one fan of decile lines per (model, provider).

    Every point in the data is a `<path>`; nothing here rasterizes, shells
    out or imports a plotting library, because the whole answer is text on
    stdout and every renderer reads SVG.
    """
    rows = run(conn, where)
    head = (f"<svg xmlns='http://www.w3.org/2000/svg'"
            f" width='{width}' height='{height}'"
            f" viewBox='0 0 {width} {height}'>"
            f"<rect width='{width}' height='{height}' fill='#ffffff'/>")
    if not rows:
        return head + "</svg>\n"

    series = {}
    for row in rows:
        series.setdefault((row["model_id"], row["provider"]), {}
                          ).setdefault(row["t_ms"], {})[row["decile"]] = row

    ts = sorted({row["t_ms"] for row in rows})
    t0, t1 = ts[0], ts[-1]
    span = max(1, t1 - t0)
    y_max = max(float(row["total_tps"] or 0.0) for row in rows) or 1.0
    left, right = margin, width - margin
    top, bottom = margin, height - margin

    def px(t):
        return left + (t - t0) / span * (right - left)

    def py(v):
        return bottom - max(0.0, float(v or 0.0)) / y_max * (bottom - top)

    parts = [
        f"<line x1='{left}' y1='{bottom}' x2='{right}' y2='{bottom}'"
        f" stroke='#999999'/>",
        f"<line x1='{left}' y1='{top}' x2='{left}' y2='{bottom}'"
        f" stroke='#999999'/>",
        f"<text x='{left}' y='{bottom + 16}' font-size='11'"
        f" fill='#666666'>{t0} ms</text>",
        f"<text x='{right}' y='{bottom + 16}' font-size='11'"
        f" fill='#666666' text-anchor='end'>{t1} ms</text>",
        f"<text x='{left - 6}' y='{top + 10}' font-size='11'"
        f" fill='#666666' text-anchor='end'>{y_max:.0f} tok/s</text>",
        f"<text x='{left - 6}' y='{bottom}' font-size='11'"
        f" fill='#666666' text-anchor='end'>0</text>",
    ]

    for i, (key, at) in enumerate(
            sorted(series.items(), key=lambda kv: (kv[0][0], kv[0][1] or ""))):
        color = COLORS[i % len(COLORS)]
        points = sorted(at)
        deciles = sorted({d for t in points for d in at[t]})
        for decile in deciles:
            line = [(t, at[t][decile]["total_tps"])
                    for t in points if decile in at[t]]
            if len(line) < 2:
                continue
            path = "M " + " L ".join(
                f"{px(t):.1f},{py(v):.1f}" for t, v in line)
            parts.append(f"<path d='{path}' fill='none' stroke='{color}'"
                         f" stroke-opacity='{shade(decile, len(deciles)):.2f}'"
                         f" stroke-width='1.4'/>")
        model_id, provider = key
        label = model_id + (f"  via {provider}" if provider else "")
        parts.append(f"<text x='{right}' y='{top + 14 + 14 * i}'"
                     f" font-size='11' text-anchor='end' fill='{color}'>"
                     f"{label}</text>")

    return head + "".join(parts) + "</svg>\n"


def csv_readout(conn, env, knobs):  # noqa: ARG001
    """commands.BY_FLAG's --tps-csv: the rates, as text on stdout."""
    return csv(conn, knobs.get("filter"))


def svg_readout(conn, env, knobs):  # noqa: ARG001
    """commands.BY_FLAG's --tps-svg: the rates, as SVG on stdout."""
    return svg(conn, knobs.get("filter"))
