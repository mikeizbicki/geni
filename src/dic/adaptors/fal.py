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
from dic.tty import DicError

PATH = ""                              # the model name is the path; see call()
UPLOAD = "https://rest.alpha.fal.ai"   # attachments are stored here first
POLL_MS = 500
POLL_S = 900                           # ten minutes is a long video

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
        raise DicError("fal: " + " ".join(p for p in parts if p)) from None
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


def model_params(model, key, env):  # noqa: ARG001
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
    request that had failed.
    """
    body = fetch(urllib.request.Request(  # noqa: S310
        model_params_url(model),
        headers={**auth(key), "accept": "application/json"}))
    models = body.get("models") or []
    if not models:
        raise DicError(f"fal: no entry for {model['model_name']}")
    doc = models[0].get("openapi")
    if not doc:
        raise DicError(f"fal: {model['model_name']}: no openapi field"
                       " in the catalog response")
    if "paths" not in doc:
        # fal says the field may be an error object rather than a
        # specification, and a document without paths is not one
        raise DicError(f"fal: {model['model_name']}: {doc.get('error') or doc}")
    params = extract_params(doc)
    if not params:
        raise DicError(f"fal: {model['model_name']}: the openapi document"
                       " names no input properties")
    return params


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
