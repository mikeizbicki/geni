"""fal.ai's queue API: submit a job, poll it, fetch the asset.

fal's models are not chat models.  There is no conversation to rebuild and
no server-sent stream to read: a request is submitted to a queue and
returns an id, the id is polled until the job stops moving, and the job's
result names one asset URL that is then fetched as bytes.  So this adaptor
replaces the client's transport with call(), and its parse() reads the two
events call() yields -- a result, and a blob.

Each fal model also names its own arguments, and reads its images, video
and audio as URLs rather than bytes.  prepare() therefore uploads the
newest turn's attachments to fal's storage before the call, and MODELS
says which argument each one lands in.

Exports PATH, auth, prepare, build, call, parse, finish; see
adaptors/openai_chat.py for the contract.
"""
import json, os, time, urllib.error, urllib.request

from dic.store import http_url
from dic.tty import DicError

PATH = ""                              # the model name is the path; see call()
QUEUE = "https://queue.fal.run"
UPLOAD = "https://rest.alpha.fal.ai"   # attachments are stored here first
POLL_MS = 500
POLL_S = 900                           # ten minutes is a long video


def auth(key):
    """>>> auth("k")
    {'Authorization': 'Key k'}
    """
    return {"Authorization": "Key " + key}


# Every fal model names its own inputs, and this table is the one place that
# admits it.  "files" is the argument each attachment goes in, in the order
# they were given with -a; "image_urls" collects every remaining one because
# that argument is a list.  "extra" is what the model wants on every call,
# which -o still overrides; a model absent here is called with its prompt
# alone, since dic forwards a provider's fields rather than validating them.
MODELS = {
    "fal-ai/nano-banana/edit":     {"files": ["image_urls"]},
    "fal-ai/nano-banana-pro/edit": {"files": ["image_urls"]},
    "fal-ai/nano-banana-2/edit":   {"files": ["image_urls"]},
    "fal-ai/openai/gpt-image-2":   {"files": ["image_urls"]},

    "fal-ai/veo3.1/first-last-frame-to-video":
        {"files": ["first_frame_url", "last_frame_url"],
         "extra": {"duration": "8s", "aspect_ratio": "16:9",
                   "resolution": "720p", "generate_audio": False}},
    "fal-ai/veo3.1/fast/first-last-frame-to-video":
        {"files": ["first_frame_url", "last_frame_url"],
         "extra": {"duration": "8s", "aspect_ratio": "16:9",
                   "resolution": "720p", "generate_audio": False}},

    "fal-ai/kling-video/v3/pro/image-to-video":
        {"files": ["image_url"], "extra": {"duration": 5}},
    "fal-ai/kling-video/v3/standard/image-to-video":
        {"files": ["image_url"], "extra": {"duration": 5}},
    "fal-ai/kling-video/v2.6/pro/image-to-video":
        {"files": ["image_url"], "extra": {"duration": 5}},
    "fal-ai/kling-video/v2.6/standard/image-to-video":
        {"files": ["image_url"], "extra": {"duration": 5}},
    "fal-ai/kling-video/v2.5-turbo/pro/image-to-video":
        {"files": ["image_url"], "extra": {"duration": 5}},
    "fal-ai/kling-video/v2.5-turbo/standard/image-to-video":
        {"files": ["image_url"], "extra": {"duration": 5}},
    "fal-ai/bytedance/seedance-2.0/image-to-video":
        {"files": ["image_url"], "extra": {"duration": 5}},

    "fal-ai/kling-video/o1/image-to-video":
        {"files": ["start_image_url", "end_image_url"], "extra": {"duration": "5"}},
    "fal-ai/kling-video/o3/pro/image-to-video":
        {"files": ["image_url", "end_image_url"], "extra": {"duration": "5"}},
    "fal-ai/kling-video/o3/standard/image-to-video":
        {"files": ["image_url", "end_image_url"], "extra": {"duration": "5"}},

    "fal-ai/veed/fabric-1.0":
        {"files": ["image_url", "audio_url"], "extra": {"resolution": "480p"}},
    "fal-ai/creatify/aurora":
        {"files": ["image_url", "audio_url"], "extra": {"resolution": "480p"}},
    "fal-ai/bytedance/omnihuman/v1.5":
        {"files": ["image_url", "audio_url"], "extra": {"resolution": "1080p"}},
    "fal-ai/kling-video/v1/pro/ai-avatar":
        {"files": ["image_url", "audio_url"]},
    "fal-ai/kling-video/v1/standard/ai-avatar":
        {"files": ["image_url", "audio_url"]},

    "fal-ai/pixverse/sound-effects":   {"files": ["video_url"]},
    "fal-ai/mmaudio-v2":               {"files": ["video_url"]},
    "fal-ai/cassetteai/video-sound-effects-generator": {"files": ["video_url"]},
    "fal-ai/mirelo-ai/sfx-v1/video-to-video":          {"files": ["video_url"]},
}


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
    """The fal arguments for one call: the spec's shape, the options, the files.

    The newest turn's text is the prompt, its attachments land in the
    argument names the model's spec gives them, and -o and the config's
    options are merged on top of the spec's defaults so a caller can say
    `-o duration=10` where the model wants an integer and `-o prompt=...`
    where it wants none.

    >>> build({"model_name": "fal-ai/nano-banana"}, [], None, {})
    {}
    """
    prompt, files = newest(turns)
    spec = MODELS.get(model["model_name"], {})
    args = dict(spec.get("extra") or {})
    args.update(params)
    if prompt:
        args["prompt"] = prompt
    names = list(spec.get("files") or [])
    if names and names[-1] == "image_urls":
        args["image_urls"] = [b["url"] for b in files]
        return args
    if files and len(files) != len(names):
        raise DicError(f"{model['model_name']} takes {len(names)} attachment(s),"
                       f" got {len(files)}")
    for name, block in zip(names, files):
        args[name] = block["url"]
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
