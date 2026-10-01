"""The OpenAI /chat/completions protocol: the default, and the lingua franca.

Also spoken by OpenRouter, Groq, Together, vLLM, llama.cpp and ollama.

Every adaptor module exports the same five names:
    PATH            the path appended to the model's api_base
    auth(key)       headers carrying the API key
    build(...)      IR turns -> request body
    parse(event, acc)  one SSE event -> (text, kind) to print, state in acc
    finish(acc)     acc -> the JSON stored in messages.response_raw
"""
import json

from dic.store import data_url, openai_usage
from dic.tty import DicError

PATH = "/chat/completions"


def auth(key):
    """>>> auth("k")
    {'Authorization': 'Bearer k'}
    """
    return {"Authorization": "Bearer " + key}


def tool_schema(tools):
    """The tools in this protocol's shape: one function declaration each.

    >>> tool_schema([{"name": "ls", "description": "List.", "parameters": {}}])
    [{'type': 'function', 'function': {'name': 'ls', 'description': 'List.', 'parameters': {}}}]
    """
    return [{"type": "function",
             "function": {"name": t["name"], "description": t["description"],
                          "parameters": t["parameters"]}}
            for t in tools]


def build(model, turns, system, params):
    """IR turns to a streaming chat-completions body; system is a leading message.

    A content list of pure text is collapsed to a plain string, which is what
    older and smaller servers actually accept.

    A turn carrying "raw" is replayed verbatim, and an image becomes a data:
    URL, so its turn stays a list of parts.

    >>> b = build({"model_name": "m"},
    ...           [{"role": "user", "blocks": [{"type": "text", "text": "hi"}]}],
    ...           "be brief", {"temperature": 0})
    >>> b["messages"]
    [{'role': 'system', 'content': 'be brief'}, {'role': 'user', 'content': 'hi'}]
    >>> b["model"], b["stream"], b["temperature"]
    ('m', True, 0)
    >>> b = build({"model_name": "m"},
    ...           [{"role": "assistant", "blocks": [], "raw": {"role": "assistant"}},
    ...            {"role": "user", "blocks": [
    ...                {"type": "image", "mime_type": "image/png", "data": b"hi"},
    ...                {"type": "text", "text": "what is this"}]}],
    ...           None, {})
    >>> b["messages"][0]
    {'role': 'assistant'}
    >>> b["messages"][1]["content"][0]["image_url"]["url"]
    'data:image/png;base64,aGk='
    """
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    for turn in turns:
        if "raw" in turn:
            msgs.append(turn["raw"])
            continue
        parts, results = [], []
        for block in turn["blocks"]:
            if block["type"] == "text":
                parts.append({"type": "text", "text": block["text"]})
            elif block["type"] == "image":
                parts.append({"type": "image_url",
                              "image_url": {"url": data_url(block)}})
            elif block["type"] == "tool_result":
                results.append({"role": "tool", "tool_call_id": block["id"],
                                "content": block["content"]})
        msgs.extend(results)        # a result answers the call just above it
        if parts or not results:    # ... and a turn of results is no message
            if all(part["type"] == "text" for part in parts):
                parts = "\n\n".join(part["text"] for part in parts)
            msgs.append({"role": turn["role"], "content": parts})
    body = {"model": model["model_name"], "messages": msgs, "stream": True,
            "stream_options": {"include_usage": True}}
    body.update(params)
    return body


def parse(event, acc):
    """Return the (text, kind) of one delta event and record usage when it appears.

    Reasoning deltas -- DeepSeek, vLLM, OpenRouter and friends put them in
    delta.reasoning_content -- come back tagged "thinking" so the caller can
    paint them differently from the answer, and are kept out of the assistant
    message that is replayed next turn.

    >>> acc = {}
    >>> parse({"choices": [{"delta": {"content": "hi"}}]}, acc)
    ('hi', '')
    >>> parse({"choices": [{"delta": {"reasoning_content": "hmm"}}]}, acc)
    ('hmm', 'thinking')
    >>> parse({"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1}}, acc)
    ('', '')
    >>> acc["usage"]
    {'in': 3, 'out': 1}
    >>> parse({"choices": [], "service_tier": "flex"}, acc)
    ('', '')
    >>> acc["tier"]
    'flex'
    >>> parse({"choices": [], "usage": {"prompt_tokens": 3, "cost": 0.0245}}, acc)
    ('', '')
    >>> acc["usage"], acc["cost"]
    ({'in': 3}, 0.0245)
    """
    if event.get("error"):
        # a failure reported as a data frame rather than as a status code
        error = event["error"]
        acc["error"] = (f"{error.get('type') or 'error'}: {error.get('message')}"
                        if isinstance(error, dict) else str(error))
        return "", ""
    if event.get("service_tier"):
        # the tier is a fact about the response, so a price is read from here
        # and never from the request options that were sent
        acc["tier"] = event["service_tier"]
    if event.get("provider"):
        # a router (OpenRouter) names the upstream it chose; a direct
        # provider sends no such field, so this costs it nothing
        acc["provider"] = event["provider"]
    usage = event.get("usage")
    if usage:
        acc["usage"] = openai_usage(usage)
        if usage.get("cost") is not None:
            # a dollar amount only when the provider charged one: absent means
            # the amount is not known, and 0.0 is a charge like any other
            acc["cost"] = float(usage["cost"])
    text, kind = "", ""
    for choice in event.get("choices") or []:
        if choice.get("finish_reason"):
            acc["stop"] = choice["finish_reason"]
        delta = choice.get("delta") or {}
        if delta.get("content"):
            text += delta["content"]
        else:
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning:
                text, kind = text + reasoning, "thinking"
        for piece in delta.get("tool_calls") or []:
            # the arguments of one call arrive as a JSON string in pieces, one
            # piece at a time; they are accumulated verbatim so that the call
            # replayed next round is the call the server made
            calls = acc.setdefault("tool_calls", [])
            slot = piece.get("index", 0)
            while len(calls) <= slot:
                calls.append({"type": "function", "function": {"arguments": ""}})
            function = piece.get("function") or {}
            if piece.get("id"):
                calls[slot]["id"] = piece["id"]
            if function.get("name"):
                calls[slot]["function"]["name"] = function["name"]
            if function.get("arguments"):
                calls[slot]["function"]["arguments"] += function["arguments"]
    if text and kind != "thinking":
        acc.setdefault("text", []).append(text)
    return text, kind


def calls(acc):
    """The tool calls this round asked for, as {id, name, arguments}.

    The wire shape stays in acc["tool_calls"] for the replay; this is the
    same list read into what a python function takes, so arguments that are
    not JSON are an error here rather than a call with no arguments at all.

    >>> acc = {}
    >>> parse({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1",
    ...     "function": {"name": "ls", "arguments": '{"path"'}}]}}]}, acc)
    ('', '')
    >>> parse({"choices": [{"delta": {"tool_calls": [{"index": 0,
    ...     "function": {"arguments": ': "."}'}}]}}]}, acc)
    ('', '')
    >>> calls(acc)
    [{'id': 'c1', 'name': 'ls', 'arguments': {'path': '.'}}]
    """
    out = []
    for call in acc.get("tool_calls") or []:
        function = call.get("function") or {}
        arguments = function.get("arguments") or "{}"
        try:
            arguments = json.loads(arguments)
        except ValueError as e:
            raise DicError(f"{function.get('name')}: its arguments are not"
                           f" JSON: {e}") from None
        out.append({"id": call.get("id"), "name": function.get("name"),
                    "arguments": arguments})
    return out


def finish(acc):
    """The assistant message to replay next turn, its tool calls included.

    >>> finish({"text": ["ab", "c"]})
    {'role': 'assistant', 'content': 'abc'}
    >>> finish({"tool_calls": [{"id": "c1"}]})["tool_calls"]
    [{'id': 'c1'}]
    """
    message = {"role": "assistant", "content": "".join(acc.get("text", []))}
    if acc.get("tool_calls"):
        message["tool_calls"] = acc["tool_calls"]
    return message
