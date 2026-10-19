#!/usr/bin/env python3
"""A one-tool MCP server on stdin/stdout, one JSON-RPC message per line.

This is the fixture the stdio test runs, and it is a real server in the
only sense a stdio MCP server is: read a line, dispatch on the method,
write a line back.  It carries one tool, `echo`, which answers with its
arguments, and its description is whatever `ECHO_ENV` says, so a test can
prove the child inherited dic's environment.  Nothing here is dic's: it is
small on purpose, because a fixture this shape is a stand-in for a server
and not for a protocol.
"""
import json
import os
import sys


VERSION = "2025-06-18"


def reply(message_id, result):
    """One JSON-RPC response, as one line of stdout."""
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message_id,
                                 "result": result}) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue                    # a line that is not a message
        method = message.get("method")
        message_id = message.get("id")
        if method == "initialize":
            reply(message_id, {"protocolVersion": VERSION,
                               "capabilities": {},
                               "serverInfo": {"name": "echo", "version": "1"}})
        elif method == "tools/list":
            reply(message_id, {"tools": [{
                "name": "echo",
                "description": os.environ.get("ECHO_ENV", "Echo arguments."),
                "inputSchema": {"type": "object",
                                "properties": {"text": {"type": "string"}}}}]})
        elif method == "tools/call":
            args = (message.get("params") or {}).get("arguments") or {}
            reply(message_id, {"content": [{"type": "text",
                                            "text": json.dumps(args)}]})
        elif message_id is not None:
            reply(message_id, {})       # a method this server does not know


if __name__ == "__main__":
    main()
