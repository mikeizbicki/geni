"""OpenAI's /videos: create the job, poll it, download the file.

Video generation is asynchronous: POST creates the job and returns its id,
GET polls its status, and the content is a second GET that answers with the
video bytes themselves rather than JSON.  So this adaptor replaces the
client's transport the way fal's does, and its parse() reads the same three
events -- a result and a blob.

A reference image travels as multipart under input_reference, the field the
service names, because the create call takes its parameters as form fields
when it takes a file at all.

Exports PATH, auth, build, call, parse, finish; see adaptors/openai_chat.py.
"""
import json, time, urllib.error, urllib.request

from dic.store import http_url, multipart
from dic.tty import DicError, pv_clock

PATH = "/videos"
POLL_MS = 1000
POLL_S = 1800                          # sora queues can be half an hour deep


def auth(key):
    """>>> auth("k")
    {'Authorization': 'Bearer k'}
    """
    return {"Authorization": "Bearer " + key}


def newest(turns):
    """(prompt, files) of the newest user turn: the prompt and its still."""
    for turn in reversed(turns):
        if turn.get("role") != "user":
            continue
        prompt = "\n".join(b["text"] for b in turn["blocks"]
                           if b["type"] == "text")
        return prompt.strip(), [b for b in turn["blocks"] if b["type"] != "text"]
    return "", []


def build(model, turns, system, params):  # noqa: ARG001
    """The form fields of a create, with the input_reference kept apart.

    size and seconds come from the config's options and -o; the reference
    image is a private key because it is bytes, and call() is what puts it
    in the multipart part the service calls input_reference.
    """
    prompt, files = newest(turns)
    body = dict(params, model=model["model_name"])
    if prompt:
        body["prompt"] = prompt
    if files:
        body["_file"] = files[0]
    return body


def fetch(request, decode=True):
    """urlopen a request; JSON when asked, bytes when not, DicError on 4xx/5xx."""
    try:
        http_url(request.full_url)
        with urllib.request.urlopen(request) as response:  # noqa: S310
            return json.load(response) if decode else response.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace").strip()[:300]
        parts = (str(e.code), request.full_url, detail)
        raise DicError("videos: " + " ".join(p for p in parts if p)) from None
    except urllib.error.URLError as e:
        raise DicError(f"videos: {request.full_url}: {e}") from None


def call(model, key, body, line, stamps):
    """Create the job, poll it, and yield its content as one blob.

    A failed job is never a silent empty file: it is reported with the error
    the service gave, so the row is a failure and does not move the session's
    pointer into a conversation that produced nothing.
    """
    block = body.pop("_file", None)
    base = model["api_base"].rstrip("/")
    headers = {**auth(key), **(model.get("headers") or {})}
    if block:
        data, content_type = multipart(body, [block], field="input_reference")
    else:
        data, content_type = json.dumps(body).encode(), "application/json"
    stamps["t_request"] = time.time_ns()
    created = fetch(urllib.request.Request(  # noqa: S310
        base + PATH, data=data,
        headers={**headers, "Content-Type": content_type}))
    stamps["t_headers"] = time.time_ns()
    video = created.get("id")
    if not video:
        raise DicError(f"videos: no id in {created}")
    start = time.time()
    while True:
        job = fetch(urllib.request.Request(  # noqa: S310
            f"{base}{PATH}/{video}", headers=headers))
        status = job.get("status")
        if status in ("completed", "failed"):
            break
        if time.time() - start > POLL_S:
            raise DicError(f"videos: {model['model_name']} still {status}"
                           f" after {POLL_S}s")
        line.status(f"videos: {status} {pv_clock(line.elapsed())}")
        time.sleep(POLL_MS / 1000)
    if status == "failed":
        message = (job.get("error") or {}).get("message") or "generation failed"
        raise DicError(f"videos: {model['model_name']}: {message}")
    stamps["t_first"] = time.time_ns()
    yield {"type": "result", "result": job}
    data = fetch(urllib.request.Request(  # noqa: S310
        f"{base}{PATH}/{video}/content?variant=video", headers=headers),
        decode=False)
    stamps["t_last"] = time.time_ns()
    yield {"type": "blob", "data": data}


def parse(event, acc):
    """Record the job and hand the video bytes on.

    >>> parse({"type": "result", "result": {"status": "completed"}}, {})
    ('', '')
    >>> parse({"type": "blob", "data": b"x"}, {})[1]
    'blob'
    """
    kind = event.get("type")
    if kind == "blob":
        return event["data"], "blob"
    if kind == "result":
        acc["result"] = event["result"]
        seconds = event["result"].get("seconds")
        if seconds:
            acc["usage"] = {"out": float(seconds)}
        return "", ""
    return "", ""


def finish(acc):
    """The job as stored raw, so a later turn can see what it made and how long."""
    return acc.get("result")
