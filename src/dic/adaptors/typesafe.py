"""The SystemOne wire protocol: one POST, one JSON document, no stream.

typesafe is not a chat model and keeps no conversation.  One request
carries a state and a set of questions, and one response carries the
answers; there is nothing to stream and nothing to poll, so this adaptor
has no events() path.  build() writes the request, output_format() puts
the compiled systemone schema in as `questions`, call() makes the one
POST, and parse() reads the answers out of it as the text the client
prints and validates.

Exports PATH, auth, build, call, parse, finish; see adaptors/openai_chat.py.
"""
import base64, json, time, urllib.error, urllib.request

from dic.store import http_url, tokens
from dic.tty import DicError

PATH = "/v1/systemone"

# typesafe's request shape comes from --schema; the flattening of that
# schema into `questions` is the protocol's whole surface, and what a
# caller passes with -o is whatever the questions dict accepts.
PARAMS = {}

# typesafe shapes its own answers, so a schema here is asked rather than
# dictated, and the row records that it was.
ENFORCES_SCHEMA = False

# The format names the API accepts, by mime type.  A file whose mime is
# neither is an error naming what the API accepts.
FORMATS = {"image/png": "png", "image/jpeg": "jpeg"}


def auth(key):
    """>>> auth("k")
    {'Authorization': 'Bearer k'}
    """
    return {"Authorization": "Bearer " + key}


def newest(turns):
    """(prompt, files) of the newest user turn: what one call is made from."""
    for turn in reversed(turns):
        if turn.get("role") != "user":
            continue
        prompt = "\n".join(b["text"] for b in turn["blocks"]
                           if b["type"] == "text")
        return prompt.strip(), [b for b in turn["blocks"] if b["type"] != "text"]
    return "", []


def build(model, turns, system, params):  # noqa: ARG001
    """The request fields: the model, the state, and any assets.

    state is the content the questions refer to, which is dic's prompt.
    Each attachment lands under the placeholder -a named, with the format
    the API accepts for its mime type; the questions themselves arrive
    from output_format(), because only the schema compilation knows them.

    >>> build({"model_name": "m"}, [{"role": "user", "blocks": [
    ...     {"type": "text", "text": "hi"}]}], None, {})["state"]
    'hi'
    """
    prompt, files = newest(turns)
    body = {"model": model["model_name"], "state": prompt}
    assets = {}
    for block in files:
        fmt = FORMATS.get(block["mime_type"])
        if not fmt:
            raise DicError(f"typesafe: {block['mime_type']}: the API accepts"
                           " image/png and image/jpeg")
        assets[block["name"]] = {
            "type": "image", "format": fmt,
            "data_base64": base64.b64encode(block["data"]).decode()}
    if assets:
        body["assets"] = assets
    body.update(params)
    return body


def output_format(body, model, schema, mime):  # noqa: ARG001
    """Put the compiled systemone questions in the request.

    The schema this protocol is asked with is the questions dict a
    systemone shorthand compiles to; a jsonschema or simonw_llm dialect
    never reaches here, because --schema refuses one for this api_type.
    -o questions, the raw escape hatch beneath the flag, arrives here in
    the body already and is left alone.

    >>> output_format({}, {}, {"questions": {"a": {"type": "noul"}}},
    ...               "application/json")["questions"]
    {'a': {'type': 'noul'}}
    >>> output_format({"questions": "x"}, {}, None, "application/json")
    {'questions': 'x'}
    """
    if schema is None:
        return body
    body["questions"] = schema.get("questions") or {}
    return body


def fetch(request):
    """urlopen a request and decode its JSON body; every failure is a DicError."""
    try:
        http_url(request.full_url)
        with urllib.request.urlopen(request) as response:  # noqa: S310
            return json.load(response)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace").strip()[:300]
        parts = (str(e.code), request.full_url, detail)
        raise DicError("typesafe: " + " ".join(p for p in parts if p)) from None
    except urllib.error.URLError as e:
        raise DicError(f"typesafe: {request.full_url}: {e}") from None


def call(model, key, body, line, stamps):  # noqa: ARG001
    """POST once and yield the whole response as one result event.

    There is nothing to stream and nothing to poll: one request carries
    every question and one response carries every answer.  The
    TypeSafe-Danger-Zone-* headers ride only when the request names what
    they gate, because a header set unconditionally says every call uses
    a feature it may not.
    """
    base = model["api_base"].rstrip("/")
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               **auth(key), **(model.get("headers") or {})}
    if body.get("assets"):
        headers["TypeSafe-Danger-Zone-Images"] = "true"
    if model.get("long_context"):
        headers["TypeSafe-Danger-Zone-Long-Context"] = "true"
    stamps["t_request"] = time.time_ns()
    result = fetch(urllib.request.Request(  # noqa: S310
        base + PATH, data=json.dumps(body).encode(), headers=headers))
    stamps["t_headers"] = stamps["t_first"] = stamps["t_last"] = time.time_ns()
    yield {"type": "result", "result": result}


def parse(event, acc):
    """Record the response and yield the answers as the text to print.

    The answers object is the response from the caller's point of view:
    usage, model and the request ride stderr as cost and metadata as they
    do for every other adaptor, and the answers go to stdout as JSON.

    >>> acc = {}
    >>> parse({"type": "result", "result": {"answers":
    ...     {"a": {"type": "noul", "noul": 0.9}},
    ...     "usage": {"input_tokens": 5, "output_tokens": 1}}}, acc)
    ('{"a": {"type": "noul", "noul": 0.9}}', '')
    >>> acc["usage"]
    {'in': 5, 'out': 1}
    """
    if event.get("type") != "result":
        return "", ""
    result = event["result"]
    acc["result"] = result
    usage = result.get("usage") or {}
    acc["usage"] = tokens(**{"in": usage.get("input_tokens"),
                             "out": usage.get("output_tokens")})
    return json.dumps(result.get("answers") or {}, ensure_ascii=False), ""


def finish(acc):
    """The whole server body, stored verbatim for dic show and any audit."""
    return acc.get("result")
