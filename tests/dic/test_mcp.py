"""MCP: a real JSON-RPC server, over a pipe and over a POST.

mcp.py's schema arithmetic is doctested where it lives.  What a doctest
cannot reach is a tool that arrives over a pipe or a POST, so this file
brings a real MCP server -- a child process and an HTTP handler -- and a
chat-completions endpoint that answers per request, because a tool loop is
two requests and one answer and conftest's `sse` gives both requests the
same one.  What these tests assert on is what a user sees: the tools the
model was offered, the row that was written, the message the second round
carried back, and the spec the row stored.
"""
import base64, collections, http.server, json, os, shlex, shutil, subprocess
import sys, threading
import urllib.parse
import time as _time

import pytest

from dic.client import dic
from dic.store import config_dir, db
from dic.tty import DicError


ECHO = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    os.pardir, "fixtures", "mcp", "echo.py")

Hit = collections.namedtuple("Hit", "headers body")

def _jail_works():
    """Whether a nested jail can be made here: `sandbox pytest` refuses one.

    bwrap being installed is not the same as a nested bwrap being permitted:
    pytest is usually run inside `sandbox pytest`, and an outer sandbox
    installs a filter that refuses the `unshare` and `mount` a nested jail
    is made of, so the probe is a real jail and not the binary's name.
    What dic hands bwrap is testable anywhere; what bwrap does with it needs
    a jail that can actually be made.
    """
    if not shutil.which("bwrap"):
        return False
    probe = ["bwrap", "--unshare-all", "--ro-bind", "/", "/", "--", "true"]
    try:
        subprocess.run(probe, check=True, capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


# The packaged sandbox is bwrap, so every test of a stdio server needs a jail
# that can actually be made: a machine without bwrap cannot run one at all,
# and a nested one inside `sandbox pytest` is refused before it starts, so
# both cases are a skip rather than a failure of the test's own assertion.
NEEDS_BWRAP = pytest.mark.skipif(
    not _jail_works(), reason="a nested jail cannot be made here")


# ------------------------------------------------------- the replies, in full


def text_reply(text):
    """The chat-completions events of one round that ends with an answer."""
    return [{"choices": [{"delta": {"content": text}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 10,
                                      "completion_tokens": 3}}]


def tool_reply(name, arguments, call_id="call_1"):
    """The chat-completions events of one round that ends by asking for a tool."""
    return [{"choices": [{"delta": {"tool_calls": [{
        "index": 0, "id": call_id,
        "function": {"name": name,
                     "arguments": json.dumps(arguments)}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 10,
                                      "completion_tokens": 5}}]


# ------------------------------------------------------------ the two servers


class _LLM(http.server.BaseHTTPRequestHandler):
    """A chat-completions endpoint that answers from a list, one per request.

    A tool loop is two requests with two answers, so `replies` is a list of
    event lists and not one list: the fixture pops the next one per request,
    and repeats the last when there are more requests than replies.  `bodies`
    is every request that arrived, in order, because what dic sent is what
    the tools blob and the replayed row are about.
    """

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        self.server.bodies.append(
            json.loads(self.rfile.read(length) or b"{}"))
        events = self.server.replies[min(len(self.server.bodies) - 1,
                                         len(self.server.replies) - 1)]
        payload = b"".join(b"data: " + json.dumps(event).encode() + b"\n\n"
                           for event in events)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass                    # the test's assertions are the log


class _MCP(http.server.BaseHTTPRequestHandler):
    """A JSON-RPC MCP server: initialize, tools/list, tools/call over one POST.

    The three methods are what mcp.py's HTTP transport speaks, and every one
    of them is answered here.  `received` is one Hit per request -- headers
    and parsed body -- so a test can assert what dic sent, not only what came
    back; `fail` makes tools/call answer with isError, which is how the tool
    loop's failure path is reached without killing the server.  The header
    names are lowercased, because HTTP header names are case-insensitive and
    urllib capitalizes what it sends.
    """

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.received.append(
            Hit({k.lower(): v for k, v in self.headers.items()}, body))
        payload = json.dumps({"jsonrpc": "2.0", "id": body.get("id"),
                              "result": self.dispatch(body)}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def dispatch(self, body):
        """The result of one request, by the method it names."""
        method = body.get("method")
        if method == "initialize":
            return {"protocolVersion": "2025-06-18", "capabilities": {},
                    "serverInfo": {"name": "fake", "version": "1"}}
        if method == "tools/list":
            return {"tools": self.server.tools}
        if method == "tools/call":
            if self.server.fail:
                return {"isError": True,
                        "content": [{"type": "text", "text": "boom"}]}
            args = (body.get("params") or {}).get("arguments") or {}
            return {"content": [{"type": "text", "text": json.dumps(args)}],
                    "structuredContent": args}
        return {}

    def log_message(self, *args):
        pass


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def llm():
    """The chat-completions endpoint dic talks to, one reply per request."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _LLM)
    server.api_base = f"http://127.0.0.1:{server.server_port}"
    server.replies = [text_reply("hello")]
    server.bodies = []
    threading.Thread(target=server.serve_forever,
                     kwargs={"poll_interval": 0.01}, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def mcp_server():
    """An MCP server with one tool, `echo`, that answers with its arguments."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _MCP)
    server.url = f"http://127.0.0.1:{server.server_port}/mcp"
    server.tools = [{"name": "echo", "description": "Echo arguments.",
                     "inputSchema": {"type": "object",
                                     "properties": {"text": {"type": "string"}}}}]
    server.fail = False
    server.received = []
    threading.Thread(target=server.serve_forever,
                     kwargs={"poll_interval": 0.01}, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def models(env, llm):
    """The test's models.json: one model, pointing at the fake LLM.

    A fixture of the same name as conftest's, and that is on purpose: this
    file brings its own LLM, so conftest's `sse` -- one event list for every
    request -- is not the endpoint to point at.
    """
    directory = config_dir(env)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "models.json"), "w") as f:
        json.dump({"fake": {"model_name": "fake-1", "api_base": llm.api_base,
                            "api_key_name": "FAKE_API_KEY"}}, f)
    env["FAKE_API_KEY"] = "test-key"


@pytest.fixture
def venv(monkeypatch):
    """VIRTUAL_ENV set to the interpreter running the tests.

    A stdio MCP server is started through dic's sandbox, and sandbox()
    binds only $PWD and $VIRTUAL_ENV.  Run from inside `sandbox
    ./test.sh`, the outer jail has already cleared the environment, so
    without this the interpreter's own path is not in the map and
    bwrap's execvp fails before the child starts.
    """
    monkeypatch.setenv(
        "VIRTUAL_ENV", os.path.dirname(os.path.dirname(sys.executable)))


def say(streams, env, prompt, **knobs):
    """One dic() call through the test's streams, environment and servers."""
    return dic(prompt, env=env, out=streams.out, err=streams.err, **knobs)


# ------------------------------------------------------------------ the wire


def test_the_model_is_offered_the_servers_tools(streams, env, models, llm,
                                                mcp_server):
    """The name is namespaced and the schema is the server's own, on the wire."""
    say(streams, env, "hi", model="fake", mcp=[mcp_server.url])

    (offered,) = llm.bodies[0]["tools"]
    assert offered["type"] == "function"
    assert offered["function"]["name"] == "mcp__127-0-0-1__echo"
    assert offered["function"]["parameters"] == \
        mcp_server.tools[0]["inputSchema"]


def test_a_tool_call_reaches_the_server_and_its_result_reaches_the_model(
        streams, env, models, llm, mcp_server):
    """One loop: the model asks, the server answers, the model reads it."""
    llm.replies = [tool_reply("mcp__127-0-0-1__echo", {"text": "hi"}),
                   text_reply("done")]
    say(streams, env, "hi", model="fake", mcp=[mcp_server.url])

    calls = [h for h in mcp_server.received
             if h.body.get("method") == "tools/call"]
    assert len(calls) == 1
    assert calls[0].body["params"] == {"name": "echo",
                                       "arguments": {"text": "hi"}}

    (result,) = [m for m in llm.bodies[1]["messages"]
                 if m.get("role") == "tool"]
    assert result["tool_call_id"] == "call_1"
    assert "hi" in result["content"]


def test_a_dead_server_is_a_tool_result_not_a_failed_call(streams, env,
                                                          models, llm,
                                                          mcp_server):
    """A server's isError is the model's to read, and the call still ends."""
    mcp_server.fail = True
    llm.replies = [tool_reply("mcp__127-0-0-1__echo", {}),
                   text_reply("done")]
    reply = say(streams, env, "hi", model="fake", mcp=[mcp_server.url])

    assert reply.status == 200
    conn = db(env)
    row = conn.execute("SELECT tool_results FROM messages").fetchone()
    conn.close()
    (result,) = json.loads(row["tool_results"])
    assert result["ok"] is False
    assert "boom" in result["error"]


def test_continue_re_offers_the_rows_tools_without_opening_the_server(
        streams, env, models, llm, mcp_server):
    """A row's tools are replayed and no server is reached to get them again."""
    say(streams, env, "hi", model="fake", mcp=[mcp_server.url])
    opened = len(mcp_server.received)

    llm.replies = [text_reply("again")]
    say(streams, env, "more", model="fake", cont=True)

    assert [t["function"]["name"] for t in llm.bodies[1]["tools"]] == \
        ["mcp__127-0-0-1__echo"]
    assert len(mcp_server.received) == opened


@NEEDS_BWRAP
def test_a_servers_own_assignment_is_set_and_dic_own_env_is_not(
        streams, env, models, llm, monkeypatch, venv):
    """A NAME=value sets what the child reads; what dic exported, it does not.

    The sandbox clears the environment, so `--mcp 'K=v cmd'` is the whole of
    how a server gets a secret now, and the second assertion is why that is
    safe: an API key dic was started with is not a variable the server can
    read, and what it reads is what the caller wrote on its own line.
    """
    monkeypatch.setenv("ECHO_ENV", "from-dic")
    say(streams, env, "hi", model="fake",
        mcp_shell=[f"ECHO_ENV=set-here {shlex.quote(sys.executable)}"
                   f" {shlex.quote(ECHO)}"])

    (offered,) = llm.bodies[0]["tools"]
    name = offered["function"]["name"]
    assert name.startswith("mcp__") and name.endswith("__echo")
    description = offered["function"]["description"]
    assert "set-here" in description
    assert "from-dic" not in description


def test_a_urls_userinfo_becomes_basic_auth(streams, env, models, llm,
                                            mcp_server):
    """A credential rides in the URL and is stripped from what is POSTed."""
    parts = urllib.parse.urlsplit(mcp_server.url)
    secret = f"http://user:pass@{parts.netloc}{parts.path}"
    say(streams, env, "hi", model="fake", mcp=[secret])

    assert mcp_server.received[0].headers["authorization"] == \
        "Basic " + base64.b64encode(b"user:pass").decode()

    # the spec -- the secret's own URL -- is what the row stores, because it
    # is what the row was given and dic keeps no credential of its own
    conn = db(env)
    row = conn.execute("SELECT mcp FROM messages").fetchone()
    conn.close()
    assert json.loads(row["mcp"]) == [["url", secret]]


@NEEDS_BWRAP
def test_a_stdio_server_is_jailed_and_only_its_own_words_widen_the_jail(
        streams, env, models, llm, venv):
    """dic puts the sandbox in front itself; `--share-net` is the one knob.

    The child does not have to speak MCP for this to be worth asserting:
    what the test reads is what the child itself reports, and the host it
    dials is the fake LLM's own listener -- so a connection that answers is
    one the jail let through and one that is refused is one it stopped.  The
    second half is the control the first half needs: a jail that was never
    there at all would read exactly like one that held.
    """
    port = urllib.parse.urlsplit(llm.api_base).port
    report = ("import socket, sys\n"
              "try:\n"
              f"    socket.create_connection(('127.0.0.1', {port}), timeout=3)\n"
              "    print('network: reached', file=sys.stderr)\n"
              "except OSError:\n"
              "    print('network: blocked', file=sys.stderr)\n")
    child = f"{shlex.quote(sys.executable)} -c {shlex.quote(report)}"

    # the child exits without answering, so the handshake is what fails and
    # the child's own report is what the error carries
    with pytest.raises(DicError) as jailed:
        say(streams, env, "hi", model="fake", mcp_shell=[child])
    assert "network: blocked" in str(jailed.value)

    with pytest.raises(DicError) as shared:
        say(streams, env, "hi", model="fake",
            mcp_shell=[f"--share-net {child}"])
    assert "network: reached" in str(shared.value)


@NEEDS_BWRAP
def test_a_server_that_will_not_start_refuses_a_fresh_call(
        streams, env, models, llm):
    """A fresh call cannot be offered part of the servers it named."""
    streams.err.tty = True                   # a terminal gets the warning
    try:
        say(streams, env, "hi", model="fake",
            mcp_shell=["definitely-not-a-command-xyz"])
    except Exception as e:
        assert "definitely-not-a-command-xyz" in str(e)
        return
    raise AssertionError("a dead server did not refuse the call")

    assert streams.out.getvalue() == "hello\n"
    assert "definitely-not-a-command-xyz" in streams.err.getvalue()
    assert "tools" not in llm.bodies[0]      # no server answered, nothing offered


@NEEDS_BWRAP
def test_the_servers_are_opened_at_once_and_not_one_after_another(
        streams, env, models, llm, venv):
    """A slow server must not delay the ones behind it: four handshakes, one wait.

    The command sleeps before it answers `initialize`, so three of them in
    series would take three sleeps and a parallel opening takes one.  The
    bound is generous -- a factor of two against the serial time -- because
    the assertion is "not serialized" and not "as fast as possible on a
    loaded machine"; the exact cost is the child processes' own.
    """
    # a one-tool MCP server whose every answer costs one sleep: the sleep
    # is what the test measures, so it comes before the first byte the
    # server sends and not only before the first tool is called.  print
    # with flush=True, because a reply that stays in the child's buffer
    # is indistinguishable from a server that never answered.
    script = (
        "import json, sys, time\n"
        "time.sleep(0.4)\n"
        "for line in sys.stdin:\n"
        "    if not line.strip():\n"
        "        continue\n"
        "    req = json.loads(line)\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': req.get('id'),\n"
        "        'result': {'protocolVersion': '2025-06-18',\n"
        "            'capabilities': {},\n"
        "            'serverInfo': {'name': 'slow', 'version': '1'}}}),\n"
        "        flush=True)\n")
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    start = _time.monotonic()
    say(streams, env, "hi", model="fake",
        mcp_shell=[command] * 3)
    elapsed = _time.monotonic() - start

    # three sleeps if serial, one if parallel; the LLM call and dic's own
    # startup are well inside the slack the factor of two leaves
    assert elapsed < 0.4 * 2, f"three handshakes took {elapsed:.2f}s"
