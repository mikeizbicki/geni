#!/usr/bin/env python3
"""mcp-bash -- one shell tool, spoken over MCP's stdio transport.

`dic --mcp mcp-bash` runs this, and dic wraps every --mcp command in
the sandbox it ships (see dic.shell.command), so this file runs no
sandbox of its own and knows nothing about one.  What it is is a
transport: one JSON-RPC message to a line of stdout, one shell command
per tools/call, and no dependency beyond the standard library.
Importing the `mcp` package would cost more at every invocation than
the whole of this file costs to run.

The tool's `command` argument is a string and is run by `/bin/sh -c`,
because it is a model calling a tool and not a user typing at a shell:
the quoting is the model's, and a pipe or a substitution between two
commands is a program the model is asking to write rather than a
target that dic's own word-list rule refuses.  stdout is the wire, so
everything a command printed goes into the tool result and anything
this file itself has to say goes to stderr, which dic drains and
reports when the server is gone.

See scripts/AGENTS.md for why every --mcp command is sandboxed, and
src/dic/mcp.py for the client side of the same conversation.
"""
import json, subprocess, sys

VERSION = "2025-06-18"

TOOL = {
    "name": "bash",
    "description": ("Run a shell command with /bin/sh -c.  The command"
                    " runs in the sandbox dic started this server in:"
                    " the checkout is read-only and the environment is"
                    " empty.  stdout and stderr are both returned."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "command": {"type": "string",
                        "description": "The shell command line to run."},
            "timeout": {"type": "integer",
                        "description": "Seconds to wait, capped at 600."
                                       " Defaults to 60."}},
        "required": ["command"],
        "additionalProperties": False,
    },
}

DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600


def run(arguments):
    """One tools/call as an MCP result: the command's output, and isError.

    A command that exits nonzero is not a protocol failure: its status
    and its output are the result, because a model that reads "exit 1"
    can correct itself and one that reads an exception cannot.  A
    timeout is the same shape for the same reason.

    >>> run({"command": "echo hi"})["content"][0]["text"]
    'hi'
    >>> run({"command": "exit 3"})["isError"]
    True
    >>> run({"command": "echo a; echo b >&2"})["content"][0]["text"]
    'a\\nb'
    >>> run({"command": "true"})["content"][0]["text"]
    '(no output)'
    """
    seconds = min(max(int(arguments.get("timeout") or DEFAULT_TIMEOUT), 1),
                  MAX_TIMEOUT)
    try:
        proc = subprocess.run(  # noqa: S603
            ["/bin/sh", "-c", arguments["command"]], capture_output=True,
            text=True, timeout=seconds)
    except subprocess.TimeoutExpired:
        return {"content": [{"type": "text",
                             "text": f"timeout after {seconds}s"}],
                "isError": True}
    text = (proc.stdout + proc.stderr).rstrip()
    if not text:
        text = "(no output)"
    return {"content": [{"type": "text", "text": text}],
            "isError": proc.returncode != 0}


def handle(message):
    """One JSON-RPC message, answered: (result, error).

    Exactly one of the two is not None.  A method this server does not
    know is an error and never a silent empty result, because a client
    that asked for something and was told nothing happened cannot tell
    a server that does not support it from one that failed.

    >>> handle({"method": "initialize"})[0]["serverInfo"]["name"]
    'mcp-bash'
    >>> handle({"method": "tools/list"})[0]["tools"][0]["name"]
    'bash'
    >>> handle({"method": "tools/call",
    ...         "params": {"arguments": {"command": "echo hi"}}})[0]["content"]
    [{'type': 'text', 'text': 'hi'}]
    >>> handle({"method": "nope"})[1]["code"]
    -32601
    """
    method = message.get("method")
    if method == "initialize":
        return {"protocolVersion": VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mcp-bash",
                               "version": VERSION}}, None
    if method == "ping":
        return {}, None
    if method == "tools/list":
        return {"tools": [TOOL]}, None
    if method == "tools/call":
        return run(message.get("params", {}).get("arguments") or {}), None
    return None, {"code": -32601, "message": f"no such method: {method}"}


def respond(message):
    """The line of json to write for one message, or None for a notification.

    A notification carries no id and is answered by nothing at all: a
    reply would be a message the client never asked for, and it would
    have no id to carry.

    >>> respond({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    True
    >>> respond({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    '{"jsonrpc": "2.0", "id": 1, "result": {}}'
    """
    if "id" not in message:
        return None
    try:
        result, error = handle(message)
    except Exception as e:
        result, error = None, {"code": -32603, "message": str(e)}
    out = {"jsonrpc": "2.0", "id": message["id"]}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    return json.dumps(out)


def main():
    """The stdio loop: one JSON message per line of stdin, one reply per line.

    A line that is not JSON is reported on stderr and skipped rather
    than answered, because it was not a message and a reply to one
    would be a message of this file's own invention.  stdout is
    flushed after every line because the client is blocked reading it:
    a buffered reply is a client that has stopped answering, which is
    what dic.mcp's handshake deadline reports as a server that never
    started.
    """
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError as e:
            print(f"mcp-bash: not JSON: {e}: {line[:200]}", file=sys.stderr)
            continue
        reply = respond(message)
        if reply is not None:
            print(reply, flush=True)


if __name__ == "__main__":
    main()
