"""fal.ai's queue API: submit a job, poll it, fetch the asset.

fal's models are not chat models.  There is no conversation to rebuild and
no server-sent stream to read: a request is submitted to a queue and
returns an id, the id is polled until the job stops moving, and the job's
result names one asset URL that is then fetched as bytes.  So this adaptor
replaces the client's transport with call(), and its parse() reads the two
events call() yields -- a result, and a blob.

Each fal model also names its own arguments, and reads its images, video
and audio as URLs rather than bytes.  prepare() therefore uploads the
newest turn's attachments to fal's storage before the call, and the NAME
of its -a says which argument each one lands in.

Exports PATH, auth, prepare, build, call, parse, finish; see
adaptors/openai_chat.py for the contract.
"""
import json, os, time, urllib.error, urllib.request

from dic.store import http_url
from dic.tty import DicError, report

PATH = ""                              # the model name is the path; see call()
UPLOAD = "https://rest.alpha.fal.ai"   # attachments are stored here first
POLL_MS = 500
POLL_S = 900                           # ten minutes is a long video

# The catalog is served through a CDN in front of fal's own api, and a
# refusal to expand the schema arrives in two shapes with one cause: a 429,
# and a 200 whose `openapi` value is an error object instead of a document.
# Both mean the request arrived and was refused further upstream, so the
# request is tried again, and the last refusal is what the caller sees.
#
# No credential is sent to the catalog.  It needs none, and sending one is
# not merely useless: the CDN marks a request carrying an Authorization
# header uncacheable, bypasses its cache, and forwards to fal's origin,
# which is where the expansion fails -- the same request without the header
# is served from the edge and carries the document.  dic's key belongs to
# queue.fal.run, where the call goes, and to nowhere else.
EXPANSION_FAILED = "expansion_failed"
CATALOG_TRIES = 4
CATALOG_BACKOFF_S = 1.5
BODY_SHOWN = 300

# What a request to the catalog names itself: the default
# `Python-urllib/3.x` is what an edge bans, and every other request dic
# makes names dic.
USER_AGENT = "dic/1"

# fal's endpoints are all different, so there is no protocol-level set
# of options to list: everything a caller can pass comes from the
# endpoint's own schema, which model_params fetches.
PARAMS = {}


def auth(key):
    """>>> auth("k")
    {'Authorization': 'Key k'}
    """
    return {"Authorization": "Key " + key}


def newest(turns):
    """(prompt, files) of the newest user turn: all of a fal call's input.

    fal generates one asset from one prompt, so only the last user turn is
    sent; a conversation cannot be replayed into a model that has none.

    >>> newest([{"role": "user", "blocks": [{"type": "text", "text": "a"}]}])
    ('a', [])
    """
    for turn in reversed(turns):
        if turn.get("role") != "user":
            continue
        prompt = "\n".join(b["text"] for b in turn["blocks"]
                           if b["type"] == "text")
        return prompt.strip(), [b for b in turn["blocks"] if b["type"] != "text"]
    return "", []


def fetch(request, decode=True):
    """urlopen a request; JSON when asked, bytes when not, DicError on 4xx/5xx."""
    try:
        http_url(request.full_url)
        with urllib.request.urlopen(request) as response:  # noqa: S310
            return json.load(response) if decode else response.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace").strip()[:300]
        parts = (str(e.code), request.full_url, detail)
        error = DicError("fal: " + " ".join(p for p in parts if p))
        # the status travels with the error: a caller that retries needs to
        # tell a 429, which is the CDN asking for the request again, from a
        # 404, which is the endpoint not being there at all
        error.code = e.code
        raise error from None
    except urllib.error.URLError as e:
        raise DicError(f"fal: {request.full_url}: {e}") from None


def upload(key, data, mime_type, name="attachment"):
    """Put bytes in fal's storage and return the URL a model reads them from.

    Two requests: one to reserve a name and be told where to write, one to
    write the bytes there.  Nothing is retried; an upload that fails fails
    the call that asked for it, rather than sending a URL that is not a file.
    """
    init = json.dumps({"content_type": mime_type, "file_name": name}).encode()
    ticket = fetch(urllib.request.Request(  # noqa: S310
        UPLOAD + "/storage/upload/initiate", data=init,
        headers={**auth(key), "Content-Type": "application/json"}))
    # the PUT is answered with an empty body, so nothing here is json: the
    # upload either took or the status code says so, and the URL is the truth
    fetch(urllib.request.Request(  # noqa: S310
        ticket["upload_url"], data=data, method="PUT",
        headers={"Content-Type": mime_type}), decode=False)
    return ticket["file_url"]


def prepare(model, key, turns, params):  # noqa: ARG001
    """Upload the newest turn's attachments to fal and return them as URLs.

    fal reads an image, a video or an audio clip from a URL, so every file
    passed with -a is uploaded once, here, and the block keeps its mime type
    and its order for build() to place.
    """
    for turn in reversed(turns):
        if turn.get("role") != "user":
            continue
        for block in turn["blocks"]:
            if "data" in block:
                block["url"] = upload(
                    key, block["data"], block["mime_type"],
                    os.path.basename(block.get("path") or "attachment"))
        break
    return turns


def build(model, turns, system, params):  # noqa: ARG001
    """The fal arguments for one call: the prompt, the options, the files.

    The newest turn's text is the prompt and each of its attachments lands
    at the argument -a named, since a fal model reads its input under an
    argument name of its own and only the caller knows which one.  A NAME
    given twice collects its URLs into a list, which is how a model that
    takes many images of one kind is asked for them.  A model's own
    defaults -- veo's duration and resolution, kling's duration -- are its
    entry's `options` key in the config, so they reach `params` with -o on
    top of them, exactly as every other adaptor's defaults do.

    >>> build({"model_name": "fal-ai/nano-banana"}, [], None, {})
    {}
    """
    prompt, files = newest(turns)
    args = dict(params)
    if prompt:
        args["prompt"] = prompt
    for block in files:
        name, url = block["name"], block["url"]
        if isinstance(args.get(name), list):
            args[name].append(url)
        elif name in args:
            args[name] = [args[name], url]
        else:
            args[name] = url
    return args


def asset_url(result):
    """The one asset in a fal result, wherever this model named it.

    A still-image model answers with {"images": [...]}, a video model with
    {"video": {...}}, an audio model with {"audio": {...}}, and a model that
    edits its input may use any of them; the URL is looked for under all of
    them rather than assumed.
    """
    for key in ("images", "video", "audio", "videos", "audios"):
        value = result.get(key)
        if isinstance(value, list) and value and value[0].get("url"):
            return value[0]["url"]
        if isinstance(value, dict) and value.get("url"):
            return value["url"]
    raise DicError(f"fal: no asset in {json.dumps(result)[:200]}")


def call(model, key, body, line, stamps):
    """Submit the job, poll it, and yield its result and its bytes.

    fal is a queue: POST returns an id, GET on the id's status says whether
    the job is done, and GET on the id says what it made.  The asset the
    result names is fetched here, so the bytes reach the client as a blob
    and land in the file --path chose.  A poll repaints `line` rather than
    printing a line of its own.
    """
    base = f"{model['api_base'].rstrip('/')}/{model['model_name']}"
    headers = {"Content-Type": "application/json", **auth(key),
               **(model.get("headers") or {})}
    stamps["t_request"] = time.time_ns()
    create = fetch(urllib.request.Request(  # noqa: S310
        base, data=json.dumps(body).encode(), headers=headers))
    stamps["t_headers"] = time.time_ns()
    job = create.get("request_id") or create.get("id")
    if not job:
        raise DicError(f"fal: {model['model_name']}: no request id in {create}")
    start = time.time()
    while True:
        status = fetch(urllib.request.Request(  # noqa: S310
            f"{base}/requests/{job}/status", headers=headers))
        state = status.get("status")
        if state in ("COMPLETED", "OK"):
            break
        if state in ("FAILED", "ERROR"):
            raise DicError(f"fal: {model['model_name']}: {state}: {status}")
        if time.time() - start > POLL_S:
            raise DicError(f"fal: {model['model_name']}: still {state}"
                           f" after {POLL_S}s")
        # the queue wait is part of the time-to-first-byte, so it annotates
        # the ttft clock rather than replacing it: one line, one number
        line.extra = f"fal: {state}"
        time.sleep(POLL_MS / 1000)
    result = fetch(urllib.request.Request(  # noqa: S310
        f"{base}/requests/{job}", headers=headers))
    stamps["t_first"] = time.time_ns()
    yield {"type": "result", "result": result}
    url = http_url(asset_url(result))
    try:
        with urllib.request.urlopen(url) as response:  # noqa: S310
            data = response.read()
    except urllib.error.HTTPError as e:
        raise DicError(f"fal: {e.code} fetching {url}") from None
    stamps["t_last"] = time.time_ns()
    yield {"type": "blob", "data": data}


def usage(result):
    """What this call produced, in the unit the model's price is quoted in.

    An image model makes one image and a video model makes one clip, whose
    price is per second; fal reports the clip's duration beside the file
    when it does, and a model with a fixed duration charges a flat rate for
    it, which is one clip.

    >>> usage({"images": [{"url": "u"}]})
    {'out': 1}
    >>> usage({"video": {"url": "u", "duration": 5}})
    {'out': 5.0}
    >>> usage({})
    {}
    """
    if isinstance(result.get("images"), list):
        return {"out": len(result["images"])}
    for key in ("video", "audio", "videos", "audios"):
        value = result.get(key)
        clip = value[0] if isinstance(value, list) and value else value
        if not isinstance(clip, dict):
            continue
        seconds = clip.get("duration")
        if isinstance(seconds, str):
            seconds = seconds.rstrip("s")
        try:
            return {"out": float(seconds)} if seconds else {"out": 1}
        except ValueError:
            return {"out": 1}
    return {}


def parse(event, acc):
    """Read the events call() yields: the result, then the asset's bytes.

    >>> acc = {}
    >>> parse({"type": "result", "result": {"images": [{"url": "u"}]}}, acc)
    ('', '')
    >>> acc["usage"]
    {'out': 1}
    >>> parse({"type": "blob", "data": b"x"}, acc)
    (b'x', 'blob')
    """
    kind = event.get("type")
    if kind == "blob":
        return event["data"], "blob"
    if kind == "result":
        acc["result"] = event["result"]
        acc["usage"] = usage(event["result"])
        return "", ""
    return "", ""


def finish(acc):
    """The fal result, stored verbatim: it is what a later turn would replay.

    >>> finish({"result": {"images": [{"url": "u"}]}})
    {'images': [{'url': 'u'}]}
    >>> finish({}) is None
    True
    """
    return acc.get("result")


def model_params_url(model):
    """The one GET that answers this model's params, before it is made.

    A caller that wants to name the request -- for a -v line, or for
    the host a pv clock is waiting on -- asks here rather than building
    the same URL a second way and drifting from it.

    >>> model_params_url({"model_name": "fal-ai/nano-banana"})
    'https://api.fal.ai/v1/models?endpoint_id=fal-ai/nano-banana&expand=openapi-3.0'
    """
    base = ((model.get("sync") or {}).get("catalog_base")
            or "https://api.fal.ai/v1").rstrip("/")
    return (f"{base}/models?endpoint_id={model['model_name']}"
            "&expand=openapi-3.0")


def catalog_once(url, headers):
    """One GET of the catalog: (status, headers, body) and nothing raised.

    Everything the wire said comes back rather than being parsed away,
    because a reader debugging this call needs the status, the CDN's own
    cache header and the bytes; a response whose body is not the JSON dic
    expected is exactly the one where a parsed form says nothing.  Only a
    connection that never opened raises here, because a 429 and a 404 are
    the same kind of fact to this function and two different ones to a
    retry.
    """
    request = urllib.request.Request(http_url(url), headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request) as response:  # noqa: S310
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers or {}, e.read()
    except urllib.error.URLError as e:
        raise DicError(f"fal: {url}: {e}") from None


def brief(body):
    """A response body as one short line: the text a message may quote.

    Decoded for a terminal and truncated, because a refusal's body is
    evidence and not something dic interprets: an HTML error page, an empty
    string and a JSON envelope dic did not anticipate each say what happened
    once they are read, and none of them says anything after being replaced
    by its parse.

    >>> brief(b'{"error": "no"}')
    '{"error": "no"}'
    >>> brief(b"")
    '<empty body>'
    """
    text = body.decode("utf-8", "replace").strip()
    return text[:BODY_SHOWN] if text else "<empty body>"


def refusal(url, status, headers, body):
    """One failed catalog request as a DicError carrying what was seen.

    A message built from parsed fields alone leaves a reader with nothing
    to go on: the status, the CDN's own cache header and the bytes are what
    say whether the answer was a refusal, a wrong id, or a document of
    another shape, and parsing has already thrown all three away by the
    time a message is written.

    >>> refusal("u", 429, {"x-vercel-cache": "BYPASS"}, b"{}").args[0]
    'fal: GET u -> 429 BYPASS\\n  {}'
    """
    cache = headers.get("x-vercel-cache") or headers.get("server") or ""
    return DicError(f"fal: GET {url} -> {status}"
                    f"{' ' + cache if cache else ''}"
                    f"\n  {brief(body)}")


def catalog(model, key, env=None):  # noqa: ARG001
    """This model's catalog entry, the transient refusal retried.

    Two shapes have one cause.  A refused expansion arrives as a 429, and
    it also arrives as a 200 whose `openapi` value holds the marker
    `expansion_failed` -- nested under `error` in one response and at the
    top level in the next.  So the marker is searched for in the whole
    document rather than read off one field path, because fal reshapes its
    own envelope and a path that stops matching silently stops retrying.

    No credential is sent: see the note beside EXPANSION_FAILED.  Every
    attempt is reported at -v with the status and how much of the budget it
    used, and every failure carries the status, the CDN's own cache header
    and the body, so a reader has the wire's evidence and not a slice of
    parsed JSON.

    Anything that is not the refusal is raised at once, because asking
    again cannot change it: a 404 is not a rate limit, and a body that is
    not JSON at all is a different problem with a different fix.  The last
    refusal is what a caller sees when the retries run out.

    Returns the model's own entry, or None when the catalog lists no such
    model.
    """
    url = model_params_url(model)
    headers = {"accept": "application/json", "user-agent": USER_AGENT}
    verbose = int((env or {}).get("DIC_VERBOSITY") or 0)
    failure = None
    for attempt in range(1, CATALOG_TRIES + 1):
        if attempt > 1:
            time.sleep(CATALOG_BACKOFF_S * 2 ** (attempt - 2))
        status, reply, body = catalog_once(url, headers)
        cache = reply.get("x-vercel-cache") or reply.get("server") or ""
        report(verbose, 1,
               f"params: GET {url} -> {status}"
               f" (attempt {attempt}/{CATALOG_TRIES}"
               f"{', ' + cache if cache else ''})", env=env)
        if status == 200:
            try:
                parsed = json.loads(body)
            except ValueError:
                raise refusal(url, status, reply, body) from None
            models = parsed.get("models") if isinstance(parsed, dict) else None
            if models is None:
                raise refusal(url, status, reply, body)
            if not models:
                raise DicError(f"fal: no entry for {model['model_name']}")
            doc = models[0].get("openapi")
            if (not isinstance(doc, dict)
                    or EXPANSION_FAILED not in json.dumps(doc)):
                return models[0]
            # the refusal fal serves as a 200: as transient as a 429
        elif status != 429 and not 500 <= status < 600:
            raise refusal(url, status, reply, body)
        failure = refusal(url, status, reply, body)
    raise failure


def metadata(entry):
    """The catalog's prose about this model, under reserved `#` keys.

    A page is about a model and not only about its options, and fal
    publishes a display name, a category and a paragraph about what the
    endpoint does.  They are not request fields, so they travel under names
    no field may take -- `#` is what models.json already uses for a key
    that is not an entry -- and the page reads them out while params_text
    skips them.

    >>> metadata({"metadata": {"display_name": "Nano Banana",
    ...                        "category": "image",
    ...                        "description": "edits a picture"}})
    {'#display_name': 'Nano Banana', '#category': 'image', '#description': 'edits a picture'}
    >>> metadata({})
    {}
    """
    meta = entry.get("metadata") or {}
    out = {}
    for key, name in (("display_name", "#display_name"),
                      ("category", "#category"),
                      ("description", "#description")):
        if meta.get(key):
            out[name] = meta[key]
    return out


def model_params(model, key, env):
    """Fetch this fal endpoint's OpenAPI schema and extract its inputs.

    One GET on the catalog API, filtered to one endpoint, with
    ``expand=openapi-3.0``.  The document fal returns has the
    endpoint's full request schema; extract_params turns it into dic's
    own shape.  Called by --help when its answer is not cached.

    A response that names no endpoint, a document that carries an error
    object rather than a specification, and a specification with no
    input properties are each a DicError naming what was seen.  The
    previous version returned an empty table for all three, so a --help
    that printed no params could not tell a model with none from a
    request that had failed.  The request itself is made by catalog(),
    which sends no credential, retries the refusal the CDN serves as a
    200, and reports each attempt -- its status, its number and the CDN's
    own cache header -- at -v.
    """
    entry = catalog(model, key, env)
    if not entry:
        raise DicError(f"fal: no entry for {model['model_name']}")
    doc = entry.get("openapi")
    if not doc:
        raise DicError(f"fal: {model['model_name']}: no openapi field"
                       " in the catalog response")
    if "paths" not in doc:
        # a document without paths is not a specification, and fal says the
        # field may hold an error object instead: its own code and message
        # when it has them, and the document itself when it does not
        error = doc.get("error") if isinstance(doc.get("error"), dict) else {}
        said = ": ".join(part for part in (
            doc.get("code") or error.get("code"),
            doc.get("message") or error.get("message")) if part)
        raise DicError(f"fal: {model['model_name']}:"
                       f" {said or json.dumps(doc)[:BODY_SHOWN]}")
    params = extract_params(doc)
    if not params:
        raise DicError(f"fal: {model['model_name']}: the openapi document"
                       " names no input properties")
    return {**metadata(entry), **params}


def extract_params(openapi):
    """The input properties of one fal OpenAPI document, as dic params.

    The document is OpenAPI 3.0: the endpoint's own path carries its
    POST, whose requestBody is the input, and the properties may be
    inline or a $ref into the document's components.  Each property
    becomes one entry with its type, its enum values if any, its
    default, and a required marker, which is what --help sorts on.

    >>> doc = {"paths": {"/x": {"post": {"requestBody": {"content": {
    ...     "application/json": {"schema": {"type": "object",
    ...         "required": ["a"],
    ...         "properties": {"a": {"type": "string"},
    ...                        "b": {"type": "integer", "default": 1}}}}}}}}}}
    >>> extract_params(doc)
    {'a': {'type': 'string', 'required': True}, 'b': {'type': 'integer', 'default': 1}}
    """
    components = (openapi.get("components") or {}).get("schemas") or {}
    schema = request_schema(openapi)
    if not schema:
        return {}
    return schema_params(schema, components)


def request_schema(openapi):
    """The requestBody schema of the document's first POST path.

    >>> request_schema({"paths": {"/x": {"post": {"requestBody": {"content":
    ...     {"application/json": {"schema": {"type": "object"}}}}}}}})
    {'type': 'object'}
    >>> request_schema({}) is None
    True
    """
    for path in (openapi.get("paths") or {}).values():
        for method in ("post", "put"):
            operation = path.get(method)
            if not operation:
                continue
            content = (operation.get("requestBody") or {}).get("content") or {}
            for piece in content.values():
                if piece.get("schema"):
                    return piece["schema"]
    return None


def schema_params(schema, components):
    """Flatten one schema and read its properties into dic's params shape.

    >>> schema_params({"type": "object",
    ...                "required": ["a"],
    ...                "properties": {"a": {"type": "string"},
    ...                               "b": {"enum": ["x", "y"], "default": "x"}}},
    ...               {})
    {'a': {'type': 'string', 'required': True}, 'b': {'type': 'enum', 'default': 'x', 'values': ['x', 'y']}}
    """
    schema = flatten(deref(schema, components), components)
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    out = {}
    for name, prop in properties.items():
        prop = flatten(deref(prop, components), components)
        entry = {"type": type_name(prop)}
        if name in required:
            entry["required"] = True
        if prop.get("default") is not None:
            entry["default"] = prop["default"]
        if prop.get("enum"):
            entry["values"] = list(prop["enum"])
        if prop.get("description"):
            entry["description"] = prop["description"]
        if prop.get("maxItems") is not None:
            entry["max"] = prop["maxItems"]
        out[name] = entry
    return out


def deref(schema, components, seen=None):
    """Resolve a chain of $refs; a cycle resolves to {}.

    >>> deref({"$ref": "#/components/schemas/In"},
    ...       {"In": {"type": "object"}})
    {'type': 'object'}
    >>> deref({"type": "string"}, {})
    {'type': 'string'}
    >>> deref({"$ref": "#/components/schemas/A"},
    ...       {"A": {"$ref": "#/components/schemas/A"}})
    {}
    """
    seen = seen if seen is not None else set()
    while isinstance(schema, dict) and "$ref" in schema:
        ref = schema["$ref"]
        if ref in seen:
            return {}
        seen.add(ref)
        schema = components.get(ref.rsplit("/", 1)[-1]) or {}
    return schema


def flatten(schema, components):
    """One schema: allOf merged, anyOf/oneOf resolved to the first non-null.

    >>> flatten({"allOf": [{"properties": {"a": {"type": "string"}}},
    ...                    {"required": ["a"]}]}, {})
    {'properties': {'a': {'type': 'string'}}, 'required': ['a']}
    >>> flatten({"anyOf": [{"type": "null"}, {"type": "string"}]}, {})
    {'type': 'string'}
    """
    schema = deref(schema, components)
    if not isinstance(schema, dict):
        return {}
    if "allOf" in schema:
        other, properties, required = {}, {}, set()
        for piece in schema["allOf"]:
            piece = flatten(piece, components)
            properties.update(piece.get("properties") or {})
            required.update(piece.get("required") or [])
            for key, value in piece.items():
                if key not in ("properties", "required"):
                    other[key] = value
        if properties:
            other["properties"] = properties
        if required:
            other["required"] = sorted(required)
        return other
    for key in ("anyOf", "oneOf"):
        if key in schema:
            for variant in schema[key]:
                flat = flatten(variant, components)
                if flat.get("type") != "null":
                    return flat
    return schema


def type_name(schema):
    """One schema's type as dic prints it: 'enum' for a bare enum.

    >>> type_name({"type": "integer"}), type_name({"enum": ["a"]})
    ('integer', 'enum')
    >>> type_name({})
    'string'
    """
    kind = schema.get("type")
    if kind in ("string", "integer", "number", "boolean", "array", "object"):
        return "enum" if kind == "string" and schema.get("enum") else kind
    if schema.get("enum"):
        return "enum"
    return "string"
