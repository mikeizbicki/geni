"""OpenAI's /images/generations and /images/edits.

One POST, one JSON answer: gpt-image-1 returns base64 PNGs rather than URLs,
so there is no stream to read and the whole reply arrives at once.  A turn
with attachments takes /images/edits instead, which is multipart -- the model
and its parameters are form fields and the reference images are file parts --
so call() chooses the endpoint and the encoding and yields the one blob the
client's Sink writes to --path.

Exports PATH, auth, build, call, parse, finish; see adaptors/openai_chat.py.
"""
import base64, json, time, urllib.error, urllib.request

from dic.store import http_url, multipart
from dic.tty import DicError

PATH = "/images/generations"


def auth(key):
    """>>> auth("k")
    {'Authorization': 'Bearer k'}
    """
    return {"Authorization": "Bearer " + key}


def newest(turns):
    """(prompt, files) of the newest user turn: what one image is made from."""
    for turn in reversed(turns):
        if turn.get("role") != "user":
            continue
        prompt = "\n".join(b["text"] for b in turn["blocks"]
                           if b["type"] == "text")
        return prompt.strip(), [b for b in turn["blocks"] if b["type"] != "text"]
    return "", []


def build(model, turns, system, params):  # noqa: ARG001
    """The request fields: the model, its prompt, its options, its images.

    size and quality and anything else the API knows are the config's
    options and -o, merged last so that -o wins over the defaults dic sets,
    as it does in every other adaptor: dic forwards them rather than
    validating them, so a new parameter works before it is written down.
    The reference images travel under a private key because they are bytes
    and not JSON, and call() is what turns them into the multipart parts
    /images/edits wants.

    >>> build({"model_name": "m"}, [], None, {"n": 4})["n"]
    4
    """
    prompt, files = newest(turns)
    body = {"model": model["model_name"], "n": 1}
    if prompt:
        body["prompt"] = prompt
    if files:
        body["_files"] = files
    body.update(params)         # -o is last, as in every other adaptor
    return body


def fetch(request):
    """urlopen a request and decode its JSON body; every failure is a DicError."""
    try:
        url = http_url(request.full_url)
        with urllib.request.urlopen(url) as response:  # noqa: S310
            return json.load(response)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace").strip()[:300]
        raise DicError(f"images: {e.code} {detail}") from None
    except urllib.error.URLError as e:
        raise DicError(f"images: {e}") from None


def call(model, key, body, line, stamps):  # noqa: ARG001
    """POST the request and yield the image bytes as one blob event.

    A turn with attachments goes to /images/edits as multipart and one
    without goes to /images/generations as JSON; both answer with base64
    PNGs in a `data` list, which is the one blob this adaptor yields.
    """
    files = body.pop("_files", None)
    base = model["api_base"].rstrip("/")
    headers = {**auth(key), **(model.get("headers") or {})}
    path = PATH
    if files:
        data, content_type = multipart(body, files)
        path = "/images/edits"
    else:
        data, content_type = json.dumps(body).encode(), "application/json"
    stamps["t_request"] = time.time_ns()
    result = fetch(urllib.request.Request(  # noqa: S310
        base + path, data=data, headers={**headers, "Content-Type": content_type}))
    stamps["t_headers"] = stamps["t_first"] = time.time_ns()
    yield {"type": "result", "result": result}
    for item in result.get("data") or []:
        stamps["t_last"] = time.time_ns()
        yield {"type": "blob", "data": base64.b64decode(item["b64_json"])}


def parse(event, acc):
    """Record the response and hand its base64 back as bytes.

    >>> parse({"type": "result", "result": {"data": [{"b64_json": "aGk="}]}}, {})
    ('', '')
    >>> parse({"type": "blob", "data": b"hi"}, {})
    (b'hi', 'blob')
    """
    kind = event.get("type")
    if kind == "blob":
        return event["data"], "blob"
    if kind == "result":
        acc["result"] = event["result"]
        return "", ""
    return "", ""


def finish(acc):
    """The response as stored raw, so a turn knows what it made."""
    return acc.get("result")
