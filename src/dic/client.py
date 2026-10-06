"""`dic()` itself: the program, minus the command line.

One call is one completion: resolve a model out of the config cache, rebuild
the conversation from sqlite, stream the answer to `out`, append the row to
the message tree, and return a `Reply` saying what was printed and how long
each phase took.  Nothing here reads `sys.argv`, and `sys.stdout` is only a
default, so this function is both the CLI's whole body and the library's
entry point.

    dic/dic.py          arguments, stdin, os._exit
    dic/client.py       dic(), Reply -- the control flow above
    dic/commands.py     the readouts -- a question, never a call
    dic/options.py      Flag: one declaration per knob, the CLI, the env vars
    dic/config.py       model and provider config: json sources, sqlite cache
    dic/store.py        sqlite message tree, attachments, session pointers
    dic/tty.py          colour, errors, the cost line, the progress meters
    dic/tool.py         --tools: python functions the model may call
    dic/tools/*.py      the tools that ship with dic, one category each
    dic/adaptors/*.py   one wire protocol each
    dic/models.json     packaged defaults, overlaid by the user's files
"""
import http.client, json, os, re, sys, time, urllib.parse

from dic import commands, config, output, price
from dic.options import MODE, OUTPUT, flag, modes, resolve
from dic.store import (INSERT, SESSION_COST, Trace, config_dir, db, history,
                       normalize, resolve_ref, session_read, session_write,
                       store_attachment, turns_from_rows, ulid)
from dic.tty import (BLUE, RESET, THINKING, DicError, Line, osc52, pv_update,
                     report, summary, use_color)

HERE = os.path.dirname(os.path.abspath(__file__))

# How many times one call may ask for tools before dic stops asking: a model
# that loops is a bug and not a conversation, and a call is not an infinite
# budget.
MAX_TOOL_ROUNDS = 16


class Reply:
    """What one dic() call produced: the answer, the row, and the timings.

    `text` is what went to `out`, `raw` is what was stored in `response_raw`,
    and `timings` is the stamps dict in nanoseconds, so a caller can measure a
    phase or assert a ceiling without reading sqlite.  `mid` is the row's
    primary key: a later call passes it as `mid=` to continue the
    conversation, which is how `-c` and `--mid` are exercised in one process.
    `status` is None for a call that never reached the network, which is what
    --models, --aliases and --stats are.
    `usage` is the quantities the API reported, and only from those does the
    caller see what the call cost, because a price is read from the config as
    it is now and recorded on the row rather than returned.
    """
    __slots__ = ("api_type", "error", "mid", "mime", "model_id", "paths",
                 "raw", "status", "text", "timings", "usage")

    def __init__(self, text="", raw=None, mid=None, model_id=None,
                 api_type=None, status=None, error=None, usage=None,
                 paths=(), mime="text/plain", timings=None):
        self.text = text
        self.raw = raw
        self.mid = mid
        self.model_id = model_id
        self.api_type = api_type
        self.status = status
        self.error = error
        self.usage = usage or {}
        self.paths = list(paths)
        self.mime = mime
        self.timings = timings or {}


def load_adaptor(api_type, env):
    """The module implementing api_type: PATH, auth, build, parse, finish.

    Built-ins are dic.adaptors.<api_type with '-' as '_'>.  Anything else
    must be ~/.config/fac/adapters/<api_type>.py exporting the same five
    names and importing dic's own helpers by package name, e.g.
    `from dic.store import data_url`.  Only the selected adaptor is ever
    imported, and each one gets its own module name, so two of them cannot
    collide.
    """
    import importlib
    name = api_type.replace("-", "_")
    if os.path.exists(os.path.join(HERE, "adaptors", f"{name}.py")):
        return importlib.import_module(f"dic.adaptors.{name}")
    path = os.path.join(config_dir(env), "adapters", f"{api_type}.py")
    if not os.path.exists(path):
        raise DicError(f"unknown api_type: {api_type}")
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"dic_adaptor_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def events(api_base, path, headers, body, stamps):
    """POST body, stamp each phase into stamps, and yield events as they arrive.

    Plain http.client: importing a vendor SDK would cost more than the whole
    time-to-first-token this streams to save.  A failure sets stamps["error"]
    and yields nothing, so the caller can record the attempt before dying.
    """
    url = urllib.parse.urlparse(api_base)
    connection = (http.client.HTTPConnection if url.scheme == "http"
                  else http.client.HTTPSConnection)
    conn = connection(url.netloc)
    conn.connect()
    stamps["t_connect"] = time.time_ns()
    conn.request("POST", url.path.rstrip("/") + path, json.dumps(body), headers)
    stamps["t_request"] = time.time_ns()
    reply = conn.getresponse()
    stamps["t_headers"], stamps["status"] = time.time_ns(), reply.status
    if reply.status != 200:
        detail = reply.read().decode("utf-8", "replace").strip()
        stamps["error"] = f"{reply.reason}: {detail}"
        return
    for line in reply:
        if line.startswith(b"data:"):
            data = line[5:].strip()
            if data and data != b"[DONE]":
                yield json.loads(data)


def code_block(text):
    """The body of the first fenced code block in text, or text unchanged.

    >>> code_block("prose\\n```python\\nx = 1\\n```\\nmore")
    'x = 1\\n'
    >>> code_block("no fence here")
    'no fence here'
    """
    match = re.search(r"```[^\n]*\n(.*?)```", text, re.S)
    return match.group(1) if match else text


def options(model, overrides):
    """The model's default options with KEY=VALUE overrides applied.

    Values are decoded as JSON when they parse, so numbers, booleans, lists
    and objects all reach the API with their proper types; anything else is
    passed through as a plain string.

    >>> options({"options": {"max_tokens": 10}}, ["max_tokens=20", "stop=x"])
    {'max_tokens': 20, 'stop': 'x'}
    >>> options({}, ['tools=[]'])
    {'tools': []}
    """
    opts = dict(model.get("options") or {})
    for override in overrides:
        if "=" not in override:
            raise DicError(f"bad option (expected key=value): {override}")
        key, raw = override.split("=", 1)
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
        opts[key.strip()] = value
    return opts


# The usage name a cache write is rated under.  A model advertises exactly
# the TTLs it has a rule for here, so the set of TTLs a user may name is the
# set of cache writes dic can pay for and nothing else.
CACHE_WRITE = "in.cache_write."


def ttl_seconds(ttl):
    """A cache TTL as seconds, so that the shortest sorts first.

    >>> ttl_seconds("5m"), ttl_seconds("1h")
    (300, 3600)
    """
    return int(ttl[:-1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[ttl[-1]]


def cache_ttls(model):
    """The cache TTLs a model's price table can rate, shortest first.

    A model advertises the caching it can pay for and nothing else, so these
    are the values --cache may take beyond `off`; a provider whose cache
    price nobody wrote down advertises none and is never sent a breakpoint.

    >>> cache_ttls({"price": {"in.cache_write.5m": {"rate": 12.5},
    ...                       "in.cache_write.1h": {"rate": 20.0}}})
    ['5m', '1h']
    >>> cache_ttls({"price": {"in": {"rate": 3.0}}})
    []
    """
    return sorted((name[len(CACHE_WRITE):] for name in (model.get("price") or {})
                   if name.startswith(CACHE_WRITE)), key=ttl_seconds)


def cache_ttl(model, asked):
    """The TTL this call caches under, or None: off unless asked and priced.

    Off is the default, and off is all a model that prices no cache write
    gets, so a model that cannot be billed for a cache write is never asked
    for one.  A TTL that was asked for and cannot be rated is an error naming
    what the model does offer, because a silent miss is a cache the user
    believes is warm.

    >>> cache_ttl({"price": {}}, None) is None
    True
    >>> cache_ttl({"model_id": "m", "price": {"in.cache_write.1h": {}}}, "1h")
    '1h'
    >>> cache_ttl({"model_id": "m", "price": {"in.cache_write.5m": {}}}, "1h")
    Traceback (most recent call last):
    ...
    dic.tty.DicError: m: --cache=1h is not available (try: off, 5m)
    >>> cache_ttl({"model_id": "m", "price": {}}, "5m")
    Traceback (most recent call last):
    ...
    dic.tty.DicError: m: cannot cache: no cache price is configured, so a cache write cannot be rated
    """
    if asked in (None, "off"):
        return None
    ttls = cache_ttls(model)
    if not ttls:
        raise DicError(f"{model['model_id']}: cannot cache: no cache price"
                       " is configured, so a cache write cannot be rated")
    if asked not in ttls:
        raise DicError(f"{model['model_id']}: --cache={asked} is not available"
                       f" (try: off, {', '.join(ttls)})")
    return asked


def dic(prompt,
        # ----- the call -----
        model:        flag(short="-m", help="which model") = None,
        system:       flag(short="-s", help="system prompt") = None,
        attachment:   flag(short="-a", action="append", metavar="FILE",
                           help="attach a file") = None,
        option:       flag(short="-o", action="append", metavar="KEY=VALUE",
                           help="override a model option") = None,
        cache:        flag(long="--cache", metavar="TTL",
                           help="cache the prompt: off (the default), or a"
                                " TTL the model prices") = None,
        tools:        flag(long="--tools", action="append",
                           metavar="MODULE:FUNC",
                           help="offer a python function as a tool; repeatable") = None,
        extract:      flag(short="-x", action="bool",
                           help="print only the first fenced code block") = False,
        cont:         flag(short="-c", long="--continue", action="bool",
                           help="continue this session's last conversation") = False,
        mid:          flag(long="--mid", metavar="REF",
                           help="continue from a message: a mid, its prefix,"
                                " or a ref like HEAD~3") = None,
        pv_thinking:  flag(action="yes/no",
                           help="meter the reasoning instead of printing it") = None,
        pv_response:  flag(action="yes/no",
                           help="meter the answer on stderr as well") = None,
        trace:        flag(action="bool",
                           help="record per-round byte-rate samples, for"
                                " plotting B/s over time") = False,
        clipboard:    flag(action="yes/no",
                           help="copy the answer to the terminal's clipboard"
                                " (the default on a terminal)") = None,
        verbosity:    flag(short="-v", action="count", env=False,
                           help="raise stderr verbosity; repeatable"
                                " (DIC_VERBOSITY sets the base)") = None,
        quiet:        flag(short="-q", action="bool",
                           help="print nothing to stderr but errors") = False,
        models_file:  flag(help="a json file of model entries to overlay") = None,
        # ----- where the answer goes -----
        path:         flag(long="--path", metavar="FILE", group=OUTPUT,
                           help="write the answer to FILE, atomically") = None,
        force:        flag(short="-f", action="bool", group=OUTPUT,
                           help="overwrite the file named by --path") = False,
        mime_type:    flag(long="--mime-type", metavar="TYPE", group=OUTPUT,
                           help="the mime type of the answer") = None,
        # ----- readouts; at most one may be given -----
        aliases:      flag(action="bool", group=MODE, mode=True,
                           help="print shell alias definitions and exit") = False,
        models:       flag(action="bool", group=MODE, mode=True,
                           help="list every configured model id, the key"
                                " each needs and whether it is set") = False,
        stats:        flag(action="bool", group=MODE, mode=True,
                           help="print per-model statistics and exit") = False,
        providers:    flag(long="--providers", metavar="MODEL", action="?",
                           env=False, group=MODE, mode=True,
                           help="print the upstreams a router chose, per model"
                                " or for MODEL, and exit") = None,
        cost_session: flag(long="--cost-session", metavar="NAME", action="?",
                           env=False, group=MODE, mode=True,
                           help="print a session's spend, its sub-sessions"
                                " included (default: $DIC_SESSION)") = None,
        cost_of:      flag(long="--cost-of", metavar="REF", env=False,
                           group=MODE, mode=True,
                           help="print the spend of the conversation"
                                " ending at REF") = None,
        cost_tree:    flag(long="--cost-tree", metavar="NAME", action="?",
                           env=False, group=MODE, mode=True,
                           help="print a per-session cost breakdown"
                                " (default: $DIC_SESSION)") = None,
        log:          flag(action="bool", env=False, group=MODE, mode=True,
                           help="list recent messages, newest first, and exit")
                      = False,
        graph:        flag(long="--graph", action="bool", env=False, group=MODE,
                           help="draw --log's forest in a column of its own")
                      = False,
        log_session:  flag(long="--session", metavar="NAME", action="?",
                           env=False, group=MODE,
                           help="--log a session and its sub-sessions"
                                " (default: $DIC_SESSION)") = None,
        show:         flag(metavar="REF", env=False, group=MODE, mode=True,
                           help="print one message's details and exit") = None,
        limit:        flag(metavar="N", type=int, env=False, group=MODE,
                           help="how many rows --log prints (default 200)") = None,
        all_:         flag(long="--all", action="bool", env=False, group=MODE,
                           help="--log every message, not just this session") = False,
        from_:        flag(long="--from", metavar="REF", env=False, group=MODE,
                           help="--log from REF instead of the session pointer")
                      = None,
        t_start=None, env=None, out=None, err=None) -> Reply:
    """Talk to one model once, and record the turn.

    Every knob carries a Flag, so `argument > $DIC_<NAME> > model config` is
    resolved here, once, by options.resolve: the command line and the library
    cannot disagree about precedence.  A continuing conversation then supplies
    the model it last used, so only an explicit -m can switch providers inside
    a thread, exactly as only -s can rewrite its system prompt.

    A readout -- one of the flags options.modes() names -- asks the database
    one question and returns, and no other flag reaches this call: a model is
    configured, an adaptor loaded, a request sent, and a row written, or one
    of the functions in commands.py answers and nothing else happens.  A ^C
    is the caller's, and a failed call is recorded before it is raised, so a
    library caller does not have to choose between seeing the error and
    counting it.
    `t_start` is the process's first
    instant when the CLI calls in and defaults to now, and `env`, `out` and
    `err` default to the process's, so a library call supplies none of them.

    What the call's usage cost is rated here, once, when the reply has ended:
    a price table is a config file, and the next invocation may have edited it,
    so the itemization is stored with the row instead of being recomputed by
    whoever reads it.  A call is never repriced.

    Returns a Reply, writing the answer to `out` and colour, the cost line
    and the meters to `err`.  A failure raises DicError, after the attempt
    has been recorded, so a failed call is countable too, and a ^C comes
    back as KeyboardInterrupt: neither exits the process, because a
    library's caller is not dic's to kill.
    """
    t_start = time.time_ns() if t_start is None else t_start
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    asked = model                   # -m, before $DIC_MODEL fills it in
    knobs = resolve(locals(), env)

    conn = db(env)
    session = env.get("DIC_SESSION", "global")
    config.sync(conn, env, knobs["models_file"])   # a stat per file; a parse only when one moved
    # a readout asks the database one question and prints the answer, and it
    # never reaches the network: options.modes() already refused a second
    # readout, so this is the whole of the dispatch, and commands.py is the
    # whole of what any one of them does.
    given = modes(knobs)
    if given:
        text = commands.BY_FLAG[given[0]](conn, env, knobs)
        out.write(text)
        out.flush()
        return Reply(text=text)

    # stderr is graded: $DIC_VERBOSITY sets the grade, otherwise 1 on a
    # terminal and 0 on a pipe; -q drops it to 0 and each -v raises it by one
    verbosity = int(env.get("DIC_VERBOSITY") or (1 if err.isatty() else 0))
    if knobs["quiet"]:
        verbosity = 0
    elif knobs["verbosity"]:
        verbosity += knobs["verbosity"]

    if not prompt.strip() and not knobs["attachment"]:
        raise DicError("no prompt")

    # A ceiling on the session's spend, checked here because this is the one
    # place that sees the row before the request is sent; the ceiling is the
    # same subtree --cost-session prints, sub-sessions included.
    budget = env.get("DIC_COST_BUDGET")
    if budget:
        try:
            limit = float(budget)
        except ValueError:
            raise DicError(f"DIC_COST_BUDGET={budget} is not a number") from None
        spent = conn.execute(SESSION_COST, (session, session)).fetchone()[0]
        if spent >= limit:
            raise DicError(f"{session}: ${spent:.4f} spent,"
                           f" DIC_COST_BUDGET=${limit:.2f}")

    prev_mid = (resolve_ref(conn, knobs["mid"], env) if knobs["mid"]
                else (session_read(env) if knobs["cont"] else None))
    if knobs["cont"] and not prev_mid:
        # -c with no pointer is an error: never a new conversation, and never
        # somebody else's
        raise DicError("no conversation in this session"
                       f" (DIC_SESSION={env.get('DIC_SESSION', 'global')})")
    rows = history(conn, prev_mid) if prev_mid else []
    if prev_mid and not rows:
        raise DicError(f"no such mid: {prev_mid}")

    # -m > the model this conversation last used > $DIC_MODEL > the default, so
    # -c carries on with the same provider unless the caller names another one
    knobs["model"] = (asked or (rows[-1]["model_id"] if rows else None)
                      or knobs["model"])
    model = config.resolve(conn, knobs["model"] or config.default_id(conn, env))
    api_type = model.get("api_type", "openai-chat")
    adaptor = load_adaptor(api_type, env)
    key_name = model.get("api_key_name")
    if not key_name:
        raise DicError(f"{model['model_id']}: no api_key_name configured")
    api_key = env.get(key_name)
    if not api_key:
        raise DicError(f"{key_name} is not set")

    # --tools names python, so dic.tool -- and the inspect and typing it reads
    # the annotations with -- is imported only when one was given, exactly as
    # only the selected adaptor is.  A spec that does not import is an error
    # here, before the network and before a .part is opened.
    specs = knobs["tools"]
    if isinstance(specs, str):      # DIC_TOOLS="pkg.mod:fn" is one spec, not chars
        specs = [specs]
    tools = ()
    if specs:
        from dic import tool
        tools = tool.resolve(specs)

    # what the answer will be, settled before the call so that a missing or
    # taken --path fails before the network instead of after a whole video
    mime = knobs["mime_type"] or model.get("output") or "text/plain"
    if mime.startswith("text/"):
        output.check_free(knobs["path"], knobs["force"])
    else:
        if not knobs["path"]:
            raise DicError(f"{mime}: a non-text answer needs --path FILE")
        output.check_free(output.numbered(knobs["path"], 0), knobs["force"])
    sink = output.Sink(path=knobs["path"], force=knobs["force"],
                       out=out, err=err, env=env, verbosity=verbosity)

    turns, system = [], knobs["system"]
    if rows:
        turns = turns_from_rows(conn, rows, api_type)
        if system is None:
            system = rows[-1]["system"]
    elif system is None:
        # fresh conversations only: continuing one must never let an exported
        # default rewrite the system prompt the thread was started with
        system = env.get("DIC_SYSTEM") or model.get("system")

    attached = [store_attachment(conn, path)
                for path in knobs["attachment"] or ()]
    turns.append({"role": "user",
                  "blocks": [dict(att) for att in attached]
                            + [{"type": "text", "text": prompt}]})
    turns = normalize(turns)

    opts = options(model, knobs["option"] or ())
    # a cache breakpoint is a request dic builds and not an option it
    # forwards, so the TTL is settled here: off unless the caller asked for
    # one the model's own price table can rate
    cache = cache_ttl(model, knobs["cache"])
    if cache:
        model["cache"] = cache
    if tools:
        # the model is offered the tools it may call, in this wire protocol's
        # own shape; a protocol with no tools at all says so here and not at
        # the API, which would reject the request as malformed
        if not (getattr(adaptor, "tool_schema", None)
                and getattr(adaptor, "calls", None)):
            raise DicError(f"{api_type}: this protocol does not support tools")
        opts["tools"] = adaptor.tool_schema(tools)
    line = Line(err, env)
    prepare = getattr(adaptor, "prepare", None)
    if prepare is not None:            # fal reads URLs, so files go up first
        # a prepare that uploads is time the user waits before anything
        # is sent, so it gets the same one line the ttft clock does; a
        # fast one never paints at all
        line.wait()
        turns = prepare(model, api_key, turns, opts)
        line.first_token()
    body = adaptor.build(model, turns, system, opts)
    headers = {"content-type": "application/json", "accept": "text/event-stream"}
    headers.update(adaptor.auth(api_key))
    headers.update(model.get("headers") or {})

    report(verbosity, 3,
           f"POST {model['api_base']}{adaptor.PATH} {json.dumps(body, default=str)}",
           err=err, env=env)
    stamps = {"t_start": t_start, "status": None, "error": None}
    painted = None
    # reasoning is progress, not content, so it is metered by default; the
    # answer is metered only where stderr is the only thing on the screen
    pv_thinking = True if knobs["pv_thinking"] is None else knobs["pv_thinking"]
    pv_response = (not out.isatty() if knobs["pv_response"] is None
                   else knobs["pv_response"])
    pv = pv_resp = None

    def close_meters():
        """Stop the wait clock and close each meter, so nothing repaints after."""
        # an error, a cancel, or a reply with no text; also ends a poll status
        line.first_token()
        if pv is not None:      # a reply that was nothing but reasoning
            pv_update(pv, final=True, err=err, env=env)
        if pv_resp is not None:
            pv_update(pv_resp, final=True, err=err, env=env)

    paint = use_color(out, env) and knobs["path"] is None

    def keep(wire):
        """Fold one round's timings into the call's, each phase counted once.

        The first round is the one whose phases are dic's own -- the connect,
        the request and the ttft that preceded the model's first token are
        what this program exists to keep small -- and a later round is a
        request into a conversation that has already begun.  What is summed is
        the time spent streaming, because a rate over a call that stopped to
        run a tool is not a rate.
        """
        for phase in ("t_connect", "t_request", "t_headers", "t_first"):
            if stamps.get(phase) is None and wire.get(phase) is not None:
                stamps[phase] = wire[phase]
        if not wire.get("kept"):
            wire["kept"] = True
            if wire.get("t_first") and wire.get("t_last"):
                stamps["t_stream"] = (stamps.get("t_stream", 0)
                                      + wire["t_last"] - wire["t_first"])
        stamps["t_last"] = wire.get("t_last") or stamps.get("t_last")
        stamps["t_done"] = wire.get("t_done") or stamps.get("t_done")
        stamps["status"] = wire.get("status")
        stamps["error"] = wire.get("error")

    def billing(acc):
        """The usage of one round, the dollars it cost, and where they came from.

        One place, reached from the reply, the tool loop and the cancelled
        path, so a ^C's cost line and the reply's are the same numbers.  The
        itemization comes back with the total because it is what gets stored:
        the rule that priced each count is not recoverable from the config,
        which the next invocation may already have edited.  A provider that
        priced the call itself wins over the table, so the fourth value names
        which of the two produced the bill.
        """
        usage = acc.get("usage") or {}
        # the tier is what the response says it charged, never what the request
        # asked for: a provider that ignores -o service_tier must not be priced
        # as though it had obeyed it
        facts = price.facts(usage, acc.get("tier") or "default")
        charged = acc.get("cost")
        if charged is not None:
            # the provider priced this call itself, so its number is the bill
            # and the table's is a second opinion kept beside it; the hash is
            # the name of that provenance and not of a table
            total, items = price.reported(charged,
                                          price.rate(model, usage, facts)[0])
            return usage, total, items, price.REPORTED
        total, items = price.rate(model, usage, facts)
        return usage, total, items, price.price_hash(model)

    ids = [att["aid"] for att in attached]

    def insert(index, prev, response, raw, outputs, results, usage, cost, items,
               status, error, wire, source):
        """Append one round -- one API call -- to the tree, and return its mid.

        A tool loop is several paid requests for one answer, so each is a row
        of its own: the rounds are countable, a failure is attributable to the
        round that failed, and the conversation they form replays out of the
        tree alone.  `prev` is the row this round continues from, which for the
        second round is the first round's own mid.  Only the first round
        carries the prompt and the attachments: a later one continues that
        turn rather than making one.

        The upstream a router chose is read from `acc`, where the adaptor's
        parse() put it: it is a fact about this round's response and not about
        the call, and a round dic is writing because it failed may have none.
        """
        mid = ulid()
        conn.execute(INSERT, (
            mid, index, prompt if index == 0 else "", system, response,
            json.dumps(raw), prev, json.dumps(ids if index == 0 else []),
            json.dumps(outputs), json.dumps(results) if results else None,
            model["model_id"], api_type, session, acc.get("provider"),
            status, error,
            json.dumps(usage) if usage else None,
            cost, json.dumps(items) if items is not None else None,
            source if cost is not None else None,
            wire.get("t_start"), wire.get("t_connect"), wire.get("t_request"),
            wire.get("t_headers"), wire.get("t_first"), wire.get("t_last"),
            wire.get("t_done")))
        if trace is not None:
            # one row per round that produced bytes, and none for one that did
            # not: the plot's x axis is time within a call and not within a row
            samples = trace.json()
            if samples:
                conn.execute("INSERT INTO trace VALUES (?, ?)", (mid, samples))
        return mid

    paint = use_color(out, env) and knobs["path"] is None
    # the answer is blue on a terminal, so it is also one escape sequence away
    # from that terminal's clipboard: OSC 52, which the emulator performs
    # itself and which needs no subprocess and no clipboard protocol.  The rule
    # is the paint rule -- a pipe, a --path and a NO_COLOR each give nothing --
    # and --clipboard/--no-clipboard force it either way.
    clipboard = paint if knobs["clipboard"] is None else knobs["clipboard"]

    acc, chunks, wire, asked, results = {}, [], {}, (), []
    trace = None
    printed, rounds, prev, index, seen = [], [], prev_mid, 0, 0

    try:
        call = getattr(adaptor, "call", None)
        while True:                 # a round may end by asking for a tool
            # a cache write that arrives as one flat total is billed at the
            # TTL this call asked for, so the adaptor is told which it was
            acc = {"cache": model["cache"]} if model.get("cache") else {}
            chunks, asked, results = [], (), []
            # a later round begins where the round before it stopped, so its
            # overhead is the tools that ran in between and not dic's startup
            wire = {"t_start": stamps.get("t_last") or t_start}
            # a new round is a new request with its own first token, so its
            # byte-rate samples start over at this round's own t_start
            trace = Trace(wire["t_start"]) if knobs["trace"] else None
            line.restart()          # this request's clock, not the call's
            pv = {"name": "thinking"} if pv_thinking else None
            pv_resp = {"name": "response"} if pv_response else None
            if pv is not None or pv_resp is not None:
                line.wait()         # a bare ttft clock until something arrives
            stream = (call(model, api_key, body, line, wire)
                      if call is not None else
                      events(model["api_base"], adaptor.PATH, headers, body, wire))
            for event in stream:
                chunk, kind = adaptor.parse(event, acc)
                if acc.get("provider"):
                    # a router named the upstream mid-wait, so it rides the
                    # clock it explains
                    line.extra = f"provider: {acc['provider']}"
                if not chunk:
                    continue
                if kind != "blob":
                    sink.end_blob()     # a blob ends when anything else arrives
                wire.setdefault("t_first", time.time_ns())
                line.first_token()      # the wait is over, whatever arrived
                if trace is not None:
                    trace.add(time.time_ns(), kind or "response",
                              len(chunk if isinstance(chunk, bytes)
                                  else chunk.encode()))
                if kind == "thinking":
                    if pv is not None:
                        pv_update(pv, chunk, err=err, env=env)
                    elif use_color(err, env):
                        err.write(THINKING + chunk + RESET)
                    else:
                        err.write(chunk)
                    if pv is None:
                        err.flush()
                    continue
                if kind == "blob":      # bytes: --path was checked above
                    sink.write_blob(chunk)
                    if pv_resp is not None:
                        pv_update(pv_resp, chunk, err=err, env=env)
                    continue
                if pv is not None:      # the answer starts on a line of its own
                    pv_update(pv, final=True, err=err, env=env)
                chunks.append(chunk)
                printed.append(chunk)
                if pv_resp is not None:
                    pv_update(pv_resp, chunk, err=err, env=env)
                if not knobs["extract"]:
                    if paint and painted != kind:
                        sink.write((RESET if painted is not None else "") + BLUE)
                        painted = kind
                    sink.write(chunk)
            wire["t_last"] = time.time_ns()
            close_meters()
            sink.end_blob()
            if acc.get("error"):
                # a stream that failed after a 200: -1 keeps the row out of
                # the averages, inside the error count, and off the session
                # pointer, exactly like a call that never reached the network
                wire["status"], wire["error"] = -1, acc["error"]
            elif wire.get("status") is None:
                # a call() adaptor replaces the transport and never sees a
                # wire status; a stream that ran to its end is a 200
                wire["status"] = 200
            keep(wire)
            asked = adaptor.calls(acc) if tools else ()
            if asked and index + 1 >= MAX_TOOL_ROUNDS:
                raise DicError(f"the model has asked for tools"
                               f" {MAX_TOOL_ROUNDS} rounds running; giving up")
            results = []
            for want in asked:
                report(verbosity, 1,
                       f"tool: {want['name']}"
                       f" {json.dumps(want['arguments'], default=str)}",
                       err=err, env=env)
                record = next((t for t in tools if t["name"] == want["name"]),
                              None)
                started = time.time_ns()
                if record is None:
                    ok, content = False, f"no such tool: {want['name']}"
                else:
                    try:
                        content = tool.call(record, want["arguments"])
                        ok = True
                    except Exception as e:
                        # a tool that fails is the model's to fix, so its
                        # message is the result and not the end of the call
                        ok, content = False, f"{type(e).__name__}: {e}"
                results.append({"id": want["id"], "name": want["name"],
                                "ok": ok, "error": None if ok else content,
                                "content": content, "t_start": started,
                                "t_end": time.time_ns()})
            usage, cost, items, source = billing(acc)
            rounds.append((usage, cost, items))
            raw = adaptor.finish(acc)
            if not asked:
                break
            # this round asked for tools, so it is not the last one: it is
            # written now, and the next round continues from it
            prev = insert(index, prev, "".join(chunks), raw,
                          [{"path": p, "mime_type": mime}
                           for p in sink.paths[seen:]],
                          results, usage, cost, items,
                          wire["status"], wire.get("error"), wire, source)
            seen = len(sink.paths)
            conn.commit()
            turns.append({"role": "assistant", "blocks": [], "raw": raw})
            turns.append({"role": "user", "blocks": [
                {"type": "tool_result", "id": r["id"], "content": r["content"]}
                for r in results]})
            body = adaptor.build(model, turns, system, opts)
            index += 1
    except KeyboardInterrupt:
        # A ^C is the caller's, not dic's: close what this call opened and let
        # it propagate, because only the entry point knows that for the CLI a
        # cancelled call is a nonzero exit.  The bytes already received are not
        # thrown away: whatever was written stays in a .part, that name is on
        # the row as this turn's output, and the error says what is on disk.
        wire["t_last"] = wire["t_done"] = time.time_ns()
        close_meters()
        if not knobs["extract"] and knobs["path"] is None:
            if painted is not None:         # do not leave stdout painted blue
                out.write(RESET)
            if chunks and not chunks[-1].endswith("\n"):
                out.write("\n")
            out.flush()
        pending = sink.pending()
        sink.abandon()
        usage, cost, items, source = billing(acc)
        rounds.append((usage, cost, items))
        keep(wire)
        # a call that was cut short has no bill to store: cost is NULL and not
        # zero, because --stats sums that column and a cancelled call is not a
        # free one.  What the provider did report is still in `usage`, and is
        # what the line below reads.
        insert(index, prev, "".join(chunks), adaptor.finish(acc),
               [{"path": pending, "mime_type": mime}] if pending else [],
               results, usage, None, None, -1, "cancelled", wire, source)
        conn.commit()
        usage, items = price.merged(rounds)
        report(verbosity, 1,
               summary(usage, items, None, stamps, verbosity, partial=True),
               err=err, env=env)
        if pending:
            report(verbosity, 1, f"partial output left at {pending}",
                   err=err, env=env)
        raise
    except DicError as e:
        # a call() adaptor reports a failure by raising, so the transport that
        # would have written the row never ran and the line it leaves may be
        # mid-repaint: close the line, and record the attempt with the same -1
        # a stream that failed after a 200 gets, so a job that never answered
        # is still countable and its row is never continued by -c
        wire["t_last"] = wire["t_done"] = time.time_ns()
        close_meters()
        pending = sink.pending()
        sink.abandon()
        usage, cost, items, source = billing(acc)
        rounds.append((usage, cost, items))
        keep(wire)
        insert(index, prev, "".join(chunks), adaptor.finish(acc),
               [{"path": pending, "mime_type": mime}] if pending else [],
               results, usage, None, None, stamps.get("status") or -1, str(e),
               wire, source)
        conn.commit()
        raise
    wire["t_done"] = stamps["t_done"] = time.time_ns()
    keep(wire)
    response = "".join(printed)
    text = response
    if knobs["extract"]:
        text = code_block(response)
        sink.write(BLUE + text + RESET if paint else text)
    else:
        if painted is not None:
            sink.write(RESET)
        if response and not response.endswith("\n"):
            sink.write("\n")
            text += "\n"
    paths = sink.close()
    outputs = [{"path": p, "mime_type": mime} for p in paths[seen:]]
    if clipboard:
        osc52(text, out)

    # a stream that failed after its 200 has no bill either: a row is written
    # with one only when the call actually ended
    billed = wire["status"] == 200
    mid = insert(index, prev, "".join(chunks), raw, outputs, results,
                 usage, cost if billed else None, items if billed else None,
                 wire["status"], wire.get("error"), wire, source)
    conn.commit()
    conn.close()
    # a failed attempt is recorded for the error rate but the session pointer
    # is left alone, so the row is always a leaf and never replayed
    if wire["status"] != 200:
        raise DicError(f"{wire['status']} {wire.get('error')}")
    session_write(mid, env)

    out.flush()

    if acc.get("stop") in ("length", "max_tokens", "max_output_tokens"):
        # a 200 that stopped because it ran out of room: the answer above is
        # cut off, and nothing else about the call says so
        report(verbosity, 1, f"truncated: {acc['stop']}", err=err, env=env)
    # a tool loop is several calls for one answer, so the line a user reads
    # is the sum of every round it took, not the last one's
    usage, items = price.merged(rounds)
    report(verbosity, 1,
           summary(usage, items, mid, stamps, verbosity),
           err=err, env=env)
    return Reply(text=text, raw=raw, mid=mid,
                 model_id=model["model_id"], api_type=api_type,
                 status=wire["status"], error=wire.get("error"),
                 usage=usage, paths=paths, mime=mime,
                 timings=stamps)


# The pool generate_async shares, built once by _pool() on first use.  A
# module global rather than a loop resource: it outlives any one event
# loop, and a batch's threads are not a loop's to shut down.
_POOL = {}


def _pool():
    """dic's own worker threads, made once and shared by every async call.

    asyncio's default executor is a process-wide resource shared with every
    run_in_executor(None, ...) in the interpreter, so a caller that filled
    it would otherwise fill dic's.  One pool of dic's own is also one place
    to cap concurrency: ThreadPoolExecutor's default cap (32 on most
    machines) applies here, and a call past it waits its turn rather than
    failing.
    """
    if "executor" not in _POOL:
        import concurrent.futures
        _POOL["executor"] = concurrent.futures.ThreadPoolExecutor(
            thread_name_prefix="dic")
    return _POOL["executor"]


async def generate_async(prompt, **knobs):
    """Talk to one model once without blocking the caller's event loop.

    `generate_async(prompt, **knobs)` is `dic(prompt, **knobs)` run on a
    worker thread of dic's own pool and awaited, so N concurrent calls are N
    in-flight HTTP requests.  The gain is concurrency and not speed: one
    call costs one thread and is no faster for it, which is why the CLI,
    which makes exactly one call, never comes here.

    Everything the sync call returns and raises is returned and raised
    unchanged, so a caller needs no second `except`: a DicError raised in
    the worker is the DicError that arrives, and a KeyboardInterrupt is the
    caller's too, never swallowed by the machinery in between.

    Cancelling the awaiting Task stops the caller and not the call.  A
    thread cannot be interrupted at an await, so the worker runs to the end
    of the reply and the row lands as it always did; the bytes it already
    printed are not taken back, exactly as a ^C in the CLI leaves them.  A
    half-recorded row would be worse than a thread that outlives its caller
    by one call.

    `asyncio` and `concurrent.futures` are imported here and not at module
    level, because the sync path must not pay for a feature it does not use.

    Import it as `from dic.client import generate_async`: dic/__init__ stays
    empty, so importing the package costs nothing.

    >>> print(asyncio.run(generate_async("hi")).text)   # doctest: +SKIP
    hi
    """
    import asyncio
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_pool(), lambda: dic(prompt, **knobs))
