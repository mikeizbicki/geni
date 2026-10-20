"""OpenAI's newer /responses protocol.

History is a flat list of input items rather than messages, and the assistant
turn is stored as the server's own output list so that reasoning items (which
are encrypted and meaningless to dic) can be replayed untouched.

Exports PATH, auth, build, parse, finish; see adaptors/openai_chat.py.
"""
from dic.store import data_url, openai_usage

PATH = "/responses"


def auth(key):
    """>>> auth("k")
    {'Authorization': 'Bearer k'}
    """
    return {"Authorization": "Bearer " + key}

# A schema here is dictated: `text.format` with `strict: true` is the
# provider's own conformance story, and the row records that it was.
ENFORCES_SCHEMA = True
def output_format(body, model, schema, mime):  # noqa: ARG001
    """Set the text.format this request carries, from --schema or a JSON mime.

    """Set the text.format this request carries, from --schema.

    The responses API names the constraint under `text.format` rather
    than at the top level, and takes the schema's name beside it; a text
    mime carries nothing and the body is left as build() made it.  A JSON
    mime without a schema never reaches here: the client refuses one,
    because no provider takes it.

    >>> output_format({}, {}, {"type": "object"}, "application/json")
    {'text': {'format': {'type': 'json_schema', 'name': 'response', 'schema': {'type': 'object'}, 'strict': True}}}
    >>> output_format({"model": "m"}, {}, None, "text/plain")
    {'model': 'm'}
    """
    if schema is not None:
        body["text"] = {"format": {"type": "json_schema", "name": "response",
                                   "schema": schema, "strict": True}}
    return body
    """IR turns to a streaming responses body; system is the top-level instructions.

    A turn carrying "raw" is replayed verbatim, and an image becomes a data: URL.

    >>> b = build({"model_name": "m"},
    ...           [{"role": "user", "blocks": [{"type": "text", "text": "hi"}]}],
    ...           "sys", {})
    >>> b["input"]
    [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'hi'}]}]
    >>> b["instructions"], b["stream"]
    ('sys', True)
    >>> b = build({"model_name": "m"},
    ...           [{"role": "assistant", "blocks": [], "raw": [{"type": "reasoning"}]},
    ...            {"role": "user", "blocks": [
    ...                {"type": "image", "mime_type": "image/png", "data": b"hi"}]}],
    ...           None, {})
    >>> b["input"][0]
    {'type': 'reasoning'}
    >>> b["input"][1]["content"][0]["image_url"]
    'data:image/png;base64,aGk='
    """
    items = []
    for turn in turns:
        if "raw" in turn:
            items.extend(turn["raw"])
            continue
        content = []
        for block in turn["blocks"]:
            if block["type"] == "text":
                content.append({"type": "input_text" if turn["role"] == "user"
                                        else "output_text",
                                "text": block["text"]})
            elif block["type"] == "image":
                content.append({"type": "input_image", "image_url": data_url(block)})
        items.append({"role": turn["role"], "content": content})
    body = {"model": model["model_name"], "input": items, "stream": True}
    if system:
        body["instructions"] = system
    body.update(params)
    return body


def parse(event, acc):
    """Text deltas print; the terminal event carries the output list and usage.

    Returns (text, kind), where reasoning-summary deltas are tagged "thinking"
    so the caller can paint them differently from the answer.

    >>> acc = {}
    >>> parse({"type": "response.output_text.delta", "delta": "hi"}, acc)
    ('hi', '')
    >>> parse({"type": "response.reasoning_summary_text.delta", "delta": "hmm"}, acc)
    ('hmm', 'thinking')
    >>> parse({"type": "response.completed", "response":
    ...        {"output": [{"type": "message"}],
    ...         "usage": {"input_tokens": 2, "output_tokens": 1}}}, acc)
    ('', '')
    >>> acc["usage"], acc["raw"]
    ({'in': 2, 'out': 1}, [{'type': 'message'}])
    >>> parse({"type": "response.completed",
    ...        "response": {"output": [], "service_tier": "flex",
    ...                     "usage": {"input_tokens": 1}}}, acc)
    ('', '')
    >>> acc["tier"]
    'flex'
    """
    kind = event.get("type")
    if kind in ("response.reasoning_summary_text.delta",
                "response.reasoning_text.delta"):
        return event.get("delta") or "", "thinking"
    if kind == "response.output_text.delta":
        return event.get("delta") or "", ""
    if kind == "response.failed":
        # a 200 whose stream carries the failure: record it, never replay it
        error = (event.get("response") or {}).get("error") or {}
        acc["error"] = (f"{error.get('code') or 'error'}: "
                        f"{error.get('message') or 'stream failed'}")
        return "", ""
    if kind in ("response.completed", "response.incomplete"):
        resp = event.get("response") or {}
        acc["raw"] = resp.get("output") or []
        acc["usage"] = openai_usage(resp.get("usage") or {})
        if resp.get("service_tier"):
            # what the response charged, so that a price is never read from
            # the request options that were sent
            acc["tier"] = resp["service_tier"]
        if kind == "response.incomplete":
            acc["stop"] = (resp.get("incomplete_details") or {}).get("reason")
    return "", ""


def finish(acc):
    """The server's own output items, stored verbatim.

    >>> finish({"raw": [{"type": "reasoning"}]})
    [{'type': 'reasoning'}]
    >>> finish({})
    []
    """
    return acc.get("raw", [])
