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
import json, os, re, sys, time, urllib.parse

from dic import config, net, output, price
from dic import schema as schema_mod
from dic.options import GLOBAL, OUTPUT, DEBUG, flag, resolve
from dic.store import (INSERT, SESSION_COST, Recorder, config_dir, db,
                       file_block, history, normalize, resolve_ref,
                       session_head, session_move, store_attachment,
                       turns_from_rows, ulid)
from dic.tty import (INFO, PAYLOAD, RESET, DicError, Line, osc52, pv_update,
                     report, summary, use_color)

HERE = os.path.dirname(os.path.abspath(__file__))

# The protocols whose structured output dic can check independently: every
# text protocol asks the model in a way the schema itself describes, so
# repair and validation are both on by default.  typesafe shapes its
# answers itself, so it validates locally and does not repair.
REPAIRS_AUTO = frozenset({"openai-chat", "openai-responses",
                          "anthropic-messages"})
VALIDATES_AUTO = frozenset({"openai-chat", "openai-responses",
                            "anthropic-messages", "typesafe"})

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
    must be ~/.config/geni/adapters/<api_type>.py exporting the same five
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


def events(api_base, path, headers, body, stamps, line=None):
    """POST body, stamp each phase into stamps, and yield events as they arrive.

    Plain http.client: importing a vendor SDK would cost more than the whole
    time-to-first-token this streams to save.  A failure sets stamps["error"]
    and yields nothing, so the caller can record the attempt before dying.
    """
    url = urllib.parse.urlparse(api_base)
    conn = net.connection(api_base, line, "ttft")
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


def repairs_auto(api_type, dialect):
    """Whether this protocol and dialect default repair on.

    A dialect dic cannot compile into a protocol's own shape never gets to
    repair: the schema would be prose the model never agreed to.

    >>> repairs_auto("openai-chat", None), repairs_auto("typesafe", "systemone")
    (True, False)
    >>> repairs_auto("fal", None)
    False
    """
    if dialect == schema_mod.SYSTEMONE:
        return False
    return api_type in REPAIRS_AUTO


def validates_auto(api_type):
    """Whether this protocol and dialect default validation on.

    Every text protocol validates against jsonschema -- they were all
    asked with the same schema -- and systemone validates locally, with
    no import, because the provider already shaped the answers.

    >>> validates_auto("openai-chat"), validates_auto("typesafe")
    (True, True)
    >>> validates_auto("fal")
    False
    """
    return api_type in VALIDATES_AUTO


def resolve_toggle(value, default, what, model):
    """A --repair or --validate value as a bool, with auto resolved.

    auto is the default and resolves per protocol; an explicit on where
    the protocol cannot is an error, because a silently skipped repair or
    validation is a call the caller believes is checked and is not.

    >>> resolve_toggle(None, True, "repair", {"model_id": "m"})
    True
    >>> resolve_toggle("off", True, "repair", {"model_id": "m"})
    False
    >>> resolve_toggle("auto", False, "validate", {"model_id": "m"})
    False
    >>> resolve_toggle("on", False, "repair",
    ...                {"model_id": "m", "api_type": "fal"})
    Traceback (most recent call last):
    ...
    dic.tty.DicError: m: --repair=on, and fal cannot repair
    """
    if value in (None, "auto"):
        return default
    if value not in ("on", "off"):
        raise DicError(f"--{what} must be on, off or auto, not {value!r}")
    if value == "on" and not default:
        raise DicError(f"{model['model_id']}: --{what}=on, and"
                       f" {model.get('api_type') or 'openai-chat'}"
                       f" cannot {what}")
    return value == "on"


def answer_from_calls(calls, output_tool):
    """The output tool's arguments as the answer, or None.

    A protocol with no response_format carries a --schema as a forced
    tool, so the answer is that call's arguments, which the wire already
    delivers as an object.  None when there is no such tool to look for,
    or the model did not call it this round.

    >>> answer_from_calls([{"name": "output", "arguments": {"a": 1}}],
    ...                   "output")
    {'a': 1}
    >>> answer_from_calls([{"name": "ls", "arguments": {}}], "output") is None
    True
    >>> answer_from_calls([], None) is None
    True
    """
    if output_tool is None:
        return None
    for call in calls:
        if call["name"] == output_tool:
            return call["arguments"]
    return None


def repair_json(text):
    """(text, what): the JSON in the answer, fenced or broken, or text as-is.

    The fallback chain is code_block then json.loads, and only when both
    have failed is json_repair imported: the import is off the hot path in
    the strong sense, because cost and failure correlate.  The third step
    is the only repair dependency.

    >>> repair_json('{"a": 1}')
    ('{"a": 1}', None)
    >>> repair_json('```json\\n{"a": 1}\\n```')
    ('{"a": 1}\\n', 'code_block')
    >>> repair_json('no json here')[0]
    'no json here'
    """
    fenced = code_block(text)
    try:
        json.loads(fenced)
    except ValueError:
        pass
    else:
        return fenced, ("code_block" if fenced != text else None)
    try:
        import json_repair
    except ImportError:
        return text, None
    try:
        fixed = json_repair.repair_json(text)
        json.loads(fixed)
    except Exception:
        return text, None
    return fixed, "json_repair"


def validate_json(schema, dialect, text):
    """() -> [errors]: validation of one answer against one schema.

    Two dialects, two checks.  jsonschema is imported whenever the caller
    asked for a jsonschema or simonw_llm dialect, and a validator that
    silently ignored anyOf would be a false pass, so there is no subset
    validator and no hand-rolled walk.  systemone validates locally, with
    no import: every question asked has an answer, and every answer's
    type matches its question's, which is what the provider's own shaping
    leaves for dic to check.

    >>> validate_json({"type": "object"}, "jsonschema", '{"a": 1}')
    []
    >>> validate_json({"questions": {"b": {"type": "noul"}}}, "systemone",
    ...               '{"b": {"type": "noul", "noul": 0.9}}')
    []
    >>> validate_json({"questions": {"b": {"type": "noul"}}}, "systemone",
    ...               '{"b": {"type": "score", "score": 1}}')
    ['b: expected a noul answer']
    >>> validate_json({"questions": {"b": {"type": "noul"}}}, "systemone", '{}')
    ['b: no answer']
    """
    try:
        answer = json.loads(text)
    except ValueError as e:
        return [f"answer is not JSON: {e}"]
    if dialect == schema_mod.SYSTEMONE:
        return _systemone_errors(schema, answer)
    return _jsonschema_errors(schema, answer)


def _systemone_errors(schema, answer):
    """Local validation of a systemone answer, with no jsonschema import.

    >>> _systemone_errors({"questions": {"a": {"type": "noul"}}},
    ...                   {"a": {"type": "noul", "noul": 0.5}})
    []
    >>> _systemone_errors({"questions": {"a": {"type": "noul"}}}, "x")
    ['answer is not an object']
    """
    if not isinstance(answer, dict):
        return ["answer is not an object"]
    questions = schema.get("questions") or {}
    errors = []
    for name, question in questions.items():
        if name not in answer:
            errors.append(f"{name}: no answer")
            continue
        got = answer[name]
        if not isinstance(got, dict) or got.get("type") != question.get("type"):
            errors.append(f"{name}: expected a {question.get('type')} answer")
    return errors


def _jsonschema_errors(schema, answer):
    """jsonschema validation, against a validator that cannot fetch.

    FormatChecker is passed because `format` is annotation-only without
    it, and a date-time that is not one should fail.  A remote $ref is an
    error: following one is a network fetch from a config file, which is
    a side effect dic's security model says it does not have.
    """
    _refuse_remote_refs(schema)
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError as e:
        raise DicError(f"validation needs jsonschema: {e}") from None
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    return [f"{'/'.join(str(p) for p in e.path) or '.'}: {e.message}"
            for e in validator.iter_errors(answer)]


def _refuse_remote_refs(node):
    """A remote $ref is an error: it is a fetch, and dic has no side effects."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(("http://", "https://")):
            raise DicError(f"--schema: remote $ref is an error: {ref}")
        for value in node.values():
            _refuse_remote_refs(value)
    elif isinstance(node, list):
        for value in node:
            _refuse_remote_refs(value)


def code_block(text):
    """The body of the first fenced code block in text, or text unchanged.

    >>> code_block("prose\\n```python\\nx = 1\\n```\\nmore")
    'x = 1\\n'
    >>> code_block("no fence here")
    'no fence here'
    """
    match = re.search(r"```[^\n]*\n(.*?)```", text, re.S)
    return match.group(1) if match else text


def jsonl_records(text, repairs=True):
    """A JSONL answer as one record per line: the container object unwrapped.

    No provider's structured output accepts a top-level array, so a stream
    of records is asked for as one object with them under `jsonl`; this is
    where that object is opened into the lines a reader counts.  A fence is
    common even under a strict schema, so the answer is unwrapped from its
    code block first and repaired after that if it still will not parse.

    A line is one JSON value, which is what a reader counts at a glance and
    what a later tool reads one record at a time.

    >>> jsonl_records('{"jsonl": [{"a": 1}, {"b": 2}]}')
    ('{"a": 1}\\n{"b": 2}\\n', None)
    >>> jsonl_records('```json\\n{"jsonl": [{"a": 1}]}\\n```')
    ('{"a": 1}\\n', 'code_block')
    >>> jsonl_records('{"jsonl": []}')
    ('', None)
    """
    repaired, what = (repair_json(text) if repairs else (text, None))
    for candidate in (text, repaired):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if not isinstance(parsed, dict):
            continue
        records = parsed.get(schema_mod.JSONL_KEY)
        if isinstance(records, list):
            lines = [json.dumps(record, ensure_ascii=False) + "\n"
                     for record in records]
            return "".join(lines), what
    return text, None


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


def split_attachment(value):
    """(name, path) of one -a value: NAME=FILE, or FILE's own basename.

    NAME is the slot or placeholder the backend looks for; a bare FILE
    takes its basename, which is what makes `-a pic.png` bind `pic.png`
    without a second word.  A name the file itself can be is left alone,
    so a path that contains '=' is still a path.

    >>> split_attachment("image_url=frame.png")
    ('image_url', 'frame.png')
    >>> split_attachment("docs/report.pdf")
    ('report.pdf', 'docs/report.pdf')
    """
    if os.path.exists(value):
        return os.path.basename(value), value
    name, sep, path = value.partition("=")
    if not sep:
        return os.path.basename(value), value
    return name, path


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


def redact(value, key, variable):
    """A header value with the API key replaced by the variable holding it.

    The replacement is length-guarded because a key short enough to appear
    in an unrelated header is one whose substitution would corrupt it; a
    real key is long and unique.

    >>> redact("Bearer sk-abcdef123", "sk-abcdef123", "K")
    'Bearer $K'
    >>> redact("application/json", "sk-abcdef123", "K")
    'application/json'
    >>> redact("Bearer x", "x", "K")
    'Bearer x'
    """
    if key and len(key) >= 8 and key in value:
        return value.replace(key, f"${variable}")
    return value


def printable(body):
    """The body as data json can encode, its file parts named rather than sent.

    A multipart adaptor keeps its files under a private key, which json.dumps
    cannot encode any more than curl can read it from -d; they are named here
    so the rest of the body is still visible.

    >>> printable({"model": "m"})
    {'model': 'm'}
    >>> printable({"model": "m", "_files": [{"path": "a.png", "data": b"x"}]})
    {'model': 'm', '# files': ['a.png']}
    """
    out = dict(body)
    for key, name in (("_files", "# files"), ("_file", "# file")):
        if key in out:
            files = out.pop(key)
            files = files if isinstance(files, list) else [files]
            out[name] = [block.get("path", "attachment") for block in files]
    return out


def curl_command(model, adaptor, body, headers, api_key, api_type):
    """The one request as a shell command a student can read and run.

    dic builds the last line and nothing else: the URL, the headers and the
    name of the variable holding the key are the same for every call to a
    model, and differ between models only in the configuration.  The key is
    printed as the variable that holds it, so the command pasted into a bug
    report leaks nothing, and the body is a heredoc so that a prompt with a
    quote in it is still one command.

    >>> class A: PATH = "/chat/completions"
    >>> model = {"model_id": "groq+qwen", "model_name": "qwen/q",
    ...          "api_base": "https://api.groq.com/openai/v1",
    ...          "api_key_name": "GROQ_API_KEY"}
    >>> print(curl_command(model, A, {"model": "qwen/q"},
    ...                    {"Authorization": "Bearer sk-abcdef123"},
    ...                    "sk-abcdef123", "openai-chat"), end="")
    # model      groq+qwen  (qwen/q)
    # api_base   https://api.groq.com/openai/v1
    # api_type   openai-chat, path /chat/completions
    # api_key    $GROQ_API_KEY
    <BLANKLINE>
    curl -sS 'https://api.groq.com/openai/v1/chat/completions' \\
      -H "Authorization: Bearer $GROQ_API_KEY" \\
      --data-binary @- <<'DIC_BODY'
    {
      "model": "qwen/q"
    }
    DIC_BODY
    """
    path = getattr(adaptor, "PATH", "") or ""
    lines = [
        f"# model      {model['model_id']}  ({model['model_name']})",
        f"# api_base   {model['api_base']}",
        f"# api_type   {api_type}, path {path or '(none)'}",
    ]
    if model.get("api_key_name"):
        lines.append(f"# api_key    ${model['api_key_name']}")
    if model.get("cache"):
        lines.append(f"# cache      {model['cache']}")
    if hasattr(adaptor, "call"):
        lines.append("# note       this adaptor polls; this starts the job")
    lines.append("")

    # a call() adaptor names its resource in the model and not in PATH, and
    # that is the one place the URL is not the base plus the adaptor's path
    base = model["api_base"].rstrip("/")
    url = base + path if path else f"{base}/{model['model_name']}"
    lines.append(f"curl -sS '{url}' \\")
    for name, value in headers.items():
        shown = redact(value, api_key, model.get("api_key_name") or "")
        quote = '"' if shown != value else "'"
        lines.append(f"  -H {quote}{name}: {shown}{quote} \\")
    shown_body = printable(body)
    files = [b for b in body.get("_files", []) if isinstance(b, dict)]
    if isinstance(body.get("_file"), dict):
        files.append(body["_file"])
    if files:
        # multipart: its fields are form parts and its files are file parts,
        # and curl picks its own boundary, so this is the shape and not the
        # bytes -- a -d body would not reproduce the request at all
        for block in files:
            lines.append(f"  -F 'file=@{block.get('path', 'attachment')}' \\")
        for name, value in shown_body.items():
            lines.append(f"  -F '{name}={value}' \\")
        lines[-1] = lines[-1].rstrip(" \\")
    else:
        lines.append("  --data-binary @- <<'DIC_BODY'")
        lines.append(json.dumps(shown_body, indent=2, ensure_ascii=False))
        lines.append("DIC_BODY")
    return "\n".join(lines) + "\n"


def dic(prompt,
        # ----- directly affect API call -----
        model:        flag(short="-m", help="which model") = None,
        system:       flag(short="-s", help="system prompt") = None,
        prepend_prompt: flag(long="--prepend-prompt", action="append",
                             metavar="TEXT", env=False,
                             help="prepend TEXT to the prompt, above the"
                                  " command-line words and any stdin;"
                                  " repeatable") = None,
        attachment:   flag(short="-a", action="append", metavar="[NAME=]FILE",
                           help="attach a file; NAME names the slot or"
                                " placeholder it lands in, and defaults to"
                                " the file's own basename") = None,
        option:       flag(short="-o", action="append", metavar="KEY=VALUE",
                           help="override a model option") = None,
        cache:        flag(long="--cache", metavar="TTL",
                           help="cache the prompt: off (the default), or a"
                                " TTL the model prices") = None,
        tools:        flag(long="--tools", action="append",
                           metavar="MODULE:FUNC",
                           help="offer a python function as a tool; repeatable") = None,
        mcp:          flag(long="--mcp", action="append", metavar="WORDS|URL",
                           help="offer the tools of an MCP server: a command"
                                " dic runs sandboxed, whose leading words are"
                                " the sandbox's own -- `--share-net mcp-fetch`"
                                " for the network, `K=v cmd` for a secret; or"
                                " a URL to POST to; repeatable") = None,
        mcp_url:      flag(long="--mcp-url", action="append", metavar="URL",
                           help="--mcp with the guess left out, for a URL"
                                " that carries no scheme") = None,
        mcp_shell:    flag(long="--mcp-shell", action="append", metavar="WORDS",
                           help="--mcp with the guess left out, for a"
                                " command that begins with a scheme; a word"
                                " list, never a shell line") = None,
        cont:         flag(short="-c", long="--continue", action="bool",
                           help="continue this session's last conversation") = False,
        mid:          flag(long="--mid", metavar="REF",
                           help="continue from a message: a mid, its prefix,"
                                " or a ref like HEAD~3") = None,
        mime_type:    flag(long="--mime-type", metavar="TYPE",
                           help="the mime type of the answer; a full type"
                                " such as text/csv, or a bare name such as"
                                " json, jsonl or png") = None,
        schema:       flag(long="--schema", metavar="REF",
                           help="a JSON Schema, a path to one, or a"
                                " shorthand in the llm or systemone"
                                " dialect: constrain the answer to it") = None,
        repair:       flag(long="--repair", metavar="MODE", group=OUTPUT,
                           help="repair a JSON answer that is fenced or"
                                " broken: on, off, or auto (the default,"
                                " per protocol)") = None,
        validate:     flag(long="--validate", metavar="MODE", group=OUTPUT,
                           help="check a JSON answer against the schema:"
                                " on, off, or auto (the default, per"
                                " protocol)") = None,
        # ----- post-process the output -----
        path:         flag(long="--path", metavar="FILE", group=OUTPUT,
                           help="write the answer to FILE, atomically") = None,
        force:        flag(short="-f", action="bool", group=OUTPUT,
                           help="overwrite the file named by --path") = False,
        clipboard:    flag(action="yes/no", group=OUTPUT,
                           help="copy the answer to the terminal's clipboard"
                                " (the default on a terminal)") = None,
        extract:      flag(short="-x", action="bool", group=OUTPUT,
                           help="print only the first fenced code block") = False,
        pretty:       flag(action="yes/no", group=OUTPUT,
                           help="reindent a JSON answer as it prints (the"
                                " default for JSON; DIC_PRETTY=0 turns it"
                                " off)") = None,
        # ----- debugging -----
        print_:       flag(long="--print", metavar="WHAT", env=False,
                           group=DEBUG,
                           help="print the request as curl or indented json"
                                " and exit") = None,
        pv_thinking:  flag(action="yes/no", group=DEBUG,
                           help="meter the reasoning instead of printing it") = None,
        pv_response:  flag(action="yes/no", group=DEBUG,
                           help="meter the answer on stderr as well") = None,
        record:       flag(long="--record", metavar="KIND", group=DEBUG,
                           help="record per-round byte-rate samples, for"
                                " plotting B/s over time; the only value is"
                                " bps") = None,
        verbosity:    flag(short="-v", action="count", env=False,
                           command=GLOBAL, group=DEBUG,
                           help="raise stderr verbosity; repeatable"
                                " (DIC_VERBOSITY sets the base)") = None,
        quiet:        flag(short="-q", action="bool", command=GLOBAL,
                           group=DEBUG,
                           help="print nothing to stderr but errors") = False,
        models_file:  flag(command=GLOBAL, group=DEBUG,
                           help="a json file of model entries to overlay") = None,
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
    # The prompt is one argument, so a wrapper with instructions of its own
    # passes them with --prepend-prompt and never as extra words on the
    # line; each is prepended in the order given, above the caller's own
    # words and above anything that arrived on stdin.
    prepend = knobs["prepend_prompt"] or []
    if isinstance(prepend, str):
        prepend = [prepend]
    prompt = "\n\n".join(piece for piece in [*prepend, prompt] if piece)
    # a print flag answers with the request dic would send, so it needs the
    # model, the history and the adaptor, and it stops before the network
    printing = knobs["print_"]
    if knobs["record"] not in (None, "bps"):
        raise DicError(f"--record must be bps, not {knobs['record']!r}")

    conn = db(env)
    session = env.get("DIC_SESSION", "global")
    config.sync(conn, env, knobs["models_file"])   # a stat per file; a parse only when one moved

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

    if knobs["cont"] and knobs["mid"]:
        # -c names this session's pointer and --mid names a ref; both at once
        # is two answers to one question, so it is refused rather than won.
        raise DicError("pick one: -c or --mid")

    prev_mid = (resolve_ref(conn, knobs["mid"], env) if knobs["mid"]
                else (session_head(env) if knobs["cont"] else None))
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
    if (api_type in ("fal", "openai-images", "openai-videos", "typesafe")
            and (knobs["cont"] or knobs["mid"])):
        # -c and --mid name a conversation, and these backends keep none:
        # fal generates one asset from one request and typesafe takes a
        # caller-supplied state on every call, so a call that answered
        # would silently drop the history the row and the session pointer
        # claim to carry
        raise DicError(f"{model['model_id']}: cannot continue:"
                       f" {api_type} has no conversation")
    adaptor = load_adaptor(api_type, env)
    key_name = model.get("api_key_name")
    if not key_name:
        raise DicError(f"{model['model_id']}: no api_key_name configured")
    api_key = env.get(key_name)
    if not api_key:
        raise DicError(f"{key_name} is not set")

    # one line, one repaint, from here to the end of the call: every wait a
    # user sits through -- dns, the mcp handshake, ttft -- is painted on it
    # and closed when it is over, so a slow server and a hung one are told
    # apart without a line of stderr for each
    line = Line(err, env)

    # --tools names python, so dic.tool -- and the inspect and typing it reads
    # the annotations with -- is imported only when one was given, exactly as
    # only the selected adaptor is.  A spec that does not import is an error
    # here, before the network and before a .part is opened.  A continuing
    # conversation replays the blob its own row recorded, so the model is
    # re-offered exactly the tools it was offered and not whatever the module
    # says today.
    given = knobs["tools"]
    if isinstance(given, str):      # DIC_TOOLS="pkg.mod:fn" is one spec, not chars
        given = [given]
    stored = json.loads(rows[-1]["tools"]) if rows and rows[-1]["tools"] else {}
    specs = given or stored.get("specs") or []

    # --mcp names servers, whose tools arrive over a pipe or a POST instead of
    # over an import.  They become records of the same shape, so the schema
    # builder, the tool loop and the stored row need no MCP case at all.  A
    # call that names one opens it now, because it must know what to offer; a
    # continuation reads its own row's servers and opens one on the first call
    # to one of its tools, so a server that has since died is a tool result
    # and never a failed call.
    named = bool(knobs["mcp"] or knobs["mcp_url"] or knobs["mcp_shell"])
    pairs, mcp_map, mcp_tools = [], {}, []
    if named or (rows and rows[-1]["mcp"]):
        from dic import mcp

        def say(level, message):
            report(verbosity, level, message, err=err, env=env)

        pairs = (mcp.specs(knobs["mcp"], knobs["mcp_url"], knobs["mcp_shell"])
                 if named else json.loads(rows[-1]["mcp"]))
        mcp_tools, mcp_map = mcp.resolve(pairs, None if named else stored,
                                         say, line)

    # the loop dispatches through tool.call whichever record it was handed, so
    # the module is imported when either source of records fired, and never
    # for an invocation that offers no tool at all
    tools = ()
    if specs or mcp_tools:
        from dic import tool
        tools = tuple(tool.resolve(specs or [])) + tuple(mcp_tools)

    # The schema is inherited by -c like -m, -s and -o are: --schema names
    # one, else the row's, else $DIC_SCHEMA, else the model's own key.  A
    # schema is a request, so an adaptor that cannot send one refuses here
    # rather than answering a call the caller believes is constrained.
    schema_text = knobs["schema"]
    schema_value, schema_dialect = None, None
    if schema_text is None:
        if rows and rows[-1]["schema"]:
            schema_value = json.loads(rows[-1]["schema"])
            schema_dialect = rows[-1]["dialect"]
        elif model.get("schema"):
            schema_value, schema_dialect = schema_mod.parse(model["schema"])
    elif schema_text != "":
        schema_value, schema_dialect = schema_mod.parse(schema_text)
    if (schema_value is not None
            and not getattr(adaptor, "output_format", None)):
        # an attribute that is absent is a refusal: an answer the caller
        # believes is schema-shaped and is not is the one failure this
        # feature exists to prevent.  --schema='' is no schema at all, so
        # it is not refused here, and a bare JSON mime is asked of every
        # adaptor that can ask it.
        raise DicError(f"{model['model_id']}: {api_type} has no"
                       " output_format, so --schema cannot be sent")
    # what the answer will be, settled before the call so that a missing or
    # Strict dialect match: a systemone schema is for typesafe and a
    # jsonschema or simonw_llm one is for the text adaptors, and there is
    # no cross-compilation.  A noul compiled to a number loses
    # calibration, which is the point of the type; a string compiled to a
    # question has no instruction to carry.  Both directions refuse.
    if schema_dialect is not None:
        if api_type == "typesafe" and schema_dialect != schema_mod.SYSTEMONE:
            raise DicError(f"typesafe: {schema_dialect} is not its dialect")
        if api_type != "typesafe" and schema_dialect == schema_mod.SYSTEMONE:
            raise DicError(f"{api_type}: {schema_dialect} is only"
                           " for typesafe")

    # taken --path fails before the network instead of after a whole video.
    # A schema makes it application/json, so a JSON mime restates what it
    # already makes and a text/* mime that is not JSON drops it; anything
    # else contradicts it.  --schema='' keeps the JSON mime with no
    # constraint.  A bare --mime-type -- json, png -- is the right half
    # of one, resolved here so both spellings name one request.
    asked_mime = (schema_mod.resolve_mime(knobs["mime_type"])
                  if knobs["mime_type"] else None)
    if (asked_mime and schema_value is not None
            and not schema_mod.is_json_mime(asked_mime)):
        if asked_mime.startswith("text/"):
            schema_value, schema_dialect = None, None
        else:
            raise DicError(f"--mime-type {asked_mime} contradicts a schema")
    mime = (asked_mime
            or ("application/json" if schema_value is not None else None)
            or model.get("output")
            or "text/plain")
    # A JSON mime promises that the answer is JSON and that it is the answer
    # the caller asked for, and a schema is the only thing that says either;
    # no provider takes one without it, so the ask is refused here rather
    # than at the API, where a bare json_object failed and a JSONL ask had
    # nothing to wrap.
    if schema_value is None and schema_mod.is_json_mime(mime):
        raise DicError(f"--mime-type {mime} needs --schema: no provider"
                       " takes a JSON mime without one")
    if knobs["extract"] and schema_mod.is_json_mime(mime):
        # -x names the part of the answer to print and a JSON mime names
        # what the answer is; the two are two answers to one question
        raise DicError("-x and a JSON mime are two answers to what the"
                       " answer is")
    # A JSONL mime is the container, and JSON Schema cannot describe a
    # stream of documents: the records are asked for in one object under a
    # key, because no provider's structured output accepts a top-level
    # array.  The wire schema is what is sent; the row keeps the caller's
    # own beside a container fact, so the wire shape differs from the
    # asked-for schema and neither lies about the other.
    wire_schema, container = schema_mod.wrap(schema_value, mime)
    # A JSON answer is reindented as it prints: --pretty is the default for
    # a mime that is one document, and --no-pretty (or DIC_PRETTY=0/false)
    # turns it off.  A JSONL answer is a line per record and reindenting it
    # would break the one thing its mime promises, so it is never pretty.
    off = knobs["pretty"] is not None and not knobs["pretty"]
    pretty_printer = (output.Reindent()
                      if schema_mod.is_json_mime(mime) and not container
                      and not off else None)
    # repair and validate resolve per (api_type, dialect), so the common
    # case is zero configuration; an explicit on where the protocol cannot
    # is an error, because a call the caller believes is checked and is not
    # is the one failure this feature exists to prevent
    repairs = resolve_toggle(knobs["repair"],
                             repairs_auto(api_type, schema_dialect),
                             "repair", model)
    validates = resolve_toggle(knobs["validate"],
                               validates_auto(api_type),
                               "validate", model)
    # both facts start empty: a call that is not a schema call writes them
    # NULL, and a schema call that repairs or validates fills them in
    repaired, errors = None, []
    # The output tool is how a protocol with no response_format carries a
    # schema: its arguments are the answer and its call ends the loop.  It
    # is never a tool the user offered, and a protocol with no OUTPUT_TOOL
    # has no such channel.
    output_tool = (getattr(adaptor, "OUTPUT_TOOL", None)
                   if schema_value is not None
                   and getattr(adaptor, "output_format", None) else None)
    # the text channel dictates with strict:true and the tool channel asks;
    # the row records which, so a provider that quietly walked a schema
    # back is visible in the row rather than hidden by it
    enforced = (1 if getattr(adaptor, "ENFORCES_SCHEMA", False) else 0
                if schema_value is not None
                and getattr(adaptor, "output_format", None) else None)
    sink = None
    if not printing:
        # a printed request writes no file and makes no call, so the sink
        # and the --path check that guards it have nothing to guard
        if schema_mod.printable(mime):
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

    # a printed request writes no row for an attachment to live in, so the
    # file is read here and nothing is recorded.  Each -a is NAME=FILE or a
    # bare FILE: NAME is the slot the backend looks for and defaults to the
    # file's basename, so the explicit form is only for where the basename
    # is not what the backend wants.
    named_files = [split_attachment(value)
                   for value in knobs["attachment"] or ()]
    attached = [file_block(path, name=name) if printing
                else store_attachment(conn, path, name)
                for name, path in named_files]
    turns.append({"role": "user",
                  "blocks": [dict(att) for att in attached]
                            + [{"type": "text", "text": prompt}]})
    turns = normalize(turns)

    # -o, like -m and -s, is replayed from the row the conversation continues
    # from unless this call names its own: the parameters a thread was shaped
    # by are part of it, and making a caller re-type them to continue would
    # change the request silently when they did not.
    overrides = knobs["option"]
    if rows and not overrides and rows[-1]["options"]:
        overrides = json.loads(rows[-1]["options"])
    opts = options(model, overrides or ())
    # typesafe has one request shape: the questions come from --schema and
    # the guide rails that shape an answer are its own.  The first errors a
    # new typesafe user hits are a missing schema and two spellings of one
    # field, and both name what to do instead.
    if api_type == "typesafe":
        if opts.get("questions") is not None and schema_value is not None:
            raise DicError("typesafe: pick one: -o questions or --schema")
        if schema_value is None and not opts.get("questions"):
            raise DicError("typesafe requires --schema; try\n"
                           "     --schema 'billing: noul \"Is this about"
                           " billing?\"'")
        if mime in schema_mod.JSONL_MIMES:
            raise DicError("typesafe: JSONL is not a typesafe shape")
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
        # a replayed row carries the schema it was offered, so a continuing
        # call asks for exactly that and never regenerates it; a caller who
        # names --tools gets the freshly described schema instead
        opts["tools"] = (adaptor.tool_schema(tools)
                         if given or named or not stored.get("schema")
                         else stored["schema"])
    prepare = getattr(adaptor, "prepare", None)
    if prepare is not None:            # fal reads URLs, so files go up first
        # a prepare that uploads is time the user waits, and an upload is a
        # side effect: a print mode names the step and never runs it
        pending = any("data" in block
                      for turn in reversed(turns) if turn.get("role") == "user"
                      for block in turn["blocks"])
        if printing and pending:
            raise DicError(f"{api_type}: this adaptor uploads its attachments"
                           " before the call, so a printed request would be"
                           " built from URLs that do not exist yet")
        if not printing:
            line.wait()
            turns = prepare(model, api_key, turns, opts)
            line.first_token()

    def request_body():
        """build() and the output_format hook, for this round's turns.

        The hook fires before --print=json/--print=curl, so a printed
        request carries the directive and not one that lacks it.  A tool
        loop rebuilds the body every round, so the hook fires on every
        rebuild and not only the first: a later round without it would be
        a schema call that stopped asking for the schema the moment the
        model used a tool.
        """
        built = adaptor.build(model, turns, system, opts)
        hook = getattr(adaptor, "output_format", None)
        return hook(built, model, wire_schema, mime) if hook else built

    body = request_body()
    headers = {"content-type": "application/json", "accept": "text/event-stream"}
    headers.update(adaptor.auth(api_key))
    headers.update(model.get("headers") or {})

    if printing:
        # the printer answers with the request, and no request is made: both
        # flags render the same body, so they cannot disagree about it
        text = json.dumps(printable(body), indent=2, ensure_ascii=False) + "\n"
        if knobs["print_"] == "curl":
            text = curl_command(model, adaptor, body, headers, api_key, api_type)
        out.write(text)
        out.flush()
        return Reply(text=text, model_id=model["model_id"], api_type=api_type)

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
            pv_update(pv, final=True)
        if pv_resp is not None:
            pv_update(pv_resp, final=True)
        # the block is finished: its rows stay where they are on the screen
        # and the next thing written starts below them
        line.close()

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
            model["model_id"], api_type, session, acc.get("upstream"),
            status, error,
            json.dumps(usage) if usage else None,
            cost, json.dumps(items) if items is not None else None,
            source if cost is not None else None,
            wire.get("t_start"), wire.get("t_connect"), wire.get("t_request"),
            wire.get("t_headers"), wire.get("t_first"), wire.get("t_last"),
            wire.get("t_done"),
            json.dumps({
                "specs": list(specs), "schema": opts.get("tools"),
                "records": [{"name": t["name"],
                             "description": t["description"],
                             "parameters": t["parameters"]} for t in tools],
                "mcp": mcp_map}) if tools else None,
            json.dumps(pairs) if pairs else None,
            json.dumps(overrides) if overrides else None,
            json.dumps(schema_value) if schema_value is not None else None,
            schema_dialect,
            mime,
            container,      # 1 when an array wrapped the schema, else NULL
            repaired,       # the repair that ran, or NULL
            enforced))      # 1 dictated, 0 asked, NULL when no schema
        if recorder is not None:
            # one row per round that produced bytes, and none for one that did
            # not: a round's samples are measured from that round's own start
            samples = recorder.json()
            if samples:
                conn.execute("INSERT INTO record VALUES (?, ?)", (mid, samples))
        return mid

    paint = use_color(out, env) and knobs["path"] is None
    # the answer is blue on a terminal, so it is also one escape sequence away
    # from that terminal's clipboard: OSC 52, which the emulator performs
    # itself and which needs no subprocess and no clipboard protocol.  The rule
    # is the paint rule -- a pipe, a --path and a NO_COLOR each give nothing --
    # and --clipboard/--no-clipboard force it either way.
    clipboard = paint if knobs["clipboard"] is None else knobs["clipboard"]

    acc, chunks, wire, asked, results = {}, [], {}, (), []
    recorder = None
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
            recorder = (Recorder(wire["t_start"])
                        if knobs["record"] == "bps" else None)
            line.restart()          # this request's clock, not the call's
            # the meters are rows of the same block as the clock, so a call
            # with four handshakes beside it shows four lines and not one
            # rewritten by four writers; a row is named, so the round a call
            # already has is the row its next round reuses
            pv = line.row("thinking") if pv_thinking else None
            pv_resp = line.row("response") if pv_response else None
            if pv is not None or pv_resp is not None:
                line.wait()         # a bare ttft clock until something arrives
            stream = (call(model, api_key, body, line, wire)
                      if call is not None else
                      events(model["api_base"], adaptor.PATH, headers, body,
                             wire, line))
            for event in stream:
                chunk, kind = adaptor.parse(event, acc)
                if acc.get("upstream"):
                    # a router named the upstream mid-wait, so it rides the
                    # clock it explains
                    line.extra = f"upstream: {acc['upstream']}"
                if not chunk:
                    continue
                if kind != "blob":
                    sink.end_blob()     # a blob ends when anything else arrives
                wire.setdefault("t_first", time.time_ns())
                line.first_token()      # the wait is over, whatever arrived
                if recorder is not None:
                    recorder.add(time.time_ns(), kind or "response",
                                 len(chunk if isinstance(chunk, bytes)
                                     else chunk.encode()))
                if kind == "thinking":
                    if pv is not None:
                        pv_update(pv, chunk)
                    elif not pv_thinking:
                        # --no-pv-thinking streams the reasoning itself, and
                        # prose is not a block: the rows are let stand and
                        # the text goes below them
                        line.close()
                        err.write(INFO + chunk + RESET if use_color(err, env)
                                  else chunk)
                        err.flush()
                    continue
                if kind == "blob":      # bytes: --path was checked above
                    sink.write_blob(chunk)
                    if pv_resp is not None:
                        pv_update(pv_resp, chunk)
                    continue
                if pv is not None:      # the answer starts on a line of its own
                    pv_update(pv, final=True)
                    pv = None           # frozen once: the next repaint would
                                        # move the block into the answer
                chunks.append(chunk)
                printed.append(chunk)
                if pv_resp is not None:
                    pv_update(pv_resp, chunk)
                # the pretty text and the row's own text are two texts: the
                # meter counted the bytes the stream carried, the row keeps
                # them, and stdout is shown the reindented form
                if pretty_printer is not None:
                    chunk = pretty_printer.feed(chunk)
                # JSONL buffers rather than streams: the fence has to be
                # found before a record can be yielded, and finding it
                # means the whole answer -- the reformat happens once,
                # below, and what reaches stdout is the records
                if chunk and not knobs["extract"] and not container:
                    if paint and painted != kind:
                        sink.write((RESET if painted is not None else "") + PAYLOAD)
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
            asked = adaptor.calls(acc) if (tools or output_tool) else ()
            # The output tool's arguments are the answer: this round is
            # the last one, whatever else the model asked for beside it,
            # because the answer arrives on that call and on no text
            # delta -- a tool run beside it would be work for an answer
            # that has already been given.
            answer = answer_from_calls(asked, output_tool)
            if answer is not None:
                asked = []
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
            if answer is not None:
                # the protocol's text stream carried no answer, so the
                # object the tool call returned is the whole of it: it is
                # serialized into the same chunks a text delta would have
                # gone into, and response_raw keeps the tool_use block
                # above for a later audit and for replay within this
                # provider.
                call_answer = json.dumps(answer, ensure_ascii=False)
                chunks.append(call_answer)
                printed.append(call_answer)
                shown = (pretty_printer.feed(call_answer)
                         if pretty_printer is not None else call_answer)
                if shown and not knobs["extract"] and not container:
                    if paint and painted != "":
                        sink.write((RESET if painted is not None else "")
                                   + PAYLOAD)
                        painted = ""
                    sink.write(shown)
                break
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
            body = request_body()
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
    if container:
        # JSONL: the mime asked for one value per line and the record
        # boundaries are the container, not the model's own newlines.
        # The reformat happens here because the fence has to be found
        # before any record can be yielded, and `chunks` becomes the
        # reformatted text so the row's response is what stdout carries.
        response, what = jsonl_records(response, repairs)
        if what:
            repaired = repaired or what
        chunks = [response]
    text = response
    if knobs["extract"]:
        text = code_block(response)
        sink.write(PAYLOAD + text + RESET if paint else text)
    elif container:
        sink.write(PAYLOAD + text + RESET if paint else text)
    else:
        if painted is not None:
            sink.write(RESET)
        if pretty_printer is not None:
            # the reindenter ends the answer with its own newline: the DFA
            # drops whitespace, so a response that ended in one has none
            # for the test below to find
            tail = pretty_printer.close()
            sink.write(tail)
            text += tail
        elif response and not response.endswith("\n"):
            sink.write("\n")
            text += "\n"
    # Repair first, then validate, on the printed text: this is the one rule
    # that makes the two features compose, and a schema is what says either
    # is worth doing at all.  A prose call owes neither and pays for neither.
    if schema_value is not None and schema_mod.is_json_mime(mime):
        if container:
            # JSONL: the wire was an array, each element is one record,
            # and the caller's schema is what each record is checked
            # against -- the array is dic's wrap and never the schema.
            # The lines are the ones stdout carries, so the error names
            # one a reader can count.
            if validates:
                errors = [f"line {n}: {error}"
                          for n, line in enumerate(text.splitlines(), 1)
                          for error in validate_json(schema_value,
                                                     schema_dialect, line)]
        else:
            if repairs:
                text, repaired = repair_json(text)
            if validates:
                errors = validate_json(schema_value, schema_dialect, text)
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
    session_move(env, mid)

    if errors:
        # The answer is printed and the pointer has moved: the model must
        # see its own bad output to correct it, and -c that could not replay
        # the failure would be a thread that cannot be fixed.  Validation
        # that did not affect the exit status would be theatre.
        raise DicError("validation failed, the answer is above:\n"
                       + "\n".join(f"  {error}" for error in errors))

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
