"""Anthropic's native /messages protocol.

Preferred over Anthropic's OpenAI-compatible shim, which silently ignores
unsupported fields and does not expose thinking or tool-use blocks faithfully.
The assistant turn is reassembled block by block from the stream so that
thinking blocks and their signatures can be replayed exactly.

Exports PATH, auth, build, parse, finish; see adaptors/openai_chat.py.
"""
import base64, json

from dic.store import tokens

PATH = "/messages"


def auth(key):
    """>>> auth("k")
    {'x-api-key': 'k', 'anthropic-version': '2023-06-01'}
    """
    return {"x-api-key": key, "anthropic-version": "2023-06-01"}


def cache_usage(usage, ttl=None):
    """The cache counts of one message_start, as disjoint usage names.

    The write side is priced by TTL, so each TTL is a name of its own; a
    response that reports the write side as one total is billed at the TTL
    the call asked for, because that is the TTL the provider charged.
    `in.cache_read` and `in` describe different tokens, so both stay.

    >>> cache_usage({"cache_read_input_tokens": 900,
    ...              "cache_creation_input_tokens": 100})
    {'in.cache_read': 900, 'in.cache_write.5m': 100}
    >>> cache_usage({"cache_creation_input_tokens": 100}, "1h")
    {'in.cache_write.1h': 100}
    >>> cache_usage({"cache_creation": {"ephemeral_1h_input_tokens": 100}})
    {'in.cache_write.1h': 100}
    >>> cache_usage({"input_tokens": 5})
    {}
    """
    out = {}
    if usage.get("cache_read_input_tokens"):
        out["in.cache_read"] = usage["cache_read_input_tokens"]
    split = usage.get("cache_creation") or {}
    five = split.get("ephemeral_5m_input_tokens")
    hour = split.get("ephemeral_1h_input_tokens")
    if five or hour:
        if five:
            out["in.cache_write.5m"] = five
        if hour:
            out["in.cache_write.1h"] = hour
    elif usage.get("cache_creation_input_tokens"):
        out[f"in.cache_write.{ttl or '5m'}"] = usage["cache_creation_input_tokens"]
    return out


def tool_schema(tools):
    """The tools in this protocol's shape: name, description, input_schema.

    >>> tool_schema([{"name": "ls", "description": "List.", "parameters": {}}])
    [{'name': 'ls', 'description': 'List.', 'input_schema': {}}]
    """
    return [{"name": t["name"], "description": t["description"],
             "input_schema": t["parameters"]} for t in tools]


# Reasoning is opaque and takes no cache_control, so a breakpoint is never
# placed on one: the block it lands on is the last that can carry it.
NO_BREAKPOINT = ("thinking", "redacted_thinking")


def cache_control(model):
    """The cache_control block this call places: None when caching is off.

    Off is every model that rates no cache write, which is the only way dic
    says a provider's cache cannot be billed for.  The 5-minute TTL is
    Anthropic's own default and is left implicit; a longer one has to be
    named, because the write it buys costs more.

    >>> cache_control({}) is None, cache_control({"cache": "5m"})
    (True, {'type': 'ephemeral'})
    >>> cache_control({"cache": "1h"})
    {'type': 'ephemeral', 'ttl': '1h'}
    """
    ttl = model.get("cache")
    if not ttl:
        return None
    if ttl == "5m":
        return {"type": "ephemeral"}
    return {"type": "ephemeral", "ttl": ttl}


def mark(blocks, cache):
    """A copy of one content list with the breakpoint on its last usable block.

    >>> mark([{"type": "text", "text": "hi"}], {"type": "ephemeral"})[-1]
    {'type': 'text', 'text': 'hi', 'cache_control': {'type': 'ephemeral'}}
    >>> mark([{"type": "thinking", "thinking": "hmm"}], {"type": "ephemeral"})
    [{'type': 'thinking', 'thinking': 'hmm'}]
    """
    out = list(blocks)
    for i in range(len(out) - 1, -1, -1):
        if out[i].get("type") not in NO_BREAKPOINT:
            out[i] = dict(out[i], cache_control=cache)
            break
    return out


def mark_prefixes(body, cache):
    """Mark the system prompt and the last turn: the prefixes a later call reuses.

    The system prompt is the head of every request, and marking the last turn
    of the conversation is what makes the *next* turn a cache read of
    everything above it, which is the whole of what -c and --mid reuse.  Only
    the content lists are copied: what gets stored in response_raw is what the
    provider sent, and a breakpoint belongs to one request.

    >>> body = {"system": "be brief",
    ...         "messages": [{"role": "user", "content": "one"},
    ...                      {"role": "assistant", "content": "hi"},
    ...                      {"role": "user", "content": "two"}]}
    >>> mark_prefixes(body, {"type": "ephemeral"})
    >>> body["system"]
    [{'type': 'text', 'text': 'be brief', 'cache_control': {'type': 'ephemeral'}}]
    >>> body["messages"][-1]["content"][-1]["cache_control"]
    {'type': 'ephemeral'}
    >>> body["messages"][0]["content"]     # one breakpoint, one turn
    'one'
    """
    system = body.get("system")
    if isinstance(system, str) and system.strip():
        body["system"] = [{"type": "text", "text": system,
                           "cache_control": cache}]
    elif isinstance(system, list) and system:
        body["system"] = mark(system, cache)
    for message in reversed(body.get("messages") or []):
        content = message.get("content")
        if isinstance(content, str) and content:
            message["content"] = [{"type": "text", "text": content,
                                   "cache_control": cache}]
            return
        if isinstance(content, list) and content:
            message["content"] = mark(content, cache)
            return


def build(model, turns, system, params):
    """IR turns to a streaming messages body; system is a top-level parameter.

    max_tokens is required by the API, so a default is supplied and params
    (merged last) can override it like anything else.

    A turn carrying "raw" is replayed verbatim, and an image becomes a base64
    source block.

    >>> b = build({"model_name": "m"},
    ...           [{"role": "user", "blocks": [{"type": "text", "text": "hi"}]}],
    ...           "sys", {"max_tokens": 10})
    >>> b["messages"]
    [{'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}]}]
    >>> b["system"], b["max_tokens"]
    ('sys', 10)
    >>> b = build({"model_name": "m"},
    ...           [{"role": "assistant", "blocks": [], "raw": [{"type": "thinking"}]},
    ...            {"role": "user", "blocks": [
    ...                {"type": "image", "mime_type": "image/png", "data": b"hi"}]}],
    ...           None, {})
    >>> b["messages"][0]
    {'role': 'assistant', 'content': [{'type': 'thinking'}]}
    >>> b["messages"][1]["content"][0]["source"]
    {'type': 'base64', 'media_type': 'image/png', 'data': 'aGk='}
    """
    msgs = []
    for turn in turns:
        if "raw" in turn:
            msgs.append({"role": "assistant", "content": turn["raw"]})
            continue
        content = []
        for block in turn["blocks"]:
            if block["type"] == "text":
                content.append({"type": "text", "text": block["text"]})
            elif block["type"] == "image":
                content.append({"type": "image", "source": {
                    "type": "base64", "media_type": block["mime_type"],
                    "data": base64.b64encode(block["data"]).decode()}})
            elif block["type"] == "tool_result":
                content.append({"type": "tool_result",
                                "tool_use_id": block["id"],
                                "content": block["content"]})
        msgs.append({"role": turn["role"], "content": content})
    body = {"model": model["model_name"], "messages": msgs, "stream": True,
            "max_tokens": 4096}
    if system:
        body["system"] = system
    body.update(params)
    cache = cache_control(model)
    if cache:
        mark_prefixes(body, cache)
    return body


def parse(event, acc):
    """Rebuild acc["raw"] from the event stream, yielding (text, kind) to print.

    kind is "thinking" for a reasoning delta and "" for anything else, so the
    caller can paint thinking differently from the answer.

    Unknown block types accumulate as-is, so a feature dic has never heard of
    still round-trips into the database and back to the API.

    >>> acc = {}
    >>> parse({"type": "message_start",
    ...        "message": {"usage": {"input_tokens": 5, "output_tokens": 0}}}, acc)
    ('', '')
    >>> parse({"type": "content_block_start",
    ...        "content_block": {"type": "text", "text": ""}}, acc)
    ('', '')
    >>> parse({"type": "content_block_delta",
    ...        "delta": {"type": "text_delta", "text": "hi"}}, acc)
    ('hi', '')
    >>> parse({"type": "message_delta", "usage": {"output_tokens": 1}}, acc)
    ('', '')
    >>> acc["usage"], acc["raw"]
    ({'in': 5, 'out': 1}, [{'type': 'text', 'text': 'hi'}])
    >>> acc = {}
    >>> parse({"type": "message_start", "message": {"usage":
    ...        {"input_tokens": 10, "cache_read_input_tokens": 900,
    ...         "cache_creation_input_tokens": 90, "output_tokens": 0}}}, acc)
    ('', '')
    >>> acc["usage"]
    {'in': 10, 'in.cache_read': 900, 'in.cache_write.5m': 90}
    >>> acc = {}
    >>> parse({"type": "content_block_start",
    ...        "content_block": {"type": "thinking", "thinking": ""}}, acc)
    ('', '')
    >>> parse({"type": "content_block_delta",
    ...        "delta": {"type": "thinking_delta", "thinking": "hmm"}}, acc)
    ('hmm', 'thinking')
    >>> parse({"type": "content_block_delta",
    ...        "delta": {"type": "signature_delta", "signature": "sig"}}, acc)
    ('', '')
    >>> acc["raw"]
    [{'type': 'thinking', 'thinking': 'hmm', 'signature': 'sig'}]
    >>> acc = {}
    >>> parse({"type": "content_block_start",
    ...        "content_block": {"type": "tool_use", "name": "f"}}, acc)
    ('', '')
    >>> parse({"type": "content_block_delta",
    ...        "delta": {"type": "input_json_delta", "partial_json": '{"a": 1}'}}, acc)
    ('', '')
    >>> parse({"type": "content_block_stop"}, acc)
    ('', '')
    >>> acc["raw"]
    [{'type': 'tool_use', 'name': 'f', 'input': {'a': 1}}]
    """
    kind = event.get("type")
    blocks = acc.setdefault("raw", [])
    if kind == "error":
        # an error frame after a 200: not a reply, and never a conversation
        error = event.get("error") or {}
        acc["error"] = (f"{error.get('type') or 'error'}: "
                        f"{error.get('message') or 'stream failed'}")
        return "", ""
    if kind == "message_start":
        # every count is reported as a quantity of its own rather than as a
        # total with subsets inside it, so all of them are taken as they come
        usage = (event.get("message") or {}).get("usage") or {}
        counts = {"in": usage.get("input_tokens"),
                  "out": usage.get("output_tokens")}
        counts.update(cache_usage(usage, acc.get("cache")))
        acc["usage"] = tokens(**counts)
    elif kind == "content_block_start":
        blocks.append(dict(event.get("content_block") or {}))
    elif kind == "content_block_delta" and blocks:
        block, delta = blocks[-1], event.get("delta") or {}
        for field, delta_type in (("text", "text_delta"),
                                  ("thinking", "thinking_delta"),
                                  ("signature", "signature_delta")):
            if delta.get("type") == delta_type:
                block[field] = block.get(field, "") + delta[field]
                if field == "signature":
                    return "", ""          # opaque; replayed, never printed
                return delta[field], "thinking" if field == "thinking" else ""
        if delta.get("type") == "input_json_delta":
            block["_json"] = block.get("_json", "") + delta["partial_json"]
    elif kind == "content_block_stop" and blocks:
        block = blocks[-1]
        if "_json" in block:
            partial = block.pop("_json")   # popped once: json.loads may fail
            try:
                block["input"] = json.loads(partial or "{}")
            except ValueError:
                block["input"] = {}        # a tool call cut off mid-arguments
    elif kind == "message_delta":
        # the output count is cumulative, and a thinking model's thinking is
        # part of it, so it is the whole of the answer rather than a subset
        usage = event.get("usage") or {}
        if usage.get("output_tokens") is not None:
            acc.setdefault("usage", {})["out"] = usage["output_tokens"]
        acc["stop"] = ((event.get("delta") or {}).get("stop_reason")
                       or acc.get("stop"))
    return "", ""


def calls(acc):
    """The tool_use blocks of this round, as {id, name, arguments}.

    >>> acc = {"raw": [{"type": "tool_use", "id": "t1", "name": "ls",
    ...                 "input": {"path": "."}}]}
    >>> calls(acc)
    [{'id': 't1', 'name': 'ls', 'arguments': {'path': '.'}}]
    """
    return [{"id": block.get("id"), "name": block.get("name"),
             "arguments": block.get("input") or {}}
            for block in acc.get("raw", []) if block.get("type") == "tool_use"]


def finish(acc):
    """The reassembled content blocks, ready to replay verbatim.

    A block whose stream ended before its content_block_stop still carries the
    scratch key the argument deltas accumulated in: it is dic's own, never the
    provider's, so it is dropped rather than replayed.

    >>> finish({"raw": [{"type": "thinking", "signature": "s"}]})
    [{'type': 'thinking', 'signature': 's'}]
    >>> finish({"raw": [{"type": "tool_use", "_json": '{"a"'}]})
    [{'type': 'tool_use', 'input': {}}]
    >>> finish({})
    []
    """
    for block in acc.get("raw", []):
        if "_json" in block:
            block.pop("_json")
            block.setdefault("input", {})
    return acc.get("raw", [])
