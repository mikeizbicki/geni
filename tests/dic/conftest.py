"""Fixtures for dic's integration tests: a HOME of its own, a real HTTP server
on 127.0.0.1, and streams whose isatty() a test controls.

Only the network is faked, so a test runs the code a user runs -- config file,
sqlite, request body, streamed events, the row -- and asserts on what came out.
"""
import collections, http.server, io, json, os, subprocess, sys, threading

import pytest

# so `pytest tests/` works from a checkout, the way `pytest src/` does;
# tests/dic sits two levels below the root that holds src/
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   os.pardir, os.pardir, "src")
sys.path.insert(0, SRC)

from dic.store import config_dir


class Stream(io.StringIO):
    """A stream that says it is a terminal when a test says it is.

    dic asks a stream two questions: whether to colour it, and (for stderr)
    how much to say on it.  Both answers are policy, so both need a stream on
    each side of the branch.
    """

    def __init__(self, tty=False):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


class Streams:
    """The two streams a dic() call writes: `out` is the answer, `err` is stderr."""

    def __init__(self, tty=False):
        self.out, self.err = Stream(tty), Stream(tty)


@pytest.fixture
def stream():
    """A fresh fake stream: `stream()` is a pipe, `stream(tty=True)` a terminal."""
    return Stream


@pytest.fixture
def streams():
    """The out/err pair to hand to dic(); set `.err.tty` to take the other branch."""
    return Streams()


@pytest.fixture
def env(tmp_path):
    """An environment pointing every path dic writes at a throwaway directory.

    DIC_PRETTY=0 is here and not in each test's call: the small answers
    these tests assert on read better compact, a JSON answer is pretty by
    default in a shell, and one variable at the fixture is the whole
    difference.  A test that wants the default pops it.
    """
    home = tmp_path / "home"
    home.mkdir()
    return {"HOME": str(home),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
            "DIC_SESSION": "test",
            "DIC_PRETTY": "0"}


Request = collections.namedtuple("Request", "path headers body")


class _Handler(http.server.BaseHTTPRequestHandler):
    """Answer every POST with the server's status and the server's events."""

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length)
        self.server.received.append(
            Request(self.path, dict(self.headers), json.loads(body or b"{}")))
        if self.server.status == 200:
            # a str is sent verbatim, so a fixture can name the [DONE] the
            # protocol's own wire carries; a dict is a message dic serializes
            payload = b"".join(
                b"data: " + (event.encode() if isinstance(event, str)
                             else json.dumps(event).encode()) + b"\n\n"
                for event in self.server.events)
        else:
            payload = self.server.error.encode()
        self.send_response(self.server.status)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass                    # the test's assertions are the log


@pytest.fixture
def sse(events):
    """A real HTTP server that replays canned SSE and keeps every request.

    `sse.events` is what it streams (build one with the `events` fixture),
    `sse.status` and `sse.error` are what it answers when a test wants a
    failure, and `sse.received` is one Request per call -- path, headers and
    parsed body -- so a test can assert what dic sent, not only what it said.
    """
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    server.api_base = f"http://127.0.0.1:{server.server_port}"
    server.status, server.error, server.events = 200, "overloaded", events()
    server.received = []
    threading.Thread(target=server.serve_forever,
                     kwargs={"poll_interval": 0.01}, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def events():
    """(text, api_type) -> the SSE events that stream that text over that protocol."""

    def build(text="hello", api_type="openai-chat"):
        if api_type == "anthropic-messages":
            return [{"type": "message_start",
                     "message": {"usage": {"input_tokens": 1000,
                                           "output_tokens": 0}}},
                    {"type": "content_block_start",
                     "content_block": {"type": "text", "text": ""}},
                    {"type": "content_block_delta",
                     "delta": {"type": "text_delta", "text": text}},
                    {"type": "content_block_stop"},
                    {"type": "message_delta", "usage": {"output_tokens": 500}},
                    {"type": "message_stop"}]
        return [{"choices": [{"delta": {"content": text}}]},
                {"choices": [], "usage": {"prompt_tokens": 1000,
                                          "completion_tokens": 500}},
                "[DONE]"]
    return build


@pytest.fixture
def models(env, sse):
    """Write the test's models.json: `fake`, then one entry per other protocol.

    Each of the others inherits its provider's api_base and key, so they are
    also the proof that a model inherits what it does not state; the three of
    them are what the protocol conformance tests parametrize over, and the
    prices are what the cache and tier tests need a model to state.
    """
    directory = config_dir(env)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "models.json"), "w") as f:
        json.dump({"fake": {"model_name": "fake-1",
                            "api_base": sse.api_base,
                            "api_key_name": "FAKE_API_KEY",
                            "cost_input": 3.0, "cost_output": 15.0,
                            # a cheaper rate for the batch tier, so a call
                            # the response billed at the default rate must
                            # not be rated at this one
                            "price": {"in.batch": {"rate": 1.5,
                                                   "key": "in",
                                                   "tier": "batch"}}},
                   # 5m only: a TTL the model does not price is an error, and
                   # in.cache_write.5m is the breakpoint 5m puts on the wire
                   "fake+anthropic": {"api_type": "anthropic-messages",
                                      "price": {"in.cache_write.5m":
                                                {"rate": 2.5}}},
                   "fake+responses": {"api_type": "openai-responses"},
                   # a protocol with no output_format field, so the schema
                   # tests can assert the refusal before the network, and a
                   # call that needs --path can be tested for the same
                   # reason
                   "fake+images": {"api_type": "openai-images",
                                   "model_name": "gpt-image-1",
                                   "output": "image/png"},
                   # typesafe: its own dialect and its own wire shape, so
                   # the refusals are what a test can reach without a
                   # second server; every schema test here refuses early
                   "fake+typesafe": {"api_type": "typesafe",
                                     "model_name": "jev-latest"}}, f)
    env["FAKE_API_KEY"] = "test-key"


@pytest.fixture
def model(models):
    """The id of the model a test uses unless it names another one."""
    return "fake"


@pytest.fixture
def dic_run(env):
    """Run one real `dic` process against this test's environment.

    An in-process dic() call cannot see startup: pytest has imported every
    module before the test runs.  A test that measures the process therefore
    starts one the way a user does, `python -m dic`.  PYTHONPATH is the same
    checkout `sys.path` above points at, and python_flags=("-S",) is how a
    test asks whether the standard library alone is enough to start.
    """
    def run(*argv, python_flags=()):
        return subprocess.run(
            [sys.executable, *python_flags, "-m", "dic", *argv],
            env={**os.environ, **env, "PYTHONPATH": SRC},
            stdin=subprocess.DEVNULL, capture_output=True, check=True)
    return run
